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
"""

import html as _html


# QEMS markup (see utils.get_formatted_question_html) mapped to YAPP's HTML tags:
#   _x_   -> <b><u>x</u></b>   (required answer: bold + underline)
#   __x__ -> <u>x</u>          (prompt / alternate answer: underline only)
#   ~x~   -> <em>x</em>        (italics)
#   \Sx\S -> <sup>x</sup>      \sx\s -> <sub>x</sub>
#   \Bx\B -> <b>x</b>          (explicit bold)
#   (*)   -> kept verbatim     (power marker)
#   \_ \~ \( \) -> literal characters
def qems_to_yapp_html(text):
    """Convert one field of QEMS markup to the HTML YAPP emits. Non-markup text
    is passed through unchanged (it is already stored as safe HTML — angle
    brackets arrive pre-escaped as ``&lt;``/``&gt;``), matching how the app
    renders questions elsewhere."""
    if not text:
        return ''

    out = []
    ital = ul = prompt = sup = sub = bold = False
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


def tossup_to_yapp(tossup, number):
    return {
        'number': number,
        'question': qems_to_yapp_html(tossup.tossup_text or ''),
        'answer': qems_to_yapp_html(tossup.tossup_answer or ''),
        'metadata': _metadata(tossup),
    }


def bonus_to_yapp(bonus, number):
    parts, answers, values, diffs = [], [], [], []
    for i in range(1, 4):
        text = getattr(bonus, 'part{0}_text'.format(i), '') or ''
        if not text.strip():
            continue
        answer = getattr(bonus, 'part{0}_answer'.format(i), '') or ''
        diff = (getattr(bonus, 'part{0}_difficulty'.format(i), '') or '').strip().lower()
        parts.append(qems_to_yapp_html(text))
        answers.append(qems_to_yapp_html(answer))
        values.append(10)
        diffs.append(diff if diff in ('e', 'm', 'h') else None)

    node = {
        'number': number,
        'leadin': qems_to_yapp_html(bonus.leadin or ''),
        'parts': parts,
        'answers': answers,
        'values': values,
        'metadata': _metadata(bonus),
    }
    # Only emit difficultyModifiers when at least one part carries one (YAPP
    # makes the field optional).
    if any(d for d in diffs):
        node['difficultyModifiers'] = diffs
    return node


def packet_to_yapp(tossups, bonuses):
    """Build a YAPP packet dict from ordered tossup and bonus lists. Question
    numbers come from each question's ``question_number`` (falling back to
    position) so the reader shows the packet's own numbering."""
    return {
        'tossups': [tossup_to_yapp(t, t.question_number or i)
                    for i, t in enumerate(tossups, 1)],
        'bonuses': [bonus_to_yapp(b, b.question_number or i)
                    for i, b in enumerate(bonuses, 1)],
    }
