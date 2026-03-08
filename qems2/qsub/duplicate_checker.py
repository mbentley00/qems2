import re
import string
from collections import defaultdict

from .utils import get_answer_no_formatting, get_primary_answer, strip_markup

# Common English stopwords to exclude from clue comparison
STOPWORDS = frozenset({
    'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'is', 'was', 'are', 'were', 'be', 'been',
    'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
    'could', 'should', 'may', 'might', 'shall', 'can', 'not', 'no', 'nor',
    'so', 'if', 'then', 'than', 'that', 'this', 'these', 'those', 'it',
    'its', 'he', 'she', 'they', 'them', 'his', 'her', 'their', 'my', 'your',
    'our', 'we', 'you', 'me', 'him', 'us', 'who', 'whom', 'which', 'what',
    'where', 'when', 'how', 'why', 'all', 'each', 'every', 'both', 'few',
    'more', 'most', 'other', 'some', 'such', 'only', 'own', 'same', 'also',
    'as', 'about', 'up', 'out', 'into', 'over', 'after', 'before', 'between',
    'under', 'again', 'further', 'once', 'here', 'there', 'just', 'very',
    'one', 'two', 'name', 'work', 'title', 'answer', 'question', 'bonus',
    'tossup', 'ten', 'points', 'part',
})

LEADING_ARTICLES = re.compile(r'^(the|a|an)\s+', re.IGNORECASE)
PUNCTUATION_TABLE = str.maketrans('', '', string.punctuation)


def normalize_answer(raw_answer):
    """Normalize an answer for duplicate grouping.

    Pipeline: strip markup/underscores → take primary answer (before '[') →
    lowercase → strip leading articles → strip punctuation → collapse whitespace.
    """
    if not raw_answer:
        return ''
    text = get_answer_no_formatting(raw_answer)
    text = get_primary_answer(text)
    text = text.lower().strip()
    text = LEADING_ARTICLES.sub('', text)
    text = text.translate(PUNCTUATION_TABLE)
    text = ' '.join(text.split())
    return text


def extract_clue_words(text):
    """Extract a set of content words from question text for similarity comparison."""
    if not text:
        return set()
    plain = strip_markup(text).lower()
    plain = plain.translate(PUNCTUATION_TABLE)
    words = plain.split()
    return {w for w in words if len(w) > 3 and w not in STOPWORDS}


def clue_similarity(words_a, words_b):
    """Jaccard similarity between two word sets. Returns float 0.0-1.0."""
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


# Severity constants
CRITICAL = 'critical'
WARNING = 'warning'
INFO = 'info'

SEVERITY_ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}


def _get_category_str(obj):
    """Get a display string for a question's category."""
    if obj.category is not None:
        return str(obj.category)
    return ''


def find_duplicates(qset):
    """Find duplicate answers across all questions in a question set.

    Returns a list of duplicate groups sorted by severity (critical first).
    Each group is a dict with:
        - answer: the normalized answer string
        - severity: 'critical', 'warning', or 'info'
        - entries: list of question entry dicts
        - pairs: list of pairwise comparison dicts
    """
    from .models import Tossup, Bonus

    entries = []

    # Collect tossups
    for tu in Tossup.objects.filter(question_set=qset).select_related('category', 'author'):
        norm = normalize_answer(tu.tossup_answer)
        if not norm:
            continue
        entries.append({
            'type': 'tossup',
            'id': tu.id,
            'answer_raw': tu.tossup_answer,
            'answer_normalized': norm,
            'text': tu.tossup_text,
            'category_str': _get_category_str(tu),
            'author': str(tu.author),
            'part_label': None,
        })

    # Collect bonus parts
    for bonus in Bonus.objects.filter(question_set=qset).select_related('category', 'author'):
        parts = [
            ('Part 1', bonus.part1_answer, bonus.part1_text),
            ('Part 2', bonus.part2_answer, bonus.part2_text),
            ('Part 3', bonus.part3_answer, bonus.part3_text),
        ]
        for part_label, answer, text in parts:
            if not answer:
                continue
            norm = normalize_answer(answer)
            if not norm:
                continue
            entries.append({
                'type': 'bonus',
                'id': bonus.id,
                'answer_raw': answer,
                'answer_normalized': norm,
                'text': text or '',
                'category_str': _get_category_str(bonus),
                'author': str(bonus.author),
                'part_label': part_label,
            })

    # Group by normalized answer
    groups_by_answer = defaultdict(list)
    for entry in entries:
        groups_by_answer[entry['answer_normalized']].append(entry)

    # Build result groups for answers with 2+ entries
    result = []
    for answer, group_entries in groups_by_answer.items():
        if len(group_entries) < 2:
            continue

        # Precompute clue words for each entry
        clue_words = [extract_clue_words(e['text']) for e in group_entries]

        # Compute pairwise comparisons
        pairs = []
        group_severity = INFO
        for i in range(len(group_entries)):
            for j in range(i + 1, len(group_entries)):
                sim = clue_similarity(clue_words[i], clue_words[j])
                same_category = (
                    group_entries[i]['category_str'] != '' and
                    group_entries[i]['category_str'] == group_entries[j]['category_str']
                )

                if sim >= 0.3:
                    pair_severity = CRITICAL
                elif same_category:
                    pair_severity = WARNING
                else:
                    pair_severity = INFO

                # Promote group severity
                if SEVERITY_ORDER[pair_severity] < SEVERITY_ORDER[group_severity]:
                    group_severity = pair_severity

                pairs.append({
                    'entry_a': i,
                    'entry_b': j,
                    'similarity': round(sim, 2),
                    'same_category': same_category,
                    'severity': pair_severity,
                })

        result.append({
            'answer': answer,
            'severity': group_severity,
            'entries': group_entries,
            'pairs': pairs,
        })

    # Sort by severity (critical first), then by answer
    result.sort(key=lambda g: (SEVERITY_ORDER[g['severity']], g['answer']))
    return result


SENTENCE_SPLIT = re.compile(r'[.;!?]+')


def _split_sentences(text):
    """Split question text into sentence-like chunks for clue reuse detection."""
    plain = strip_markup(text).lower()
    sentences = SENTENCE_SPLIT.split(plain)
    return [s.strip() for s in sentences if len(s.strip()) > 20]


def find_internal_issues(qset):
    """Find within-question issues: bonus part answer repeats and tossup clue reuse.

    Returns a list of issue dicts sorted by severity, each with:
        - issue_type: 'bonus_repeat_answer' or 'tossup_clue_reuse'
        - severity: 'critical' or 'warning'
        - question_type: 'bonus' or 'tossup'
        - question_id: int
        - author: str
        - category_str: str
        - description: human-readable explanation
        - details: dict with issue-specific info
    """
    from .models import Tossup, Bonus

    issues = []

    # Check bonuses for repeated part answers
    for bonus in Bonus.objects.filter(question_set=qset).select_related('category', 'author'):
        parts = []
        for label, answer in [('Part 1', bonus.part1_answer),
                               ('Part 2', bonus.part2_answer),
                               ('Part 3', bonus.part3_answer)]:
            if answer:
                parts.append((label, normalize_answer(answer), answer))

        # Check all pairs of parts for matching normalized answers
        matches = []
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                if parts[i][1] and parts[i][1] == parts[j][1]:
                    matches.append((parts[i][0], parts[j][0], parts[i][2]))

        if matches:
            matched_parts = []
            for a, b, raw in matches:
                matched_parts.append(f'{a} and {b}')
            issues.append({
                'issue_type': 'bonus_repeat_answer',
                'severity': CRITICAL,
                'question_type': 'bonus',
                'question_id': bonus.id,
                'author': str(bonus.author),
                'category_str': _get_category_str(bonus),
                'description': f'Bonus has the same answer for {", ".join(matched_parts)}: '
                               f'"{matches[0][2][:60]}"',
                'details': {
                    'matched_parts': [(a, b, raw) for a, b, raw in matches],
                },
            })

    # Check tossups for clue reuse (repeated content across sentences)
    for tu in Tossup.objects.filter(question_set=qset).select_related('category', 'author'):
        sentences = _split_sentences(tu.tossup_text)
        if len(sentences) < 2:
            continue

        sent_words = [extract_clue_words(s) for s in sentences]
        reused_pairs = []
        for i in range(len(sent_words)):
            for j in range(i + 1, len(sent_words)):
                sim = clue_similarity(sent_words[i], sent_words[j])
                if sim >= 0.3:
                    reused_pairs.append((i + 1, j + 1, sim))

        if reused_pairs:
            pair_strs = [f'sentences {a} and {b} ({int(s * 100)}% overlap)'
                         for a, b, s in reused_pairs]
            issues.append({
                'issue_type': 'tossup_clue_reuse',
                'severity': WARNING,
                'question_type': 'tossup',
                'question_id': tu.id,
                'author': str(tu.author),
                'category_str': _get_category_str(tu),
                'description': f'Tossup has similar clues in {", ".join(pair_strs)}',
                'details': {
                    'reused_pairs': reused_pairs,
                    'answer': tu.tossup_answer[:80],
                },
            })

    issues.sort(key=lambda i: (SEVERITY_ORDER[i['severity']], i['question_type']))
    return issues
