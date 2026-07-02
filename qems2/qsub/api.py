"""Machine-to-machine API for an external recorder (the Discord playtest bot)
to write buzzes, bonus results, and comments to a question set.

Auth: a per-set secret key, sent as `Authorization: Bearer <key>` (or the
`X-QEMS-Set-Key` header). The key resolves to exactly one set and authorizes
writes only to that set. These endpoints are CSRF-exempt (no browser session).

Questions are identified by ANSWER LINE, not id (the recorder generally has no
QEMS id): the supplied answer is normalized and matched against the set's
question answers. If nothing matches the item is reported `unmatched` and the
recorder is expected to retry later, once the question exists. A per-item
`external_id` makes writes idempotent so retries/re-syncs don't double-count.
"""

import json
import re
import unicodedata
from collections import defaultdict
from functools import wraps

from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.models import Site
from django_comments.models import Comment

from .models import (
    QuestionSet, Tossup, Bonus, TossupBuzz, BonusResult, PlaytestSession,
    TossupHistory, BonusHistory,
    SetApiKey, DiscordCommentRef, DiscordThread, PLAYTEST_SOURCE_DISCORD,
    DISCORD_BOT_NAME)
from .utils import get_answer_no_formatting, get_primary_answer


# --- helpers -----------------------------------------------------------------

def _api_json(payload, status=200):
    return JsonResponse(payload, status=status)


def _set_from_bearer(request):
    """Resolve the active SetApiKey from the request's auth header, or None."""
    token = ''
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if auth.startswith('Bearer '):
        token = auth[len('Bearer '):].strip()
    if not token:
        token = (request.META.get('HTTP_X_QEMS_SET_KEY') or '').strip()
    if not token:
        return None
    key = (SetApiKey.objects.filter(key=token, active=True)
           .select_related('question_set').first())
    return key.question_set if key else None


def discord_api(view):
    """Authenticate by set key and exempt from CSRF. Attaches request.api_qset."""
    @csrf_exempt
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        qset = _set_from_bearer(request)
        if qset is None:
            return _api_json({'ok': False, 'error': 'Invalid or missing set API key.'},
                             status=401)
        request.api_qset = qset
        return view(request, *args, **kwargs)
    return _wrapped


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _norm(text):
    """Normalize an answer to a comparable key: drop formatting/markup and
    accents, keep only lowercase alphanumerics."""
    base = get_answer_no_formatting(text or '')
    base = unicodedata.normalize('NFKD', base)
    base = ''.join(c for c in base if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]', '', base.lower())


def _answer_forms(text):
    """The normalized forms a question's answer can be matched by: its primary
    answer (before any '[...]') and the full answer line."""
    forms = set()
    for variant in (get_primary_answer(text or ''), text or ''):
        n = _norm(variant)
        if n:
            forms.add(n)
    return forms


def _tossup_index(qset):
    idx = defaultdict(set)
    for t in Tossup.objects.filter(question_set=qset).only('id', 'tossup_answer'):
        for f in _answer_forms(t.tossup_answer):
            idx[f].add(t.id)
    return idx


def _bonus_index(qset):
    idx = defaultdict(set)
    for b in Bonus.objects.filter(question_set=qset).only(
            'id', 'part1_answer', 'part2_answer', 'part3_answer'):
        forms = set()
        forms |= _answer_forms(b.part1_answer)
        forms |= _answer_forms(b.part2_answer)
        forms |= _answer_forms(b.part3_answer)
        for f in forms:
            idx[f].add(b.id)
    return idx


def _latest_history_ids(model, history_model, question_ids):
    """Map each question id to the id of its current (most recent) history row,
    so imported buzzes/results can link to the version that was current at import
    time. Two queries total regardless of how many questions."""
    qh_by_q = dict(model.objects.filter(id__in=question_ids)
                   .values_list('id', 'question_history_id'))
    qh_ids = [qh for qh in qh_by_q.values() if qh]
    latest_by_qh = {}
    for hid, qh in (history_model.objects.filter(question_history_id__in=qh_ids)
                    .order_by('id').values_list('id', 'question_history_id')):
        latest_by_qh[qh] = hid  # ascending order leaves the largest id last
    return {qid: latest_by_qh.get(qh) for qid, qh in qh_by_q.items()}


def _resolve(idx, answer):
    """Match a supplied answer against an index. Returns ('ok', id),
    ('unmatched', None), or ('ambiguous', None). Tries both the primary answer
    (before any '[...]') and the full normalized line, so a caller can send a
    whole answer line like 'witchcraft [accept ...]' and still match a question
    stored with differently-worded acceptable answers."""
    ids = set()
    for form in _answer_forms(answer):
        ids |= idx.get(form, set())
    if not ids:
        return ('unmatched', None)
    if len(ids) > 1:
        return ('ambiguous', None)
    return ('ok', next(iter(ids)))


def _resolve_question(t_idx, b_idx, answer, hint):
    """Resolve an answer (+ optional qtype hint) to one question. Returns
    ('ok', 'tossup'|'bonus', id), ('ambiguous', None, None), or
    ('unmatched', None, None)."""
    candidates = []
    if hint in (None, '', 'tossup'):
        s, qid = _resolve(t_idx, answer)
        if s == 'ok':
            candidates.append(('tossup', qid))
        elif s == 'ambiguous':
            candidates.append(('ambiguous', None))
    if hint in (None, '', 'bonus'):
        s, qid = _resolve(b_idx, answer)
        if s == 'ok':
            candidates.append(('bonus', qid))
        elif s == 'ambiguous':
            candidates.append(('ambiguous', None))
    real = [c for c in candidates if c[0] != 'ambiguous']
    if len(real) > 1 or (not real and any(c[0] == 'ambiguous' for c in candidates)):
        return ('ambiguous', None, None)
    if not real:
        return ('unmatched', None, None)
    return ('ok', real[0][0], real[0][1])


def _discord_session(qset, player_name):
    """One PlaytestSession per Discord player on a set, so their buzzes group."""
    session, _ = PlaytestSession.objects.get_or_create(
        question_set=qset, player=None, source=PLAYTEST_SOURCE_DISCORD,
        player_name=player_name or 'Discord')
    return session


def _load_events(request, field):
    """Parse the POST body, returning (events_list, error_response_or_None)."""
    if request.method != 'POST':
        return None, _api_json({'ok': False, 'error': 'POST required.'}, status=405)
    try:
        body = json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return None, _api_json({'ok': False, 'error': 'Invalid JSON body.'}, status=400)
    events = body.get(field)
    if not isinstance(events, list):
        return None, _api_json(
            {'ok': False, 'error': 'Expected a JSON list under "{0}".'.format(field)},
            status=400)
    return events, None


# --- endpoints ---------------------------------------------------------------

@discord_api
def api_ping(request):
    """Validate a set key. GET or POST."""
    qset = request.api_qset
    return _api_json({'ok': True, 'set': qset.name, 'set_id': qset.id})


@discord_api
def api_buzzes(request):
    """Record tossup buzzes. Body: {"events": [{external_id, answer, player_name,
    buzz_word_index, total_words, char_position, correct, powered, superpowered,
    value, neg, answer_given, occurred_at}, ...]}. `answer` is matched to a tossup;
    `player_name` records who buzzed; `answer_given` (optional) stores what they
    said; `occurred_at` (optional ISO-8601) sets when the buzz happened.
    Re-sending the same `external_id` updates the existing buzz in place."""
    events, err = _load_events(request, 'events')
    if err:
        return err
    qset = request.api_qset
    idx = _tossup_index(qset)

    eids = [e.get('external_id') for e in events if isinstance(e, dict) and e.get('external_id')]
    # Existing rows by external_id, so a re-send UPDATES in place (corrected
    # position/value/time) instead of being skipped as an unchangeable duplicate.
    existing = {b.external_id: b for b in
                TossupBuzz.objects.filter(external_id__in=eids)} if eids else {}

    # Current question version per matched tossup, fetched once (see history_url).
    resolved_ids = [_resolve(idx, e.get('answer'))[1] for e in events if isinstance(e, dict)]
    hist_ids = _latest_history_ids(Tossup, TossupHistory, [q for q in resolved_ids if q])

    results = []
    for e in events:
        if not isinstance(e, dict):
            results.append({'external_id': '', 'status': 'error', 'error': 'not an object'})
            continue
        eid = (e.get('external_id') or '').strip()
        status, qid = _resolve(idx, e.get('answer'))
        if status != 'ok':
            results.append({'external_id': eid, 'status': status})
            continue

        correct = bool(e.get('correct'))
        superpowered = bool(e.get('superpowered')) and correct
        # A superpower buzz is also inside the regular power region.
        powered = (superpowered or bool(e.get('powered'))) and correct
        if e.get('value') is not None:
            value = _int(e.get('value'))
        elif correct:
            value = 20 if superpowered else 15 if powered else 10
        else:
            value = -5 if e.get('neg') else 0
        name = (e.get('player_name') or '').strip()
        when = parse_datetime(e.get('occurred_at') or '')

        fields = dict(
            tossup_id=qid, player_name=name,
            buzz_word_index=_int(e.get('buzz_word_index')),
            total_words=_int(e.get('total_words')),
            char_position=_int(e.get('char_position')),
            correct=correct, powered=powered, superpowered=superpowered, value=value,
            answer_given=(e.get('answer_given') or '')[:1000],
            tossup_history_id=hist_ids.get(qid),
            source=PLAYTEST_SOURCE_DISCORD)
        if when:
            fields['buzz_date'] = when

        obj = existing.get(eid) if eid else None
        if obj is not None:
            for k, v in fields.items():
                setattr(obj, k, v)
            obj.save()
            results.append({'external_id': eid, 'status': 'updated', 'value': value})
        else:
            obj = TossupBuzz.objects.create(
                session=_discord_session(qset, name), player=None,
                external_id=eid, **fields)
            if eid:
                existing[eid] = obj
            results.append({'external_id': eid, 'status': 'recorded', 'value': value})

    return _api_json({'ok': True, 'results': results})


@discord_api
def api_bonus_results(request):
    """Record bonus results. Body: {"events": [{external_id, answer, player_name,
    part1_correct, part2_correct, part3_correct, occurred_at}, ...]}. `answer` is
    matched against any of the bonus's part answers; `occurred_at` (optional
    ISO-8601) sets when it happened. Re-sending the same `external_id` updates
    the existing result in place."""
    events, err = _load_events(request, 'events')
    if err:
        return err
    qset = request.api_qset
    idx = _bonus_index(qset)

    eids = [e.get('external_id') for e in events if isinstance(e, dict) and e.get('external_id')]
    # See api_buzzes: re-sends UPDATE the existing row instead of being skipped.
    existing = {b.external_id: b for b in
                BonusResult.objects.filter(external_id__in=eids)} if eids else {}

    # Current question version per matched bonus, fetched once (see history_url).
    resolved_ids = [_resolve(idx, e.get('answer'))[1] for e in events if isinstance(e, dict)]
    hist_ids = _latest_history_ids(Bonus, BonusHistory, [q for q in resolved_ids if q])

    results = []
    for e in events:
        if not isinstance(e, dict):
            results.append({'external_id': '', 'status': 'error', 'error': 'not an object'})
            continue
        eid = (e.get('external_id') or '').strip()
        status, qid = _resolve(idx, e.get('answer'))
        if status != 'ok':
            results.append({'external_id': eid, 'status': status})
            continue

        p1 = bool(e.get('part1_correct'))
        p2 = bool(e.get('part2_correct'))
        p3 = bool(e.get('part3_correct'))
        name = (e.get('player_name') or '').strip()
        total = 10 * sum((p1, p2, p3))
        when = parse_datetime(e.get('occurred_at') or '')

        fields = dict(
            bonus_id=qid, player_name=name,
            part1_correct=p1, part2_correct=p2, part3_correct=p3, total=total,
            bonus_history_id=hist_ids.get(qid),
            source=PLAYTEST_SOURCE_DISCORD)
        if when:
            fields['answered_date'] = when

        obj = existing.get(eid) if eid else None
        if obj is not None:
            for k, v in fields.items():
                setattr(obj, k, v)
            obj.save()
            results.append({'external_id': eid, 'status': 'updated', 'total': total})
        else:
            obj = BonusResult.objects.create(
                session=_discord_session(qset, name), player=None,
                external_id=eid, **fields)
            if eid:
                existing[eid] = obj
            results.append({'external_id': eid, 'status': 'recorded', 'total': total})

    return _api_json({'ok': True, 'results': results})


@discord_api
def api_comments(request):
    """Add comments to questions. Body: {"comments": [{external_id, answer, text,
    author_name, qtype?}, ...]}. `answer` identifies the question; optional
    `qtype` ('tossup'|'bonus') disambiguates; `author_name` records who said it;
    `text` is the comment."""
    comments, err = _load_events(request, 'comments')
    if err:
        return err
    qset = request.api_qset
    t_idx = _tossup_index(qset)
    b_idx = _bonus_index(qset)

    eids = [c.get('external_id') for c in comments if isinstance(c, dict) and c.get('external_id')]
    existing_refs = {r.external_id: r for r in
                     DiscordCommentRef.objects.filter(external_id__in=eids)
                     .select_related('comment')} if eids else {}
    seen = set(existing_refs)
    site = Site.objects.get_current()
    tu_ct = ContentType.objects.get_for_model(Tossup)
    bs_ct = ContentType.objects.get_for_model(Bonus)

    results = []
    for c in comments:
        if not isinstance(c, dict):
            results.append({'external_id': '', 'status': 'error', 'error': 'not an object'})
            continue
        eid = (c.get('external_id') or '').strip()
        text = (c.get('text') or '').strip()
        if eid and eid in seen:
            # Already synced. Refresh the comment in place when the text changed
            # (e.g. new discussion added to the thread) so re-syncs stay current.
            ref = existing_refs.get(eid)
            if ref is None:
                results.append({'external_id': eid, 'status': 'duplicate'})
            elif not text:
                results.append({'external_id': eid, 'status': 'error', 'error': 'empty text'})
            elif ref.comment.comment != text:
                ref.comment.comment = text
                ref.comment.save(update_fields=['comment'])
                results.append({'external_id': eid, 'status': 'updated'})
            else:
                results.append({'external_id': eid, 'status': 'duplicate'})
            continue
        if not text:
            results.append({'external_id': eid, 'status': 'error', 'error': 'empty text'})
            continue

        hint = c.get('qtype')
        candidates = []
        if hint in (None, '', 'tossup'):
            s, qid = _resolve(t_idx, c.get('answer'))
            if s == 'ok':
                candidates.append((tu_ct, qid))
            elif s == 'ambiguous':
                candidates.append(('ambiguous', None))
        if hint in (None, '', 'bonus'):
            s, qid = _resolve(b_idx, c.get('answer'))
            if s == 'ok':
                candidates.append((bs_ct, qid))
            elif s == 'ambiguous':
                candidates.append(('ambiguous', None))

        real = [c2 for c2 in candidates if c2[0] != 'ambiguous']
        if len(real) > 1 or (not real and any(c2[0] == 'ambiguous' for c2 in candidates)):
            results.append({'external_id': eid, 'status': 'ambiguous'})
            continue
        if not real:
            results.append({'external_id': eid, 'status': 'unmatched'})
            continue

        ct, qid = real[0]
        # Comments from the bot are attributed to the bot's persona, not the
        # individual playtester. Discord thread links live on the question (see
        # api_threads), not inside the comment text.
        comment = Comment.objects.create(
            content_type=ct, object_pk=str(qid), site=site, user=None,
            user_name=DISCORD_BOT_NAME, comment=text, is_public=True, is_removed=False)
        if eid:
            ref = DiscordCommentRef.objects.create(
                external_id=eid, comment=comment, question_set=qset)
            existing_refs[eid] = ref
            seen.add(eid)
        results.append({'external_id': eid, 'status': 'recorded',
                        'qtype': 'tossup' if ct == tu_ct else 'bonus'})

    return _api_json({'ok': True, 'results': results})


@discord_api
def api_threads(request):
    """Attach Discord thread links to questions, shown on the question itself
    (not inside a comment). Body: {"threads": [{external_id, answer, url, title,
    qtype?}, ...]}. `answer` is matched like comments; optional `qtype`
    ('tossup'|'bonus') disambiguates. Idempotent per `external_id`; without one,
    deduped per (question, url)."""
    threads, err = _load_events(request, 'threads')
    if err:
        return err
    qset = request.api_qset
    t_idx = _tossup_index(qset)
    b_idx = _bonus_index(qset)

    eids = [t.get('external_id') for t in threads if isinstance(t, dict) and t.get('external_id')]
    seen = set(DiscordThread.objects.filter(question_set=qset, external_id__in=eids)
               .values_list('external_id', flat=True)) if eids else set()

    results = []
    for t in threads:
        if not isinstance(t, dict):
            results.append({'external_id': '', 'status': 'error', 'error': 'not an object'})
            continue
        eid = (t.get('external_id') or '').strip()
        if eid and eid in seen:
            results.append({'external_id': eid, 'status': 'duplicate'})
            continue
        url = (t.get('url') or '').strip()
        if not url:
            results.append({'external_id': eid, 'status': 'error', 'error': 'empty url'})
            continue
        status, qtype, qid = _resolve_question(t_idx, b_idx, t.get('answer'), t.get('qtype'))
        if status != 'ok':
            results.append({'external_id': eid, 'status': status})
            continue

        title = (t.get('title') or '')[:300]
        tossup_id = qid if qtype == 'tossup' else None
        bonus_id = qid if qtype == 'bonus' else None
        if eid:
            DiscordThread.objects.create(
                question_set=qset, tossup_id=tossup_id, bonus_id=bonus_id,
                url=url, title=title, external_id=eid)
            seen.add(eid)
            results.append({'external_id': eid, 'status': 'recorded', 'qtype': qtype})
        else:
            _, created = DiscordThread.objects.get_or_create(
                question_set=qset, tossup_id=tossup_id, bonus_id=bonus_id, url=url,
                defaults={'title': title})
            results.append({'external_id': eid,
                            'status': 'recorded' if created else 'duplicate',
                            'qtype': qtype})

    return _api_json({'ok': True, 'results': results})
