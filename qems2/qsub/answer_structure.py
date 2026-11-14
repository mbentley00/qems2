"""Structured answer lines.

An answer line is ordinarily one string whose structure is implied by word
order: ``_Louis XIV_ [or _the Sun King_; prompt on __Louis__ by asking "which
Louis?"; reject "Louis XVI"] (He revoked the Edict of Nantes.)``. Everything
downstream -- the reader, the style checker, the answer database -- has to
re-derive that structure from the prose, and gets it wrong at the edges.

A set can opt in to recording the structure instead (``QuestionSet.
structured_answers``, off by default). The shape follows the "answer-fancy"
breakdown used by packets like minkowski.space's QBOWLOSM: a canonical answer,
then one list per directive, then a trailing note::

    {'primary': 'Louis XIV',
     'alternates': [{'text': 'the Sun King', ...}],          # "or ..."
     'accepts':    [{'text': 'the King', 'when': 'after', 'word': 'Sun', ...}],
     'prompts':    [{'text': 'Louis', 'instruction': 'which Louis?',
                     'anti': False, ...}],
     'rejects':    [{'text': '"Louis XVI"'}],
     'unparsed':   ['clauses inside the bracket it could not place'],
     'note':       'He revoked the Edict of Nantes.'}

Every alternate, accept and prompt can carry the same modifiers:

* ``when`` / ``word`` -- a reading window: ``'until_read'`` ("until read": the
  answer stops being good once the question says it), ``'until'`` ("until
  "word" is read") or ``'after'`` ("after "word" is read"). Tossups only; a
  bonus part has no shared text to read up to, so it is dropped there.
* ``in_place_of`` -- ``accept USSR in place of "Soviet Union"``.
* ``instruction`` (prompts) -- what the moderator says: ``by asking "..."``.

**The prose stays canonical.** `format_line` rebuilds the printed line from the
structure, and a question saved through the structured editor stores both; a
question that arrives as prose (pasted, imported, typed in bulk) keeps its line
exactly as written and has the structure derived beside it. Nothing downstream
- the PDF, the Word export, the character count, YAPP - reads the structure, so
turning the setting on cannot change what a packet looks like.

Parsing is best-effort by design: a clause it can't place is preserved verbatim
in ``unparsed`` rather than dropped, so no text is lost in the round trip.
"""

import re

from .utils import strip_markup

#: Keys of a well-formed structure, in the order `format_line` writes them.
STRUCTURE_KEYS = ('primary', 'alternates', 'accepts', 'prompts', 'rejects', 'unparsed', 'note')

#: The list groups, in printed order, with the label each gets in a breakdown.
GROUPS = (('alternates', 'or'), ('accepts', 'accept'), ('prompts', 'prompt on'),
          ('rejects', 'reject'))

#: Reading windows an entry can carry.
WHENS = ('until_read', 'until', 'after')

# The keyword that opens a clause. The "on" after prompt is optional because
# plenty of lines leave it out. "do not accept or prompt on" is one rejection.
_DIRECTIVE_RE = re.compile(
    r'(?i)^\s*(?:'
    r'(?P<reject>reject|(?:do\s+not|don[\'’]t)\s+accept(?:\s+or\s+prompt(?:\s+on)?)?)'
    r'|(?P<accept>(?:also\s+)?accept)'
    r'|(?P<alt>or)'
    r'|(?P<anti>anti-?prompt)(?:\s+on)?'
    r'|(?P<prompt>prompt)(?:\s+on)?'
    r')\b\s*')

# A quoted phrase or a bare run of words (no quote marks), as the object of a
# modifier like "until", "after", "in place of" or "by asking".
_OBJECT = r'(?P<obj>"[^"]+"|“[^”]+”|[^"“”;\]]+?)'

# Trailing modifiers. Each is anchored at the end of the clause and peeled off
# one at a time, latest first, so they can come in any order.
_MODIFIERS = (
    ('until_read', re.compile(r'(?i)\s+until\s+(?:read|mentioned|said)\s*$')),
    ('until', re.compile(r'(?i)\s+(?:until|before)\s+' + _OBJECT +
                         r'(?:\s+(?:is|are)\s+(?:read|mentioned|said))?\s*$')),
    # "after" is a common word, so a bare object needs the "is read" to count
    ('after', re.compile(r'(?i)\s+after\s+(?P<obj>"[^"]+"|“[^”]+”)'
                         r'(?:\s+(?:is|are)\s+(?:read|mentioned|said))?\s*$')),
    ('after', re.compile(r'(?i)\s+after\s+(?P<obj>[^"“”;\]]+?)'
                         r'\s+(?:is|are)\s+(?:read|mentioned|said)\s*$')),
    ('in_place_of', re.compile(r'(?i)\s+in\s+place\s+of\s+' + _OBJECT + r'\s*$')),
    ('instruction', re.compile(r'(?i)\s+by\s+[a-z]+ing\s+' + _OBJECT + r'\s*$')),
    ('instruction', re.compile(r'(?i)\s+with\s+(?P<obj>"[^"]+"|“[^”]+”)\s*$')),
)

_QUOTED_RE = re.compile('^["“‘\'](?P<inner>.+?)["”’\']$')

# A clause separator: a semicolon, or a comma whose next clause opens with a
# directive other than "or" -- "a, b, or c" is one list, not two clauses.
_CLAUSE_COMMA_RE = re.compile(
    r',\s*(?=(?:accept|also\s+accept|anti-?prompt|prompt|reject|do\s+not\s+accept)\b)',
    re.IGNORECASE)


def is_enabled(question):
    """Whether this question's set records structured answers. Tolerates a
    stand-in object with no set (the type-questions preview builds unsaved
    questions)."""
    getter = getattr(question, 'question_set', None)
    return bool(getattr(getter, 'structured_answers', False))


def empty_structure(primary=''):
    return {'primary': primary or '', 'alternates': [], 'accepts': [], 'prompts': [],
            'rejects': [], 'unparsed': [], 'note': ''}


def _entry(text='', when='', word='', in_place_of='', instruction=None, anti=None):
    entry = {'text': text, 'when': when, 'word': word, 'in_place_of': in_place_of}
    if instruction is not None:
        entry['instruction'] = instruction
        entry['anti'] = bool(anti)
    return entry


def _strip_outer_quotes(text):
    m = _QUOTED_RE.match((text or '').strip())
    return m.group('inner').strip() if m else (text or '').strip()


def _closing(text, start, opener, closer):
    """Index of the bracket closing the one at `start`, or -1."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_line(raw):
    """(primary, clause-body, trailing note) for an answer line.

    The body is the first square bracket. Without one, a parenthesis counts as
    the body only when it opens with a directive -- otherwise it is a
    pronunciation guide or an aside and belongs to the answer it follows.
    Whatever comes after the body is the note.
    """
    text = (raw or '').strip()

    start = text.find('[')
    if start != -1:
        end = _closing(text, start, '[', ']')
        if end == -1:
            end = text.rfind(']')
        if end > start:
            return text[:start].strip(), text[start + 1:end].strip(), text[end + 1:].strip()

    for m in re.finditer(r'\(', text):
        end = _closing(text, m.start(), '(', ')')
        if end == -1:
            continue
        inner = text[m.start() + 1:end]
        if _DIRECTIVE_RE.match(inner) and not inner.strip().lower().startswith('or '):
            return text[:m.start()].strip(), inner.strip(), text[end + 1:].strip()
    return text, '', ''


def _clean_note(note):
    """A trailing note without the parentheses it is printed in."""
    note = (note or '').strip()
    if note.startswith('(') and note.endswith(')') and _closing(note, 0, '(', ')') == len(note) - 1:
        note = note[1:-1].strip()
    return note


def _split_clauses(body):
    """The body split into clauses. Semicolons separate clauses outright; a
    comma only does when what follows it opens with a directive, since commas
    inside a single clause are ordinary punctuation."""
    if not body:
        return []
    clauses = []
    for chunk in body.split(';'):
        clauses.extend(p.strip() for p in _CLAUSE_COMMA_RE.split(chunk) if p.strip())
    return clauses


def _peel_modifiers(text, allow_instruction=True):
    """(target, modifiers) for the part of a clause after its directive.

    Modifiers sit at the end in any order ("until read by asking ...", "by
    asking ... until "x" is read"), so the one that starts latest is taken
    off first, repeatedly, until none is left.
    """
    found = {}
    text = (text or '').strip()
    while True:
        best = None
        for kind, pattern in _MODIFIERS:
            if kind in found or (kind in WHENS and 'when' in found):
                continue
            if kind == 'instruction' and not allow_instruction:
                continue
            m = pattern.search(text)
            if m and m.start() > 0 and (best is None or m.start() > best[1].start()):
                best = (kind, m)
        if best is None:
            return text, found
        kind, m = best
        obj = _strip_outer_quotes(m.groupdict().get('obj') or '')
        if kind in WHENS:
            found['when'] = kind
            found['word'] = obj
        else:
            found[kind] = obj
        text = text[:m.start()].strip()


def parse_line(raw, is_tossup=True):
    """Best-effort structure for a prose answer line.

    Never raises and never loses text: anything the grammar can't place ends up
    in ``unparsed``.
    """
    primary, body, note = _split_line(raw)
    structure = empty_structure(primary)
    structure['note'] = _clean_note(note)

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
        if m.group('reject'):
            if not rest:
                structure['unparsed'].append(clause)
            else:
                structure['rejects'].append({'text': rest})
            continue

        is_prompt = bool(m.group('prompt') or m.group('anti'))
        target, mods = _peel_modifiers(rest, allow_instruction=is_prompt)
        if not target:
            structure['unparsed'].append(clause)
            continue
        when = mods.get('when', '') if is_tossup else ''
        word = mods.get('word', '') if when else ''

        if not is_prompt:
            group = 'alternates' if m.group('alt') else 'accepts'
            structure[group].append(_entry(target, when, word, mods.get('in_place_of', '')))
        else:
            structure['prompts'].append(_entry(
                target, when, word, mods.get('in_place_of', ''),
                instruction=mods.get('instruction', ''), anti=bool(m.group('anti'))))

    return structure


def _quote(text):
    text = (text or '').strip()
    return text if _QUOTED_RE.match(text) else '"{0}"'.format(text)


def format_entry(entry, is_prompt=False):
    """One alternate/accept/prompt as it prints after its directive."""
    out = entry['text']
    if entry.get('in_place_of'):
        out += ' in place of ' + _quote(entry['in_place_of'])
    when = entry.get('when')
    if when == 'until_read':
        out += ' until read'
    elif when in ('until', 'after') and entry.get('word'):
        out += ' {0} {1} is read'.format(when, _quote(entry['word']))
    if is_prompt and entry.get('instruction'):
        out += ' by asking ' + _quote(entry['instruction'])
    return out


def format_line(structure):
    """The printed answer line for a structure -- the inverse of `parse_line`
    for anything the structured editor produces."""
    structure = normalize(structure)
    clauses = []
    for entry in structure['alternates']:
        clauses.append('or ' + format_entry(entry))
    for entry in structure['accepts']:
        clauses.append('accept ' + format_entry(entry))
    for entry in structure['prompts']:
        directive = 'antiprompt on' if entry['anti'] else 'prompt on'
        clauses.append(directive + ' ' + format_entry(entry, is_prompt=True))
    for entry in structure['rejects']:
        clauses.append('reject ' + entry['text'])
    clauses.extend(structure['unparsed'])

    line = structure['primary']
    if clauses:
        line = '{0} [{1}]'.format(line, '; '.join(clauses))
    if structure['note']:
        line = '{0} ({1})'.format(line, structure['note'])
    return line


def describes(structure, line):
    """Whether `structure` is still the structure of `line`.

    A structured save writes the two together, so they agree exactly. Anything
    that edits the line on its own -- a bulk edit, an import, a reverted history
    entry -- leaves a stored structure describing an answer that is no longer
    there, and the line is the one to believe. The comparison runs the formatted
    line through the same `strip_markup` the save applies, so the escaping it
    does isn't mistaken for an edit.
    """
    formatted = format_line(structure)
    line = (line or '').strip()
    return formatted.strip() == line or strip_markup(formatted).strip() == line


def _normalize_entry(raw, is_tossup, is_prompt):
    """One entry with every key present, or None when it has no text. Reads the
    older shape too, whose only window was ``until: "word"``."""
    text = (raw.get('text') or '').strip()
    if not text:
        return None
    when = (raw.get('when') or '').strip()
    word = (raw.get('word') or '').strip()
    legacy_until = (raw.get('until') or '').strip()
    if not when and legacy_until:
        when, word = 'until', legacy_until
    if when not in WHENS or (when != 'until_read' and not word):
        when = ''
    if when == 'until_read' or not when:
        word = ''
    if not is_tossup:
        when, word = '', ''
    return _entry(text, when, word, (raw.get('in_place_of') or '').strip(),
                  instruction=(raw.get('instruction') or '').strip() if is_prompt else None,
                  anti=raw.get('anti') if is_prompt else None)


def normalize(structure, is_tossup=True):
    """A structure with every key present, blank entries dropped, and reading
    windows cleared on a bonus. Accepts partial input from a form post and
    structures stored in the older primary/accepts/prompts/unparsed shape."""
    structure = structure or {}
    out = empty_structure((structure.get('primary') or '').strip())

    for group in ('alternates', 'accepts', 'prompts'):
        for raw in structure.get(group) or []:
            entry = _normalize_entry(raw or {}, is_tossup, group == 'prompts')
            if entry:
                out[group].append(entry)

    for raw in structure.get('rejects') or []:
        text = ((raw or {}).get('text') or '').strip()
        if text:
            out['rejects'].append({'text': text})

    out['unparsed'] = [u.strip() for u in (structure.get('unparsed') or []) if (u or '').strip()]
    out['note'] = _clean_note(structure.get('note'))
    return out


def plain(text):
    """The words of an answer fragment, without QEMS markup -- `strip_markup`
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

    These are warnings for an editor, not save-blocking errors -- the one
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
        for group in ('alternates', 'accepts', 'prompts'):
            for entry in structure[group]:
                if entry['when'] not in ('until', 'after'):
                    continue
                if available and not _words(entry['word']) <= available:
                    problems.append(
                        '"{0}" isn\'t in the tossup, so there is nothing for the '
                        'moderator to read up to.'.format(entry['word']))

    return problems


def posted(post, prefix, is_tossup=True):
    """The structure a structured-editor form posted, or None when the form
    didn't render one (a suggestion, a set without the setting on).

    Rows arrive as parallel lists -- accept 2's window is the second entry of
    ``<prefix>_accept_when`` -- so a blank row is dropped by `normalize` rather
    than shifting the ones after it. The older ``<prefix>_<group>_until`` field
    is still read, as an "until "word" is read" window.
    """
    if '{0}_primary'.format(prefix) not in post:
        return None

    def rows(name):
        return post.getlist('{0}_{1}'.format(prefix, name))

    def at(values, index):
        return values[index] if index < len(values) else ''

    def entries(group, is_prompt=False):
        text = rows(group + '_text')
        when, word = rows(group + '_when'), rows(group + '_word')
        until = rows(group + '_until')
        in_place = rows(group + '_inplace')
        instruction, anti = rows(group + '_instruction'), rows(group + '_anti')
        out = []
        for i, value in enumerate(text):
            entry = {'text': value, 'when': at(when, i), 'word': at(word, i),
                     'until': at(until, i), 'in_place_of': at(in_place, i)}
            if is_prompt:
                entry['instruction'] = at(instruction, i)
                entry['anti'] = at(anti, i) == 'anti'
            out.append(entry)
        return out

    structure = {
        'primary': post.get('{0}_primary'.format(prefix), ''),
        'alternates': entries('or'),
        'accepts': entries('accept'),
        'prompts': entries('prompt', is_prompt=True),
        'rejects': [{'text': t} for t in rows('reject_text')],
        'unparsed': rows('unparsed'),
        'note': post.get('{0}_note'.format(prefix), ''),
    }
    return normalize(structure, is_tossup)


def has_content(structure):
    """Whether a structure says anything beyond a primary answer."""
    structure = normalize(structure)
    return any(structure[key] for key in
               ('alternates', 'accepts', 'prompts', 'rejects', 'unparsed', 'note'))


def breakdown(structure):
    """The structure as labelled groups for display, in printed order: one
    ``{'key', 'label', 'items'}`` per non-empty group, each item a dict of the
    entry's text and its modifiers as ``(kind, words)`` pairs. Mirrors the
    expandable "answer-fancy" list under each answer line in QBOWLOSM-style
    HTML packets."""
    structure = normalize(structure)
    groups = [{'key': 'canonical', 'label': 'ANSWER',
               'items': [{'text': structure['primary'], 'mods': []}]}]
    for key, label in GROUPS:
        items = []
        for entry in structure[key]:
            mods = []
            if entry.get('in_place_of'):
                mods.append(('in place of', entry['in_place_of']))
            if entry.get('when') == 'until_read':
                mods.append(('until read', ''))
            elif entry.get('when') in ('until', 'after'):
                mods.append(('{0} "{1}" is read'.format(entry['when'], entry['word']), ''))
            if entry.get('instruction'):
                mods.append(('by asking', entry['instruction']))
            items.append({'text': entry['text'], 'mods': mods})
        if key == 'prompts':
            anti = [i for i, e in zip(items, structure[key]) if e['anti']]
            items = [i for i, e in zip(items, structure[key]) if not e['anti']]
            if items:
                groups.append({'key': key, 'label': label, 'items': items})
            if anti:
                groups.append({'key': 'antiprompts', 'label': 'antiprompt on', 'items': anti})
        elif items:
            groups.append({'key': key, 'label': label, 'items': items})
    if structure['unparsed']:
        groups.append({'key': 'other', 'label': 'also',
                       'items': [{'text': u, 'mods': []} for u in structure['unparsed']]})
    return {'groups': groups, 'note': structure['note']}


def _fancy_mods(entry, fmt):
    """The directive spans after an entry, as an answer-fancy list prints them."""
    out = []

    def directive(words):
        out.append('<span class="af-directive"> {0} </span>'.format(words))

    def quoted(css, words):
        out.append('<span class="{0}">\u201c{1}\u201d</span>'.format(css, fmt(words)))

    if entry.get('in_place_of'):
        directive('in place of')
        quoted('af-inplace', entry['in_place_of'])
    when = entry.get('when')
    if when == 'until_read':
        out.append('<span class="af-directive"> until read</span>')
    elif when in ('until', 'after') and entry.get('word'):
        directive(when)
        quoted('af-until', entry['word'])
        out.append('<span class="af-directive"> is read</span>')
    if entry.get('instruction'):
        directive('by asking')
        quoted('af-ask', entry['instruction'])
    return ''.join(out)


def fancy_html(line, structure, fmt):
    """An answer line the way QBOWLOSM's HTML packets print one: the line itself
    as a ``<details>`` summary, and, opened, one labelled list per group
    (ANSWER / or / accept / prompt on / reject) and the note.

    `fmt` turns a fragment of QEMS markup into HTML (the caller's formatter, so
    the set's pronunciation-guide rules apply). A line with nothing beyond its
    primary answer has nothing to break down and prints as plain ``ANSWER:``.
    """
    head = 'ANSWER: ' + fmt(line)
    structure = normalize(structure)
    if not has_content(structure):
        return '<p class="answer">{0}</p>'.format(head)

    rows = [('canonical', 'ANSWER', [structure['primary']], None)]
    for key, label in GROUPS:
        entries = structure[key]
        if key == 'prompts':
            plain_prompts = [e for e in entries if not e['anti']]
            anti = [e for e in entries if e['anti']]
            if plain_prompts:
                rows.append(('prompt', 'prompt on', plain_prompts, True))
            if anti:
                rows.append(('antiprompt', 'antiprompt on', anti, True))
        elif key == 'rejects':
            if entries:
                rows.append(('reject', 'reject', [e['text'] for e in entries], None))
        elif entries:
            rows.append((key == 'alternates' and 'or' or 'accept', label, entries, True))
    if structure['unparsed']:
        rows.append(('other', 'also', structure['unparsed'], None))

    dl = []
    for css, label, items, has_mods in rows:
        lis = []
        for item in items:
            if has_mods:
                lis.append('<li>{0}{1}</li>'.format(fmt(item['text']), _fancy_mods(item, fmt)))
            else:
                lis.append('<li>{0}</li>'.format(fmt(item)))
        dl.append('<dt class="af-{0}">{1}:</dt><dd class="af-{0}"><ul>{2}</ul></dd>'.format(
            css, label, ''.join(lis)))
    note = ('<p class="af-note">({0})</p>'.format(fmt(structure['note']))
            if structure['note'] else '')
    return ('<details class="af"><summary><p class="answer">{0}</p></summary>'
            '<div class="answer-fancy"><dl>{1}</dl>{2}</div></details>').format(
                head, ''.join(dl), note)
