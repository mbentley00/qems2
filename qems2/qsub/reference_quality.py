"""Confidence signals for the bundled reference data.

Both datasets are applied to questions automatically — the style checker offers
a pronunciation guide for any dictionary term it finds, and an answer line's
standard alternates for any answer it knows — so a bad entry writes a wrong
guide or a wrong alternate into somebody's question rather than merely failing
to help. `Koch` is the example that started this: Robert Koch is KOKH, Ed Koch
KOTCH, the Koch brothers COKE, and the dictionary holds one guide for all three.

Nothing here checks an entry against an external authority. A "high" tier means
no evidence *against* the entry was found, not that it was verified correct.

The signals are graded by how much they can be trusted:

  objective  the entry is demonstrably not carrying information (its guide is
             an ordinary English word, spells the term out letter by letter, or
             just repeats the spelling)
  strong     two entries disagree with each other
  mixed      the dictionary itself sounds the term differently inside a longer
             name — real often enough to matter, cosmetic often enough that it
             must not be automated
  heuristic  a shape that tends to go wrong (a bare surname the dictionary also
             knows a full name for, a one-chunk guide on a capitalised name)

An entry with an objective or strong signal lands in `low`, one with only mixed
or heuristic signals in `review`, and everything else in `high`.
"""

import io
import os
import re

from . import pron_dict, answer_db

_COMMON_WORDS_PATH = os.path.join(os.path.dirname(__file__), 'data', 'common_words.txt')

TIER_LOW = 'low'
TIER_REVIEW = 'review'
TIER_HIGH = 'high'
TIER_LABELS = ((TIER_LOW, 'Least confidence'),
               (TIER_REVIEW, 'Worth reviewing'),
               (TIER_HIGH, 'Nothing against it'))

GRADE_OF = {'objective': TIER_LOW, 'strong': TIER_LOW,
            'mixed': TIER_REVIEW, 'heuristic': TIER_REVIEW}

# (key, grade, label) — label is what the review screen shows.
PRON_SIGNALS = (
    ('guide_is_common_word', 'objective',
     'The guide is an ordinary English word, so it reads as that word'),
    ('guide_spells_out', 'objective', 'The guide spells the term out letter by letter'),
    ('guide_repeats_spelling', 'objective', 'The guide just repeats the spelling'),
    ('conflicting_guides', 'strong', 'Another entry spells this term the same and sounds it differently'),
    ('differs_inside_full_name', 'mixed', 'The dictionary sounds this differently inside a full name'),
    ('surname_of_full_name', 'heuristic', 'A bare surname the dictionary also knows a full name for'),
    ('one_chunk_guide', 'heuristic', 'A one-chunk guide on a capitalised name'),
    ('very_short_term', 'heuristic', 'A very short term'),
)

ANSWER_SIGNALS = (
    ('alt_repeats_head', 'objective', 'An alternate is the head answer again'),
    ('duplicate_alts', 'objective', 'Two alternates are the same answer'),
    # The rest are shapes that often go wrong rather than proof of anything:
    # "eight" for 8 is an ordinary word and perfectly correct.
    ('alt_is_common_word', 'heuristic', 'An alternate is an ordinary English word'),
    ('alt_very_short', 'heuristic', 'An alternate is one or two characters'),
    ('alt_is_another_head', 'heuristic', 'An alternate is a different answer in its own right'),
    ('many_alts', 'heuristic', 'A long list of alternates'),
)

PRON_SIGNAL_LABELS = {k: (g, l) for k, g, l in PRON_SIGNALS}
ANSWER_SIGNAL_LABELS = {k: (g, l) for k, g, l in ANSWER_SIGNALS}

_COMMON_WORDS = None
_PRON_CACHE = None
_ANSWER_CACHE = None


def _common_words():
    global _COMMON_WORDS
    if _COMMON_WORDS is None:
        words = set()
        if os.path.exists(_COMMON_WORDS_PATH):
            with io.open(_COMMON_WORDS_PATH, encoding='utf-8') as fh:
                words = {w.strip().lower() for w in fh if w.strip()}
        _COMMON_WORDS = words
    return _COMMON_WORDS


def _spoken(guide):
    """A guide reduced to the letters it would be read as: 'AH-khen' -> 'ahkhen'."""
    return re.sub(r'[^a-z]', '', (guide or '').lower())


def _is_letter_spelling(guide):
    """'A-A-V-E' / 'N. A. A. C. P.' — a guide that spells the term out."""
    parts = [p for p in re.split(r'[\s.\-]+', (guide or '').strip()) if p]
    return len(parts) >= 2 and all(len(p) == 1 and p.isalpha() for p in parts)


#: signal key -> grade, for both datasets (the keys don't collide).
SIGNAL_GRADE = {k: g for k, g, _ in PRON_SIGNALS}
SIGNAL_GRADE.update({k: g for k, g, _ in ANSWER_SIGNALS})


def _tier(signals):
    grades = {GRADE_OF.get(SIGNAL_GRADE.get(s)) for s in signals}
    if TIER_LOW in grades:
        return TIER_LOW
    if TIER_REVIEW in grades:
        return TIER_REVIEW
    return TIER_HIGH


def _stamp():
    from . import reference_overrides as overrides
    return (overrides.stamp(overrides.PRONUNCIATION), overrides.stamp(overrides.ANSWER_LINE))


def reset_cache():
    global _PRON_CACHE, _ANSWER_CACHE
    _PRON_CACHE = None
    _ANSWER_CACHE = None


def pronunciation_report():
    """[{key, term, pron, source, signals, tier}] for every dictionary entry."""
    global _PRON_CACHE
    if _PRON_CACHE is not None and _PRON_CACHE[0] == _stamp():
        return _PRON_CACHE[1]

    entries = pron_dict._load_entries()
    common = _common_words()

    # Cross-entry indexes: how a term sounds elsewhere, and which single-word
    # terms turn up as the last word of a full name.
    spoken_by_alnum = {}
    inside_full_name = {}
    last_word_of = {}
    for key, info in entries.items():
        alnum = re.sub(r'[^a-z0-9]', '', key)
        spoken_by_alnum.setdefault(alnum, set()).add(_spoken(info['pron']))
        words = key.split()
        if len(words) > 1:
            guide_chunks = re.split(r'\s+', (info['pron'] or '').strip())
            last = pron_dict.normalize_term(words[-1])
            last_word_of.setdefault(last, []).append(key)
            if len(guide_chunks) == len(words):
                inside_full_name.setdefault(last, set()).add(_spoken(guide_chunks[-1]))

    rows = []
    for key, info in entries.items():
        term = info.get('term') or key
        pron = info.get('pron') or ''
        signals = []
        spoken = _spoken(pron)

        if spoken and spoken in common and len(spoken) > 2:
            signals.append('guide_is_common_word')
        if _is_letter_spelling(pron):
            signals.append('guide_spells_out')
        # Same letters as the term, and no syllable break to say where the
        # stress falls: "AIL-er-ons" earns its keep, "AILERONS" doesn't.
        if (spoken and spoken == re.sub(r'[^a-z]', '', key.lower())
                and not re.search(r'[\s\-]', pron.strip())):
            signals.append('guide_repeats_spelling')

        alnum = re.sub(r'[^a-z0-9]', '', key)
        if len(spoken_by_alnum.get(alnum, ())) > 1:
            signals.append('conflicting_guides')

        if ' ' not in key:
            elsewhere = inside_full_name.get(key, set())
            if elsewhere and spoken not in elsewhere:
                signals.append('differs_inside_full_name')
            if key in last_word_of:
                signals.append('surname_of_full_name')
            if (term[:1].isupper() and not re.search(r'[\s\-]', pron.strip())
                    and len(spoken) > 3):
                signals.append('one_chunk_guide')
            if len(key) <= 3:
                signals.append('very_short_term')

        rows.append({'key': key, 'term': term, 'value': pron,
                     'source': info.get('source', 'bundled'),
                     'signals': signals, 'tier': _tier(signals)})

    rows.sort(key=lambda r: r['key'])
    _PRON_CACHE = (_stamp(), rows)
    return rows


def answer_report():
    """The same for the answer-line database, keyed on the head answer."""
    global _ANSWER_CACHE
    if _ANSWER_CACHE is not None and _ANSWER_CACHE[0] == _stamp():
        return _ANSWER_CACHE[1]

    db = answer_db._load()
    common = _common_words()
    heads = set(db.keys())

    rows = []
    for key, rec in db.items():
        alts = rec.get('alts') or []
        signals = []
        seen = set()
        for display, norm in alts:
            if norm == key:
                signals.append('alt_repeats_head')
            if norm in seen:
                signals.append('duplicate_alts')
            seen.add(norm)
            word = (display or '').strip().lower()
            if (word and word in common and len(word) > 2
                    and not re.search(r'[\d\s]', word)):
                signals.append('alt_is_common_word')
            if len(re.sub(r'\s', '', display or '')) <= 2:
                signals.append('alt_very_short')
            if norm != key and norm in heads:
                signals.append('alt_is_another_head')
        if len(alts) > 8:
            signals.append('many_alts')

        signals = sorted(set(signals))
        rows.append({'key': key, 'term': rec.get('answer') or key,
                     'value': rec.get('line') or '',
                     'source': rec.get('source', 'bundled'),
                     'alt_count': len(alts),
                     'signals': signals, 'tier': _tier(signals)})

    rows.sort(key=lambda r: r['key'])
    _ANSWER_CACHE = (_stamp(), rows)
    return rows


def report(dataset):
    """The report for a dataset key ('pron' / 'answer')."""
    from .models import ReferenceDataOverride
    if dataset == ReferenceDataOverride.ANSWER_LINE:
        return answer_report(), ANSWER_SIGNAL_LABELS
    return pronunciation_report(), PRON_SIGNAL_LABELS


def summarize(rows):
    """Counts per tier and per signal, for the filter bar."""
    tiers = {TIER_LOW: 0, TIER_REVIEW: 0, TIER_HIGH: 0}
    signals = {}
    for row in rows:
        tiers[row['tier']] = tiers.get(row['tier'], 0) + 1
        for s in row['signals']:
            signals[s] = signals.get(s, 0) + 1
    return tiers, signals
