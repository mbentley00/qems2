"""The phone writer's thinking: reading screenshots of highlighted source
material, proposing what to write, drafting and revising a question, and
finding quotes in the sources. The request handling is in mobile_writer_views.

Everything here goes through Claude (settings.AI_WRITER_MODEL) except the two
outside lookups -- QB Reader, for what has already been asked, and Wikipedia,
as a source to quote from alongside the writer's own screenshots.

A quote is only ever shown as a quote if it can be found, word for word, in
the source it claims to come from (verify_quote). The model is asked for exact
copies; this is what holds it to that.
"""

import json
import re
import unicodedata
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.cache import cache

from . import ai


class WriterError(Exception):
    """Something the writer should see in place of a result."""


_USER_AGENT = 'QEMS3 question writer (https://qems3.buzz)'

# Opus 5.5 can decline a request in a safety category; "default" fallbacks have
# the API rerun such a request on a model chosen for that category, inside the
# same call, rather than handing back nothing.
_FALLBACK_BETA = 'server-side-fallback-2026-07-01'


def _call(system, content, schema, effort='medium', max_tokens=16000):
    """One structured-output request. Returns the parsed JSON, or raises
    WriterError with something fit to show."""
    client = ai._client()
    if client is None:
        raise WriterError('AI features are not configured (no API key).')
    import anthropic
    try:
        resp = client.beta.messages.create(
            model=settings.AI_WRITER_MODEL, max_tokens=max_tokens,
            betas=[_FALLBACK_BETA], fallbacks='default',
            system=system,
            messages=[{'role': 'user', 'content': content}],
            output_config={'effort': effort,
                           'format': {'type': 'json_schema', 'schema': schema}})
    except anthropic.RateLimitError:
        raise WriterError('Claude is busy right now. Try again in a minute.')
    except anthropic.APIStatusError as ex:
        raise WriterError('Claude returned an error: {0}'.format(ex.message))
    except anthropic.APIConnectionError:
        raise WriterError('Could not reach Claude. Check the connection and try again.')
    if resp.stop_reason == 'refusal':
        raise WriterError('Claude declined this request.')
    if resp.stop_reason == 'max_tokens':
        raise WriterError('The answer ran too long and was cut off. Try a narrower request.')
    text = next((b.text for b in resp.content if getattr(b, 'type', '') == 'text'), '')
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        raise WriterError('Claude returned something unexpected. Try again.')


# --- verbatim quotes -------------------------------------------------------

_QUOTE_MARKS = str.maketrans({'‘': "'", '’': "'", '“': '"', '”': '"',
                              '–': '-', '—': '-', ' ': ' '})


def _norm(text):
    """Text as compared for quoting: the same characters, minus differences no
    reader would call a misquote (curly vs straight quotes, dash widths,
    runs of whitespace, letter case)."""
    text = unicodedata.normalize('NFKC', text or '').translate(_QUOTE_MARKS)
    return re.sub(r'\s+', ' ', text).strip().lower()


def verify_quote(quote, sources):
    """The index of the first source that contains `quote` verbatim, or None.
    `sources` is a list of {'text': ...}."""
    needle = _norm(quote)
    if len(needle) < 8:
        return None
    for i, source in enumerate(sources):
        if needle in _norm(source.get('text', '')):
            return i
    return None


# --- question shapes ---------------------------------------------------------

_DIFFICULTY = {'type': 'string', 'enum': ['e', 'm', 'h']}

TOSSUP_SCHEMA = {
    'type': 'object',
    'properties': {
        'sentences': {'type': 'array', 'items': {'type': 'string'},
                      'description': 'The tossup, one sentence per item, hardest clue first and '
                                     'the "For 10 points" giveaway last.'},
        'power_after': {'type': 'integer',
                        'description': 'How many sentences are in power: the (*) mark goes '
                                       'after this many sentences.'},
        'answer': {'type': 'string', 'description': 'The answer line, in QEMS markup.'},
    },
    'required': ['sentences', 'power_after', 'answer'],
    'additionalProperties': False,
}

BONUS_SCHEMA = {
    'type': 'object',
    'properties': {
        'leadin': {'type': 'string'},
        'parts': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {'text': {'type': 'string'},
                               'answer': {'type': 'string'},
                               'difficulty': _DIFFICULTY},
                'required': ['text', 'answer', 'difficulty'],
                'additionalProperties': False,
            },
            'description': 'Exactly three parts, in the order they are read.',
        },
    },
    'required': ['leadin', 'parts'],
    'additionalProperties': False,
}

_MARKUP = (
    "QEMS markup: in answer lines, _underscores_ wrap the words a player must "
    "say; ~tildes~ wrap titles of works (in question text too). Add alternate "
    "answers in brackets: [accept _X_; prompt on Y]. Don't use HTML or "
    "asterisks for emphasis. In a tossup, never write the (*) power mark "
    "yourself -- say where it goes with power_after.")

_STYLE = (
    "You write quizbowl questions in the ACF style: pyramidal, every clue "
    "uniquely identifying the answer, harder clues first, no clue that gives "
    "the answer away before the giveaway, no ambiguous or transferable clues, "
    "concrete facts rather than vague praise. Tossups end with a 'For 10 "
    "points, name this ...' giveaway. Bonuses have a one-sentence leadin and "
    "three parts, each starting with 'For 10 points,' (except where the part is "
    "a follow-on like 'Name this ...'), one easy, one medium, one hard. Use only "
    "facts you are confident are true; the sources given are the ground truth "
    "and win over your memory when they disagree.")


def _clean_question(kind, q):
    """Hold a model-written question to its shape: no stray power marks in
    the text, a power position that fits, three bonus parts."""
    if kind == 'tossup':
        sentences = [re.sub(r'\s*\(\*\)\s*', ' ', s).strip()
                     for s in q.get('sentences', []) if s and s.strip()]
        power = max(0, min(int(q.get('power_after') or 0), max(len(sentences) - 1, 0)))
        return {'sentences': sentences, 'power_after': power,
                'answer': (q.get('answer') or '').strip()}
    parts = [{'text': (p.get('text') or '').strip(), 'answer': (p.get('answer') or '').strip(),
              'difficulty': p.get('difficulty') or ''} for p in q.get('parts', [])][:3]
    while len(parts) < 3:
        parts.append({'text': '', 'answer': '', 'difficulty': ''})
    return {'leadin': (q.get('leadin') or '').strip(), 'parts': parts}


def question_text(kind, q):
    """The question as QEMS stores it (tossup text with the (*) in place)."""
    if kind == 'tossup':
        out = []
        for i, sentence in enumerate(q['sentences']):
            out.append(sentence)
            if q['power_after'] and i == q['power_after'] - 1 and i < len(q['sentences']) - 1:
                out[-1] = sentence + ' (*)'
        return ' '.join(out)
    return q['leadin']


def _question_block(kind, q):
    if kind == 'tossup':
        lines = ['[{0}] {1}'.format(i + 1, s) for i, s in enumerate(q['sentences'])]
        return ('TOSSUP (power mark after sentence {0}):\n{1}\nANSWER: {2}'
                .format(q['power_after'], '\n'.join(lines), q['answer']))
    parts = '\n'.join('[part {0}] ({1}) {2}\nANSWER: {3}'.format(
        i + 1, p['difficulty'] or '?', p['text'], p['answer']) for i, p in enumerate(q['parts']))
    return 'BONUS:\n[leadin] {0}\n{1}'.format(q['leadin'], parts)


def _sources_block(sources):
    if not sources:
        return 'No sources were given.'
    return '\n\n'.join('<source index="{0}" label="{1}">\n{2}\n</source>'.format(
        i, s.get('label', ''), s.get('text', '')) for i, s in enumerate(sources))


# --- reading screenshots ---------------------------------------------------

_EXTRACT_SCHEMA = {
    'type': 'object',
    'properties': {
        'topic': {'type': 'string', 'description': 'What the highlighted material is about, '
                                                   'as a short name (a person, work, event, idea).'},
        'transcriptions': {
            'type': 'array',
            'items': {'type': 'object',
                      'properties': {'label': {'type': 'string'},
                                     'text': {'type': 'string'}},
                      'required': ['label', 'text'],
                      'additionalProperties': False},
            'description': 'One per image, in order: every legible word of the image, '
                           'copied exactly.'},
        'clues': {
            'type': 'array',
            'items': {'type': 'object',
                      'properties': {
                          'text': {'type': 'string',
                                   'description': 'The fact as a short, self-contained clue.'},
                          'quote': {'type': 'string',
                                    'description': 'The highlighted words it comes from, '
                                                   'copied exactly from the transcription.'},
                          'source': {'type': 'integer',
                                     'description': 'Index of the transcription it is from.'},
                          'search': {'type': 'string',
                                     'description': '2-4 distinctive words to search past '
                                                    'questions for this clue.'}},
                      'required': ['text', 'quote', 'source', 'search'],
                      'additionalProperties': False}},
    },
    'required': ['topic', 'transcriptions', 'clues'],
    'additionalProperties': False,
}

_EXTRACT_SYSTEM = (
    "You help a quizbowl writer turn research into questions. You are given "
    "screenshots or photos of source material (web pages, book pages, notes) "
    "in which the writer has highlighted the parts they care about, and maybe "
    "some pasted text. First transcribe each image completely and exactly -- "
    "this transcription is what later quotes are checked against, so never "
    "paraphrase, fix, or summarize it. Then list the facts the writer marked "
    "(highlighting, underlining, boxes, or bracketed in pasted text; if "
    "nothing is marked, the facts most useful as quizbowl clues). Each clue "
    "restates one fact briefly and carries the exact words it came from. Name "
    "the topic all of it is about.")


def extract_clues(images, pasted_text=''):
    """Read screenshots (a list of (media_type, base64 data)) and pasted text.
    Returns {'topic', 'sources': [{label, text}], 'clues': [{text, quote,
    source, search}]}; a clue's quote is dropped if it isn't really in the
    sources."""
    content = []
    for n, (media_type, data) in enumerate(images, start=1):
        content.append({'type': 'text', 'text': 'Image {0}:'.format(n)})
        content.append({'type': 'image',
                        'source': {'type': 'base64', 'media_type': media_type, 'data': data}})
    if pasted_text.strip():
        content.append({'type': 'text',
                        'text': 'Pasted text (transcribe it as the last item):\n' + pasted_text})
    content.append({'type': 'text', 'text': 'Transcribe, then list the marked clues.'})
    result = _call(_EXTRACT_SYSTEM, content, _EXTRACT_SCHEMA)

    sources = [{'label': t.get('label') or 'Source {0}'.format(i + 1), 'text': t.get('text', '')}
               for i, t in enumerate(result.get('transcriptions', []))]
    if pasted_text.strip() and not any(_norm(pasted_text)[:60] in _norm(s['text']) for s in sources):
        # The pasted text is a source in its own right, whatever the model did
        # with it.
        sources.append({'label': 'Pasted text', 'text': pasted_text})
    clues = []
    for c in result.get('clues', []):
        found = verify_quote(c.get('quote', ''), sources)
        clues.append({'text': c.get('text', ''), 'search': c.get('search', ''),
                      'quote': c.get('quote', '') if found is not None else '',
                      'source': found})
    return {'topic': result.get('topic', ''), 'sources': sources, 'clues': clues}


# --- what to write -----------------------------------------------------------

_PLAN_SCHEMA = {
    'type': 'object',
    'properties': {
        'angles': {
            'type': 'array',
            'items': {'type': 'object',
                      'properties': {
                          'title': {'type': 'string', 'description': 'The answer, plainly.'},
                          'answer_line': {'type': 'string'},
                          'note': {'type': 'string',
                                   'description': 'One line: which clues it uses and why it '
                                                  'works as a question.'},
                          'category': {'type': 'string',
                                       'description': 'Copied exactly from the category list.'}},
                      'required': ['title', 'answer_line', 'note', 'category'],
                      'additionalProperties': False},
            'description': 'Two or three different answers these clues could support.'},
    },
    'required': ['angles'],
    'additionalProperties': False,
}


def plan_angles(kind, topic, clues, categories):
    """Two or three answers the clues could be written toward, each with an
    answer line and a category from `categories` (a list of labels)."""
    clue_lines = '\n'.join('- ' + c for c in clues)
    content = ('Question type: {0}\nTopic: {1}\nClues:\n{2}\n\nCategories:\n{3}\n\n'
               'Propose answers to write a {0} on. The first should be the topic itself '
               'if it makes a good answer.').format(kind, topic, clue_lines,
                                                    '\n'.join(categories))
    system = _STYLE + ' ' + _MARKUP + (
        ' You are helping choose what to write a question on: propose distinct answers '
        'the clues support, each with a properly marked-up answer line.')
    result = _call(system, content, _PLAN_SCHEMA, effort='low')
    allowed = set(categories)
    return [dict(a, category=a.get('category') if a.get('category') in allowed else '')
            for a in result.get('angles', [])]


# --- drafting and revising -------------------------------------------------

def draft_question(kind, answer_line, clues, sources, max_chars, notes=''):
    content = ('Write a {0} whose answer is: {1}\n\nUse these clues, in whatever order '
               'makes it pyramidal (drop any that are weak or give the answer away too '
               'early):\n{2}\n\nKeep it under {3} characters.{4}\n\nSources:\n{5}').format(
        kind, answer_line, '\n'.join('- ' + c for c in clues), max_chars,
        ('\n\nWriter\'s notes: ' + notes) if notes else '', _sources_block(sources))
    schema = TOSSUP_SCHEMA if kind == 'tossup' else BONUS_SCHEMA
    return _clean_question(kind, _call(_STYLE + ' ' + _MARKUP, content, schema))


_REVISE_EXTRA = {
    'summary': {'type': 'string', 'description': 'One short line saying what changed.'},
}


def revise_question(kind, question, instruction, target=None, sources=(), max_chars=None):
    """Apply a free-form instruction to the question, or to one segment of it
    (`target` = sentence index for a tossup; 'leadin' or a part index for a
    bonus). Returns (question, summary)."""
    if target is None or target == '':
        scope = 'Apply it to the whole question.'
    elif target == 'answer':
        scope = 'Apply it to the answer line only; leave the question text word for word.'
    elif kind == 'tossup':
        scope = ('Apply it to sentence [{0}] only; leave the other sentences word for word '
                 'as they are, unless the instruction asks to add a sentence next to it.'
                 ).format(int(target) + 1)
    else:
        scope = 'Apply it to {0} only; leave the rest word for word.'.format(
            'the leadin' if target == 'leadin' else 'part {0}'.format(int(target) + 1))
    content = ('{0}\n\nInstruction: {1}\n{2}{3}\n\nSources:\n{4}').format(
        _question_block(kind, question), instruction, scope,
        ' Keep it under {0} characters.'.format(max_chars) if max_chars else '',
        _sources_block(list(sources)))
    base = TOSSUP_SCHEMA if kind == 'tossup' else BONUS_SCHEMA
    schema = dict(base, properties=dict(base['properties'], **_REVISE_EXTRA),
                  required=base['required'] + ['summary'])
    system = _STYLE + ' ' + _MARKUP + (
        ' You are revising a question the writer drafted. Make the change asked for and '
        'nothing else; keep their wording wherever the instruction does not touch it.')
    result = _call(system, content, schema)
    return _clean_question(kind, result), result.get('summary', '')


# --- looking things up -----------------------------------------------------

_LOOKUP_SCHEMA = {
    'type': 'object',
    'properties': {
        'quotes': {
            'type': 'array',
            'items': {'type': 'object',
                      'properties': {
                          'source': {'type': 'integer'},
                          'quote': {'type': 'string',
                                    'description': 'One to three sentences copied character '
                                                   'for character from that source.'},
                          'key': {'type': 'string',
                                  'description': 'The few words of the quote that matter most, '
                                                 'copied exactly from it.'},
                          'clue': {'type': 'string',
                                   'description': 'The quote rewritten as a quizbowl clue that '
                                                  'refers to the answer as "this ..."'}},
                      'required': ['source', 'quote', 'key', 'clue'],
                      'additionalProperties': False}},
        'answer': {'type': 'string',
                   'description': 'A direct answer to the question asked, from the quotes only; '
                                  'empty if no question was asked; say so if the sources do '
                                  'not answer it.'},
    },
    'required': ['quotes', 'answer'],
    'additionalProperties': False,
}

_LOOKUP_SYSTEM = (
    "You find passages in source documents for a quizbowl writer. Every quote "
    "must be copied character for character from the source it names -- it "
    "will be checked, and anything not found word for word is thrown away. "
    "Prefer passages with specific, checkable facts that would make good "
    "clues and that the writer has not used yet. Never answer from memory: if "
    "the sources don't say it, say that.")


def lookup(topic, focus, sources, question='', used=()):
    """Quotes from `sources` about `focus` (a clue, or the whole topic), each
    checked against its source. Returns {'quotes': [...], 'answer': str}."""
    if not sources:
        return {'quotes': [], 'answer': 'There are no sources to look in.'}
    content = ('Topic: {0}\nLooking for: {1}\n{2}{3}\n\nSources:\n{4}').format(
        topic, focus or 'more good clues about the topic',
        ('Question: ' + question + '\n') if question else '',
        ('Already used in the question:\n' + '\n'.join('- ' + u for u in used) + '\n') if used else '',
        _sources_block(sources))
    result = _call(_LOOKUP_SYSTEM, content, _LOOKUP_SCHEMA, effort='low')
    quotes = []
    for q in result.get('quotes', []):
        idx = verify_quote(q.get('quote', ''), sources)
        if idx is None:
            continue
        key = q.get('key', '')
        if _norm(key) not in _norm(q['quote']):
            key = ''
        quotes.append({'source': idx, 'label': sources[idx].get('label', ''),
                       'url': sources[idx].get('url', ''), 'quote': q['quote'],
                       'key': key, 'clue': q.get('clue', '')})
    return {'quotes': quotes[:4], 'answer': result.get('answer', '')}


# --- outside sources -------------------------------------------------------

def _get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={'User-Agent': _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def wikipedia_source(topic):
    """The plain text of the English Wikipedia article that best matches
    `topic`, as a source ({label, text, url}), or None. Cached for a day."""
    topic = (topic or '').strip()
    if not topic:
        return None
    key = 'mw-wiki:' + re.sub(r'\W+', '_', topic.lower())[:200]
    hit = cache.get(key)
    if hit is not None:
        return hit or None
    api = 'https://en.wikipedia.org/w/api.php?'
    try:
        found = _get_json(api + urllib.parse.urlencode(
            {'action': 'query', 'list': 'search', 'srsearch': topic, 'srlimit': 1,
             'format': 'json'}))
        results = found.get('query', {}).get('search', [])
        if not results:
            cache.set(key, '', 24 * 3600)
            return None
        title = results[0]['title']
        page = _get_json(api + urllib.parse.urlencode(
            {'action': 'query', 'prop': 'extracts', 'explaintext': 1, 'titles': title,
             'redirects': 1, 'format': 'json'}))
        pages = list(page.get('query', {}).get('pages', {}).values())
        text = pages[0].get('extract', '') if pages else ''
    except Exception:
        return None   # not cached: a network blip shouldn't stick for a day
    if not text:
        cache.set(key, '', 24 * 3600)
        return None
    source = {'label': 'Wikipedia: ' + title, 'text': text[:60000],
              'url': 'https://en.wikipedia.org/wiki/' + urllib.parse.quote(title.replace(' ', '_'))}
    cache.set(key, source, 24 * 3600)
    return source


_QBREADER = 'https://www.qbreader.org/api/query?'


def _qbreader_query(text, limit):
    return _get_json(_QBREADER + urllib.parse.urlencode(
        {'queryString': text, 'questionType': 'all', 'searchType': 'all',
         'maxReturnLength': limit}))


def _qb_set(q):
    s = q.get('set') or {}
    if isinstance(s, dict):
        return '{0}'.format(s.get('name', '')).strip()
    return str(s)


def qbreader_search(text, terms=(), limit=6):
    """Past questions on QB Reader mentioning `text`: how many there are, the
    first few, and how many of them use each clue in `terms`."""
    try:
        data = _qbreader_query(text, 100)
    except Exception:
        raise WriterError('QB Reader did not answer. Try again in a moment.')
    tossups, bonuses = data.get('tossups', {}), data.get('bonuses', {})
    items = []
    for q in tossups.get('questionArray', []):
        items.append({'kind': 'Tossup', 'set': _qb_set(q),
                      'text': q.get('question_sanitized', ''),
                      'answer': q.get('answer_sanitized', '')})
    for q in bonuses.get('questionArray', []):
        parts = q.get('parts_sanitized') or []
        answers = q.get('answers_sanitized') or []
        items.append({'kind': 'Bonus', 'set': _qb_set(q),
                      'text': (q.get('leadin_sanitized', '') + ' ' + ' / '.join(parts)).strip(),
                      'answer': ' / '.join(answers)})
    return {'tossups': tossups.get('count', 0), 'bonuses': bonuses.get('count', 0),
            'searched': len(items), 'clue_counts': clue_counts(items, terms),
            'items': items[:limit]}


_STOP = {'this', 'that', 'with', 'from', 'were', 'was', 'have', 'their', 'which', 'into',
         'after', 'before', 'about', 'name', 'points', 'work', 'works'}


def _stems(text):
    """Words worth matching on, cut to a stem so 'painted' meets 'painting'."""
    words = re.findall(r'[a-z0-9]+', unicodedata.normalize('NFKD', text or '').lower())
    return {w[:6] for w in words if len(w) > 3 and w not in _STOP}


def clue_counts(questions, terms):
    """For each clue's search words, how many of `questions` use that clue:
    those containing most of its distinctive words. Counting within the past
    questions on this answer is what says whether a clue is stale -- a bare
    search for 'Chess Game' would count every chess question ever written."""
    texts = [_stems(q['text'] + ' ' + q['answer']) for q in questions]
    out = []
    for term in [t for t in terms if t and t.strip()][:8]:
        want = _stems(term)
        if not want:
            continue
        need = max(1, -(-len(want) * 2 // 3))   # two thirds, rounded up
        out.append({'term': term.strip(),
                    'count': sum(1 for t in texts if len(want & t) >= need)})
    return out
