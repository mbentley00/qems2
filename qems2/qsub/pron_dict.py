"""Verified-OL pronunciation dictionary: loading + phrase matching.

The data file (qsub/data/pronunciations.json) is generated offline by the
``build_pron_dict`` management command from the Minkowski pronouncing dictionary,
keeping only maintainer-verified ("OL") entries. This module loads it lazily and
finds dictionary terms inside question text so the style checker can suggest a
pronunciation guide where one is missing.
"""

import json
import os
import re

DATA_PATH = os.path.join(os.path.dirname(__file__), 'data', 'pronunciations.json')

# A word token: a run of (unicode) letters, allowing internal apostrophes and
# hyphens (so "d'Ivoire" and "Lévi-Strauss" stay single tokens).
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*")

# A pronunciation-guide opener right after a term: "(", "[", or a quote.
_GUIDE_OPENER_RE = re.compile(r"""\s{0,2}[(\["“‘']""")

# Cache: (by_first, max_phrase_len). Built once on first use.
_MATCHER = None


def normalize_term(text):
    """Lowercase, collapse whitespace, and drop a trailing possessive so
    headwords and question words compare equal (e.g. "Åbo's" -> "åbo")."""
    t = (text or '').strip().lower()
    t = re.sub(r'\s+', ' ', t)
    t = re.sub(r"['’]s$", '', t)
    return t


def _alnum_lower(text):
    return re.sub(r'[^0-9a-zÀ-ɏ]+', '', (text or '').lower())


def _load_raw():
    if not os.path.exists(DATA_PATH):
        return {}
    with open(DATA_PATH, encoding='utf-8') as fh:
        return json.load(fh)


def _matcher():
    """Build (and cache) the phrase matcher: first-token -> list of
    (phrase_token_tuple, term, pron), each list sorted longest-phrase-first so a
    greedy scan prefers the most specific match."""
    global _MATCHER
    if _MATCHER is not None:
        return _MATCHER
    by_first = {}
    max_len = 1
    for key, info in _load_raw().items():
        tokens = tuple(normalize_term(tok) for tok in _WORD_RE.findall(key))
        if not tokens:
            continue
        max_len = max(max_len, len(tokens))
        by_first.setdefault(tokens[0], []).append((tokens, info['term'], info['pron']))
    for lst in by_first.values():
        lst.sort(key=lambda x: len(x[0]), reverse=True)
    _MATCHER = (by_first, max_len)
    return _MATCHER


def reset_cache():
    """Drop the cached matcher (used by tests after regenerating data)."""
    global _MATCHER
    _MATCHER = None


def guide_opener_at(text, pos):
    """True if a pronunciation-guide opener (paren/quote/bracket) begins at
    `pos` in `text` (used to tell whether a term is already guided)."""
    return bool(_GUIDE_OPENER_RE.match(text, pos))


def _already_guided(text, end_char, pron):
    """True if the term ending at `end_char` already has a pronunciation guide:
    a paren/quote/bracket opener immediately follows it, or the verified
    respelling already appears somewhere in the text."""
    if _GUIDE_OPENER_RE.match(text, end_char):
        return True
    np = _alnum_lower(pron)
    return bool(np) and np in _alnum_lower(text)


def suggest_guides(text):
    """Return [(term, pron), ...] for verified dictionary terms found in `text`
    that do not already carry a pronunciation guide. Each term is suggested at
    most once. `text` should be readable plain text (markup/HTML stripped)."""
    by_first, max_len = _matcher()
    if not by_first:
        return []

    tokens = [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(text)]
    norm = [normalize_term(tok[0]) for tok in tokens]
    n = len(tokens)

    suggestions = []
    seen = set()
    i = 0
    while i < n:
        candidates = by_first.get(norm[i])
        match = None
        if candidates:
            for phrase, term, pron in candidates:
                L = len(phrase)
                if i + L <= n and tuple(norm[i:i + L]) == phrase:
                    match = (term, pron, L, tokens[i + L - 1][2])
                    break
        if match:
            term, pron, L, end_char = match
            low = term.lower()
            if low not in seen and not _already_guided(text, end_char, pron):
                suggestions.append((term, pron))
                seen.add(low)
            i += L
        else:
            i += 1
    return suggestions
