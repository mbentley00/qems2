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
