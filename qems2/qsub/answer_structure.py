"""Structured answer lines.

An answer line is ordinarily one string whose structure is implied by word
order: ``_Louis XIV_ [accept _the Sun King_; prompt on __Louis__ by asking
"which Louis?"]``. Everything downstream — the reader, the style checker, the
answer database — has to re-derive that structure from the prose, and gets it
wrong at the edges.

A set can opt in to recording the structure instead (``QuestionSet.
structured_answers``, off by default). The structure is:

    {'primary': 'Louis XIV',
     'accepts': [{'text': 'the Sun King', 'until': 'Bourbon'}],
     'prompts': [{'text': 'Louis', 'instruction': 'which Louis?',
                  'until': None, 'anti': False}],
     'unparsed': ['do not accept "Louis XVI"']}

``primary`` is required; the rest are optional. ``until`` names a word in the
tossup after which the alternative stops being acceptable, and applies to
tossups only — a bonus part has no shared text to read up to, so it is dropped
there rather than silently kept.

**The prose stays canonical.** `format_line` rebuilds the printed line from the
structure, and a question saved through the structured editor stores both; a
question that arrives as prose (pasted, imported, typed in bulk) keeps its line
exactly as written and has the structure derived beside it. Nothing downstream
- the PDF, the Word export, the character count, YAPP - reads the structure, so
turning the setting on cannot change what a packet looks like.

Parsing is best-effort by design: a clause it can't place is preserved verbatim
in ``unparsed`` rather than dropped, so no text is ever lost in the round trip.
"""

import re

from .utils import strip_markup

#: Keys of a well-formed structure, in the order `format_line` writes them.
STRUCTURE_KEYS = ('primary', 'accepts', 'prompts', 'unparsed')

# The keyword that opens a clause. "or" and "accept" both introduce something
# acceptable; "prompt"/"antiprompt" introduce something to prompt on. The "on"
# after prompt is optional because plenty of lines leave it out.
_DIRECTIVE_RE = re.compile(
    r'(?i)^\s*(?:'
    r'(?P<accept>or|accept|also\s+accept)'
    r'|(?P<anti>anti-?prompt)(?:\s+on)?'
    r'|(?P<prompt>prompt)(?:\s+on)?'
    r')\b\s*')

# "until X is read" / "until X" / "before X is read", with or without quotes.
_UNTIL_RE = re.compile(
    r'(?i)\s*\b(?:until|before)\s+'
    r'(?:"(?P<quoted>[^"]+)"|“(?P<curly>[^”]+)”|(?P<bare>[^;\]]+?))'
    r'(?:\s+is\s+read|\s+are\s+read)?\s*$')

# What the moderator should say: "by asking ...", "by saying ...", any
# "by <verb>ing ...", or a quoted question introduced by "with".
_INSTRUCTION_RE = re.compile(
    r'(?i)\s*\b(?:by\s+[a-z]+ing|with)\s+(?P<rest>.+)$')

_QUOTED_RE = re.compile(r'^["“‘\'](?P<inner>.+?)["”’\']$')


def is_enabled(question):
    """Whether this question's set records structured answers. Tolerates a
    stand-in object with no set (the type-questions preview builds unsaved
    questions)."""
    getter = getattr(question, 'question_set', None)
    return bool(getattr(getter, 'structured_answers', False))


def empty_structure(primary=''):
    return {'primary': primary or '', 'accepts': [], 'prompts': [], 'unparsed': []}


def _strip_outer_quotes(text):
    m = _QUOTED_RE.match((text or '').strip())
    return m.group('inner').strip() if m else (text or '').strip()


def _split_body(raw):
    """(primary, clause-body) for an answer line, splitting at the bracket that
    holds the alternatives. Falls back to a parenthesized body, which plenty of
    lines use instead, and then to no body at all."""
    text = (raw or '').strip()
    for opener, closer in (('[', ']'), ('(', ')')):
        start = text.find(opener)
        if start == -1:
            continue
        end = text.rfind(closer)
        if end > start:
            return text[:start].strip(), text[start + 1:end].strip()
    return text, ''


def _split_clauses(body):
    """The body split into clauses. Semicolons separate clauses outright; a
    comma only does when what follows it opens with a directive, since commas
    inside a single clause are ordinary punctuation."""
    if not body:
        return []
    clauses = []
    for chunk in body.split(';'):
        pieces = re.split(r',\s*(?=(?:or|accept|also\s+accept|anti-?prompt|prompt)\b)',
                          chunk, flags=re.IGNORECASE)
        clauses.extend(p.strip() for p in pieces if p.strip())
    return clauses


def _take_until(text):
    """(text-without-until, until-target). The trailing "until X is read"."""
    m = _UNTIL_RE.search(text or '')
    if not m:
        return (text or '').strip(), None
    target = m.group('quoted') or m.group('curly') or m.group('bare') or ''
    return text[:m.start()].strip(), _strip_outer_quotes(target) or None


def _take_instruction(text):
    """(target, instruction) for a prompt clause."""
    m = _INSTRUCTION_RE.search(text or '')
    if not m:
        return (text or '').strip(), ''
    return text[:m.start()].strip(), _strip_outer_quotes(m.group('rest'))


def parse_line(raw, is_tossup=True):
    """Best-effort structure for a prose answer line.

    Never raises and never loses text: anything the grammar can't place ends up
    in ``unparsed``.
    """
    primary, body = _split_body(raw)
    structure = empty_structure(primary)

    # A line with no bracket still says something: "Louis XIV or the Sun King"
    # names an alternative, and it is the one form common enough to be worth
    # reading without a directive to announce it.
    if not body and re.search(r'(?i)\s+or\s+', structure['primary']):
        head, rest = re.split(r'(?i)\s+or\s+', structure['primary'], maxsplit=1)
        structure['primary'] = head.strip()
        body = 'or ' + rest.strip()

    for clause in _split_clauses(body):
        m = _DIRECTIVE_RE.match(clause)
        if not m:
            structure['unparsed'].append(clause)
            continue

        rest = clause[m.end():].strip()
        rest, until = _take_until(rest)
        if not is_tossup:
            # A bonus part has no shared text to read up to
            until = None

        if m.group('accept'):
            if not rest:
                structure['unparsed'].append(clause)
                continue
            structure['accepts'].append({'text': rest, 'until': until})
        else:
            target, instruction = _take_instruction(rest)
            if not target:
                structure['unparsed'].append(clause)
                continue
            structure['prompts'].append({
                'text': target,
                'instruction': instruction,
                'until': until,
                'anti': bool(m.group('anti')),
            })

    return structure


def _format_until(until):
    return ' until "{0}" is read'.format(until) if until else ''


def format_line(structure):
    """The printed answer line for a structure — the inverse of `parse_line`
    for anything the structured editor produces."""
    structure = normalize(structure)
    clauses = []
    for accept in structure['accepts']:
        clauses.append('accept {0}{1}'.format(accept['text'], _format_until(accept['until'])))
    for prompt in structure['prompts']:
        directive = 'antiprompt on' if prompt['anti'] else 'prompt on'
        instruction = ' by asking "{0}"'.format(prompt['instruction']) if prompt['instruction'] else ''
        clauses.append('{0} {1}{2}{3}'.format(
            directive, prompt['text'], instruction, _format_until(prompt['until'])))
    clauses.extend(structure['unparsed'])

    if not clauses:
        return structure['primary']
    return '{0} [{1}]'.format(structure['primary'], '; '.join(clauses))


def describes(structure, line):
    """Whether `structure` is still the structure of `line`.

    A structured save writes the two together, so they agree exactly. Anything
    that edits the line on its own — a bulk edit, an import, a reverted history
    entry — leaves a stored structure describing an answer that is no longer
    there, and the line is the one to believe. The comparison runs the formatted
    line through the same `strip_markup` the save applies, so the escaping it
    does isn't mistaken for an edit.
    """
    formatted = format_line(structure)
    line = (line or '').strip()
    return formatted.strip() == line or strip_markup(formatted).strip() == line


def normalize(structure, is_tossup=True):
    """A structure with every key present, blank entries dropped, and `until`
    cleared on a bonus. Accepts partial input from a form post."""
    structure = structure or {}
    out = empty_structure((structure.get('primary') or '').strip())

    for accept in structure.get('accepts') or []:
        text = (accept.get('text') or '').strip()
        if not text:
            continue
        until = (accept.get('until') or '').strip() or None
        out['accepts'].append({'text': text, 'until': until if is_tossup else None})

    for prompt in structure.get('prompts') or []:
        text = (prompt.get('text') or '').strip()
        if not text:
            continue
        until = (prompt.get('until') or '').strip() or None
        out['prompts'].append({
            'text': text,
            'instruction': (prompt.get('instruction') or '').strip(),
            'until': until if is_tossup else None,
            'anti': bool(prompt.get('anti')),
        })

    out['unparsed'] = [u.strip() for u in (structure.get('unparsed') or []) if (u or '').strip()]
    return out


def plain(text):
    """The words of an answer fragment, without QEMS markup — `strip_markup`
    leaves the underscores and tildes, which would otherwise show up in a
    message to the editor and glue themselves onto words."""
    out = strip_markup(text or '')
    for marker in ('\\S', '\\s', '\\B', '\\P', '\\N'):
        out = out.replace(marker, '')
    return out.replace('_', '').replace('~', '').strip()


def _words(text):
    return set(re.findall(r"[\w'’-]+", plain(text).lower().replace('_', '')))


def validate(structure, question_text='', is_tossup=True):
    """Problems with a structure, as plain sentences. Empty when it's sound.

    These are warnings for an editor, not save-blocking errors — the one
    exception being a missing primary answer, which leaves nothing to print.
    """
    structure = normalize(structure, is_tossup)
    problems = []

    if not structure['primary']:
        problems.append('An answer line needs a primary answer.')

    for prompt in structure['prompts']:
        if not prompt['instruction']:
            problems.append(
                'The prompt on "{0}" doesn\'t say what to ask. Two moderators '
                'inventing their own follow-up is what a directed prompt '
                'prevents.'.format(plain(prompt['text'])))

    if is_tossup:
        available = _words(question_text)
        for entry in structure['accepts'] + structure['prompts']:
            until = entry.get('until')
            if not until:
                continue
            if available and not _words(until) <= available:
                problems.append(
                    '"{0}" isn\'t in the tossup, so there is nothing for the '
                    'moderator to read up to.'.format(until))

    return problems


def posted(post, prefix, is_tossup=True):
    """The structure a structured-editor form posted, or None when the form
    didn't render one (a suggestion, a set without the setting on).

    Rows arrive as parallel lists — accept 2's "until" is the second entry of
    ``<prefix>_accept_until`` — so a blank row is dropped by `normalize` rather
    than shifting the ones after it.
    """
    if '{0}_primary'.format(prefix) not in post:
        return None

    def rows(name):
        return post.getlist('{0}_{1}'.format(prefix, name))

    accept_text = rows('accept_text')
    accept_until = rows('accept_until')
    prompt_text = rows('prompt_text')
    prompt_instruction = rows('prompt_instruction')
    prompt_until = rows('prompt_until')
    prompt_anti = rows('prompt_anti')

    def at(values, index):
        return values[index] if index < len(values) else ''

    structure = {
        'primary': post.get('{0}_primary'.format(prefix), ''),
        'accepts': [{'text': text, 'until': at(accept_until, i)}
                    for i, text in enumerate(accept_text)],
        'prompts': [{'text': text,
                     'instruction': at(prompt_instruction, i),
                     'until': at(prompt_until, i),
                     'anti': at(prompt_anti, i) == 'anti'}
                    for i, text in enumerate(prompt_text)],
        'unparsed': rows('unparsed'),
    }
    return normalize(structure, is_tossup)


def has_content(structure):
    """Whether a structure says anything beyond a primary answer."""
    structure = normalize(structure)
    return bool(structure['accepts'] or structure['prompts'] or structure['unparsed'])
