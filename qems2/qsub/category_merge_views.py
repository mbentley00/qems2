"""The page for merging a sub-category into one of its parents. The work is
in category_merge; this is the request handling."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpResponseRedirect
from django.shortcuts import render, get_object_or_404

from qems2.qsub import category_merge as cm
from qems2.qsub import distribution_lines as dl
from qems2.qsub.models import QuestionSet


@login_required
def merge_category(request, qset_id):
    qset = get_object_or_404(QuestionSet, id=qset_id)
    writer = request.user.writer
    if not (qset.is_owner(writer) or qset.editor.filter(id=writer.id).exists()):
        return render(request, 'failure.html', {'message': 'Only the set\'s owners and editors can merge categories.',
                                                'message_class': 'alert-box alert'})
    raw = request.POST.get('path') if request.method == 'POST' else request.GET.get('path')
    source = dl.path_parts(raw)
    back = request.POST.get('next') or request.GET.get('next') or '/category_overview/{0}/'.format(qset.id)
    if not back.startswith('/'):
        back = '/category_overview/{0}/'.format(qset.id)
    targets = cm.merge_targets(qset, source) if len(source) > 1 else []
    if not targets or not cm.branch(qset, source):
        messages.error(request, 'Pick a sub-category to merge.')
        return HttpResponseRedirect(back)

    if request.method == 'POST':
        target = dl.path_parts(request.POST.get('target'))
        if tuple(p.lower() for p in target) not in [tuple(p.lower() for p in t) for t in targets]:
            messages.error(request, 'Pick one of its parents to merge it into.')
        elif request.POST.get('confirm') != '1':
            messages.error(request, 'Nothing was merged. Tick the box to confirm.')
        else:
            done = cm.merge(qset, source, target)
            cache.clear()
            messages.success(request, '{0} merged into {1}: {2} question{3} moved{4}.'.format(
                ' - '.join(source), done['target'], done['questions'], '' if done['questions'] == 1 else 's',
                ', {0} tag{1} moved'.format(done['tags_moved'] + done['tags_merged'],
                                            '' if done['tags_moved'] + done['tags_merged'] == 1 else 's')
                if done['tags_moved'] + done['tags_merged'] else ''))
            return HttpResponseRedirect(back)

    return render(request, 'merge_category.html', {
        'qset': qset, 'user': writer, 'source': ' - '.join(source), 'leaf': source[-1],
        'targets': [' - '.join(t) for t in targets], 'plan': cm.plan(qset, source), 'next': back,
    })
