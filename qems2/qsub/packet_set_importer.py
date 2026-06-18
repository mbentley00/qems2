"""Create a new QuestionSet ("tournament") from uploaded packet files.

Each uploaded file becomes one packet. Three input formats are supported:

* **.json** &mdash; the JSON produced by YetAnotherPacketParser / MODAQ
  (https://github.com/alopezlago/YetAnotherPacketParser). This is the most
  reliable path: questions are already cleanly separated with HTML formatting,
  so we just convert that HTML to QEMS markup. Each question's category is read
  from its ``metadata`` ("Author, Category - Subcategory") and a distribution is
  built from the categories seen.
* **.docx / .pdf** &mdash; best-effort plain-text parsing (standard ACF layout:
  numbered tossups + ``ANSWER:`` lines, numbered bonuses with ``[10]`` parts).
  Real packets vary a lot, so prefer the JSON path when available.

Unparseable questions are skipped and reported with a readable reason.

Admin-only; wired up in views.import_packets.
"""

import io
import json
import os
import re

from bs4 import BeautifulSoup, NavigableString
from django.db import transaction
from django.db.models.signals import post_save
from django.utils import timezone
from django.utils.html import escape

from qems2.qsub.models import (QuestionSet, Distribution, DistributionEntry,
                               SetWideDistributionEntry, Packet, Tossup, Bonus,
                               QuestionType)
from qems2.qsub.packet_parser import parse_packet_data, is_answer, is_bpart, ansregex
from qems2.qsub.utils import (QUESTION_CREATE, ACF_STYLE_TOSSUP, ACF_STYLE_BONUS,
                              InvalidTossup, InvalidBonus)
from qems2.qsub import signals as qems_signals


SUPPORTED_EXTS = ('.json', '.docx', '.pdf')

# Section headers / page furniture that are not question content (docx/pdf).
_NOISE_RE = re.compile(
    r'^\s*(tossups?|bonus(es)?|round\s*\d*|packet\s*\d*|extras?\b.*|'
    r'page\s*\d+|\d+|first\s*half|second\s*half|half\s*\w*)\s*[:.]?\s*$',
    re.IGNORECASE)

# Leading question number, e.g. "1. " or "12) ".
_NUM_PREFIX_RE = re.compile(r'^\s*\d{1,3}[\.\)]\s+')

# Same, but tolerant of leading QEMS markup: packets often bold the question
# number (and the lead-in), so the line starts with an opening '_' or '~'. The
# capture keeps that markup so stripping the number doesn't unbalance it.
_NUM_PREFIX_MARKUP_RE = re.compile(r'^([_~]*\s*)\d{1,3}[\.\)]\s+')


def _strip_markup(s):
    """Drop QEMS markup characters so we can test the underlying text."""
    return (s.replace('\\S', '').replace('\\s', '')
             .replace('_', '').replace('~', '').strip())


def _strip_num_prefix(s):
    """Remove a leading '1. '/'2) ' question number, preserving any leading
    markup so bold/underline spans stay balanced."""
    return _NUM_PREFIX_MARKUP_RE.sub(r'\1', s, count=1)


def _has_num_prefix(s):
    return bool(_NUM_PREFIX_RE.match(_strip_markup(s)))

# HTML tag -> formatting flag it sets while in scope.
_TAG_FLAGS = {'b': 'bold', 'strong': 'bold', 'u': 'ul', 'em': 'ital',
              'i': 'ital', 'sup': 'sup', 'sub': 'sub'}


class PacketImportError(Exception):
    pass


# --- HTML (YAPP) -> QEMS markup -------------------------------------------
#
# QEMS markup (see utils.get_formatted_question_html):
#   _x_   -> bold+underline  (required answer)
#   __x__ -> underline only  (prompt / alternate answer)
#   ~x~   -> italics
#   \Sx\S -> superscript     \sx\s -> subscript
#   (*)   -> power marker (kept verbatim; the renderer bolds up to it)

def _wrap(text, fmt, is_answer):
    """Wrap one text run in QEMS markup based on the active formatting flags.
    Leading/trailing whitespace is kept outside the markers."""
    if not text.strip():
        return text
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    core = text.strip()

    if fmt['ul'] and fmt['bold']:
        u_o = u_c = '_'
    elif fmt['ul']:
        u_o = u_c = '__'
    elif fmt['bold'] and is_answer:
        # Bold without underline in an answer is still the required answer.
        u_o = u_c = '_'
    else:
        # Bold in question text marks the power region, which QEMS derives from
        # the (*) marker, so it carries no markup here.
        u_o = u_c = ''
    i_o = i_c = '~' if fmt['ital'] else ''
    sup_o = sup_c = '\\S' if fmt['sup'] else ''
    sub_o = sub_c = '\\s' if fmt['sub'] else ''

    return lead + u_o + i_o + sup_o + sub_o + core + sub_c + sup_c + i_c + u_c + trail


def _walk(node, fmt, out, is_answer):
    for child in node.children:
        if isinstance(child, NavigableString):
            out.append(_wrap(str(child), fmt, is_answer))
            continue
        name = (child.name or '').lower()
        if name == 'br':
            out.append(' ')
            continue
        flag = _TAG_FLAGS.get(name)
        child_fmt = fmt if flag is None else dict(fmt, **{flag: True})
        _walk(child, child_fmt, out, is_answer)


def _html_to_qems(html, is_answer):
    """Convert YAPP's sanitized HTML to QEMS markup, then escape literal
    angle brackets/ampersands so they render as text (markup chars are left
    intact)."""
    if not html:
        return ''
    soup = BeautifulSoup(html, 'html.parser')
    out = []
    _walk(soup, {'bold': False, 'ul': False, 'ital': False, 'sup': False, 'sub': False},
          out, is_answer)
    text = re.sub(r'\s+', ' ', ''.join(out)).strip()
    return escape(text)


# --- metadata ("Author, Category - Subcategory") --------------------------

def _split_metadata(meta):
    """Return (author, category, subcategory). Only treats text after the first
    comma as the category (YAPP packets that put just an author there, with no
    comma, leave the question uncategorized rather than inventing a category)."""
    meta = (meta or '').strip()
    if not meta or ',' not in meta:
        return meta, '', ''
    author, cat = meta.split(',', 1)
    author, cat = author.strip(), cat.strip()
    if ' - ' in cat:
        category, subcategory = cat.split(' - ', 1)
        return author, category.strip(), subcategory.strip()
    return author, cat, ''


def _tally_category(counts, meta, idx):
    _author, cat, sub = _split_metadata(meta)
    if cat:
        counts.setdefault((cat, sub), [0, 0])[idx] += 1


def _build_distribution_from_metadata(qset, payloads):
    """Create DistributionEntry + SetWideDistributionEntry rows from the
    categories seen across all YAPP payloads. Returns {(cat, sub): entry}."""
    counts = {}
    for payload in payloads:
        for t in payload.get('tossups') or []:
            _tally_category(counts, t.get('metadata'), 0)
        for b in payload.get('bonuses') or []:
            _tally_category(counts, b.get('metadata'), 1)
    lookup = {}
    for (cat, sub), (n_tu, n_bs) in counts.items():
        entry = DistributionEntry.objects.create(
            distribution=qset.distribution, category=cat, subcategory=sub)
        SetWideDistributionEntry.objects.create(
            question_set=qset, dist_entry=entry, num_tossups=n_tu, num_bonuses=n_bs)
        lookup[(cat, sub)] = entry
    return lookup


# --- YAPP question builders ------------------------------------------------

def _yapp_category(meta, lookup):
    _author, cat, sub = _split_metadata(meta)
    return lookup.get((cat, sub)) if cat else None


def _build_tossup_from_yapp(t, qset, owner, acf_type, lookup):
    return Tossup(
        question_set=qset,
        tossup_text=_html_to_qems(t.get('question', ''), is_answer=False),
        tossup_answer=_html_to_qems(t.get('answer', ''), is_answer=True),
        category=_yapp_category(t.get('metadata'), lookup),
        author=owner, question_type=acf_type, locked=False, edited=False)


def _build_bonus_from_yapp(b, qset, owner, acf_type, lookup):
    parts = b.get('parts') or []
    answers = b.get('answers') or []
    diffs = b.get('difficultyModifiers') or []

    def part(i):
        return _html_to_qems(parts[i], is_answer=False) if i < len(parts) else ''

    def ans(i):
        return _html_to_qems(answers[i], is_answer=True) if i < len(answers) else ''

    def diff(i):
        d = str(diffs[i]).strip().lower() if i < len(diffs) and diffs[i] else ''
        return d if d in ('e', 'm', 'h') else ''

    return Bonus(
        question_set=qset,
        leadin=_html_to_qems(b.get('leadin', ''), is_answer=False)[:500],
        part1_text=part(0), part1_answer=ans(0), part1_difficulty=diff(0),
        part2_text=part(1), part2_answer=ans(1), part2_difficulty=diff(1),
        part3_text=part(2), part3_answer=ans(2), part3_difficulty=diff(2),
        category=_yapp_category(b.get('metadata'), lookup),
        author=owner, question_type=acf_type, locked=False, edited=False)


def _parse_yapp(payload, qset, owner, acf_tu, acf_bn, lookup, name, summary):
    """Build (unsaved) Tossup/Bonus objects from a YAPP payload. Per-question
    failures are recorded and skipped."""
    tossups, bonuses = [], []
    for i, t in enumerate(payload.get('tossups') or []):
        try:
            tossups.append(_build_tossup_from_yapp(t, qset, owner, acf_tu, lookup))
        except Exception as ex:
            summary['errors'].append('{0}: tossup #{1} skipped ({2})'.format(name, i + 1, ex))
    for i, b in enumerate(payload.get('bonuses') or []):
        try:
            bonuses.append(_build_bonus_from_yapp(b, qset, owner, acf_bn, lookup))
        except Exception as ex:
            summary['errors'].append('{0}: bonus #{1} skipped ({2})'.format(name, i + 1, ex))
    return tossups, bonuses


# --- docx/pdf text extraction ---------------------------------------------

def _wrap_run_text(text, run):
    """Wrap a single run's text segment in QEMS markup from its formatting."""
    if not text.strip():
        return text
    pre = suf = ''
    if run.underline or run.bold:
        pre, suf = '_' + pre, suf + '_'
    if run.italic:
        pre, suf = '~' + pre, suf + '~'
    return pre + text + suf


def _para_lines_to_qems(para):
    """A docx paragraph often holds several logical lines separated by soft line
    breaks (``<w:br/>`` -> ``\\n`` in the run text) — real packets put a tossup's
    stem, its ``ANSWER:`` line and its ``<Author, Category>`` metadata all in one
    paragraph. Split on those breaks so each becomes its own line, applying run
    formatting per segment. Returns a list of QEMS-markup lines."""
    if not para.runs:
        return [seg.strip() for seg in (para.text or '').split('\n')]
    lines = ['']
    for run in para.runs:
        segments = (run.text or '').split('\n')
        for i, seg in enumerate(segments):
            if i > 0:
                lines.append('')
            lines[-1] += _wrap_run_text(seg, run)
    return [ln.strip() for ln in lines]


def _docx_lines(stream):
    from docx import Document
    doc = Document(stream)
    lines = []
    for p in doc.paragraphs:
        lines.extend(_para_lines_to_qems(p))
    return lines


def _pdf_lines(stream):
    from pypdf import PdfReader
    reader = PdfReader(stream)
    raw = []
    for page in reader.pages:
        raw.extend((page.extract_text() or '').split('\n'))
    return _coalesce_wrapped(raw)


def _starts_logical_line(s):
    return bool(_NUM_PREFIX_RE.match(s)) or is_answer(s) or is_bpart(s)


def _coalesce_wrapped(lines):
    """Merge soft-wrapped PDF lines back into one line per question component.
    Blank lines and section headers act as hard breaks so a continuation can't
    be glued onto the previous question."""
    out = []
    force_new = True
    for ln in lines:
        s = (ln or '').strip()
        if not s or _NOISE_RE.match(s):
            force_new = True
            continue
        if force_new or not out or _starts_logical_line(s):
            out.append(s)
        else:
            out[-1] = out[-1] + ' ' + s
        force_new = False
    return out


def _ensure_underline(ansline):
    if '_' in ansline:
        return ansline
    m = re.match(ansregex, ansline)
    prefix = m.group(0) if m else ''
    body = ansline[len(prefix):]
    cut = len(body)
    for delim in ('[', '('):
        idx = body.find(delim)
        if idx != -1:
            cut = min(cut, idx)
    primary = body[:cut].rstrip()
    if not primary:
        return ansline
    return prefix + '_' + primary + '_' + body[len(primary):]


def _is_meta_line(s):
    """A trailing attribution line such as
    ``<Author, Category - Subcategory> ~id~ <Editor: Name>``."""
    s = s.strip()
    return s.startswith('<') and '>' in s


def _meta_category(s):
    """Return (category, subcategory) from a metadata line, or None if it
    carries no category (e.g. ``<Author>`` with no comma)."""
    inner = s.strip()[1:s.strip().index('>')]
    if ',' not in inner:
        return None
    _author, cat, sub = _split_metadata(inner)
    return (cat, sub) if cat else None


def _category_tag(cat, sub):
    # parse_packet_data matches against "category - subcategory" (see
    # create_tossup), so always emit both halves joined by " - ".
    return '{' + cat + ' - ' + sub + '}'


def _normalize_lines(lines):
    """Back-compat: normalized lines only (used by diagnostics)."""
    return _normalize_docx_lines(lines)[0]


def _normalize_docx_lines(lines):
    """Turn raw docx/pdf lines into the stem/ANSWER/[10x] line stream that
    parse_packet_data expects, and pull out per-question categories.

    * Section headers / page furniture and the leading tournament-title lines
      (anything before the first numbered question) are dropped.
    * Each question's ``<Author, Category - Subcategory>`` metadata line is
      removed and its category folded into the preceding answer as a
      ``{Category - Subcategory}`` tag, so the questions get categorized.

    Returns ``(cleaned_lines, categories)`` where ``categories`` is the set of
    ``(category, subcategory)`` pairs seen."""
    cleaned = []
    categories = set()
    last_answer_idx = None
    started = False
    for ln in lines:
        s = (ln or '').strip()
        if not s or _NOISE_RE.match(s):
            continue
        if _is_meta_line(s):
            cat_sub = _meta_category(s)
            if cat_sub:
                categories.add(cat_sub)
                if last_answer_idx is not None and '{' not in cleaned[last_answer_idx]:
                    cleaned[last_answer_idx] += ' ' + _category_tag(*cat_sub)
            continue
        if not started:
            # Skip the tournament title / front matter until the first question.
            if not (_has_num_prefix(s) or is_answer(s) or is_bpart(s)):
                continue
            started = True
        if is_answer(s):
            cleaned.append(_ensure_underline(s))
            last_answer_idx = len(cleaned) - 1
        elif is_bpart(s):
            cleaned.append(s)
        else:
            cleaned.append(_strip_num_prefix(s))
    return cleaned, categories


def _extract_lines_from_bytes(data, ext):
    if ext == '.docx':
        return _docx_lines(io.BytesIO(data))
    if ext == '.pdf':
        return _pdf_lines(io.BytesIO(data))
    raise PacketImportError('Unsupported file type for line extraction: {0}'.format(ext))


def _extract_lines(uploaded_file):
    """Back-compat helper (used by diagnostics): extract normalized lines from
    an uploaded docx/pdf."""
    ext = os.path.splitext((uploaded_file.name or '').lower())[1]
    return _extract_lines_from_bytes(uploaded_file.read(), ext)


# --- shared helpers --------------------------------------------------------

def _packet_name_from_file(filename):
    base = os.path.splitext(os.path.basename(filename or 'Packet'))[0]
    base = re.sub(r'[_]+', ' ', base).strip()
    return (base or 'Packet')[:200]


def _describe_parse_error(ex):
    """Turn an InvalidTossup/InvalidBonus (whose str() is HTML) into a short,
    readable one-line reason for the user."""
    if isinstance(ex, (InvalidTossup, InvalidBonus)):
        args = list(ex.args)
        field = args[0] if len(args) > 0 else '?'
        value = str(args[1]) if len(args) > 1 else ''
        number = args[2] if len(args) > 2 else '?'
        kind = 'tossup' if isinstance(ex, InvalidTossup) else 'bonus'
        snippet = (value[:80] + '…') if len(value) > 80 else value
        reason = {
            'answer': 'answer has no underlined required portion (or unbalanced _ / ~)',
            'answers': 'an answer has no underlined required portion (or unbalanced _ / ~)',
            'question': 'question has unbalanced _ / ~ markup',
            'leadin': 'missing or unbalanced bonus leadin',
            'parts': 'a bonus part is empty or has unbalanced markup',
            'category': 'category did not match the set distribution',
        }.get(field, 'problem with {0}'.format(field))
        return '{0} #{1}: {2} — "{3}"'.format(kind, number, reason, snippet)
    return str(ex)


def _save_questions(questions, qset, packet, owner, kind, summary):
    """Save parsed Tossup/Bonus instances into a packet with sequential
    numbering. Each is its own savepoint. Returns the number saved."""
    saved = 0
    for q in questions:
        try:
            with transaction.atomic():
                q.question_set = qset
                q.packet = packet
                q.author = owner
                q.question_number = saved + 1
                q.locked = False
                q.edited = False
                q.save_question(edit_type=QUESTION_CREATE, changer=owner)
            saved += 1
        except Exception as ex:
            summary['errors'].append('{0}: {1} #{2} could not be saved ({3})'.format(
                packet.packet_name, kind, saved + 1, ex))
    return saved


# --- import ----------------------------------------------------------------

def _prepare_files(uploaded_files):
    """Validate and read each uploaded file once; pre-parse JSON and extract +
    normalize docx/pdf lines (so a single read covers category discovery and
    parsing). Returns (prepared_items, json_payloads, docx_categories)."""
    files = list(uploaded_files)
    if not files:
        raise PacketImportError('No files were uploaded.')
    for f in files:
        ext = os.path.splitext((f.name or '').lower())[1]
        if ext not in SUPPORTED_EXTS:
            raise PacketImportError(
                'Unsupported file "{0}". Only .json, .docx and .pdf are accepted.'.format(f.name))
    files.sort(key=lambda f: (f.name or '').lower())

    prepared, json_payloads, docx_categories = [], [], set()
    for f in files:
        ext = os.path.splitext((f.name or '').lower())[1]
        name = _packet_name_from_file(f.name)
        raw = f.read()
        if ext == '.json':
            try:
                payload = json.loads(raw.decode('utf-8-sig'))
            except Exception as ex:
                prepared.append({'name': name, 'ext': ext, 'error': 'invalid JSON ({0})'.format(ex)})
                continue
            json_payloads.append(payload)
            prepared.append({'name': name, 'ext': ext, 'payload': payload})
        else:
            try:
                lines, categories = _normalize_docx_lines(_extract_lines_from_bytes(raw, ext))
            except Exception as ex:
                prepared.append({'name': name, 'ext': ext, 'error': 'could not read file ({0})'.format(ex)})
                continue
            docx_categories |= categories
            prepared.append({'name': name, 'ext': ext, 'lines': lines})
    return prepared, json_payloads, docx_categories


def _ensure_categories(qset, categories, lookup=None):
    """Create DistributionEntry + SetWideDistributionEntry rows for any
    (category, subcategory) not already in the set's distribution. Updates and
    returns `lookup` if given."""
    if lookup is None:
        lookup = {(e.category, e.subcategory): e
                  for e in DistributionEntry.objects.filter(distribution=qset.distribution)}
    for (cat, sub) in categories:
        if (cat, sub) in lookup:
            continue
        entry = DistributionEntry.objects.create(
            distribution=qset.distribution, category=cat, subcategory=sub)
        SetWideDistributionEntry.objects.create(
            question_set=qset, dist_entry=entry, num_tossups=0, num_bonuses=0)
        lookup[(cat, sub)] = entry
    return lookup


def _unique_packet_name(name, used):
    """Avoid colliding with packets already in the set (or earlier files in this
    run) by appending ' (2)', ' (3)', ..."""
    if name not in used:
        return name
    i = 2
    while '{0} ({1})'.format(name, i) in used:
        i += 1
    return '{0} ({1})'.format(name, i)


def _lookup_for_existing_set(qset, json_payloads):
    """Map (category, subcategory) -> DistributionEntry for the set's existing
    distribution, extended with any new categories seen in the JSON metadata
    (added to the set's distribution so the imported questions can be filed)."""
    lookup = {(e.category, e.subcategory): e
              for e in DistributionEntry.objects.filter(distribution=qset.distribution)}
    needed = set()
    for payload in json_payloads:
        for q in (payload.get('tossups') or []) + (payload.get('bonuses') or []):
            _a, cat, sub = _split_metadata(q.get('metadata'))
            if cat and (cat, sub) not in lookup:
                needed.add((cat, sub))
    for (cat, sub) in needed:
        entry = DistributionEntry.objects.create(
            distribution=qset.distribution, category=cat, subcategory=sub)
        SetWideDistributionEntry.objects.create(
            question_set=qset, dist_entry=entry, num_tossups=0, num_bonuses=0)
        lookup[(cat, sub)] = entry
    return lookup


def import_packets_into_set(uploaded_files, qset, owner):
    """Add packets (one per uploaded file) to an EXISTING question set."""
    return import_packets_from_files(uploaded_files, owner=owner, existing_qset=qset)


def import_packets_from_files(uploaded_files, set_name=None, owner=None, existing_qset=None):
    """Import packet files (.json/.docx/.pdf), one Packet per file. Creates a new
    QuestionSet named `set_name`, or — when `existing_qset` is given — adds the
    packets to that set. Returns a summary dict; raises PacketImportError on a
    fatal problem."""
    prepared, json_payloads, docx_categories = _prepare_files(uploaded_files)

    summary = {'packets': [], 'tossups': 0, 'bonuses': 0, 'errors': []}

    email_receivers = [
        (qems_signals.email_on_new_tossup, Tossup),
        (qems_signals.email_on_new_bonus, Bonus),
    ]
    for receiver, sender in email_receivers:
        post_save.disconnect(receiver, sender=sender)

    try:
        with transaction.atomic():
            if existing_qset is None:
                distribution = Distribution.objects.create(name='{0} (imported)'.format(set_name)[:100])
                qset = QuestionSet.objects.create(
                    name=set_name, date=timezone.now().date(), host='', address='',
                    owner=owner, num_packets=len([p for p in prepared if 'error' not in p]) or 1,
                    distribution=distribution)
                owner.question_set_editor.add(qset)
                category_lookup = _build_distribution_from_metadata(qset, json_payloads)
                _ensure_categories(qset, docx_categories, category_lookup)
                used_names = set()
            else:
                qset = existing_qset
                category_lookup = _lookup_for_existing_set(qset, json_payloads)
                _ensure_categories(qset, docx_categories, category_lookup)
                used_names = set(p.packet_name for p in qset.packet_set.all())

            acf_tu = QuestionType.objects.filter(question_type=ACF_STYLE_TOSSUP).first()
            acf_bn = QuestionType.objects.filter(question_type=ACF_STYLE_BONUS).first()

            for item in prepared:
                name = _unique_packet_name(item['name'], used_names)
                used_names.add(name)
                packet = Packet.objects.create(
                    question_set=qset, packet_name=name, created_by=owner)

                if item.get('error'):
                    summary['errors'].append('{0}: {1}'.format(name, item['error']))
                    summary['packets'].append({'name': name, 'tossups': 0, 'bonuses': 0, 'parse_errors': 1})
                    continue

                try:
                    if item['ext'] == '.json':
                        tossups, bonuses = _parse_yapp(
                            item['payload'], qset, owner, acf_tu, acf_bn, category_lookup, name, summary)
                        parse_errors = 0
                    else:
                        tossups, bonuses, t_errs, b_errs = parse_packet_data(item['lines'], qset)
                        for e in list(t_errs) + list(b_errs):
                            summary['errors'].append('{0}: {1}'.format(name, _describe_parse_error(e)))
                        parse_errors = len(t_errs) + len(b_errs)
                except Exception as ex:
                    summary['errors'].append('{0}: could not read file ({1})'.format(name, ex))
                    summary['packets'].append({'name': name, 'tossups': 0, 'bonuses': 0, 'parse_errors': 0})
                    continue

                n_tu = _save_questions(tossups, qset, packet, owner, 'tossup', summary)
                n_bs = _save_questions(bonuses, qset, packet, owner, 'bonus', summary)
                summary['tossups'] += n_tu
                summary['bonuses'] += n_bs
                summary['packets'].append({
                    'name': name, 'tossups': n_tu, 'bonuses': n_bs, 'parse_errors': parse_errors})

            if existing_qset is None:
                # A brand-new import with no bonuses at all is tossup-only.
                if summary['tossups'] > 0 and summary['bonuses'] == 0:
                    qset.tossups_only = True
                    qset.save(update_fields=['tossups_only'])
            else:
                # Grow the packet count by however many packets we added.
                qset.num_packets = (qset.num_packets or 0) + len(prepared)
                qset.save(update_fields=['num_packets'])

            summary['question_set'] = qset
            summary['added_to_existing'] = existing_qset is not None
    finally:
        for receiver, sender in email_receivers:
            post_save.connect(receiver, sender=sender)

    return summary
