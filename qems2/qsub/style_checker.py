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

_GUIDE_RULES = {
    'minkowski': {'mechanical', 'numerals', 'fps', 'fpe', 'power', 'imperative',
                  'underline', 'answer_leak', 'pronunciation'},
    'generic': {'mechanical', 'underline'},
}


def guide_keys():
    return {g['key'] for g in STYLE_GUIDES}


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
        issues.append(_issue(INFO, '{0}: use an ellipsis (…) instead of three periods'.format(label),
                             'ellipsis', label,
                             {'field': field, 'op': 'regex', 'pattern': r'\.{3,}', 'repl': '…'}))
    if '--' in text:
        issues.append(_issue(INFO, '{0}: use an em dash (—) instead of double hyphens'.format(label),
                             'double_hyphen', label,
                             {'field': field, 'op': 'regex', 'pattern': r'-{2,}', 'repl': '—'}))
    if no_power.count('(') != no_power.count(')'):
        issues.append(_issue(WARNING, '{0}: unbalanced parentheses (check pronunciation guides)'.format(label),
                             'unbalanced_parens', label))
    return issues


def _pronunciation_issues(label, raw, field):
    """Suggest a verified-OL pronunciation guide for any dictionary term in the
    text that doesn't already have one (INFO, auto-applicable)."""
    issues = []
    for term, pron in suggest_guides(_plain(raw)):
        issues.append(_issue(
            INFO, '{0}: consider a pronunciation guide for "{1}" — ("{2}")'.format(label, term, pron),
            'pronunciation', '{0}|{1}'.format(label, term),
            {'field': field, 'op': 'guide', 'term': term, 'pron': pron}))
    return issues


def _has_underline(raw):
    return '_' in (raw or '')


def check_tossup(tu, guide=DEFAULT_GUIDE):
    rules = _GUIDE_RULES.get(guide, _GUIDE_RULES[DEFAULT_GUIDE])
    issues = []
    text = tu.tossup_text or ''
    plain = _plain(text)

    if 'mechanical' in rules:
        issues += _mechanical_issues('Question', text, 'tossup_text')

    if 'answer_leak' in rules and re.search(r'\banswers?\s*:', plain, re.IGNORECASE):
        issues.append(_issue(WARNING, 'Question text contains "ANSWER:"', 'answer_leak'))

    if 'numerals' in rules and re.search(r'for ten points', plain, re.IGNORECASE):
        issues.append(_issue(WARNING, 'Use numerals: "For 10 points", not "for ten points"',
                             'numerals', 'tossup_text',
                             {'field': 'tossup_text', 'op': 'regex',
                              'pattern': r'(?i)for ten points', 'repl': 'For 10 points'}))

    if 'fps' in rules and not re.search(r'for \d+ points', plain, re.IGNORECASE):
        issues.append(_issue(INFO, 'No "For 10 points" giveaway phrase found', 'fps'))

    if 'power' in rules:
        if text.count('(*)') > 1:
            issues.append(_issue(WARNING, 'More than one power mark (*)', 'power'))

    if 'imperative' in rules and plain.rstrip().endswith('?'):
        issues.append(_issue(WARNING, 'Giveaway is interrogative; prefer an imperative ("name this…")',
                             'imperative'))

    if 'underline' in rules and not _has_underline(tu.tossup_answer):
        issues.append(_issue(WARNING, 'Answer line has no underlined required portion', 'underline'))

    if 'pronunciation' in rules:
        issues += _pronunciation_issues('Question', text, 'tossup_text')

    return issues


def check_bonus(b, guide=DEFAULT_GUIDE):
    rules = _GUIDE_RULES.get(guide, _GUIDE_RULES[DEFAULT_GUIDE])
    issues = []
    leadin = b.leadin or ''
    parts = [('Leadin', leadin, 'leadin'),
             ('Part 1', b.part1_text or '', 'part1_text'),
             ('Part 2', b.part2_text or '', 'part2_text'),
             ('Part 3', b.part3_text or '', 'part3_text')]

    if 'mechanical' in rules:
        for label, raw, field in parts:
            if raw.strip():
                issues += _mechanical_issues(label, raw, field)

    plain_leadin = _plain(leadin)
    if 'numerals' in rules and re.search(r'for ten points each', plain_leadin, re.IGNORECASE):
        issues.append(_issue(WARNING, 'Leadin: use numerals: "For 10 points each"',
                             'numerals', 'leadin',
                             {'field': 'leadin', 'op': 'regex',
                              'pattern': r'(?i)for ten points each', 'repl': 'For 10 points each'}))
    if 'fpe' in rules and not re.search(r'for \d+ points each', plain_leadin, re.IGNORECASE):
        issues.append(_issue(INFO, 'Leadin has no "For 10 points each" phrase', 'fpe', 'leadin'))

    if 'power' in rules:
        for label, raw, field in parts:
            if '(*)' in raw:
                issues.append(_issue(WARNING, '{0}: bonuses should not have a power mark (*)'.format(label),
                                     'power', label))

    if 'underline' in rules:
        for label, ans in (('Answer 1', b.part1_answer), ('Answer 2', b.part2_answer), ('Answer 3', b.part3_answer)):
            if (ans or '').strip() and not _has_underline(ans):
                issues.append(_issue(WARNING, '{0}: no underlined required portion'.format(label),
                                     'underline', label))

    if 'pronunciation' in rules:
        for label, raw, field in parts:
            if raw.strip():
                issues += _pronunciation_issues(label, raw, field)

    return issues


# --- auto-apply ------------------------------------------------------------

def _insert_guide(text, term, pron):
    """Insert ``("RESPELLING")`` after the first occurrence of `term` that isn't
    already followed by a guide. Returns the text unchanged if none is found."""
    guide = ' ("{0}")'.format(pron)
    pat = re.compile(r'(?<!\w)' + re.escape(term) + r'(?!\w)', re.IGNORECASE)
    for m in pat.finditer(text):
        if guide_opener_at(text, m.end()):
            continue
        return text[:m.end()] + guide + text[m.end():]
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
