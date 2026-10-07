"""The News page, and dismissing the news bar. Administrators post and remove
announcements on the page; opening it marks everything on it as read."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import render

from qems2.qsub import news as site_news
from qems2.qsub.models import Announcement


@login_required
def news(request):
    writer = request.user.writer
    is_admin = request.user.is_superuser

    if request.method == 'POST':
        if not is_admin:
            messages.error(request, 'Only an administrator can post news.')
            return HttpResponseRedirect('/news/')
        if request.POST.get('action') == 'delete':
            Announcement.objects.filter(id=request.POST.get('id') or 0).delete()
            messages.success(request, 'Announcement removed.')
            return HttpResponseRedirect('/news/')
        title = (request.POST.get('title') or '').strip()[:200]
        body = (request.POST.get('body') or '').strip()
        if not title:
            messages.error(request, 'An announcement needs a title.')
            return render(request, 'news.html', _context(writer, is_admin, title, body, unread_ids=set()))
        Announcement.objects.create(title=title, body=body, created_by=writer)
        messages.success(request, 'Posted. Everyone sees it once, on their next visit.')
        return HttpResponseRedirect('/news/')

    # What was new to them is marked as such on the page they read it on.
    unread_ids = set(site_news.unread(writer).values_list('id', flat=True))
    if unread_ids:
        site_news.mark_seen(writer)
    return render(request, 'news.html', _context(writer, is_admin, '', '', unread_ids))


def _context(writer, is_admin, title, body, unread_ids):
    return {'user': writer, 'is_admin': is_admin, 'unread_ids': unread_ids,
            'announcements': Announcement.objects.select_related('created_by__user')[:50],
            'form_title': title, 'form_body': body}


@login_required
def dismiss_news(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    site_news.mark_seen(request.user.writer)
    return JsonResponse({'ok': True})
