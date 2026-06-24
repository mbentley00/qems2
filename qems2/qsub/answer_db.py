"""Standard answer-line lookup.

A bundled, slimmed database of high-quality answer lines mined from PACE NSC and
ACF Nationals packets (key / primary answer / ``best_core`` only). The style
checker uses it to flag acceptable alternates a question's answer line is
missing — e.g. an answer of ``France`` whose line omits ``French Republic`` /
``République française``.

The data file (``qsub/data/answer_alternates.jsonl``) is built offline from the
local ``answer_database`` project. Loaded lazily and cached.
"""

import html as _html
import json
import os
import re
import unicodedata

DATA_PATH = os.path.join(os.path.dirname(__file__), 'data', 'answer_alternates.jsonl')

# Cache: {norm_key: {'answer': str, 'alts': [(display, norm_key), ...]}}. Built once.
_DB = None

# Separators between names inside an answer line / bracket.
_SPLIT_RE = re.compile(r'\s*(?:;|,|\bor\b)\s*', re.IGNORECASE)
# Leading answer-line directive on a name part (kept material is the name itself).
_DIRECTIVE_RE = re.compile(
    r'^(?:accept|prompt on|prompt|reject|do not accept|anti-?prompt on|antiprompt|'
    r'equivalents?|equiv\.?)\s+', re.IGNORECASE)


def norm_key(s):
    """Normalize an answer name to a lookup key: lowercase, strip diacritics, drop
    a leading article, keep only ``[a-z0-9 ]``. Mirrors the database's own keys."""
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r'^(the|a|an)\s+', '', s)
    s = re.sub(r'[^a-z0-9 ]', '', s)
    return re.sub(r'\s+', ' ', s).strip()


def _plain(text):
    """Strip HTML and QEMS markup so names compare as written."""
    t = _html.unescape(text or '')
    t = re.sub(r'<[^>]+>', '', t)
    return t.replace('_', '').replace('~', '')


def _clean_name(s):
    """Trim a single name part: drop a leading directive, surrounding quotes, and
    trailing punctuation."""
    s = (s or '').strip()
    s = _DIRECTIVE_RE.sub('', s).strip()
    s = re.sub(r'^["“”\'`(]+|["“”\'`).,;:]+$', '', s).strip()
    return s


def _names(line):
    """All distinct answer names in ``line`` (head plus bracketed alternates) as
    ``(display, norm_key)`` pairs, in order, deduped by key."""
    line = (line or '').strip()
    m = re.match(r'([^\[\]]*)\[(.*)\]', line, re.DOTALL)
    if m:
        head, bracket = m.group(1), m.group(2)
    else:
        head, bracket = line, ''
    parts = [head] + (_SPLIT_RE.split(bracket) if bracket else [])
    out, seen = [], set()
    for p in parts:
        disp = _clean_name(p)
        if not disp:
            continue
        nk = norm_key(disp)
        if not nk or nk in seen:
            continue
        seen.add(nk)
        out.append((disp, nk))
    return out


def _load():
    global _DB
    if _DB is not None:
        return _DB
    db = {}
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                names = _names(_plain(r.get('best_core', '')))
                if len(names) < 2:  # need a head plus at least one alternate
                    continue
                key = r.get('key') or names[0][1]
                db[key] = {'answer': r.get('answer', names[0][0]), 'alts': names[1:]}
    _DB = db
    return _DB


def reset_cache():
    """Drop the cached database (used by tests)."""
    global _DB
    _DB = None


def missing_alternates(raw_answer):
    """Given a question's raw answer line, return ``(head_key, [missing_display,
    ...])``: standard acceptable alternates whose head matches a database entry
    but which the line does not already mention. ``head_key`` is '' (and the list
    empty) when there is no database match or nothing is missing.

    The head is matched on the underlined required portion first (the canonical
    answer), then on the full text before the first bracket."""
    db = _load()
    raw = raw_answer or ''
    head_region = raw.split('[', 1)[0]

    candidates = []
    underlined = ' '.join(re.findall(r'_([^_]+)_', head_region))
    if underlined.strip():
        candidates.append(norm_key(_plain(underlined)))
    candidates.append(norm_key(_plain(head_region)))
    candidates = [c for c in candidates if c]

    rec, head_key = None, ''
    for c in candidates:
        if c in db:
            rec, head_key = db[c], c
            break
    if not rec:
        return '', []

    present = set(candidates)
    for _disp, nk in _names(_plain(raw)):
        present.add(nk)

    missing = [disp for disp, nk in rec['alts'] if nk not in present]
    return head_key, missing
