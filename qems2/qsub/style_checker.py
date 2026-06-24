"""Style checker for quizbowl questions.

Runs a set of mechanically-checkable style rules over tossups and bonuses. The
rule set is selectable per "style guide"; the default is the guide at
https://minkowski.space/quizbowl/manuals/style/index.html. Each guide turns a
common pool of rules on or off.

Each issue is a dict:
    {'severity': 'error'|'warning'|'info',
     'message': str,
     'code': str,          # stable rule id
     'token': str,         # distinguishes issues of the same code in a question
     'fix': {...}}         # OPTIONAL, server-side only: how to auto-apply it

The (code, token) pair identifies an issue for dismissal/apply. `fix` (when
present) is applied server-side by apply_fix(); it is never sent by the client.
"""

import re

from .utils import strip_markup
from .pron_dict import suggest_guides, guide_opener_at

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
    ('ellipsis', 'Ellipsis (… instead of ...)'),
    ('double_hyphen', 'Em dash (— instead of --)'),
    ('number_range', 'En dash for number ranges (1990–1995)'),
    ('ampersand', 'Spell out "and" instead of &'),
    ('repeated_word', 'Repeated words ("the the")'),
    ('contractions', 'Contractions (don\'t, it\'s, …)'),
    ('imprecise_from', 'Imprecise "from this country" (prefer "born in")'),
    ('unbalanced_parens', 'Unbalanced parentheses'),
    ('answer_leak', 'ANSWER: leaked into question text'),
    ('numerals', 'Numerals in "For 10 points"'),
    ('fps', 'Missing "For 10 points" (tossup)'),
    ('fpe', 'Missing "For 10 points each" (bonus)'),
    ('power', 'Power-mark problems'),
    ('imperative', 'Interrogative giveaway'),
    ('underline', 'Answer line has no underline'),
    ('pronunciation', 'Pronunciation-guide suggestions'),
    ('answer_alts', 'Answer line missing standard alternates'),
]
RULE_LABEL_MAP = dict(RULE_LABELS)
ALL_CODES = [c for c, _ in RULE_LABELS]

# Which rule codes each guide turns on. A per-set "disabled" list can switch any
# of these off (e.g. teams that allow contractions).
GUIDE_CODES = {
    'minkowski': set(ALL_CODES),
    'generic': {'double_space', 'space_before_punct', 'comma_no_space', 'ellipsis',
                'double_hyphen', 'unbalanced_parens', 'repeated_word', 'underline'},
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


def _issue(severity, message, code, token='', fix=None):
    d = {'severity': severity, 'message': message, 'code': code, 'token': token}
    if fix:
        d['fix'] = fix
    return d


def _plain(text):
    """Readable plain text: strip HTML/smart quotes (strip_markup) plus QEMS
    markup characters, so style checks see the words as read."""
    t = strip_markup(text or '')
    for marker in ('\\S', '\\s', '\\B'):
        t = t.replace(marker, '')
    return t.replace('_', '').replace('~', '')


def _mechanical_issues(label, raw, field):
    """Spacing/punctuation/typography rules that apply to any text blob. The
    fixable ones carry a `fix` keyed to `field` so they can be auto-applied."""
    issues = []
    text = _plain(raw)
    no_power = text.replace('(*)', '')
    if '  ' in text:
        issues.append(_issue(WARNING, '{0}: double space'.format(label), 'double_space', label,
                             {'field': field, 'op': 'regex', 'pattern': r' {2,}', 'repl': ' '}))
    if re.search(r'\s[,.;:!?]', no_power):
        issues.append(_issue(WARNING, '{0}: space before punctuation'.format(label),
                             'space_before_punct', label,
                             {'field': field, 'op': 'regex', 'pattern': r'[ \t]+([,.;:!?])', 'repl': r'\1'}))
    if re.search(r',[A-Za-z]', text):
        issues.append(_issue(WARNING, '{0}: missing space after comma'.format(label),
                             'comma_no_space', label,
                             {'field': field, 'op': 'regex', 'pattern': r',([A-Za-z])', 'repl': r', \1'}))
    if '...' in text:
        issues.append(_issue(INFO, '{0}: use ellipsis (…)'.format(label),
                             'ellipsis', label,
                             {'field': field, 'op': 'regex', 'pattern': r'\.{3,}', 'repl': '…'}))
    if '--' in text:
        issues.append(_issue(INFO, '{0}: use em dash (—)'.format(label),
                             'double_hyphen', label,
                             {'field': field, 'op': 'regex', 'pattern': r'-{2,}', 'repl': '—'}))
    if no_power.count('(') != no_power.count(')'):
        issues.append(_issue(WARNING, '{0}: unbalanced parentheses'.format(label),
                             'unbalanced_parens', label))
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
        issues.append(_issue(WARNING, '{0}: contraction "{1}"'.format(label, m.group(0)),
                             'contractions', '{0}|{1}'.format(label, key)))

    seen_rep = set()
    for m in re.finditer(r'\b(\w+)\s+\1\b', text, re.IGNORECASE):
        word = m.group(1).lower()
        if word in seen_rep:
            continue
        seen_rep.add(word)
        issues.append(_issue(WARNING, '{0}: repeated word "{1} {1}"'.format(label, m.group(1)),
                             'repeated_word', '{0}|{1}'.format(label, word),
                             {'field': field, 'op': 'regex', 'pattern': r'\b(\w+)\s+\1\b', 'repl': r'\1'}))

    seen_from = set()
    for m in re.finditer(r'\bfrom this (country|nation|empire|kingdom|city|state)\b', text, re.IGNORECASE):
        place = m.group(1).lower()
        if place in seen_from:
            continue
        seen_from.add(place)
        issues.append(_issue(
            INFO, '{0}: "from this {1}" → prefer "born in this {1}"'.format(label, place),
            'imprecise_from', '{0}|{1}'.format(label, place)))

    if '&' in text:
        issues.append(_issue(INFO, '{0}: spell out "and" (not &)'.format(label), 'ampersand', label,
                             {'field': field, 'op': 'regex', 'pattern': r'\s*&\s*', 'repl': ' and '}))

    if re.search(r'\d\s*-\s*\d', text):
        issues.append(_issue(INFO, '{0}: use en dash (–) for ranges'.format(label),
                             'number_range', label,
                             {'field': field, 'op': 'regex', 'pattern': r'(\d)\s*-\s*(\d)', 'repl': r'\1–\2'}))
    return issues


def _pronunciation_issues(label, raw, field):
    """Suggest a verified-OL pronunciation guide for any dictionary term in the
    text that doesn't already have one (INFO, auto-applicable)."""
    issues = []
    for term, pron in suggest_guides(_plain(raw)):
        issues.append(_issue(
            INFO, '{0}: PG for "{1}" ({2})'.format(label, term, pron),
            'pronunciation', '{0}|{1}'.format(label, term),
            {'field': field, 'op': 'guide', 'term': term, 'pron': pron}))
    return issues


def _has_underline(raw):
    return '_' in (raw or '')


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
    return [_issue(INFO, '{0}: also accept {1}{2}'.format(label, ' / '.join(shown), suffix),
                   'answer_alts', '{0}|{1}'.format(label, head_key))]


def check_tossup(tu, guide=DEFAULT_GUIDE, disabled=None):
    enabled = _enabled_codes(guide, disabled)
    issues = []
    text = tu.tossup_text or ''
    plain = _plain(text)

    issues += _mechanical_issues('Question', text, 'tossup_text')
    issues += _prose_issues('Question', text, 'tossup_text')

    if re.search(r'\banswers?\s*:', plain, re.IGNORECASE):
        issues.append(_issue(WARNING, '"ANSWER:" in question text', 'answer_leak'))

    if re.search(r'for ten points', plain, re.IGNORECASE):
        issues.append(_issue(WARNING, 'use numerals: "For 10 points"',
                             'numerals', 'tossup_text',
                             {'field': 'tossup_text', 'op': 'regex',
                              'pattern': r'(?i)for ten points', 'repl': 'For 10 points'}))

    if not re.search(r'for \d+ points', plain, re.IGNORECASE):
        issues.append(_issue(INFO, 'no "For 10 points" phrase', 'fps'))

    if text.count('(*)') > 1:
        issues.append(_issue(WARNING, 'more than one power mark (*)', 'power'))

    if plain.rstrip().endswith('?'):
        issues.append(_issue(WARNING, 'interrogative giveaway; prefer imperative', 'imperative'))

    if not _has_underline(tu.tossup_answer):
        issues.append(_issue(WARNING, 'answer not underlined', 'underline'))

    if 'answer_alts' in enabled:
        issues += _answer_alt_issues('Answer', tu.tossup_answer)

    if 'pronunciation' in enabled:
        issues += _pronunciation_issues('Question', text, 'tossup_text')

    return [i for i in issues if i['code'] in enabled]


def check_bonus(b, guide=DEFAULT_GUIDE, disabled=None):
    enabled = _enabled_codes(guide, disabled)
    issues = []
    leadin = b.leadin or ''
    parts = [('Leadin', leadin, 'leadin'),
             ('Part 1', b.part1_text or '', 'part1_text'),
             ('Part 2', b.part2_text or '', 'part2_text'),
             ('Part 3', b.part3_text or '', 'part3_text')]

    for label, raw, field in parts:
        if raw.strip():
            issues += _mechanical_issues(label, raw, field)
            issues += _prose_issues(label, raw, field)

    plain_leadin = _plain(leadin)
    if re.search(r'for ten points each', plain_leadin, re.IGNORECASE):
        issues.append(_issue(WARNING, 'Leadin: use numerals: "For 10 points each"',
                             'numerals', 'leadin',
                             {'field': 'leadin', 'op': 'regex',
                              'pattern': r'(?i)for ten points each', 'repl': 'For 10 points each'}))
    if not re.search(r'for \d+ points each', plain_leadin, re.IGNORECASE):
        issues.append(_issue(INFO, 'Leadin: no "For 10 points each"', 'fpe', 'leadin'))

    for label, raw, field in parts:
        if '(*)' in raw:
            issues.append(_issue(WARNING, '{0}: power mark (*) not allowed in bonus'.format(label),
                                 'power', label))

    for label, ans in (('Answer 1', b.part1_answer), ('Answer 2', b.part2_answer), ('Answer 3', b.part3_answer)):
        if (ans or '').strip() and not _has_underline(ans):
            issues.append(_issue(WARNING, '{0}: not underlined'.format(label),
                                 'underline', label))

    if 'answer_alts' in enabled:
        for label, ans in (('Answer 1', b.part1_answer), ('Answer 2', b.part2_answer), ('Answer 3', b.part3_answer)):
            if (ans or '').strip():
                issues += _answer_alt_issues(label, ans)

    if 'pronunciation' in enabled:
        for label, raw, field in parts:
            if raw.strip():
                issues += _pronunciation_issues(label, raw, field)

    return [i for i in issues if i['code'] in enabled]


# --- auto-apply ------------------------------------------------------------

# QEMS inline markup that interleaves with words: underline/italic chars and the
# backslash escape tokens. We ignore these when locating a term so a clued term
# wrapped in markup (e.g. "_Goethe_") still matches and the guide lands after it.
_MARKUP_TOKENS = ('\\S', '\\s', '\\B')


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


def _insert_guide(text, term, pron):
    """Insert ``("RESPELLING")`` after the first occurrence of `term` that isn't
    already followed by a guide. Matching ignores QEMS inline markup so an
    underlined/italicized term still matches, and the guide is placed after any
    closing markup. Returns the text unchanged if no occurrence is found."""
    guide = ' ("{0}")'.format(pron)
    clean, idx_map = _strip_markup_indexed(text)
    pat = re.compile(r'(?<!\w)' + re.escape(term) + r'(?!\w)', re.IGNORECASE)
    for m in pat.finditer(clean):
        pos = idx_map[m.end()]  # raw index after the term (past any closing markup)
        if guide_opener_at(text, pos):
            continue
        return text[:pos] + guide + text[pos:]
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
