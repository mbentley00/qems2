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

from . import reference_overrides as overrides

DATA_PATH = os.path.join(os.path.dirname(__file__), 'data', 'pronunciations.json')

# A word token: a run of (unicode) letters, allowing internal apostrophes and
# hyphens (so "d'Ivoire" and "Lévi-Strauss" stay single tokens).
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*")

# A pronunciation-guide opener right after a term: "(", "[", or a double quote.
# NB: single quotes are deliberately excluded — an apostrophe in a possessive
# ("Goethe's") would otherwise be read as an opener, blocking the auto-fix.
_GUIDE_OPENER_RE = re.compile(r"""\s{0,2}[(\["“]""")
# The same, for a set whose guides must carry quotation marks: a bare "(" opens
# an ordinary aside there, so only a paren that goes straight into a quote
# counts. Without this, "Diderot (the encyclopedist)" would read as already
# guided and no guide would ever be suggested for it.
_QUOTED_GUIDE_OPENER_RE = re.compile(r"""\s{0,2}(?:[\["“]|\(\s*["“])""")

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


def _load_entries():
    """The dictionary the matcher and the admin screens work from: the bundled
    file with any admin overrides applied ({key: {'term', 'pron', 'source'}}).
    `source` is 'bundled', 'edited' (an override replacing a bundled entry) or
    'added'. Suppressed entries are dropped."""
    entries = {k: dict(v, source='bundled') for k, v in _load_raw().items()}
    for key, term, value, suppressed in overrides.rows(overrides.PRONUNCIATION):
        if suppressed:
            entries.pop(key, None)
        else:
            entries[key] = {'term': term, 'pron': value,
                            'source': 'edited' if key in entries else 'added'}
    return entries


def _matcher():
    """Build (and cache) the phrase matcher: first-token -> list of
    (phrase_token_tuple, term, pron), each list sorted longest-phrase-first so a
    greedy scan prefers the most specific match."""
    global _MATCHER
    if _MATCHER is not None and _MATCHER[2] == overrides.stamp(overrides.PRONUNCIATION):
        return _MATCHER
    by_first = {}
    max_len = 1
    for key, info in _load_entries().items():
        tokens = tuple(normalize_term(tok) for tok in _WORD_RE.findall(key))
        if not tokens:
            continue
        max_len = max(max_len, len(tokens))
        by_first.setdefault(tokens[0], []).append((tokens, info['term'], info['pron']))
    for lst in by_first.values():
        lst.sort(key=lambda x: len(x[0]), reverse=True)
    _MATCHER = (by_first, max_len, overrides.stamp(overrides.PRONUNCIATION))
    return _MATCHER


def reset_cache():
    """Drop the cached matcher (used by tests after regenerating data, and by
    the admin screens right after an override is saved)."""
    global _MATCHER
    _MATCHER = None
    overrides.reset_stamp()


def search(query, limit=50):
    """Dictionary entries whose headword contains `query`, for the admin
    reference-data screen. Returns dicts with key/term/pron/source, headword
    order, plus the total number of matches (which may exceed `limit`)."""
    q = normalize_term(query)
    entries = _load_entries()
    matched = [dict(info, key=key) for key, info in entries.items() if not q or q in key]
    matched.sort(key=lambda e: e['key'])
    return matched[:limit], len(matched)


def get_entry(key):
    """One dictionary entry by key, or None."""
    info = _load_entries().get(key)
    return dict(info, key=key) if info else None


def guide_opener_at(text, pos):
    """True if a pronunciation-guide opener (paren/quote/bracket) begins at
    `pos` in `text` (used to tell whether a term is already guided)."""
    return bool(_GUIDE_OPENER_RE.match(text, pos))


def _already_guided(text, end_char, pron, require_quotes=False):
    """True if the term ending at `end_char` already has a pronunciation guide:
    a paren/quote/bracket opener immediately follows it, or the verified
    respelling already appears somewhere in the text."""
    opener = _QUOTED_GUIDE_OPENER_RE if require_quotes else _GUIDE_OPENER_RE
    if opener.match(text, end_char):
        return True
    np = _alnum_lower(pron)
    return bool(np) and np in _alnum_lower(text)


def _iter_guide_matches(text, require_quotes=False):
    """Yield (term, pron, start, end) for verified dictionary terms found in
    `text` that do not already carry a pronunciation guide. Each term is yielded
    at most once. start/end are character offsets into `text`. `text` should be
    readable plain text (markup/HTML stripped).

    With `require_quotes`, only a quoted respelling counts as an existing guide
    — matching a set that reads a bare parenthetical as ordinary text."""
    by_first, max_len, _stamp = _matcher()
    if not by_first:
        return

    tokens = [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(text)]
    norm = [normalize_term(tok[0]) for tok in tokens]
    n = len(tokens)

    seen = set()
    i = 0
    while i < n:
        candidates = by_first.get(norm[i])
        match = None
        if candidates:
            for phrase, term, pron in candidates:
                L = len(phrase)
                if i + L <= n and tuple(norm[i:i + L]) == phrase:
                    match = (term, pron, L, tokens[i][1], tokens[i + L - 1][2])
                    break
        if match:
            term, pron, L, start_char, end_char = match
            low = term.lower()
            if low not in seen and not _already_guided(text, end_char, pron, require_quotes):
                yield (term, pron, start_char, end_char)
                seen.add(low)
            i += L
        else:
            i += 1


def suggest_guides(text):
    """Return [(term, pron), ...] for verified dictionary terms found in `text`
    that do not already carry a pronunciation guide."""
    return [(term, pron) for term, pron, _s, _e in _iter_guide_matches(text)]


def suggest_guide_matches(text, require_quotes=False):
    """Like suggest_guides but each item is (term, pron, start, end) with the
    character offsets of the match, so callers can build a context preview."""
    return list(_iter_guide_matches(text, require_quotes))


def context_snippet(text, start, end, words=7):
    """A readable excerpt of `text` around the [start, end) span: up to `words`
    whitespace-delimited words of context on each side, preserving the original
    spacing. Returns (prefix, before, match, after, suffix) where prefix/suffix
    are an ellipsis ('… ' / ' …') when the text was truncated on that side and
    `match` is the term itself, so a caller can emphasize it."""
    left = text[:start]
    match = text[start:end]
    right = text[end:]
    # Each chunk is a word plus its trailing (left) / leading (right) spacing, so
    # joining the kept chunks reproduces the source text verbatim.
    left_parts = re.findall(r'\S+\s*', left)
    right_parts = re.findall(r'\s*\S+', right)
    before = ''.join(left_parts[-words:])
    after = ''.join(right_parts[:words])
    prefix = '… ' if len(left_parts) > words else ''
    suffix = ' …' if len(right_parts) > words else ''
    return prefix, before, match, after, suffix
