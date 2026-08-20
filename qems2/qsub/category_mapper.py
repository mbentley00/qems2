"""Mapping a category name from somewhere else onto a set's own distribution.

An imported packet names its categories the way its own set did: "RMP - World
Mythology", "History - American - 1865-1945", "Fine Arts - Music - Recent". The
set being imported into has its own, coarser list — "RMP - Mythology", "History
- American", "Fine Arts - Audio" — and those are the categories its editors work
to. Inventing the incoming names as new categories, which is what used to
happen, leaves a distribution nobody asked for and questions filed outside it.

So: clean the name up, then find the nearest thing the set already has, and if
there is no near thing, leave the question uncategorized for a human. Never
create.

The matching runs in order of how much it assumes:

  1. the same category and subcategory (case-insensitive)
  2. the same category, with trailing detail dropped from the subcategory
     ("American - 1865-1945" -> "American")
  3. the same category, subcategory matched through the alias table below
     (Music and Opera are Audio; Painting and Architecture are Visual)
  4. the same category, subcategory matched on shared words
     ("World Mythology" -> "Mythology")
  5. the incoming *category* read as a subcategory somewhere else
     ("Geography - World" -> "Other - Geography")
  6. the same category with whatever general subcategory it has ("Any", "Other",
     or a lone entry)

Nothing here is clever about meaning; it is string work with a table of the
equivalences quizbowl actually uses.
"""

import html
import re

#: Subcategory words that mean the same thing across distributions. Each key is
#: a word that may appear in an incoming subcategory; the values are words a
#: target subcategory may contain. First match wins, so order matters within a
#: list only in that a target containing several of them is still one match.
ALIASES = {
    'music': ('audio', 'music'),
    'opera': ('audio', 'music', 'opera'),
    'jazz': ('audio', 'music'),
    'classical': ('audio', 'music'),
    'auditory': ('audio',),
    'audio': ('audio', 'music'),
    'painting': ('visual', 'painting', 'art'),
    'sculpture': ('visual', 'sculpture', 'art'),
    'architecture': ('visual', 'architecture', 'art'),
    'photography': ('visual', 'art'),
    'film': ('visual', 'film'),
    'dance': ('visual', 'dance'),
    'visual': ('visual', 'painting', 'art'),
    'myth': ('mythology',),
    'mythology': ('mythology',),
    'philosophy': ('philosophy', 'thought'),
    'religion': ('religion',),
    'belief': ('religion',),
    'geography': ('geography',),
    'current': ('current events', 'current'),
    'events': ('current events', 'events'),
    'trash': ('pop culture', 'popular culture', 'trash'),
    'pop': ('pop culture', 'popular culture', 'trash'),
    'miscellaneous': ('other', 'misc', 'miscellaneous'),
    'misc': ('other', 'misc', 'miscellaneous'),
    'general': ('other', 'general', 'any'),
    'social': ('social science',),
    'economics': ('social science', 'economics'),
    'psychology': ('social science', 'psychology'),
    'sociology': ('social science', 'sociology'),
    'anthropology': ('social science', 'anthropology'),
    'linguistics': ('social science', 'linguistics'),
}

#: Subcategories that mean "anything in this category".
GENERAL_SUBS = ('any', 'other', 'general', 'miscellaneous', 'misc', '')

_WORD_RE = re.compile(r"[a-z0-9']+")
# Where a category name stops being a category name. YAPP hands over the whole
# post-question metadata line, and packets often carry more than one bracketed
# group on it — "<Author, Category> ~31899~ <Editor: Name>" — so everything from
# the first closing bracket, tilde or opening bracket on is somebody else's data.
_TAIL_RE = re.compile(r'[>~<\]]')


def clean_category_text(raw):
    """A category path with the packet's own bookkeeping stripped off.

    ``"RMP - World Mythology&gt; ~25806~ &lt;Editor: Sinecio Morales"`` is the
    category ``RMP - World Mythology``; the rest is the question id and the
    editor, which YAPP left on the end of the same field.
    """
    text = html.unescape(raw or '').strip()
    text = _TAIL_RE.split(text, 1)[0]
    # A stray "Editor:" with no bracket in front of it means the same thing.
    text = re.split(r'(?i)\beditors?\s*:', text, 1)[0]
    text = text.strip().strip('-').strip()
    return re.sub(r'\s+', ' ', text)


def split_path(text):
    """``"Fine Arts - Music - Recent"`` -> ``("Fine Arts", "Music - Recent")``."""
    text = clean_category_text(text)
    if ' - ' in text:
        cat, sub = text.split(' - ', 1)
        return cat.strip(), sub.strip()
    return text, ''


def _words(text):
    return set(_WORD_RE.findall((text or '').lower()))


def _norm(text):
    return re.sub(r'\s+', ' ', (text or '').strip().lower())


def _alias_targets(sub):
    """Every target word the incoming subcategory's words stand in for."""
    out = set()
    for word in _words(sub):
        for target in ALIASES.get(word, ()):
            out.add(target)
    return out


def _sub_score(sub, entry):
    """How well an entry's subcategory answers to `sub`, or 0 for not at all.

    Scored rather than taken first-match: "Architecture" reaches both "Visual"
    and "Other Visual" through the alias table, and the plain one is the better
    answer.
    """
    entry_text = _norm(entry.subcategory)
    entry_words = _words(entry.subcategory)
    sub_words = _words(sub)
    if not entry_text:
        return 0

    score = 0
    if entry_text == _norm(sub):
        score = 100
    else:
        targets = _alias_targets(sub)
        if targets:
            if entry_text in targets:
                score = 70                       # "Visual" for Architecture
            elif entry_words & targets:
                score = 55                       # "Other Visual" for the same
        shared = len(sub_words & entry_words)
        if shared:
            score = max(score, 30 + 10 * shared)
    if not score:
        return 0
    # Prefer the plainer name of two that both answer: "Visual" over "Other
    # Visual", "Mythology" over "Other Mythology".
    score -= 2 * max(0, len(entry_words) - len(sub_words & entry_words))
    if 'other' in entry_words and 'other' not in sub_words:
        score -= 3
    return score


def _best_scored(sub, candidates):
    best, best_score = None, 0
    for e in candidates:
        score = _sub_score(sub, e)
        if score > best_score:
            best, best_score = e, score
    return best


def _general_bucket(entries):
    """A set's catch-all category, if it has one: "Other - Other", "Other -
    Any", "Miscellaneous - ...". Where a whole top-level category has no
    counterpart — an imported Trash question arriving in a set with no Trash —
    this is the only honest home short of leaving it uncategorized."""
    for e in entries:
        if (_norm(e.category) in ('other', 'miscellaneous', 'misc')
                and _norm(e.subcategory) in GENERAL_SUBS):
            return e
    for e in entries:
        if _norm(e.category) in ('other', 'miscellaneous', 'misc'):
            return e
    return None


def best_entry(cat, sub, entries):
    """The distribution entry nearest to ``(cat, sub)``, or None.

    ``entries`` is any iterable of objects with ``.category``/``.subcategory``
    (DistributionEntry rows). Returns the entry and never creates one.
    """
    entries = list(entries)
    if not entries:
        return None
    cat, sub = _norm(cat), _norm(sub)
    if not cat and not sub:
        return None

    same_cat = [e for e in entries if _norm(e.category) == cat]

    # 1. exact
    for e in same_cat:
        if _norm(e.subcategory) == sub:
            return e

    # 2. trailing detail dropped: "American - 1865-1945" -> "American"
    if sub:
        parts = [p.strip() for p in sub.split(' - ') if p.strip()]
        while len(parts) > 1:
            parts.pop()
            head = _norm(' - '.join(parts))
            for e in same_cat:
                if _norm(e.subcategory) == head:
                    return e
            hit = _best_scored(head, same_cat)
            if hit is not None:
                return hit

    # 3. alias or shared words, within the same category
    if same_cat and sub:
        hit = _best_scored(sub, same_cat)
        if hit is not None:
            return hit

    # 4. the incoming category read as somebody's subcategory:
    #    "Geography - World" onto "Other - Geography"
    if cat:
        for e in entries:
            if _norm(e.subcategory) == cat:
                return e
        hit = _best_scored(cat, entries)
        if hit is not None:
            return hit

    # 5. the category's own general bucket, then the set's
    for e in same_cat:
        if _norm(e.subcategory) in GENERAL_SUBS:
            return e
    if len(same_cat) == 1:
        return same_cat[0]
    return _general_bucket(entries)


def map_metadata_category(raw, entries):
    """Nearest entry for a raw metadata category string, or None."""
    cat, sub = split_path(raw)
    if not cat:
        return None
    return best_entry(cat, sub, entries)


def looks_like_debris(entry):
    """Whether a distribution entry's name is import debris rather than a
    category somebody chose — a question id, an editor credit, half a bracketed
    group. These are what a bad import leaves behind."""
    text = html.unescape('{0} {1}'.format(entry.category or '', entry.subcategory or ''))
    if re.search(r'[<>~\]]', text):
        return True
    if re.search(r'(?i)\beditors?\s*:', text):
        return True
    return False
