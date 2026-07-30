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
and leaves the power marker ``(*)`` as literal text in the question (the reader
locates the power boundary from it). This mirrors how QEMS stores questions, so
the export is round-trippable through this app's own YAPP importer.

YAPP2
-----
Plain YAPP has nowhere to record *which words a pronunciation guide covers* —
QEMS tracks that with ``\\Pword\\P`` markers (see ``utils`` and the ``.pg-target``
styling), and a YAPP export has to throw it away. YAPP2 is a backward-compatible
superset that keeps it. See ``YAPP2_FORMAT.md`` at the repo root for the spec; in
brief:

* a top-level ``"version": "yapp2/1.0"`` marks the file;
* the canonical YAPP fields are **unchanged** — byte-identical to what a plain
  YAPP export writes, so every existing consumer reads a YAPP2 file correctly;
* each question may carry an ``"anchored"`` object holding the same fields with
  ``<pg>...</pg>`` around the anchored word(s). Readers that understand YAPP2
  prefer those; readers that don't never see the tag.

``<pg>`` marks question *words*, not a guide: they are read aloud, count as
words, and are buzzable. Only the parenthesized guide itself is non-word text.
"""

import html as _html

#: Value of the top-level ``version`` field on a YAPP2 packet.
YAPP2_VERSION = 'yapp2/1.0'


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


def _metadata(question):
    """Build YAPP's ``metadata`` string (shown by the reader after the answer)
    from the question's author and category, as ``Author, Category - Subcategory``.
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
        return '{0}, {1}'.format(author, cat)
    return cat or author


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


def tossup_to_yapp(tossup, number, version=1):
    text = tossup.tossup_text or ''
    answer = tossup.tossup_answer or ''
    node = {
        'number': number,
        'question': qems_to_yapp_html(text),
        'answer': qems_to_yapp_html(answer),
        'metadata': _metadata(tossup),
    }
    if version < 2:
        return node
    return _anchored(node, {
        'question': (node['question'], qems_to_yapp_html(text, anchors=True)),
        'answer': (node['answer'], qems_to_yapp_html(answer, anchors=True)),
    })


def bonus_to_yapp(bonus, number, version=1):
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
        'metadata': _metadata(bonus),
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


def packet_to_yapp(tossups, bonuses, version=1, name=None):
    """Build a YAPP packet dict from ordered tossup and bonus lists. Question
    numbers come from each question's ``question_number`` (falling back to
    position) so the reader shows the packet's own numbering.

    ``version=2`` emits YAPP2: the same packet plus a ``version`` marker and
    per-question ``anchored`` fields carrying pronunciation-guide anchoring. The
    canonical fields are identical either way."""
    packet = {
        'tossups': [tossup_to_yapp(t, t.question_number or i, version=version)
                    for i, t in enumerate(tossups, 1)],
        'bonuses': [bonus_to_yapp(b, b.question_number or i, version=version)
                    for i, b in enumerate(bonuses, 1)],
    }
    if name:
        packet['name'] = name
    if version >= 2:
        # Leading key: a reader (or a human opening the file) sees the version
        # before the questions.
        packet = {'version': YAPP2_VERSION, **packet}
    return packet
