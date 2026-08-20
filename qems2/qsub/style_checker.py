"""Style checker for quizbowl questions.

Runs a set of mechanically-checkable style rules over tossups and bonuses. The
rule set is selectable per "style guide"; the default is the guide at
https://minkowski.space/quizbowl/manuals/style/index.html. Each guide turns a
common pool of rules on or off.

Each issue is a dict:
    {'severity': 'error'|'warning'|'info',
     'message': str,
     'message_html': str,  # OPTIONAL: pre-escaped rich rendering of `message`
     'code': str,          # stable rule id
     'token': str,         # distinguishes issues of the same code in a question
     'fix': {...}}         # OPTIONAL, server-side only: how to auto-apply it

The (code, token) pair identifies an issue for dismissal/apply. `fix` (when
present) is applied server-side by apply_fix(); it is never sent by the client.
"""

import re
from html import escape as _escape

from .utils import strip_markup, parenthetical_has_quotes
from .pron_dict import suggest_guide_matches, context_snippet, guide_opener_at

ERROR = 'error'
WARNING = 'warning'
INFO = 'info'

# Selectable style guides. Each enables a subset of the rule pool below.
STYLE_GUIDES = [
    {'key': 'minkowski', 'name': 'Minkowski (default)',
     'url': 'https://minkowski.space/quizbowl/manuals/style/index.html'},
    {'key': 'generic', 'name': 'Generic (mechanical only)', 'url': ''},
]
DEFAULT_GUIDE = 'minkowski'

# Friendly labels for every rule code (used by the per-set settings UI).
RULE_LABELS = [
    ('double_space', 'Double spaces'),
    ('space_before_punct', 'Space before punctuation'),
    ('comma_no_space', 'Missing space after a comma'),
    ('quote_punct', 'Period/comma outside closing quotes'),
    ('ellipsis', 'Ellipsis (… instead of ...)'),
    ('double_hyphen', 'Em dash (— instead of --)'),
    ('number_range', 'En dash for number ranges (1990–1995)'),
    ('ampersand', 'Spell out "and" instead of &'),
    ('repeated_word', 'Repeated words ("the the")'),
    ('contractions', 'Contractions (don\'t, it\'s, …)'),
    ('imprecise_from', 'Imprecise "from this country" (prefer "born in")'),
    ('late_identifier', 'Identifier comes late in the first sentence'),
    ('mixed_identifier', 'Identifier switches between singular and plural'),
    ('unbalanced_parens', 'Unbalanced parentheses'),
    ('answer_leak', 'ANSWER: leaked into question text'),
    ('numerals', 'Numerals in "For 10 points"'),
    ('fps', 'Missing "For 10 points" (tossup)'),
    ('fpe', 'Missing "For 10 points each" (bonus)'),
    ('power', 'Power-mark problems'),
    ('imperative', 'Interrogative giveaway'),
    ('underline', 'Answer line has no underline'),
    ('answer_format', 'Non-standard bold/underline on an answer line'),
    ('pronunciation', 'Pronunciation-guide suggestions'),
    ('pg_span', 'Pronunciation guides without a marked target (\\P…\\P)'),
    ('pg_possessive', 'Pronunciation guides that split a possessive'),
    ('answer_alts', 'Answer line missing standard alternates'),
    ('prompt_undirected', 'Prompts with no directed instruction'),
]
RULE_LABEL_MAP = dict(RULE_LABELS)
ALL_CODES = [c for c, _ in RULE_LABELS]

# Which rule codes each guide turns on. A per-set "disabled" list can switch any
# of these off (e.g. teams that allow contractions).
GUIDE_CODES = {
    'minkowski': set(ALL_CODES),
    'generic': {'double_space', 'space_before_punct', 'comma_no_space', 'quote_punct',
                'ellipsis', 'double_hyphen', 'unbalanced_parens', 'repeated_word', 'underline'},
}


def guide_keys():
    return {g['key'] for g in STYLE_GUIDES}


def configurable_rules(guide=DEFAULT_GUIDE):
    """(code, label) for the rules a given guide runs — what editors can toggle
    per set, in display order."""
    on = GUIDE_CODES.get(guide, GUIDE_CODES[DEFAULT_GUIDE])
    return [(c, lbl) for c, lbl in RULE_LABELS if c in on]


def _enabled_codes(guide, disabled):
    return GUIDE_CODES.get(guide, GUIDE_CODES[DEFAULT_GUIDE]) - set(disabled or ())


# A dismissal is keyed by (code, token), and the token is what keeps it narrow:
# for these rules it names the *thing* the suggestion was about — one term, one
# guide, one cue — so dismissing it silences that one thing and nothing else.
# The number is which '|'-separated part of the token holds that name.
_TOKEN_SUBJECT_PART = {
    'pronunciation': 1,     # Question|Diderot
    'pg_span': 2,           # Question|0|("DID-er-OW")
    'pg_possessive': 2,     # Question|0|("SAH-chee")
    'prompt_undirected': 1,  # Answer|Louis
    'answer_alts': 1,       # Answer|boston
    'late_identifier': 1,   # Question|this composer
    'mixed_identifier': 1,  # Question|animal
}


def describe_dismissal(code, token):
    """What dismissing (code, token) actually silences, in words.

    A dismissal is never "turn this rule off". It is keyed to the exact thing
    the suggestion was about: one term's pronunciation guide, or one field's
    double spaces. Saying which is the difference between an editor silencing a
    single guide and believing they have silenced every guide in the set.

    Returns a dict with `rule` (the rule's own label), `subject` (the specific
    thing, when the token names one), `field` (the part of the question it was
    found in) and `summary` (a sentence combining them).
    """
    rule = RULE_LABEL_MAP.get(code, code)
    parts = (token or '').split('|')
    field = parts[0] if parts and parts[0] else ''
    subject = ''
    idx = _TOKEN_SUBJECT_PART.get(code)
    if idx is not None and len(parts) > idx:
        subject = parts[idx]

    if subject:
        summary = '{0} for "{1}"'.format(rule, subject)
        # The field is part of the key, so the same term in a different field is
        # a separate suggestion and stays. Say so rather than implying otherwise.
        if field:
            summary += ' (where it appears in {0} text)'.format(field)
    elif field:
        summary = '{0}, in {1} text'.format(rule, field)
    else:
        summary = rule
    return {'rule': rule, 'subject': subject, 'field': field, 'summary': summary}


def _issue(severity, message, code, token='', fix=None, message_html=''):
    d = {'severity': severity, 'message': message, 'code': code, 'token': token}
    if message_html:
        d['message_html'] = message_html
    if fix:
        d['fix'] = fix
    return d


def _widen_to_words(text, start, end):
    """Grow the [start, end) span out to whole words. A bolded space — a double
    space, or the space before a comma — would otherwise be invisible in the
    preview, and a bare "..." reads better with the word it trails."""
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


def _context_html(message, source, start, end):
    """`message` plus a short excerpt of `source` around the [start, end) span,
    the offending text bolded — the same preview the pronunciation suggestions
    show, so an editor can see what a rule is pointing at without hunting for
    it in the question."""
    start, end = _widen_to_words(source, start, end)
    prefix, before, match, after, suffix = context_snippet(source, start, end)
    return '{0} — <span class="pg-context">{1}<strong>{2}</strong>{3}</span>'.format(
        _escape(message), _escape(prefix + before), _escape(match), _escape(after + suffix))


def _issue_at(severity, message, code, token, fix, source, m):
    """An issue that points at the text it found: `_issue` plus the context
    preview for match `m` within `source`."""
    return _issue(severity, message, code, token, fix,
                  message_html=_context_html(message, source, m.start(), m.end()))


def _plain(text):
    """Readable plain text: strip HTML/smart quotes (strip_markup) plus QEMS
    markup characters, so style checks see the words as read. A note to the
    moderator or players (``\\N…\\N``) keeps its words — it is read out, and the
    prose rules apply to it like any other sentence — but loses its markers."""
    t = strip_markup(text or '')
    for marker in ('\\S', '\\s', '\\B', '\\P', '\\N'):
        t = t.replace(marker, '')
    return t.replace('_', '').replace('~', '')


def _mechanical_issues(label, raw, field):
    """Spacing/punctuation/typography rules that apply to any text blob. The
    fixable ones carry a `fix` keyed to `field` so they can be auto-applied."""
    issues = []
    text = _plain(raw)
    no_power = text.replace('(*)', '').replace('(+)', '')
    m = re.search(r' {2,}', text)
    if m:
        issues.append(_issue_at(WARNING, '{0}: double space'.format(label), 'double_space', label,
                                {'field': field, 'op': 'regex', 'pattern': r' {2,}', 'repl': ' '},
                                text, m))
    m = re.search(r'\s[,.;:!?]', no_power)
    if m:
        issues.append(_issue_at(WARNING, '{0}: space before punctuation'.format(label),
                                'space_before_punct', label,
                                {'field': field, 'op': 'regex', 'pattern': r'[ \t]+([,.;:!?])', 'repl': r'\1'},
                                no_power, m))
    m = re.search(r',[A-Za-z]', text)
    if m:
        issues.append(_issue_at(WARNING, '{0}: missing space after comma'.format(label),
                                'comma_no_space', label,
                                {'field': field, 'op': 'regex', 'pattern': r',([A-Za-z])', 'repl': r', \1'},
                                text, m))
    m = re.search(r'\.{3,}', text)
    if m:
        issues.append(_issue_at(INFO, '{0}: use ellipsis (…)'.format(label),
                                'ellipsis', label,
                                {'field': field, 'op': 'regex', 'pattern': r'\.{3,}', 'repl': '…'},
                                text, m))
    m = re.search(r'-{2,}', text)
    if m:
        issues.append(_issue_at(INFO, '{0}: use em dash (—)'.format(label),
                                'double_hyphen', label,
                                {'field': field, 'op': 'regex', 'pattern': r'-{2,}', 'repl': '—'},
                                text, m))
    if no_power.count('(') != no_power.count(')'):
        issues.append(_issue(WARNING, '{0}: unbalanced parentheses'.format(label),
                             'unbalanced_parens', label))
    # American style puts periods and commas INSIDE a closing double quote:
    # «..."end of sentence".» should be «..."end of sentence."» Only . and ,
    # are checked — colons, semicolons and question marks belong outside.
    # Single quotes are skipped (a possessive like «writers'.» is correct).
    m = re.search(r'["”]([.,])', no_power)
    if m:
        issues.append(_issue_at(
            WARNING,
            '{0}: "{1}" after a closing quote — American style puts periods and '
            'commas inside the quotes'.format(label, m.group(1)),
            'quote_punct', label,
            {'field': field, 'op': 'regex',
             'pattern': r'(["”]|&quot;|&#x22;|&#34;)([.,])', 'repl': r'\2\1'},
            no_power, m))
    return issues


_CONTRACTIONS = re.compile(
    r"\b(can't|won't|don't|doesn't|didn't|isn't|aren't|wasn't|weren't|hasn't|haven't|"
    r"hadn't|wouldn't|couldn't|shouldn't|mustn't|it's|that's|there's|here's|he's|she's|"
    r"what's|who's|let's|they're|we're|you're|they've|we've|you've|i've|they'll|we'll|"
    r"you'll|he'll|she'll|i'll|i'm|you'd|they'd|we'd|he'd|she'd|i'd)\b", re.IGNORECASE)


def _prose_issues(label, raw, field):
    """Prose-style rules from the Minkowski manual that apply to any text blob:
    contractions, repeated words, ampersands, and number ranges."""
    issues = []
    text = _plain(raw)

    seen = set()
    for m in _CONTRACTIONS.finditer(text):
        key = m.group(0).lower()
        if key in seen:
            continue
        seen.add(key)
        issues.append(_issue_at(WARNING, '{0}: contraction "{1}"'.format(label, m.group(0)),
                                'contractions', '{0}|{1}'.format(label, key), None, text, m))

    seen_rep = set()
    for m in re.finditer(r'\b(\w+)\s+\1\b', text, re.IGNORECASE):
        word = m.group(1).lower()
        if word in seen_rep:
            continue
        seen_rep.add(word)
        issues.append(_issue_at(WARNING, '{0}: repeated word "{1} {1}"'.format(label, m.group(1)),
                                'repeated_word', '{0}|{1}'.format(label, word),
                                {'field': field, 'op': 'regex', 'pattern': r'\b(\w+)\s+\1\b', 'repl': r'\1'},
                                text, m))

    seen_from = set()
    for m in re.finditer(r'\bfrom this (country|nation|empire|kingdom|city|state)\b', text, re.IGNORECASE):
        place = m.group(1).lower()
        if place in seen_from:
            continue
        seen_from.add(place)
        issues.append(_issue_at(
            INFO, '{0}: "from this {1}" → prefer "born in this {1}"'.format(label, place),
            'imprecise_from', '{0}|{1}'.format(label, place), None, text, m))

    m = re.search(r'&', text)
    if m:
        issues.append(_issue_at(INFO, '{0}: spell out "and" (not &)'.format(label), 'ampersand', label,
                                {'field': field, 'op': 'regex', 'pattern': r'\s*&\s*', 'repl': ' and '},
                                text, m))

    m = re.search(r'\d\s*-\s*\d', text)
    if m:
        issues.append(_issue_at(INFO, '{0}: use en dash (–) for ranges'.format(label),
                                'number_range', label,
                                {'field': field, 'op': 'regex', 'pattern': r'(\d)\s*-\s*(\d)', 'repl': r'\1–\2'},
                                text, m))
    return issues


def _pronunciation_issues(label, raw, field, require_quotes=False):
    """Suggest a verified-OL pronunciation guide for any dictionary term in the
    text that doesn't already have one (INFO, auto-applicable). Each suggestion
    shows the term in surrounding context so the editor can confirm the match
    is the intended sense (e.g. the proper noun, not a common-word homograph)."""
    plain = _plain(raw)
    issues = []
    for term, pron, start, end in suggest_guide_matches(plain, require_quotes):
        prefix, before, match, after, suffix = context_snippet(plain, start, end)
        message = '{0}: PG for "{1}" ({2}) — {3}{4}{5}{6}{7}'.format(
            label, term, pron, prefix, before, match, after, suffix)
        # Bold the matched term inside the (escaped) context so it stands out.
        message_html = '{0}: PG for "{1}" ({2}) — <span class="pg-context">{3}<strong>{4}</strong>{5}</span>'.format(
            _escape(label), _escape(term), _escape(pron),
            _escape(prefix + before), _escape(match), _escape(after + suffix))
        issues.append(_issue(
            INFO, message, 'pronunciation', '{0}|{1}'.format(label, term),
            {'field': field, 'op': 'guide', 'term': term, 'pron': pron},
            message_html=message_html))
    return issues


# A parenthetical that isn't an escaped literal paren; used to find guides.
_GUIDE_PAREN = re.compile(r'(?<!\\)\(([^()]*)\)')


def _guide_matches(text):
    """Every parenthetical that could be a pronunciation guide, in order. Power
    marks are excluded, but nothing else is — the *index* of a guide in this list
    is what an auto-fix is stored against (see mark_pg_target), so it must not
    depend on any per-set setting. Callers that only want real guides skip the
    ones they don't want and keep the enumeration intact."""
    return [m for m in _GUIDE_PAREN.finditer(text or '')
            if m.group(1) not in ('*', '+')]


def _not_a_guide(match, require_quotes):
    """True when a set requiring quoted respellings would read this parenthetical
    as ordinary text rather than a guide."""
    return require_quotes and not parenthetical_has_quotes(match.group(1))


def guide_word_count(inner):
    """How many words a guide's respelling covers, guessed from its whitespace:
    one respelled chunk per spoken word. `("zhahn-pohl SAR-truh")` covers the two
    words of "Jean-Paul Sartre". Returns 0 when there's nothing to count."""
    stripped = (inner or '').strip().strip('"“”\'’')
    return len(stripped.split())


def ends_with_pg_target(head):
    """True if `head` (the text in front of a guide) ends with a closing ``\\P``.

    Closing markup may sit between the marked word and its guide — a target
    inside italics reads ``~Death of the \\PDauphin\\P~ ("DOFF-in")`` — so any
    trailing italic/underline characters and ``\\S``/``\\s``/``\\B`` tokens are
    stepped over before looking for the marker."""
    head = (head or '').rstrip()
    while head:
        if head.endswith('\\P'):
            return True
        if head[-1] in ('_', '~'):
            head = head[:-1].rstrip()
        elif head[-2:] in ('\\S', '\\s', '\\B'):
            head = head[:-2].rstrip()
        else:
            return False
    return False


def mark_pg_target(text, guide_index):
    """Wrap the word(s) preceding the `guide_index`-th pronunciation guide in
    ``\\P...\\P``, guessing how many words the guide covers from its word count.
    Returns `text` unchanged if the guide can't be found or there aren't enough
    words in front of it."""
    guides = _guide_matches(text)
    if guide_index >= len(guides):
        return text
    m = guides[guide_index]
    n = guide_word_count(m.group(1))
    if n <= 0:
        return text

    head = text[:m.start()]
    trailing = head[len(head.rstrip()):]  # whitespace between target and guide
    head = head.rstrip()
    if ends_with_pg_target(head):
        return text  # already marked

    words = head.split(' ')
    if len(words) < n or not all(w.strip() for w in words[-n:]):
        return text
    target = ' '.join(words[-n:])
    if '\\P' in target:
        return text
    return '{0}\\P{1}\\P{2}{3}'.format(
        ' '.join(words[:-n]) + (' ' if len(words) > n else ''),
        target, trailing, text[m.start():])


def fix_pg_possessive(text, guide_index):
    """Move the possessive that trails the `guide_index`-th pronunciation guide
    back onto the word it belongs to, and respell the guide to cover it:
    ``Saatchi ("SAH-chee")'s`` -> ``Saatchi's ("SAH-cheez")``. An existing
    ``\\P...\\P`` mark grows to include the possessive. Returns `text` unchanged
    if that guide isn't followed by one."""
    guides = _guide_matches(text)
    if guide_index >= len(guides):
        return text
    m = guides[guide_index]
    pm = _POSSESSIVE_RE.match(text, m.end())
    if not pm:
        return text
    poss = pm.group(0)

    head = text[:m.start()]
    gap = head[len(head.rstrip()):]  # whitespace between the word and its guide
    head = head.rstrip()
    if not head:
        return text
    # The possessive goes on the word itself — inside any \P mark and any
    # closing italic/underline markup that sits between the word and the guide.
    at = len(head)
    while at > 0:
        if head[at - 2:at] in _MARKUP_TOKENS:
            at -= 2
        elif head[at - 1] in ('_', '~'):
            at -= 1
        else:
            break
    if at == 0:
        return text
    inner = m.group(1)
    quote = inner[:1] if inner[:1] in ('"', '“') else ''
    close = inner[-1:] if quote and inner[-1:] in ('"', '”') else ''
    body = inner[len(quote):len(inner) - len(close)] if close else inner[len(quote):]
    if _possessive_sounds(poss):
        body = possessive_respelling(body)
    guide = '({0}{1}{2})'.format(quote, body, close)
    return (head[:at] + poss + head[at:] + gap + guide + text[pm.end():])


def _pg_possessive_issues(label, raw, field, require_quotes=False):
    """Flag a pronunciation guide that splits a possessive —
    ``Saatchi ("SAH-chee")'s`` — since the word is read as one. The fix moves
    the possessive onto the word and respells the guide to match."""
    text = raw or ''
    issues = []
    for idx, m in enumerate(_guide_matches(text)):
        if _not_a_guide(m, require_quotes):
            continue
        pm = _POSSESSIVE_RE.match(text, m.end())
        if not pm:
            continue
        fix = None
        if fix_pg_possessive(text, idx) != text:
            fix = {'field': field, 'op': 'pg_possessive', 'idx': idx}
        message = ('{0}: pronunciation guide {1} splits the possessive "{2}" — keep the '
                   'possessive on the word and respell the guide to cover it'.format(
                       label, m.group(0), pm.group(0)))
        issues.append(_issue_at(WARNING, message, 'pg_possessive',
                                '{0}|{1}|{2}'.format(label, idx, m.group(0)), fix, text, m))
    return issues


def _pg_span_issues(label, raw, field, require_quotes=False):
    """Flag a pronunciation guide ``("...")`` whose spoken word(s) aren't wrapped
    in ``\\P...\\P``. Marking the target ties the guide to exactly the word(s) it
    covers (used for audio and rich rendering). The auto-fix guesses the target
    from the guide's word count, so the editor should check what it picked.
    Power marks ``(*)``/``(+)`` are not guides, and neither is an unquoted aside
    when the set requires quoted respellings."""
    text = raw or ''
    issues = []
    for idx, m in enumerate(_guide_matches(text)):
        if _not_a_guide(m, require_quotes):
            continue
        # A \P closing the target span should sit just before the '(' (any
        # whitespace, or closing italic/underline markup, in between is fine).
        if ends_with_pg_target(text[:m.start()]):
            continue
        guide = m.group(0)
        fix = None
        if mark_pg_target(text, idx) != text:
            fix = {'field': field, 'op': 'pg_span', 'idx': idx}
        issues.append(_issue_at(
            INFO,
            '{0}: pronunciation guide {1} has no marked target — mark the word(s) '
            'it covers (\\P…\\P)'.format(label, guide),
            'pg_span', '{0}|{1}|{2}'.format(label, idx, guide), fix, text, m))
    return issues


def _has_underline(raw):
    return '_' in (raw or '')


# Answer-line markup, in the order the alternation must be tried: `__x__` is
# underline-only (a prompt target), `_x_` is bold + underlined (a required or
# acceptable answer), and `\Bx\B` is bold on its own, which has no standard
# meaning on an answer line.
_ANSWER_RUN_RE = re.compile(r'(?<!\\)__([^_]+)__|(?<![\\_])_([^_]+)_(?!_)|\\B(.+?)\\B')
_ANSWER_DIRECTIVE_RE = re.compile(r'(?i)\b(anti-?prompt|prompt|accept|reject)\b')


def _last_directive(raw, upto):
    """The clause keyword governing the markup run at `upto` ('prompt',
    'accept', 'reject'), or '' for the primary answer. Antiprompts follow the
    same underline-only convention as prompts."""
    found = _ANSWER_DIRECTIVE_RE.findall(raw[:upto])
    if not found:
        return ''
    last = found[-1].lower()
    return 'prompt' if 'prompt' in last else last


def _answer_format_issues(label, raw):
    """Flag answer-line formatting that departs from the convention: answers
    that are accepted are bold + underlined, prompt targets are underlined
    only, and nothing is bolded without being underlined."""
    if not (raw or '').strip():
        return []
    issues = []
    for n, m in enumerate(_ANSWER_RUN_RE.finditer(raw)):
        underline_only, bold_underline, bold_only = m.group(1), m.group(2), m.group(3)
        directive = _last_directive(raw, m.start())
        token = '{0}|{1}'.format(label, n)
        if bold_only is not None:
            issues.append(_issue(
                WARNING,
                '{0}: "{1}" is bolded but not underlined — underline the required '
                'portion instead (_…_ renders bold and underlined)'.format(label, bold_only),
                'answer_format', token))
        elif bold_underline is not None and directive == 'prompt':
            issues.append(_issue(
                WARNING,
                '{0}: prompt target "{1}" is bolded — a prompt should be underlined '
                'only (__…__)'.format(label, bold_underline),
                'answer_format', token))
        elif underline_only is not None and directive != 'prompt':
            issues.append(_issue(
                INFO,
                '{0}: "{1}" is underlined but not bolded — an answer that is accepted '
                'should be bold and underlined (_…_)'.format(label, underline_only),
                'answer_format', token))
    return issues


# What makes a prompt *directed*: it tells the moderator what to say. "by
# asking", "by saying" and the like are the standard phrasings; a quoted
# question after "with" ("prompt on __Louis__ with \"which Louis?\"") says the
# same thing. The open "by <verb>ing" form catches the rest ("by requesting the
# regnal number") without listing every verb a writer might reach for.
_DIRECTED_PROMPT_RE = re.compile(
    r'(?i)\bby\s+[a-z]+ing\b'
    r'|\bwith\s*["“‘\']'
    r'|\bask(?:ing)?\s*["“‘\']')

# Where a prompt clause ends: the next directive, or a separator that starts a
# new one. Without this, "prompt on __Louis__; accept __Louis XIV__ by ..." would
# look directed because a later clause happens to contain the phrasing. "or" is
# deliberately not a boundary — "prompt on __Louis__ or __Louis the Great__ by
# asking ..." is one clause with two targets.
_CLAUSE_END_RE = re.compile(r'(?i)[;\]]|\banti-?prompt\b|\bprompt\b|\baccept\b|\breject\b')


def _prompt_target(clause):
    """The word(s) a prompt clause is about: its first markup run ("__Louis__"),
    or the words after "on" when the writer didn't mark one."""
    m = _ANSWER_RUN_RE.search(clause)
    if m:
        return (m.group(1) or m.group(2) or m.group(3) or '').strip()
    m = re.search(r'(?i)\bon\s+(.{1,40}?)(?:[;,\]]|$)', clause)
    return _plain(m.group(1)).strip() if m else ''


def _prompt_direction_issues(label, raw):
    """Flag a prompt that doesn't tell the moderator what to say.

    "Prompt on __Louis__" leaves the moderator to invent the follow-up, and two
    moderators inventing different ones is the whole reason directed prompts
    exist. "Prompt on __Louis__ by asking for the regnal number" says it once,
    in the packet.
    """
    if not (raw or '').strip():
        return []
    issues = []
    for n, m in enumerate(_ANSWER_DIRECTIVE_RE.finditer(raw)):
        directive = m.group(1).lower()
        if 'prompt' not in directive:
            continue

        end = _CLAUSE_END_RE.search(raw, m.end())
        clause = raw[m.end():end.start() if end else len(raw)]
        if _DIRECTED_PROMPT_RE.search(clause):
            continue

        target = _prompt_target(clause)
        quoted = ' "{0}"'.format(target) if target else ''
        message = ('{0}: undirected {1}{2} — say what the moderator should ask '
                   '("by asking ...")'.format(label, directive, quoted))
        issues.append(_issue_at(
            WARNING, message, 'prompt_undirected',
            '{0}|{1}'.format(label, target or n), None, raw, m))
    return issues


def _answer_alt_issues(label, raw_answer):
    """Suggest standard acceptable alternates the answer line is missing, looked
    up by primary answer in the bundled answer database (INFO, not auto-fixed —
    the editor decides which alternates apply)."""
    from .answer_db import missing_alternates
    head_key, missing = missing_alternates(raw_answer)
    if not missing:
        return []
    shown = missing[:6]
    suffix = '' if len(missing) <= len(shown) else ' …'
    # Standard answer-line phrasing: names joined by "; or", each shown the way
    # it would appear on the line — underlined + bold, and italicized too when
    # the primary answer is an italicized title.
    italic = '~' in (raw_answer or '').split('[', 1)[0]

    def fmt(name):
        h = '<u><b>{0}</b></u>'.format(_escape(name))
        return '<i>{0}</i>'.format(h) if italic else h

    return [_issue(INFO, '{0}: also accept {1}{2}'.format(label, '; or '.join(shown), suffix),
                   'answer_alts', '{0}|{1}'.format(label, head_key),
                   message_html='{0}: also accept {1}{2}'.format(
                       _escape(label), '; or '.join(fmt(n) for n in shown), suffix))]


# Prepositions that introduce a trailing phrase the identifier can sit in. When
# the answer cue ("this artist") is the object of one of these AND lands late in
# the opening sentence, the phrase can usually be fronted to surface the cue
# earlier ("...by this artist" -> "By this artist, ...").
_IDENT_PREPS = {
    'in', 'on', 'at', 'by', 'for', 'with', 'of', 'from', 'to', 'within', 'during',
    'throughout', 'about', 'near', 'into', 'onto', 'upon', 'over', 'under', 'around',
    'among', 'amongst', 'across', 'against', 'toward', 'towards', 'via',
}


def _first_sentence(plain):
    """The opening sentence of `plain` (best-effort; splits on . ! ? + space)."""
    m = re.search(r'[.!?]\s', plain)
    return plain[:m.start() + 1] if m else plain


def _late_identifier_issues(label, raw):
    """Flag a tossup whose first-sentence answer cue ("this/these X") is the
    object of a preposition and sits late in the sentence — i.e. the phrase
    holding the cue could be moved to the front so it reads earlier."""
    plain = _plain(raw).strip()
    if not plain:
        return []
    words = _first_sentence(plain).split()
    n = len(words)
    if n < 10:
        return []

    def clean(w):
        return w.strip('.,;:!?()"‘’\'').lower()

    idx = next((i for i, w in enumerate(words) if clean(w) in ('this', 'these')), None)
    if idx is None or idx < 6:
        return []
    # The cue must be the object of a preposition (a trailing phrase) and land in
    # the back portion of the sentence.
    if clean(words[idx - 1]) not in _IDENT_PREPS:
        return []
    if idx / float(n) < 0.55:
        return []
    nxt = clean(words[idx + 1]) if idx + 1 < n else ''
    if not nxt.isalpha():
        return []
    ident = '{0} {1}'.format(clean(words[idx]), nxt)
    return [_issue(
        INFO,
        '{0}: the cue "{1}" comes late in the first sentence; consider fronting the '
        '"{2} {1} ..." phrase so the answer cue appears earlier.'.format(label, ident, clean(words[idx - 1])),
        'late_identifier', '{0}|{1}'.format(label, ident))]


def _requires_quoted_guides(question):
    """Whether this question's set only reads a quoted parenthetical as a
    pronunciation guide. Tolerates a bare stand-in object (the style-check
    preview posts unsaved text)."""
    getter = getattr(question, 'guides_require_quotes', None)
    try:
        return bool(getter()) if callable(getter) else bool(getter)
    except Exception:
        return False


# An answer cue: "this"/"these" plus the noun it points at. Quizbowl uses only
# these two determiners for the cue, so "that"/"those" (ordinary prose) are out.
_IDENT_CUE_RE = re.compile(r'\b(this|these)\s+([A-Za-z][A-Za-z\-]*)\b', re.IGNORECASE)

# Nouns whose singular already ends in "s" — a naive de-pluralizer would turn
# "this species" into "specie" and then never match "these species".
_S_SINGULARS = {
    'species', 'series', 'crisis', 'thesis', 'analysis', 'basis', 'axis', 'genesis',
    'hypothesis', 'oasis', 'campus', 'virus', 'census', 'chorus', 'corpus', 'nucleus',
    'process', 'class', 'glass', 'mass', 'pass', 'press', 'business', 'congress',
    'address', 'goddess', 'princess', 'canvas', 'atlas', 'bias', 'gas', 'lens',
    'news', 'physics', 'mathematics', 'politics', 'economics', 'ethics',
}

# Irregular plurals that carry the answer often enough to be worth knowing.
_IRREGULAR_PLURALS = {
    'men': 'man', 'women': 'woman', 'children': 'child', 'people': 'person',
    'peoples': 'people', 'phenomena': 'phenomenon', 'criteria': 'criterion',
    'media': 'medium', 'data': 'datum', 'bacteria': 'bacterium', 'curricula': 'curriculum',
    'symposia': 'symposium', 'formulae': 'formula', 'indices': 'index',
    'appendices': 'appendix', 'matrices': 'matrix', 'vertices': 'vertex',
    'analyses': 'analysis', 'bases': 'basis', 'crises': 'crisis', 'theses': 'thesis',
    'hypotheses': 'hypothesis', 'oases': 'oasis', 'axes': 'axis',
    'feet': 'foot', 'teeth': 'tooth', 'geese': 'goose', 'mice': 'mouse',
}


def _singular_stem(noun):
    """A best-effort singular form of `noun`, used only to decide whether two
    cues name the same thing. Wrong guesses cost a missed report, never a false
    one: an unrecognized plural simply keys to itself and matches nothing."""
    w = (noun or '').lower()
    if w in _IRREGULAR_PLURALS:
        return _IRREGULAR_PLURALS[w]
    if w in _S_SINGULARS or not w.endswith('s'):
        return w
    if w.endswith('ies') and len(w) > 4:
        return w[:-3] + 'y'
    for suffix in ('ches', 'shes', 'sses', 'xes', 'zes'):
        if w.endswith(suffix):
            return w[:-2]
    if w.endswith('ss'):        # "this glass" — not a plural at all
        return w
    return w[:-1]


def _mixed_identifier_issues(label, raw):
    """Flag a question that names its answer both ways — "these animals" early
    and "this animal" later. One question has one answer, so the cue has to pick
    a number and keep it; a reader who hears both doesn't know what shape of
    answer to give.

    Matching is by the noun's singular stem, so only the *same* cue counts:
    "this novel" alongside "these poems" is ordinary writing, not a mismatch.
    There is no auto-fix — which number is right depends on the answer line."""
    text = _plain(raw)
    if not text:
        return []

    # stem -> {'this'|'these': first match}. The report points at the second
    # form to appear, which is the one that breaks the pattern already set.
    seen = {}
    issues = []
    reported = set()
    for m in _IDENT_CUE_RE.finditer(text):
        det = m.group(1).lower()
        stem = _singular_stem(m.group(2))
        forms = seen.setdefault(stem, {})
        other = 'these' if det == 'this' else 'this'
        if other in forms and stem not in reported:
            reported.add(stem)
            first = forms[other].group(0)
            message = ('{0}: the answer cue switches number — "{1}" and "{2}" '
                       'both refer to the answer; pick one and use it '
                       'throughout'.format(label, first, m.group(0)))
            issues.append(_issue_at(WARNING, message, 'mixed_identifier',
                                    '{0}|{1}'.format(label, stem), None, text, m))
        forms.setdefault(det, m)
    return issues


def check_tossup(tu, guide=DEFAULT_GUIDE, disabled=None):
    enabled = _enabled_codes(guide, disabled)
    issues = []
    text = tu.tossup_text or ''
    plain = _plain(text)
    quoted = _requires_quoted_guides(tu)

    issues += _mechanical_issues('Question', text, 'tossup_text')
    issues += _prose_issues('Question', text, 'tossup_text')
    issues += _late_identifier_issues('Question', text)
    issues += _mixed_identifier_issues('Question', text)

    m = re.search(r'\banswers?\s*:', plain, re.IGNORECASE)
    if m:
        issues.append(_issue_at(WARNING, '"ANSWER:" in question text', 'answer_leak', '', None,
                                plain, m))

    m = re.search(r'for ten points', plain, re.IGNORECASE)
    if m:
        issues.append(_issue_at(WARNING, 'use numerals: "For 10 points"',
                                'numerals', 'tossup_text',
                                {'field': 'tossup_text', 'op': 'regex',
                                 'pattern': r'(?i)for ten points', 'repl': 'For 10 points'},
                                plain, m))

    if not re.search(r'for \d+ points', plain, re.IGNORECASE):
        issues.append(_issue(INFO, 'no "For 10 points" phrase', 'fps'))

    if text.count('(*)') > 1:
        issues.append(_issue(WARNING, 'more than one power mark (*)', 'power'))
    if text.count('(+)') > 1:
        issues.append(_issue(WARNING, 'more than one superpower mark (+)', 'power'))
    # A 20-point superpower "(+)" should precede the 15-point power "(*)".
    if '(+)' in text and '(*)' in text and text.find('(+)') > text.find('(*)'):
        issues.append(_issue(WARNING, 'superpower (+) should come before the power (*)', 'power'))

    if plain.rstrip().endswith('?'):
        # Point at the giveaway itself — the question mark and the words leading
        # up to it are what has to be reworded.
        end = len(plain.rstrip())
        msg = 'interrogative giveaway; prefer imperative'
        issues.append(_issue(WARNING, msg, 'imperative', '', None,
                             _context_html(msg, plain, end - 1, end)))

    if not _has_underline(tu.tossup_answer):
        issues.append(_issue(WARNING, 'answer not underlined', 'underline'))

    if 'answer_format' in enabled:
        issues += _answer_format_issues('Answer', tu.tossup_answer)

    if 'answer_alts' in enabled:
        issues += _answer_alt_issues('Answer', tu.tossup_answer)

    if 'prompt_undirected' in enabled:
        issues += _prompt_direction_issues('Answer', tu.tossup_answer)

    if 'pronunciation' in enabled:
        issues += _pronunciation_issues('Question', text, 'tossup_text', quoted)

    if 'pg_span' in enabled:
        issues += _pg_span_issues('Question', text, 'tossup_text', quoted)

    if 'pg_possessive' in enabled:
        issues += _pg_possessive_issues('Question', text, 'tossup_text', quoted)

    return [i for i in issues if i['code'] in enabled]


def check_bonus(b, guide=DEFAULT_GUIDE, disabled=None):
    enabled = _enabled_codes(guide, disabled)
    issues = []
    quoted = _requires_quoted_guides(b)
    leadin = b.leadin or ''
    parts = [('Leadin', leadin, 'leadin'),
             ('Part 1', b.part1_text or '', 'part1_text'),
             ('Part 2', b.part2_text or '', 'part2_text'),
             ('Part 3', b.part3_text or '', 'part3_text')]

    for label, raw, field in parts:
        if raw.strip():
            issues += _mechanical_issues(label, raw, field)
            issues += _prose_issues(label, raw, field)
            # Per part, not across the bonus: each part has its own answer, so
            # "this novel" in one and "these poems" in another is correct.
            issues += _mixed_identifier_issues(label, raw)

    plain_leadin = _plain(leadin)
    m = re.search(r'for ten points each', plain_leadin, re.IGNORECASE)
    if m:
        issues.append(_issue_at(WARNING, 'Leadin: use numerals: "For 10 points each"',
                                'numerals', 'leadin',
                                {'field': 'leadin', 'op': 'regex',
                                 'pattern': r'(?i)for ten points each', 'repl': 'For 10 points each'},
                                plain_leadin, m))
    if not re.search(r'for \d+ points each', plain_leadin, re.IGNORECASE):
        issues.append(_issue(INFO, 'Leadin: no "For 10 points each"', 'fpe', 'leadin'))

    for label, raw, field in parts:
        if '(*)' in raw or '(+)' in raw:
            mark = '(*)' if '(*)' in raw else '(+)'
            issues.append(_issue(WARNING, '{0}: power mark {1} not allowed in bonus'.format(label, mark),
                                 'power', label))

    for label, ans in (('Answer 1', b.part1_answer), ('Answer 2', b.part2_answer), ('Answer 3', b.part3_answer)):
        if (ans or '').strip() and not _has_underline(ans):
            issues.append(_issue(WARNING, '{0}: not underlined'.format(label),
                                 'underline', label))

    if 'answer_format' in enabled:
        for label, ans in (('Answer 1', b.part1_answer), ('Answer 2', b.part2_answer),
                           ('Answer 3', b.part3_answer)):
            issues += _answer_format_issues(label, ans)

    if 'answer_alts' in enabled:
        for label, ans in (('Answer 1', b.part1_answer), ('Answer 2', b.part2_answer), ('Answer 3', b.part3_answer)):
            if (ans or '').strip():
                issues += _answer_alt_issues(label, ans)

    if 'prompt_undirected' in enabled:
        for label, ans in (('Answer 1', b.part1_answer), ('Answer 2', b.part2_answer), ('Answer 3', b.part3_answer)):
            issues += _prompt_direction_issues(label, ans)

    if 'pronunciation' in enabled:
        for label, raw, field in parts:
            if raw.strip():
                issues += _pronunciation_issues(label, raw, field, quoted)

    if 'pg_span' in enabled:
        for label, raw, field in parts:
            if raw.strip():
                issues += _pg_span_issues(label, raw, field, quoted)

    if 'pg_possessive' in enabled:
        for label, raw, field in parts:
            if raw.strip():
                issues += _pg_possessive_issues(label, raw, field, quoted)

    return [i for i in issues if i['code'] in enabled]


# --- auto-apply ------------------------------------------------------------

# QEMS inline markup that interleaves with words: underline/italic chars and the
# backslash escape tokens. We ignore these when locating a term so a clued term
# wrapped in markup (e.g. "_Goethe_") still matches and the guide lands after it.
_MARKUP_TOKENS = ('\\S', '\\s', '\\B', '\\P')


def _strip_markup_indexed(text):
    """Return (clean, idx_map): `clean` is `text` with QEMS inline markup removed,
    and idx_map[i] is the index in `text` of clean[i] (with a final sentinel
    mapping len(clean) -> len(text)), so a match in `clean` maps back to `text`."""
    clean, idx_map = [], []
    i, n = 0, len(text)
    while i < n:
        if text[i:i + 2] in _MARKUP_TOKENS:
            i += 2
        elif text[i] in ('_', '~'):
            i += 1
        else:
            clean.append(text[i])
            idx_map.append(i)
            i += 1
    idx_map.append(n)
    return ''.join(clean), idx_map


# A possessive ending: "'s" / "’s", or the bare apostrophe of a plural
# possessive ("Jones' letters"). A guide must never be dropped in front of one —
# «Saatchi ("SAH-chee")'s» splits a word that is read as one.
_POSSESSIVE_RE = re.compile(r"['’]s(?![\w'’])|(?<=[sS])['’](?![\w'’])")

# Respelling endings that decide how a possessive is said: a sibilant takes an
# extra syllable ("BUSH" -> "BUSH-iz"), a voiceless consonant takes /s/
# ("BAHK" -> "BAHKS"), everything else takes /z/ ("SAH-chee" -> "SAH-cheez").
_SIBILANT_ENDINGS = ('sh', 'ch', 'zh', 'ge', 'ce', 'se', 'ss', 'zz', 'dg')
_VOICELESS_ENDINGS = ('p', 't', 'k', 'f', 'th', 'ph', 'ck')


def possessive_respelling(pron):
    """`pron` with the sound the possessive adds, so the guide still matches the
    word it covers once the possessive is folded into the target: "SAH-chee" ->
    "SAH-cheez". Case follows the respelling's last letter, which carries the
    stress convention (all-caps syllables). Returns `pron` unchanged when it
    already ends in the possessive sound."""
    body = (pron or '').rstrip()
    tail = pron[len(body):]
    letters = re.sub(r'[^A-Za-z]+$', '', body)
    if not letters:
        return pron
    low = letters.lower()
    if low.endswith(_SIBILANT_ENDINGS) or low[-1] in ('s', 'z', 'x', 'j'):
        # An added syllable — unless it's already there ("BUSH-iz").
        if low.endswith(('iz', 'ez', 'es', 'is')):
            return pron
        add = '-iz'
    elif low.endswith('th') or low[-1] in _VOICELESS_ENDINGS:
        add = 's'
    else:
        add = 'z'
    if letters[-1].isupper():
        add = add.upper()
    return body[:len(body) - len(letters)] + letters + add + tail


def _possessive_sounds(poss):
    """True if the possessive adds a sound to the spoken word. "Saatchi's" does;
    the bare apostrophe of a plural possessive ("Jones' letters") doesn't, so
    its respelling is left alone."""
    return bool(poss) and poss[-1] in ('s', 'S')


def _absorb_possessive(text, pos):
    """The end of a possessive ending that starts at `pos` in `text`, or `pos`
    itself when there isn't one."""
    m = _POSSESSIVE_RE.match(text, pos)
    return m.end() if m else pos


def _insert_guide(text, term, pron):
    """Insert ``("RESPELLING")`` after the first occurrence of `term` that isn't
    already followed by a guide, wrapping the term in ``\\P...\\P`` so the guide
    is tied to exactly the word(s) it covers. Matching ignores QEMS inline
    markup so an underlined/italicized term still matches, and the guide is
    placed after any closing markup (the \\P wrap is skipped there — it would
    misnest with the other markup).

    A possessive goes with the term rather than being split off by the guide:
    "Saatchi's" becomes ``\\PSaatchi's\\P ("SAH-cheez")``, not
    ``\\PSaatchi\\P ("SAH-chee")'s``. Returns the text unchanged if no
    occurrence is found."""
    clean, idx_map = _strip_markup_indexed(text)
    pat = re.compile(r'(?<!\w)' + re.escape(term) + r'(?!\w)', re.IGNORECASE)
    for m in pat.finditer(clean):
        start = idx_map[m.start()]
        pos = idx_map[m.end()]  # raw index after the term (past any closing markup)
        end = _absorb_possessive(text, pos)
        guide = ' ("{0}")'.format(
            possessive_respelling(pron) if _possessive_sounds(text[pos:end]) else pron)
        if guide_opener_at(text, end):
            continue
        raw_term = text[start:end]
        if raw_term.lower() == m.group(0).lower() + text[pos:end].lower():
            return text[:start] + '\\P' + raw_term + '\\P' + guide + text[end:]
        return text[:end] + guide + text[end:]
    return text


def apply_fix(question, fix):
    """Apply a server-computed `fix` to `question` in place. Returns True if the
    text actually changed. The caller is responsible for saving."""
    field = (fix or {}).get('field')
    if not field or not hasattr(question, field):
        return False
    text = getattr(question, field) or ''
    op = fix.get('op')
    if op == 'regex':
        new = re.sub(fix['pattern'], fix['repl'], text)
    elif op == 'guide':
        new = _insert_guide(text, fix['term'], fix['pron'])
    elif op == 'pg_span':
        new = mark_pg_target(text, fix['idx'])
    elif op == 'pg_possessive':
        new = fix_pg_possessive(text, fix['idx'])
    else:
        return False
    if new == text:
        return False
    setattr(question, field, new)
    return True


def find_fix(question, qtype, code, token, guide=DEFAULT_GUIDE):
    """Re-run the checker for `question` and return the `fix` dict of the issue
    matching (code, token), or None. Recomputing server-side means the client
    never supplies the transform."""
    issues = check_tossup(question, guide) if qtype == 'tossup' else check_bonus(question, guide)
    for i in issues:
        if i.get('code') == code and i.get('token') == token:
            return i.get('fix')
    return None
