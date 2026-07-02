"""Compare a question set against a *previous* set (e.g. 2027 NSC vs 2026 NSC)
to catch repeats. The previous set is supplied as uploaded packet files
(.json/.docx/.pdf) — the same formats the packet importer accepts — parsed in
memory without being saved.

The three repeat signals we flag, in priority order:
  * a hard bonus part whose answer matches an answer in the old set (worst);
  * an unusual/distinctive answer line shared with the old set;
  * a tossup leadin / early clue that closely matches an old tossup's.
"""

import re

from .duplicate_checker import normalize_answer, extract_clue_words, clue_similarity
from .utils import strip_markup, get_primary_answer, get_answer_no_formatting

CRITICAL = 'critical'
WARNING = 'warning'
INFO = 'info'
_SEV_ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}

# Tuning: a leadin/early-clue match needs both a decent overlap fraction and a
# minimum number of shared content words, so short generic leadins don't match.
_LEADIN_JACCARD = 0.4
_LEADIN_MIN_SHARED = 4


def _answer_preview(raw):
    return get_answer_no_formatting(get_primary_answer(raw or '')).strip()


def _is_distinctive(norm):
    """A normalized answer worth flagging on an exact match: multi-word, or long.
    Filters out ubiquitous one-word answers (e.g. 'france') that coincide by
    chance rather than being a genuine repeat."""
    if not norm:
        return False
    return norm.count(' ') >= 1 or len(norm) >= 12


def _first_clue(text):
    """The tossup's leadin — its first sentence of plain text."""
    plain = strip_markup(text or '').strip()
    parts = re.split(r'(?<=[.!?])\s+', plain, maxsplit=1)
    return parts[0] if parts else plain


def _hard_part_index(diffs, parts):
    """Index of the hard part: an explicit 'h' difficulty, else the last
    non-empty part (hard parts conventionally come third)."""
    for i, d in enumerate(diffs):
        if (d or '').strip().lower() == 'h':
            return i
    nonempty = [i for i, p in enumerate(parts) if (p or '').strip()]
    return nonempty[-1] if nonempty else 0


def parse_previous_questions(uploaded_files, current_qset):
    """Parse uploaded previous-set packets into lightweight dicts. Returns
    (tossups, bonuses, errors). No database writes."""
    from .packet_set_importer import _prepare_files, _html_to_qems, _split_answer_category
    from .packet_parser import parse_packet_data, remove_category, remove_answer_label

    prepared, _payloads, _cats = _prepare_files(uploaded_files)
    tossups, bonuses, errors = [], [], []

    def _json_answer(raw):
        # Strip a trailing category tag and any leading "ANSWER:" label so the
        # answer normalizes the same way as the current set's stored answers.
        return remove_answer_label(_html_to_qems(_split_answer_category(raw or '')[0], is_answer=True))

    for item in prepared:
        pname = item.get('name', '?')
        if item.get('error'):
            errors.append('{0}: {1}'.format(pname, item['error']))
            continue

        if item['ext'] == '.json':
            for t in item['payload'].get('tossups') or []:
                tossups.append({'packet': pname, 'answer': _json_answer(t.get('answer', '')),
                                'text': _html_to_qems(t.get('question', ''), is_answer=False)})
            for b in item['payload'].get('bonuses') or []:
                answers = [_json_answer(a) for a in (b.get('answers') or [])]
                parts = [_html_to_qems(p, is_answer=False) for p in (b.get('parts') or [])]
                diffs = [str(d or '').lower() for d in (b.get('difficultyModifiers') or [])]
                bonuses.append({'packet': pname, 'leadin': _html_to_qems(b.get('leadin', ''), is_answer=False),
                                'answers': answers, 'parts': parts, 'diffs': diffs})
        else:
            # docx/pdf: reuse the packet parser. Strip category tags first so a
            # category that doesn't exist in the current set can't fail validation
            # and drop the question.
            lines = [remove_category(ln) for ln in item['lines']]
            try:
                tus, bos, _te, _be = parse_packet_data(lines, current_qset)
            except Exception as ex:
                errors.append('{0}: could not parse ({1})'.format(pname, ex))
                continue
            for tu in tus:
                tossups.append({'packet': pname, 'answer': tu.tossup_answer, 'text': tu.tossup_text})
            for bo in bos:
                bonuses.append({'packet': pname, 'leadin': bo.leadin,
                                'answers': [bo.part1_answer, bo.part2_answer, bo.part3_answer],
                                'parts': [bo.part1_text, bo.part2_text, bo.part3_text],
                                'diffs': [bo.part1_difficulty or '', bo.part2_difficulty or '',
                                          bo.part3_difficulty or '']})
    return tossups, bonuses, errors


def _build_prev_index(prev_tossups, prev_bonuses):
    """Index the previous set for lookup: answer -> occurrences (marking hard
    bonus parts), plus tossup leadin word-sets."""
    answers = {}   # normalized answer -> list of {packet, preview, hard}
    leadins = []   # {packet, answer_preview, words, text}

    def add_answer(norm, packet, raw, hard=False):
        if not norm:
            return
        answers.setdefault(norm, []).append(
            {'packet': packet, 'preview': _answer_preview(raw), 'hard': hard})

    for t in prev_tossups:
        add_answer(normalize_answer(t['answer']), t['packet'], t['answer'])
        words = extract_clue_words(_first_clue(t['text']))
        if words:
            leadins.append({'packet': t['packet'], 'answer_preview': _answer_preview(t['answer']),
                            'words': words, 'text': _first_clue(t['text'])})

    for b in prev_bonuses:
        parts = b.get('parts') or []
        diffs = b.get('diffs') or []
        hard_i = _hard_part_index(diffs, parts)
        for i, ans in enumerate(b.get('answers') or []):
            add_answer(normalize_answer(ans), b['packet'], ans, hard=(i == hard_i))

    return answers, leadins


def compare(current_qset, prev_tossups, prev_bonuses):
    """Compare the current set's saved questions against the parsed previous set.
    Returns a list of finding dicts (most severe first)."""
    from .models import Tossup, Bonus

    prev_answers, prev_leadins = _build_prev_index(prev_tossups, prev_bonuses)
    findings = []

    def loc(q, qtype):
        pkt = q.packet.packet_name if q.packet else 'Unpacketed'
        return '{0} · {1} #{2}'.format(qtype, pkt, q.question_number or '?')

    def edit_url(q, qtype):
        return '/edit_{0}/{1}/'.format(qtype, q.id)

    tossups = list(Tossup.objects.filter(question_set=current_qset)
                   .select_related('packet').order_by('packet__packet_name', 'question_number'))
    bonuses = list(Bonus.objects.filter(question_set=current_qset)
                   .select_related('packet').order_by('packet__packet_name', 'question_number'))

    for tu in tossups:
        matches = []
        norm = normalize_answer(tu.tossup_answer)
        if _is_distinctive(norm) and norm in prev_answers:
            for m in prev_answers[norm]:
                matches.append({'kind': 'answer', 'severity': WARNING,
                                'detail': 'Same answer in {0}'.format(m['packet']),
                                'preview': m['preview']})
        # Leadin / early-clue overlap.
        words = extract_clue_words(_first_clue(tu.tossup_text))
        if words:
            best = None
            for pl in prev_leadins:
                shared = words & pl['words']
                if len(shared) >= _LEADIN_MIN_SHARED:
                    j = clue_similarity(words, pl['words'])
                    if j >= _LEADIN_JACCARD and (best is None or j > best[0]):
                        best = (j, pl)
            if best is not None:
                j, pl = best
                matches.append({'kind': 'leadin', 'severity': WARNING,
                                'detail': 'Leadin ~{0:.0f}% shared with {1} ({2})'.format(
                                    100 * j, pl['packet'], pl['answer_preview']),
                                'preview': pl['text'][:160]})
        if matches:
            findings.append({
                'qtype': 'tossup', 'location': loc(tu, 'Tossup'), 'edit_url': edit_url(tu, 'tossup'),
                'answer_preview': _answer_preview(tu.tossup_answer),
                'severity': min((m['severity'] for m in matches), key=lambda s: _SEV_ORDER[s]),
                'matches': matches})

    for bo in bonuses:
        matches = []
        parts = [bo.part1_text, bo.part2_text, bo.part3_text]
        answers = [bo.part1_answer, bo.part2_answer, bo.part3_answer]
        diffs = [bo.part1_difficulty or '', bo.part2_difficulty or '', bo.part3_difficulty or '']
        hard_i = _hard_part_index(diffs, parts)
        for i, ans in enumerate(answers):
            if not (ans or '').strip():
                continue
            norm = normalize_answer(ans)
            if not (_is_distinctive(norm) and norm in prev_answers):
                continue
            is_hard = (i == hard_i)
            # A hard part matching *any* old answer is the most serious; extra so
            # if it matched an old hard part too.
            prev_hits = prev_answers[norm]
            prev_hard = any(h['hard'] for h in prev_hits)
            sev = CRITICAL if is_hard else WARNING
            label = 'Hard part' if is_hard else 'Part {0}'.format(i + 1)
            detail = '{0} answer repeats {1}'.format(label, prev_hits[0]['packet'])
            if is_hard and prev_hard:
                detail += ' (also a hard part there)'
            matches.append({'kind': 'hard-part' if is_hard else 'answer', 'severity': sev,
                            'detail': detail, 'preview': prev_hits[0]['preview']})
        if matches:
            findings.append({
                'qtype': 'bonus', 'location': loc(bo, 'Bonus'), 'edit_url': edit_url(bo, 'bonus'),
                'answer_preview': ' / '.join(filter(None, (
                    _answer_preview(a) for a in answers))),
                'severity': min((m['severity'] for m in matches), key=lambda s: _SEV_ORDER[s]),
                'matches': matches})

    findings.sort(key=lambda f: _SEV_ORDER[f['severity']])
    return findings
