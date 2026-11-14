"""Words that may need a pronunciation guide, from a small local model.

The model (qsub/data/pg_need_model.json) is a logistic regression trained on
which words writers guided in about 7,000 tossups from 366 packets: the word's
letters (2-4 letter chunks), its length, capitals, accents, italics, how common
it is in that corpus, and the words on either side. It is generated offline by
``scripts/export_pg_model.py`` in the pg_split_experiments folder, and the
features here must stay in step with that script's ``feats()``.

It answers one question per word, "would a writer guide this?", with a
probability. Against writers it is right about a third of the time at the
threshold used here, and many of its "misses" are guides the writer left out,
so it is a hint to look, never a fix: it doesn't know the pronunciation.

Pure Python, no numpy: a few microseconds per candidate word.
"""

import json
import math
import os
import re

DATA_PATH = os.path.join(os.path.dirname(__file__), 'data', 'pg_need_model.json')

# Flag a word at or above this probability. The trained (max-F1) threshold is
# about 0.33, where three in four flags are words the writer left unguided; a
# style-check hint should be choosier than that.
THRESHOLD = 0.5

_MODEL = None

# A parenthetical or bracketed guide: ("FAH-bree"), [MON-dree-ahn], ("eev san lo-RON")
_PG_RE = re.compile(r'\s*[\(\[]\s*(["“”]?)([^()\[\]"“”]{1,70}?)(["“”]?)\s*[\)\]]')
_DIRECTIVE_WORDS = {
    'read', 'slowly', 'pause', 'emphasize', 'emphasise', 'pronounced', 'note', 'moderator',
    'points', 'point', 'or', 'and', 'the', 'accept', 'prompt', 'do', 'not', 'reveal', 'this',
    'for', 'of', 'in', 'a', 'is', 'as', 'see', 'aka', 'also', 'known', 'spelled', 'sic'}
_SENTENCE_END = re.compile(r'[.!?]["”’)]*$')
# Never worth a guide, whatever the model says: Roman numerals ("XIII"),
# contractions ("They're"), and two words run together with a dash or a dot
# that the whitespace tokenizer didn't separate ("machines—robot").
_ROMAN = re.compile(r'M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})')
_CONTRACTION = re.compile(r"['’](re|ll|ve|d|m|t)$|n['’]t$", re.I)
_JOINED = re.compile(r'[—–•·/]')
_POSSESSIVE = re.compile(r"['’]s?$")
_CLAUSE_END = re.compile(r'[,.;:!?]["”’)\]]*$')


def _load():
    global _MODEL
    if _MODEL is None:
        if not os.path.exists(DATA_PATH):
            _MODEL = {}
        else:
            with open(DATA_PATH, encoding='utf-8') as fh:
                raw = json.load(fh)
            _MODEL = {
                'bias': raw['bias'],
                'weights': raw['weights'],
                'common': frozenset(raw['common']),
                # {form: any_count} or {form: [any_count, lower_count]}
                'counts': raw['counts'],
            }
    return _MODEL


def _is_pg(open_q, body, close_q):
    """Whether a parenthetical reads as a pronunciation guide (a respelling),
    not an aside or a note to the moderator. The training data's own rule."""
    body = body.strip()
    if not body or '*' in body:
        return False
    if not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿĀ-žḀ-ỿ' \-·.]+", body):
        return False
    toks = [t for t in re.split(r'[\s\-]+', body) if t]
    if not toks:
        return False
    if all(t.lower().strip(".'") in _DIRECTIVE_WORDS for t in toks):
        return False
    caps_syllable = any(len(t) >= 2 and t.isupper() for t in toks)
    hyphen = '-' in body
    quoted = bool(open_q or close_q)
    if hyphen and caps_syllable:
        return True
    if quoted and (caps_syllable or hyphen):
        return True
    return quoted and len(toks) <= 3 and body == body.lower() and len(body) <= 25


def _tokens(text):
    """Whitespace tokens of `text` with its guides taken out, as
    [token, start, end, guided]. A guide covers as many words before it as it
    has words itself, not reaching back past punctuation (MODAQ's rule).
    Punctuation right after a guide's ")" is glued to the guided word."""
    out = []
    pos = 0

    def add(segment, offset):
        for m in re.finditer(r'\S+', segment):
            out.append([m.group(0), offset + m.start(), offset + m.end(), False])

    for m in _PG_RE.finditer(text):
        if not _is_pg(m.group(1), m.group(2), m.group(3)):
            continue
        add(text[pos:m.start()], pos)
        want = max(1, len(m.group(2).split()))
        k, took = len(out) - 1, 0
        while k >= 0 and took < want:
            if took > 0 and _CLAUSE_END.search(out[k][0]):
                break
            out[k][3] = True
            took += 1
            k -= 1
        pos = m.end()
        tail = re.match(r'\S+', text[pos:])
        if tail and out:
            out[-1][0] += tail.group(0)
            pos += len(tail.group(0))
    add(text[pos:], pos)
    return out


def _core(tok):
    return re.sub(r'^\W+|\W+$', '', tok)


def _counts(model, word):
    """(any_count, lower_count) of a word form in the training corpus. A form
    the corpus has once or never is scored as seen once, here: every training
    word was counted in its own corpus, so that is what the model learned."""
    c = model['counts'].get(word.lower())
    if c is None:
        return 1, int(word[:1].islower())
    if isinstance(c, list):
        return c[0], c[1]
    return c, 0


def _hard_looking(model, word, sentence_initial, any_count):
    """Words worth asking about: 4+ letters, no digits, not among the corpus's
    6,000 commonest lowercase words, and accented, capitalised mid-sentence, or
    rare. The model only ever saw these."""
    if len(word) < 4 or not re.search(r'[^\W\d_]', word) or re.search(r'\d', word):
        return False
    if word.lower() in model['common']:
        return False
    return (any(ord(ch) > 127 for ch in word)
            or (word[0].isupper() and not sentence_initial)
            or any_count <= 3)


def _features(word, left, right, sentence_initial, italic, any_count, lower_count):
    lw = word.lower()
    f = {'bias': 1.0}
    padded = '^' + lw + '$'
    for n in (2, 3, 4):
        for i in range(len(padded) - n + 1):
            k = 'c%d:%s' % (n, padded[i:i + n])
            f[k] = f.get(k, 0.0) + 1.0 / n
    f['len:%d' % min(len(word), 14)] = 1.0
    f['cap'] = float(word[0].isupper())
    f['allcaps'] = float(word.isupper())
    f['sent_init'] = float(sentence_initial)
    f['nonascii'] = float(any(ord(c) > 127 for c in word))
    f['italic'] = float(italic)
    f['log_any'] = math.log1p(any_count)
    f['log_lower'] = math.log1p(lower_count)
    f['lower_ratio'] = lower_count / max(1, any_count)
    f['vowel_ratio'] = sum(c in 'aeiouy' for c in lw) / len(lw)
    for k, tok in enumerate(reversed(left[-2:])):
        f['L%d:%s' % (k, re.sub(r'\W', '', tok.lower()))] = 1.0
    if right:
        f['R0:%s' % re.sub(r'\W', '', right[0].lower())] = 1.0
        f['next_cap'] = float(right[0][:1].isupper())
    if left:
        f['prev_cap'] = float(left[-1][:1].isupper() and not _SENTENCE_END.search(left[-1]))
    return f


def probability(word, left, right, sentence_initial=False, italic=False):
    """P(a writer would guide `word`) given the tokens before and after it.
    Exposed for the parity test against the training script."""
    model = _load()
    if not model:
        return 0.0
    any_count, lower_count = _counts(model, word)
    weights = model['weights']
    z = model['bias'] + sum(weights.get(k, 0.0) * v for k, v in
                            _features(word, left, right, sentence_initial, italic,
                                      any_count, lower_count).items())
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def flag_words(text, italic_text='', threshold=THRESHOLD):
    """Words in `text` (readable plain text, markup stripped) that may need a
    pronunciation guide: [(word, start, end, probability)], first occurrence of
    each word only, in text order.

    A word that is guided anywhere in `text` is skipped, as are its repeats.
    `italic_text` is the italicised parts of the text joined together (titles
    and foreign words), which the model reads as a hint."""
    model = _load()
    if not model or not text:
        return []
    toks = _tokens(text)
    words = [t[0] for t in toks]
    def form_of(word):
        # "Pallavicino's" and "Pallavicino" are one name
        return _POSSESSIVE.sub('', word).lower()

    guided_forms = {form_of(_core(t[0])) for t in toks if t[3]}
    seen = set()
    out = []
    for i, (tok, start, end, guided) in enumerate(toks):
        word = _core(tok)
        if not word or guided:
            continue
        form = form_of(word)
        if form in seen or form in guided_forms:
            continue
        if _ROMAN.fullmatch(word) or _CONTRACTION.search(word) or _JOINED.search(word):
            continue
        sentence_initial = i == 0 or bool(_SENTENCE_END.search(words[i - 1]))
        any_count, _lower = _counts(model, word)
        if not _hard_looking(model, word, sentence_initial, any_count):
            continue
        p = probability(word, words[max(0, i - 25):i], words[i + 1:i + 26],
                        sentence_initial, bool(italic_text) and word in italic_text)
        if p >= threshold:
            seen.add(form)
            # Report "Yggdrasil", not "Yggdrasil's": the guide goes on the name
            shown = _POSSESSIVE.sub('', word) or word
            lead = tok.index(word) if word in tok else 0
            out.append((shown, start + lead, start + lead + len(shown), p))
    return out
