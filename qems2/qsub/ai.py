"""Claude-powered helpers for the AI-assisted editing features.

These are gated to the admin user in the views; this module only deals with
talking to the Claude API. The API key comes from settings.ANTHROPIC_API_KEY
(env var or the git-ignored `anthropic_key` file) and the model defaults to
Haiku (settings.AI_DEFAULT_MODEL). AI features are simply unavailable when no
key is configured.
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


# JSON schema the grammar checker constrains Claude's response to, so we get
# back a clean list of findings rather than free-form prose.
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


# How many questions to send per Claude call. Batching keeps each request well
# within the model's output budget so the whole set can be checked across
# several calls rather than truncated to one request.
_GRAMMAR_BATCH_SIZE = 40


def _grammar_check_batch(client, model, items):
    """Check a single batch of questions. Returns (findings, error)."""
    body = '\n\n'.join('[{0}]\n{1}'.format(it['ref'], it['text']) for it in items)
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=_GRAMMAR_SYSTEM,
            messages=[{'role': 'user', 'content':
                       'Check these questions for errors:\n\n' + body}],
            output_config={'format': {'type': 'json_schema', 'schema': _GRAMMAR_SCHEMA}},
        )
    except Exception as ex:
        return [], 'The AI grammar check failed: {0}'.format(ex)

    text = next((b.text for b in resp.content if getattr(b, 'type', '') == 'text'), '')
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return [], 'The AI returned an unexpected response.'
    return data.get('findings', []), None


def grammar_check_questions(items, model=None):
    """Run an AI grammar/spelling/error pass over the given questions.

    `items` is a list of dicts: {'ref': str, 'text': str}. The whole list is
    checked, in batches, so there is no cap on set size. Returns
    (findings, error) where findings is a list of dicts (ref, severity,
    excerpt, suggestion, explanation). On a mid-run failure, whatever findings
    were gathered so far are returned alongside the error string.
    """
    client = _client()
    if client is None:
        return [], 'AI features are not configured (no API key).'
    if not items:
        return [], None

    model = model or settings.AI_DEFAULT_MODEL
    findings = []
    for start in range(0, len(items), _GRAMMAR_BATCH_SIZE):
        batch = items[start:start + _GRAMMAR_BATCH_SIZE]
        batch_findings, error = _grammar_check_batch(client, model, batch)
        if error:
            return findings, error
        findings.extend(batch_findings)
    return findings, None
