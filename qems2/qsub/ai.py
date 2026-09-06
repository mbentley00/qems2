"""Claude-powered helpers for the AI-assisted editing features.

These are gated to the admin user in the views; this module only deals with
talking to the Claude API. The API key comes from settings.ANTHROPIC_API_KEY
(env var or the git-ignored `anthropic_key` file). Proofreading uses the cheap
default model (settings.AI_DEFAULT_MODEL, Haiku); suggesting alternate answers
is a harder knowledge task and uses a stronger model (settings.AI_ANSWER_MODEL,
Sonnet). AI features are simply unavailable when no key is configured.
"""

import json

from django.conf import settings


def ai_enabled():
    """True when an Anthropic API key is configured."""
    return bool(getattr(settings, 'ANTHROPIC_API_KEY', ''))


def _client():
    """Return an Anthropic client, or None when AI is not configured."""
    if not ai_enabled():
        return None
    import anthropic
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


# --- grammar / spelling proofread -----------------------------------------

_GRAMMAR_SCHEMA = {
    'type': 'object',
    'properties': {
        'findings': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'ref': {'type': 'string',
                            'description': 'The exact ref label of the question this applies to.'},
                    'severity': {'type': 'string', 'enum': ['error', 'warning']},
                    'excerpt': {'type': 'string',
                                'description': 'The exact problematic text, copied verbatim.'},
                    'suggestion': {'type': 'string',
                                   'description': 'The corrected text to replace the excerpt.'},
                    'explanation': {'type': 'string',
                                    'description': 'A short reason for the correction.'},
                },
                'required': ['ref', 'severity', 'excerpt', 'suggestion', 'explanation'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['findings'],
    'additionalProperties': False,
}

_GRAMMAR_SYSTEM = (
    "You are a meticulous copy editor for quizbowl questions. You are given a "
    "list of questions, each preceded by a ref label. Check each one for "
    "genuine grammar mistakes, spelling errors, typos, punctuation errors, and "
    "obvious word-level errors (e.g. wrong homophone, doubled or missing "
    "words). Report only real errors that should be fixed — do not flag "
    "stylistic preferences, quizbowl formatting conventions, pronunciation "
    "guides, the markup characters (underscores, tildes, asterisks), or "
    "matters of taste. If a question has no errors, return nothing for it. For "
    "each finding, copy the problematic text verbatim into 'excerpt', give the "
    "corrected text in 'suggestion', and set 'ref' to the exact ref label of "
    "the question it came from."
)


# --- alternate-answer suggestions -----------------------------------------

_ANSWER_SCHEMA = {
    'type': 'object',
    'properties': {
        'suggestions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'ref': {'type': 'string',
                            'description': 'The exact ref label of the question this applies to.'},
                    'suggestion': {'type': 'string',
                                   'description': 'The addition to the answer line, phrased like '
                                                  '"accept X" or "prompt on Y". Use answer-line '
                                                  'markup: _underscores_ around the required words '
                                                  'of each accepted name, ~tildes~ around titles of '
                                                  'works. Join multiple names with "; or" — never '
                                                  'with a slash.'},
                    'explanation': {'type': 'string',
                                    'description': 'Why this names the same answer and should be '
                                                   'accepted or prompted.'},
                },
                'required': ['ref', 'suggestion', 'explanation'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['suggestions'],
    'additionalProperties': False,
}

_ANSWER_SYSTEM = (
    "You are an expert quizbowl editor reviewing answer lines. Each question is "
    "given with a ref label, its text, and its ANSWER line (after 'ANSWER:'). "
    "Answer lines use QEMS markup: _underscores_ mark the underlined words a "
    "player is required to say, and ~tildes~ mark italicized titles of works. "
    "The correct answer is the underlined/required entity named on the ANSWER "
    "line. Your ONLY job: identify additional ways to name that SAME correct "
    "entity that a moderator should accept or prompt on but that are NOT already "
    "on the answer line — alternate spellings, transliterations, English vs. "
    "original-language names, common nicknames, full vs. partial names, or a "
    "broader/narrower form that merits a prompt.\n\n"
    "ABSOLUTE RULES:\n"
    "- An alternate answer must refer to the EXACT SAME person, place, thing, or "
    "work as the given answer. It is another NAME for the answer — never a "
    "different entity.\n"
    "- NEVER suggest something that merely appears in the question as a CLUE (an "
    "author, character, related person, place, or work mentioned to lead to the "
    "answer). Clues are not answers. Example: if the answer is the city "
    "'Vienna' and the question mentions Adolf Loos as a clue, do NOT suggest "
    "'accept Adolf Loos' — Loos is not Vienna. This mistake is the single most "
    "important thing to avoid.\n"
    "- Do not restate the answer already given, or trivial case/punctuation "
    "variants of it.\n"
    "- If you are not highly confident an addition is a correct equivalent that "
    "a good moderator would accept, suggest nothing. MOST questions need "
    "nothing — returning an empty list is the common, correct outcome.\n\n"
    "FORMAT RULES for 'suggestion' (standard answer-line style):\n"
    "- Phrase it as 'accept X' or 'prompt on Y'.\n"
    "- Wrap the words a moderator must hear in underscores, mirroring how the "
    "given answer line marks its own required portion: 'accept _Bill Clinton_', "
    "'accept William Jefferson _Clinton_'.\n"
    "- Italicize titles of works with tildes as well: 'accept ~_The Trial_~'.\n"
    "- Do NOT underline names after 'prompt on' — write them plain (tildes for "
    "titles still apply): 'prompt on Clinton'.\n"
    "- Join multiple names with '; or ' — NEVER separate names with a slash. "
    "Example: 'accept _William Jefferson Clinton_; or _Bill Clinton_'.\n\n"
    "For each suggestion, set 'ref' to the exact ref label and give a one-line "
    "'explanation' of why it names the same answer."
)


# How many questions to send per Claude call. Batching keeps each request well
# within the model's output budget so the whole set can be checked across
# several calls rather than truncated to one request.
_BATCH_SIZE = 40


def _grammar_batch(client, model, items):
    """Proofread one batch. Returns (findings, error); findings carry kind='grammar'."""
    body = '\n\n'.join('[{0}]\n{1}'.format(it['ref'], it['text']) for it in items)
    try:
        resp = client.messages.create(
            model=model, max_tokens=8000, system=_GRAMMAR_SYSTEM,
            messages=[{'role': 'user', 'content': 'Check these questions for errors:\n\n' + body}],
            output_config={'format': {'type': 'json_schema', 'schema': _GRAMMAR_SCHEMA}})
    except Exception as ex:
        return [], 'The AI grammar check failed: {0}'.format(ex)
    text = next((b.text for b in resp.content if getattr(b, 'type', '') == 'text'), '')
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return [], 'The AI returned an unexpected response.'
    return [dict(f, kind='grammar') for f in data.get('findings', [])], None


def _answer_batch(client, model, items):
    """Suggest alternate answers for one batch. Returns (findings, error);
    findings carry kind='answer'."""
    body = '\n\n'.join('[{0}]\n{1}'.format(it['ref'], it['text']) for it in items)
    try:
        resp = client.messages.create(
            model=model, max_tokens=8000, system=_ANSWER_SYSTEM,
            messages=[{'role': 'user', 'content':
                       'Suggest alternate acceptable answers for these questions:\n\n' + body}],
            output_config={'format': {'type': 'json_schema', 'schema': _ANSWER_SCHEMA}})
    except Exception as ex:
        return [], 'The AI answer-suggestion pass failed: {0}'.format(ex)
    text = next((b.text for b in resp.content if getattr(b, 'type', '') == 'text'), '')
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return [], 'The AI returned an unexpected response.'
    return [{'kind': 'answer', 'ref': a.get('ref', ''), 'severity': 'info', 'excerpt': '',
             'suggestion': a.get('suggestion', ''), 'explanation': a.get('explanation', '')}
            for a in data.get('suggestions', [])], None


# --- category tag suggestions ---------------------------------------------

_TAG_SCHEMA = {
    'type': 'object',
    'properties': {
        'assignments': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'ref': {'type': 'string',
                            'description': 'The exact ref label of the question this applies to.'},
                    'tag': {'type': 'string',
                            'description': 'The tag name, copied exactly as given in the tag list.'},
                    'confidence': {'type': 'string', 'enum': ['high', 'medium']},
                    'explanation': {'type': 'string',
                                    'description': 'One short sentence: what in the question puts '
                                                   'it under this tag.'},
                },
                'required': ['ref', 'tag', 'confidence', 'explanation'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['assignments'],
    'additionalProperties': False,
}

_TAG_SYSTEM = (
    "You are an experienced quizbowl editor filing questions under a set's own "
    "category tags. You are given the set's tag list, then a batch of "
    "questions, each preceded by a ref label. For each question, say which of "
    "the listed tags it belongs under.\n\n"
    "RULES:\n"
    "- Use ONLY tag names from the list, copied exactly. Never invent a tag, "
    "and never propose one that is already listed as a tag the question "
    "carries.\n"
    "- Tag what the question is ABOUT -- its answer and the subject its clues "
    "belong to -- not every proper noun that appears in it. A passing mention "
    "is not what a tag is for. A question whose answer is a French novel is a "
    "French question even if it mentions a German city in one clue.\n"
    "- A question may take several tags (they usually run along different "
    "axes, e.g. a period and a region), or none at all. Returning nothing for "
    "a question is a perfectly good answer, and better than a guess.\n"
    "- Prefer the most specific tag that fits. A tag broad enough to cover "
    "nearly every question in the category tells an editor nothing, so propose "
    "one only where no more specific tag in the list applies.\n"
    "- Where a tag's name carries a definition in the list (a period, a region, "
    "a form), hold the question to it. Do not stretch a tag to fit.\n"
    "- Use 'high' confidence only when the question plainly belongs under the "
    "tag and an editor would agree without argument; use 'medium' when it is a "
    "reasonable call someone might make differently. If it is weaker than that, "
    "leave it out.\n"
    "- The answer line is the strongest evidence of what a question is about. "
    "It follows 'ANSWER:'."
)

# Fewer questions per call than the proofreading passes: the tag list rides
# along in every request, and each question is being judged rather than scanned.
_TAG_BATCH_SIZE = 20


def _tag_batch(client, model, items, tag_block):
    """Propose tags for one batch. Returns (assignments, error)."""
    body = '\n\n'.join('[{0}]\n{1}'.format(it['ref'], it['text']) for it in items)
    try:
        resp = client.messages.create(
            model=model, max_tokens=8000,
            system=[{'type': 'text', 'text': _TAG_SYSTEM},
                    # The tag list is the same in every batch of a run, so it is
                    # cached rather than re-read at full price each time.
                    {'type': 'text', 'text': 'The tags defined for this category:\n\n' + tag_block,
                     'cache_control': {'type': 'ephemeral'}}],
            messages=[{'role': 'user',
                       'content': 'Which of those tags does each of these questions belong '
                                  'under?\n\n' + body}],
            output_config={'format': {'type': 'json_schema', 'schema': _TAG_SCHEMA}})
    except Exception as ex:
        return [], 'The AI tag suggestion failed: {0}'.format(ex)
    text = next((b.text for b in resp.content if getattr(b, 'type', '') == 'text'), '')
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return [], 'The AI returned an unexpected response.'
    return data.get('assignments', []), None


def suggest_category_tags(items, tags, model=None):
    """Propose category tags for questions.

    `items` is a list of {'ref': str, 'text': str} (the question, with its
    answer line, and any tags it already carries). `tags` is the list of tags
    on offer as {'name': str, 'description': str} -- the description carries
    the tag's group and quota so the model can see what the tag is for.
    Returns (assignments, error), where each assignment is a dict of
    ref/tag/confidence/explanation. The caller matches the tag name back to a
    real CategoryTag and discards anything that doesn't match: the names come
    from a model, so they are checked rather than trusted. On a mid-run
    failure, whatever was gathered so far is returned alongside the error.
    """
    client = _client()
    if client is None:
        return [], 'AI features are not configured (no API key).'
    if not items or not tags:
        return [], None

    tag_block = '\n'.join(
        '- {0}{1}'.format(t['name'], (': ' + t['description']) if t.get('description') else '')
        for t in tags)
    tag_model = model or getattr(settings, 'AI_TAG_MODEL', None) or settings.AI_DEFAULT_MODEL

    assignments = []
    for start in range(0, len(items), _TAG_BATCH_SIZE):
        batch, error = _tag_batch(client, tag_model, items[start:start + _TAG_BATCH_SIZE],
                                  tag_block)
        if error:
            return assignments, error
        assignments.extend(batch)
    return assignments, None


def grammar_check_questions(items, model=None, answer_items=None):
    """Proofread the questions AND suggest alternate acceptable answers.

    `items` is a list of dicts: {'ref': str, 'text': str}. The whole list is
    processed in batches (no cap on set size). Grammar uses AI_DEFAULT_MODEL;
    alternate answers use the stronger AI_ANSWER_MODEL. `answer_items` (same
    shape) optionally carries markup-preserving text for the answer pass, so
    the model can see which words are underlined/required and copy the set's
    formatting; grammar always reads the plain `items`. Returns (findings,
    error) where findings is a list of dicts (kind, ref, severity, excerpt,
    suggestion, explanation); kind is 'grammar' or 'answer'. On a mid-run
    failure, whatever was gathered so far is returned alongside the error
    string.
    """
    client = _client()
    if client is None:
        return [], 'AI features are not configured (no API key).'
    if not items:
        return [], None

    grammar_model = model or settings.AI_DEFAULT_MODEL
    answer_model = getattr(settings, 'AI_ANSWER_MODEL', None) or grammar_model
    answer_src = answer_items if answer_items is not None else items

    findings = []
    for start in range(0, len(items), _BATCH_SIZE):
        batch = items[start:start + _BATCH_SIZE]
        batch_findings, error = _grammar_batch(client, grammar_model, batch)
        if error:
            return findings, error
        findings.extend(batch_findings)
    for start in range(0, len(answer_src), _BATCH_SIZE):
        batch = answer_src[start:start + _BATCH_SIZE]
        batch_findings, error = _answer_batch(client, answer_model, batch)
        if error:
            return findings, error
        findings.extend(batch_findings)
    return findings, None
