"""Export packetized questions to the JSON format produced by YetAnotherPacketParser
(YAPP, https://github.com/alopezlago/YetAnotherPacketParser), which is what the
MODAQ online reader consumes.

The YAPP JSON shape (camelCase keys, text fields are HTML):

    {
      "tossups": [
        {"number": 1, "question": "<html>", "answer": "<html>", "metadata": "..."}
      ],
      "bonuses": [
        {"number": 1, "leadin": "<html>", "parts": ["<html>", ...],
         "answers": ["<html>", ...], "values": [10, 10, 10],
         "difficultyModifiers": ["e", "m", "h"], "metadata": "..."}
      ]
    }

YAPP marks bold/underline/italic/super/subscript with ``<b> <u> <em> <sup> <sub>``
and keeps the power marker ``(*)`` as literal text in the question (the reader
scores the power buzz from it). The region up to the marker is *also* written as
bold, because that's how it comes out of a Word packet and MODAQ renders the bold
it is given rather than inferring any — see ``bold_power_region``. The importer
drops bold in question text (QEMS derives the power region from the marker), so
the export still round-trips through this app's own YAPP importer.

The one place the two models differ is an **all-power** tossup, which QEMS marks
with a flag and no ``(*)`` at all. Written out as-is it would score no power
anywhere, so the export appends the marker after the last word — see
``all_power_tail``.

YAPP2
-----
Plain YAPP has nowhere to record *which words a pronunciation guide covers* —
QEMS tracks that with ``\\Pword\\P`` markers (see ``utils`` and the ``.pg-target``
styling), and a YAPP export has to throw it away. YAPP2 is a backward-compatible
superset that keeps it. See ``YAPP2_FORMAT.md`` at the repo root for the spec; in
brief:

* a top-level ``"version": "yapp2/<major>.<minor>"`` marks the file;
* the canonical YAPP fields are **unchanged** — byte-identical to what a plain
  YAPP export writes, so every existing consumer reads a YAPP2 file correctly;
* each question may carry an ``"anchored"`` object holding the same fields with
  ``<pg>...</pg>`` around the anchored word(s). Readers that understand YAPP2
  prefer those; readers that don't never see the tag.
* an optional top-level ``"readingOrder"`` (1.1) says the packet is read
  interlaced — tossup, bonus, tossup, bonus — rather than all tossups then all
  bonuses. It only points at the canonical arrays; it never moves a question.

``<pg>`` marks question *words*, not a guide: they are read aloud, count as
words, and are buzzable. Only the parenthesized guide itself is non-word text.
"""

import html as _html
import re as _re

#: Value of the top-level ``version`` field on a YAPP2 packet. Minor bumps only
#: add optional fields, and readers match on the ``yapp2/`` prefix, so raising
#: this doesn't strand anything that read the previous version.
#: 1.1 added ``readingOrder``.
YAPP2_VERSION = 'yapp2/1.1'


# QEMS markup (see utils.get_formatted_question_html) mapped to YAPP's HTML tags:
#   _x_   -> <b><u>x</u></b>   (required answer: bold + underline)
#   __x__ -> <u>x</u>          (prompt / alternate answer: underline only)
#   ~x~   -> <em>x</em>        (italics)
#   \Sx\S -> <sup>x</sup>      \sx\s -> <sub>x</sub>
#   \Bx\B -> <b>x</b>          (explicit bold)
#   (*)   -> kept verbatim     (power marker)
#   \Px\P -> dropped, or <pg>x</pg> when anchors=True (YAPP2 only)
#   \_ \~ \( \) -> literal characters
def qems_to_yapp_html(text, anchors=False):
    """Convert one field of QEMS markup to the HTML YAPP emits. Non-markup text
    is passed through unchanged (it is already stored as safe HTML — angle
    brackets arrive pre-escaped as ``&lt;``/``&gt;``), matching how the app
    renders questions elsewhere.

    With ``anchors=True`` the pronunciation-guide target markers ``\\P`` become
    ``<pg>``/``</pg>`` (YAPP2). By default they are dropped, which is what plain
    YAPP requires."""
    if not text:
        return ''

    out = []
    ital = ul = prompt = sup = sub = bold = pg = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ''

        # Backslash escapes: drop the backslash, emit the literal / toggle format.
        if c == '\\' and nxt in ('_', '~', '(', ')', '\\'):
            out.append(nxt)
            i += 2
            continue
        if c == '\\' and nxt == 'S':
            out.append('</sup>' if sup else '<sup>')
            sup = not sup
            i += 2
            continue
        if c == '\\' and nxt == 's':
            out.append('</sub>' if sub else '<sub>')
            sub = not sub
            i += 2
            continue
        if c == '\\' and nxt == 'B':
            out.append('</b>' if bold else '<b>')
            bold = not bold
            i += 2
            continue
        if c == '\\' and nxt == 'P':
            # Pronunciation-guide target marker: which word(s) the guide that
            # follows covers. Plain YAPP has no way to say this, so it is
            # dropped; YAPP2 carries it as <pg>.
            if anchors:
                out.append('</pg>' if pg else '<pg>')
                pg = not pg
            i += 2
            continue
        if c == '\\' and nxt == 'N':
            # A note to the moderator/players. It is read aloud, so the words
            # stay; only the marker goes. YAPP has no field for "this is an
            # instruction", and inventing markup here would show up as literal
            # text in a reader.
            i += 2
            continue

        # Power markers stay as literal text ((+) superpower, (*) power).
        if text[i:i + 3] == '(*)' or text[i:i + 3] == '(+)':
            out.append(text[i:i + 3])
            i += 3
            continue

        if c == '~':
            out.append('</em>' if ital else '<em>')
            ital = not ital
            i += 1
            continue

        if c == '_':
            if nxt == '_':                      # prompt: underline only
                out.append('</u>' if prompt else '<u>')
                prompt = not prompt
                i += 2
                continue
            # required answer: bold + underline
            out.append('</u></b>' if ul else '<b><u>')
            ul = not ul
            i += 1
            continue

        out.append(c)
        i += 1

    # Close anything left open so the fragment is well-formed.
    if ital:
        out.append('</em>')
    if ul:
        out.append('</u></b>')
    if prompt:
        out.append('</u>')
    if sup:
        out.append('</sup>')
    if sub:
        out.append('</sub>')
    if bold:
        out.append('</b>')
    if pg:
        out.append('</pg>')
    return ''.join(out)


def _metadata(question, tags=None):
    """Build YAPP's ``metadata`` string (shown by the reader after the answer)
    from the question's author and category, as ``Author, Category - Subcategory``,
    followed by ``[Tag, Tag]`` when the set prints its category tags.
    Any piece that is missing is dropped."""
    author = ''
    if getattr(question, 'author', None) is not None:
        try:
            author = _html.unescape(question.author.get_real_name() or '').strip()
        except Exception:
            author = ''
    cat = ''
    if getattr(question, 'category', None) is not None:
        c = (question.category.category or '').strip()
        s = (question.category.subcategory or '').strip()
        cat = '{0} - {1}'.format(c, s) if s else c
    if author and cat:
        head = '{0}, {1}'.format(author, cat)
    else:
        head = cat or author
    if tags:
        return '{0} [{1}]'.format(head, ', '.join(tags)).strip()
    return head


def _anchored(node, fields):
    """Attach an ``anchored`` object to a YAPP2 question node.

    ``fields`` maps a field name to (plain, anchored) text — or, for the bonus
    arrays, to (plain list, anchored list). Only fields whose anchored form
    actually differs are included, and the object is omitted entirely when no
    field carries an anchor, so YAPP2 costs nothing on questions without one.
    Arrays are emitted in full so a reader can index them alongside the
    canonical ones."""
    extra = {}
    for name, (plain, anchored) in fields.items():
        if anchored != plain:
            extra[name] = anchored
    if extra:
        node['anchored'] = extra
    return node


def all_power_tail(tossup):
    """``' (*)'`` for an all-power tossup that carries no marker of its own,
    else ``''``.

    QEMS records a whole-stem power as a flag, and the *absence* of a ``(*)`` is
    part of how it's detected (a stem with a marker is by definition not
    all-power). YAPP has no such flag: a reader finds the power boundary from
    the literal marker, so exporting the stem as written would score every buzz
    as a plain 10. A marker after the last word says the same thing in YAPP's
    terms — everything before it is power, and that's the whole question.
    """
    is_all_power = getattr(tossup, 'is_all_power', None)
    if not callable(is_all_power) or not is_all_power():
        return ''
    return '' if '(*)' in (tossup.tossup_text or '') else ' (*)'


_TAG_RE = _re.compile(r'</?([a-z]+)>')


def bold_power_region(html):
    """Wrap everything through the last power marker in ``<b>``.

    A YAPP file made from a Word packet carries the power region as bold runs,
    because that's how the region is written in the document — and MODAQ renders
    the bold it is given rather than inferring any from the ``(*)`` marker, which
    it uses only to score the buzz. QEMS stores the marker alone and bolds at
    render time, so an export without this reads as an unpowered question.

    Inline tags still open at the marker are closed before the ``</b>`` and
    reopened after it, so a run of italics spanning the power boundary doesn't
    produce crossed tags."""
    idx = max(html.rfind('(*)'), html.rfind('(+)'))
    if idx == -1:
        return html
    head, tail = html[:idx + 3], html[idx + 3:]
    open_tags = []
    for m in _TAG_RE.finditer(head):
        name = m.group(1)
        if m.group(0).startswith('</'):
            if name in open_tags:
                open_tags.reverse()
                open_tags.remove(name)
                open_tags.reverse()
        else:
            open_tags.append(name)
    closing = ''.join('</{0}>'.format(t) for t in reversed(open_tags))
    reopen = ''.join('<{0}>'.format(t) for t in open_tags)
    return '<b>{0}{1}</b>{2}{3}'.format(head, closing, reopen, tail)


def tossup_to_yapp(tossup, number, version=1, tags=None):
    text = tossup.tossup_text or ''
    answer = tossup.tossup_answer or ''
    # Appended after conversion so it lands outside any markup, and to both
    # copies alike — an anchored field may differ from its canonical twin only
    # by <pg> tags.
    tail = all_power_tail(tossup)
    node = {
        'number': number,
        'question': bold_power_region(qems_to_yapp_html(text) + tail),
        'answer': qems_to_yapp_html(answer),
        'metadata': _metadata(tossup, tags),
    }
    if version < 2:
        return node
    return _anchored(node, {
        'question': (node['question'],
                     bold_power_region(qems_to_yapp_html(text, anchors=True) + tail)),
        'answer': (node['answer'], qems_to_yapp_html(answer, anchors=True)),
    })


def bonus_to_yapp(bonus, number, version=1, tags=None):
    parts, answers, values, diffs = [], [], [], []
    a_parts, a_answers = [], []
    for i in range(1, 4):
        text = getattr(bonus, 'part{0}_text'.format(i), '') or ''
        if not text.strip():
            continue
        answer = getattr(bonus, 'part{0}_answer'.format(i), '') or ''
        diff = (getattr(bonus, 'part{0}_difficulty'.format(i), '') or '').strip().lower()
        parts.append(qems_to_yapp_html(text))
        answers.append(qems_to_yapp_html(answer))
        a_parts.append(qems_to_yapp_html(text, anchors=True))
        a_answers.append(qems_to_yapp_html(answer, anchors=True))
        values.append(10)
        diffs.append(diff if diff in ('e', 'm', 'h') else None)

    leadin = bonus.leadin or ''
    node = {
        'number': number,
        'leadin': qems_to_yapp_html(leadin),
        'parts': parts,
        'answers': answers,
        'values': values,
        'metadata': _metadata(bonus, tags),
    }
    # Only emit difficultyModifiers when at least one part carries one (YAPP
    # makes the field optional).
    if any(d for d in diffs):
        node['difficultyModifiers'] = diffs
    if version < 2:
        return node
    return _anchored(node, {
        'leadin': (node['leadin'], qems_to_yapp_html(leadin, anchors=True)),
        'parts': (parts, a_parts),
        'answers': (answers, a_answers),
    })


def interlaced_reading_order(tossup_count, bonus_count):
    """A YAPP2 ``readingOrder``: tossup 1, bonus 1, tossup 2, bonus 2 … as
    ``{'type', 'index'}`` entries pointing into the canonical arrays. Whichever
    kind runs out first, the rest of the other simply follows, and every
    question appears exactly once — a reader that drops the field still sees
    them all, just grouped the plain-YAPP way."""
    order = []
    for i in range(max(tossup_count, bonus_count)):
        if i < tossup_count:
            order.append({'type': 'tossup', 'index': i})
        if i < bonus_count:
            order.append({'type': 'bonus', 'index': i})
    return order


def packet_to_yapp(tossups, bonuses, version=1, name=None, interlace=False,
                   tag_names=None):
    """Build a YAPP packet dict from ordered tossup and bonus lists. Question
    numbers come from each question's ``question_number`` (falling back to
    position) so the reader shows the packet's own numbering.

    ``version=2`` emits YAPP2: the same packet plus a ``version`` marker and
    per-question ``anchored`` fields carrying pronunciation-guide anchoring. The
    canonical fields are identical either way.

    ``interlace`` (YAPP2 only) adds a ``readingOrder`` saying the packet is read
    tossup/bonus/tossup/bonus rather than all tossups then all bonuses. It only
    reorders — the questions themselves are untouched, so a reader that ignores
    it still gets the whole packet.

    ``tag_names`` maps ('tossup'|'bonus', id) to the category tag names to print
    after the metadata; leave it out for a set that keeps its tags to itself."""

    def _tags(kind, question):
        # Everything else here works on anything shaped like a question, not
        # only on a saved model, so an id is asked for only when there are tags
        # to look up.
        if not tag_names:
            return None
        return tag_names.get((kind, getattr(question, 'id', None)))

    packet = {
        'tossups': [tossup_to_yapp(t, t.question_number or i, version=version,
                                   tags=_tags('tossup', t))
                    for i, t in enumerate(tossups, 1)],
        'bonuses': [bonus_to_yapp(b, b.question_number or i, version=version,
                                  tags=_tags('bonus', b))
                    for i, b in enumerate(bonuses, 1)],
    }
    if name:
        packet['name'] = name
    if version >= 2:
        if interlace:
            packet['readingOrder'] = interlaced_reading_order(
                len(packet['tossups']), len(packet['bonuses']))
        # Leading key: a reader (or a human opening the file) sees the version
        # before the questions.
        packet = {'version': YAPP2_VERSION, **packet}
    return packet
