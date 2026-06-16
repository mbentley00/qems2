"""Style checker for quizbowl questions.

Runs a set of mechanically-checkable style rules over tossups and bonuses. The
rule set is selectable per "style guide"; the default is the guide at
https://minkowski.space/quizbowl/manuals/style/index.html. Each guide turns a
common pool of rules on or off.

Returns issues as dicts: {'severity': 'error'|'warning'|'info', 'message': str}.
"""

import re

from .utils import strip_markup

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
    'minkowski': {'mechanical', 'numerals', 'fps', 'fpe', 'power', 'imperative', 'underline', 'answer_leak'},
    'generic': {'mechanical', 'underline'},
}


def guide_keys():
    return {g['key'] for g in STYLE_GUIDES}


def _plain(text):
    """Readable plain text: strip HTML/smart quotes (strip_markup) plus QEMS
    markup characters, so style checks see the words as read."""
    t = strip_markup(text or '')
    for marker in ('\\S', '\\s', '\\B'):
        t = t.replace(marker, '')
    return t.replace('_', '').replace('~', '')


def _mechanical_issues(label, raw):
    """Spacing/punctuation/typography rules that apply to any text blob."""
    issues = []
    text = _plain(raw)
    no_power = text.replace('(*)', '')
    if '  ' in text:
        issues.append((WARNING, '{0}: double space'.format(label)))
    if re.search(r'\s[,.;:!?]', no_power):
        issues.append((WARNING, '{0}: space before punctuation'.format(label)))
    if re.search(r',[A-Za-z]', text):
        issues.append((WARNING, '{0}: missing space after comma'.format(label)))
    if '...' in text:
        issues.append((INFO, '{0}: use an ellipsis (…) instead of three periods'.format(label)))
    if '--' in text:
        issues.append((INFO, '{0}: use an em dash (—) instead of double hyphens'.format(label)))
    if no_power.count('(') != no_power.count(')'):
        issues.append((WARNING, '{0}: unbalanced parentheses (check pronunciation guides)'.format(label)))
    return issues


def _has_underline(raw):
    return '_' in (raw or '')


def check_tossup(tu, guide=DEFAULT_GUIDE):
    rules = _GUIDE_RULES.get(guide, _GUIDE_RULES[DEFAULT_GUIDE])
    issues = []
    text = tu.tossup_text or ''
    plain = _plain(text)

    if 'mechanical' in rules:
        issues += _mechanical_issues('Question', text)

    if 'answer_leak' in rules and re.search(r'\banswers?\s*:', plain, re.IGNORECASE):
        issues.append((WARNING, 'Question text contains "ANSWER:"'))

    if 'numerals' in rules and re.search(r'for ten points', plain, re.IGNORECASE):
        issues.append((WARNING, 'Use numerals: "For 10 points", not "for ten points"'))

    if 'fps' in rules and not re.search(r'for \d+ points', plain, re.IGNORECASE):
        issues.append((INFO, 'No "For 10 points" giveaway phrase found'))

    if 'power' in rules:
        n = text.count('(*)')
        if n > 1:
            issues.append((WARNING, 'More than one power mark (*)'))

    if 'imperative' in rules and plain.rstrip().endswith('?'):
        issues.append((WARNING, 'Giveaway is interrogative; prefer an imperative ("name this…")'))

    if 'underline' in rules and not _has_underline(tu.tossup_answer):
        issues.append((WARNING, 'Answer line has no underlined required portion'))

    return [{'severity': s, 'message': m} for s, m in issues]


def check_bonus(b, guide=DEFAULT_GUIDE):
    rules = _GUIDE_RULES.get(guide, _GUIDE_RULES[DEFAULT_GUIDE])
    issues = []
    leadin = b.leadin or ''
    parts = [('Leadin', leadin),
             ('Part 1', b.part1_text or ''), ('Part 2', b.part2_text or ''),
             ('Part 3', b.part3_text or '')]

    if 'mechanical' in rules:
        for label, raw in parts:
            if raw.strip():
                issues += _mechanical_issues(label, raw)

    plain_leadin = _plain(leadin)
    if 'numerals' in rules and re.search(r'for ten points each', plain_leadin, re.IGNORECASE):
        issues.append((WARNING, 'Leadin: use numerals: "For 10 points each"'))
    if 'fpe' in rules and not re.search(r'for \d+ points each', plain_leadin, re.IGNORECASE):
        issues.append((INFO, 'Leadin has no "For 10 points each" phrase'))

    if 'power' in rules:
        for label, raw in parts:
            if '(*)' in raw:
                issues.append((WARNING, '{0}: bonuses should not have a power mark (*)'.format(label)))

    if 'underline' in rules:
        for label, ans in (('Answer 1', b.part1_answer), ('Answer 2', b.part2_answer), ('Answer 3', b.part3_answer)):
            if (ans or '').strip() and not _has_underline(ans):
                issues.append((WARNING, '{0}: no underlined required portion'.format(label)))

    return [{'severity': s, 'message': m} for s, m in issues]
