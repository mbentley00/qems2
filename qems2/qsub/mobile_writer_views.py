"""The phone writer: /m/write/. A single page that goes from screenshots of
highlighted research to a saved question, built for a phone. Site admins only
while it is being tried out (the same gate as the other AI features).

The page keeps the work in progress itself and calls the JSON endpoints here
for each step; nothing is written to the set until Save. The thinking is in
mobile_writer.
"""

import base64
import json
import re

from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import JsonResponse, Http404
from django.shortcuts import render, get_object_or_404

from . import ai
from . import mobile_writer as mw
from .models import (QuestionSet, DistributionEntry, SetWideDistributionEntry, Tossup, Bonus,
                     QuestionType)
from .utils import ACF_STYLE_TOSSUP, ACF_STYLE_BONUS, QUESTION_CREATE, strip_markup


_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
_MAX_IMAGES = 6
_MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _allowed(user):
    return user.is_authenticated and user.is_superuser


def _writable_sets(writer):
    sets = (QuestionSet.objects.filter(owner=writer)
            | QuestionSet.objects.filter(co_owners=writer)
            | QuestionSet.objects.filter(editor=writer)
            | QuestionSet.objects.filter(writer=writer))
    return sets.exclude(archived=True).distinct()


def _set_for(request, qset_id):
    """The set, if this admin may add questions to it; 404 otherwise, so the
    page gives away nothing to anyone else."""
    if not _allowed(request.user):
        raise Http404
    qset = get_object_or_404(QuestionSet, id=qset_id)
    if not _writable_sets(request.user.writer).filter(id=qset.id).exists():
        raise Http404
    return qset


def _body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except ValueError:
        return {}


def _error(message, status=400):
    return JsonResponse({'ok': False, 'error': message}, status=status)


def _limit(qset, kind):
    return qset.max_acf_tossup_length if kind == 'tossup' else qset.max_acf_bonus_length


def _categories(qset):
    """Every category in the set's distribution, with how many questions it
    has and how many the set calls for."""
    entries = list(DistributionEntry.objects.filter(distribution_id=qset.distribution_id)
                   .order_by('category', 'subcategory'))
    wanted = {e.dist_entry_id: e for e in
              SetWideDistributionEntry.objects.filter(question_set=qset)}
    tu = dict(Tossup.objects.filter(question_set=qset).values('category')
              .annotate(n=Count('id')).values_list('category', 'n'))
    bs = dict(Bonus.objects.filter(question_set=qset).values('category')
              .annotate(n=Count('id')).values_list('category', 'n'))
    out = []
    for e in entries:
        want = wanted.get(e.id)
        out.append({'id': e.id, 'label': str(e),
                    'tossups': tu.get(e.id, 0), 'bonuses': bs.get(e.id, 0),
                    'tossups_wanted': want.num_tossups if want else None,
                    'bonuses_wanted': want.num_bonuses if want else None})
    return out


@login_required
def writer_home(request):
    """Pick a set to write for."""
    if not _allowed(request.user):
        raise Http404
    sets = _writable_sets(request.user.writer).order_by('-date')
    return render(request, 'mobile_writer_home.html', {'sets': sets})


@login_required
def writer_page(request, qset_id):
    qset = _set_for(request, qset_id)
    config = {
        'qset': {'id': qset.id, 'name': qset.name, 'tossups_only': qset.tossups_only},
        'limits': {'tossup': qset.max_acf_tossup_length, 'bonus': qset.max_acf_bonus_length},
        'categories': _categories(qset),
        'ai': ai.ai_enabled(),
    }
    return render(request, 'mobile_writer.html', {'qset': qset, 'config': config})


def _post_only(view):
    def wrapped(request, qset_id):
        if request.method != 'POST':
            return _error('POST only.', 405)
        qset = _set_for(request, qset_id)
        try:
            return view(request, qset)
        except mw.WriterError as ex:
            return _error(str(ex), 502)
    wrapped.__name__ = view.__name__
    return login_required(wrapped)


@_post_only
def writer_extract(request, qset):
    """Screenshots (multipart `images`) and pasted `text` -> topic, sources,
    clues."""
    files = request.FILES.getlist('images')[:_MAX_IMAGES]
    text = request.POST.get('text', '')
    if not files and not text.strip():
        return _error('Add a screenshot or paste some text first.')
    images = []
    for f in files:
        if f.content_type not in _IMAGE_TYPES:
            return _error('{0} is not an image Claude can read (use JPEG or PNG).'.format(f.name))
        if f.size > _MAX_IMAGE_BYTES:
            return _error('{0} is too large (5 MB at most).'.format(f.name))
        images.append((f.content_type, base64.standard_b64encode(f.read()).decode('ascii')))
    result = mw.extract_clues(images, text)
    return JsonResponse(dict(result, ok=True))


@_post_only
def writer_plan(request, qset):
    data = _body(request)
    kind = 'bonus' if data.get('kind') == 'bonus' else 'tossup'
    clues = [c for c in data.get('clues', []) if isinstance(c, str) and c.strip()]
    if not clues:
        return _error('Pick at least one clue.')
    labels = [c['label'] for c in _categories(qset)]
    angles = mw.plan_angles(kind, data.get('topic', ''), clues, labels)
    by_label = {c['label']: c['id'] for c in _categories(qset)}
    for a in angles:
        a['category_id'] = by_label.get(a['category'])
    return JsonResponse({'ok': True, 'angles': angles})


def _plain(text):
    """Text to show or search on: no HTML and no QEMS _underline_/~title~
    markup."""
    return re.sub(r'[_~]', '', strip_markup(text or ''))


def _plain_answer(answer_line):
    """The answer itself, for searching: no markup, no [accept ...] brackets."""
    return ' '.join(re.sub(r'\[[^\]]*\]?|\([^)]*\)', ' ', _plain(answer_line)).split())


def _set_hits(qset, query, exclude=()):
    from .views import fulltext_filter
    hits = []
    for model, kind in ((Tossup, 'Tossup'), (Bonus, 'Bonus')):
        found = fulltext_filter(model.objects.filter(question_set=qset), query)
        for q in found.select_related('category', 'packet')[:6]:
            if (kind, q.id) in exclude:
                continue
            if kind == 'Tossup':
                text, answer = q.tossup_text, q.tossup_answer
            else:
                text = ' '.join(filter(None, [q.leadin, q.part1_text, q.part2_text, q.part3_text]))
                answer = ' / '.join(filter(None, [q.part1_answer, q.part2_answer, q.part3_answer]))
            hits.append({'kind': kind, 'id': q.id,
                         'where': ' · '.join(filter(None, [q.packet.packet_name if q.packet else '',
                                                           str(q.category) if q.category else ''])),
                         'text': _plain(text)[:280], 'answer': _plain(answer),
                         'url': '/edit_{0}/{1}/'.format(kind.lower(), q.id)})
    return hits


@_post_only
def writer_related(request, qset):
    """What this set and QB Reader already have on the answer, and how often
    each clue has come up before."""
    data = _body(request)
    answer = _plain_answer(data.get('answer', '')) or data.get('topic', '')
    if not answer:
        return _error('No answer to look for yet.')
    out = {'ok': True, 'answer': answer, 'set_hits': _set_hits(qset, answer)}
    try:
        out['qbreader'] = mw.qbreader_search(answer, data.get('terms', []))
    except mw.WriterError as ex:
        out['qbreader'] = {'error': str(ex)}
    return JsonResponse(out)


def _question(data, kind):
    """The question the page sent, held to the expected shape."""
    q = data.get('question') or {}
    return mw._clean_question(kind, q)


def _sources(data, topic=''):
    """The writer's own sources, plus Wikipedia's article on the topic."""
    sources = [{'label': str(s.get('label', ''))[:200], 'text': str(s.get('text', ''))[:60000]}
               for s in data.get('sources', []) if isinstance(s, dict) and s.get('text')][:8]
    wiki = mw.wikipedia_source(topic) if topic else None
    if wiki:
        sources.append(wiki)
    return sources


@_post_only
def writer_draft(request, qset):
    data = _body(request)
    kind = 'bonus' if data.get('kind') == 'bonus' else 'tossup'
    answer = (data.get('answer') or '').strip()
    clues = [c for c in data.get('clues', []) if isinstance(c, str) and c.strip()]
    if not answer:
        return _error('Give the answer line first.')
    question = mw.draft_question(kind, answer, clues, _sources(data), _limit(qset, kind),
                                 data.get('notes', ''))
    return JsonResponse({'ok': True, 'question': question})


@_post_only
def writer_revise(request, qset):
    data = _body(request)
    kind = 'bonus' if data.get('kind') == 'bonus' else 'tossup'
    instruction = (data.get('instruction') or '').strip()
    if not instruction:
        return _error('Say what to change.')
    question, summary = mw.revise_question(
        kind, _question(data, kind), instruction, data.get('target'),
        _sources(data), _limit(qset, kind))
    return JsonResponse({'ok': True, 'question': question, 'summary': summary})


@_post_only
def writer_lookup(request, qset):
    data = _body(request)
    topic = (data.get('topic') or '').strip()
    sources = _sources(data, topic)
    result = mw.lookup(topic, (data.get('focus') or '').strip(), sources,
                       (data.get('ask') or '').strip(),
                       [u for u in data.get('used', []) if isinstance(u, str)])
    return JsonResponse(dict(result, ok=True))


def _build(qset, writer, kind, q, category_id):
    """An unsaved Tossup or Bonus from the page's question."""
    category = DistributionEntry.objects.filter(
        id=category_id, distribution_id=qset.distribution_id).first() if category_id else None
    if kind == 'tossup':
        obj = Tossup(question_set=qset, author=writer, category=category, locked=False,
                     edited=False, tossup_text=mw.question_text('tossup', q),
                     tossup_answer=q['answer'])
        obj.question_type = QuestionType.objects.filter(question_type=ACF_STYLE_TOSSUP).first()
        return obj
    parts = q['parts']
    obj = Bonus(question_set=qset, author=writer, category=category, locked=False, edited=False,
                leadin=q['leadin'],
                part1_text=parts[0]['text'], part1_answer=parts[0]['answer'],
                part2_text=parts[1]['text'], part2_answer=parts[1]['answer'],
                part3_text=parts[2]['text'], part3_answer=parts[2]['answer'],
                part1_difficulty=parts[0]['difficulty'], part2_difficulty=parts[1]['difficulty'],
                part3_difficulty=parts[2]['difficulty'])
    obj.question_type = QuestionType.objects.filter(question_type=ACF_STYLE_BONUS).first()
    return obj


@_post_only
def writer_check(request, qset):
    """Length, the set's style rules and the repeat check, before saving."""
    from .views import _attach_preview_checks
    data = _body(request)
    kind = 'bonus' if data.get('kind') == 'bonus' else 'tossup'
    obj = _build(qset, request.user.writer, kind, _question(data, kind), data.get('category_id'))
    _attach_preview_checks(qset, [obj] if kind == 'tossup' else [],
                           [obj] if kind == 'bonus' else [])
    return JsonResponse({
        'ok': True, 'chars': obj.character_count(), 'limit': _limit(qset, kind),
        'style': [{'severity': i['severity'], 'message': i['message']} for i in obj.style_issues],
        'repeats': [{'answer': _plain(m['answer_raw']), 'where': m.get('category_str', ''),
                     'packet': m.get('packet', ''),
                     'url': '/edit_{0}/{1}/'.format(m['type'], m['id'])}
                    for m in obj.dup_matches],
    })


@_post_only
def writer_save(request, qset):
    data = _body(request)
    kind = 'bonus' if data.get('kind') == 'bonus' else 'tossup'
    if kind == 'bonus' and qset.tossups_only:
        return _error('This set is tossup-only.')
    q = _question(data, kind)
    if kind == 'tossup' and not (q['sentences'] and q['answer']):
        return _error('The tossup needs text and an answer.')
    if kind == 'bonus' and not all(p['text'] and p['answer'] for p in q['parts']):
        return _error('Every bonus part needs text and an answer.')
    writer = request.user.writer
    obj = _build(qset, writer, kind, q, data.get('category_id'))
    if obj.category is None:
        return _error('Pick a category from this set.')
    obj.save_question(edit_type=QUESTION_CREATE, changer=writer)
    return JsonResponse({'ok': True, 'id': obj.id, 'url': '/edit_{0}/{1}/'.format(kind, obj.id)})
