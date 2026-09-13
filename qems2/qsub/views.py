import json
import urllib.parse
import csv
import html
import io
import math
import random
import re
import zipfile
import unicodecsv
import time
import datetime
import sys
import unicodedata
from collections import defaultdict

from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment

from django.shortcuts import render, get_object_or_404
from django.forms.formsets import formset_factory
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse

from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import *
from .forms import *
from .model_utils import *
from .utils import *
from .packet_parser import parse_packet_data
from . import answer_structure
from . import category_mapper
from .duplicate_checker import (find_duplicates, find_internal_issues,
                                find_topic_repeats, find_answer_matches,
                                build_answer_index, lookup_answer_matches,
                                normalize_answer, CRITICAL, WARNING, INFO)
from django.utils.safestring import mark_safe
from django_comments.models import Comment
from django.db.models import Q, Max, Prefetch, Count
from django.db import connection


def _next_question_number(model, packet_id):
    """The next free question number in a packet: one past the current highest.

    Using max+1 (not count+1) avoids colliding with an existing tiebreaker when
    a middle question was unassigned — count+1 would reuse a number already held
    by the tiebreaker, and the packet grid (keyed by number) would then hide one
    of the two."""
    current = (model.objects.filter(packet_id=packet_id)
               .aggregate(m=Max('question_number'))['m'] or 0)
    return current + 1
from django.utils import timezone
from django.core.cache import cache


def fulltext_filter(queryset, query):
    """Full-text match against the maintained search_question_content /
    search_question_answers fields. Uses Postgres full-text search in
    production and falls back to icontains on SQLite (local dev)."""
    if not query:
        return queryset.none()
    if connection.vendor == 'postgresql':
        from django.contrib.postgres.search import SearchVector, SearchQuery
        vector = SearchVector('search_question_content', 'search_question_answers')
        return queryset.annotate(qsearch=vector).filter(qsearch=SearchQuery(query, search_type='websearch'))
    return queryset.filter(
        Q(search_question_content__icontains=query) | Q(search_question_answers__icontains=query))

from django.contrib.contenttypes.models import ContentType


def main (request):
    # Logged-out visitors get the public splash page; members get their
    # question-set dashboard.
    if not request.user.is_authenticated:
        return render(request, 'splash.html', {})
    return question_sets(request)

@login_required
def about (request):
    return render(request, 'about.html', {'user': request.user.writer})

@login_required
def help_page (request):
    return render(request, 'help.html', {'user': request.user.writer})

@login_required
def sidebar (request):
    writer = request.user.writer
    # the tournaments for which this user is a writer
    writer_sets = writer.question_set_writer.all()
    # all the tournaments owned by this user
    owned_sets = QuestionSet.objects.filter(owner=writer)
    # the tournaments for which this user is an editor
    editor_sets = writer.question_set_editor.all()

    all_sets = editor_sets
    print('All sets object:')
    print(all_sets)
        
    return render(request, 'sidebar.html', {'question_sets': all_sets, 'user': writer})

@login_required
def question_sets (request):
    writer = request.user.writer

    # all the tournaments owned by this user
    owned_sets = QuestionSet.objects.filter(owner=writer)
    # the tournaments for which this user is an editor
    editor_sets = writer.question_set_editor.all()
    
    all_sets = owned_sets | editor_sets | writer.question_set_writer.all()
    all_sets = all_sets.order_by('date')
    
    # Sets that are upcoming or finished within the last month
    upcoming_sets = {}

    # Sets that are in the past
    completed_sets = {}

    # Keep recently-finished sets in "upcoming" for a month after the
    # tournament date before moving them to "completed".
    from datetime import timedelta
    cutoff = datetime.now().date() - timedelta(days=30)

    # Sets put away on purpose, whatever their date.
    archived_sets = {}

    for qset in (all_sets):
        if qset.archived:
            archived_sets[qset.id] = qset
        elif (qset.date >= cutoff):
            upcoming_sets[qset.id] = qset
        else:
            completed_sets[qset.id] = qset
            
    upcoming_sets = upcoming_sets
    completed_sets = completed_sets
            
    upcoming_sets = upcoming_sets.values()
    completed_sets = completed_sets.values()
    
    upcoming_sets = sorted(upcoming_sets, key=lambda qset: qset.date)
    completed_sets = sorted(completed_sets, key=lambda qset: qset.date)
    # By name: an archive is looked through by what a set is called, not by
    # when its tournament was.
    archived_sets = sorted(archived_sets.values(), key=lambda qset: qset.name.lower())

    all_sets  = [{'header': 'Upcoming question sets', 'qsets': upcoming_sets, 'id': 'qsets-write'},
                 {'header': 'Completed question sets', 'qsets': completed_sets, 'id': 'qsets-complete'}]
    if archived_sets:
        all_sets.append({'header': 'Archived question sets', 'qsets': archived_sets,
                         'id': 'qsets-archived'})

    # Public sets the user isn't already part of — they can request to join.
    my_set_ids = {qset.id for qset in all_sets[0]['qsets']} | {qset.id for qset in all_sets[1]['qsets']}
    public_sets = [qs for qs in QuestionSet.objects.filter(
                       public=True, approval_status=QuestionSet.APPROVAL_APPROVED).order_by('-date')
                   if qs.id not in my_set_ids]

    # Administrators get a count of sets waiting on them, so approval isn't
    # something you only hear about by e-mail.
    pending_set_count = (QuestionSet.objects.filter(
        approval_status=QuestionSet.APPROVAL_PENDING).count()
        if request.user.is_superuser else 0)

    return render(request, 'question_sets.html',
                  {'question_set_list': all_sets, 'public_sets': public_sets, 'user': writer,
                   'pending_set_count': pending_set_count})

@login_required
def request_to_join(request):
    """A logged-in user asks to join a public question set; emails the owner(s)."""
    user = request.user.writer
    if request.method != 'POST':
        return HttpResponse(json.dumps({'success': False, 'message': 'Invalid request'}))
    try:
        qset = QuestionSet.objects.get(id=int(request.POST['qset_id']))
    except (KeyError, ValueError, QuestionSet.DoesNotExist):
        return HttpResponse(json.dumps({'success': False, 'message': 'Set not found.'}))
    if not qset.public or not qset.visible_to_strangers:
        return HttpResponse(json.dumps({'success': False, 'message': 'This set is not public.'}))
    if _is_set_member(user, qset):
        return HttpResponse(json.dumps({'success': False, 'message': 'You are already part of this set.'}))

    note = (request.POST.get('message') or '').strip()[:1000]
    _record_join_request(qset, user, note, via_link=False)
    sent = _notify_set_join_request(qset, user, note, via_link=False)
    return HttpResponse(json.dumps({'success': True,
        'message': ('Your request has been emailed to the set owner.' if sent else
                    'Your request is pending. (The owner has no email on file, but they '
                    'will see it on the set page.)')}))


def _record_join_request(qset, requester, note, via_link):
    """Persist a pending access request so an owner can act on it from the set
    page. Re-requesting refreshes the note rather than creating a duplicate."""
    req, created = SetJoinRequest.objects.get_or_create(
        question_set=qset, requester=requester,
        defaults={'message': note, 'via_link': via_link})
    if not created and note and req.message != note:
        req.message = note
        req.save(update_fields=['message'])
    return req


def _notify_set_join_request(qset, requester, note, via_link):
    """Email the set's owners that someone wants access, with a direct approve
    link. Returns False if no owner has an email on file (the request is still
    recorded and visible on the set page)."""
    from django.core.mail import EmailMessage
    from django.conf import settings as dj_settings
    requester_name = _actor_name(requester)
    requester_email = requester.user.email or ''
    recipients = [o.user.email for o in qset.all_owners() if o.user and o.user.email]
    if not recipients:
        return False

    base = dj_settings.BASE_URL.rstrip('/')
    approve_url = '{0}/approve_join/{1}/{2}/'.format(base, qset.id, requester.id)
    # A publicly listed set draws requests from people the owner has never heard
    # of, and saying no was the one answer the mail had no link for -- it meant
    # finding the set and its Writers and Editors tab. Both decisions get a
    # link; neither acts on being followed, since mail clients and link
    # scanners fetch what they are sent.
    decline_url = '{0}/decline_join/{1}/{2}/'.format(base, qset.id, requester.id)
    subject = 'QEMS3: {0} requests to join "{1}"'.format(requester_name, qset.name)
    body = ('{0} (@{1}{2}) has requested to join your question set "{3}" on QEMS3{4}.\n\n'
            '{5}\n\n'
            'Approve this request (add them as a writer or editor):\n{6}\n\n'
            'Decline it:\n{7}\n\n'
            'You can also open the set on QEMS3 and review pending requests on the '
            '"Writers and Editors" tab:\n{8}/edit_question_set/{9}/').format(
        requester_name, requester.user.username,
        ', ' + requester_email if requester_email else '', qset.name,
        ' using your join link' if via_link else '',
        ('Their message: ' + note) if note else '(No message included.)',
        approve_url, decline_url, base, qset.id)
    try:
        EmailMessage(subject, body, dj_settings.DEFAULT_FROM_EMAIL, recipients,
                     reply_to=[requester_email] if requester_email else None).send(fail_silently=False)
    except Exception as ex:
        print('Could not send join-request mail:', ex)
        return False
    return True


@login_required
def manage_join_link(request, qset_id):
    """Owner-only: create, regenerate, disable, re-enable, or delete the set's
    shareable join link. Always a POST that redirects back to the set page."""
    user = request.user.writer
    try:
        qset = QuestionSet.objects.get(id=int(qset_id))
    except (ValueError, QuestionSet.DoesNotExist):
        return render(request, 'failure.html',
                      {'message': 'That set no longer exists.',
                       'message_class': 'alert-box alert'})
    if not qset.is_owner(user):
        return render(request, 'failure.html',
                      {'message': 'Only an owner of this set can manage its join link.',
                       'message_class': 'alert-box alert'})

    back = '/edit_question_set/{0}/#editors'.format(qset.id)
    if request.method != 'POST':
        return HttpResponseRedirect(back)

    action = request.POST.get('action', '')
    link = SetJoinLink.objects.filter(question_set=qset).first()
    role = 'editor' if request.POST.get('default_role') == 'editor' else 'writer'

    if action == 'create':
        if link is None:
            SetJoinLink.objects.create(question_set=qset, token=SetJoinLink.new_token(),
                                       default_role=role, created_by=user)
            messages.success(request, 'Join link created. Anyone with the link can request '
                                      'access, which you still have to approve.')
        else:
            link.active = True
            link.default_role = role
            link.save()
            messages.success(request, 'Join link is active.')
    elif link is None:
        messages.error(request, 'This set has no join link yet.')
    elif action == 'regenerate':
        link.token = SetJoinLink.new_token()
        link.active = True
        link.save()
        messages.success(request, 'A new join link was generated. The old link no longer works.')
    elif action == 'disable':
        link.active = False
        link.save()
        messages.success(request, 'Join link disabled. It can be turned back on at any time.')
    elif action == 'enable':
        link.active = True
        link.save()
        messages.success(request, 'Join link enabled.')
    elif action == 'delete':
        link.delete()
        messages.success(request, 'Join link deleted.')
    elif action == 'set_role':
        link.default_role = role
        link.save()
        messages.success(request, 'Join link updated.')

    cache.clear()
    return HttpResponseRedirect(back)


@login_required
def join_set(request, token):
    """The page a join link opens. Shows the set and lets a logged-in user ask
    for access; the request always waits on an owner's approval — following the
    link never grants a role."""
    user = request.user.writer
    link = SetJoinLink.objects.filter(token=token).select_related('question_set').first()
    if link is None or not link.active:
        return render(request, 'join_set.html', {'user': user, 'invalid': True})

    qset = link.question_set
    ctx = {'user': user, 'qset': qset, 'link': link,
           'already': _is_set_member(user, qset)}

    if ctx['already']:
        SetJoinRequest.objects.filter(question_set=qset, requester=user).delete()
        return render(request, 'join_set.html', ctx)

    # A set still waiting on its own approval can't recruit anyone. The link
    # isn't broken and doesn't need reissuing — it starts working the moment an
    # administrator approves the set.
    if not qset.visible_to_strangers:
        ctx['unavailable'] = True
        return render(request, 'join_set.html', ctx)

    existing = SetJoinRequest.objects.filter(question_set=qset, requester=user).first()

    if request.method == 'POST':
        note = (request.POST.get('message') or '').strip()[:1000]
        _record_join_request(qset, user, note, via_link=True)
        sent = _notify_set_join_request(qset, user, note, via_link=True)
        ctx.update({'requested': True, 'emailed': sent})
        return render(request, 'join_set.html', ctx)

    ctx['pending'] = existing is not None
    return render(request, 'join_set.html', ctx)


@login_required
def resolve_join_request(request, qset_id):
    """Owner-only approve/decline of a pending access request, from the set's
    "Writers and Editors" tab."""
    user = request.user.writer
    try:
        qset = QuestionSet.objects.get(id=int(qset_id))
    except (ValueError, QuestionSet.DoesNotExist):
        return render(request, 'failure.html',
                      {'message': 'That set no longer exists.',
                       'message_class': 'alert-box alert'})
    if not qset.is_owner(user):
        return render(request, 'failure.html',
                      {'message': 'Only an owner of this set can approve join requests.',
                       'message_class': 'alert-box alert'})

    back = '/edit_question_set/{0}/#editors'.format(qset.id)
    if request.method != 'POST':
        return HttpResponseRedirect(back)

    try:
        requester = Writer.objects.get(id=int(request.POST.get('writer_id', '')))
    except (ValueError, Writer.DoesNotExist):
        messages.error(request, 'That user no longer exists.')
        return HttpResponseRedirect(back)

    # The set page renders messages with autoescape off, so escape the name here.
    from django.utils.html import escape
    name = escape(_actor_name(requester))
    action = request.POST.get('action', '')
    if action == 'decline':
        SetJoinRequest.objects.filter(question_set=qset, requester=requester).delete()
        messages.success(request, 'Request from {0} declined.'.format(name))
    elif action == 'approve':
        if _is_set_member(requester, qset):
            SetJoinRequest.objects.filter(question_set=qset, requester=requester).delete()
            messages.error(request, '{0} is already part of this set.'.format(name))
        else:
            role = 'editor' if request.POST.get('role') == 'editor' else 'writer'
            _grant_set_role(qset, requester, role, user)
            messages.success(request, '{0} was added as {1} {2}.'.format(
                name, 'an' if role == 'editor' else 'a', role))
    return HttpResponseRedirect(back)


def _join_request_parties(user, qset_id, writer_id):
    """(qset, requester, error) for the approve and decline links in a join
    request email. Both are owner-only, and either can be followed long after
    the request itself is gone, so the checks are the same for both."""
    try:
        qset = QuestionSet.objects.get(id=int(qset_id))
        requester = Writer.objects.get(id=int(writer_id))
    except (ValueError, QuestionSet.DoesNotExist, Writer.DoesNotExist):
        return None, None, {'message': 'That set or user no longer exists.',
                            'message_class': 'alert-box alert'}
    if not qset.is_owner(user):
        return None, None, {'message': 'Only an owner of this set can act on join requests.',
                            'message_class': 'alert-box alert'}
    return qset, requester, None


@login_required
def approve_join(request, qset_id, writer_id):
    """Owner-facing approval of a join request, linked directly from the request
    email. Shows a small confirmation page; on POST adds the requester to the
    set as a writer (default) or editor and emails them that they were added."""
    user = request.user.writer
    qset, requester, error = _join_request_parties(user, qset_id, writer_id)
    if error is not None:
        return render(request, 'failure.html', error)

    already = _is_set_member(requester, qset)

    if request.method == 'POST' and not already:
        role = 'editor' if request.POST.get('role') == 'editor' else 'writer'
        _grant_set_role(qset, requester, role, user)
        return render(request, 'approve_join.html',
                      {'qset': qset, 'requester': requester, 'added_role': role, 'user': user})

    if already:
        SetJoinRequest.objects.filter(question_set=qset, requester=requester).delete()

    link = SetJoinLink.objects.filter(question_set=qset).first()
    return render(request, 'approve_join.html',
                  {'qset': qset, 'requester': requester, 'already': already, 'user': user,
                   'default_role': link.default_role if link else 'writer'})


@login_required
def decline_join(request, qset_id, writer_id):
    """Owner-facing decline of a join request, the other link in the request
    email. GET asks; POST drops the pending request.

    The requester isn't emailed about it -- the same as declining from the set
    page -- so a no stays between the owner and the set, and nothing stops the
    requester asking again later.
    """
    user = request.user.writer
    qset, requester, error = _join_request_parties(user, qset_id, writer_id)
    if error is not None:
        return render(request, 'failure.html', error)

    already = _is_set_member(requester, qset)
    pending = SetJoinRequest.objects.filter(question_set=qset, requester=requester)

    if request.method == 'POST' and not already:
        pending.delete()
        return render(request, 'approve_join.html',
                      {'qset': qset, 'requester': requester, 'declined': True, 'user': user})

    # Someone already on the set has no request left to answer either way.
    if already:
        pending.delete()

    return render(request, 'approve_join.html',
                  {'qset': qset, 'requester': requester, 'already': already,
                   'declining': True, 'user': user})


def _grant_set_role(qset, requester, role, by_writer):
    """Add a writer to a set as editor or writer, clear any pending join request,
    and email them. Shared by every approval path."""
    if role == 'editor':
        qset.editor.add(requester)
        GroupRoleGrant.objects.filter(
            question_set=qset, writer=requester, role='editor').delete()
        if qset.writer.filter(id=requester.id).exists():
            qset.writer.remove(requester)
    else:
        qset.writer.add(requester)
        GroupRoleGrant.objects.filter(
            question_set=qset, writer=requester, role='writer').delete()
    qset.save()
    SetJoinRequest.objects.filter(question_set=qset, requester=requester).delete()
    _notify_added_to_set(requester, qset, role, by_writer)
    cache.clear()


@login_required
def approve_group_join(request, group_id, writer_id):
    """Manager-facing approval of a role-group join request, linked directly from
    the request email. Shows a confirmation page; on POST adds the requester as a
    member, clears the pending request, and emails them."""
    user = request.user.writer
    try:
        group = RoleGroup.objects.get(id=int(group_id))
        requester = Writer.objects.get(id=int(writer_id))
    except (ValueError, RoleGroup.DoesNotExist, Writer.DoesNotExist):
        return render(request, 'failure.html',
                      {'message': 'That group or user no longer exists.',
                       'message_class': 'alert-box alert'})

    if not group.can_manage(user):
        return render(request, 'failure.html',
                      {'message': 'Only the group\'s creator can approve join requests.',
                       'message_class': 'alert-box alert'})

    already = group.members.filter(id=requester.id).exists()

    if request.method == 'POST' and not already:
        group.members.add(requester)
        RoleGroupJoinRequest.objects.filter(role_group=group, requester=requester).delete()
        reconcile_group(group)
        _notify_added_to_group(requester, group, user)
        cache.clear()
        return render(request, 'approve_group_join.html',
                      {'group': group, 'requester': requester, 'added': True, 'user': user})

    if already:
        RoleGroupJoinRequest.objects.filter(role_group=group, requester=requester).delete()

    return render(request, 'approve_group_join.html',
                  {'group': group, 'requester': requester, 'already': already, 'user': user})


def _writer_email(writer):
    return writer.user.email if (writer and writer.user and writer.user.email) else ''


def _actor_name(writer):
    if writer is None:
        return 'An organizer'
    return writer.get_real_name().strip() or writer.user.username


def _notify_added_to_set(writer, qset, role, by_writer):
    """Email a writer that they were added to a set (editor/writer/co-owner)."""
    email = _writer_email(writer)
    if not email:
        return
    from .signals import _send_mail_async
    from django.conf import settings as dj_settings
    role_label = {'editor': 'an editor', 'writer': 'a writer', 'co-owner': 'a co-owner'}.get(role, role)
    subject = 'QEMS3: you were added to "{0}"'.format(qset.name)
    body = ('{0} added you as {1} on the QEMS3 question set "{2}".\n\n'
            '{3}/edit_question_set/{4}/').format(
        _actor_name(by_writer), role_label, qset.name, dj_settings.BASE_URL, qset.id)
    _send_mail_async(subject, body, [email])


def _notify_added_to_group(writer, group, by_writer):
    """Email a writer that they were added to a role group."""
    email = _writer_email(writer)
    if not email:
        return
    from .signals import _send_mail_async
    from django.conf import settings as dj_settings
    subject = 'QEMS3: you were added to the role group "{0}"'.format(group.name)
    body = ('{0} added you to the QEMS3 role group "{1}". Members of a role group '
            'automatically get its role on every question set the group is attached to.\n\n'
            '{2}/role_groups/').format(_actor_name(by_writer), group.name, dj_settings.BASE_URL)
    _send_mail_async(subject, body, [email])


def _notify_group_join_request(group, requester):
    """Email the group owner that someone has requested to join, with a direct
    approve link. Returns False if the owner has no email on file."""
    email = _writer_email(group.created_by)
    if not email:
        return False
    from .signals import _send_mail_async
    from django.conf import settings as dj_settings
    rname = _actor_name(requester)
    remail = requester.user.email or ''
    approve_url = '{0}/approve_group_join/{1}/{2}/'.format(
        dj_settings.BASE_URL.rstrip('/'), group.id, requester.id)
    subject = 'QEMS3: {0} requests to join role group "{1}"'.format(rname, group.name)
    body = ('{0} (@{1}{2}) has requested to join your QEMS3 role group "{3}".\n\n'
            'Approve this request (adds them to the group):\n{4}\n\n'
            'Or open Role Groups to review pending requests:\n{5}/role_groups/').format(
        rname, requester.user.username, ', ' + remail if remail else '',
        group.name, approve_url, dj_settings.BASE_URL)
    _send_mail_async(subject, body, [email])
    return True


def _new_set_approval_recipients():
    """Who reviews sets made by brand-new accounts. Blank setting = nobody, and
    the review is skipped rather than leaving sets pending forever."""
    from django.conf import settings as dj_settings
    raw = getattr(dj_settings, 'SET_APPROVAL_EMAIL', '') or ''
    return [addr.strip() for addr in raw.split(',') if addr.strip()]


def _notify_new_set_pending(qset):
    """Email the administrator that an account too new to create a set has made
    one anyway, provisionally, with a direct link to approve or decline it.
    Returns False when no approval address is configured."""
    recipients = _new_set_approval_recipients()
    if not recipients:
        return False
    from .signals import _send_mail_async
    from django.conf import settings as dj_settings
    owner = qset.owner
    base = dj_settings.BASE_URL.rstrip('/')
    joined = owner.user.date_joined.strftime('%Y-%m-%d %H:%M UTC') if owner.user else 'unknown'
    subject = 'QEMS3: new account created "{0}" — approve?'.format(qset.name)
    body = ('{0} (@{1}{2}) signed up on {3} and has created the question set "{4}".\n\n'
            'The account is younger than the two-day wait, so the set is provisional: '
            'they can work in it, but it stays out of the public set list and its join '
            'links are dead until you approve it.\n\n'
            'Review it:\n{5}/approve_new_set/{6}/\n\n'
            'The set itself:\n{5}/edit_question_set/{6}/').format(
        _actor_name(owner), owner.user.username if owner.user else '?',
        ', ' + owner.user.email if (owner.user and owner.user.email) else '',
        joined, qset.name, base, qset.id)
    _send_mail_async(subject, body, recipients)
    return True


def _remembered_set_name(request):
    """The set the sidebar was last showing, for context in a report. The
    reporting page belongs to no set of its own, so this is the nearest thing
    to "what were you working on" -- the id the nav keeps in the session."""
    set_id = request.session.get('nav_active_set')
    if not set_id:
        return '(none)'
    qset = QuestionSet.objects.filter(id=set_id).first()
    return '{0} (#{1})'.format(qset.name, qset.id) if qset else '(none)'


def _support_recipients():
    """Who gets what the bug/idea form collects. Blank setting = the form is
    off, and the sidebar stops offering it."""
    from django.conf import settings as dj_settings
    raw = getattr(dj_settings, 'SUPPORT_EMAIL', '') or ''
    return [addr.strip() for addr in raw.split(',') if addr.strip()]


@login_required
def report_issue(request):
    """Report a bug or ask for a feature, by email to whoever runs the site.

    Sent on the request rather than in the background: this is somebody
    deliberately writing to a person, so the page has to be able to say whether
    it actually went -- and if the mail fails, to hand them back what they
    wrote instead of swallowing it.
    """
    from django.core.mail import EmailMessage
    from django.conf import settings as dj_settings
    from .forms import IssueReportForm

    user = request.user.writer
    recipients = _support_recipients()
    if not recipients:
        return render(request, 'failure.html',
                      {'message': 'Reporting is not configured on this site.',
                       'message_class': 'alert-box warning'})

    sent = False
    failed = False
    if request.method == 'POST':
        form = IssueReportForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            kind = 'Bug' if data['kind'] == 'bug' else 'Feature request'
            # Who they are and what they were browsing with -- and nothing
            # that points at a question. This mail leaves the site for an
            # ordinary inbox, so no question text and no link that would lead
            # to one travels with it, not even the page they came from.
            context = [
                'From:    {0} <{1}>'.format(data['name'] or _actor_name(user), data['email']),
                'Account: {0}'.format(request.user.username),
                'Set:     {0}'.format(_remembered_set_name(request)),
                'Browser: {0}'.format((request.META.get('HTTP_USER_AGENT') or '?')[:200]),
                'Site:    {0}'.format(dj_settings.BASE_URL),
            ]
            body = '{0}\n\n{1}\n\n{2}'.format(
                data['details'].strip(), '-' * 40, '\n'.join(context))
            message = EmailMessage(
                subject='QEMS3 {0}: {1}'.format(kind.lower(), data['summary']),
                body=body,
                from_email=dj_settings.DEFAULT_FROM_EMAIL,
                to=recipients,
                # The From has to stay the verified sender, so the reporter's
                # address goes here -- Reply lands on them, not on the site.
                reply_to=[data['email']])
            try:
                message.send(fail_silently=False)
                sent = True
            except Exception as ex:
                # Say so rather than claiming it went, and leave the form filled
                # in so nothing they wrote is lost.
                print('Issue report mail failed:', ex)
                print('Unsent issue report:', body)
                failed = True
        return render(request, 'report_issue.html',
                      {'form': IssueReportForm() if sent else form,
                       'user': user, 'sent': sent, 'failed': failed,
                       'support_email': recipients[0]})

    form = IssueReportForm(initial={
        'name': user.get_real_name().strip(),
        'email': request.user.email,
        'kind': request.GET.get('kind') if request.GET.get('kind') in ('bug', 'feature') else 'bug',
    })
    return render(request, 'report_issue.html',
                  {'form': form, 'user': user, 'support_email': recipients[0]})


@login_required
def suggestion_quality(request):
    """Which style-check suggestions editors keep throwing out.

    A suggestion rejected again and again is usually the suggestion's fault:
    the bundled pronunciation dictionary offering a guide for a name nobody
    mispronounces, or an answer-line alternate that doesn't belong. This is the
    only view of that, and it is deliberately about the suggestion rather than
    about anyone's set -- no set, question or writer appears here, by design
    and in the stored data.
    """
    if not request.user.is_superuser:
        return render(request, 'failure.html',
                      {'message': 'Only a site administrator can review suggestion quality.',
                       'message_class': 'alert-box alert'})

    from . import style_checker
    KINDS = {'pronunciation': ('pronunciation',),
             'answers': ('answer_alts',)}
    kind = request.GET.get('kind', 'all')
    rows_qs = SuggestionFeedback.objects.all()
    if kind in KINDS:
        rows_qs = rows_qs.filter(code__in=KINDS[kind])

    rows = []
    for fb in rows_qs:
        described = style_checker.describe_dismissal(fb.code, fb.token)
        rows.append({
            'code': fb.code,
            'rule': described['rule'],
            'subject': described['subject'] or described['field'] or '(whole rule)',
            'accepted': fb.accepted,
            'rejected': fb.rejected,
            'total': fb.total(),
            'rate': fb.rejection_rate(),
            'last': fb.last_action_date,
            # Rejected several times and taken rarely or never: the shape of a
            # suggestion that should be pulled from the dictionary rather than
            # dismissed one editor at a time.
            'suspect': fb.rejected >= 3 and fb.rejection_rate() >= 75,
        })
    # Worst first: most rejected, then by how one-sided the verdicts are.
    rows.sort(key=lambda r: (-r['rejected'], -r['rate'], r['subject']))

    totals = {
        'tracked': len(rows),
        'rejected': sum(r['rejected'] for r in rows),
        'accepted': sum(r['accepted'] for r in rows),
        'suspect': sum(1 for r in rows if r['suspect']),
    }
    return render(request, 'suggestion_quality.html',
                  {'rows': rows[:400], 'totals': totals, 'kind': kind,
                   'truncated': len(rows) > 400,
                   'user': request.user.writer})


def _reference_verdicts(dataset):
    """{entry key: {'accepted': n, 'rejected': n}} from the suggestion counters.

    The counters are keyed by the style-check token, which names the thing the
    suggestion was about — a dictionary term, or an answer database key — so
    they line up with the reference entries once the term is normalized.
    """
    from . import pron_dict
    code = 'pronunciation' if dataset == ReferenceDataOverride.PRONUNCIATION else 'answer_alts'
    out = {}
    for fb in SuggestionFeedback.objects.filter(code=code):
        parts = (fb.token or '').split('|')
        subject = parts[1] if len(parts) > 1 else ''
        if not subject:
            continue
        key = (pron_dict.normalize_term(subject)
               if code == 'pronunciation' else subject.strip().lower())
        row = out.setdefault(key, {'accepted': 0, 'rejected': 0})
        row['accepted'] += fb.accepted
        row['rejected'] += fb.rejected
    return out


@login_required
def reference_review(request, dataset='pron'):
    """Work through the bundled reference data by how much it can be trusted.

    The search screen answers "what does the dictionary say about X". This one
    answers the question that actually gets bad entries fixed: which entries
    look wrong, worst first. Entries editors have thrown out come first of all —
    that is evidence rather than a heuristic — then the tiers.
    """
    from . import reference_quality

    denied = _reference_admin_only(request)
    if denied is not None:
        return denied
    if dataset not in (ReferenceDataOverride.PRONUNCIATION, ReferenceDataOverride.ANSWER_LINE):
        dataset = ReferenceDataOverride.PRONUNCIATION

    rows, signal_labels = reference_quality.report(dataset)
    tier_counts, signal_counts = reference_quality.summarize(rows)
    verdicts = _reference_verdicts(dataset)

    tier = request.GET.get('tier', '')
    signal = request.GET.get('signal', '')
    query = (request.GET.get('q') or '').strip().lower()
    rejected_only = request.GET.get('rejected') == '1'

    shown = []
    for row in rows:
        if tier and row['tier'] != tier:
            continue
        if signal and signal not in row['signals']:
            continue
        if query and query not in row['key'] and query not in (row['value'] or '').lower():
            continue
        verdict = verdicts.get(row['key'], {})
        if rejected_only and not verdict.get('rejected'):
            continue
        shown.append(dict(row,
                          accepted=verdict.get('accepted', 0),
                          rejected=verdict.get('rejected', 0),
                          reasons=[signal_labels[s][1] for s in row['signals']
                                   if s in signal_labels]))

    tier_rank = {reference_quality.TIER_LOW: 0, reference_quality.TIER_REVIEW: 1,
                 reference_quality.TIER_HIGH: 2}
    shown.sort(key=lambda r: (-r['rejected'], tier_rank.get(r['tier'], 3),
                              -len(r['signals']), r['key']))

    try:
        page = max(1, int(request.GET.get('page', '1')))
    except ValueError:
        page = 1
    per_page = 100
    total = len(shown)
    start = (page - 1) * per_page
    page_rows = shown[start:start + per_page]

    filters = []
    for name, value in (('tier', tier), ('signal', signal), ('q', query),
                        ('rejected', '1' if rejected_only else '')):
        if value:
            filters.append('{0}={1}'.format(name, quote(value)))
    querystring = '&'.join(filters)

    return render(request, 'reference_review.html',
                  {'user': request.user.writer,
                   'dataset': dataset,
                   'is_pron': dataset == ReferenceDataOverride.PRONUNCIATION,
                   'rows': page_rows,
                   'total': total,
                   'page': page,
                   'has_prev': page > 1,
                   'has_next': start + per_page < total,
                   'tier': tier,
                   'signal': signal,
                   'query': query,
                   'rejected_only': rejected_only,
                   'querystring': querystring,
                   'tier_counts': tier_counts,
                   'tiers': [{'key': k, 'label': l, 'count': tier_counts.get(k, 0)}
                             for k, l in reference_quality.TIER_LABELS],
                   'signals': [{'key': k, 'label': signal_labels[k][1],
                                'grade': signal_labels[k][0],
                                'count': signal_counts.get(k, 0)}
                               for k in signal_labels if signal_counts.get(k)],
                   'entry_total': len(rows)})


@login_required
def pending_sets(request):
    """Every set waiting on an administrator, so approval doesn't depend on an
    e-mail arriving. Deliberately says nothing about what is *in* a set: who
    made it, when they signed up and how much is written is what the decision
    turns on, and a set under review is still its owner's private work.
    """
    if not request.user.is_superuser:
        return render(request, 'failure.html',
                      {'message': 'Only a site administrator can review new sets.',
                       'message_class': 'alert-box alert'})

    pending = (QuestionSet.objects
               .filter(approval_status=QuestionSet.APPROVAL_PENDING)
               .select_related('owner__user').order_by('id'))
    tu_counts = {row['question_set']: row['n'] for row in
                 Tossup.objects.filter(question_set__in=pending)
                 .values('question_set').annotate(n=Count('id'))}
    bs_counts = {row['question_set']: row['n'] for row in
                 Bonus.objects.filter(question_set__in=pending)
                 .values('question_set').annotate(n=Count('id'))}

    rows = []
    for qset in pending:
        owner_user = qset.owner.user if qset.owner else None
        rows.append({
            'qset': qset,
            'owner': qset.owner,
            'owner_name': _actor_name(qset.owner),
            'username': owner_user.username if owner_user else '',
            'email': owner_user.email if owner_user else '',
            'joined': owner_user.date_joined if owner_user else None,
            'tossups': tu_counts.get(qset.id, 0),
            'bonuses': bs_counts.get(qset.id, 0),
        })

    recipients = _new_set_approval_recipients()
    return render(request, 'pending_sets.html',
                  {'rows': rows, 'user': request.user.writer,
                   'recipients': ', '.join(recipients)})


@login_required
def approve_new_set(request, qset_id):
    """Administrator review of a set created by an account that hadn't waited
    out the new-account period, linked directly from the notification email.
    Approving makes it an ordinary set. Declining leaves it in place but keeps
    it out of everyone else's way; deleting is offered separately, since that
    throws away whatever the owner has written."""
    user = request.user.writer
    if not request.user.is_superuser:
        return render(request, 'failure.html',
                      {'message': 'Only a site administrator can review new sets.',
                       'message_class': 'alert-box alert'})
    try:
        qset = QuestionSet.objects.get(id=int(qset_id))
    except (ValueError, QuestionSet.DoesNotExist):
        return render(request, 'failure.html',
                      {'message': 'That set no longer exists.',
                       'message_class': 'alert-box alert'})

    action = request.POST.get('action', '') if request.method == 'POST' else ''

    if action == 'delete':
        name = qset.name
        from . import set_importer
        set_importer.delete_question_set(qset)
        cache.clear()
        return render(request, 'approve_new_set.html',
                      {'deleted': True, 'set_name': name, 'user': user})

    if action in ('approve', 'decline'):
        qset.approval_status = (QuestionSet.APPROVAL_APPROVED if action == 'approve'
                                else QuestionSet.APPROVAL_DECLINED)
        qset.approval_date = timezone.now()
        qset.save(update_fields=['approval_status', 'approval_date'])
        cache.clear()
        return render(request, 'approve_new_set.html',
                      {'qset': qset, 'acted': action, 'user': user})

    counts = {'tossups': Tossup.objects.filter(question_set=qset).count(),
              'bonuses': Bonus.objects.filter(question_set=qset).count()}
    return render(request, 'approve_new_set.html',
                  {'qset': qset, 'owner': qset.owner, 'counts': counts, 'user': user})


@login_required
def import_set(request):
    """Admin-only: create a new question set from an uploaded TSV/CSV in the
    export format, including comments, and index it for search."""
    user = request.user.writer

    if not request.user.is_superuser:
        messages.error(request, 'Only the admin account may import sets.')
        return HttpResponseRedirect('/failure.html/')

    message = ''
    message_class = ''
    summary = None

    if request.method == 'POST':
        form = ImportSetForm(request.POST, request.FILES, writer=user)
        if form.is_valid():
            from .set_importer import import_set_from_file, SetImportError
            try:
                target = form.cleaned_data.get('target_set')
                summary = import_set_from_file(
                    form.cleaned_data['set_file'],
                    (form.cleaned_data.get('set_name') or '').strip(), user,
                    target_set=target)
                qset = summary['question_set']
                # Name the file, not just the set: importing an archive is a
                # run of near-identical uploads, and "which one did I just do"
                # is the only thing the confirmation can answer that the set
                # page cannot.
                message = ('{0} — {1} "{2}": {3} tossups, {4} bonuses, {5} comments.'
                           .format(form.cleaned_data['set_file'].name,
                                   'added to' if target is not None else 'imported as',
                                   qset.name, summary['tossups'], summary['bonuses'],
                                   summary['comments']))
                if target is not None:
                    message += (' The set now holds {0} tossups and {1} bonuses.'
                                .format(Tossup.objects.filter(question_set=qset).count(),
                                        Bonus.objects.filter(question_set=qset).count()))
                    if summary.get('categories_created'):
                        message += (' Added {0} categor{1} the set did not have.'
                                    .format(summary['categories_created'],
                                            'y' if summary['categories_created'] == 1 else 'ies'))
                if summary.get('users_created'):
                    message += ' Created {0} placeholder commenter account(s).'.format(summary['users_created'])
                if summary.get('legacy_authors'):
                    message += ' Created {0} legacy author account(s) for authors with no current account.'.format(summary['legacy_authors'])
                message += ' Search indexing is running in the background and will finish shortly.'
                message_class = 'alert-box success'
            except SetImportError as ex:
                message = str(ex)
                message_class = 'alert-box alert'
            except Exception as ex:
                message = 'Import failed: {0}'.format(ex)
                message_class = 'alert-box alert'
    else:
        # ?target=<id> comes from a set's own overview ("Import questions into
        # this set"), so the page opens on the set you came from rather than
        # making you find it in the list again. An id you cannot import into is
        # simply not in the field's queryset and is ignored.
        target = request.GET.get('target', '')
        form = ImportSetForm(writer=user,
                             initial={'target_set': target} if target.isdigit() else None)

    return render(request, 'import_set.html',
                  {'form': form, 'user': user, 'summary': summary,
                   'message': message, 'message_class': message_class})

@login_required
def import_packets(request):
    """Admin-only: create a new tournament (question set) from uploaded packet
    files (.docx or .pdf), one packet per file."""
    user = request.user.writer

    if not request.user.is_superuser:
        messages.error(request, 'Only the admin account may import packets.')
        return HttpResponseRedirect('/failure.html/')

    message = ''
    message_class = ''
    summary = None

    if request.method == 'POST':
        form = ImportPacketsForm(request.POST, request.FILES, writer=user)
        if form.is_valid():
            from .packet_set_importer import (import_packets_from_files,
                                              import_packets_into_set, PacketImportError)
            files = form.cleaned_data['packet_files']
            target = form.cleaned_data.get('target_set')
            try:
                if target is not None:
                    summary = import_packets_into_set(files, target, user)
                    qset = summary['question_set']
                    message = ('Added {0} packet(s) to "{1}": {2} tossups, {3} bonuses.'
                               .format(len(summary['packets']), qset.name,
                                       summary['tossups'], summary['bonuses']))
                else:
                    summary = import_packets_from_files(
                        files, form.cleaned_data['set_name'], user)
                    qset = summary['question_set']
                    message = ('Imported "{0}" from {1} packet(s): {2} tossups, {3} bonuses.'
                               .format(qset.name, len(summary['packets']),
                                       summary['tossups'], summary['bonuses']))
                if summary['errors']:
                    message += ' {0} question(s) could not be parsed (see below).'.format(len(summary['errors']))
                message_class = 'alert-box success'
            except PacketImportError as ex:
                message = str(ex)
                message_class = 'alert-box alert'
            except Exception as ex:
                message = 'Import failed: {0}'.format(ex)
                message_class = 'alert-box alert'
        else:
            errs = form.errors.get('__all__')
            message = errs[0] if errs else 'Please choose files and a destination (new name or existing set).'
            message_class = 'alert-box alert'
    else:
        form = ImportPacketsForm(writer=user)

    return render(request, 'import_packets.html',
                  {'form': form, 'user': user, 'summary': summary,
                   'message': message, 'message_class': message_class})

def packet(request):
    if request.user.is_authenticated:
        player = request.user.get_profile()
        packets = player.packet_set.filter(date_submitted=None)

        print('packets: ', packets)

        return render(request, 'packetview.html',
                                  {'packet_list': packets})

    else:
        return HttpResponseRedirect('/accounts/login/')

def _sync_set_categories(qset):
    """Give a set one category row per entry of the distribution it currently
    points at, and drop rows left behind by a distribution it no longer uses.

    A set's categories live in SetWideDistributionEntry rows written when the
    set is created; changing the distribution afterwards only repointed the
    foreign key, so the categories, the requirements and the packetize page all
    went on describing the old distribution (or stayed empty, if the rows were
    never written). Existing rows keep their numbers -- an owner may have
    edited a requirement by hand -- so this only adds what is missing and
    removes what belongs to another distribution.

    Returns (added, removed, stranded): stranded counts questions still filed
    under a category from the old distribution, which nothing here moves.
    """
    dist = qset.distribution
    entries = list(dist.distributionentry_set.all())
    existing = {e.dist_entry_id: e for e in qset.setwidedistributionentry_set.all()}

    removed = qset.setwidedistributionentry_set.exclude(
        dist_entry__distribution=dist).delete()[0]
    qset.tiebreakdistributionentry_set.exclude(dist_entry__distribution=dist).delete()

    have_tiebreak = set(qset.tiebreakdistributionentry_set.values_list('dist_entry_id', flat=True))
    added = 0
    for entry in entries:
        if entry.id not in existing:
            # A distribution entry may leave its minimums blank; the set-wide
            # row can't be null, and "unspecified" means none required.
            SetWideDistributionEntry.objects.create(
                question_set=qset, dist_entry=entry,
                num_tossups=qset.num_packets * (entry.min_tossups or 0),
                num_bonuses=qset.num_packets * (entry.min_bonuses or 0))
            added += 1
        if entry.id not in have_tiebreak:
            TieBreakDistributionEntry.objects.create(
                question_set=qset, dist_entry=entry, num_tossups=1, num_bonuses=1)

    stranded = (qset.tossup_set.exclude(category__distribution=dist).count() +
                qset.bonus_set.exclude(category__distribution=dist).count())
    return added, removed, stranded


@login_required
def create_question_set (request):
    user = request.user.writer

    # An account too new to create a set outright isn't turned away: the set is
    # made provisionally and an administrator is emailed to approve it. That
    # keeps a real person's first evening from being a dead end while still
    # giving a spam set nowhere to go — until it's approved it stays out of the
    # public list and its join links don't work. With no approval address
    # configured there's nobody to ask, so the set is simply approved.
    needs_approval = (not _account_can_create(request.user)
                      and bool(_new_set_approval_recipients()))

    if request.method == 'POST':
        form = QuestionSetForm(data=request.POST, writer=user)
        if form.is_valid():
            # for the moment, just use the default ACF Distribution
            #dist = Distribution.objects.get(id=1)
            question_set = form.save(commit=False)
            if needs_approval:
                question_set.approval_status = QuestionSet.APPROVAL_PENDING
            question_set.owner = user
            question_set.editors = []
            question_set.editors.append(user)
            #question_set.distribution = dist
            question_set.save()
            form.save_m2m()
            user.question_set_editor.add(question_set)
            user.save()

            # Ask for approval as soon as there is a set to approve. This used
            # to come after the category rows below, so anything that went
            # wrong building them left a pending set nobody had been told
            # about -- and a failure to notify shouldn't lose the set either.
            if needs_approval:
                try:
                    _notify_new_set_pending(question_set)
                except Exception:
                    print('Could not e-mail the new-set approval request:',
                          sys.exc_info()[0], sys.exc_info()[1])

            _sync_set_categories(question_set)

            # Redirect rather than render: the set page's own URL is what tells
            # the shell which set is active (rendering here left the sidebar's
            # active set on whatever you had before), and it stops a refresh
            # from re-submitting the form.
            request.session['nav_active_set'] = question_set.id
            return HttpResponseRedirect(
                '/edit_question_set/{0}/?created=1'.format(question_set.id))
        else:
            print(form.errors)
            distributions = Distribution.visible_to(user)
            return render(request, 'create_question_set.html',
                                      {'message': 'There was an error in creating your question set!',
                                       'message_class': 'alert-box warning',
                                       'form': form,
                                       'distributions': distributions,
                                       'needs_approval': needs_approval,
                                       'user': user})
    else:
        form = QuestionSetForm(writer=user)
        distributions = Distribution.visible_to(user)

    return render(request, 'create_question_set.html',
                              {'form': form,
                               'distributions': distributions,
                               'needs_approval': needs_approval,
                               'user': user})

def _editor_tag_context(qset):
    """Editor tags for the Writers & Editors tab: {editor_id: [EditorTag,...]}
    plus the set's category paths (top-level and sub, matching the category
    overview's row names) offered when adding a category tag."""
    tags_by_editor = {}
    for t in qset.editor_tags.select_related('editor__user').order_by('category', 'label'):
        tags_by_editor.setdefault(t.editor_id, []).append(t)
    cats = set()
    for e in qset.setwidedistributionentry_set.select_related('dist_entry'):
        de = e.dist_entry
        parts = [de.category] + [s.strip() for s in (de.subcategory or '').split(' - ') if s.strip()]
        for i in range(1, len(parts) + 1):
            cats.add(' - '.join(parts[:i]))
    return {'editor_tags': tags_by_editor, 'category_options': sorted(cats)}


def _join_link_context(qset, user):
    """Join-link controls and pending access requests for the Writers & Editors
    tab. Both are owner-only: editors can add members directly but don't hand out
    links or approve requests."""
    is_set_owner = qset.is_owner(user)
    if not is_set_owner:
        return {'is_set_owner': False, 'join_link': None, 'pending_join_requests': []}
    return {'is_set_owner': True,
            'join_link': SetJoinLink.objects.filter(question_set=qset).first(),
            'pending_join_requests': list(
                qset.join_requests.select_related('requester__user'))}


@login_required
def edit_question_set(request, qset_id):
    read_only = False
    message = ''
    message_class = ''
    tossups = []
    bonuses = []
    
    qset = QuestionSet.objects.get(id=qset_id)
    qset_editors = qset.editor.all()
    qset_writers = qset.writer.all()
    user = request.user.writer
    # Membership provenance: which members come from a role group (and which
    # groups), so the writers/editors list can show it. group_granted_ids are
    # those present ONLY via a group (no direct assignment) — they can't be
    # removed here; you remove them from the group instead.
    member_groups = {}
    for _a in qset.role_group_assignments.select_related('role_group'):
        for _w_id in _a.role_group.members.values_list('id', flat=True):
            member_groups.setdefault(_w_id, []).append(_a.role_group.name)
    group_granted_ids = set(GroupRoleGrant.objects.filter(question_set=qset)
                            .values_list('writer_id', flat=True))
    set_status = {}
    set_distro_formset = None
    tiebreak_formset = None
    writer_stats = {}

    total_tu_req = 0
    total_bs_req = 0
    total_tu_written = 0
    total_bs_written = 0
    comment_tab_list = []
    tu_needed = 0
    bs_needed = 0
    set_pct_complete = 0

    role = get_role_no_owner(user, qset)

    if not qset.is_owner(user) and user not in qset_editors and user not in qset_writers:
        messages.error(request, 'You are not authorized to view information about this tournament!')
        return HttpResponseRedirect('/failure.html/')

    new_activity = _new_activity_count(user, qset)
    visit_summary = None

    if request.method == 'POST':
        if (qset.is_owner(user) or user in qset_editors):
            form = QuestionSetForm(data=request.POST, writer=user)
            if form.is_valid():
                qset = QuestionSet.objects.get(id=qset_id)
                previous_distribution_id = qset.distribution_id
                # Every field the form carries, rather than a list repeated
                # here: the list had fallen behind the form twice over
                # (export_category_tags and enable_duplicate_checks were on the
                # page, were submitted, and were dropped on the floor), and a
                # setting that silently does not save is worse than one that
                # does not exist. The form is a ModelForm over this model with
                # an explicit exclude, so its fields are exactly the ones a set
                # is allowed to change here.
                # The many-to-many sides (co-owners, editors, writers) belong
                # to the Writers and Editors tab and its own actions; this form
                # never means to set them, and assigning one directly raises.
                _m2m = {f.name for f in QuestionSet._meta.many_to_many}
                for _field in form.fields:
                    if _field in form.cleaned_data and _field not in _m2m:
                        setattr(qset, _field, form.cleaned_data[_field])
                qset.save()

                # Switching the distribution used to change nothing but the
                # foreign key: the set's categories are its own rows, so they
                # went on describing the distribution it was created with. The
                # `not exists()` arm repairs a set that ended up with no
                # categories at all.
                saved_message = 'Your changes have been successfully saved.'
                if (previous_distribution_id != qset.distribution_id
                        or not qset.setwidedistributionentry_set.exists()):
                    added, removed, stranded = _sync_set_categories(qset)
                    if added or removed:
                        saved_message += (' The set now carries the {0} categories of '
                                          '"{1}"').format(len(qset.distribution.distributionentry_set.all()),
                                                          qset.distribution)
                        saved_message += (' ({0} added, {1} from the previous distribution '
                                          'removed).').format(added, removed)
                    if stranded:
                        saved_message += (' {0} question(s) are still filed under a category '
                                          'from the previous distribution and need '
                                          'recategorizing.').format(stranded)
                cache.clear()

                tossups, tossup_dict, bonuses, bonus_dict = get_tossup_and_bonuses_in_set(qset, question_limit=30, preview_only=True)

                if qset.is_owner(user):
                    read_only = False
                else:
                    read_only = True

                set_status, total_tu_req, total_bs_req, tu_needed, bs_needed, set_pct_complete = get_questions_remaining(qset)
                writer_stats = get_writer_questions_remaining(qset, total_tu_req, total_bs_req)
                                                                
                comment_tab_list = get_comment_tab_list(tossup_dict, bonus_dict, qset=qset)

                return render(request, 'edit_question_set.html',
                                          {'form': form,
                                           'qset': qset,
                                           'favicon_colors': QuestionSet.FAVICON_COLORS,
                                           'credit_tokens': QuestionSet.CREDIT_TOKENS,
                                           'user': user,
                                           'editors': [ed for ed in qset_editors if ed != qset.owner],
                                           'writers': qset.writer.all(),
                                           'writer_stats': writer_stats,
                                           'upload_form': QuestionUploadForm(),
                                           'set_status': set_status,
                                           'set_pct_complete': '{0:0.2f}%'.format(set_pct_complete),
                                           'set_pct_progress_bar': '{0:0.0f}%'.format(set_pct_complete),
                                           'tu_needed': tu_needed,
                                           'bs_needed': bs_needed,
                                           'tossups': tossups,
                                           'bonuses': bonuses,
                                           'packets': sorted_packets(qset, with_counts=True),
                                           'comment_list': comment_tab_list,
                                           'role': role,
                                           'new_activity': new_activity,
                                           'member_groups': member_groups,
                                           'group_granted_ids': group_granted_ids,
                                           **_editor_tag_context(qset),
                                           **_join_link_context(qset, user),
                                           'message': saved_message,
                                           'message_class': 'alert-success'})
            else:
                # Form invalid: still populate question data so the page isn't
                # blank (and so an empty render isn't cached).
                tossups, tossup_dict, bonuses, bonus_dict = get_tossup_and_bonuses_in_set(qset, question_limit=30, preview_only=True)
                set_status, total_tu_req, total_bs_req, tu_needed, bs_needed, set_pct_complete = get_questions_remaining(qset)
                writer_stats = get_writer_questions_remaining(qset, total_tu_req, total_bs_req)
                comment_tab_list = get_comment_tab_list(tossup_dict, bonus_dict, qset=qset)
                read_only = not (qset.is_owner(user) or user in qset_editors)
        else:
            return render(request, 'failure.html', {'message': 'You are not authorized to change this set!', 'message_class': 'alert-box alert'})
    else:
        print("Begin edit_question_set get", time.strftime("%H:%M:%S"))
        if user not in qset_editors and not qset.is_owner(user) and user not in qset.writer.all():
            # Just redirect to main in this case of no permissions
            # TODO: a better story
            return HttpResponseRedirect('/main.html')

        visit_summary = _record_visit_and_summarize(user, qset)

        tossups, tossup_dict, bonuses, bonus_dict = get_tossup_and_bonuses_in_set(qset, question_limit=30, preview_only=True)

        # create_question_set redirects here after a successful create.
        if request.GET.get('created'):
            message = 'Your question set has been successfully created!'
            message_class = 'alert-box success'

        if user not in qset_editors and not qset.is_owner(user):
            form = QuestionSetForm(instance=qset, read_only=True, writer=user)
            read_only = True
        else:
            if qset.is_owner(user):
                read_only = False
            elif user in qset.writer.all() or user in qset.editor.all():
                read_only = True
            form = QuestionSetForm(instance=qset, writer=user)

        set_status, total_tu_req, total_bs_req, tu_needed, bs_needed, set_pct_complete = get_questions_remaining(qset)
        writer_stats = get_writer_questions_remaining(qset, total_tu_req, total_bs_req)
                                                                
        comment_tab_list = get_comment_tab_list(tossup_dict, bonus_dict, qset=qset)                    

    print("End edit_question_set get", time.strftime("%H:%M:%S"))
        
    return render(request, 'edit_question_set.html',
                              {'form': form,
                               'user': user,
                               'favicon_colors': QuestionSet.FAVICON_COLORS,
                               'credit_tokens': QuestionSet.CREDIT_TOKENS,
                               'editors': [ed for ed in qset_editors if ed != qset.owner],
                               'writers': [wr for wr in qset_writers if wr != qset.owner],
                               'writer_stats': writer_stats,
                               'set_status': set_status,
                               'set_pct_complete': '{0:0.2f}%'.format(set_pct_complete),
                               'set_pct_progress_bar': '{0:0.0f}%'.format(set_pct_complete),
                               'tu_needed': tu_needed,
                               'bs_needed': bs_needed,
                               'upload_form': QuestionUploadForm(),
                               'tossups': tossups,
                               'bonuses': bonuses,
                               'packets': sorted_packets(qset, with_counts=True),
                               'comment_tab_list': comment_tab_list,
                               'qset': qset,
                               'role': role,
                               'new_activity': new_activity,
                               'visit_summary': visit_summary,
                               'read_only': read_only,
                               'all_role_groups': RoleGroup.objects.all().order_by('name'),
                               'attached_role_groups': list(
                                   qset.role_group_assignments.select_related('role_group')),
                               'member_groups': member_groups,
                               'group_granted_ids': group_granted_ids,
                               **_editor_tag_context(qset),
                               **_join_link_context(qset, user),
                               'message': message,
                               'message_class': message_class})

def _table_order(qset, questions):
    """The order a set's question tables list in.

    Shuffled when the set asks for it, so the questions at the end of a long
    category get looked at as often as the ones at the top; otherwise left in
    whatever order the query returned, which is stable and roughly by age.
    """
    questions = list(questions)
    if qset.question_table_random_order:
        random.shuffle(questions)
    return questions


@login_required
def categories(request, qset_id, category_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    qset_editors = qset.editor.all()
    qset_writers = qset.writer.all()

    category_object = DistributionEntry.objects.get(id=category_id)

    entry = qset.setwidedistributionentry_set.get(dist_entry=category_object)
    tu_required = entry.num_tossups
    bs_required = entry.num_bonuses
    tu_written = qset.tossup_set.filter(category=entry.dist_entry).count()
    bs_written = qset.bonus_set.filter(category=entry.dist_entry).count()

    category_status =   {'tu_req': tu_required,
                         'tu_in_cat': tu_written,
                         'bs_req': bs_required,
                         'bs_in_cat': bs_written
                         }

    message = category_object.category
    tossups = []
    bonuses = []
    if user not in qset_editors and not qset.is_owner(user) and user not in qset.writer.all():
        message = 'You are not authorized to view this set'
    else:
        # The table has a tags column; without prefetching it, showing it
        # costs one query per question in the category.
        tossups = _table_order(qset, Tossup.objects
                               .filter(question_set=qset, category=category_id)
                               .select_related(*QUESTION_LIST_RELATED)
                               .prefetch_related('category_tags'))
        bonuses = _table_order(qset, Bonus.objects
                               .filter(question_set=qset, category=category_id)
                               .select_related(*QUESTION_LIST_RELATED)
                               .prefetch_related('category_tags'))
        attach_question_comments({t.id: t for t in tossups}, {b.id: b for b in bonuses})

    return render(request, 'categories.html',
        dict({
        'table_columns': qset.question_table_headers(),
        'user': user,
        'tossups': tossups,
        'bonuses': bonuses,
        'category_status': category_status,
        'qset': qset,
        'message': message,
        'category': category_object},
        **_category_comment_context(request, qset, str(category_object) if category_object else '')))

@login_required
def top_category(request, qset_id, category_name):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)

    if user not in qset.editor.all() and not qset.is_owner(user) and user not in qset.writer.all():
        return render(request, 'failure.html', {'message': 'You are not authorized to view this set'})

    role = get_role_no_owner(user, qset)

    entries = DistributionEntry.objects.filter(
        setwidedistributionentry__question_set=qset,
        category=category_name
    ).distinct().order_by('subcategory')

    sub_categories = []
    for entry in entries:
        swide = qset.setwidedistributionentry_set.filter(dist_entry=entry).first()
        if swide:
            tu_req = swide.num_tossups
            bs_req = swide.num_bonuses
            tu_written = qset.tossup_set.filter(category=entry).count()
            bs_written = qset.bonus_set.filter(category=entry).count()
            sub_categories.append({
                'entry': entry,
                'tu_req': tu_req,
                'tu_written': tu_written,
                'bs_req': bs_req,
                'bs_written': bs_written,
            })

    return render(request, 'top_category.html', {
        'user': user,
        'qset': qset,
        'category_name': category_name,
        'sub_categories': sub_categories,
        'role': role,
    })

@login_required
def category_document(request, qset_id, category_id=None, category_name=None):
    """Read-only document view of every question in a category or subcategory
    (e.g. all of History - American, or all of History)."""
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return render(request, 'failure.html',
                      {'message': 'You are not authorized to view this set',
                       'message_class': 'alert-box alert'})

    if category_id is not None:
        cat = DistributionEntry.objects.get(id=category_id)
        title = str(cat)
        tu_qs = Tossup.objects.filter(question_set=qset, category=cat)
        bs_qs = Bonus.objects.filter(question_set=qset, category=cat)
    else:
        title = category_name
        tu_qs = Tossup.objects.filter(question_set=qset, category__category=category_name)
        bs_qs = Bonus.objects.filter(question_set=qset, category__category=category_name)

    related = ('category', 'author__user', 'packet')
    order = ('packet__sort_order', 'packet__id', 'question_number', 'id')
    tu_qs = tu_qs.select_related(*related).order_by(*order)
    bs_qs = bs_qs.select_related(*related).order_by(*order)

    def label(writer):
        if writer is None:
            return ''
        return '{0} {1}'.format(writer.user.first_name, writer.user.last_name).strip() or writer.user.username

    def item(q, qtype):
        return {
            'id': q.id, 'qtype': qtype, 'html': q.to_html(),
            'category': str(q.category) if q.category else '',
            'packet': q.packet.packet_name if q.packet else 'Unpacketized',
            'number': q.question_number or '',
            'author': label(q.author),
            'edit_url': '/edit_{0}/{1}/'.format(qtype, q.id),
        }

    tossups = [item(t, 'tossup') for t in tu_qs]
    bonuses = [item(b, 'bonus') for b in bs_qs]
    return render(request, 'category_document.html', dict({
        'qset': qset, 'title': title, 'tossups': tossups, 'bonuses': bonuses,
        'count': len(tossups) + len(bonuses), 'user': user,
        'category_id': category_id, 'category_name': category_name},
        # A whole top-level category shows what was said about its
        # subcategories too; a subcategory page shows only its own.
        **_category_comment_context(request, qset, title,
                                    include_children=category_id is None)))


def _dup_fingerprint(qset):
    """A cheap signature of the set's question state; changes whenever a
    question is added, removed, or edited, so the cached report auto-refreshes."""
    import hashlib
    from django.db.models import Count, Max
    t = Tossup.objects.filter(question_set=qset).aggregate(n=Count('id'), m=Max('last_changed_date'))
    b = Bonus.objects.filter(question_set=qset).aggregate(n=Count('id'), m=Max('last_changed_date'))
    raw = '{0}-{1}-{2}-{3}'.format(t['n'], t['m'], b['n'], b['m'])
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


def _dup_render_answer(raw):
    return get_formatted_question_html(get_primary_answer(raw or ''), True, True, False, False).strip()


def _new_question_checks(qset, question, qtype):
    """Style-check and repeat-check results for a question that was just
    created, for the one-time panel on its edit page. Same rules as the sidebar
    style panel (honoring the set's disabled rules and dismissals) plus the
    duplicate-answer scan the add pages used to show."""
    from . import style_checker
    disabled = qset.disabled_style_rule_set()
    found = (style_checker.check_tossup(question, style_checker.DEFAULT_GUIDE, disabled)
             if qtype == 'tossup'
             else style_checker.check_bonus(question, style_checker.DEFAULT_GUIDE, disabled))
    dismissed = set(StyleIssueDismissal.objects.filter(
        question_type=qtype, question_id=question.id).values_list('code', 'token'))
    rule_dismissed = set(StyleRuleDismissal.objects.filter(
        question_set=qset).values_list('code', 'token'))
    issues = [i for i in found
              if (i['code'], i.get('token', '')) not in dismissed
              and (i['code'], i.get('token', '')) not in rule_dismissed]
    return {'style_issues': issues,
            'dup_matches': _post_submit_dup_matches(qset, question, qtype)}


def _post_submit_dup_matches(qset, question, qtype):
    """Find duplicate-answer matches for a just-saved question and render their
    answers, for the 'you may have created a duplicate' warning shown after a
    question is submitted."""
    matches = find_answer_matches(qset, question, qtype)
    for m in matches:
        m['answer_html'] = _dup_render_answer(m['answer_raw'])
    return matches


def _dup_answer_html(entry, bonus_map):
    """Rendered answer for an entry. For bonuses, show all three answer lines
    (each cut at the '[') so the whole bonus is identifiable."""
    if entry['type'] == 'bonus':
        bonus = bonus_map.get(entry['id'])
        if bonus is not None:
            parts = [_dup_render_answer(a) for a in (bonus.part1_answer, bonus.part2_answer, bonus.part3_answer)
                     if a and a.strip()]
            if parts:
                return ' / '.join(parts)
    return _dup_render_answer(entry.get('answer_raw'))


def _dup_preview_html(raw_text, term, width=280):
    """A formatted preview window centered on where the repeat (`term`) occurs,
    falling back to the start of the text. Keeps QEMS markup so italics/
    underlines render."""
    raw = (raw_text or '').strip()
    if not raw:
        return ''
    center = 0
    if term:
        i = raw.lower().find(term.lower())
        if i < 0:
            i = strip_markup(raw).lower().find(term.lower())
        if i > 0:
            center = i
    start = max(0, center - width // 3)
    end = min(len(raw), start + width)
    start = max(0, end - width)
    snippet = raw[start:end]
    if start > 0 and ' ' in snippet:
        snippet = snippet.split(' ', 1)[1]
    if end < len(raw) and ' ' in snippet:
        snippet = snippet.rsplit(' ', 1)[0]
    html = get_formatted_question_html(snippet, False, True, False, True)
    return ('&hellip; ' if start > 0 else '') + html + (' &hellip;' if end < len(raw) else '')


@login_required
def compare_repeats(request, qset_id):
    """Compare this set against a PREVIOUS set (uploaded as packet files) to flag
    repeats: shared unusual answers, matching hard bonus parts, and tossup
    leadins/early clues that closely match an old tossup's."""
    from . import set_compare
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return render(request, 'failure.html',
                      {'message': 'You are not authorized to view this set.',
                       'message_class': 'alert-box alert'})

    findings = None
    parse_errors = []
    counts = {}
    message = message_class = ''
    if request.method == 'POST':
        form = CompareRepeatsForm(request.POST, request.FILES)
        if form.is_valid():
            from .packet_set_importer import PacketImportError
            files = form.cleaned_data['packet_files']
            try:
                prev_tu, prev_bo, parse_errors = set_compare.parse_previous_questions(files, qset)
                findings = set_compare.compare(qset, prev_tu, prev_bo)
                counts = {'prev_tossups': len(prev_tu), 'prev_bonuses': len(prev_bo),
                          'flagged': len(findings),
                          'critical': sum(1 for f in findings if f['severity'] == 'critical')}
                message = ('Compared against {0} tossups and {1} bonuses; flagged '
                           '{2} question(s).').format(len(prev_tu), len(prev_bo), len(findings))
                message_class = 'alert-box success' if findings else 'alert-box info'
            except PacketImportError as ex:
                message, message_class = str(ex), 'alert-box alert'
            except Exception as ex:
                message, message_class = 'Comparison failed: {0}'.format(ex), 'alert-box alert'
        else:
            message = "Please choose the previous set's packet files (.json, .docx, or .pdf)."
            message_class = 'alert-box alert'
    else:
        form = CompareRepeatsForm()

    return render(request, 'compare_repeats.html',
                  {'qset': qset, 'user': user, 'form': form, 'findings': findings,
                   'parse_errors': parse_errors, 'counts': counts,
                   'message': message, 'message_class': message_class})


@login_required
def duplicate_check(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)

    if not qset.is_owner(user) and user not in qset.editor.all() and user not in qset.writer.all():
        messages.error(request, 'You are not authorized to view this set.')
        return render(request, 'failure.html',
                      {'message': 'You are not authorized to view this set.',
                       'message_class': 'alert-box alert'})

    if not qset.enable_duplicate_checks:
        return render(request, 'duplicate_check.html',
                      {'qset': qset, 'user': user, 'checks_off': True,
                       'read_only': not (qset.is_owner(user) or user in qset.editor.all())})

    # Cache the (expensive) report, keyed by a fingerprint of the question
    # state so it recomputes only when something actually changed.
    cache_key = 'dupcheck:{0}:{1}'.format(qset.id, _dup_fingerprint(qset))
    context = cache.get(cache_key)

    if context is None:
        groups = find_duplicates(qset)
        # The summary describes the whole set, so it is taken now: the lists
        # below are capped and their entries truncated in place, and counting
        # afterwards would report only what the page happens to show.
        dup_summary = {
            'critical': sum(1 for g in groups if g['severity'] == CRITICAL),
            'warning': sum(1 for g in groups if g['severity'] == WARNING),
            'info': sum(1 for g in groups if g['severity'] == INFO),
            'entries': sum(len(g['entries']) for g in groups),
        }
        internal_issues = find_internal_issues(qset)
        topic_groups = find_topic_repeats(qset)

        # Only as many groups as a page can actually be, decided before
        # anything is loaded or rendered for them. The archive produces 1,894
        # duplicate groups over 8,818 entries, and rendering the lot made 780 MB
        # of HTML -- a page no browser opens, from a request no server should
        # spend three minutes on. Both lists are severity-sorted, so a cap keeps
        # the ones worth reading; the summary above still counts them all.
        DUP_RENDER_CAP = 150
        TOPIC_RENDER_CAP = 150
        dup_total = len(groups)
        topic_total = len(topic_groups)
        groups = groups[:DUP_RENDER_CAP]
        topic_render = topic_groups[:TOPIC_RENDER_CAP]

        # Batch-load packets and bonus answers for the questions actually shown
        tu_ids, bs_ids = set(), set()
        for group_list in (groups, topic_render):
            for group in group_list:
                for entry in group['entries']:
                    (bs_ids if entry['type'] == 'bonus' else tu_ids).add(entry['id'])
        bonus_map = {b.id: b for b in Bonus.objects.filter(id__in=bs_ids).select_related('packet')}
        tu_map = {t.id: t for t in Tossup.objects.filter(id__in=tu_ids).select_related('packet')}

        def packet_of(entry):
            obj = tu_map.get(entry['id']) if entry['type'] == 'tossup' else bonus_map.get(entry['id'])
            return obj.packet.packet_name if (obj is not None and obj.packet) else ''

        # ...and how much of a group: one answer shared by thousands of
        # questions is a fact, not a table worth printing in full.
        ENTRY_RENDER_CAP = 25
        for group in groups:
            group['entry_total'] = len(group['entries'])
            group['entries'] = group['entries'][:ENTRY_RENDER_CAP]
            group['entries_truncated'] = group['entry_total'] > len(group['entries'])
            for entry in group['entries']:
                entry['packet'] = packet_of(entry)
                entry['answer_html'] = _dup_answer_html(entry, bonus_map)
                entry['preview_html'] = _dup_preview_html(entry.get('text'), None)
            for pair in group['pairs']:
                pair['similarity_pct'] = int(pair['similarity'] * 100)

        for group in topic_render:
            term = group.get('label')
            for entry in group['entries']:
                entry['packet'] = packet_of(entry)
                entry['answer_html'] = _dup_answer_html(entry, bonus_map)
                entry['preview_html'] = _dup_preview_html(entry.get('text'), term)

        context = {
            'groups': groups,
            'critical_count': dup_summary['critical'],
            'warning_count': dup_summary['warning'],
            'info_count': dup_summary['info'],
            'total_groups': dup_total,
            'dup_shown': len(groups),
            'total_questions': dup_summary['entries'],
            'internal_issues': internal_issues,
            'bonus_repeat_count': sum(1 for i in internal_issues if i['issue_type'] == 'bonus_repeat_answer'),
            'clue_reuse_count': sum(1 for i in internal_issues if i['issue_type'] == 'tossup_clue_reuse'),
            'topic_groups': topic_render,
            'topic_total': topic_total,
            'topic_shown': len(topic_render),
            'topic_truncated': topic_total > len(topic_render),
            'topic_critical': sum(1 for g in topic_groups if g['severity'] == CRITICAL),
            'topic_warning': sum(1 for g in topic_groups if g['severity'] == WARNING),
            'topic_info': sum(1 for g in topic_groups if g['severity'] == INFO),
            'has_packets': qset.packet_set.exists(),
        }
        cache.set(cache_key, context, 1800)

    context = dict(context)
    context['qset'] = qset
    context['user'] = user
    return render(request, 'duplicate_check.html', context)

@login_required
def view_all_questions(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    qset_editors = qset.editor.all()
    qset_writers = qset.writer.all()

    message = ''
    tossups = []
    bonuses = []
    if user not in qset_editors and not qset.is_owner(user) and user not in qset.writer.all():
        message = 'You are not authorized to view this set'
        return render(request, 'failure.html',
                                 {'message': message,
                                  'message_class': 'alert-box alert'})        
    else:
        tossups, tossup_dict, bonuses, bonus_dict = get_tossup_and_bonuses_in_set(qset, question_limit=10000, preview_only=True)
        tossups = _table_order(qset, tossups)
        bonuses = _table_order(qset, bonuses)

    return render(request, 'view_all_questions.html',
        {
        'user': user,
        'tossups': tossups,
        'bonuses': bonuses,
        'qset': qset,
        'message': message})	

@login_required
def view_all_comments(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    qset_editors = qset.editor.all()
    qset_writers = qset.writer.all()

    message = ''
    tossups = []
    bonuses = []
    if user not in qset_editors and not qset.is_owner(user) and user not in qset.writer.all():
        message = 'You are not authorized to view this set'
        return render(request, 'failure.html',
                                 {'message': message,
                                  'message_class': 'alert-box alert'})        
    else:
        tossups, tossup_dict, bonuses, bonus_dict = get_tossup_and_bonuses_in_set(qset, question_limit=10000, preview_only=True)
        comment_tab_list = get_comment_tab_list(tossup_dict, bonus_dict, comment_limit=10000, qset=qset)
            
    return render(request, 'view_all_comments.html',
        {
        'user': user,
        'comment_tab_list': comment_tab_list,
        'qset': qset,
        'message': message})	

@login_required
def question_set_distribution(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    qset_editors = qset.editor.all()
    qset_writers = qset.writer.all()
    set_distro_formset = []
    tiebreak_formset = []
    read_only = True

    message = ''
    if user not in qset_editors and not qset.is_owner(user) and user not in qset.writer.all():
        message = 'You are not authorized to view this set'
        return render(request, 'failure.html',
                                 {'message': message,
                                  'message_class': 'alert-box alert'})                
    elif qset.is_owner(user):
        set_distro_formset = create_set_distro_formset(qset)
        tiebreak_formset = create_tiebreak_formset(qset)    
        read_only = False
    else:
        set_distro_formset = create_set_distro_formset(qset)
        tiebreak_formset = create_tiebreak_formset(qset)        
            
    return render(request, 'question_set_distribution.html',
        {
        'user': user,
        'set_distro_formset': set_distro_formset,
        'tiebreak_formset': tiebreak_formset,
        'qset': qset,
        'message': message,
        'read_only': read_only})	

@login_required
def edit_set_distribution(request, qset_id):

    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)

    if request.method == 'POST':

        DistributionEntryFormset = formset_factory(SetWideDistributionEntryForm, can_delete=False, extra=0)
        formset = DistributionEntryFormset(data=request.POST, prefix='distentry')

        if formset.is_valid() and qset.is_owner(user):
            for dist_form in formset.forms:
                entry_id = int(dist_form.cleaned_data['entry_id'])
                num_tossups = int(dist_form.cleaned_data['num_tossups'])
                num_bonuses = int(dist_form.cleaned_data['num_bonuses'])

                entry = SetWideDistributionEntry.objects.get(id=entry_id)
                entry.num_tossups = num_tossups
                entry.num_bonuses = num_bonuses
                entry.save()

            return HttpResponseRedirect('/question_set_distribution/{0}'.format(qset_id))
        else:
            return render(request, 'failure.html',
                                     {'message': 'Something went wrong. We\'re working on it.',
                                      'message_class': 'alert-box alert'})
    elif request.method == 'GET':
        if qset.is_owner(user):
            return render(request, 'view_all_questions.html',
                {
                'user': user,
                'tossups': tossups,
                'bonuses': bonuses,
                'qset': qset,
                'message': message})	
            

@login_required
def edit_set_tiebreak(request, qset_id):

    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)

    if request.method == 'POST':

        TiebreakDistributionEntryFormset = formset_factory(TieBreakDistributionEntryForm, can_delete=False, extra=0)
        formset = TiebreakDistributionEntryFormset(data=request.POST, prefix='tiebreak')

        if formset.is_valid() and qset.is_owner(user):
            for dist_form in formset.forms:
                entry_id = int(dist_form.cleaned_data['entry_id'])
                num_tossups = int(dist_form.cleaned_data['num_tossups'])
                num_bonuses = int(dist_form.cleaned_data['num_bonuses'])

                entry = TieBreakDistributionEntry.objects.get(id=entry_id)
                entry.num_tossups = num_tossups
                entry.num_bonuses = num_bonuses
                entry.save()

            return HttpResponseRedirect('/question_set_distribution/{0}'.format(qset_id))
        else:
            return render(request, 'failure.html',
                                     {'message': 'Something went wrong. We\'re working on it.',
                                      'message_class': 'alert-box alert'})

@login_required
def find_editor(request):
    pass

@login_required
def add_editor(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    message = ''

    if request.method == 'GET':
        if qset.is_owner(user):
            current_editors = qset.editor.all()

            available_editors = [writer for writer in pickable_writers().order_by('user__last_name', 'user__first_name', 'user__username')
                                 if writer not in current_editors and
                                    not qset.is_owner(writer)]
        else:
            available_editors = []
            return render(request, 'failure.html',
                                     {'message': 'You are not authorized to make changes to this tournament!',
                                      'message_class': 'alert-box alert'})

        return render(request, 'add_editor.html',
                                 {'qset': qset,
                                  'available_editors': available_editors,
                                  'message': message,
                                  'user': user})


    elif request.method == 'POST':
        if qset.is_owner(user):
            editors_to_add = request.POST.getlist('editors_to_add')
            # do some basic validation here
            if all([x.isdigit() for x in editors_to_add]):
                for editor_id in editors_to_add:
                    editor = Writer.objects.get(id=editor_id)
                    qset.editor.add(editor)
                    _notify_added_to_set(editor, qset, 'editor', user)
                    # A direct add takes ownership of the membership (so it
                    # survives role-group changes).
                    GroupRoleGrant.objects.filter(
                        question_set=qset, writer=editor, role='editor').delete()

                    # Don't have someone be both a writer and editor--delete them
                    try:
                        writer = qset.writer.get(id=editor_id)
                        if (writer is not None):
                            qset.writer.remove(writer)
                    except:
                        print("No writer to delete") # TODO: Come up with a better way of handling this

                qset.save()
                cache.clear()
                set_editors = qset.editor.all()
                available_editors = [writer for writer in pickable_writers().order_by('user__last_name', 'user__first_name', 'user__username')
                                     if writer not in set_editors and
                                        not qset.is_owner(writer)]
            else:
                message = 'Invalid data entered!'
                available_editors = []
        else:
            available_editors = []
            return render(request, 'failure.html',
                                     {'message': 'You are not authorized to make changes to this tournament!',
                                      'message_class': 'alert-box alert'})

        return render(request, 'add_editor.html',
                                 {'qset': qset,
                                  'available_editors': available_editors,
                                  'message': message,
                                  'user': user})

@login_required
def add_co_owner(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    message = ''

    def get_available():
        current_owners = qset.all_owners()
        return [writer for writer in pickable_writers().order_by('user__last_name', 'user__first_name', 'user__username')
                if writer not in current_owners]

    if not qset.is_owner(user):
        return render(request, 'failure.html',
                                 {'message': 'You are not authorized to make changes to this tournament!',
                                  'message_class': 'alert-box alert'})

    if request.method == 'POST':
        co_owners_to_add = request.POST.getlist('co_owners_to_add')
        if all([x.isdigit() for x in co_owners_to_add]):
            for co_owner_id in co_owners_to_add:
                co_owner = Writer.objects.get(id=co_owner_id)
                qset.co_owners.add(co_owner)
                # Co-owners get full editor privileges as well
                qset.editor.add(co_owner)
                _notify_added_to_set(co_owner, qset, 'co-owner', user)

                # Don't have someone be both a writer and a co-owner--remove them as writer
                try:
                    writer = qset.writer.get(id=co_owner_id)
                    if writer is not None:
                        qset.writer.remove(writer)
                except:
                    pass

            qset.save()
            cache.clear()
            message = 'Co-owner(s) added'
        else:
            message = 'Invalid data entered!'

    return render(request, 'add_co_owner.html',
                             {'qset': qset,
                              'available_co_owners': get_available(),
                              'message': message,
                              'user': user})

@login_required
def delete_co_owner(request):
    user = request.user.writer
    message = ''
    message_class = ''

    if request.method == 'POST':
        qset_id = request.POST['qset_id']
        qset = QuestionSet.objects.get(id=qset_id)
        co_owner_id = request.POST['co_owner_id']
        if qset.is_owner(user):
            co_owner = qset.co_owners.get(id=co_owner_id)
            qset.co_owners.remove(co_owner)
            # Also drop the editor privilege that came with co-ownership
            qset.editor.remove(co_owner)
            cache.clear()
            message = 'Co-owner removed'
            message_class = 'alert-box success'
        else:
            message = 'You are not authorized to remove co-owners from this set!'
            message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

@login_required
def add_writer(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    message = ''
    message_class = ''

    if request.method == 'GET':
        if qset.is_owner(user):
            set_writers = Writer.objects.filter(Q(question_set_writer=qset) | Q(question_set_editor=qset)).distinct().order_by('user__last_name', 'user__first_name', 'user__username')
            available_writers = [writer for writer in pickable_writers().order_by('user__last_name', 'user__first_name', 'user__username')
                                 if writer not in set_writers and
                                    not qset.is_owner(writer)]
        else:
            available_writers = []
            return render(request, 'failure.html',
                                     {'message': 'You are not authorized to make changes to this tournament!',
                                      'message_class': 'alert-box alert'})

        return render(request, 'add_writer.html',
                                 {'qset': qset,
                                  'available_writers': available_writers,
                                  'message': message,
                                  'user': user})


    elif request.method == 'POST':
        if qset.is_owner(user):
            writers_to_add = request.POST.getlist('writers_to_add')
            # do some basic validation here
            if all([x.isdigit() for x in writers_to_add]):
                for writer_id in writers_to_add:
                    writer = Writer.objects.get(id=writer_id)
                    qset.writer.add(writer)
                    _notify_added_to_set(writer, qset, 'writer', user)
                    GroupRoleGrant.objects.filter(
                        question_set=qset, writer=writer, role='writer').delete()
                qset.save()
                cache.clear()
                set_writers = Writer.objects.filter(Q(question_set_writer=qset) | Q(question_set_editor=qset)).distinct().order_by('user__last_name', 'user__first_name', 'user__username')
                available_writers = [writer for writer in pickable_writers().order_by('user__last_name', 'user__first_name', 'user__username')
                                     if writer not in set_writers and
                                        not qset.is_owner(writer)]
            else:
                message = 'Invalid data entered!'
                available_writers = []
        else:
            available_writers = []
            return render(request, 'failure.html',
                                     {'message': 'You are not authorized to make changes to this tournament!',
                                      'message_class': 'alert-box alert'})

        return render(request, 'add_writer.html',
                                 {'qset': qset,
                                  'available_writers': available_writers,
                                  'message': message,
                                  'message_class': message_class,
                                  'user': user})


def pickable_writers():
    """Writers a person can be offered when adding someone to a set or group:
    current accounts only. Imported sets create inactive "-legacy" placeholder
    accounts so old questions keep their author's name; those are attribution,
    not people, and must not turn up in a picker (older imports made the
    placeholder without the inactive flag, so both marks are checked)."""
    return (Writer.objects.filter(user__is_active=True)
            .exclude(user__username__endswith='-legacy')
            .select_related('user'))


@login_required
def user_search(request):
    """Find writers by username, email, or real name for the role-group member
    picker. Every space-separated term must match somewhere (so "Aarush Kikani"
    matches a first + last name). Returns up to 12 as JSON."""
    q = (request.GET.get('q') or '').strip()
    results = []
    if len(q) >= 2:
        qobj = Q()
        for term in q.split():
            qobj &= (Q(user__username__icontains=term) | Q(user__email__icontains=term) |
                     Q(user__first_name__icontains=term) | Q(user__last_name__icontains=term))
        matches = pickable_writers().filter(qobj).order_by('user__username')[:12]
        for w in matches:
            name = '{0} {1}'.format(w.user.first_name or '', w.user.last_name or '').strip()
            label = ('{0} ({1})'.format(name, w.user.username) if name else w.user.username)
            if w.user.email:
                label += ' · ' + w.user.email
            results.append({'id': w.id, 'label': label, 'username': w.user.username})
    return HttpResponse(json.dumps({'results': results}), content_type='application/json')


@login_required
def role_groups(request):
    """Create and manage role groups (named groups of writers). Members added
    here propagate to every set the group is attached to."""
    user = request.user.writer
    message = message_class = ''

    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action == 'create':
            if not _account_can_create(request.user):
                message, message_class = ("New accounts can't create role groups until 2 days "
                                          "after sign-up. Please try again later.", 'alert-box warning')
            else:
                name = (request.POST.get('name') or '').strip()
                if not name:
                    message, message_class = 'Enter a group name.', 'alert-box warning'
                elif RoleGroup.objects.filter(name__iexact=name).exists():
                    message, message_class = 'A group with that name already exists.', 'alert-box warning'
                else:
                    group = RoleGroup.objects.create(name=name, created_by=user)
                    # The creator belongs to their own group (so it isn't
                    # "0 members" and they get any role it grants).
                    group.members.add(user)
                    message, message_class = 'Group "{0}" created.'.format(name), 'alert-box success'
        elif action == 'request_join':
            try:
                group = RoleGroup.objects.get(id=int(request.POST.get('group_id', 0)))
            except (ValueError, RoleGroup.DoesNotExist):
                group = None
            if group is None:
                message, message_class = 'Group not found.', 'alert-box warning'
            elif group.can_manage(user) or group.members.filter(id=user.id).exists():
                message, message_class = 'You are already part of this group.', 'alert-box warning'
            elif RoleGroupJoinRequest.objects.filter(role_group=group, requester=user).exists():
                message, message_class = ('You already have a pending request to join '
                                          '"{0}".'.format(group.name), 'alert-box info')
            else:
                RoleGroupJoinRequest.objects.get_or_create(role_group=group, requester=user)
                if _notify_group_join_request(group, user):
                    message, message_class = ('Your request to join "{0}" was sent to the group '
                                              'owner.'.format(group.name), 'alert-box success')
                else:
                    message, message_class = ('Your request to join "{0}" is pending. (The group '
                                              'owner has no email on file, but they\'ll see it in '
                                              'their pending list.)'.format(group.name), 'alert-box info')
        else:
            try:
                group = RoleGroup.objects.get(id=int(request.POST.get('group_id', 0)))
            except (ValueError, RoleGroup.DoesNotExist):
                group = None
            if group is None or not group.can_manage(user):
                message, message_class = 'You can only manage groups you created.', 'alert-box alert'
            elif action == 'delete':
                sets = [a.question_set for a in group.set_assignments.all()]
                group.delete()
                for qs in sets:
                    reconcile_group_roles(qs)
                message, message_class = 'Group deleted.', 'alert-box success'
            elif action == 'add_member':
                # The picker submits writer_id; a typed username still works as a
                # fallback.
                wid = (request.POST.get('writer_id') or '').strip()
                uname = (request.POST.get('username') or '').strip()
                w = None
                if wid.isdigit():
                    w = Writer.objects.filter(id=int(wid)).first()
                if w is None and uname:
                    w = Writer.objects.filter(user__username__iexact=uname).first()
                who = w.user.username if w else (uname or 'that user')
                if w is None:
                    message, message_class = 'No user found for "{0}".'.format(uname), 'alert-box warning'
                elif group.members.filter(id=w.id).exists():
                    message, message_class = '{0} is already a member.'.format(who), 'alert-box warning'
                else:
                    group.members.add(w)
                    RoleGroupJoinRequest.objects.filter(role_group=group, requester=w).delete()
                    reconcile_group(group)
                    _notify_added_to_group(w, group, user)
                    message, message_class = 'Added {0}.'.format(who), 'alert-box success'
            elif action in ('approve_join', 'decline_join'):
                try:
                    w = Writer.objects.get(id=int(request.POST.get('writer_id', 0)))
                except (ValueError, Writer.DoesNotExist):
                    w = None
                if w is None:
                    message, message_class = 'That user no longer exists.', 'alert-box warning'
                else:
                    RoleGroupJoinRequest.objects.filter(role_group=group, requester=w).delete()
                    if action == 'approve_join':
                        if group.members.filter(id=w.id).exists():
                            message, message_class = ('{0} is already a member.'.format(
                                w.user.username), 'alert-box info')
                        else:
                            group.members.add(w)
                            reconcile_group(group)
                            _notify_added_to_group(w, group, user)
                            message, message_class = ('Approved {0}.'.format(
                                w.user.username), 'alert-box success')
                    else:
                        message, message_class = ('Declined {0}\'s request.'.format(
                            w.user.username), 'alert-box success')
            elif action == 'remove_member':
                try:
                    w = Writer.objects.get(id=int(request.POST.get('writer_id', 0)))
                    group.members.remove(w)
                    reconcile_group(group)
                    message, message_class = 'Member removed.', 'alert-box success'
                except (ValueError, Writer.DoesNotExist):
                    pass

    groups = []
    for g in RoleGroup.objects.all().order_by('name').prefetch_related(
            'members__user', 'set_assignments__question_set', 'join_requests__requester__user'):
        member_list = list(g.members.all().order_by('user__username'))
        is_member = any(m.id == user.id for m in member_list)
        can_manage = g.can_manage(user)
        # Only a manager sees who's waiting to join.
        pending = list(g.join_requests.all()) if can_manage else []
        has_requested = any(r.requester_id == user.id for r in g.join_requests.all())
        # Show only groups this user is involved with — ones they created, belong
        # to, or have a pending request for — not every group in the system.
        if not (g.created_by_id == user.id or is_member or has_requested):
            continue
        # Membership is private: only members (and managers) see who's in a group.
        groups.append({'group': g,
                       'members': member_list if (is_member or can_manage) else None,
                       'member_count': len(member_list),
                       'assignments': list(g.set_assignments.all()),
                       'pending': pending,
                       'can_manage': can_manage,
                       'is_member': is_member,
                       'has_requested': has_requested})
    return render(request, 'role_groups.html',
                  {'groups': groups, 'message': message, 'message_class': message_class, 'user': user})


@login_required
def attach_role_group(request, qset_id):
    """Attach a role group to a set with a role (owner only); members gain the
    role. GET shows a dedicated confirmation page — an inline dropdown on the
    set page read as though the group it happened to be showing was already
    attached, so attaching is now a deliberate two-step."""
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if not qset.is_owner(user):
        return render(request, 'failure.html',
                      {'message': 'Only the set owner can attach role groups.',
                       'message_class': 'alert-box alert'})

    if request.method == 'POST':
        role = request.POST.get('role', 'writer')
        role = role if role in ('editor', 'writer') else 'writer'
        try:
            group = RoleGroup.objects.get(id=int(request.POST.get('role_group_id', 0)))
            SetRoleGroupAssignment.objects.update_or_create(
                question_set=qset, role_group=group, defaults={'role': role})
            reconcile_group_roles(qset)
            cache.clear()
            messages.success(
                request,
                '{0} attached as {1}s — its {2} member(s) now have that role on this set.'.format(
                    group.name, role, group.members.count()),
                extra_tags='alert-box success')
        except (ValueError, RoleGroup.DoesNotExist):
            messages.error(request, 'Pick a role group to attach.',
                           extra_tags='alert-box warning')
            return HttpResponseRedirect('/attach_role_group/{0}/'.format(qset_id))
        return HttpResponseRedirect('/edit_question_set/{0}/#editors'.format(qset_id))

    attached_ids = set(SetRoleGroupAssignment.objects
                       .filter(question_set=qset).values_list('role_group_id', flat=True))
    groups = [{'group': g, 'attached': g.id in attached_ids, 'members': list(g.members.all())}
              for g in RoleGroup.objects.all().order_by('name')]
    return render(request, 'attach_role_group.html',
                  {'qset': qset, 'groups': groups, 'user': user})


@login_required
def detach_role_group(request, qset_id):
    """Remove a role group from a set (owner only); group-granted members lose the role."""
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if request.method == 'POST' and qset.is_owner(user):
        try:
            SetRoleGroupAssignment.objects.filter(
                question_set=qset, role_group_id=int(request.POST.get('role_group_id', 0))).delete()
            reconcile_group_roles(qset)
            cache.clear()
        except ValueError:
            pass
    return HttpResponseRedirect('/edit_question_set/{0}/#editors'.format(qset_id))


@login_required
def edit_packet(request, packet_id):
    user = request.user.writer
    packet = Packet.objects.get(id=packet_id)
    qset = packet.question_set
    message = ''
    message_class = ''
    read_only = True
    tossup_status = []
    bonus_status = []
    can_rename = qset.is_owner(user) or user in qset.editor.all()

    if request.method == 'POST' and 'packet_name' in request.POST:
        if can_rename:
            new_name = (request.POST.get('packet_name') or '').strip()[:200]
            if not new_name:
                message, message_class = 'Packet name cannot be empty.', 'alert-box warning'
            elif Packet.objects.filter(question_set=qset, packet_name=new_name).exclude(id=packet.id).exists():
                message = 'A packet named "{0}" already exists in this set.'.format(new_name)
                message_class = 'alert-box warning'
            elif new_name != packet.packet_name:
                packet.packet_name = new_name
                packet.save(update_fields=['packet_name'])
                cache.clear()
                message, message_class = 'Packet renamed to "{0}".'.format(new_name), 'alert-box success'
        else:
            message = 'Only an owner or editor can rename packets.'
            message_class = 'alert-box alert'

    if request.method == 'POST' and 'header_note' in request.POST:
        # The note this packet's exported copy carries above its first tossup.
        # Same hands as the name: it is printed in front of a room.
        if can_rename:
            note = (request.POST.get('header_note') or '').strip()
            if note != (packet.header_note or ''):
                packet.header_note = note
                packet.save(update_fields=['header_note'])
                cache.clear()
                message = ('Packet note saved.' if note else 'Packet note removed.')
                message_class = 'alert-box success'
        else:
            message = 'Only an owner or editor can write a packet note.'
            message_class = 'alert-box alert'

    if request.method in ('GET', 'POST'):
        if qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all():
            # The table shows each question's author, editor, category, type
            # and comments. Without this the page asked the database for every
            # one of those a row at a time -- 2,500 queries for a 24-question
            # packet, which is slow anywhere and painful against a managed
            # Postgres, where each one is a network round trip.
            tossups = list(packet.tossup_set.order_by('question_number')
                           .select_related(*QUESTION_LIST_RELATED))
            bonuses = list(packet.bonus_set.order_by('question_number')
                           .select_related(*QUESTION_LIST_RELATED))
            attach_question_comments({t.id: t for t in tossups},
                                     {b.id: b for b in bonuses})
            if user not in qset.writer.all():
                read_only = False

            # Per-packet requirements, shown per top-level category AND broken
            # out by subcategory so it's clear which subcategories are needed.
            # Use the packetization quota for a path when defined, otherwise the
            # set-wide total for that path.
            num_packets = max(qset.num_packets, 1)
            quota_by_path = {e.path: e for e in PacketizationEntry.objects.filter(question_set=qset)}

            # Where this packet falls in the set's order, so the remainder is
            # handed out to the same packets every time this page is loaded.
            packet_order = [p.id for p in sorted_packets(qset)]
            packet_index = packet_order.index(packet.id) if packet.id in packet_order else 0

            def _share(total, index, n):
                """One packet's whole-number share of `total` questions.

                You can't write 1.6 tossups, and showing that as the requirement
                made the page unreadable. Split the set total into whole
                questions instead: the first `total % n` packets carry one extra,
                so every packet gets an integer and the shares still add up to
                the set total exactly.
                """
                base, extra = divmod(int(round(total)), n)
                return base + (1 if index < extra else 0)

            def _req(path, total, attr):
                quota = quota_by_path.get(path)
                if quota is not None and getattr(quota, attr) is not None:
                    # A packetization quota is a per-packet average, and may
                    # itself be fractional (2.2 = "mostly 2, sometimes 3").
                    # Scale it back to a set total and split that the same way.
                    total = float(getattr(quota, attr)) * num_packets
                return _share(total, packet_index, num_packets)

            entries = list(qset.setwidedistributionentry_set.select_related('dist_entry')
                           .order_by('dist_entry__category', 'dist_entry__subcategory'))
            by_top = {}
            for swde in entries:
                by_top.setdefault(swde.dist_entry.category, []).append(swde)

            def _sub_status(in_cat, req, parent_met):
                """How to mark a subcategory row.

                A subcategory this packet doesn't cover isn't automatically a
                failure: when the category's own total is already met, the
                shortfall is someone else's to make up — with "Other" needing 2
                and its subcategories 1/1/0 here, a packet can be complete with
                no Geography at all. Those rows read as optional rather than as
                a 0% failure.

                (A subcategory that averages under one per packet needs 0 in the
                packets it skips, so it never shows a shortfall there.)"""
                if req <= 0 or in_cat >= req:
                    return '', ''
                if parent_met:
                    return 'flex', 'This packet already meets the category total, so this subcategory is optional here.'
                return '', ''

            # How many of this packet's questions sit in each category, in one
            # query per type. The rows below want this per category and per
            # subcategory, which was two queries apiece -- 160 of them on a set
            # with eighty categories, to count a couple of dozen questions.
            def _counts_by_entry(model):
                return dict(model.objects.filter(packet=packet)
                            .values_list('category_id')
                            .annotate(n=Count('id'))
                            .values_list('category_id', 'n'))

            tu_by_entry = _counts_by_entry(Tossup)
            bs_by_entry = _counts_by_entry(Bonus)

            def _in_top(counts, entries):
                return sum(counts.get(e.dist_entry_id, 0) for e in entries)

            tossup_status = []
            bonus_status = []
            for top, swdes in by_top.items():
                tu_total = sum(s.num_tossups or 0 for s in swdes)
                bs_total = sum(s.num_bonuses or 0 for s in swdes)
                tu_in_top = _in_top(tu_by_entry, swdes)
                bs_in_top = _in_top(bs_by_entry, swdes)

                # Each entry's share for this packet, worked out first: a
                # category asks for exactly what its parts add up to, so the
                # rows can't disagree (rounding each separately let a category
                # ask for fewer questions than the subcategories beneath it).
                entry_reqs = {}
                for s in swdes:
                    de = s.dist_entry
                    path = ('{0} - {1}'.format(de.category, de.subcategory)
                            if de.subcategory else de.category)
                    entry_reqs[s.id] = (
                        _req(path, s.num_tossups or 0, 'min_tossups'),
                        _req(path, s.num_bonuses or 0, 'min_bonuses'))

                def _top_req(attr, total, part_index):
                    quota = quota_by_path.get(top)
                    if quota is not None and getattr(quota, attr) is not None:
                        return _req(top, total, attr)   # an explicit quota wins
                    return sum(v[part_index] for v in entry_reqs.values())

                tu_top_req = _top_req('min_tossups', tu_total, 0)
                bs_top_req = _top_req('min_bonuses', bs_total, 1)
                tu_top_met = tu_in_top >= tu_top_req
                bs_top_met = bs_in_top >= bs_top_req
                tossup_status.append({'label': top, 'is_sub': False,
                                      'tu_req': tu_top_req, 'tu_in_cat': tu_in_top})
                bonus_status.append({'label': top, 'is_sub': False,
                                     'bs_req': bs_top_req, 'bs_in_cat': bs_in_top})
                # Subcategory detail rows (only when the category has subcategories).
                for swde in swdes:
                    de = swde.dist_entry
                    if not de.subcategory:
                        continue
                    tu_req, bs_req = entry_reqs[swde.id]
                    tu_in = tu_by_entry.get(de.id, 0)
                    tu_state, tu_note = _sub_status(tu_in, tu_req, tu_top_met)
                    tossup_status.append({
                        'label': de.subcategory, 'is_sub': True,
                        'tu_req': tu_req, 'tu_in_cat': tu_in,
                        'state': tu_state, 'note': tu_note})
                    bs_in = bs_by_entry.get(de.id, 0)
                    bs_state, bs_note = _sub_status(bs_in, bs_req, bs_top_met)
                    bonus_status.append({
                        'label': de.subcategory, 'is_sub': True,
                        'bs_req': bs_req, 'bs_in_cat': bs_in,
                        'state': bs_state, 'note': bs_note})


        else:
            message = 'You are not authorized to view or edit this packet!'
            message_class = 'alert-box alert'
            tossups = None
            bonuses = None

    return render(request, 'edit_packet.html',
        {'qset': qset,
         'packet': packet,
         'message': message,
         'message_class': message_class,
         'tossups': tossups,
         'bonuses': bonuses,
         'tossup_status': tossup_status,
         'bonus_status': bonus_status,
         'role': get_role_no_owner(user, qset),
         'read_only': read_only,
         'can_rename': can_rename,
         'user': user})

@login_required
def _last_category_for(user, qset):
    """The category the writer used most recently in this set, to start the
    next Add Tossup / Add Bonus form on. A writer works through one category
    at a time, so the last one saved is the likeliest next; nothing is stored,
    it is read off the newest question they wrote or changed here. None if
    they have none yet or it no longer belongs to the set's distribution."""
    latest = None
    for model in (Tossup, Bonus):
        q = (model.objects.filter(question_set=qset, author=user, category__isnull=False)
             .order_by('-last_changed_date', '-id').first())
        if q is not None and (latest is None or q.last_changed_date > latest.last_changed_date):
            latest = q
    if latest is None:
        return None
    if qset.distribution_id and latest.category.distribution_id != qset.distribution_id:
        return None
    return latest.category


def _requested_category(qset, request):
    """The category an add-a-question link asked to start on, or None.

    The category and category-tag pages link here so that "this needs two more
    tossups" and writing one are the same gesture. A page that knows the exact
    distribution entry sends `category` as its id; one that knows only the path
    its tags sit on sends `category_path`, which names a category only when it
    matches one exactly -- a tag on "Literature" covers several, and picking one
    of them on the writer's behalf would be worse than leaving the picker as it
    was. Either way the id is checked against this set's own distribution.
    """
    raw = (request.GET.get('category') or '').strip()
    if raw.isdigit():
        return DistributionEntry.objects.filter(
            id=int(raw), distribution_id=qset.distribution_id).first()

    path = (request.GET.get('category_path') or '').strip()
    if not path:
        return None
    entries = list(DistributionEntry.objects.filter(distribution_id=qset.distribution_id))
    exact = [e for e in entries if str(e) == path]
    if len(exact) == 1:
        return exact[0]
    # A top-level category with a single entry under it is still unambiguous.
    under = [e for e in entries if (e.category or '').strip() == path]
    return under[0] if len(under) == 1 else None


def _add_initial(user, qset, question_type_id, requested=None):
    """Opening values for an add-a-question form: the category asked for by the
    link that got here, else the last one this writer used."""
    initial = {'question_type': question_type_id}
    entry = requested if requested is not None else _last_category_for(user, qset)
    if entry is not None:
        initial['category'] = entry.id
    return initial


def add_tossups(request, qset_id, packet_id=None):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    message = ''
    message_class = ''
    tossup = None
    read_only = True
    question_type_id = []
    tossup_form = []

    if (QuestionType.objects.exists()):
        question_type_id = QuestionType.objects.get(question_type=ACF_STYLE_TOSSUP)

    if request.method == 'GET':
        if user in qset.editor.all() or user in qset.writer.all() or qset.is_owner(user):
            initial = _add_initial(user, qset, question_type_id,
                                   _requested_category(qset, request))
            if user in qset.writer.all() and user not in qset.editor.all() and not qset.is_owner(user):
                tossup_form = TossupForm(qset_id=qset.id, packet_id=packet_id, role='writer', writer=user.user.username, initial=initial)
            else:
                tossup_form = TossupForm(qset_id=qset.id, packet_id=packet_id, writer=user.user.username, initial=initial)
            read_only = False
        else:
            tossup_form = []
            message = 'You are not authorized to add questions to this tournament!'
            message_class = 'alert-box warning'
            read_only = True

        return render(request, 'add_tossups.html',
            {'form': tossup_form,
             'message': message,
             'message_class': message_class,
             'read_only': read_only,
             'user': user,
             'qset': qset})

    elif request.method == 'POST':
        if user in qset.editor.all() or user in qset.writer.all() or qset.is_owner(user):
            read_only = False

            # The user may have set the packet ID through the POST body, so check for it there
            if packet_id == None and 'packet' in request.POST and request.POST['packet'] != '':
                packet_id = int(request.POST['packet'])
            tossup_form = TossupForm(request.POST, qset_id=qset.id, packet_id=packet_id, writer=user.user.username)

            if tossup_form.is_valid():
                tossup = tossup_form.save(commit=False)
                if (tossup.author is None):
                    tossup.author = user
                tossup.question_set = qset
                tossup.tossup_text = strip_markup(tossup.tossup_text)
                # A trailing "(note)" after the [...] section is an editorial
                # aside, not a pronunciation guide — escape it so it renders as
                # text. New questions only; an edit leaves the answer as typed.
                tossup.tossup_answer = escape_answer_note_parens(strip_markup(tossup.tossup_answer))
                tossup.locked = False

                try:
                    tossup.is_valid()

                    if packet_id is None or packet_id == '':
                        # If the tossup doesn't have a packet, set its number to be the magic number
                        # of 999, meaning that it's unassigned.  Can't assign -1 because this is outside
                        # of the legal range of tossup numbers and it ends up getting set to 1 for some
                        # reason, except in the case where there are no packets in the system in which
                        # case there's an error adding the question
                        tossup.question_number = 999
                    else:
                        tossup.packet_id = packet_id
                        tossup.question_number = _next_question_number(Tossup, packet_id)

                    tossup.save_question(edit_type=QUESTION_CREATE, changer=user)
                    # Tags ticked on the add page, so a question arrives tagged
                    # rather than needing a second visit to its edit page.
                    save_tag_selection(request, qset, tossup, tossup.category, is_tossup=True)
                    cache.clear()
                    # Straight to the new question's edit page, where a one-time
                    # panel reports its style and repeat checks (?new=1).
                    return HttpResponseRedirect('/edit_tossup/{0}/?new=1'.format(tossup.id))

                except InvalidTossup as ex:
                    message = str(ex)
                    message_class = 'alert-box warning'

            else:
                message = 'Problem adding a tossup.  Make sure that all required fields are filled out!'
                message_class = 'alert-box warning'

        else:
            tossup = None
            message = 'You are not authorized to add questions to this tournament!'
            message_class = 'alert-box warning'
            tossup_form = []
            read_only = True
            
        if (tossup_form is None):
            tossup_form = TossupForm(qset_id=qset.id, packet_id=packet_id, initial=_add_initial(user, qset, question_type_id))

        # In the error case, return the whole tossup object so you can edit it
        return render(request, 'add_tossups.html',
                 {'form': tossup_form,
                 'message': message,
                 'message_class': message_class,
                 'tossup' : tossup,
                 'tossup_id': None,
                 'read_only': read_only,
                 'user': user,
                 'qset': qset})

    else:
        return render(request, 'failure.html',
            {'message': 'The request cannot be completed as specified',
             'message_class': 'alert-box alert'})

@login_required
def add_bonuses(request, qset_id, bonus_type, packet_id=None):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    message = ''
    message_class = ''
    read_only = True
    role = get_role_no_owner(user, qset)
    question_type_id = []
    bonus_form = []

    if (QuestionType.objects.exists()):
        if (bonus_type == VHSL_BONUS):
            question_type_id = QuestionType.objects.get(question_type=VHSL_BONUS)
        elif (bonus_type == ACF_STYLE_BONUS):
            question_type_id = QuestionType.objects.get(question_type=ACF_STYLE_BONUS)
        else:
            return render(request, 'failure.html',
                {'message': 'The request cannot be completed as specified.  Bonus type is invalid.',
                 'message_class': 'alert-box alert'})

    if request.method == 'GET':
        if user in qset.editor.all() or user in qset.writer.all() or qset.is_owner(user):
            form = BonusForm(qset_id=qset.id, packet_id=packet_id, role=role,
                             initial=_add_initial(user, qset, question_type_id,
                                                  _requested_category(qset, request)),
                             writer=user.user.username, question_type=bonus_type)
            read_only = False
        else:
            form = None
            message = 'You are not authorized to add questions to this tournament!'
            message_class = 'alert-box warning'
            read_only = True

        return render(request, 'add_bonuses.html',
            {'form': form,
             'message': message,
             'message_class': message_class,
             'read_only': read_only,
             'question_type': bonus_type,
             'user': user,
             'qset': qset})

    elif request.method == 'POST':
        bonus = None
        if user in qset.editor.all() or user in qset.writer.all() or qset.is_owner(user):
            bonus_form = BonusForm(request.POST, qset_id=qset.id, packet_id=packet_id, initial={'question_type': question_type_id}, writer=user.user.username, question_type=bonus_type)
            read_only = False

            if bonus_form.is_valid():
                bonus = bonus_form.save(commit=False)
                if (bonus.author is None):
                    bonus.author = user

                bonus.question_set = qset
                bonus.leadin = strip_markup(bonus.leadin)
                bonus.part1_text = strip_markup(bonus.part1_text)
                bonus.part2_text = strip_markup(bonus.part2_text)
                bonus.part3_text = strip_markup(bonus.part3_text)
                # See add_tossups: a trailing "(note)" after the [...] section
                # is an aside, not a pronunciation guide. New bonuses only.
                bonus.part1_answer = escape_answer_note_parens(strip_markup(bonus.part1_answer))
                bonus.part2_answer = escape_answer_note_parens(strip_markup(bonus.part2_answer))
                bonus.part3_answer = escape_answer_note_parens(strip_markup(bonus.part3_answer))
                bonus.locked = False

                if packet_id is None or packet_id == '':
                    # If the bonus doesn't have a packet, set its number to be the magic number
                    # of 999, meaning that it's unassigned.  Can't assign -1 because this is outside
                    # of the legal range of bonus numbers and it ends up getting set to 1 for some
                    # reason, except in the case where there are no packets in the system in which
                    # case there's an error adding the question
                    bonus.question_number = 999
                else:
                    bonus.packet_id = packet_id
                    bonus.question_number = _next_question_number(Bonus, packet_id)

                try:
                    bonus.is_valid()
                    bonus.save_question(edit_type=QUESTION_CREATE, changer=user)
                    save_tag_selection(request, qset, bonus, bonus.category, is_tossup=False)
                    cache.clear()
                    # As with tossups: land on the new question with its checks.
                    return HttpResponseRedirect('/edit_bonus/{0}/?new=1'.format(bonus.id))

                except InvalidBonus as ex:
                    message = str(ex)
                    message_class = 'alert-box alert'

            else:
                message = 'There was an error with the form: ' + str(bonus_form.errors)
                message_class = 'alert-box alert'

            read_only = False
        else:
            message = 'You are not authorized to add questions to this tournament!'
            message_class = 'alert-box alert'
            bonus_form = []
            bonus = None
            read_only = True

        if (bonus_form is None):
            bonus_form = BonusForm(qset_id=qset.id, packet_id=packet_id, initial=_add_initial(user, qset, question_type_id), writer=user.user.username)

        return render(request, 'add_bonuses.html',
                 {'form': bonus_form,
                 'message': message,
                 'message_class': message_class,
                 'bonus': bonus,
                 'bonus_id': None,
                 'read_only': read_only,
                 'question_type': bonus_type,
                 'user': user,
                 'qset': qset})

    else:
        return render(request, 'failure.html',
            {'message': 'The request cannot be completed as specified',
             'message_class': 'alert-box alert'})


#########################################################################
# Suggested edits (track-changes style proposals on questions)
#########################################################################

SUGGESTABLE_FIELDS = {
    'tossup': [('tossup_text', 'Tossup Text'), ('tossup_answer', 'Answer')],
    'bonus': [('leadin', 'Leadin'),
              ('part1_text', 'Part 1'), ('part1_answer', 'Part 1 Answer'),
              ('part2_text', 'Part 2'), ('part2_answer', 'Part 2 Answer'),
              ('part3_text', 'Part 3'), ('part3_answer', 'Part 3 Answer')],
}


def _is_set_member(user, qset):
    return qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()


def _can_review_suggestions(user, question, qset):
    """The question's author, or any editor/owner, may accept/reject suggestions."""
    return (getattr(question, 'author_id', None) == user.id
            or qset.is_owner(user) or user in qset.editor.all())


def _pending_suggestions(question, qtype):
    if question is None:
        return []
    return list(SuggestedEdit.objects.filter(
        question_type=qtype, question_id=question.id, status='pending')
        .select_related('suggested_by__user'))


def _suggestion_render_ctx(user, question, qtype, qset):
    if question is None:
        return {'pending_suggestions': [], 'can_review_suggestions': False,
                'suggest_fields': [], 'can_suggest': False}
    return {
        'pending_suggestions': _pending_suggestions(question, qtype),
        'can_review_suggestions': _can_review_suggestions(user, question, qset),
        'suggest_fields': [{'name': f, 'label': lbl, 'value': getattr(question, f, '') or ''}
                           for f, lbl in SUGGESTABLE_FIELDS.get(qtype, [])],
        'can_suggest': _is_set_member(user, qset),
    }


def _apply_suggestion(question, suggestion, user):
    setattr(question, suggestion.field, suggestion.new_value)
    question.save_question(edit_type=QUESTION_CHANGE, changer=user)
    suggestion.status = 'accepted'
    suggestion.resolved_by = user
    suggestion.resolved_date = timezone.now()
    suggestion.save()
    # Other pending suggestions for the same field were diffed against the old
    # text, so they no longer apply cleanly — mark them superseded.
    SuggestedEdit.objects.filter(
        question_type=suggestion.question_type, question_id=suggestion.question_id,
        field=suggestion.field, status='pending').exclude(id=suggestion.id).update(
        status='superseded', resolved_by=user, resolved_date=timezone.now())


def _record_suggestions(qset, question, qtype, user, proposed, note=''):
    """Create a pending SuggestedEdit for each content field whose proposed value
    differs from the current one. `proposed` maps field name -> raw new value."""
    created = 0
    for f, lbl in SUGGESTABLE_FIELDS.get(qtype, []):
        if f not in proposed:
            continue
        new_val = strip_markup(proposed.get(f, '') or '')
        old_val = getattr(question, f, '') or ''
        if (new_val or '').strip() != (old_val or '').strip():
            SuggestedEdit.objects.create(
                question_set=qset, question_type=qtype, question_id=question.id,
                field=f, field_label=lbl, old_value=old_val, new_value=new_val,
                note=note, suggested_by=user)
            created += 1
    return created


@login_required
def suggest_edit(request):
    """Any set member proposes changes to a question's fields (track-changes).
    Used by the standalone suggest form; the edit pages now post the main form
    with save_as_suggestion instead."""
    user = request.user.writer
    if request.method != 'POST':
        return HttpResponseRedirect('/')
    qtype = request.POST.get('question_type', '')
    question = _style_question(qtype, request.POST.get('question_id', ''))
    if question is None:
        return render(request, 'failure.html',
                      {'message': 'No such question.', 'message_class': 'alert-box alert'})
    qset = question.question_set
    if not _is_set_member(user, qset):
        return render(request, 'failure.html',
                      {'message': 'You must be a member of this set to suggest changes.',
                       'message_class': 'alert-box alert'})
    note = (request.POST.get('note') or '').strip()[:255]
    proposed = {f: request.POST.get('field_' + f, '')
                for f, _ in SUGGESTABLE_FIELDS.get(qtype, []) if ('field_' + f) in request.POST}
    created = _record_suggestions(qset, question, qtype, user, proposed, note)
    return HttpResponseRedirect('/edit_{0}/{1}/?suggested={2}#suggested-changes'.format(
        qtype, question.id, created))


@login_required
def resolve_suggestion(request):
    """The question's author or an editor accepts/rejects a single suggestion."""
    user = request.user.writer
    if request.method != 'POST':
        return HttpResponse(json.dumps({'ok': False, 'error': 'POST required'}))
    try:
        s = SuggestedEdit.objects.get(id=int(request.POST['suggestion_id']))
    except (KeyError, ValueError, SuggestedEdit.DoesNotExist):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Not found'}))
    question = _style_question(s.question_type, s.question_id)
    if question is None or not _can_review_suggestions(user, question, s.question_set):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Not authorized'}))
    if s.status != 'pending':
        return HttpResponse(json.dumps({'ok': False, 'error': 'Already resolved'}))
    action = request.POST.get('action', '')
    if action == 'accept':
        _apply_suggestion(question, s, user)
    elif action == 'reject':
        s.status = 'rejected'
        s.resolved_by = user
        s.resolved_date = timezone.now()
        s.save()
    else:
        return HttpResponse(json.dumps({'ok': False, 'error': 'Bad action'}))
    cache.clear()
    return HttpResponse(json.dumps({'ok': True}))


@login_required
def resolve_all_suggestions(request):
    """Accept or reject every pending suggestion on a question at once."""
    user = request.user.writer
    if request.method != 'POST':
        return HttpResponse(json.dumps({'ok': False, 'error': 'POST required'}))
    qtype = request.POST.get('question_type', '')
    question = _style_question(qtype, request.POST.get('question_id', ''))
    if question is None or not _can_review_suggestions(user, question, question.question_set):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Not authorized'}))
    action = request.POST.get('action', '')
    pending = SuggestedEdit.objects.filter(
        question_type=qtype, question_id=question.id, status='pending')
    if action == 'accept':
        for s in list(pending):
            s.refresh_from_db()
            if s.status == 'pending':
                _apply_suggestion(question, s, user)
    elif action == 'reject':
        pending.update(status='rejected', resolved_by=user, resolved_date=timezone.now())
    else:
        return HttpResponse(json.dumps({'ok': False, 'error': 'Bad action'}))
    cache.clear()
    return HttpResponse(json.dumps({'ok': True}))


def _structure_preview(answer, question_text='', is_tossup=True):
    """How a prose answer line was understood, for the type-questions preview:
    the structure, the line it would print as, and anything worth warning about.

    Questions typed or pasted in bulk arrive as prose, so this is the "do its
    best" path — the writer sees the reading before committing, rather than
    finding out later that a prompt was read as part of the primary answer.
    """
    structure = answer_structure.parse_line(answer, is_tossup)
    return {
        'structure': structure,
        'line': answer_structure.format_line(structure),
        'problems': answer_structure.validate(structure, question_text, is_tossup),
        'interesting': answer_structure.has_content(structure),
    }


def _attach_structure_previews(qset, tossups, bonuses):
    """Hang a structure preview on each unsaved question in the upload preview.
    A no-op for sets that don't record structure."""
    if not getattr(qset, 'structured_answers', False):
        return
    for tossup in tossups:
        tossup.structured_preview = _structure_preview(
            tossup.tossup_answer, tossup.tossup_text, is_tossup=True)
    for bonus in bonuses:
        bonus.structured_preview = [
            dict(_structure_preview(getattr(bonus, 'part{0}_answer'.format(part), '') or '',
                                    is_tossup=False),
                 part=part, label='Part {0}'.format(part))
            for part in (1, 2, 3)
            if (getattr(bonus, 'part{0}_answer'.format(part), '') or '').strip()
        ]


def _attach_preview_checks(qset, tossups, bonuses):
    """Run the style check and the repeat check over the parsed questions on the
    upload preview and hang the results on each one, so a writer can fix what
    they say while the text is still editable instead of finding out on each
    question's edit page after the batch is committed.

    Same rules as the edit page's panels. Set-wide rule dismissals apply;
    per-question dismissals can't, since these questions don't exist yet.
    Repeats cover both the questions already in the set and the rest of this
    batch -- two questions typed together share no saved answer to match on, so
    nothing downstream would catch that pair until both were in.

    Returns a summary count for the header, {'style': n, 'repeat': n}.
    """
    from . import style_checker
    disabled = qset.disabled_style_rule_set()
    rule_dismissed = set(StyleRuleDismissal.objects.filter(
        question_set=qset).values_list('code', 'token'))
    index = build_answer_index(qset)

    questions = ([('tossup', 'Tossup', i, tu) for i, tu in enumerate(tossups or [], start=1)]
                 + [('bonus', 'Bonus', i, bs) for i, bs in enumerate(bonuses or [], start=1)])

    # Every answer in the batch, keyed the way the set index is keyed, so a
    # repeat inside the batch is found the same way one against the set is. The
    # raw answer is kept alongside so a match can be shown as it was typed.
    batch = []
    for qtype, noun, number, question in questions:
        raws = ((question.tossup_answer,) if qtype == 'tossup'
                else (question.part1_answer, question.part2_answer, question.part3_answer))
        answers = {}
        for raw in raws:
            norm = normalize_answer(raw)
            if norm:
                answers.setdefault(norm, raw)
        batch.append({'label': '{0} {1}'.format(noun, number), 'norms': set(answers),
                      'answers': answers})

    style_count = 0
    repeat_count = 0
    for pos, (qtype, noun, number, question) in enumerate(questions):
        found = (style_checker.check_tossup(question, style_checker.DEFAULT_GUIDE, disabled)
                 if qtype == 'tossup'
                 else style_checker.check_bonus(question, style_checker.DEFAULT_GUIDE, disabled))
        question.style_issues = [i for i in found
                                 if (i['code'], i.get('token', '')) not in rule_dismissed]
        style_count += len(question.style_issues)

        mine = batch[pos]['norms']
        question.dup_matches = []
        if mine:
            for m in lookup_answer_matches(index, mine):
                m = dict(m, answer_html=_dup_render_answer(m['answer_raw']))
                question.dup_matches.append(m)

        question.batch_repeats = []
        for other_pos, other in enumerate(batch):
            if other_pos == pos:
                continue
            shared = mine & other['norms']
            if shared:
                norm = sorted(shared)[0]
                question.batch_repeats.append(
                    {'label': other['label'],
                     'answer_html': _dup_render_answer(other['answers'][norm])})
        repeat_count += len(question.dup_matches) + len(question.batch_repeats)

    return {'style': style_count, 'repeat': repeat_count}


def _structured_tossup_ctx(qset, tossup):
    """Template context for a tossup's structured answer editor. Empty when the
    set doesn't record structure, which leaves the plain answer box in place."""
    if tossup is None or not getattr(qset, 'structured_answers', False):
        return {}
    structure = tossup.answer_structure()
    return {
        'structured_answers_on': True,
        'structured_answer': structure,
        'structured_answer_line': answer_structure.format_line(structure),
        'structured_answer_problems': tossup.answer_structure_problems(),
    }


def _structured_bonus_ctx(qset, bonus):
    """The same for a bonus, one block per part. A bonus part can't say "until
    X is read" — there is no shared text to read up to."""
    if bonus is None or not getattr(qset, 'structured_answers', False):
        return {}
    parts = []
    for part in (1, 2, 3):
        structure = bonus.answer_structure(part)
        parts.append({
            'part': part,
            'prefix': 'part{0}'.format(part),
            'structure': structure,
            'line': answer_structure.format_line(structure),
        })
    return {
        'structured_answers_on': True,
        'structured_parts': parts,
        'structured_answer_problems': bonus.answer_structure_problems(),
    }


def _with_structured_answers(request, qset, fields):
    """`request.POST`, with each answer field rebuilt from the structured
    editor's rows, plus the structures themselves.

    The printed line stays canonical, so the form validates and everything
    downstream reads exactly what it did before — the structure is what the
    editor typed, and the line is what it prints as.
    """
    if not getattr(qset, 'structured_answers', False):
        return request.POST, {}

    post = None
    structures = {}
    for field, prefix, is_tossup in fields:
        structure = answer_structure.posted(request.POST, prefix, is_tossup)
        if structure is None:
            continue
        if post is None:
            post = request.POST.copy()
        post[field] = answer_structure.format_line(structure)
        structures[field] = structure

    return (post if post is not None else request.POST), structures


@login_required
def edit_tossup(request, tossup_id):
    user = request.user.writer
    tossup = Tossup.objects.get(id=tossup_id)
    tossup_length = tossup.character_count()
    qset = tossup.question_set
    packet = tossup.packet
    message = ''
    message_class = ''
    read_only = True
    dup_matches = []
    role = get_role_no_owner(user, qset)

    if request.method == 'GET':
        if user == tossup.author or qset.is_owner(user) or user in qset.editor.all():
            form = TossupForm(instance=tossup, qset_id=qset.id, role=role)
            if user == tossup.author and not qset.is_owner(user) and not user in qset.editor.all() and tossup.locked:
                read_only = True
                message = 'This tossup has been locked by an editor. It cannot be changed except by another editor.'
                message_class = 'alert-box warning'
            else:
                read_only = False

        elif _is_set_member(user, qset):
            # Members who can't edit directly can still propose suggested changes,
            # so render the form (the template hides the direct Save button).
            read_only = True
            form = TossupForm(instance=tossup, qset_id=qset.id, role=role)
        else:
            read_only = True
            tossup = None
            form = None
            message = 'You are not authorized to view or edit this question!'
            message_class = 'alert-box alert'

        # Arrived straight from Add a Tossup / a one-question Type Questions:
        # report the style and repeat checks once, at the top of the page.
        new_checks = None
        if request.GET.get('new') and tossup is not None:
            new_checks = _new_question_checks(qset, tossup, 'tossup')

        if request.GET.get('suggested') is not None and tossup is not None:
            if request.GET.get('suggested') != '0':
                message = '{0} change(s) saved as suggestions for the author/editors to review.'.format(request.GET.get('suggested'))
                message_class = 'alert-box success'
            else:
                message = 'No changes were detected, so nothing was suggested.'
                message_class = 'alert-box warning'

        return render(request, 'edit_tossup.html',
            {'tossup': tossup,
             'packet_nav': _packet_neighbors(tossup, 'tossup'),
             'tossup_length': tossup_length,
             'form': form,
             'qset': qset,
             'packet': packet,
             'available_tags': build_tag_checkboxes(qset, tossup, tossup.category if tossup else None),
             'message': message,
             'message_class': message_class,
             'read_only': read_only,
             'role': role,
             'playtest': _question_buzz_data(tossup, 'tossup'),
             'discord_threads': tossup.discord_threads.order_by('created_date'),
             'new_checks': new_checks,
             'constraint_qtype': 'tossup',
             'constraint_qid': tossup.id if tossup else None,
             'constraint_rows': (_constraint_rows(qset, 'tossup', tossup.id)
                                 if tossup else []),
             'user': user,
             **_structured_tossup_ctx(qset, tossup),
             **_suggestion_render_ctx(user, tossup, 'tossup', qset)})

    elif request.method == 'POST':
        print("start post for edit tossup")
        if 'save_as_suggestion' in request.POST:
            if not _is_set_member(user, qset):
                return render(request, 'failure.html',
                              {'message': 'You must be a member of this set to suggest changes.',
                               'message_class': 'alert-box alert'})
            proposed = {f: request.POST.get(f, '') for f, _ in SUGGESTABLE_FIELDS['tossup']}
            note = (request.POST.get('suggestion_note') or '').strip()[:255]
            created = _record_suggestions(qset, tossup, 'tossup', user, proposed, note)
            return HttpResponseRedirect(
                '/edit_tossup/{0}/?suggested={1}#suggested-changes'.format(tossup.id, created))

        if user == tossup.author or qset.is_owner(user) or user in qset.editor.all():
            # Pass instance so the current author stays a valid choice even when
            # they aren't a member of this set (imported/moved questions).
            post_data, posted_structures = _with_structured_answers(
                request, qset, [('tossup_answer', 'answer', True)])
            form = TossupForm(post_data, instance=tossup, qset_id=qset.id, role=role)
            can_change = True
            if tossup.locked and not (qset.is_owner(user) or user in qset.editor.all()):
                can_change = False
            # Stay in edit mode (not suggest/read-only) on a form error so an
            # owner/editor sees the form and the error, not the suggest panel.
            read_only = not can_change

            if form.is_valid() and can_change:
                read_only = False

                is_tossup_already_edited = tossup.edited
                is_tossup_already_proofread = tossup.proofread
                is_tossup_already_read_carefully = tossup.read_carefully

                tossup.tossup_text = strip_markup(form.cleaned_data['tossup_text'])
                tossup.tossup_answer = strip_markup(form.cleaned_data['tossup_answer'])
                # Stored beside the line it prints as, never instead of it
                if 'tossup_answer' in posted_structures:
                    tossup.tossup_answer_structure = posted_structures['tossup_answer']
                # all_power is a plain checkbox (present only when checked), so
                # every save records an explicit True/False override.
                tossup.all_power = 'all_power' in request.POST
                tossup.category = form.cleaned_data['category']
                tossup.packet = form.cleaned_data['packet']
                tossup.locked = form.cleaned_data['locked']
                tossup.edited = form.cleaned_data['edited']
                tossup.proofread = form.cleaned_data['proofread']
                tossup.read_carefully = form.cleaned_data['read_carefully']
                tossup.question_type = form.cleaned_data['question_type']
                tossup.author = form.cleaned_data['author']
                print("trying to save tossup")

                try:
                    tossup.is_valid()
                    change_type = QUESTION_CHANGE
                    if (not is_tossup_already_edited and tossup.edited == True):
                        change_type = QUESTION_EDIT

                    if (not is_tossup_already_proofread and tossup.proofread == True):
                        change_type = QUESTION_PROOFREAD

                    if (not is_tossup_already_read_carefully and tossup.read_carefully == True):
                        change_type = QUESTION_READ_CAREFULLY

                    tossup.save_question(edit_type=change_type, changer=user)
                    save_tag_selection(request, qset, tossup, tossup.category, is_tossup=True)
                    tossup_length = tossup.character_count()
                    cache.clear()
                    print("Tossup saved")
                    message = 'Your changes have been saved!'
                    message_class = 'alert-box success'
                    dup_matches = _post_submit_dup_matches(qset, tossup, 'tossup')

                except InvalidTossup as ex:
                    message = str(ex)
                    message_class = 'alert-box warning'

            elif form.is_valid() and not can_change:
                message = 'This tossup is locked and can only be changed by an editor!'
                message_class = 'alert-box warning'
                read_only = True
            else:
                message = 'There was an error with the form: ' + str(form.errors)
                message_class = 'alert-box warning'


        elif user in qset.writer.all():
            read_only = True
            form = None
            message = 'You are only authorized to view, not to edit, this question!'
            message_class = 'alert-box warning'
        else:
            read_only = True
            tossup = None
            message = 'You are not authorized to view or edit this question!'
            message_class = 'alert-box alert'

        return render(request, 'edit_tossup.html',
            {'tossup': tossup,
             'packet_nav': _packet_neighbors(tossup, 'tossup'),
             'tossup_length': tossup_length,
             'form': form,
             'role': role,
             'qset': qset,
             'packet': packet,
             'available_tags': build_tag_checkboxes(qset, tossup, tossup.category if tossup else None),
             'message': message,
             'message_class': message_class,
             'dup_matches': dup_matches,
             'read_only': read_only,
             'playtest': _question_buzz_data(tossup, 'tossup'),
             'discord_threads': tossup.discord_threads.order_by('created_date'),
             'user': user,
             **_structured_tossup_ctx(qset, tossup),
             **_suggestion_render_ctx(user, tossup, 'tossup', qset)})

@login_required
def edit_bonus(request, bonus_id):
    user = request.user.writer
    bonus = Bonus.objects.get(id=bonus_id)
    char_count = bonus.character_count()
    qset = bonus.question_set
    packet = bonus.packet
    message = ''
    message_class = ''
    read_only = True
    dup_matches = []
    role = get_role_no_owner(user, qset)

    question_type = ACF_STYLE_BONUS
    if (bonus.question_type is not None):
        question_type = bonus.question_type.question_type
        
    if request.method == 'GET':
        if user == bonus.author or qset.is_owner(user) or user in qset.editor.all():
            form = BonusForm(instance=bonus, qset_id=qset.id, role=role, question_type=question_type)
            if user == bonus.author and not qset.is_owner(user) and not user in qset.editor.all() and bonus.locked:
                read_only = True
                message = 'This bonus has been locked by an editor. It cannot be changed except by another editor.'
                message_class = 'alert-box warning'
            else:
                read_only = False

        elif _is_set_member(user, qset):
            # Members who can't edit directly can still propose suggested changes.
            read_only = True
            form = BonusForm(instance=bonus, qset_id=qset.id, role=role, question_type=question_type)
        else:
            read_only = True
            bonus = None
            form = None
            message = 'You are not authorized to view or edit this question!'
            message_class = 'alert-box alert'

        # Arrived straight from Add a Bonus / a one-question Type Questions.
        new_checks = None
        if request.GET.get('new') and bonus is not None:
            new_checks = _new_question_checks(qset, bonus, 'bonus')

        if request.GET.get('suggested') is not None and bonus is not None:
            if request.GET.get('suggested') != '0':
                message = '{0} change(s) saved as suggestions for the author/editors to review.'.format(request.GET.get('suggested'))
                message_class = 'alert-box success'
            else:
                message = 'No changes were detected, so nothing was suggested.'
                message_class = 'alert-box warning'

        return render(request, 'edit_bonus.html',
            {'bonus': bonus,
             'packet_nav': _packet_neighbors(bonus, 'bonus'),
             'new_checks': new_checks,
             'constraint_qtype': 'bonus',
             'constraint_qid': bonus.id if bonus else None,
             'constraint_rows': (_constraint_rows(qset, 'bonus', bonus.id)
                                 if bonus else []),
             'char_count': char_count,
             'question_type': question_type,
             'form': form,
             'qset': qset,
             'packet': packet,
             'available_tags': build_tag_checkboxes(qset, bonus, bonus.category if bonus else None),
             'message': message,
             'message_class': message_class,
             'read_only': read_only,
             'role': role,
             'playtest': _question_buzz_data(bonus, 'bonus'),
             'discord_threads': bonus.discord_threads.order_by('created_date'),
             'user': user,
             **_structured_bonus_ctx(qset, bonus),
             **_suggestion_render_ctx(user, bonus, 'bonus', qset)})

    elif request.method == 'POST':
        if 'save_as_suggestion' in request.POST:
            if not _is_set_member(user, qset):
                return render(request, 'failure.html',
                              {'message': 'You must be a member of this set to suggest changes.',
                               'message_class': 'alert-box alert'})
            proposed = {f: request.POST.get(f, '') for f, _ in SUGGESTABLE_FIELDS['bonus']}
            note = (request.POST.get('suggestion_note') or '').strip()[:255]
            created = _record_suggestions(qset, bonus, 'bonus', user, proposed, note)
            return HttpResponseRedirect(
                '/edit_bonus/{0}/?suggested={1}#suggested-changes'.format(bonus.id, created))

        if user == bonus.author or qset.is_owner(user) or user in qset.editor.all():
            # Pass instance so the current author stays a valid choice even when
            # they aren't a member of this set (imported/moved questions).
            post_data, posted_structures = _with_structured_answers(
                request, qset,
                [('part1_answer', 'part1', False),
                 ('part2_answer', 'part2', False),
                 ('part3_answer', 'part3', False)])
            form = BonusForm(post_data, instance=bonus, qset_id=qset.id, role=role, question_type=question_type)

            can_change = True
            if bonus.locked and not (qset.is_owner(user) or user in qset.editor.all()):
                can_change = False
            # Stay in edit mode (not suggest/read-only) even if the form has an
            # error, so an owner/editor sees the form and the error, not the
            # "you can't edit this directly" panel.
            read_only = not can_change

            if form.is_valid() and can_change:
                is_bonus_already_edited = bonus.edited
                is_bonus_already_proofread = bonus.proofread
                is_bonus_already_read_carefully = bonus.read_carefully

                bonus.leadin = strip_markup(form.cleaned_data['leadin'])
                bonus.part1_text = strip_markup(form.cleaned_data['part1_text'])
                bonus.part1_answer = strip_markup(form.cleaned_data['part1_answer'])
                bonus.part2_text = strip_markup(form.cleaned_data['part2_text'])
                bonus.part2_answer = strip_markup(form.cleaned_data['part2_answer'])
                bonus.part3_text = strip_markup(form.cleaned_data['part3_text'])
                bonus.part3_answer = strip_markup(form.cleaned_data['part3_answer'])
                # Stored beside the lines they print as, never instead of them
                for part in (1, 2, 3):
                    field = 'part{0}_answer'.format(part)
                    if field in posted_structures:
                        setattr(bonus, field + '_structure', posted_structures[field])
                bonus.part1_difficulty = form.cleaned_data.get('part1_difficulty', '')
                bonus.part2_difficulty = form.cleaned_data.get('part2_difficulty', '')
                bonus.part3_difficulty = form.cleaned_data.get('part3_difficulty', '')
                bonus.category = form.cleaned_data['category']
                bonus.packet = form.cleaned_data['packet']
                bonus.locked = form.cleaned_data['locked']
                bonus.edited = form.cleaned_data['edited']
                bonus.proofread = form.cleaned_data['proofread']
                bonus.read_carefully = form.cleaned_data['read_carefully']
                bonus.question_type = form.cleaned_data['question_type']
                bonus.author = form.cleaned_data['author']

                try:
                    bonus.is_valid()
                    change_type = QUESTION_CHANGE
                    if (not is_bonus_already_edited and bonus.edited):
                        change_type = QUESTION_EDIT

                    if (not is_bonus_already_proofread and bonus.proofread):
                        change_type = QUESTION_PROOFREAD
                        
                    if (not is_bonus_already_read_carefully and bonus.read_carefully):
                        change_type = QUESTION_READ_CAREFULLY

                    bonus.save_question(edit_type=change_type, changer=user)
                    save_tag_selection(request, qset, bonus, bonus.category, is_tossup=False)
                    char_count = bonus.character_count()
                    cache.clear()

                    message = 'Your changes have been saved!'
                    message_class = 'alert-box success'
                    read_only = False
                    dup_matches = _post_submit_dup_matches(qset, bonus, 'bonus')
                except InvalidBonus as ex:
                    message = str(ex)
                    message_class = 'alert-box warning'
                    read_only = False

            elif form.is_valid() and not can_change:
                message = 'This bonus is locked and can only be changed by an editor!'
                message_class = 'alert-box warning'
                read_only = True
            else:
                message = 'There was an error with the form: ' + str(form.errors)
                message_class = 'alert-box warning'

        elif user in qset.writer.all():
            form = None
            read_only = True
            message = 'You are only authorized to view, not to edit, this question!'
            message_class = 'alert-box warning'
        else:
            form = None
            bonus = None
            read_only = True
            message = 'You are not authorized to view or edit this question!'
            message_class = 'alert-box alert'

        return render(request, 'edit_bonus.html',
            {'bonus': bonus,
             'packet_nav': _packet_neighbors(bonus, 'bonus'),
             'char_count': char_count,
             'question_type': question_type,
             'form': form,
             'qset': qset,
             'packet': packet,
             'available_tags': build_tag_checkboxes(qset, bonus, bonus.category if bonus else None),
             'message': message,
             'message_class': message_class,
             'dup_matches': dup_matches,
             'read_only': read_only,
             'role': role,
             'playtest': _question_buzz_data(bonus, 'bonus'),
             'discord_threads': bonus.discord_threads.order_by('created_date'),
             'user': user,
             **_structured_bonus_ctx(qset, bonus),
             **_suggestion_render_ctx(user, bonus, 'bonus', qset)})

@login_required
def delete_tossup(request):
    user = request.user.writer
    message = ''
    message_class = ''
    read_only = True

    if request.method == 'POST':
        tossup_id = int(request.POST['tossup_id'])
        tossup = Tossup.objects.get(id=tossup_id)
        qset = tossup.question_set
        if user == tossup.author or qset.is_owner(user) or user in qset.editor.all():
            tossup.delete()
            message = 'Tossup deleted'
            message_class = 'alert-box success'
            read_only = False
        else:
            message = 'You are not authorized to delete questions from this set!'
            message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

@login_required
def delete_bonus(request):
    user = request.user.writer
    message = ''
    message_class = ''
    read_only = True

    if request.method == 'POST':
        bonus_id = int(request.POST['bonus_id'])
        bonus = Bonus.objects.get(id=bonus_id)
        qset = bonus.question_set
        if user == bonus.author or qset.is_owner(user) or user in qset.editor.all():
            bonus.delete()
            cache.clear()
            message = 'Bonus deleted'
            message_class = 'alert-box success'
            read_only = False
        else:
            message = 'You are not authorized to delete questions from this set!'
            message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

@login_required
def delete_writer(request):
    user = request.user.writer
    message = ''
    message_class = ''
    read_only = True

    if request.method == 'POST':
        qset_id = request.POST['qset_id']
        qset = QuestionSet.objects.get(id=qset_id)
        writer_id = request.POST['writer_id']
        writer = qset.writer.get(id=writer_id)
        role = get_role_no_owner(user, qset)
        if role == "editor":
            qset.writer.remove(writer)
            # If they're still in a role group attached as writer, the group
            # re-grants access; otherwise they're fully removed.
            reconcile_group_roles(qset)
            cache.clear()
            message = 'Writer removed'
            message_class = 'alert-box success'
        else:
            message = 'You are not authorized to remove writers from this set!'
            message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

@login_required
def delete_editor(request):
    user = request.user.writer
    message = ''
    message_class = ''
    read_only = True

    if request.method == 'POST':
        qset_id = request.POST['qset_id']
        qset = QuestionSet.objects.get(id=qset_id)
        editor_id = request.POST['editor_id']
        editor = qset.editor.get(id=editor_id)
        role = get_role_no_owner(user, qset)
        if role == "editor":
            qset.editor.remove(editor)
            reconcile_group_roles(qset)
            cache.clear()
            message = 'Editor removed'
            message_class = 'alert-box success'
        else:
            message = 'You are not authorized to remove editors from this set!'
            message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

@login_required
def delete_set(request):
    user = request.user.writer
    message = ''
    message_class = ''
    read_only = True

    print("In editor removed")
    if request.method == 'POST':
        qset_id = request.POST['qset_id']
        qset = QuestionSet.objects.get(id=qset_id)
        if qset.is_owner(user) or user in qset.editor.all():
            from .set_importer import delete_question_set
            delete_question_set(qset)
            cache.clear()
            message = 'Set deleted'
            message_class = 'alert-box success'
        else:
            message = 'You are not authorized to delete this set!'
            message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

def _question_set_of(obj):
    """The question set an object belongs to, or None.

    Authorization has to come from the thing being changed. Several of these
    views used to read a `qset_id` out of the same POST that named the object,
    and check the caller against *that* — so an editor of their own set could
    name someone else's question and pass the check.
    """
    if obj is None:
        return None
    qset = getattr(obj, 'question_set', None)
    if qset is None:
        target = getattr(obj, 'content_object', None)   # a django_comments Comment
        qset = getattr(target, 'question_set', None)
    return qset


def _may_edit_set(user, qset):
    """Owner or editor of that set."""
    return qset is not None and (qset.is_owner(user) or user in qset.editor.all())


def _may_edit_question(user, question):
    """Owner or editor of the question's set, or the question's own author."""
    qset = _question_set_of(question)
    if qset is None:
        return False
    return _may_edit_set(user, qset) or question.author_id == user.id


@login_required
def delete_comment(request):
    user = request.user.writer
    message = ''
    message_class = ''
    read_only = True

    if request.method == 'POST':
        comment_id = request.POST['comment_id']
        comment = Comment.objects.filter(id=comment_id).first()
        # The set is the one the comment is on. The posted qset_id is only
        # good for going back to the page afterwards.
        qset = _question_set_of(comment)

        if (comment is None or qset is None):
            message = 'Error retrieving comment.'
            message_class = 'alert-box warning'
        else:
            if _may_edit_set(user, qset):
                comment.is_removed = True
                comment.save()
                cache.clear()
                message = 'Comment removed'
                message_class = 'alert-box success'
            else:
                message = 'You are not authorized to remove comments from this set!'
                message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

@login_required
def post_comment(request):
    """Create a top-level comment on a tossup, bonus, or packet via AJAX.

    Bypasses django_comments' security form (whose timestamp expires after a
    couple of hours, producing a 400 on a tab left open too long). The Comment
    post_save signals still fire, so @mentions and notification emails work.
    """
    if request.method != 'POST':
        return HttpResponse(json.dumps({'success': False, 'message': 'Invalid request'}))
    target_type = request.POST.get('target_type')
    target_id = request.POST.get('target_id')
    text = (request.POST.get('comment_text') or '').strip()
    models_by_type = {'tossup': Tossup, 'bonus': Bonus, 'packet': Packet}
    if target_type not in models_by_type or not target_id or not text:
        return HttpResponse(json.dumps({'success': False, 'message': 'Missing or invalid fields.'}))
    try:
        obj = models_by_type[target_type].objects.select_related('question_set').get(id=target_id)
    except (Tossup.DoesNotExist, Bonus.DoesNotExist, Packet.DoesNotExist):
        return HttpResponse(json.dumps({'success': False, 'message': 'Target not found.'}))

    qset = obj.question_set
    user = request.user.writer
    if user not in qset.writer.all() and user not in qset.editor.all() and not qset.is_owner(user):
        return HttpResponse(json.dumps({'success': False, 'message': 'You are not authorized to comment on this set.'}))

    from django.contrib.sites.models import Site
    Comment.objects.create(
        content_type=ContentType.objects.get_for_model(obj),
        object_pk=str(obj.id), site=Site.objects.get_current(),
        user=request.user, comment=text, is_public=True, is_removed=False)
    cache.clear()
    return HttpResponse(json.dumps({'success': True, 'message': 'Comment posted.'}))


@login_required
def reply_to_comment(request):
    message = ''
    message_class = ''

    if request.method == 'POST':
        parent_id = request.POST.get('parent_id')
        comment_text = request.POST.get('comment_text', '').strip()
        qset_id = request.POST.get('qset_id')

        if not parent_id or not comment_text or not qset_id:
            message = 'Missing required fields.'
            message_class = 'alert-box warning'
            return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

        try:
            parent_comment = Comment.objects.get(id=parent_id)
        except Comment.DoesNotExist:
            message = 'Comment or question set not found.'
            message_class = 'alert-box warning'
            return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

        # A reply belongs to the set the comment it answers is in; the posted
        # qset_id says nothing about that.
        qset = _question_set_of(parent_comment)
        if qset is None:
            message = 'Comment or question set not found.'
            message_class = 'alert-box warning'
            return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

        user = request.user.writer
        qset_writers = qset.writer.all()
        qset_editors = qset.editor.all()

        if user not in qset_writers and user not in qset_editors and not qset.is_owner(user):
            message = 'You are not authorized to comment on this set.'
            message_class = 'alert-box warning'
            return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

        from django.contrib.sites.models import Site
        new_comment = Comment(
            content_type=parent_comment.content_type,
            object_pk=parent_comment.object_pk,
            site=Site.objects.get_current(),
            user=request.user,
            comment=comment_text,
        )
        new_comment.save()

        CommentReply.objects.create(comment=new_comment, parent=parent_comment)

        message = 'Reply posted.'
        message_class = 'alert-box success'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))


@login_required
def add_anchored_comment(request):
    message = ''
    message_class = ''

    if request.method == 'POST':
        question_type = request.POST.get('question_type')
        question_id = request.POST.get('question_id')
        comment_text = request.POST.get('comment_text', '').strip()
        selected_text = request.POST.get('selected_text', '').strip()
        prefix = request.POST.get('prefix', '')
        suffix = request.POST.get('suffix', '')

        if not question_id or not comment_text or not selected_text or question_type not in ('tossup', 'bonus'):
            message = 'Missing required fields.'
            message_class = 'alert-box warning'
            return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

        try:
            if question_type == 'tossup':
                question = Tossup.objects.get(id=question_id)
            else:
                question = Bonus.objects.get(id=question_id)
        except (Tossup.DoesNotExist, Bonus.DoesNotExist):
            message = 'Question not found.'
            message_class = 'alert-box warning'
            return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

        qset = question.question_set
        user = request.user.writer

        if user not in qset.writer.all() and user not in qset.editor.all() and not qset.is_owner(user):
            message = 'You are not authorized to comment on this set.'
            message_class = 'alert-box warning'
            return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

        from django.contrib.sites.models import Site
        new_comment = Comment(
            content_type=ContentType.objects.get_for_model(question),
            object_pk=str(question.id),
            site=Site.objects.get_current(),
            user=request.user,
            comment=comment_text,
        )
        new_comment.save()

        CommentAnchor.objects.create(
            comment=new_comment,
            selected_text=selected_text[:1000],
            prefix=prefix[:100],
            suffix=suffix[:100],
        )

        message = 'Comment posted.'
        message_class = 'alert-box success'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))


@login_required
def add_question_comment(request):
    """Add a plain (non-anchored) comment to a tossup or bonus. Used by the
    document viewer's inline comment boxes."""
    message = ''
    message_class = ''

    if request.method == 'POST':
        question_type = request.POST.get('question_type')
        question_id = request.POST.get('question_id')
        comment_text = request.POST.get('comment_text', '').strip()

        if not question_id or not comment_text or question_type not in ('tossup', 'bonus'):
            return HttpResponse(json.dumps({'message': 'Missing required fields.', 'message_class': 'alert-box warning'}))

        try:
            if question_type == 'tossup':
                question = Tossup.objects.get(id=question_id)
            else:
                question = Bonus.objects.get(id=question_id)
        except (Tossup.DoesNotExist, Bonus.DoesNotExist):
            return HttpResponse(json.dumps({'message': 'Question not found.', 'message_class': 'alert-box warning'}))

        qset = question.question_set
        user = request.user.writer
        if user not in qset.writer.all() and user not in qset.editor.all() and not qset.is_owner(user):
            return HttpResponse(json.dumps({'message': 'You are not authorized to comment on this set.', 'message_class': 'alert-box warning'}))

        from django.contrib.sites.models import Site
        Comment.objects.create(
            content_type=ContentType.objects.get_for_model(question),
            object_pk=str(question.id),
            site=Site.objects.get_current(),
            user=request.user,
            comment=comment_text,
            is_public=True,
            is_removed=False,
        )
        message = 'Comment posted.'
        message_class = 'alert-box success'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))


@login_required
def delete_all_comments(request):
    user = request.user.writer
    message = ''
    message_class = ''
    read_only = True

    tossup_content_type_id = ContentType.objects.get_for_model(Tossup).id
    bonus_content_type_id = ContentType.objects.get_for_model(Bonus).id

    if request.method == 'POST':
        question_type = request.POST['question_type']
        question_id = request.POST['question_id']

        # The set is the question's own, not whatever the form said.
        if (question_type == 'tossup'):
            question = Tossup.objects.filter(id=question_id).first()
            comment_list = Comment.objects.filter(content_type_id=tossup_content_type_id).filter(object_pk=question_id).order_by('submit_date')
        else:
            question = Bonus.objects.filter(id=question_id).first()
            comment_list = Comment.objects.filter(content_type_id=bonus_content_type_id).filter(object_pk=question_id).order_by('submit_date')
        qset = _question_set_of(question)

        if (question is None or qset is None):
            message = 'Error retrieving comments.'
            message_class = 'alert-box warning'
        else:
            if _may_edit_set(user, qset):
                comment_list.update(is_removed=True)
                cache.clear()

                message = 'Comments removed'
                message_class = 'alert-box success'
            else:
                message = 'You are not authorized to remove comments from this set!'
                message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

@login_required
def add_packets(request, qset_id):

    qset = QuestionSet.objects.get(id=qset_id)
    user = request.user.writer
    message = ''
    message_class = ''

    if qset.is_owner(user):
        if request.method == 'GET':
            form = NewPacketsForm()
        elif request.method == 'POST':
            form = NewPacketsForm(data=request.POST)
            if form.is_valid():
                packet_name = form.cleaned_data['packet_name']
                name_base = form.cleaned_data['name_base']
                num_packets = form.cleaned_data['num_packets']
                if packet_name and len(packet_name.strip()) > 0 and (name_base is None or num_packets is None):
                    if Packet.objects.filter(question_set=qset, packet_name=packet_name).exists():
                        message = 'The packet name "{0}" arleady exists.'.format(packet_name)
                        message_class = 'alert-box warning'
                    else:
                        new_packet = Packet()
                        new_packet.packet_name = packet_name
                        new_packet.created_by = user
                        new_packet.question_set = qset
                        new_packet.save()
                        cache.clear()
                        message = 'Your packet named {0} has been created.'.format(packet_name)
                        message_class = 'alert-box success'

                elif name_base and len(name_base.strip()) > 0 and num_packets is not None:
                    create_all_failed = False
                    for i in range(1, num_packets + 1):
                        new_packet = Packet()
                        packet_name = '{0!s} {1:02}'.format(name_base, i)
                        if Packet.objects.filter(question_set=qset, packet_name=packet_name).exists():
                            message = 'The packet name "{0}" arleady exists.'.format(packet_name)
                            message_class = 'alert-box warning'
                            create_all_failed = True
                            break
                        new_packet.packet_name = packet_name
                        new_packet.created_by = user
                        new_packet.question_set = qset
                        new_packet.save()
                        cache.clear()
                    if not create_all_failed:
                        message = 'Your {0} packet(s) with the base name {1} have been created.'.format(num_packets, name_base)
                        message_class = 'alert-box success'
                else:
                    message = 'You must enter either the name for an individual packet or a base name and the number of packets to create!'
                    message_class = 'alert-box warning'

            else:
                message = 'Invalid information entered into form!'
                message_class = 'alert-box alert'
        else:
            message = 'Invalid method!'
            message_class = 'alert-box alert'
            form = None

    else:
        message = 'You are not authorized to add packets to this set!'
        message_class = 'alert-box alert'
        form = None

    return render(request, 'add_packets.html',
                             {'message': message,
                              'message_class': message_class,
                              'form': form,
                              'qset': qset,
                              'user': user})

@login_required
def delete_packet(request):
    user = request.user.writer
    message = ''
    message_class = ''
    read_only = True

    if request.method == 'POST':
        packet_id = int(request.POST['packet_id'])
        packet = Packet.objects.get(id=packet_id)
        qset = packet.question_set
        if qset.is_owner(user):
            packet.delete()
            cache.clear()
            message = 'Packet deleted'
            message_class = 'alert-box success'
            read_only = False
        else:
            message = 'You are not authorized to delete packets from this set!'
            message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

@login_required
def get_unassigned_tossups(request):
    user = request.user.writer
    qset_id = request.GET['qset_id']
    message = ''
    message_class = ''
    data = []

    try:
        qset = QuestionSet.objects.get(id=qset_id)

        if request.method == 'GET':
            if qset.is_owner(user):
                available_tossups = Tossup.objects.filter(question_set=qset, packet=None)
                for tu in available_tossups:
                    data.append(tu.to_json())
            else:
                available_tossups = []
                message = 'Only the set owner has the power to add questions to it!'
                message_class = 'alert-box alert'

        else:
            message = 'Invalid request!'
            message_class = 'alert-box alert'
    except Exception as ex:
        print(ex)
        message = 'Unable to retrieve question set; qset_id either missing or incorrect!'
        message_class = 'alert-box alert'

    return HttpResponse(json.dumps(data))

@login_required
def get_unassigned_bonuses(request):
    user = request.user.writer
    qset_id = request.GET['qset_id']
    message = ''
    message_class = ''
    data = []

    try:
        qset = QuestionSet.objects.get(id=qset_id)

        if request.method == 'GET':
            if qset.is_owner(user):
                available_bonuses= Bonus.objects.filter(question_set=qset, packet=None)
                for bs in available_bonuses:
                    data.append(bs.to_json())
            else:
                available_tossups = []
                message = 'Only the set owner has the power to add questions to it!'
                message_class = 'alert-box alert'

        else:
            message = 'Invalid request!'
            message_class = 'alert-box alert'
    except Exception as ex:
        print(ex)
        message = 'Unable to retrieve question set; qset_id either missing or incorrect!'
        message_class = 'alert-box alert'

    return HttpResponse(json.dumps(data))

@login_required
def assign_tossups_to_packet(request):

    user = request.user.writer
    packet_id = int(request.POST['packet_id'])
    tossup_ids = request.POST.getlist('tossup_ids[]')
    packet = Packet.objects.get(id=packet_id)
    qset = packet.question_set
    message = ''
    message_class = ''


    if request.method == 'POST':
        if qset.is_owner(user):
            for tu_id in tossup_ids:
                # Only questions already in this packet's set may be assigned,
                # so foreign question IDs can't be pulled across sets
                tossup = Tossup.objects.filter(id=tu_id, question_set=qset).first()
                if tossup is None:
                    continue
                tossup.packet = packet
                # Potential race condition?
                tossup.question_number = _next_question_number(Tossup, packet_id)
                message = 'Your tossups have been added to the set!'
                message_class = 'alert-box success'
                tossup.save()
                cache.clear()
        else:
            message = 'Only the set owner is authorized to add questions to the set!'
            message_class = 'alert-box warning'

    else:
        message = 'Invalid request!'
        message_class = 'alert-box alert'

    return HttpResponse(json.dumps({'message': message,
                                    'message_class': message_class}))

@login_required
def assign_bonuses_to_packet(request):

    user = request.user.writer
    packet_id = int(request.POST['packet_id'])
    bonus_ids = request.POST.getlist('bonus_ids[]')
    packet = Packet.objects.get(id=packet_id)
    qset = packet.question_set
    message = ''
    message_class = ''

    if request.method == 'POST':
        if qset.is_owner(user):
            for bs_id in bonus_ids:
                # Only questions already in this packet's set may be assigned,
                # so foreign question IDs can't be pulled across sets
                bonus = Bonus.objects.filter(id=bs_id, question_set=qset).first()
                if bonus is None:
                    continue
                bonus.packet = packet
                bonus.question_number = _next_question_number(Bonus, packet_id)
                message = 'Your bonuses have been added to the set!'
                message_class = 'alert-box success'
                bonus.save()
                cache.clear()
        else:
            message = 'Only the set owner is authorized to add questions to the set!'
            message_class = 'alert-box warning'

    else:
        message = 'Invalid request!'
        message_class = 'alert-box alert'

    return HttpResponse(json.dumps({'message': message,
                                    'message_class': message_class}))

@login_required
def change_question_order(request):

    user = request.user.writer
    packet_id = int(request.POST['packet_id'])
    num_questions = int(request.POST['num_questions'])
    question_type = request.POST['question_type']
    packet = Packet.objects.get(id=packet_id)
    qset = packet.question_set

    if request.method == 'POST':
        if qset.is_owner(user):
            try:
                for i in range(num_questions):
                    id_key = 'order_data[{0}][id]'.format(i)
                    order_key = 'order_data[{0}][order]'.format(i)
                    id = int(request.POST[id_key])
                    order = int(request.POST[order_key])
                    if question_type == 'tossup':
                        question = Tossup.objects.get(id=id, question_set=qset)
                    elif question_type == 'bonus':
                        question = Bonus.objects.get(id=id, question_set=qset)
                    question.question_number = order
                    question.save()
                    cache.clear()
                message = ''
                message_class = ''

            except Exception as ex:
                print(ex)
                message = 'Something went terribly wrong!'
                message_class = 'alert-box alert'

        else:
            message = 'Only the owner of the set is allowed to change the order of questions!'
            message_class = 'alert-box warning'
    else:
        message = 'Invalid request!'
        message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

# @login_required
# def change_tossup_order(request):
#     # packet_id, old_index, new_index
#     packet_id = int(request.POST['packet_id'])
#     user = request.user.writer
#     packet = Packet.objects.get(id=packet_id)
#     qset = packet.question_set
#
#     old_index = int(request.POST['old_index'])
#     new_index = int(request.POST['new_index'])
#
#     if request.method == 'POST':
#         if qset.is_owner(user):
#             change_question_order(packet, int(old_index), int(new_index), Tossup)
#             message = ''
#             message_class = ''
#         else:
#             message = 'Only the set owner is authorized to change question order'
#             message_class = 'alert-box warning'
#     else:
#         message = 'Invalid request!'
#         message_class = 'alert-box alert'
#
#     return HttpResponse(json.dumps({'message': message,
#                                     'message_class': message_class}))


# @login_required
# def change_bonus_order(request):
#     user = request.user.writer
#     packet = Packet.objects.get(id=packet_id)
#     qset = packet.question_set
#
#     if request.method == 'POST':
#         if qset.is_owner(user):
#             change_question_order(packet, int(old_index), int(new_index), Bonus)
#             message = ''
#             message_class = ''
#         else:
#             message = 'Only the set owner is authorized to change question order'
#             message_class = 'alert-box warning'
#     else:
#         message = 'Invalid request!'
#         message_class = 'alert-box alert'
#     return HttpResponse(json.dumps({'message': message,
#                                     'message_class': message_class}))
#
# # Not a URL action, just a helper method. old_index and new_index should be integers
# def change_question_order(packet, old_index, new_index, model_class):
#     if old_index != new_index and old_index >= 0 and new_index >= 0:
#         # If oldIndex < newIndex, decrease question_number for questions [oldIndex + 1, newIndex]
#         # Otherwise, increase question_number for questions [newIndex, oldIndex - 1]
#         lowerIndex = old_index + 1 if old_index < new_index else new_index
#         higherIndex = old_index - 1 if old_index > new_index else new_index
#
#         selected_question = model_class.objects.get(packet=packet, question_number=old_index)
#         selected_id = selected_question.id
#         reordered_questions = model_class.objects.filter(packet=packet, question_number__range=(lowerIndex, higherIndex))
#         # This prevents a race condition where selected_question's question_number is set to something in the range
#         # before this QuerySet is evaluated.
#         reordered_questions = reordered_questions.exclude(id=selected_id)
#         selected_question.question_number = new_index
#         selected_question.save()
#
#         direction = -1 if old_index < new_index else 1
#         for question in reordered_questions:
#             question.question_number += direction
#             question.save()

def _account_can_create(user):
    """Anti-spam: a new account can't create question sets or distributions
    until 2 days after it was created.

    Two ways out, both an administrator's call: a superuser is never held by
    the wait, and ticking "can create early" on a writer in the Django admin
    lifts it for that one person. Everyone else waits — but for question sets
    waiting no longer means being turned away; see `create_question_set`."""
    from datetime import timedelta
    if user.is_superuser:
        return True
    writer = getattr(user, 'writer', None)
    if writer is not None and writer.can_create_early:
        return True
    return (timezone.now() - user.date_joined) >= timedelta(days=2)


_ACCOUNT_TOO_NEW_MSG = ('New accounts can\'t create question sets or distributions '
                        'until 2 days after sign-up. Please try again later.')


@login_required
def distribution_preview(request, dist_id):
    """JSON summary of a distribution for the picker on the create-set page:
    who made it, when, and the per-packet category breakdown. Limited to the
    distributions the viewer can pick — public ones plus their own — since a
    private distribution's category breakdown is its author's to share."""
    try:
        dist = Distribution.visible_to(request.user.writer).get(id=dist_id)
    except Distribution.DoesNotExist:
        return HttpResponse(json.dumps({'ok': False, 'error': 'No such distribution'}), status=404)

    creator = ''
    if dist.created_by is not None and dist.created_by.user is not None:
        u = dist.created_by.user
        creator = '{0} {1}'.format(u.first_name, u.last_name).strip() or u.username

    rows = dist.entry_summary()
    return HttpResponse(json.dumps({
        'ok': True,
        'id': dist.id,
        'name': dist.name,
        'created_by': creator,
        'created_date': (timezone.localtime(dist.created_date).strftime('%b %d, %Y')
                         if dist.created_date else ''),
        'entry_count': sum(r['subcategories'] for r in rows),
        'category_count': len(rows),
        'total_min_tossups': sum(r['min_tossups'] for r in rows),
        'total_max_tossups': sum(r['max_tossups'] for r in rows),
        'total_min_bonuses': sum(r['min_bonuses'] for r in rows),
        'total_max_bonuses': sum(r['max_bonuses'] for r in rows),
        'in_use_by': QuestionSet.objects.filter(distribution=dist).count(),
        'categories': rows,
    }))


def _writer_distribution_ids(writer):
    """Distribution ids for the sets a writer belongs to (owner, co-owner,
    editor or writer), plus any they created — the ones they may edit. Public
    distributions made by other people are *not* included: those are readable
    and clonable (see Distribution.visible_to) but not editable."""
    return Distribution.member_ids(writer)


def _entry_delete_refusal(entry, writer):
    """Why `writer` may not delete this distribution entry, or None if they may.

    The delete is a cascade (questions, and every set's quota rows), so this is
    checked on the POST rather than only hidden in the page -- a stale form or a
    hand-made request must not get past it.
    """
    if not entry.owned_by(writer):
        creator = entry.distribution.created_by
        who = creator.get_real_name().strip() if creator else ''
        return '{0}: only {1} can delete a category from this distribution.'.format(
            entry, who or 'whoever created it')
    blockers = entry.deletion_blockers(writer)
    if blockers:
        return '{0}: {1}'.format(entry, ' '.join(blockers))
    return None


def _dist_owned_by(dist_id, writer):
    """Whether `writer` made this distribution. Archiving it is theirs alone:
    everyone on a set that uses it can edit its categories, and none of them
    should be able to take it off other people's pickers."""
    if dist_id is None:
        return False
    return Distribution.objects.filter(id=dist_id, created_by=writer).exists()


def _annotate_entry_delete_rights(formset, dist, writer):
    """Tell each row of the distribution formset whether its entry can be
    deleted, and if not why, so the page can show the reason instead of a
    checkbox that would be refused."""
    looked_up = {}
    for form in formset.forms:
        try:
            entry_id = int(form['entry_id'].value())
        except (TypeError, ValueError):
            # A row that has never been saved: there is nothing to protect, and
            # ticking Delete just drops it from the submission.
            form.entry_deletable, form.entry_blockers = True, []
            continue
        if entry_id not in looked_up:
            entry = (DistributionEntry.objects.filter(id=entry_id, distribution=dist).first()
                     if dist is not None else None)
            if entry is None:
                looked_up[entry_id] = (True, [])
            elif not entry.owned_by(writer):
                looked_up[entry_id] = (
                    False, ['Only the person who created this distribution can delete '
                            'its categories.'])
            else:
                blockers = entry.deletion_blockers(writer)
                looked_up[entry_id] = (not blockers, blockers)
        form.entry_deletable, form.entry_blockers = looked_up[entry_id]


@login_required
def distributions (request):
    # Yours (editable) first; public ones from other people are listed
    # separately below and can only be previewed and copied.
    user = request.user.writer
    mine_ids = _writer_distribution_ids(user)
    # Archived ones last, so the list reads as what you are using followed by
    # what you are keeping; both are still here, since this is the page you
    # come to to bring one back.
    dists = Distribution.objects.filter(id__in=mine_ids).order_by('archived', 'name')
    public_dists = (Distribution.objects.filter(public=True, archived=False)
                    .exclude(id__in=mine_ids)
                    .select_related('created_by__user').order_by('name'))

    return render(request, 'distributions.html',
                             {'dists': dists,
                              'public_dists': public_dists,
                              'user': user})

@login_required
def import_distribution(request):
    """Create a distribution from an uploaded spreadsheet (.xlsx/.csv/.tsv),
    one row per category entry. The page offers a template to fill in."""
    from . import distribution_importer as di
    user = request.user.writer
    if not _account_can_create(request.user):
        return render(request, 'failure.html',
                      {'message': _ACCOUNT_TOO_NEW_MSG, 'message_class': 'alert-box alert'})
    message = message_class = ''
    name = ''
    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()
        upload = request.FILES.get('sheet')
        if not name:
            message, message_class = 'Give the distribution a name.', 'alert-box warning'
        elif upload is None:
            message, message_class = 'Choose a spreadsheet to import.', 'alert-box warning'
        else:
            try:
                dist = di.create_distribution_from_sheet(
                    upload.name, upload.read(), name, user, public=bool(request.POST.get('public')))
            except di.DistributionImportError as ex:
                message, message_class = str(ex), 'alert-box warning'
            else:
                messages.success(request, 'Created "{0}" with {1} entries from {2}.'.format(
                    dist.name, dist.distributionentry_set.count(), upload.name))
                return HttpResponseRedirect('/edit_distribution/{0}'.format(dist.id))
    return render(request, 'import_distribution.html',
                  {'user': user, 'message': message, 'message_class': message_class,
                   'name': name, 'columns': di.COLUMNS, 'example_rows': di.TEMPLATE_ROWS[:6]})


@login_required
def distribution_template(request, fmt):
    """The spreadsheet template, as .xlsx or .csv."""
    from . import distribution_importer as di
    if fmt == 'xlsx':
        resp = HttpResponse(di.template_xlsx(),
                            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    else:
        resp = HttpResponse(di.template_csv(), content_type='text/csv; charset=utf-8')
        fmt = 'csv'
    resp['Content-Disposition'] = 'attachment; filename="distribution_template.{0}"'.format(fmt)
    return resp


@login_required
def clone_distribution(request, dist_id):
    if request.method != 'POST':
        return HttpResponseRedirect('/distributions/')

    user = request.user.writer
    # Cloning reads the source and writes a new distribution, so a public one is
    # fair game — that's the point of publishing it. Editing still isn't.
    if not Distribution.visible_to(user).filter(id=dist_id).exists():
        return render(request, 'failure.html',
                      {'message': 'You can only clone your own distributions or public ones.',
                       'message_class': 'alert-box alert'})
    if not _account_can_create(request.user):
        return render(request, 'failure.html',
                      {'message': _ACCOUNT_TOO_NEW_MSG, 'message_class': 'alert-box alert'})

    source = Distribution.objects.get(id=dist_id)
    new_dist = Distribution()
    new_dist.name = source.name + ' (Copy)'
    # A copy starts private regardless of the source: publishing is the new
    # owner's call to make.
    new_dist.public = False
    new_dist.created_by = user
    new_dist.created_date = timezone.now()
    new_dist.acf_tossup_per_period_count = source.acf_tossup_per_period_count
    new_dist.acf_bonus_per_period_count = source.acf_bonus_per_period_count
    new_dist.vhsl_bonus_per_period_count = source.vhsl_bonus_per_period_count
    new_dist.save()

    # Entries have no ordering field — they display in the order they were
    # created — so copy them alphabetically by category, then subcategory. A
    # clone of a distribution built up piecemeal then starts out tidy.
    source_entries = sorted(
        DistributionEntry.objects.filter(distribution=source),
        key=lambda e: ((e.category or '').lower(), (e.subcategory or '').lower()))
    DistributionEntry.objects.bulk_create([
        DistributionEntry(
            distribution=new_dist,
            category=entry.category,
            subcategory=entry.subcategory,
            min_tossups=entry.min_tossups,
            min_bonuses=entry.min_bonuses,
            max_tossups=entry.max_tossups,
            max_bonuses=entry.max_bonuses,
        )
        for entry in source_entries])

    return HttpResponseRedirect('/edit_distribution/' + str(new_dist.id) + '/')

@login_required
def edit_distribution(request, dist_id=None):

    data = []
    message = ''
    message_class = ''

    user = request.user.writer
    if dist_id is None:
        # Creating a brand-new distribution: gate by account age.
        if not _account_can_create(request.user):
            return render(request, 'failure.html',
                          {'message': _ACCOUNT_TOO_NEW_MSG, 'message_class': 'alert-box alert'})
    elif int(dist_id) not in _writer_distribution_ids(user):
        return render(request, 'failure.html',
                      {'message': 'You can only view or edit distributions for your own sets.',
                       'message_class': 'alert-box alert'})

    if request.user.is_authenticated:
        DistributionEntryFormset = formset_factory(DistributionEntryForm, can_delete=True)
        if request.method == 'POST':
            # no dist_id supplied means new dist
            if dist_id is None:
                dist_form = DistributionForm(data=request.POST)
                if 'add_row' in request.POST:
                    # The no-script fallback for the Add Category button: give
                    # the page back with one more row. Without this the request
                    # fell through to the save below and created the
                    # distribution, which is not what a button called Add Row
                    # should do.
                    distentry_post = request.POST.copy()
                    distentry_post['distentry-TOTAL_FORMS'] = \
                        int(distentry_post['distentry-TOTAL_FORMS']) + 1
                    formset = DistributionEntryFormset(data=distentry_post, prefix='distentry')
                    _annotate_entry_delete_rights(formset, None, user)
                    return render(request, 'edit_distribution.html',
                                  {'form': dist_form,
                                   'formset': formset,
                                   'message': message,
                                   'message_class': message_class,
                                   'is_dist_owner': False,
                                   'user': user})
                formset = DistributionEntryFormset(data=request.POST, prefix='distentry')
                if dist_form.is_valid() and formset.is_valid():
                    new_dist = Distribution()
                    new_dist.name = dist_form.cleaned_data['name']
                    new_dist.public = dist_form.cleaned_data['public']
                    new_dist.created_by = user
                    new_dist.created_date = timezone.now()
                    new_dist.save()

                    for form in formset:
                        if form.cleaned_data != {}:
                            new_entry = DistributionEntry()
                            new_entry.category = form.cleaned_data['category']
                            new_entry.subcategory = form.cleaned_data['subcategory']
                            new_entry.min_bonuses = form.cleaned_data['min_bonuses']
                            new_entry.min_tossups = form.cleaned_data['min_tossups']
                            new_entry.max_bonuses = form.cleaned_data['max_bonuses']
                            new_entry.max_tossups = form.cleaned_data['max_tossups']
                            if new_entry.min_bonuses > new_entry.max_bonuses:
                                new_entry.min_bonuses = new_entry.max_bonuses
                                #TODO: display the message
                                message = 'Minimum bonuses for ' + new_entry.category + ' - ' + new_entry.subcategory +\
                                          ' was higher than maximum bonuses and has been set to maximum bonuses.'
                                message_class = 'alert-box warning'
                            if new_entry.min_tossups > new_entry.max_tossups:
                                new_entry.min_tossups = new_entry.max_tossups
                                #TODO: display the message
                                message = 'Minimum tossups for ' + new_entry.category + ' - ' + new_entry.subcategory +\
                                          ' was higher than maximum tossups and has been set to maximum tossups.'
                                message_class = 'alert-box warning'

                            new_entry.distribution = new_dist
                            new_entry.save()

                    return HttpResponseRedirect('/edit_distribution/' + str(new_dist.id))

            else:
                dist_form = DistributionForm(data=request.POST)
                #print dist_form.is_valid()
                #print formset.is_valid()
                #print formset.errors
                if 'add_row' in request.POST:
                    distentry_post = request.POST.copy()
                    #TODO: grab a value from an input
                    num_rows = 1
                    distentry_post['distentry-TOTAL_FORMS'] = int(distentry_post['distentry-TOTAL_FORMS']) + num_rows
                    formset = DistributionEntryFormset(data=distentry_post, prefix='distentry')
                else:
                    formset = DistributionEntryFormset(data=request.POST, prefix='distentry')
                    if dist_form.is_valid() and formset.is_valid():
                        dist = Distribution.objects.get(id=dist_id)
                        dist.name = dist_form.cleaned_data['name']
                        dist.public = dist_form.cleaned_data['public']
                        dist.save()

                        qsets = dist.questionset_set.all()
                        delete_refusals = []
                        for form in formset:
                            if form.cleaned_data != {}:
                                if form.cleaned_data['entry_id'] is not None:
                                    entry_id = int(form.cleaned_data['entry_id'])
                                    entry = DistributionEntry.objects.get(id=entry_id)
                                    if form.cleaned_data['DELETE']:
                                        refusal = _entry_delete_refusal(entry, user)
                                        if refusal:
                                            delete_refusals.append(refusal)
                                        else:
                                            entry.delete()
                                            entry = None
                                    else:
                                        entry.category = form.cleaned_data['category']
                                        entry.subcategory = form.cleaned_data['subcategory']
                                        entry.min_bonuses = form.cleaned_data['min_bonuses']
                                        entry.min_tossups = form.cleaned_data['min_tossups']
                                        entry.max_bonuses = form.cleaned_data['max_bonuses']
                                        entry.max_tossups = form.cleaned_data['max_tossups']
                                        if entry.min_bonuses > entry.max_bonuses:
                                            entry.min_bonuses = entry.max_bonuses
                                            message = 'Minimum bonuses for ' + entry.category + ' - ' + entry.subcategory +\
                                                      ' was higher than maximum bonuses and has been set to maximum bonuses.'
                                            message_class = 'alert-box warning'
                                        if entry.min_tossups > entry.max_tossups:
                                            entry.min_tossups = entry.max_tossups
                                            message = 'Minimum tossups for ' + entry.category + ' - ' + entry.subcategory +\
                                                      ' was higher than maximum tossups and has been set to maximum tossups.'
                                            message_class = 'alert-box warning'

                                        entry.save()
                                else:
                                    entry = form.save(commit=False)
                                    entry.distribution = dist
                                    entry.save()

                                if entry is not None:
                                    for qset in qsets:
                                        set_wide_entry = qset.setwidedistributionentry_set.filter(dist_entry=entry)
                                        print(set_wide_entry)
                                        if set_wide_entry.count() == 0:
                                            new_set_wide_entry = SetWideDistributionEntry()
                                            new_set_wide_entry.dist_entry = entry
                                            new_set_wide_entry.question_set = qset
                                            new_set_wide_entry.num_tossups = qset.num_packets * entry.min_tossups
                                            new_set_wide_entry.num_bonuses = qset.num_packets * entry.min_bonuses
                                            new_set_wide_entry.save()

                        if delete_refusals:
                            message = ' '.join(
                                ['Kept: {0}'.format(delete_refusals[0])] + delete_refusals[1:])
                            message_class = 'alert-box warning'

                        # By creation order — the only order these have. Clones
                        # are written alphabetically, so they list that way.
                        entries = dist.distributionentry_set.order_by('id')
                        initial_data = []
                        for entry in entries:
                            initial_data.append({'entry_id': entry.id,
                                                 'category': entry.category,
                                                 'subcategory': entry.subcategory,
                                                 'min_tossups': entry.min_tossups,
                                                 'min_bonuses': entry.min_bonuses,
                                                 'max_tossups': entry.max_tossups,
                                                 'max_bonuses': entry.max_bonuses})
                        formset = DistributionEntryFormset(initial=initial_data, prefix='distentry')

                    else:
                        dist = Distribution.objects.get(id=dist_id)
                        dist_form = DistributionForm(instance=dist)
                        formset = DistributionEntryFormset(data=request.POST, prefix='distentry')

            _annotate_entry_delete_rights(
                formset,
                Distribution.objects.filter(id=dist_id).first() if dist_id else None,
                user)
            return render(request, 'edit_distribution.html',
                                     {'form': dist_form,
                                      'formset': formset,
                                      'message': message,
                                      'message_class': message_class,
                                      'is_dist_owner': _dist_owned_by(dist_id, user),
                                      'user': request.user.writer})
        else:
            if dist_id is not None:
                dist = Distribution.objects.get(id=dist_id)
                entries = dist.distributionentry_set.order_by('id')
                initial_data = []
                for entry in entries:
                    initial_data.append({'entry_id': entry.id,
                                         'category': entry.category,
                                         'subcategory': entry.subcategory,
                                         'min_tossups': entry.min_tossups,
                                         'min_bonuses': entry.min_bonuses,
                                         'max_tossups': entry.max_tossups,
                                         'max_bonuses': entry.max_bonuses})
                dist_form = DistributionForm(instance=dist)
                formset = DistributionEntryFormset(initial=initial_data, prefix='distentry')
            else:
                dist_form = DistributionForm()
                formset = DistributionEntryFormset(prefix='distentry')

            _annotate_entry_delete_rights(
                formset,
                Distribution.objects.filter(id=dist_id).first() if dist_id else None,
                user)
            return render(request, 'edit_distribution.html',
                                     {'form': dist_form,
                                      'formset': formset,
                                      'message': message,
                                      'message_class': message_class,
                                      'is_dist_owner': _dist_owned_by(dist_id, user),
                                      'user': request.user.writer})

@login_required()
def edit_tiebreak(request, dist_id=None):

    user = request.user.writer
    data = []


    TiebreakDistributionEntryFormset = formset_factory(TieBreakDistributionEntryForm, can_delete=True)
    if request.method == 'POST':
        # no dist_id supplied means new dist
        if dist_id is None:
            formset = TiebreakDistributionEntryFormset(data=request.POST, prefix='tiebreak')
            dist_form = TieBreakDistributionForm(data=request.POST)
            if dist_form.is_valid() and formset.is_valid():
                new_dist = TieBreakDistribution()
                new_dist.name = dist_form.cleaned_data['name']
                new_dist.save()

                for form in formset:
                    if form.cleaned_data != {}:
                        new_entry = DistributionEntry()
                        new_entry.category = form.cleaned_data['category']
                        new_entry.subcategory = form.cleaned_data['subcategory']
                        new_entry.bonuses = form.cleaned_data['num_bonuses']
                        new_entry.tossups = form.cleaned_data['num_tossups']
                        new_entry.distribution = new_dist
                        new_entry.save()

                return HttpResponseRedirect('/edit_tiebreak/' + str(new_dist.id))
        else:
            formset = TiebreakDistributionEntryFormset(data=request.POST, prefix='tiebreak')
            dist_form = TieBreakDistributionForm(data=request.POST)
            print(dist_form.is_valid())
            print(formset.is_valid())
            print(formset.errors)
            if dist_form.is_valid() and formset.is_valid():

                dist = TieBreakDistribution.objects.get(id=dist_id)
                dist.name = dist_form.cleaned_data['name']
                qsets = dist.questionset_set.all()
                for form in formset:
                    if form.cleaned_data != {}:
                        if form.cleaned_data['entry_id'] is not None:
                            entry_id = int(form.cleaned_data['entry_id'])
                            entry = DistributionEntry.objects.get(id=entry_id)
                            if form.cleaned_data['DELETE']:
                                # Same cascade, same guard as the distribution page.
                                if _entry_delete_refusal(entry, user) is None:
                                    entry.delete()
                                entry = None
                            else:
                                entry.category = form.cleaned_data['category']
                                entry.subcategory = form.cleaned_data['subcategory']
                                entry.bonuses = form.cleaned_data['num_bonuses']
                                entry.tossups = form.cleaned_data['num_tossups']
                                entry.save()
                        else:
                            entry = form.save(commit=False)
                            entry.distribution = dist
                            entry.save()

                        if entry is not None:
                            for qset in qsets:
                                set_wide_entry = qset.tiebreakdistributionentry_set.filter(dist_entry=entry)
                                print(set_wide_entry)
                                if set_wide_entry.count() == 0:
                                    print('here')
                                    new_set_wide_entry = DistributionEntry()
                                    new_set_wide_entry.dist_entry = entry
                                    new_set_wide_entry.question_set = qset
                                    new_set_wide_entry.num_tossups = qset.num_packets * entry.min_tossups
                                    new_set_wide_entry.num_bonuses = qset.num_packets * entry.min_bonuses
                                    new_set_wide_entry.save()

                entries = dist.distributionentry_set.all()
                initial_data = []
                for entry in entries:
                    initial_data.append({'entry_id': entry.id,
                                         'category': entry.category,
                                         'subcategory': entry.subcategory,
                                         'num_bonuses': entry.min_bonuses,
                                         'num_tossups': entry.max_tossups,})
                formset = TiebreakDistributionEntryFormset(initial=initial_data, prefix='tiebreak')

            else:
                dist = Distribution.objects.get(id=dist_id)
                dist_form = DistributionForm(instance=dist)
                formset = TiebreakDistributionEntryFormset(data=request.POST, prefix='tiebreak')

        return render(request, 'edit_tiebreak.html',
                                  {'form': dist_form,
                                   'formset': formset})

    else:
        if dist_id is not None:
            dist = TieBreakDistribution.objects.get(id=dist_id)
            entries = dist.distributionentry_set.all()
            initial_data = []
            for entry in entries:
                initial_data.append({'entry_id': entry.id,
                                     'category': entry.category,
                                     'subcategory': entry.subcategory,
                                     'num_tossups': entry.min_tossups,
                                     'num_bonuses': entry.min_bonuses,})
            dist_form = TieBreakDistributionForm(instance=dist)
            formset = TiebreakDistributionEntryFormset(initial=initial_data, prefix='tiebreak')
        else:
            dist_form = TieBreakDistributionForm()
            formset = TiebreakDistributionEntryFormset(prefix='tiebreak')

        return render(request, 'edit_tiebreak.html',
        {'form': dist_form,
        'formset': formset,})


@login_required
def add_comment(request):

    user = request.user.writer
    qset_id = request.POST['qset-id']
    qset = QuestionSet.objects.get(id=qset_id)

    if request.method == 'POST':

        comment_text = request.POST['comment-text']
        cache.clear()
        print(comment_text)


@login_required
def upload_questions(request, qset_id):
    qset = QuestionSet.objects.get(id=qset_id)
    user = request.user.writer

    if request.method == 'POST':
        if (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
            form = QuestionUploadForm(request.POST, request.FILES)
            if form.is_valid():
                uploaded_tossups, uploaded_bonuses = parse_uploaded_packet(request.FILES['questions_file'])
                cache.clear()

                return render(request, 'upload_preview.html',
                {'tossups': uploaded_tossups,
                'bonuses': uploaded_bonuses,
                'message': mark_safe('Please verify that this data is correct. Hitting "Submit" will upload these questions '\
                'If you see any mistakes in the submissions, please correct them in the <strong><em>original file</em></strong> and reupload.'),
                'message_class': 'alert-box warning',
                'qset': qset})
            else:
                messages.error(request, form.questions_file.errors)
                return HttpResponseRedirect('/edit_question_set/{0}'.format(qset_id))
        else:
            messages.error(request, 'You do not have permission to upload ')

@login_required
def type_questions(request, qset_id=None):
    if qset_id is not None:
        qset = QuestionSet.objects.get(id=qset_id)
    else:
        qset = QuestionSet.objects.get(id=request.POST['qset_id'])

    user = request.user.writer

    if request.method == 'POST':
        if (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
            form = TypeQuestionsForm(request.POST)
            if form.is_valid():
                question_data = request.POST['questions'].splitlines()
                tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(question_data, qset)

                # Preview what will actually be saved: complete_upload escapes
                # a trailing "(note)" on an answer line, so do it here too.
                for tu in tossups:
                    tu.tossup_answer = escape_answer_note_parens(tu.tossup_answer)
                for bs in bonuses:
                    bs.part1_answer = escape_answer_note_parens(bs.part1_answer)
                    bs.part2_answer = escape_answer_note_parens(bs.part2_answer)
                    bs.part3_answer = escape_answer_note_parens(bs.part3_answer)

                # Every question needs a category from this set's distribution.
                # Uncategorized questions used to sail through and land in the
                # set with no category, which nothing downstream can count.
                category_errors = _uncategorized_errors(tossups, bonuses)

                # A set can ask to be taken straight to the questions when the
                # confirmation screen would have nothing to say: the parse is
                # clean, every question has a category, and -- for a set that
                # asked to be warned about untagged questions -- they all carry a
                # tag. Anything else and the screen appears as it always has,
                # which is the point of the option: it skips the click, not the
                # check.
                preselected = _preselected_category_tags(request)
                if (qset.skip_type_questions_preview and not tossup_errors
                        and not bonus_errors and not category_errors):
                    untagged = [q for q in list(tossups) + list(bonuses)
                                if not _tags_for_parsed_question(qset, q, preselected)]
                    if not (qset.warn_missing_category_tags and untagged):
                        return _save_parsed_questions(request, qset, user, tossups,
                                                      bonuses, preselected)

                # Show how each prose answer line was read, for sets that
                # record structure
                _attach_structure_previews(qset, tossups, bonuses)

                # ...and which category tags each question could be given, so
                # they can be ticked now instead of on a second pass through
                # every question's edit page.
                attach_tag_choices(qset, tossups, preselected)
                attach_tag_choices(qset, bonuses, preselected)

                # Style and repeat checks run here, on the confirmation screen,
                # where "Back to Editing" can still act on what they say.
                check_summary = _attach_preview_checks(qset, tossups, bonuses)

                return render(request, 'type_questions_preview.html',
                                         {'tossups': tossups,
                                          'bonuses': bonuses,
                                          'category_choices': _category_choices(qset),
                                          'tossup_errors': tossup_errors,
                                          'bonus_errors': bonus_errors,
                                          'category_errors': category_errors,
                                          'check_summary': check_summary,
                                          'message': 'Please verify that these questions have been correctly parsed. Hitting "Submit" will '\
                                          'commit these questions to the database. If you see any mistakes, hit "Cancel" and correct your mistakes.',
                                          'qset': qset,
                                          'user': user})
            else:
                question_data = request.POST['questions']
                tossups, bonuses = parse_packet_data(question_data, qset)
                messages.error(request, form.questions.errors)

        else:
            tossups = None
            bonuses = None
            messages.error(request, 'You do not have permission to add questions to this set')
            return render(request, 'type_questions.html',
                                     {'tossups': tossups,
                                      'bonuses': bonuses,
                                      'qset': qset,
                                      'user': user})
    elif request.method == 'GET':
        if (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
            dist_entries = qset.setwidedistributionentry_set.all().order_by('dist_entry__category', 'dist_entry__subcategory')

            form = TypeQuestionsForm(request.POST)
            return render(request, 'type_questions.html',
                                     {'user': user,
                                      'qset': qset,
                                      'form': form,
                                      'dist_entries': dist_entries})
        else:
            messages.error(request, 'You do not have permission to add questions to this set')
            return render(request, 'type_questions.html',
                                     {'qset': qset,
                                      'user': user})

@login_required
def type_questions_edit(request, question_type, question_id):
    user = request.user.writer
    
    if (question_type == "tossup"):
        question = Tossup.objects.get(id=question_id)
    elif (question_type == "bonus"):
        question = Bonus.objects.get(id=question_id)
    
    qset = question.question_set
    packet = question.packet
    message = ''
    message_class = ''
    read_only = True
    role = get_role_no_owner(user, qset)

    if request.method == 'POST':
        if (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
            form = TypeQuestionsForm(request.POST)
            if form.is_valid():
                question_data = request.POST['questions'].splitlines()
                tossups, bonuses, tossup_errors, bonus_errors = parse_packet_data(question_data, qset)

                return render(request, 'type_questions_edit_preview.html',
                                         {'tossups': tossups,
                                          'bonuses': bonuses,
                                          'tossup_errors': tossup_errors,
                                          'bonus_errors': bonus_errors,
                                          'message': 'Please verify that these questions have been correctly parsed. Hitting "Submit" will '\
                                          'commit these questions to the database. If you see any mistakes, hit "Cancel" and correct your mistakes.',
                                          'qset': qset,
                                          'user': user})
            else:
                question_data = request.POST['questions']
                tossups, bonuses = parse_packet_data(question_data, qset)
                messages.error(request, form.questions.errors)

        else:
            tossups = None
            bonuses = None
            messages.error(request, 'You do not have permission to edit this question')
            return render(request, 'type_questions_edit.html',
                                     {'tossups': tossups,
                                      'bonuses': bonuses,
                                      'qset': qset,
                                      'user': user})
    elif request.method == 'GET':
        if (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
            dist_entries = qset.setwidedistributionentry_set.all().order_by('dist_entry__category', 'dist_entry__subcategory')

            form = TypeQuestionsForm(request.POST)
            return render(request, 'type_questions_edit.html',
                                     {'user': user,
                                      'qset': qset,
                                      'form': form,
                                      'dist_entries': dist_entries})
        else:
            messages.error(request, 'You do not have permission to edit this question')
            return render(request, 'type_questions_edit.html',
                                     {'qset': qset,
                                      'user': user})

def _answer_preview(text, limit=60):
    """Short, markup-free answer for an error message."""
    plain = strip_markup(text or '').strip()
    return (plain[:limit] + '…') if len(plain) > limit else (plain or '(no answer)')


def _category_choices(qset):
    """The categories a typed question may be filed under: the label
    complete_upload matches on, plus the entry id the tag lookup needs.

    Taken from the whole distribution, which is what complete_upload validates
    against -- offering only the set-wide rows would leave out categories the
    server would have accepted.
    """
    entries = (DistributionEntry.objects.filter(distribution_id=qset.distribution_id)
               .order_by('category', 'subcategory'))
    return [{'id': e.id, 'label': str(e)} for e in entries]


@login_required
def typed_question_tags(request, qset_id):
    """The tag chips for one category, for a question being typed.

    The Type Questions preview asks for these when a category is chosen there:
    which tags a question may carry follows from its category, so a question
    that parsed without one has nothing to show until it has been given one.
    """
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return HttpResponse(json.dumps({'html': ''}), content_type='application/json')

    raw = (request.GET.get('category') or '').strip()
    entry = None
    if raw.isdigit():
        # Only a category of this set's own distribution: the id comes from a
        # form field, so it is the caller's word for it.
        entry = DistributionEntry.objects.filter(
            id=int(raw), distribution_id=qset.distribution_id).first()
    if entry is None:
        return HttpResponse(json.dumps({'html': ''}), content_type='application/json')

    # The chips have to post under the same field the page already uses for
    # this question, so the caller names it and it is checked rather than
    # trusted.
    m = re.match(r'^(tossup|bonus)-tags-(\d+)$', (request.GET.get('field') or '').strip())
    if not m:
        return HttpResponse(json.dumps({'html': ''}), content_type='application/json')

    from django.template.loader import render_to_string
    html = render_to_string('_tq_tag_chips.html',
                            {'tag_choices': build_tag_checkboxes(qset, None, entry),
                             'prefix': m.group(1), 'idx': m.group(2)},
                            request=request)
    return HttpResponse(json.dumps({'html': html}), content_type='application/json')


def _uncategorized_errors(tossups, bonuses):
    """Messages for parsed questions whose {Category - Subcategory} tag is
    missing or doesn't match the set's distribution. Empty list = all good."""
    errors = []
    for i, tu in enumerate(tossups or [], start=1):
        if tu.category is None:
            errors.append('Tossup {0} ("{1}") has no valid category tag. Add a '
                          '{{Category - Subcategory}} tag from this set\'s distribution '
                          'to the end of the answer line.'.format(i, _answer_preview(tu.tossup_answer)))
    for i, bs in enumerate(bonuses or [], start=1):
        if bs.category is None:
            errors.append('Bonus {0} ("{1}") has no valid category tag. Add a '
                          '{{Category - Subcategory}} tag from this set\'s distribution '
                          'to one of its answer lines.'.format(i, _answer_preview(bs.part1_answer)))
    return errors


def _typed_questions_done(request, qset_id, new_tossups, new_bonuses):
    """Where a batch of typed questions leaves you, however it was saved --
    through the confirmation screen or straight past it.

    A single question goes to its own edit page with the one-time style and
    repeat checks, the same as Add a Tossup does; a batch goes back to the set
    with a link to each question it made."""
    cache.clear()
    if len(new_tossups) + len(new_bonuses) == 1:
        only = (new_tossups or new_bonuses)[0]
        kind = 'tossup' if new_tossups else 'bonus'
        return HttpResponseRedirect('/edit_{0}/{1}/?new=1'.format(kind, only.id))

    messages.success(request, 'Your questions have been uploaded.', extra_tags='alert-box success')
    for tossup in new_tossups:
        messages.success(request, u'View your tossup on <a href="/edit_tossup/{0}">{1}.</a>'.format(tossup.id, get_answer_no_formatting(tossup.tossup_answer)), extra_tags='safe alert-box info')

    for bonus in new_bonuses:
        messages.success(request, u'View your bonus on <a href="/edit_bonus/{0}">{1}.</a>'.format(bonus.id, get_answer_no_formatting(bonus.part1_answer)), extra_tags='safe alert-box info')

    return HttpResponseRedirect('/edit_question_set/{0}'.format(qset_id))


def _tags_for_parsed_question(qset, question, preselected):
    """The tags a question would arrive with when the confirmation screen is
    skipped: what the Type Questions tag panel had ticked for the category it
    was filed under. The ids come from a form, so they are checked against the
    tags that actually apply rather than trusted."""
    entry = getattr(question, 'category', None)
    if entry is None:
        return []
    wanted = set(preselected.get(entry.id, ()))
    if not wanted:
        return []
    return [tag for tag in get_applicable_tags(qset, entry) if tag.id in wanted]


def _save_parsed_questions(request, qset, user, tossups, bonuses, preselected):
    """Save what Type Questions parsed, without going through the confirmation
    screen.

    The screen exists to let the category, the tags and the text be corrected
    before anything is written. Skipping it means taking the parse as it stands,
    so this saves exactly the objects the parser built -- the same ones the
    screen would have shown -- and tags them from the entry page's panel.
    """
    new_tossups, new_bonuses = [], []
    for tossup in tossups:
        tossup.author = user
        tossup.question_set = qset
        tossup.locked = False
        tossup.edited = False
        tossup.save_question(edit_type=QUESTION_CREATE, changer=user)
        for tag in _tags_for_parsed_question(qset, tossup, preselected):
            tag.tossups.add(tossup)
        new_tossups.append(tossup)
    for bonus in bonuses:
        bonus.author = user
        bonus.question_set = qset
        bonus.locked = False
        bonus.edited = False
        bonus.save_question(edit_type=QUESTION_CREATE, changer=user)
        for tag in _tags_for_parsed_question(qset, bonus, preselected):
            tag.bonuses.add(bonus)
        new_bonuses.append(bonus)
    return _typed_questions_done(request, qset.id, new_tossups, new_bonuses)


@login_required
def complete_upload(request):
    user = request.user.writer
    if request.method == 'POST':
        qset_id = request.POST['qset-id']
        qset = QuestionSet.objects.get(id=qset_id)

        if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
            messages.error(request, 'You are not authorized to add questions to this set!')
            return HttpResponseRedirect('/failure.html/')

        num_tossups = int(request.POST['num-tossups'])
        num_bonuses = int(request.POST['num-bonuses'])
        categories = DistributionEntry.objects.filter(distribution=qset.distribution)
        questionTypes = QuestionType.objects.all()

        # Type Questions requires every question to carry a category from this
        # set's distribution — an uncategorized question counts toward nothing
        # and is easy to lose track of. Enforced here as well as on the preview
        # so hiding the button isn't the only thing standing in the way. The
        # flag comes from the Type Questions preview only: a whole-packet file
        # upload still goes through (it just flags the untagged questions),
        # since refusing an entire packet over one tag would be worse.
        if request.POST.get('require-categories'):
            valid_categories = {'{0} - {1}'.format(c.category, c.subcategory) for c in categories}
            missing = []
            for tu_num in range(num_tossups):
                if request.POST.get('tossup-category-{0}'.format(tu_num), '') not in valid_categories:
                    missing.append('Tossup {0} ("{1}")'.format(
                        tu_num + 1, _answer_preview(request.POST.get('tossup-answer-{0}'.format(tu_num), ''))))
            for bs_num in range(num_bonuses):
                if request.POST.get('bonus-category-{0}'.format(bs_num), '') not in valid_categories:
                    missing.append('Bonus {0} ("{1}")'.format(
                        bs_num + 1, _answer_preview(request.POST.get('bonus-answer1-{0}'.format(bs_num), ''))))
            if missing:
                return render(request, 'failure.html', {
                    'message': 'Nothing was submitted. These questions have no valid '
                               '{Category - Subcategory} tag from this set\'s distribution: '
                               + '; '.join(missing)
                               + '. Go back, add the tags, and submit again.',
                    'message_class': 'alert-box alert'})

        new_tossups = []
        new_bonuses = []

        for tu_num in range(num_tossups):
            data="UTF-8 DATA"
            tu_text_name = 'tossup-text-{0}'.format(tu_num)
            tu_ans_name = 'tossup-answer-{0}'.format(tu_num)
            tu_cat_name = 'tossup-category-{0}'.format(tu_num)
            tu_type_name = 'tossup-type-{0}'.format(tu_num)

            tu_text = strip_markup(request.POST[tu_text_name])
            tu_ans = escape_answer_note_parens(strip_markup(request.POST[tu_ans_name]))
            tu_cat = request.POST[tu_cat_name]
            tu_type = request.POST[tu_type_name]

            new_tossup = Tossup()
            new_tossup.tossup_text = tu_text
            new_tossup.tossup_answer = tu_ans
            new_tossup.author = user
            new_tossup.question_set = qset

            for category in categories:
                formattedCategory = category.category + " - " + category.subcategory
                if (formattedCategory == tu_cat):
                    new_tossup.category = category
                    break

            for questionType in questionTypes:
                if (str(questionType) == tu_type):
                    new_tossup.question_type = questionType
                    break

            new_tossup.locked = False
            new_tossup.edited = False

            new_tossup.save_question(edit_type=QUESTION_CREATE, changer=user)
            _apply_typed_tags(request, qset, new_tossup,
                              'tossup-tags-{0}'.format(tu_num), is_tossup=True)
            new_tossups.append(new_tossup)

        for bs_num in range(num_bonuses):
            bs_leadin_name = 'bonus-leadin-{0}'.format(bs_num)

            bs_part1_name = 'bonus-part1-{0}'.format(bs_num)
            bs_ans1_name = 'bonus-answer1-{0}'.format(bs_num)
            bs_part2_name = 'bonus-part2-{0}'.format(bs_num)
            bs_ans2_name = 'bonus-answer2-{0}'.format(bs_num)
            bs_part3_name = 'bonus-part3-{0}'.format(bs_num)
            bs_ans3_name = 'bonus-answer3-{0}'.format(bs_num)
            bs_cat_name = 'bonus-category-{0}'.format(bs_num)
            bs_type_name = 'bonus-type-{0}'.format(bs_num)
            bs_type = request.POST[bs_type_name]

            new_bonus = Bonus()
            new_bonus.question_set = qset
            new_bonus.author = user
            new_bonus.edited = False
            new_bonus.locked = False
            new_bonus.leadin = strip_markup(request.POST[bs_leadin_name])
            new_bonus.part1_text = strip_markup(request.POST[bs_part1_name])
            new_bonus.part1_answer = escape_answer_note_parens(strip_markup(request.POST[bs_ans1_name]))
            new_bonus.part2_text = strip_markup(request.POST[bs_part2_name])
            new_bonus.part2_answer = escape_answer_note_parens(strip_markup(request.POST[bs_ans2_name]))
            new_bonus.part3_text = strip_markup(request.POST[bs_part3_name])
            new_bonus.part3_answer = escape_answer_note_parens(strip_markup(request.POST[bs_ans3_name]))
            new_bonus.part1_difficulty = request.POST.get('bonus-difficulty1-{0}'.format(bs_num), '')
            new_bonus.part2_difficulty = request.POST.get('bonus-difficulty2-{0}'.format(bs_num), '')
            new_bonus.part3_difficulty = request.POST.get('bonus-difficulty3-{0}'.format(bs_num), '')

            bonus_cat = request.POST[bs_cat_name]
            for category in categories:
                formattedCategory = category.category + " - " + category.subcategory
                if (formattedCategory == bonus_cat):
                    new_bonus.category = category
                    break

            for questionType in questionTypes:
                if (str(questionType) == bs_type):
                    new_bonus.question_type = questionType
                    break

            new_bonus.save_question(edit_type=QUESTION_CREATE, changer=user)
            _apply_typed_tags(request, qset, new_bonus,
                              'bonus-tags-{0}'.format(bs_num), is_tossup=False)
            new_bonuses.append(new_bonus)

        return _typed_questions_done(request, qset_id, new_tossups, new_bonuses)

    else:
        messages.error(request, 'Invalid request!')
        return render(request, 'failure.html')

@login_required
def settings(request):

    if request.method == 'GET':
        return render(request, 'settings.html', {})

    else:
        messages.error(request, 'Invalid request!')
        return render(request, 'failure.html', {})

@login_required
def profile(request):

    user = request.user
    writer = Writer.objects.get(user=user)

    if request.method == 'GET':
        initial_data = {'username': user.username,
                        'first_name': user.first_name,
                        'last_name': user.last_name,
                        'email': user.email,
                        'send_mail_on_comments': writer.send_mail_on_comments,
                        'search_in_new_tab': writer.search_in_new_tab}

        form = WriterChangeForm(initial=initial_data)

    elif request.method == 'POST':

        print(request.POST)
        form = WriterChangeForm(request.POST)

        if form.is_valid():
            user.username = form.cleaned_data['username']
            user.first_name = form.cleaned_data['first_name']
            user.last_name = form.cleaned_data['last_name']
            user.email = form.cleaned_data['email']
            writer.send_mail_on_comments = form.cleaned_data['send_mail_on_comments']
            writer.search_in_new_tab = form.cleaned_data['search_in_new_tab']
            user.save()
            writer.save()

    return render(request, 'profile.html',
            {'form': form,
             'user': request.user.writer})

@login_required()
def search(request, passed_qset_id=None):

    user = request.user.writer

    passed_q_set = None
    if passed_qset_id is not None:
        passed_q_set = QuestionSet.objects.get(id=passed_qset_id)

    question_sets = QuestionSet.objects.filter(Q(writer=user) | Q(editor=user) | Q(owner=user)).distinct()

    if request.method == 'GET':
        all_categories = [(cat.category, cat.subcategory) for cat in DistributionEntry.objects.all()]
        categories = []
        for cat in all_categories:
            if cat not in categories:
                categories.append(cat)

        if request.GET.dict() == {}:

            q_set = passed_q_set

            return render(request, 'search/search.html',
                                      {'user': user,
                                       'categories': categories,
                                       'q_sets': question_sets,
                                       'selected_qset': q_set,
                                       'tossups_selected': 'checked',
                                       'bonuses_selected': 'checked',
                                       'search_all_selected' :'unchecked',
                                       'passed_q_set': passed_q_set})

        else:
            query = request.GET.get('q')
            search_models = request.GET.getlist('models')
            qset_id = int(request.GET.get('qset'))
            qset = QuestionSet.objects.get(id=qset_id)
            search_category = request.GET.get('category')
            tossups_selected = "unchecked"
            bonuses_selected = "unchecked"
            search_all_selected = "unchecked"
            if 'qsub.tossup' in search_models:
                tossups_selected = "checked"
            if 'qsub.bonus' in search_models:
                bonuses_selected = "checked"
            if 'qsub.search_all' in search_models:
                search_all_selected = "checked"

            if user in qset.writer.all() or user in qset.editor.all() or qset.is_owner(user):
                # Postgres full-text search (icontains on SQLite) over the
                # maintained search fields, scoped to the relevant set(s).
                if search_all_selected == 'checked':
                    set_ids = list(question_sets.values_list('id', flat=True))
                else:
                    set_ids = [qset.id]

                questions = []
                if 'qsub.tossup' in search_models:
                    tu_qs = (Tossup.objects.filter(question_set_id__in=set_ids)
                             .select_related(*QUESTION_LIST_RELATED)
                             .prefetch_related('category_tags'))
                    questions += list(fulltext_filter(tu_qs, query))
                if 'qsub.bonus' in search_models:
                    bs_qs = (Bonus.objects.filter(question_set_id__in=set_ids)
                             .select_related(*QUESTION_LIST_RELATED)
                             .prefetch_related('category_tags'))
                    questions += list(fulltext_filter(bs_qs, query))

                if search_category and search_category != 'All':
                    questions = [q for q in questions if str(q.category) == search_category]

                # The results table is the set's own question table, so the
                # columns it can show are the ones the category pages show --
                # tags and comments among them, which cost a query per row
                # unless they are loaded for the whole page at once.
                attach_question_comments(
                    {q.id: q for q in questions if isinstance(q, Tossup)},
                    {q.id: q for q in questions if isinstance(q, Bonus)})

                result = questions
                message = ''
                message_class = ''

            else:
                result = []
                message = 'You are not authorized to view questions from this set.'
                message_class = 'alert-box alert'

            return render(request, 'search/search.html',
                                      {'user': user,
                                       'categories': categories,
                                       'q_sets': question_sets,
                                       'result': result,
                                       'search_term': query,
                                       'search_category': search_category,
                                       'selected_qset': qset,
                                       'tossups_selected': tossups_selected,
                                       'bonuses_selected': bonuses_selected,
                                       'search_all_selected': search_all_selected,
                                       # The columns the set you are searching
                                       # shows on its own question tables. A
                                       # search can span sets, but it is nearly
                                       # always run from the set you are working
                                       # in -- which is the one the form carries
                                       # -- so that is whose layout the results
                                       # take, rather than a fixed one that
                                       # ignores the preference entirely.
                                       'table_columns': qset.question_table_headers(),
                                       # Which set a result came from only needs
                                       # saying when they can differ.
                                       'show_tournament': search_all_selected == 'checked',
                                       'passed_q_set': passed_q_set,
                                       'message': message,
                                       'message_class': message_class})

def _quick_search_scope(user, qset_param):
    """(scope_ids, selected_value) for quick search. `scope_ids` is the set of
    QuestionSet ids the user may search; `selected_value` is 'all' or the chosen
    id as a string. Only sets the user can access are ever included."""
    accessible = list(QuestionSet.objects.filter(
        Q(writer=user) | Q(editor=user) | Q(owner=user) | Q(co_owners=user)
    ).distinct().values_list('id', flat=True))
    if qset_param and qset_param != 'all':
        try:
            sid = int(qset_param)
        except (TypeError, ValueError):
            sid = None
        if sid in set(accessible):
            return [sid], str(sid)
    return accessible, 'all'


@login_required
def quick_search(request, passed_qset_id=None):
    """Fast type-ahead search over answer lines, filterable by category, packet,
    and writer. This view only renders the page shell and the filter facets for
    the chosen scope; results stream in from quick_search_results as the user
    types."""
    user = request.user.writer
    qset_param = request.GET.get('qset') or (str(passed_qset_id) if passed_qset_id else 'all')
    scope_ids, selected = _quick_search_scope(user, qset_param)
    multi = len(scope_ids) != 1  # spanning multiple sets: qualify facets by set

    accessible = QuestionSet.objects.filter(
        Q(writer=user) | Q(editor=user) | Q(owner=user) | Q(co_owners=user)
    ).distinct().order_by('-date', 'name')

    packets = (Packet.objects.filter(question_set_id__in=scope_ids)
               .select_related('question_set')
               .order_by('question_set__name', 'sort_order', 'packet_name'))
    packet_facets = [{
        'id': p.id,
        'name': '{0} — {1}'.format(p.question_set.name, p.packet_name) if multi else p.packet_name,
    } for p in packets]

    writers = (Writer.objects.filter(
        Q(tossup__question_set_id__in=scope_ids) | Q(bonus__question_set_id__in=scope_ids))
        .select_related('user').distinct())
    writer_facets = sorted(
        ({'id': w.id, 'name': w.get_real_name().strip() or w.user.username} for w in writers),
        key=lambda w: w['name'].lower())

    dist_ids = list(QuestionSet.objects.filter(id__in=scope_ids).values_list('distribution_id', flat=True))
    seen, category_facets = set(), []
    for cat, sub in DistributionEntry.objects.filter(
            distribution_id__in=dist_ids).values_list('category', 'subcategory'):
        label = '{0} - {1}'.format(cat, sub)
        if label not in seen:
            seen.add(label)
            category_facets.append(label)
    category_facets.sort()

    passed_q_set = QuestionSet.objects.filter(id__in=scope_ids).first() if len(scope_ids) == 1 else None

    return render(request, 'quick_search.html', {
        'user': user,
        'q_sets': accessible,
        'selected_qset': selected,
        'passed_q_set': passed_q_set,
        'packet_facets': packet_facets,
        'writer_facets': writer_facets,
        'category_facets': category_facets,
        'multi_set': multi,
    })


@login_required
def quick_search_results(request):
    """JSON answer-line search for the quick-search page. Matches the maintained
    (markup-stripped) search_question_answers field with optional category /
    packet / writer filters, and returns each hit's rendered content for inline
    preview plus an edit URL to open it in a new tab."""
    user = request.user.writer
    scope_ids, _ = _quick_search_scope(user, request.GET.get('qset', 'all'))
    if not scope_ids:
        return JsonResponse({'results': [], 'truncated': False})

    norm_q = strip_special_chars((request.GET.get('q') or '').strip())
    category = (request.GET.get('category') or '').strip()
    packet_id = request.GET.get('packet') or ''
    writer_id = request.GET.get('writer') or ''
    types = request.GET.getlist('types') or ['tossup', 'bonus']

    # Don't dump the whole scope when nothing is specified.
    if not (norm_q or category or packet_id or writer_id):
        return JsonResponse({'results': [], 'truncated': False, 'empty': True})

    cat_ids = None
    if category:
        dist_ids = list(QuestionSet.objects.filter(id__in=scope_ids).values_list('distribution_id', flat=True))
        cat_ids = [d.id for d in DistributionEntry.objects.filter(distribution_id__in=dist_ids)
                   if str(d) == category]
        if not cat_ids:
            return JsonResponse({'results': [], 'truncated': False})

    LIMIT = 50
    multi = len(scope_ids) != 1

    def scoped(qs):
        qs = qs.filter(question_set_id__in=scope_ids)
        if norm_q:
            qs = qs.filter(search_question_answers__icontains=norm_q)
        if cat_ids is not None:
            qs = qs.filter(category_id__in=cat_ids)
        if packet_id:
            qs = qs.filter(packet_id=packet_id)
        if writer_id:
            qs = qs.filter(author_id=writer_id)
        return (qs.select_related('packet', 'category', 'author', 'author__user', 'question_set')
                  .prefetch_related('category_tags')
                  .order_by('question_set__name', 'packet__sort_order', 'packet__packet_name', 'question_number'))

    rows = []
    if 'tossup' in types:
        rows += [('tossup', t) for t in scoped(Tossup.objects.all())[:LIMIT + 1]]
    if 'bonus' in types:
        rows += [('bonus', b) for b in scoped(Bonus.objects.all())[:LIMIT + 1]]
    truncated = len(rows) > LIMIT
    rows = rows[:LIMIT]

    def answer_preview(qtype, q):
        if qtype == 'tossup':
            return _grid_answer_preview(q.tossup_answer, 90)
        return ' / '.join(filter(None, [
            _grid_answer_preview(q.part1_answer, 30),
            _grid_answer_preview(q.part2_answer, 30),
            _grid_answer_preview(q.part3_answer, 30)]))

    data = []
    for qtype, q in rows:
        pkt = q.packet.packet_name if q.packet_id else '(unpacketized)'
        loc = '{0} #{1}'.format(pkt, q.question_number) if q.question_number else pkt
        if multi:
            loc = '{0} · {1}'.format(q.question_set.name, loc)
        data.append({
            'type': qtype,
            'id': q.id,
            'answer': answer_preview(qtype, q),
            'category': str(q.category) if q.category_id else '',
            # What a question is tagged with is most of what you are looking
            # for when searching an archive -- "the Asian Literature ones" --
            # and the answer line alone does not say it.
            'tags': sorted(t.name for t in q.category_tags.all()),
            'location': loc,
            'author': (q.author.get_real_name().strip() or q.author.user.username) if q.author_id else '',
            'edit_url': '/edit_{0}/{1}/'.format(qtype, q.id),
            'content': q.to_html(),
        })

    return JsonResponse({'results': data, 'truncated': truncated})


@login_required
def logout_view(request):
    logout(request)
    return HttpResponseRedirect("/main/")

def forgot_username(request):
    sent = False
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        if email:
            from django.contrib.auth.models import User
            from django.core.mail import send_mail
            from django.conf import settings as django_settings
            users = User.objects.filter(email__iexact=email)
            if users.exists():
                usernames = ', '.join(u.username for u in users)
                send_mail(
                    'Your QEMS2 Username',
                    f'Your username is: {usernames}\n\nYou can sign in at {request.build_absolute_uri("/accounts/login/")}',
                    django_settings.DEFAULT_FROM_EMAIL,
                    [email],
                    fail_silently=False,
                )
        # Always show success to prevent email enumeration
        sent = True
    return render(request, 'account/forgot_username.html', {'sent': sent})

def _move_success_context(question, qtype, q_set, dest_qset):
    """What the move did, for the page that reports it.

    The category, author and tags were settled on the confirmation step, so
    this says what they ended up as rather than telling anyone to go and set
    them. The packet genuinely is open -- a packet belongs to the set the
    question left -- and the destination's length limit is its own, so both are
    worth naming here.
    """
    limit = (dest_qset.max_acf_tossup_length if qtype == 'tossup'
             else dest_qset.max_acf_bonus_length)
    count = question.character_count()
    return {
        'question': question, 'qtype': qtype,
        'q_set': q_set, 'dest_qset': dest_qset,
        'edit_url': '/edit_{0}/{1}/'.format(qtype, question.id),
        'carried_tags': list(question.category_tags.all()),
        'char_count': count,
        'char_limit': limit,
        'over_limit': bool(limit and count > limit),
    }


def _move_repeat_matches(question, qtype, dest_qset):
    """Questions already in `dest_qset` that answer the same thing.

    The same check the add pages run after a submit, pointed at the set the
    question is moving to: the repeat that matters is a repeat where it is
    going. Empty when the destination has duplicate checking turned off --
    reading a whole set to answer this is exactly what that switch is for.
    """
    if not dest_qset.enable_duplicate_checks:
        return []
    matches = find_answer_matches(dest_qset, question, qtype)
    for m in matches:
        m['answer_html'] = _dup_render_answer(m['answer_raw'])
    return matches


def _move_confirm_context(question, qtype, q_set, dest_qset, post=None):
    """What moving this question would do to the things it carries.

    A question's category is a row in its set's distribution, its tags are rows
    owned by its set, and its author is someone who writes for its set. None of
    the three survives a move by itself. This works out what the destination can
    offer for each, preferring what the question already has where the
    destination has the same thing by name.

    `post` is the confirmation form coming back, so a re-render (a changed
    category brings different tags) keeps what was chosen.
    """
    dest_entries = [e.dist_entry for e in dest_qset.setwidedistributionentry_set
                    .select_related('dist_entry').order_by('dist_entry__category',
                                                           'dist_entry__subcategory')]
    current_path = str(question.category) if question.category_id else ''
    twin = next((e for e in dest_entries if str(e) == current_path), None)

    if post is not None and 'category' in post:
        raw = (post.get('category') or '').strip()
        selected = next((e for e in dest_entries if str(e.id) == raw), None)
    else:
        selected = twin

    # Tags the destination could give the question, for the category it will
    # land in. A tag with the same name on the same path is the same tag as far
    # as a writer is concerned, so it comes across ticked.
    carried_names = set()
    dest_tags = []
    if selected is not None or not current_path:
        applicable = get_applicable_tags(dest_qset, selected)
        mine = {(t.category_path, t.name) for t in question.category_tags.all()}
        if post is not None and 'confirm' in post:
            chosen = {int(v) for v in post.getlist('tags') if str(v).isdigit()}
        else:
            chosen = None
        for tag in applicable:
            carried = (tag.category_path, tag.name) in mine
            if carried:
                carried_names.add(tag.name)
            dest_tags.append({'tag': tag, 'carried': carried,
                              'checked': carried if chosen is None else tag.id in chosen})

    dropped = [t for t in question.category_tags.all()
               if t.question_set_id != dest_qset.id and t.name not in carried_names]

    dest_writers = list(dict.fromkeys(
        list(dest_qset.all_owners()) + list(dest_qset.editor.all())
        + list(dest_qset.writer.all())))
    author_id = question.author_id
    if post is not None and 'author' in post:
        raw = (post.get('author') or '').strip()
        author_id = int(raw) if raw.isdigit() else None

    return {
        'user': None, 'q_set': q_set, 'dest_qset': dest_qset,
        'question': question, 'qtype': qtype,
        'edit_url': '/edit_{0}/{1}/'.format(qtype, question.id),
        # What the destination already has on this answer. A question that is
        # not a repeat where it was written can easily be one where it is
        # going, and after the move it is too late to find out cheaply.
        'dest_repeats': _move_repeat_matches(question, qtype, dest_qset),
        'dest_repeats_off': not dest_qset.enable_duplicate_checks,
        'dest_categories': dest_entries,
        'category_match': str(twin) if twin is not None else '',
        'selected_category_id': selected.id if selected is not None else None,
        'dest_tags': dest_tags,
        'dropped_tags': dropped,
        'dest_writers': dest_writers,
        'current_author_id': question.author_id,
        'selected_author_id': author_id,
        'current_author_outside': (question.author_id is not None
                                   and question.author not in dest_writers),
        'message': '', 'message_class': '',
    }


def _apply_move(question, qtype, dest_qset, post):
    """Move the question and set what the confirmation asked for."""
    dest_entries = {e.dist_entry_id: e.dist_entry
                    for e in dest_qset.setwidedistributionentry_set.select_related('dist_entry')}
    raw_cat = (post.get('category') or '').strip()
    category = dest_entries.get(int(raw_cat)) if raw_cat.isdigit() else None

    raw_author = (post.get('author') or '').strip()
    author = Writer.objects.filter(id=int(raw_author)).first() if raw_author.isdigit() else None
    # A question must have an author; if the form didn't name a usable one,
    # keep the one it has rather than failing the save outright.
    author = author or question.author

    wanted = {int(v) for v in post.getlist('tags') if str(v).isdigit()}

    question.question_set = dest_qset
    question.packet = None
    question.category = category
    question.author = author
    question.save()

    # Only tags of the destination, and only the ones ticked: a tag of the old
    # set left attached would count a question it no longer has.
    keep = [t for t in get_applicable_tags(dest_qset, category) if t.id in wanted]
    question.category_tags.set(keep)
    cache.clear()
    return question


@login_required
def move_tossup(request, q_set_id, tossup_id):
    user = request.user.writer
    q_set = QuestionSet.objects.get(id=q_set_id)
    role = get_role_no_owner(user, q_set)
    
    tossup = Tossup.objects.get(id=tossup_id)
    if (tossup is None or tossup.question_set != q_set):
        message = 'Invalid tossup'
        message_class = 'alert-box alert'
        tossup = None

    # By name, and without the sets that have been archived: the list is for
    # picking a destination, and a set someone archived is not one.
    move_sets = (user.question_set_editor
                 .exclude(id=q_set_id).exclude(archived=True).order_by('name'))

    if request.method == 'GET':
        if (role == "editor"):
            if (tossup is not None):
                form = MoveTossupForm(move_sets=move_sets)

                message = ''
                message_class = ''

                return render(request, 'move_tossup.html',
                                    {'user': user,
                                     'q_set': q_set,
                                     'form': form,
                                     'tossup': tossup,
                                     'message': message,
                                     'message_class': message_class})
            else:
                form = []
                return render(request, 'move_tossup.html',
                                    {'user': user,
                                     'q_set': q_set,
                                     'form': form,
                                     'tossup': tossup,
                                     'message': message,
                                     'message_class': message_class})
        else:
            form = []
            message = 'You do not have permissions to move this question.'
            message_class = 'alert-box alert'
            q_set = []
            return render(request, 'move_tossup.html',
                                {'user': user,
                                 'q_set': q_set,
                                 'tossup': None,
                                 'form': form,
                                 'message': message,
                                 'message_class': message_class})

    else:
        # Update the question set for this tossup
        if (role == 'editor'):
            form = MoveTossupForm(request.POST, move_sets=move_sets)

            if form.is_valid():
                dest_qset_id = request.POST["move_sets"]
                dest_qset = QuestionSet.objects.get(id=dest_qset_id)

                if (tossup is not None and dest_qset is not None):
                    # Choosing the destination is the first half; the second is
                    # deciding what the question's category, author and tags
                    # become there, since none of them travels on its own.
                    if not request.POST.get('confirm'):
                        ctx = _move_confirm_context(tossup, 'tossup', q_set, dest_qset,
                                                    request.POST)
                        ctx['user'] = user
                        return render(request, 'move_question_confirm.html', ctx)

                    _apply_move(tossup, 'tossup', dest_qset, request.POST)
                    ctx = _move_success_context(tossup, 'tossup', q_set, dest_qset)
                    ctx['user'] = user
                    return render(request, 'move_question_success.html', ctx)
                else:
                    message = 'There was an error with your submission.  Hit the back button and make sure you selected a valid question set to move to.'
                    message_class = 'alert-box warning'

                    return render(request, 'move_tossup.html',
                                        {'user': user,
                                         'q_set': q_set,
                                         'form': form,
                                         'tossup': tossup,
                                         'message': message,
                                         'message_class': message_class})
            else:
                message = 'There was an error moving your question.  Hit the back button and make sure you selected a valid question set to move to.'
                message_class = 'alert-box warning'
                q_set = []
                return render(request, 'move_tossup.html',
                                    {'user': user,
                                     'q_set': q_set,
                                     'tossup': None,
                                     'form': form,
                                     'message': message,
                                     'message_class': message_class})

        else:
            message = 'You do not have permissions to move this question.'
            message_class = 'alert-box alert'
            q_set = []
            form = []
            return render(request, 'move_tossup.html',
                                {'user': user,
                                 'q_set': q_set,
                                 'tossup': None,
                                 'form': form,
                                 'message': message,
                                 'message_class': message_class})

@login_required
def move_bonus(request, q_set_id, bonus_id):
    user = request.user.writer
    q_set = QuestionSet.objects.get(id=q_set_id)
    role = get_role_no_owner(user, q_set)

    bonus = Bonus.objects.get(id=bonus_id)
    if (bonus is None or bonus.question_set != q_set):
        message = 'Invalid bonus'
        message_class = 'alert-box alert'
        bonus = None

    # By name, and without the sets that have been archived: the list is for
    # picking a destination, and a set someone archived is not one.
    move_sets = (user.question_set_editor
                 .exclude(id=q_set_id).exclude(archived=True).order_by('name'))

    if request.method == 'GET':
        if (role == 'editor'):
            if (bonus is not None):
                form = MoveBonusForm(move_sets=move_sets)

                message = ''
                message_class = ''

                return render(request, 'move_bonus.html',
                                    {'user': user,
                                     'q_set': q_set,
                                     'form': form,
                                     'bonus': bonus,
                                     'message': message,
                                     'message_class': message_class})
            else:
                form = []
                return render(request, 'move_bonus.html',
                                    {'user': user,
                                     'q_set': q_set,
                                     'form': form,
                                     'bonus': bonus,
                                     'message': message,
                                     'message_class': message_class})
        else:
            form = []
            message = 'You do not have permissions to move this question.'
            message_class = 'alert-box alert'
            q_set = []
            return render(request, 'move_bonus.html',
                                {'user': user,
                                 'q_set': q_set,
                                 'bonus': None,
                                 'form': form,
                                 'message': message,
                                 'message_class': message_class})

    else:
        # Update the question set for this bonus
        if (role == 'editor'):
            form = MoveBonusForm(request.POST, move_sets=move_sets)
            if form.is_valid():
                dest_qset_id = request.POST["move_sets"]
                dest_qset = QuestionSet.objects.get(id=dest_qset_id)

                if (bonus is not None and dest_qset is not None):
                    if not request.POST.get('confirm'):
                        ctx = _move_confirm_context(bonus, 'bonus', q_set, dest_qset,
                                                    request.POST)
                        ctx['user'] = user
                        return render(request, 'move_question_confirm.html', ctx)

                    _apply_move(bonus, 'bonus', dest_qset, request.POST)
                    ctx = _move_success_context(bonus, 'bonus', q_set, dest_qset)
                    ctx['user'] = user
                    return render(request, 'move_question_success.html', ctx)
                else:
                    message = 'There was an error with your submission.  Hit the back button and make sure you selected a valid question set to move to.'
                    message_class = 'alert-box warning'

                    return render(request, 'move_bonus.html',
                                        {'user': user,
                                         'q_set': q_set,
                                         'form': form,
                                         'bonus': bonus,
                                         'message': message,
                                         'message_class': message_class})
            else:
                message = 'There was an error moving your question.  Hit the back button and make sure you selected a valid question set to move to.'
                message_class = 'alert-box warning'
                q_set = []
                return render(request, 'move_bonus.html',
                                    {'user': user,
                                     'q_set': q_set,
                                     'bonus': None,
                                     'form': form,
                                     'message': message,
                                     'message_class': message_class})

        else:
            message = 'You do not have permissions to move this question.'
            message_class = 'alert-box alert'
            q_set = []
            form = []
            return render(request, 'move_bonus.html',
                                {'user': user,
                                 'q_set': q_set,
                                 'bonus': None,
                                 'form': form,
                                 'message': message,
                                 'message_class': message_class})

#: How a pronunciation guide is set in exported Word documents: a gray
#: sans-serif aside, distinct from the serif question text around it.
PRONUNCIATION_GUIDE_FONT = 'Source Sans Pro'
PRONUNCIATION_GUIDE_COLOR = RGBColor(0x80, 0x80, 0x80)

#: A marked note to the moderator or players (\Ntext\N) in exported Word
#: documents: italic and dark grey — still clearly meant to be read, but not
#: mistaken for a clue.
QUESTION_NOTE_COLOR = RGBColor(0x55, 0x55, 0x55)


def add_qems_formatted_runs(paragraph, text, bold=False, is_answer=False, smart_quotes=False,
                            all_power=False, allow_superpower=True, quoted_guides=False):
    """Convert QEMS markup to python-docx runs on a paragraph.

    Markup rules (mirroring get_formatted_question_html in utils.py):
      _text_   → bold + underline (answer line)
      __text__ → underline only (prompt)
      ~text~   → italic
      (text)   → pronunciation guide: gray, sans-serif, never bold
      (*)      → bold (power mark)
      \\s / \\S  → subscript / superscript (approximated with smaller font)
      \\Ptext\\P → pronunciation-guide target word(s): rendered in a teal color

    A guide keeps its own look even inside a bolded power region — it's an aside
    to the moderator, not part of what's read for points, and bolding it made it
    compete with the clue text.
    """
    if text is None:
        return paragraph

    # Stored question text is HTML-escaped (e.g. apostrophes as &#x27; from the
    # YAPP import). Word gets raw characters, not a browser, so decode entities
    # first or they'd render literally as "&#x27;".
    text = html.unescape(text)
    if smart_quotes:
        text = smarten_quotes(text)
    # A set may require a guide to carry quotation marks; escaping the rest makes
    # them print as the ordinary parentheses they are. Done after unescaping so
    # the quotes are visible to the test.
    if quoted_guides:
        text = escape_unquoted_parens(text)

    allow_underlines = True
    allow_parens = True
    allow_powers = not is_answer  # powers only in question text

    italics_flag = False
    parens_flag = False
    underline_flag = False
    prompt_flag = False
    power_flag = False
    power_index = -1
    sub_flag = False
    super_flag = False
    bold_flag = False
    pg_flag = False
    note_flag = False
    need_restore_italics = False

    if allow_powers:
        # Bold runs to the last power mark: a 20-point superpower "(+)" precedes
        # the 15-point power "(*)"; either is optional. (+) is only honored when
        # the set enables superpower.
        plus_index = text.find("(+)") if allow_superpower else -1
        power_index = max(text.find("(*)"), plus_index)
        if power_index > -1:
            power_flag = True

    # All-power stem with no explicit mark: bold from end to end.
    all_power_wrap = allow_powers and all_power and power_index == -1

    buf = ""
    # Current formatting state
    cur_bold = bold or power_flag or all_power_wrap
    cur_italic = False
    cur_underline = False
    cur_sub = False
    cur_super = False

    def flush(b=cur_bold, i=cur_italic, u=cur_underline, sub=cur_sub, sup=cur_super,
              pg=False, guide=False):
        nonlocal buf
        if buf:
            run = paragraph.add_run(buf)
            # A note to the moderator/players keeps its own look wherever it
            # falls — italic, grey, never bold — the same way a pronunciation
            # guide does: it is read out, but it isn't clue text, so it
            # shouldn't read as part of a bolded power region. `note_flag` is
            # read live from the enclosing scope rather than passed in, since
            # every caller flushes the buffer *before* toggling a flag, so the
            # value here is the one the buffered text was written under.
            run.bold = False if (guide or note_flag) else b
            run.italic = i or note_flag
            run.underline = u
            if sub:
                run.font.subscript = True
            if sup:
                run.font.superscript = True
            if note_flag and not guide:
                run.font.color.rgb = QUESTION_NOTE_COLOR
            if guide:
                # The parenthesized guide itself: a gray sans-serif aside, set
                # apart from the question text a moderator actually reads.
                run.font.name = PRONUNCIATION_GUIDE_FONT
                run.font.color.rgb = PRONUNCIATION_GUIDE_COLOR
            elif pg:
                # Pronunciation-guide target word(s): a teal tint, matching the
                # web view's .pg-target color.
                run.font.color.rgb = RGBColor(0x0B, 0x72, 0x85)
            buf = ""

    def current_state():
        b = bold or power_flag or underline_flag or bold_flag or all_power_wrap
        i = italics_flag
        u = underline_flag or prompt_flag
        return b, i, u, sub_flag, super_flag, pg_flag, parens_flag

    index = 0
    prev = ""
    prev2 = ""

    while index < len(text):
        c = text[index]
        next_c = text[index + 1] if index < len(text) - 1 else ""

        new_bold, new_italic, new_underline, new_sub, new_sup, new_pg, new_guide = current_state()

        # Power mark ((*) or (+))
        if index == power_index and power_flag:
            flush(new_bold, new_italic, new_underline, new_sub, new_sup, new_pg, new_guide)
            run = paragraph.add_run(text[index:index + 3])
            run.bold = True
            power_flag = False
            index += 3
            prev2, prev = prev, ")"
            continue

        # Tildes → italic toggle
        if c == "~":
            flush(new_bold, new_italic, new_underline, new_sub, new_sup, new_pg, new_guide)
            italics_flag = not italics_flag
            index += 1
            prev2, prev = prev, c
            continue

        # A power/superpower mark that isn't the one `power_index` points at
        # (a stem can carry both "(+)" and "(*)"; power_index is the later one).
        # It's a scoring mark, not a guide, so it stays bold.
        if (c == "(" and allow_parens and prev != "\\"
                and text[index:index + 3] in ("(*)", "(+)")):
            flush(new_bold, new_italic, new_underline, new_sub, new_sup, new_pg, new_guide)
            paragraph.add_run(text[index:index + 3]).bold = True
            index += 3
            prev2, prev = prev, ")"
            continue

        # Open paren (pronunciation guide)
        if c == "(" and allow_parens and prev != "\\":
            flush(new_bold, new_italic, new_underline, new_sub, new_sup, new_pg, new_guide)
            if italics_flag:
                need_restore_italics = True
                italics_flag = False
            # Set even inside a power region: a guide is styled as a guide
            # wherever it falls, rather than inheriting the region's bold.
            parens_flag = True
            buf = "("
            index += 1
            prev2, prev = prev, c
            continue

        # Escaped open paren
        if c == "(" and allow_parens and prev == "\\" and prev2 != "\\":
            # Remove the backslash from buffer
            if buf.endswith("\\"):
                buf = buf[:-1]
            buf += c
            index += 1
            prev2, prev = prev, c
            continue

        # Close paren
        if c == ")" and allow_parens and prev != "\\":
            buf += ")"
            new_b, new_i, new_u, new_sub2, new_sup2, new_pg2, new_guide2 = current_state()
            flush(new_b, new_i, new_u, new_sub2, new_sup2, new_pg2, new_guide2)
            parens_flag = False
            if need_restore_italics:
                italics_flag = True
                need_restore_italics = False
            index += 1
            prev2, prev = prev, c
            continue

        # Escaped close paren
        if c == ")" and allow_parens and prev == "\\":
            if buf.endswith("\\"):
                buf = buf[:-1]
            buf += c
            index += 1
            prev2, prev = prev, c
            continue

        # Subscript toggle: \s
        if c == "s" and prev == "\\" and prev2 != "\\" and not super_flag:
            if buf.endswith("\\"):
                buf = buf[:-1]
            flush(*current_state())
            sub_flag = not sub_flag
            index += 1
            prev2, prev = prev, c
            continue

        # Superscript toggle: \S
        if c == "S" and prev == "\\" and prev2 != "\\" and not sub_flag:
            if buf.endswith("\\"):
                buf = buf[:-1]
            flush(*current_state())
            super_flag = not super_flag
            index += 1
            prev2, prev = prev, c
            continue

        # Bold-only toggle: \B
        if c == "B" and prev == "\\" and prev2 != "\\":
            if buf.endswith("\\"):
                buf = buf[:-1]
            flush(*current_state())
            bold_flag = not bold_flag
            index += 1
            prev2, prev = prev, c
            continue

        # Pronunciation-guide target toggle: \P
        if c == "P" and prev == "\\" and prev2 != "\\":
            if buf.endswith("\\"):
                buf = buf[:-1]
            flush(*current_state())
            pg_flag = not pg_flag
            index += 1
            prev2, prev = prev, c
            continue

        # Note to the moderator/players toggle: \N
        if c == "N" and prev == "\\" and prev2 != "\\":
            if buf.endswith("\\"):
                buf = buf[:-1]
            flush(*current_state())
            note_flag = not note_flag
            index += 1
            prev2, prev = prev, c
            continue

        # Underline markup
        if c == "_" and allow_underlines:
            if next_c == "_":
                # Double underscore → prompt (underline only)
                flush(*current_state())
                prompt_flag = not prompt_flag
                index += 2
                prev2, prev = "_", "_"
                continue
            else:
                # Single underscore → answer line (bold + underline)
                flush(*current_state())
                underline_flag = not underline_flag
                index += 1
                prev2, prev = prev, c
                continue

        # Regular character
        buf += c
        index += 1
        prev2, prev = prev, c

    # Flush remaining buffer
    flush(*current_state())
    return paragraph


@login_required
def export_question(request, question_type, question_id, output_format):
    """Download one question as a YAPP/YAPP2 JSON file.

    MODAQ and the other YAPP readers open a *packet*, not a question, so the
    single question is wrapped in a packet whose other array is empty — that is
    a valid YAPP file, and it opens in the reader with exactly this question in
    it. Handy for checking how one question reads, or for handing a single
    question to someone without exporting the whole set.

    Anyone who can see the question on its edit page can export it; the file
    holds nothing the page doesn't already show.
    """
    from . import yapp_export

    is_tossup = question_type == 'tossup'
    model = Tossup if is_tossup else Bonus
    question = get_object_or_404(model, id=question_id)
    qset = question.question_set
    user = request.user.writer

    if not (question.author == user or _is_set_member(user, qset)):
        return render(request, 'failure.html',
                      {'message': 'You are not authorized to view or export this question!',
                       'message_class': 'alert-box alert'})

    version = 2 if output_format == 'yapp2-json' else 1
    label = 'YAPP2' if version >= 2 else 'YAPP'
    tossups = [question] if is_tossup else []
    bonuses = [] if is_tossup else [question]
    # `name` is a YAPP2 addition, so a version-1 file leaves it out — same as the
    # set-wide export, which keeps its plain-YAPP output free of YAPP2 fields.
    name = (question.packet.packet_name if question.packet else qset.name) or None
    payload = yapp_export.packet_to_yapp(
        tossups, bonuses, version=version, name=name if version >= 2 else None,
        tag_names=(_tag_names_by_question(qset, for_output=True)
                   if qset.export_category_tags else {}))

    # Name the file after the answerline, so a folder of these is readable.
    answer = get_answer_no_formatting(get_primary_answer(
        question.tossup_answer if is_tossup else question.part1_answer))
    answer = html.unescape(answer or '').strip()[:60].strip()
    base = answer or '{0} {1}'.format(question_type.capitalize(), question.id)
    filename = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', '{0} - {1}.json'.format(base, label))

    response = HttpResponse(json.dumps(payload, ensure_ascii=False, indent=2),
                            content_type='application/json')
    response['Content-Disposition'] = 'attachment; filename="{0}"'.format(filename)
    return response


@login_required
def export_question_set(request, qset_id, output_format):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    role = get_role_no_owner(user, qset)

    tossup_content_type_id = ContentType.objects.get_for_model(Tossup).id
    bonus_content_type_id = ContentType.objects.get_for_model(Bonus).id

    def safe_text(val):
        """Safely convert a value to string and remove newlines."""
        if val is None:
            return ""
        return remove_new_lines(str(val))

    def safe_name(writer_obj):
        """Safely get a writer's real name, handling None."""
        if writer_obj is None:
            return ""
        try:
            return writer_obj.get_real_name()
        except Exception:
            return str(writer_obj)

    def safe_category(cat_obj):
        """Safely convert a category to string."""
        if cat_obj is None:
            return ""
        return str(cat_obj)

    def safe_packet(packet_obj):
        """Safely convert a packet to string."""
        if packet_obj is None:
            return ""
        return str(packet_obj)

    def build_tag_string(question):
        """A question's tags for the export sheet: "Group: Name" pieces joined
        by "||", matching how the Comments column is written. A tag with no
        group is just its name."""
        pieces = []
        for tag in question.category_tags.all():
            group = (tag.group_name or '').strip()
            pieces.append('{0}: {1}'.format(group, tag.name) if group else tag.name)
        return '||'.join(pieces)

    def build_comment_string(content_type_id, object_id):
        """Build a comment string including threaded replies."""
        comment_list = Comment.objects.filter(
            content_type_id=content_type_id,
            object_pk=object_id,
            is_removed=False
        ).order_by('submit_date')

        # Get reply mappings
        reply_comment_ids = set()
        parent_map = {}
        for cr in CommentReply.objects.filter(comment__in=comment_list):
            reply_comment_ids.add(cr.comment_id)
            parent_map[cr.comment_id] = cr.parent_id

        # Build replies dict: parent_id -> [comments]
        replies = {}
        top_level = []
        for comment in comment_list:
            if comment.id in reply_comment_ids:
                parent_id = parent_map[comment.id]
                replies.setdefault(parent_id, []).append(comment)
            else:
                top_level.append(comment)

        parts = []
        for comment in top_level:
            parts.append(str(comment.user) + ": " + (comment.comment or ""))
            for reply in replies.get(comment.id, []):
                parts.append("  > " + str(reply.user) + ": " + (reply.comment or ""))

        return "||".join(parts)

    if request.method == 'GET':
        if (role == 'editor'):
            if (output_format in ("csv", "tsv")):
                tossups = Tossup.objects.filter(question_set=qset)
                bonuses = Bonus.objects.filter(question_set=qset)

                if output_format == "tsv":
                    response = HttpResponse(content_type='text/tab-separated-values')
                    response['Content-Disposition'] = 'attachment; filename="packet2.tsv"'
                    csv_writer = unicodecsv.writer(response, encoding='utf-8', delimiter='\t')
                else:
                    response = HttpResponse(content_type='text/csv')
                    response['Content-Disposition'] = 'attachment; filename="packet2.csv"'
                    csv_writer = unicodecsv.writer(response, encoding='utf-8', quoting=csv.QUOTE_ALL)

                csv_writer.writerow(["Tossup Question", "Answer", "Category", "Author", "Edited", "Packet", "Question Number", "Comments","Id", "Editor", "Proofreader", "Read Carefully", "Tags"])
                for tossup in tossups:
                    comment_string = build_comment_string(tossup_content_type_id, tossup.id)

                    editor_name = ""
                    if tossup.edited and tossup.editor is not None:
                        editor_name = safe_name(tossup.editor)

                    proofreader_name = ""
                    if tossup.proofread and tossup.proofreader is not None:
                        proofreader_name = safe_name(tossup.proofreader)

                    csv_writer.writerow([safe_text(tossup.tossup_text), safe_text(tossup.tossup_answer), safe_category(tossup.category), safe_name(tossup.author), tossup.edited, safe_packet(tossup.packet), tossup.question_number, safe_text(comment_string), tossup.id, editor_name, proofreader_name, tossup.read_carefully, safe_text(build_tag_string(tossup))])

                csv_writer.writerow([])

                csv_writer.writerow(["Bonus Leadin", "Bonus Part 1", "Bonus Answer 1", "Part 1 Difficulty", "Bonus Part 2", "Bonus Answer 2", "Part 2 Difficulty", "Bonus Part 3", "Bonus Answer 3", "Part 3 Difficulty", "Category", "Author", "Edited", "Packet", "Question Number", "Comments", "Id", "Editor", "Proofreader", "Read Carefully", "Tags"])
                for bonus in bonuses:
                    comment_string = build_comment_string(bonus_content_type_id, bonus.id)

                    editor_name = ""
                    if bonus.edited and bonus.editor is not None:
                        editor_name = safe_name(bonus.editor)

                    proofreader_name = ""
                    if bonus.proofread and bonus.proofreader is not None:
                        proofreader_name = safe_name(bonus.proofreader)

                    csv_writer.writerow([safe_text(bonus.leadin), safe_text(bonus.part1_text), safe_text(bonus.part1_answer), bonus.part1_difficulty, safe_text(bonus.part2_text), safe_text(bonus.part2_answer), bonus.part2_difficulty, safe_text(bonus.part3_text), safe_text(bonus.part3_answer), bonus.part3_difficulty, safe_category(bonus.category), safe_name(bonus.author), bonus.edited, safe_packet(bonus.packet), bonus.question_number, safe_text(comment_string), bonus.id, editor_name, proofreader_name, bonus.read_carefully, safe_text(build_tag_string(bonus))])

                csv_writer.writerow([])
                entries = qset.setwidedistributionentry_set.all()
                csv_writer.writerow(["Category", "Subcategory", "Total Tossups", "Total Bonuses"])
                for entry in entries:
                    csv_writer.writerow([entry.dist_entry.category, entry.dist_entry.subcategory, entry.num_tossups, entry.num_bonuses])

                return response
            elif output_format in ("docx", "docx-by-category", "docx-packetized"):
                tu_comment_ct = ContentType.objects.get_for_model(Tossup)
                bs_comment_ct = ContentType.objects.get_for_model(Bonus)

                # Export options come from the packetized-Word options form. A
                # legacy plain link (no `opts` marker) keeps the old defaults:
                # comments/writers/editors/ids on, credits off.
                _opts_explicit = request.GET.get('opts') == '1'

                def _export_opt(name, default):
                    if _opts_explicit:
                        return request.GET.get(name) == '1'
                    return default

                include_comments = _export_opt('comments', True)
                include_writers = _export_opt('writers', True)
                include_editors = _export_opt('editors', True)
                include_ids = _export_opt('ids', True)
                # A set whose owner has written credits means them to be printed;
                # the options form can still take them out of one export. A set that
                # has written none keeps the old default, where the generated
                # writers/editors block is opt-in.
                include_credits = _export_opt(
                    'credits', bool((qset.packet_credits or qset.first_packet_credits).strip()))
                smart_quotes = _export_opt('smartq', False)
                # Category tag names beside each question. The set says whether
                # they belong in a packet at all; the options form can drop them
                # from one export without changing the set.
                include_tags = _export_opt('ctags', qset.export_category_tags)
                export_tag_names = (_tag_names_by_question(qset, for_output=True)
                                    if include_tags else {})
                # Not an export option: whether an unquoted parenthetical is a
                # pronunciation guide is a property of the set, so the document
                # has to agree with what the edit pages show.
                quoted_guides = qset.guides_require_quotes
                # Interlaced: tossup 1, bonus 1, tossup 2, bonus 2 … the reading
                # order at a tournament, instead of all tossups then all bonuses.
                interlace = _export_opt('interlace', False)

                def question_meta(q):
                    """Attribution line in the standard QEMS packet format:
                    ``<Author, Category - Subcategory> [Tags] ~Id~ <Editor: Name>``.
                    Writer name, category tags, question id and editor name are
                    each optional."""
                    # get_real_name() pads with spaces and is blank when a writer
                    # has no name, so strip before deciding what to include.
                    # author_real_name() is the freeform credit when there is
                    # one, and the account's name otherwise.
                    author = html.unescape(q.author_real_name()).strip() if include_writers else ''
                    cat = html.unescape(safe_category(q.category)).strip()
                    if author and cat:
                        head = '<{0}, {1}>'.format(author, cat)
                    elif author:
                        head = '<{0}>'.format(author)
                    elif cat:
                        head = '<{0}>'.format(cat)
                    else:
                        head = ''
                    parts = [head]
                    tags = export_tag_names.get(
                        ('tossup' if isinstance(q, Tossup) else 'bonus', q.id), [])
                    if tags:
                        parts.append('[{0}]'.format(', '.join(tags)))
                    if include_ids:
                        parts.append('~{0}~'.format(q.id))
                    if include_editors and q.edited:
                        editor = html.unescape(safe_name(q.editor)).strip() if q.editor else ''
                        if editor:
                            parts.append('<Editor: {0}>'.format(editor))
                    return ' '.join(p for p in parts if p).strip()

                def _set_contributors():
                    """(writer_names, editor_names): everyone who wrote at least one
                    question, and every editor of at least one edited question, on
                    the whole set. Names are de-duplicated and sorted."""
                    writers, editors = {}, {}
                    all_qs = (list(Tossup.objects.filter(question_set=qset)
                                   .select_related('author', 'editor')) +
                              list(Bonus.objects.filter(question_set=qset)
                                   .select_related('author', 'editor')))
                    for q in all_qs:
                        if q.author_id:
                            writers[q.author_id] = html.unescape(safe_name(q.author)).strip()
                        if q.edited and q.editor_id:
                            editors[q.editor_id] = html.unescape(safe_name(q.editor)).strip()
                    return (sorted(n for n in writers.values() if n),
                            sorted(n for n in editors.values() if n))

                def add_front_matter_to_doc(document, packet=None, first=False):
                    """Whatever goes above a packet's first tossup: the set's
                    credits, then the packet's own note.

                    The credits are the owner's text when they have written any --
                    the first packet's version on the first packet -- and the
                    generated writers/editors block otherwise."""
                    if include_credits:
                        written = qset.credits_for_packet(packet, first=first)
                        if written:
                            add_text_block_to_doc(document, written)
                        elif first:
                            # The generated block says who wrote and edited the
                            # set, which is the same on every packet, so it goes
                            # once as it always has. Credits a set writes for
                            # itself say what that set wants said, packet by
                            # packet, and are printed wherever they are set.
                            add_credits_to_doc(document)
                    note = (packet.header_note or '').strip() if packet is not None else ''
                    if note:
                        add_text_block_to_doc(document, note)

                def add_text_block_to_doc(document, text):
                    """Plain text above the questions, one paragraph per line, kept
                    on the page with what follows it."""
                    for line in text.splitlines():
                        para = document.add_paragraph()
                        para.paragraph_format.space_after = Pt(2)
                        para.paragraph_format.keep_with_next = True
                        para.add_run(line)
                    document.add_paragraph().paragraph_format.space_after = Pt(8)

                def add_credits_to_doc(document):
                    """The generated credits: who wrote and who edited the questions,
                    for a set whose owner has not written credits of their own."""
                    writer_names, editor_names = _set_contributors()
                    if not writer_names and not editor_names:
                        return
                    document.add_heading('Credits', level=2)
                    if writer_names:
                        p = document.add_paragraph()
                        p.paragraph_format.space_after = Pt(2)
                        p.add_run('Writers: ').bold = True
                        p.add_run(', '.join(writer_names))
                    if editor_names:
                        p = document.add_paragraph()
                        p.paragraph_format.space_after = Pt(2)
                        p.add_run('Editors: ').bold = True
                        p.add_run(', '.join(editor_names))
                    document.add_paragraph().paragraph_format.space_after = Pt(8)

                def _initials(name):
                    parts = (name or '').split()
                    return ''.join(p[0] for p in parts[:2]).upper() or 'QC'

                def open_comment_threads(question, ct):
                    """Open (non-removed, unresolved) comments on a question, as
                    (author, text) pairs with any replies folded into the text.
                    Resolved threads and replies under them are skipped."""
                    comments = list(Comment.objects.filter(
                        content_type=ct, object_pk=str(question.id), is_removed=False
                    ).select_related('user').order_by('submit_date'))
                    if not comments:
                        return []
                    ids = [c.id for c in comments]
                    resolved = set(CommentResolution.objects.filter(
                        comment_id__in=ids, resolved=True).values_list('comment_id', flat=True))
                    parent_of = dict(CommentReply.objects.filter(
                        comment_id__in=ids).values_list('comment_id', 'parent_id'))

                    def author_of(c):
                        if c.user_id and c.user:
                            return c.user.get_username()
                        return c.user_name or 'Anonymous'

                    replies = defaultdict(list)
                    tops = []
                    for c in comments:
                        pid = parent_of.get(c.id)
                        (replies[pid].append(c) if pid is not None else tops.append(c))

                    out = []
                    for c in tops:
                        if c.id in resolved:
                            continue
                        text = strip_markup(c.comment or '').strip()
                        for r in replies.get(c.id, []):
                            text += '\n↳ {0}: {1}'.format(
                                author_of(r), strip_markup(r.comment or '').strip())
                        out.append((author_of(c), text))
                    return out

                def attach_open_comments(document, paragraph, question, ct):
                    """Add each open comment on the question as a Word comment
                    anchored to the question's paragraph."""
                    if not paragraph.runs:
                        return
                    for author, text in open_comment_threads(question, ct):
                        if not text:
                            continue
                        document.add_comment(paragraph.runs, text=text,
                                             author=author or 'QEMS', initials=_initials(author))

                def new_docx():
                    # Match the reference PACE packets: Times New Roman 12pt body,
                    # Word "Narrow" (0.5") margins, black bold centered headings.
                    document = Document()
                    for section in document.sections:
                        section.top_margin = Inches(0.5)
                        section.bottom_margin = Inches(0.5)
                        section.left_margin = Inches(0.5)
                        section.right_margin = Inches(0.5)

                    normal = document.styles['Normal']
                    normal.font.size = Pt(12)
                    normal.font.name = 'Times New Roman'
                    normal.paragraph_format.space_before = Pt(0)
                    normal.paragraph_format.space_after = Pt(0)
                    # Single-spaced: python-docx's default template ships 1.15
                    # line spacing, which stretched a packet over extra pages.
                    # (The gap *between* questions is set per-paragraph below.)
                    normal.paragraph_format.line_spacing = 1.0

                    h1 = document.styles['Heading 1']  # packet/round title
                    h1.font.name = 'Times New Roman'
                    h1.font.size = Pt(16)
                    h1.font.bold = True
                    h1.font.color.rgb = RGBColor(0, 0, 0)
                    h1.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    h1.paragraph_format.space_before = Pt(0)
                    h1.paragraph_format.space_after = Pt(10)

                    h2 = document.styles['Heading 2']  # "Tossups" / "Bonuses"
                    h2.font.name = 'Times New Roman'
                    h2.font.size = Pt(13)
                    h2.font.bold = True
                    h2.font.color.rgb = RGBColor(0, 0, 0)
                    h2.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    h2.paragraph_format.space_before = Pt(12)
                    h2.paragraph_format.space_after = Pt(6)
                    return document

                def _line_break(paragraph):
                    paragraph.add_run().add_break()

                def add_tossup_to_doc(document, tossup, num):
                    # One paragraph per tossup (stem / answer / attribution on
                    # their own lines) so Word keeps the whole question together.
                    p = document.add_paragraph()
                    p.paragraph_format.keep_together = True
                    p.paragraph_format.space_after = Pt(10)
                    p.add_run(f"{num}. ").bold = True
                    add_qems_formatted_runs(p, safe_text(tossup.tossup_text), smart_quotes=smart_quotes,
                                            all_power=tossup.is_all_power(),
                                            allow_superpower=tossup.superpower_enabled(),
                                            quoted_guides=quoted_guides)
                    _line_break(p)
                    # Not bolded: the label is scaffolding, and bolding it drew the eye
                    # away from the underlined required answer next to it.
                    p.add_run("ANSWER: ")
                    add_qems_formatted_runs(p, safe_text(tossup.tossup_answer), is_answer=True,
                                            smart_quotes=smart_quotes, quoted_guides=quoted_guides)
                    meta = question_meta(tossup)
                    if meta:
                        _line_break(p)
                        p.add_run(meta)
                    if include_comments:
                        attach_open_comments(document, p, tossup, tu_comment_ct)

                def add_bonus_to_doc(document, bonus, num):
                    # One paragraph per bonus (leadin, each part + answer, then
                    # attribution) so the whole bonus stays together.
                    p = document.add_paragraph()
                    p.paragraph_format.keep_together = True
                    p.paragraph_format.space_after = Pt(10)
                    p.add_run(f"{num}. ").bold = True
                    add_qems_formatted_runs(p, safe_text(bonus.leadin), smart_quotes=smart_quotes,
                                            quoted_guides=quoted_guides)
                    for part_num in range(1, 4):
                        part_text = getattr(bonus, f'part{part_num}_text', None)
                        part_answer = getattr(bonus, f'part{part_num}_answer', None)
                        part_diff = getattr(bonus, f'part{part_num}_difficulty', '')
                        if part_text:
                            diff_tag = part_diff if part_diff else ''
                            _line_break(p)
                            p.add_run(f"[10{diff_tag}] ").bold = True
                            add_qems_formatted_runs(p, safe_text(part_text), smart_quotes=smart_quotes,
                                                    quoted_guides=quoted_guides)
                            _line_break(p)
                            p.add_run("ANSWER: ")
                            add_qems_formatted_runs(p, safe_text(part_answer), is_answer=True,
                                                    smart_quotes=smart_quotes,
                                                    quoted_guides=quoted_guides)
                    meta = question_meta(bonus)
                    if meta:
                        _line_break(p)
                        p.add_run(meta)
                    if include_comments:
                        attach_open_comments(document, p, bonus, bs_comment_ct)

                def add_careful_notes_to_doc(document, tossup_qs, bonus_qs):
                    """At the top of a packet, list the answer lines flagged
                    "read answer carefully" so the moderator is warned."""
                    flagged = []
                    for t in tossup_qs:
                        if getattr(t, 'read_carefully', False):
                            flagged.append(('Tossup', t.question_number, t.tossup_answer))
                    for b in bonus_qs:
                        if getattr(b, 'read_carefully', False):
                            ans = ' / '.join(filter(None, [b.part1_answer, b.part2_answer, b.part3_answer]))
                            flagged.append(('Bonus', b.question_number, ans))
                    if not flagged:
                        return
                    head = document.add_paragraph()
                    head.paragraph_format.space_after = Pt(2)
                    head.add_run('Moderator — read these answer lines carefully:').bold = True
                    for label, num, answer in flagged:
                        line = document.add_paragraph()
                        line.paragraph_format.space_after = Pt(0)
                        line.add_run('{0} {1}: '.format(label, num or '?')).bold = True
                        add_qems_formatted_runs(line, safe_text(answer), is_answer=True,
                                                smart_quotes=smart_quotes,
                                                quoted_guides=quoted_guides)
                    document.add_paragraph().paragraph_format.space_after = Pt(8)

                def save_docx_bytes(document):
                    buf = io.BytesIO()
                    document.save(buf)
                    return buf.getvalue()

                def write_all_questions_to_doc(document, tossup_qs, bonus_qs):
                    """Write a packet's questions to a document: tossups then
                    bonuses, or interlaced (tossup 1, bonus 1, tossup 2, …) when
                    the export asked for it. Accepts a queryset or a list."""
                    if interlace:
                        write_interlaced_to_doc(document, tossup_qs, bonus_qs)
                        return
                    # Only label the sections when there are two of them: a
                    # lone "Tossups" heading implies bonuses somewhere else.
                    label = bool(tossup_qs) and bool(bonus_qs)
                    if tossup_qs:
                        if label:
                            document.add_heading('Tossups', level=2)
                        for i, tossup in enumerate(tossup_qs, 1):
                            num = tossup.question_number if tossup.question_number else i
                            add_tossup_to_doc(document, tossup, num)
                    if bonus_qs:
                        if label:
                            document.add_heading('Bonuses', level=2)
                        for i, bonus in enumerate(bonus_qs, 1):
                            num = bonus.question_number if bonus.question_number else i
                            add_bonus_to_doc(document, bonus, num)

                def write_interlaced_to_doc(document, tossup_qs, bonus_qs):
                    """Tossup 1, bonus 1, tossup 2, bonus 2 … — the order they're
                    read at a tournament. Pairs by position, and once one kind
                    runs out the rest of the other simply follow."""
                    tossups = list(tossup_qs)
                    bonuses = list(bonus_qs)
                    if not tossups and not bonuses:
                        return
                    document.add_heading('Questions', level=2)
                    for i in range(max(len(tossups), len(bonuses))):
                        if i < len(tossups):
                            tossup = tossups[i]
                            add_tossup_to_doc(document, tossup,
                                              tossup.question_number or i + 1)
                        if i < len(bonuses):
                            bonus = bonuses[i]
                            add_bonus_to_doc(document, bonus,
                                             bonus.question_number or i + 1)

                if output_format == "docx":
                    document = new_docx()

                    packets = Packet.objects.filter(question_set=qset).order_by('packet_name')
                    for pkt_i, packet in enumerate(packets):
                        if pkt_i > 0:
                            document.add_page_break()
                        document.add_heading('{0} {1}'.format(qset.name, packet.packet_name), level=1)
                        tossups = list(Tossup.objects.filter(
                            packet=packet, question_set=qset
                        ).order_by('question_number'))
                        bonuses = list(Bonus.objects.filter(
                            packet=packet, question_set=qset
                        ).order_by('question_number'))
                        add_front_matter_to_doc(document, packet, first=(pkt_i == 0))
                        add_careful_notes_to_doc(document, tossups, bonuses)
                        write_all_questions_to_doc(document, tossups, bonuses)

                    # Unpacketed questions
                    unpacketed_tossups = Tossup.objects.filter(
                        packet__isnull=True, question_set=qset
                    ).order_by('question_number')
                    unpacketed_bonuses = Bonus.objects.filter(
                        packet__isnull=True, question_set=qset
                    ).order_by('question_number')
                    if unpacketed_tossups.exists() or unpacketed_bonuses.exists():
                        document.add_heading('Unpacketed Questions', level=1)
                        write_all_questions_to_doc(document, unpacketed_tossups, unpacketed_bonuses)

                    response = HttpResponse(
                        save_docx_bytes(document),
                        content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                    )
                    filename = f"{qset.name}.docx" if qset.name else "questions.docx"
                    response['Content-Disposition'] = f'attachment; filename="{filename}"'
                    return response

                elif output_format == "docx-by-category":
                    # Collect top-level categories
                    all_tossups = Tossup.objects.filter(question_set=qset).select_related('category', 'author', 'editor', 'packet')
                    all_bonuses = Bonus.objects.filter(question_set=qset).select_related('category', 'author', 'editor', 'packet')

                    categories = set()
                    has_uncategorized = False
                    for t in all_tossups:
                        if t.category:
                            categories.add(t.category.category)
                        else:
                            has_uncategorized = True
                    for b in all_bonuses:
                        if b.category:
                            categories.add(b.category.category)
                        else:
                            has_uncategorized = True

                    zip_buf = io.BytesIO()
                    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                        for cat_name in sorted(categories):
                            document = new_docx()
                            document.add_heading(cat_name, level=1)

                            cat_tossups = all_tossups.filter(
                                category__category=cat_name
                            ).order_by('packet__packet_name', 'question_number')
                            cat_bonuses = all_bonuses.filter(
                                category__category=cat_name
                            ).order_by('packet__packet_name', 'question_number')
                            write_all_questions_to_doc(document, cat_tossups, cat_bonuses)

                            zf.writestr(f"{cat_name}.docx", save_docx_bytes(document))

                        if has_uncategorized:
                            document = new_docx()
                            document.add_heading('Uncategorized', level=1)
                            uncat_tossups = all_tossups.filter(
                                category__isnull=True
                            ).order_by('packet__packet_name', 'question_number')
                            uncat_bonuses = all_bonuses.filter(
                                category__isnull=True
                            ).order_by('packet__packet_name', 'question_number')
                            write_all_questions_to_doc(document, uncat_tossups, uncat_bonuses)
                            zf.writestr("Uncategorized.docx", save_docx_bytes(document))

                    response = HttpResponse(
                        zip_buf.getvalue(),
                        content_type='application/zip'
                    )
                    filename = f"{qset.name} - By Category.zip" if qset.name else "questions-by-category.zip"
                    response['Content-Disposition'] = f'attachment; filename="{filename}"'
                    return response

                else:  # output_format == "docx-packetized"
                    # Export the set's actual packets in their packetized order
                    # (one .docx per packet, questions in question_number order),
                    # not a fresh re-deal. Packets are natural-sorted by name so
                    # "Packet 2" precedes "Packet 10".
                    def _packet_sort_key(pk):
                        nums = re.findall(r'\d+', pk.packet_name or '')
                        return (int(nums[0]) if nums else float('inf'),
                                pk.packet_name or '', pk.id)

                    packets = sorted(Packet.objects.filter(question_set=qset),
                                     key=_packet_sort_key)

                    packet_tus, packet_bos = [], []
                    for packet in packets:
                        packet_tus.append(list(
                            Tossup.objects.filter(packet=packet, question_set=qset)
                            .select_related('category', 'author', 'editor')
                            .order_by('question_number')))
                        packet_bos.append(list(
                            Bonus.objects.filter(packet=packet, question_set=qset)
                            .select_related('category', 'author', 'editor')
                            .order_by('question_number')))

                    # Build answer matrix workbook from the actual packets
                    wb = Workbook()

                    # Tossup answers sheet
                    ws_tu = wb.active
                    ws_tu.title = "Tossup Answers"
                    header_font = Font(bold=True)
                    wrap = Alignment(wrap_text=True, vertical='top')
                    max_tu = max((len(p) for p in packet_tus), default=0)
                    # Header row
                    ws_tu.cell(row=1, column=1, value="Packet").font = header_font
                    for col in range(1, max_tu + 1):
                        ws_tu.cell(row=1, column=col + 1, value=col).font = header_font
                    for pkt_idx, (packet, tus) in enumerate(zip(packets, packet_tus)):
                        row = pkt_idx + 2
                        ws_tu.cell(row=row, column=1, value=packet.packet_name).font = header_font
                        for q_idx, tossup in enumerate(tus):
                            answer = get_answer_no_formatting(
                                get_primary_answer(tossup.tossup_answer)
                            ).strip()
                            cell = ws_tu.cell(row=row, column=q_idx + 2, value=answer)
                            cell.alignment = wrap
                    # Auto-width columns
                    for col_idx in range(1, max_tu + 2):
                        ws_tu.column_dimensions[ws_tu.cell(row=1, column=col_idx).column_letter].width = 18

                    # Bonus answers sheet
                    ws_bo = wb.create_sheet("Bonus Answers")
                    max_bo = max((len(p) for p in packet_bos), default=0)
                    ws_bo.cell(row=1, column=1, value="Packet").font = header_font
                    for col in range(1, max_bo + 1):
                        ws_bo.cell(row=1, column=col + 1, value=col).font = header_font
                    for pkt_idx, (packet, bos) in enumerate(zip(packets, packet_bos)):
                        row = pkt_idx + 2
                        ws_bo.cell(row=row, column=1, value=packet.packet_name).font = header_font
                        for q_idx, bonus in enumerate(bos):
                            answers = []
                            for part_num in range(1, 4):
                                ans = getattr(bonus, f'part{part_num}_answer', None)
                                if ans:
                                    answers.append(get_answer_no_formatting(
                                        get_primary_answer(ans)
                                    ).strip())
                            cell = ws_bo.cell(row=row, column=q_idx + 2,
                                              value=" / ".join(answers))
                            cell.alignment = wrap
                    for col_idx in range(1, max_bo + 2):
                        ws_bo.column_dimensions[ws_bo.cell(row=1, column=col_idx).column_letter].width = 24

                    # Unpacketed questions (if any) go into a trailing document,
                    # kept out of the per-packet answer matrix.
                    unpacketed_tus = list(
                        Tossup.objects.filter(packet__isnull=True, question_set=qset)
                        .select_related('category', 'author', 'editor')
                        .order_by('question_number'))
                    unpacketed_bos = list(
                        Bonus.objects.filter(packet__isnull=True, question_set=qset)
                        .select_related('category', 'author', 'editor')
                        .order_by('question_number'))

                    def _safe_filename(name):
                        return re.sub(r'[\\/:*?"<>|]', '_', name or 'Packet').strip() or 'Packet'

                    # Package everything into a zip
                    zip_buf = io.BytesIO()
                    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                        used_names = set()
                        for pkt_i, (packet, tus, bos) in enumerate(zip(packets, packet_tus, packet_bos)):
                            document = new_docx()
                            document.add_heading(
                                '{0} {1}'.format(qset.name, packet.packet_name), level=1)
                            # Each packet travels as its own file, so each one carries
                            # the credits -- the first packet's version on the first.
                            add_front_matter_to_doc(document, packet, first=(pkt_i == 0))
                            add_careful_notes_to_doc(document, tus, bos)
                            write_all_questions_to_doc(document, tus, bos)
                            base = _safe_filename(packet.packet_name)
                            fname = base
                            n = 2
                            while fname in used_names:
                                fname = '{0} ({1})'.format(base, n)
                                n += 1
                            used_names.add(fname)
                            zf.writestr(f"{fname}.docx", save_docx_bytes(document))

                        if unpacketed_tus or unpacketed_bos:
                            document = new_docx()
                            document.add_heading(
                                '{0} Unpacketed'.format(qset.name), level=1)
                            write_all_questions_to_doc(document, unpacketed_tus, unpacketed_bos)
                            zf.writestr("Unpacketed.docx", save_docx_bytes(document))

                        # Add answer matrix
                        xlsx_buf = io.BytesIO()
                        wb.save(xlsx_buf)
                        zf.writestr("Answer Matrix.xlsx", xlsx_buf.getvalue())

                    response = HttpResponse(
                        zip_buf.getvalue(),
                        content_type='application/zip'
                    )
                    filename = f"{qset.name} - Packets.zip" if qset.name else "packets.zip"
                    response['Content-Disposition'] = f'attachment; filename="{filename}"'
                    return response
            elif output_format in ("yapp-json", "yapp2-json"):
                # Export each packet as a YAPP (YetAnotherPacketParser) JSON file,
                # readable by MODAQ. One .json per packet, zipped for the set.
                # "yapp2-json" additionally carries pronunciation-guide anchoring
                # (see yapp_export and YAPP2_FORMAT.md); the questions themselves
                # are identical, so a plain-YAPP reader is unaffected.
                from . import yapp_export
                yapp_version = 2 if output_format == "yapp2-json" else 1
                # Interlacing is a YAPP2 `readingOrder`; plain YAPP has no way to
                # say it, so the option is quietly ignored there.
                yapp_interlace = (request.GET.get('opts') == '1'
                                  and request.GET.get('interlace') == '1')
                # Tag names ride along in each question's metadata string, which
                # is what a YAPP reader shows after the answer.
                yapp_show_tags = (request.GET.get('ctags') == '1'
                                  if request.GET.get('opts') == '1'
                                  else qset.export_category_tags)
                yapp_tag_names = (_tag_names_by_question(qset, for_output=True)
                                  if yapp_show_tags else {})

                def _packet_sort_key(pk):
                    nums = re.findall(r'\d+', pk.packet_name or '')
                    return (int(nums[0]) if nums else float('inf'),
                            pk.packet_name or '', pk.id)

                def _safe_filename(name):
                    return re.sub(r'[\\/:*?"<>|]', '_', name or 'Packet').strip() or 'Packet'

                # (name, tossups, bonuses) groups: one per packet, plus a trailing
                # group for any questions that aren't in a packet.
                groups = []
                for packet in sorted(Packet.objects.filter(question_set=qset),
                                     key=_packet_sort_key):
                    groups.append((
                        packet.packet_name,
                        list(Tossup.objects.filter(packet=packet, question_set=qset)
                             .select_related('category', 'author').order_by('question_number')),
                        list(Bonus.objects.filter(packet=packet, question_set=qset)
                             .select_related('category', 'author').order_by('question_number'))))
                unpacketed_tus = list(
                    Tossup.objects.filter(packet__isnull=True, question_set=qset)
                    .select_related('category', 'author').order_by('question_number'))
                unpacketed_bos = list(
                    Bonus.objects.filter(packet__isnull=True, question_set=qset)
                    .select_related('category', 'author').order_by('question_number'))
                if unpacketed_tus or unpacketed_bos:
                    groups.append(('Unpacketed', unpacketed_tus, unpacketed_bos))

                zip_buf = io.BytesIO()
                with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                    used_names = set()
                    for name, tus, bos in groups:
                        payload = yapp_export.packet_to_yapp(
                            tus, bos, version=yapp_version,
                            name=name if yapp_version >= 2 else None,
                            interlace=yapp_interlace, tag_names=yapp_tag_names)
                        base = _safe_filename(name)
                        fname = base
                        n = 2
                        while fname in used_names:
                            fname = '{0} ({1})'.format(base, n)
                            n += 1
                        used_names.add(fname)
                        zf.writestr('{0}.json'.format(fname),
                                    json.dumps(payload, ensure_ascii=False, indent=2))

                response = HttpResponse(zip_buf.getvalue(), content_type='application/zip')
                label = 'YAPP2 JSON' if yapp_version >= 2 else 'YAPP JSON'
                filename = (f"{qset.name} - {label}.zip" if qset.name
                            else f"packets-{output_format}.zip")
                response['Content-Disposition'] = f'attachment; filename="{filename}"'
                return response
            elif output_format == "pdf":
                # Packetized, moderator-ready PDF (one page per packet).
                from . import pdf_export

                _explicit = request.GET.get('opts') == '1'

                def _o(name, default):
                    return (request.GET.get(name) == '1') if _explicit else default

                # As in the Word export: credits a set has actually written are on
                # by default, generated ones stay opt-in.
                _has_written_credits = bool(
                    (qset.packet_credits or qset.first_packet_credits).strip())
                pdf_opts = {'writers': _o('writers', True), 'editors': _o('editors', True),
                            'ids': _o('ids', True), 'credits': _o('credits', _has_written_credits),
                            'interlace': _o('interlace', False)}
                pdf_opts['tag_names'] = (
                    _tag_names_by_question(qset, for_output=True)
                    if _o('ctags', qset.export_category_tags) else {})

                def _packet_sort_key(pk):
                    nums = re.findall(r'\d+', pk.packet_name or '')
                    return (int(nums[0]) if nums else float('inf'),
                            pk.packet_name or '', pk.id)

                groups = []
                packets_in_order = sorted(Packet.objects.filter(question_set=qset),
                                          key=_packet_sort_key)
                for packet in packets_in_order:
                    groups.append((
                        packet.packet_name,
                        list(Tossup.objects.filter(packet=packet, question_set=qset)
                             .select_related('category', 'author', 'editor').order_by('question_number')),
                        list(Bonus.objects.filter(packet=packet, question_set=qset)
                             .select_related('category', 'author', 'editor').order_by('question_number'))))
                unp_tu = list(Tossup.objects.filter(packet__isnull=True, question_set=qset)
                              .select_related('category', 'author', 'editor').order_by('question_number'))
                unp_bo = list(Bonus.objects.filter(packet__isnull=True, question_set=qset)
                              .select_related('category', 'author', 'editor').order_by('question_number'))
                if unp_tu or unp_bo:
                    groups.append(('Unpacketed', unp_tu, unp_bo))
                    packets_in_order.append(None)

                credits = None
                if pdf_opts['credits']:
                    writers, editors = {}, {}
                    for q in (list(Tossup.objects.filter(question_set=qset).select_related('author', 'editor')) +
                              list(Bonus.objects.filter(question_set=qset).select_related('author', 'editor'))):
                        if q.author_id:
                            writers[q.author_id] = html.unescape(safe_name(q.author)).strip()
                        if q.edited and q.editor_id:
                            editors[q.editor_id] = html.unescape(safe_name(q.editor)).strip()
                    credits = (sorted(n for n in writers.values() if n),
                               sorted(n for n in editors.values() if n))

                # One PDF per packet, zipped — the same shape as the Word export.
                # A packet is what gets handed to a room, so it has to be its own
                # file; credits go in every packet rather than only the first,
                # since each file now travels on its own.
                def _safe_filename(name):
                    return re.sub(r'[\\/:*?"<>|]', '_', name or 'Packet').strip() or 'Packet'

                zip_buf = io.BytesIO()
                with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                    used_names = set()
                    for gi, group in enumerate(groups):
                        packet = packets_in_order[gi] if gi < len(packets_in_order) else None
                        # The owner's own credits, if they wrote any, in place of the
                        # generated ones; then this packet's note.
                        written = (qset.credits_for_packet(packet, first=(gi == 0))
                                   if pdf_opts['credits'] else '')
                        note = (packet.header_note or '').strip() if packet is not None else ''
                        pdf_bytes = pdf_export.build_packetized_pdf(
                            qset.name, [group], pdf_opts, credits=credits,
                            front_matter=[t for t in (written, note) if t])
                        base = _safe_filename(group[0])
                        fname = base
                        n = 2
                        while fname in used_names:
                            fname = '{0} ({1})'.format(base, n)
                            n += 1
                        used_names.add(fname)
                        zf.writestr('{0}.pdf'.format(fname), pdf_bytes)

                response = HttpResponse(zip_buf.getvalue(), content_type='application/zip')
                filename = f"{qset.name} - Packets (PDF).zip" if qset.name else "packets-pdf.zip"
                response['Content-Disposition'] = f'attachment; filename="{filename}"'
                return response
            else:
                message = 'Unsupported export format.'
                message_class = 'alert-box alert'
                q_set = []
                tossups = []
                bonuses = []
                return render(request, 'export_question_set.html',
                                    {'user': user,
                                     'q_set': q_set,
                                     'tossups': tossups,
                                     'bonuses': bonuses,
                                     'message': message,
                                     'message_class': message_class})

        else:
            message = 'You are not authorized to export questions from this set.'
            message_class = 'alert-box alert'
            q_set = []
            tossups = []
            bonuses = []
            return render(request, 'export_question_set.html',
                                {'user': user,
                                 'q_set': q_set,
                                 'tossups': tossups,
                                 'bonuses': bonuses,
                                 'message': message,
                                 'message_class': message_class})

@login_required
def restore_tossup(request):
    user = request.user.writer

    message = ''
    message_class = ''
    read_only = True

    if request.method == 'POST':
        th_id = request.POST['th_id']
        tossup_history = TossupHistory.objects.filter(id=th_id).first()
        tossup = (Tossup.objects.filter(
            question_history=tossup_history.question_history).first()
            if tossup_history is not None else None)
        if (tossup_history is None or tossup is None):
            message = 'Invalid tossup history restoration!'
            message_class = 'alert-box warning'
        else:
            if _may_edit_question(user, tossup):
                tossup = Tossup.objects.get(question_history=tossup_history.question_history)
                if (tossup is None):
                    message = 'Invalid tossup restoration!'
                    message_class = 'alert-box warning'
                else:
                    tossup.tossup_answer = tossup_history.tossup_answer
                    tossup.tossup_text = tossup_history.tossup_text
                    tossup.save_question(edit_type=QUESTION_RESTORE, changer=user)
                    cache.clear()
                    message = 'Successfully restored question'
                    message_class = 'alert-box success'
            else:
                message = 'You are not authorized to restore this question!'
                message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

@login_required
def restore_bonus(request):
    user = request.user.writer

    message = ''
    message_class = ''
    read_only = True

    if request.method == 'POST':
        bh_id = request.POST['bh_id']
        bonus_history = BonusHistory.objects.filter(id=bh_id).first()
        bonus = (Bonus.objects.filter(
            question_history=bonus_history.question_history).first()
            if bonus_history is not None else None)
        if (bonus_history is None or bonus is None):
            message = 'Invalid bonus history restoration!'
            message_class = 'alert-box warning'
        else:
            if _may_edit_question(user, bonus):
                bonus = Bonus.objects.get(question_history=bonus_history.question_history)
                if (bonus is None):
                    message = 'Invalid bonus restoration!'
                    message_class = 'alert-box warning'
                else:
                    bonus.question_type = bonus_history.question_type
                    bonus.leadin = bonus_history.leadin
                    bonus.part1_text = bonus_history.part1_text
                    bonus.part1_answer = bonus_history.part1_answer
                    bonus.part2_text = bonus_history.part2_text
                    bonus.part2_answer = bonus_history.part2_answer
                    bonus.part3_text = bonus_history.part3_text
                    bonus.part3_answer = bonus_history.part3_answer
                    bonus.save_question(edit_type=QUESTION_RESTORE, changer=user)
                    cache.clear()
                    message = 'Successfully restored question'
                    message_class = 'alert-box success'
            else:
                message = 'You are not authorized to restore this question!'
                message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

@login_required
def tossup_history(request, tossup_id):
    user = request.user.writer
    if request.method == 'GET':
        tossup = Tossup.objects.get(id=tossup_id)
        if (tossup is None):
            message = 'Invalid tossup'
            message_class = 'alert-box alert'
            tossup = None
        else:
            q_set = tossup.question_set
            if (q_set is None):
                message = 'Invalid question set'
                message_class = 'alert-box alert'
                tossup = None
            else:
                q_set_writers = Writer.objects.filter(Q(question_set_writer=q_set) | Q(question_set_editor=q_set)).distinct()
                if (user in q_set_writers):
                    tossup_histories, bonus_histories = tossup.get_question_history()
                    tossup_histories = tossup_histories.order_by('-id')
                    bonus_histories = bonus_histories.order_by('-id')
                    message = ''
                    message_class = ''

                    return render(request, 'tossup_history.html',
                                        {'user': user,
                                         'qset': q_set,
                                         'tossup': tossup,
                                         'tossup_histories': tossup_histories,
                                         'bonus_histories': bonus_histories,
                                         'highlight_version': request.GET.get('v', ''),
                                         'message': message,
                                         'message_class': message_class})

                else:
                    message = "You don't have permission to view this question"
                    message_class = 'alert-box alert'
                    tossup = None


    return render(request, 'tossup_history.html',
                        {'user': user,
                         'q_set': q_set,
                         'tossup': tossup,
                         'tossup_histories': [],
                         'bonus_histories': [],
                         'message': message,
                         'message_class': message_class})

@login_required
def bonus_history(request, bonus_id):
    user = request.user.writer
    if request.method == 'GET':
        bonus = Bonus.objects.get(id=bonus_id)
        if (bonus is None):
            message = 'Invalid bonus'
            message_class = 'alert-box alert'
            bonus = None
        else:
            q_set = bonus.question_set
            if (q_set is None):
                message = 'Invalid question set'
                message_class = 'alert-box alert'
                bonus = None
            else:
                q_set_writers = Writer.objects.filter(Q(question_set_writer=q_set) | Q(question_set_editor=q_set)).distinct()
                if (user in q_set_writers):
                    message = ''
                    message_class = ''

                    tossup_histories, bonus_histories = bonus.get_question_history()
                    tossup_histories = tossup_histories.order_by('-id')
                    bonus_histories = bonus_histories.order_by('-id')
                    return render(request, 'bonus_history.html',
                                        {'user': user,
                                         'qset': q_set,
                                         'bonus': bonus,
                                         'tossup_histories': tossup_histories,
                                         'bonus_histories': bonus_histories,
                                         'highlight_version': request.GET.get('v', ''),
                                         'message': message,
                                         'message_class': message_class})

                else:
                    message = "You don't have permission to view this question"
                    message_class = 'alert-box alert'
                    bonus = None


    return render(request, 'bonus_history.html',
                        {'user': user,
                         'q_set': q_set,
                         'bonus': bonus,
                         'tossup_histories': [],
                         'bonus_histories': [],
                         'message': message,
                         'message_class': message_class})

@login_required
def convert_tossup(request):
    user = request.user.writer

    message = ''
    message_class = ''
    redirect_url = None

    if request.method == 'POST':
        tossup_id = request.POST['tossup_id']
        tossup = Tossup.objects.filter(id=tossup_id).first()
        if (tossup is None):
            message = 'Invalid tossup!'
            message_class = 'alert-box warning'
        else:
            qset = _question_set_of(tossup)
            if _may_edit_question(user, tossup):
                target_type = request.POST['target_type']
                if (target_type == ACF_STYLE_TOSSUP):
                    tossup_to_tossup(tossup, target_type)
                else:
                    result = tossup_to_bonus(tossup, target_type)
                    if result:
                        redirect_url = '/edit_bonus/{}/'.format(result.id)

                message = 'Successfully changed tossup type'
                message_class = 'alert-box success'
                cache.clear()
            else:
                message = 'You are not authorized to change this tossup type!'
                message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class, 'redirect_url': redirect_url}))

@login_required
def convert_bonus(request):
    user = request.user.writer

    message = ''
    message_class = ''
    redirect_url = None

    if request.method == 'POST':
        bonus_id = request.POST['bonus_id']
        bonus = Bonus.objects.filter(id=bonus_id).first()
        if (bonus is None):
            message = 'Invalid bonus!'
            message_class = 'alert-box warning'
        else:
            qset = _question_set_of(bonus)
            if _may_edit_question(user, bonus):
                target_type = request.POST['target_type']
                if (target_type == ACF_STYLE_BONUS or target_type == VHSL_BONUS):
                    result = bonus_to_bonus(bonus, target_type)
                    if result:
                        redirect_url = '/edit_bonus/{}/'.format(result.id)
                else:
                    result = bonus_to_tossup(bonus, target_type)
                    if result:
                        redirect_url = '/edit_tossup/{}/'.format(result.id)

                message = 'Successfully changed bonus type'
                message_class = 'alert-box success'
                cache.clear()
            else:
                message = 'You are not authorized to change this bonus type!'
                message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class, 'redirect_url': redirect_url}))

@login_required
def questions_remaining(request, qset_id):
    message = ''

    qset = QuestionSet.objects.get(id=qset_id)
    user = request.user.writer
    set_status = {}

    total_tu_req = 0
    total_bs_req = 0
    total_tu_written = 0
    total_bs_written = 0
    tu_needed = 0
    bs_needed = 0

    role = get_role_no_owner(user, qset)

    if role == 'none':
        messages.error(request, 'You are not authorized to view information about this tournament!')
        return HttpResponseRedirect('/failure.html/')

    if request.method == 'GET':
        set_status, total_tu_req, total_bs_req, tu_needed, bs_needed, set_pct_complete = get_questions_remaining(qset)

    return render(request, 'questions_remaining.html',
                             {'user': user,
                              'set_status': set_status,
                              'set_pct_complete': '{0:0.2f}%'.format(set_pct_complete),
                              'set_pct_progress_bar': '{0:0.0f}%'.format(set_pct_complete),
                              'tu_needed': tu_needed,
                              'bs_needed': bs_needed,
                              'qset': qset,
                              'message': message})

@login_required
def category_overview(request, qset_id):
    qset = QuestionSet.objects.get(id=qset_id)
    user = request.user.writer

    role = get_role_no_owner(user, qset)

    if role == 'none':
        messages.error(request, 'You are not authorized to view information about this tournament!')
        return HttpResponseRedirect('/failure.html/')

    set_status, total_tu_req, total_bs_req, tu_needed, bs_needed, set_pct_complete = get_questions_remaining(qset)
    overview_rows = get_category_overview(qset)

    # Attach the editors tagged for each category (by matching a category tag's
    # path to the overview row's full name).
    editors_by_cat = {}
    for t in qset.editor_tags.exclude(category='').select_related('editor__user'):
        name = (t.editor.user.get_full_name() or t.editor.user.username).strip()
        editors_by_cat.setdefault(t.category, [])
        if name not in editors_by_cat[t.category]:
            editors_by_cat[t.category].append(name)
    # The category's tags belong on the page about categories. Grouped by the
    # axis they run along, with how many questions each has against what it
    # asks for, so a row says what is outstanding without a second page.
    tags_by_path = {}
    for tag in (CategoryTag.objects.filter(question_set=qset)
                ):
        entry = tag.progress()
        entry['tag'] = tag
        entry['count_label'] = _tag_count_label(tag, entry)
        tags_by_path.setdefault(tag.category_path, []).append(entry)

    for row in overview_rows:
        row['editors'] = editors_by_cat.get(row['name'], [])
        rows_for_path = tags_by_path.get(row['name'], [])
        by_group, order = {}, []
        for entry in rows_for_path:
            key = (entry['tag'].group_name or '').strip()
            if key not in by_group:
                by_group[key] = []
                order.append(key)
            by_group[key].append(entry)
        order.sort(key=lambda k: (k == '', k.lower()))
        row['tag_groups'] = [{'name': k, 'label': k or 'Ungrouped', 'tags': by_group[k]}
                             for k in order]
        row['tag_count'] = len(rows_for_path)

    can_edit_tags = qset.is_owner(user) or user in qset.editor.all()
    group_choices = sorted(set(
        CategoryTag.objects.filter(question_set=qset)
        .exclude(group_name='').values_list('group_name', flat=True)))

    return render(request, 'category_overview.html',
                             {'user': user,
                              'overview_rows': overview_rows,
                              'set_pct_complete': '{0:0.2f}%'.format(set_pct_complete),
                              'set_pct_progress_bar': '{0:0.0f}%'.format(set_pct_complete),
                              'tu_needed': tu_needed,
                              'bs_needed': bs_needed,
                              'can_edit_tags': can_edit_tags,
                              'group_choices': group_choices,
                              'qset': qset})

@login_required
def bulk_change_set(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    qset_editors = qset.editor.all()
    qset_writers = qset.writer.all()

    message = ''
    message_class = ''
    tossups = []
    bonuses = []
    role = get_role_no_owner(user, qset)

    if role != 'editor':
        message = 'You are not authorized to make bulk operations on this set'
        return HttpResponseRedirect('/failure.html/')
    else:
        tossups = Tossup.objects.filter(question_set=qset).order_by('-id')
        bonuses = Bonus.objects.filter(question_set=qset).order_by('-id')

    if request.method == 'GET':
        return render(request, 'bulk_change_set.html',
                                 {'user': user,
                                  'tossups': tossups,
                                  'bonuses': bonuses,
                                  'qset': qset,
                                  'message': message,
                                  'message_class': message_class})
    else:
        if ('confirm' in request.POST):
            operation = request.POST['change-type']
            if (operation == "author-step2"):
                return bulk_change_author(request, qset_id)
            elif (operation == "move-step2"):
                return bulk_move_question(request, qset_id)
            elif (operation == "packet-step2"):
                return bulk_change_packet(request, qset_id)

            num_questions_selected = 0
            num_tossups = int(request.POST['num-tossups'])
            num_bonuses = int(request.POST['num-bonuses'])

            change_tossups = []
            change_bonuses = []

            for tu_num in range(num_tossups):
                tu_checked_name = 'tossup-checked-{0}'.format(tu_num)
                tu_id_name = 'tossup-id-{0}'.format(tu_num)

                if (tu_checked_name in request.POST):
                    tu_id = request.POST[tu_id_name]
                    tossup = Tossup.objects.filter(id=tu_id, question_set=qset).first()
                    if tossup is None:
                        continue
                    change_tossups.append(tossup)
                    num_questions_selected += 1

            for bs_num in range(num_bonuses):
                bs_checked_name = 'bonus-checked-{0}'.format(bs_num)
                bs_id_name = 'bonus-id-{0}'.format(bs_num)

                if (bs_checked_name in request.POST):
                    bs_id = request.POST[bs_id_name]
                    bonus = Bonus.objects.filter(id=bs_id, question_set=qset).first()
                    if bonus is None:
                        continue
                    change_bonuses.append(bonus)
                    num_questions_selected += 1

            if (num_questions_selected > 0):
                # Do the actual operation

                if (operation == 'edit'):
                    bulk_edit_questions(True, change_tossups, change_bonuses, qset, user)

                    message = "Successfully edited questions."
                    message_class = 'alert-box success'
                    cache.clear()
                    return render(request, 'bulk_change_set.html',
                                             {'user': user,
                                              'tossups': tossups,
                                              'bonuses': bonuses,
                                              'qset': qset,
                                              'message': message,
                                              'message_class': message_class})
                elif (operation == 'unedit'):
                    bulk_edit_questions(False, change_tossups, change_bonuses, qset, user)

                    message = "Successfully unedited questions."
                    message_class = 'alert-box success'
                    cache.clear()
                    return render(request, 'bulk_change_set.html',
                                             {'user': user,
                                              'tossups': tossups,
                                              'bonuses': bonuses,
                                              'qset': qset,
                                              'message': message,
                                              'message_class': message_class})
                elif (operation == 'packet'):
                    packets = Packet.objects.filter(question_set=qset)
                    
                    cache.clear()
                    return render(request, 'bulk_change_packet.html',
                                             {'user': user,
                                              'tossups': change_tossups,
                                              'bonuses': change_bonuses,
                                              'qset': qset,
                                              'message': message,
                                              'message_class': message_class})
                elif (operation == 'lock'):
                    bulk_lock_questions(True, change_tossups, change_bonuses, qset, user)

                    message = "Successfully locked questions."
                    message_class = 'alert-box success'
                    cache.clear()
                    return render(request, 'bulk_change_set.html',
                                             {'user': user,
                                              'tossups': tossups,
                                              'bonuses': bonuses,
                                              'qset': qset,
                                              'message': message,
                                              'message_class': message_class})
                elif (operation == 'unlock'):
                    bulk_lock_questions(False, change_tossups, change_bonuses, qset, user)

                    message = "Successfully unlocked questions."
                    message_class = 'alert-box success'
                    cache.clear()
                    return render(request, 'bulk_change_set.html',
                                             {'user': user,
                                              'tossups': tossups,
                                              'bonuses': bonuses,
                                              'qset': qset,
                                              'message': message,
                                              'message_class': message_class})
                elif (operation == 'delete'):
                    bulk_delete_questions(change_tossups, change_bonuses, qset, user)
                    message = "Successfully deleted questions."
                    message_class = 'alert-box success'
                    cache.clear()
                    tossups = Tossup.objects.filter(question_set=qset).order_by('-id')
                    bonuses = Bonus.objects.filter(question_set=qset).order_by('-id')

                    return render(request, 'bulk_change_set.html',
                                             {'user': user,
                                              'tossups': tossups,
                                              'bonuses': bonuses,
                                              'qset': qset,
                                              'message': message,
                                              'message_class': message_class})

                elif (operation == 'convert-to-acf-style-tossup'):
                    bulk_convert_to_acf_style_tossup(change_tossups, change_bonuses, qset, user)
                    message = "Successfully converted question type to ACF-style tossups."
                    message_class = 'alert-box success'
                    cache.clear()
                    tossups = Tossup.objects.filter(question_set=qset).order_by('-id')
                    bonuses = Bonus.objects.filter(question_set=qset).order_by('-id')

                    return render(request, 'bulk_change_set.html',
                                             {'user': user,
                                              'tossups': tossups,
                                              'bonuses': bonuses,
                                              'qset': qset,
                                              'message': message,
                                              'message_class': message_class})

                elif (operation == 'convert-to-acf-style-bonus'):
                    bulk_convert_to_acf_style_bonus(change_tossups, change_bonuses, qset, user)
                    message = "Successfully converted question type to ACF-style bonuses."
                    message_class = 'alert-box success'
                    cache.clear()
                    tossups = Tossup.objects.filter(question_set=qset).order_by('-id')
                    bonuses = Bonus.objects.filter(question_set=qset).order_by('-id')

                    return render(request, 'bulk_change_set.html',
                                             {'user': user,
                                              'tossups': tossups,
                                              'bonuses': bonuses,
                                              'qset': qset,
                                              'message': message,
                                              'message_class': message_class})
                elif (operation == 'convert-to-vhsl-bonus'):
                    bulk_convert_to_vhsl_bonus(change_tossups, change_bonuses, qset, user)
                    message = "Successfully converted question type to VHSL bonuses."
                    message_class = 'alert-box success'
                    cache.clear()

                    tossups = Tossup.objects.filter(question_set=qset).order_by('-id')
                    bonuses = Bonus.objects.filter(question_set=qset).order_by('-id')

                    return render(request, 'bulk_change_set.html',
                                             {'user': user,
                                              'tossups': tossups,
                                              'bonuses': bonuses,
                                              'qset': qset,
                                              'message': message,
                                              'message_class': message_class})
                elif (operation == 'move'):
                    new_sets = (user.question_set_editor
                                .exclude(id=qset_id).exclude(archived=True)
                                .order_by('name'))
                    cache.clear()
                    return render(request, 'bulk_move_questions.html',
                                             {'user': user,
                                              'tossups': change_tossups,
                                              'bonuses': change_bonuses,
                                              'qset': qset,
                                              'new_sets': new_sets,
                                              'message': message,
                                              'message_class': message_class})
                elif (operation == 'author'):
                    writers = Writer.objects.filter(Q(question_set_writer=qset) | Q(question_set_editor=qset)).distinct()
                    cache.clear()

                    return render(request, 'bulk_change_author.html',
                                             {'user': user,
                                              'tossups': change_tossups,
                                              'bonuses': change_bonuses,
                                              'qset': qset,
                                              'writers': writers,
                                              'message': message,
                                              'message_class': message_class})

            else:
                message = "Error!  You must select at least one question."
                message_class = 'alert-box warning'
                return render(request, 'bulk_change_set.html',
                                         {'user': user,
                                          'tossups': tossups,
                                          'bonuses': bonuses,
                                          'qset': qset,
                                          'message': message,
                                          'message_class': message_class})
        else:
            message = "You didn't hit the confirm button."
            message_class = 'alert-box warning'
            return render(request, 'bulk_change_set.html',
                                     {'user': user,
                                      'tossups': tossups,
                                      'bonuses': bonuses,
                                      'qset': qset,
                                      'message': message,
                                      'message_class': message_class})

@login_required
def bulk_change_author(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)

    message = ''
    message_class = ''
    read_only = True

    role = get_role_no_owner(user, qset)

    if role != 'editor':
        message = 'You are not authorized to make bulk operations on this set'
        return HttpResponseRedirect('/failure.html/')

    if request.method == 'POST':
        num_tossups = int(request.POST['num-tossups'])
        num_bonuses = int(request.POST['num-bonuses'])
        new_author_id = request.POST['new-author']
        new_author = Writer.objects.get(id=new_author_id)

        new_author_role = get_role_no_owner(new_author, qset)
        if (new_author_role == 'none'):
            message = 'Could not change author to ' + str(new_author)
            return HttpResponseRedirect('/failure.html/')

        for tu_num in range(num_tossups):
            tu_id_name = 'tossup-id-{0}'.format(tu_num)
            tu_id = request.POST[tu_id_name]
            tossup = Tossup.objects.filter(id=tu_id, question_set=qset).first()
            if tossup is None:
                continue
            tossup.author = new_author
            tossup.save()

        for bs_num in range(num_bonuses):
            bs_id_name = 'bonus-id-{0}'.format(bs_num)
            bs_id = request.POST[bs_id_name]
            bonus = Bonus.objects.filter(id=bs_id, question_set=qset).first()
            if bonus is None:
                continue
            bonus.author = new_author
            bonus.save()

        message = 'Successfully changed author'
        message_class = 'alert-box success'
        cache.clear()

        tossups = Tossup.objects.filter(question_set=qset).order_by('-id')
        bonuses = Bonus.objects.filter(question_set=qset).order_by('-id')

        return render(request, 'bulk_change_set.html',
                                 {'user': user,
                                  'tossups': tossups,
                                  'bonuses': bonuses,
                                  'qset': qset,
                                  'message': message,
                                  'message_class': message_class})

@login_required
def bulk_change_packet(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)

    message = ''
    message_class = ''
    read_only = True

    role = get_role_no_owner(user, qset)

    if role != 'editor':
        message = 'You are not authorized to make bulk operations on this set'
        return HttpResponseRedirect('/failure.html/')

    if request.method == 'POST':
        num_tossups = int(request.POST['num-tossups'])
        num_bonuses = int(request.POST['num-bonuses'])
        new_packet_id = request.POST['new-packet']
        # The target packet must belong to this set
        new_packet = Packet.objects.filter(id=new_packet_id, question_set=qset).first()
        if new_packet is None:
            message = 'Could not change packet'
            return HttpResponseRedirect('/failure.html/')

        # TODO: We may want to clear the numbers from these questions in the future
        for tu_num in range(num_tossups):
            tu_id_name = 'tossup-id-{0}'.format(tu_num)
            tu_id = request.POST[tu_id_name]
            tossup = Tossup.objects.filter(id=tu_id, question_set=qset).first()
            if tossup is None:
                continue
            tossup.packet = new_packet
            tossup.save()

        for bs_num in range(num_bonuses):
            bs_id_name = 'bonus-id-{0}'.format(bs_num)
            bs_id = request.POST[bs_id_name]
            bonus = Bonus.objects.filter(id=bs_id, question_set=qset).first()
            if bonus is None:
                continue
            bonus.packet = new_packet
            bonus.save()

        message = 'Successfully changed packet'
        message_class = 'alert-box success'
        cache.clear()

        tossups = Tossup.objects.filter(question_set=qset).order_by('-id')
        bonuses = Bonus.objects.filter(question_set=qset).order_by('-id')

        return render(request, 'bulk_change_set.html',
                                 {'user': user,
                                  'tossups': tossups,
                                  'bonuses': bonuses,
                                  'qset': qset,
                                  'message': message,
                                  'message_class': message_class})

@login_required
def bulk_move_question(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)

    message = ''
    message_class = ''
    read_only = True

    role = get_role_no_owner(user, qset)

    if role != 'editor':
        message = 'You are not authorized to make bulk operations on this set'
        return HttpResponseRedirect('/failure.html/')

    if request.method == 'POST':
        num_tossups = int(request.POST['num-tossups'])
        num_bonuses = int(request.POST['num-bonuses'])
        new_set_id = request.POST['new-set']
        new_set = QuestionSet.objects.get(id=new_set_id)

        new_set_role = get_role_no_owner(user, new_set)
        if (new_set_role != 'editor'):
            message = 'Could not move questions to ' + str(new_set)
            return HttpResponseRedirect('/failure.html/')

        for tu_num in range(num_tossups):
            tu_id_name = 'tossup-id-{0}'.format(tu_num)
            tu_id = request.POST[tu_id_name]
            tossup = Tossup.objects.filter(id=tu_id, question_set=qset).first()
            if tossup is None:
                continue

            tossup.question_set = new_set
            tossup.packet = None

            # It's not guaranteed that these categories exist, so clear them
            tossup.category = None
            tossup.subtype = ''

            tossup.save()
            carry_tags_to_set(tossup, new_set)

        for bs_num in range(num_bonuses):
            bs_id_name = 'bonus-id-{0}'.format(bs_num)
            bs_id = request.POST[bs_id_name]
            bonus = Bonus.objects.filter(id=bs_id, question_set=qset).first()
            if bonus is None:
                continue

            bonus.question_set = new_set
            bonus.packet = None

            # It's not guaranteed that these categories exist, so clear them
            bonus.category = None
            bonus.subtype = ''

            bonus.save()
            carry_tags_to_set(bonus, new_set)

        message = 'Successfully moved questions'
        message_class = 'alert-box success'
        cache.clear()

        tossups = Tossup.objects.filter(question_set=qset).order_by('-id')
        bonuses = Bonus.objects.filter(question_set=qset).order_by('-id')

        return render(request, 'bulk_change_set.html',
                                 {'user': user,
                                  'tossups': tossups,
                                  'bonuses': bonuses,
                                  'qset': qset,
                                  'message': message,
                                  'message_class': message_class})

@login_required
def writer_question_set_settings(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)

    message = ''
    message_class = ''

    role = get_role_no_owner(user, qset)
    if (role == 'none'):
        return render(request, 'failure.html',
            {'message': 'You do not have permissions to this set',
             'message_class': 'alert-box alert'})
    
    # Create the settings if it doesn't exist
    settings = None
    try:
        settings = WriterQuestionSetSettings.objects.get(question_set=qset, writer=user)        
    except:
        settings = WriterQuestionSetSettings(writer=user, question_set=qset)
        settings.save()
        settings.create_per_category_writer_settings()
        
    if request.method == 'POST':
        form = WriterQuestionSetSettingsForm(request.POST)

        PerCategoryWriterSettingsFormset = formset_factory(PerCategoryWriterSettingsForm, can_delete=False, extra=0)
        formset = PerCategoryWriterSettingsFormset(data=request.POST)

        if (form.is_valid() and formset.is_valid()):
            settings.email_on_all_new_comments = form.cleaned_data['email_on_all_new_comments']
            settings.email_on_all_new_questions = form.cleaned_data['email_on_all_new_questions']
            settings.save()
            
            for per_category_form in formset.forms:
                entry_id = int(per_category_form.cleaned_data['entry_id'])
                email_on_new_questions = bool(per_category_form.cleaned_data['email_on_new_questions'])
                email_on_new_comments = bool(per_category_form.cleaned_data['email_on_new_comments'])

                entry = PerCategoryWriterSettings.objects.get(id=entry_id)
                entry.email_on_new_questions = email_on_new_questions
                entry.email_on_new_comments = email_on_new_comments
                entry.activity_on_question_changes = bool(
                    per_category_form.cleaned_data['activity_on_question_changes'])
                entry.save()

            message = 'Your settings have been updated.'
            message_class = 'alert-box success'

            return render(request, 'writer_question_set_settings.html',
                     {'form': form,
                     'formset': formset,
                     'message': message,
                     'message_class': message_class,
                     'user': user,
                     'qset': qset})
            
        else:
            message = 'There was an error saving your settings.'
            message_class = 'alert-box warning'
            return render(request, 'writer_question_set_settings.html',
                     {'form': form,
                     'formset': formset,
                     'message': message,
                     'message_class': message_class,
                     'user': user,
                     'qset': qset})
        
    elif request.method == 'GET':
        entries = settings.percategorywritersettings_set.all()
        initial_data = []
        for entry in entries:
            initial_data.append({
                'entry_id': entry.id,
                'distribution_entry_string': str(entry.distribution_entry),
                'email_on_new_questions': entry.email_on_new_questions,
                'email_on_new_comments': entry.email_on_new_comments,
                'activity_on_question_changes': entry.activity_on_question_changes})
                
        form = WriterQuestionSetSettingsForm(instance=settings)
        PerCategoryWriterSettingsFormset = formset_factory(PerCategoryWriterSettingsForm, can_delete=False, extra=0)
        formset = PerCategoryWriterSettingsFormset(initial=initial_data)
                
        return render(request, 'writer_question_set_settings.html',
                                 {'form': form,
                                  'formset': formset,
                                  'message': message,
                                  'message_class': message_class,
                                  'user': user,
                                  'qset': qset})

@login_required
def contributor(request, qset_id, writer_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    qset_editors = qset.editor.all()
    qset_writers = qset.writer.all()

    writer = Writer.objects.get(id=writer_id)
    
    tossups = []
    bonuses = []

    
    if (writer not in qset_editors and not qset.is_owner(writer) and writer not in qset.writer.all()):
        return render(request, 'failure.html',
            {'message': 'The specified contributor is not in this set',
             'message_class': 'alert-box alert'})
        
    if user not in qset_editors and not qset.is_owner(user) and user not in qset.writer.all():
        return render(request, 'failure.html',
            {'message': 'You are not authorized to view this set',
             'message_class': 'alert-box alert'})

    tossups = Tossup.objects.filter(question_set=qset).filter(author=writer)
    bonuses = Bonus.objects.filter(question_set=qset).filter(author=writer)

    writer_status =   {'tossups_written': tossups.count(),
                         'bonuses_written': bonuses.count()
                         }
            
            
    return render(request, 'contributor.html',
        {
        'user': user,
        'tossups': tossups,
        'bonuses': bonuses,
        'writer_status': writer_status,
        'qset': qset,
        'writer': writer})	


#########################################################################
# Auto-packetization views
#########################################################################

from .packetizer import auto_packetize, build_quota_dict, get_path_parts
from django.db import transaction
from decimal import Decimal, InvalidOperation

def get_packetization_rows(qset):
    """Category tree rows for the packetization page.  Mirrors the category
    overview tree but adds per-packet recommended values derived from the
    set-wide distribution and any previously saved quota entries."""
    num_packets = max(qset.num_packets, 1)
    entries = qset.setwidedistributionentry_set.all().order_by('dist_entry__category', 'dist_entry__subcategory')

    tree = {}
    for entry in entries:
        parts = list(get_path_parts(entry.dist_entry))
        if not parts:
            continue
        leaf_key = tuple(parts)
        tu_total = entry.num_tossups or 0
        bs_total = entry.num_bonuses or 0
        tu_written = qset.tossup_set.filter(category=entry.dist_entry).count()
        bs_written = qset.bonus_set.filter(category=entry.dist_entry).count()

        for i in range(1, len(parts) + 1):
            prefix = tuple(parts[:i])
            if prefix not in tree:
                tree[prefix] = {'tu_total': 0, 'bs_total': 0, 'tu_written': 0, 'bs_written': 0, 'is_leaf': False}
            tree[prefix]['tu_total'] += tu_total
            tree[prefix]['bs_total'] += bs_total
            tree[prefix]['tu_written'] += tu_written
            tree[prefix]['bs_written'] += bs_written
        tree[leaf_key]['is_leaf'] = True

    saved = {e.path: e for e in PacketizationEntry.objects.filter(question_set=qset)}

    rows = []
    for key in sorted(tree.keys()):
        node = tree[key]
        path = ' - '.join(key)
        entry = saved.get(path)
        rec_tu = round(node['tu_total'] / float(num_packets), 1)
        rec_bs = round(node['bs_total'] / float(num_packets), 1)
        is_top = len(key) == 1
        rows.append({
            'path': path,
            'short_name': key[-1],
            'depth': len(key) - 1,
            'padding': (len(key) - 1) * 30,
            'is_top': is_top,
            'is_leaf': node['is_leaf'],
            'tu_total': node['tu_total'],
            'bs_total': node['bs_total'],
            'tu_written': node['tu_written'],
            'bs_written': node['bs_written'],
            'rec_tu': rec_tu,
            'rec_bs': rec_bs,
            'min_tu': entry.min_tossups if entry else (rec_tu if is_top else None),
            'max_tu': entry.max_tossups if entry else _sub_default(rec_tu, is_top),
            'min_bs': entry.min_bonuses if entry else (rec_bs if is_top else None),
            'max_bs': entry.max_bonuses if entry else _sub_default(rec_bs, is_top),
        })
    return rows

def _sub_default(recommended, is_top):
    """The maximum a row starts out holding.  A subcategory only offers
    maximums, and leaving them blank let one packet take every 20th Century
    tossup while another took none, so a subcategory starts at its recommended
    per-packet share -- the shape the distribution already asks for.  Nothing
    else changes: the cap rounds up (`_quota_cap`), so a share of 3.3 still
    admits 4 and the parent minimum stays reachable.

    A recommendation of zero stays blank instead of capping at zero: a
    subcategory with no share of the distribution but questions written in it
    would otherwise be shut out of every packet.
    """
    if is_top:
        return recommended
    return recommended or None

def _parse_quota_value(raw):
    raw = (raw or '').strip()
    if raw == '':
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError('"{0}" is not a number'.format(raw))
    if value < 0:
        raise ValueError('Quota values cannot be negative')
    return value

@login_required
def packetize_set(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    message = ''
    message_class = ''
    report = None

    if not qset.is_owner(user):
        return render(request, 'failure.html',
                                 {'message': 'Only the set owner can packetize a set!',
                                  'message_class': 'alert-box alert'})

    if request.method == 'POST':
        try:
            num_packets = int(request.POST.get('num_packets', qset.num_packets))
            tossups_per_packet = int(request.POST.get('tossups_per_packet', qset.tossups_per_packet))
            bonuses_per_packet = int(request.POST.get('bonuses_per_packet', qset.bonuses_per_packet))
            if num_packets < 1 or tossups_per_packet < 1 or bonuses_per_packet < 1:
                raise ValueError('Packet counts must be positive')

            row_count = int(request.POST.get('row_count', 0))
            new_entries = []
            for i in range(row_count):
                path = request.POST.get('row_{0}_path'.format(i), '').strip()
                if not path:
                    continue
                depth = path.count(' - ')
                min_tu = _parse_quota_value(request.POST.get('row_{0}_min_tu'.format(i)))
                max_tu = _parse_quota_value(request.POST.get('row_{0}_max_tu'.format(i)))
                min_bs = _parse_quota_value(request.POST.get('row_{0}_min_bs'.format(i)))
                max_bs = _parse_quota_value(request.POST.get('row_{0}_max_bs'.format(i)))
                if depth == 0 and (min_tu is None or max_tu is None or min_bs is None or max_bs is None):
                    raise ValueError('Top-level category "{0}" needs minimum and maximum tossups and bonuses'.format(path))
                if (min_tu is not None and max_tu is not None and min_tu > max_tu) or \
                   (min_bs is not None and max_bs is not None and min_bs > max_bs):
                    raise ValueError('Minimum exceeds maximum for "{0}"'.format(path))
                # A row cleared of every value is still saved, so that clearing
                # a subcategory's suggested maximum sticks instead of being
                # filled back in from the recommendation on the next visit.
                new_entries.append(PacketizationEntry(
                    question_set=qset, path=path, depth=depth,
                    min_tossups=min_tu, max_tossups=max_tu,
                    min_bonuses=min_bs, max_bonuses=max_bs))

            with transaction.atomic():
                qset.num_packets = num_packets
                qset.tossups_per_packet = tossups_per_packet
                qset.bonuses_per_packet = bonuses_per_packet
                qset.save()

                PacketizationEntry.objects.filter(question_set=qset).delete()
                PacketizationEntry.objects.bulk_create(new_entries)

                quotas = build_quota_dict(qset)
                report = auto_packetize(qset, num_packets, tossups_per_packet,
                                        bonuses_per_packet, quotas, created_by=user)
            cache.clear()
            message = 'The set has been packetized into {0} packet(s).'.format(num_packets)
            message_class = 'alert-box success'
        except ValueError as ex:
            message = str(ex)
            message_class = 'alert-box warning'

    rows = get_packetization_rows(qset)
    top_min_tu = sum(float(r['min_tu']) for r in rows if r['is_top'] and r['min_tu'] is not None)
    top_min_bs = sum(float(r['min_bs']) for r in rows if r['is_top'] and r['min_bs'] is not None)

    return render(request, 'packetize_set.html',
                             {'qset': qset,
                              'user': user,
                              'rows': rows,
                              'row_count': len(rows),
                              'top_min_tu': round(top_min_tu, 1),
                              'top_min_bs': round(top_min_bs, 1),
                              'report': report,
                              'message': message,
                              'message_class': message_class})

def _grid_answer_preview(text, limit=45):
    # Decode HTML entities (imported answers store apostrophes as &#x27; etc.);
    # the template re-escapes safely, so the grid shows real characters.
    # Parentheticals — pronunciation guides above all — go too: in a 45-character
    # cell they push the actual answer out of view.
    answer = html.unescape(get_answer_no_formatting(get_primary_answer(text or ''))).strip()
    answer = strip_parentheticals(answer)
    if len(answer) > limit:
        answer = answer[:limit].rstrip() + '...'
    return answer

def _packet_neighbors(question, qtype):
    """The question before and after this one in its packet, so you can walk a
    packet without opening a tab per question.

    The walk is the packet's reading order: every tossup by number, then every
    bonus. Crossing from the last tossup into the first bonus is deliberate —
    "next question in the packet" means the next one, not the next one of the
    same kind. Returns ``{'prev': {...}|None, 'next': {...}|None}``, empty for
    an unpacketized question (there's no sequence to walk).
    """
    packet = getattr(question, 'packet', None)
    if packet is None:
        return {'prev': None, 'next': None}

    sequence = []
    for model, kind in ((Tossup, 'tossup'), (Bonus, 'bonus')):
        for q in (model.objects.filter(packet=packet)
                  .only('id', 'question_number', 'tossup_answer' if kind == 'tossup' else 'part1_answer')
                  .order_by('question_number', 'id')):
            sequence.append((kind, q))

    position = next((i for i, (kind, q) in enumerate(sequence)
                     if kind == qtype and q.id == question.id), None)
    if position is None:
        return {'prev': None, 'next': None}

    def describe(index):
        if index < 0 or index >= len(sequence):
            return None
        kind, q = sequence[index]
        answer = q.tossup_answer if kind == 'tossup' else q.part1_answer
        return {
            'url': '/edit_{0}/{1}/'.format(kind, q.id),
            'label': _grid_answer_preview(answer, 40) or 'Untitled',
            'qtype': kind,
            'number': q.question_number,
        }

    return {'prev': describe(position - 1), 'next': describe(position + 1)}


def _tag_count_label(tag, p):
    """The chip-sized progress string -- "2/3", "1/2\u22645", "4\u22647" per type, joined
    with a middot. A type with no quota at either end is left out, and a tag
    with no quota at all just counts what it has."""
    bits = []
    for (lo, hi), done in zip(tag.quota_pairs(),
                              (p['tu_done'], p['bs_done'], p['q_done'])):
        if lo and hi is not None:
            bits.append('{0}/{1}\u2264{2}'.format(done, lo, hi))
        elif lo:
            bits.append('{0}/{1}'.format(done, lo))
        elif hi is not None:
            bits.append('{0}\u2264{1}'.format(done, hi))
        else:
            bits.append(None)
    if all(b is None for b in bits):
        return '{0}\u00b7{1}'.format(p['tu_done'], p['bs_done'])
    # An any-type quota with nothing said about the typed counts stands alone;
    # otherwise the typed pair leads and it follows.
    tu, bs, q = bits
    if tu is None and bs is None:
        return q
    return '\u00b7'.join(x for x in (tu or str(p['tu_done']),
                                bs or str(p['bs_done']),
                                q) if x is not None)


def _tag_names_by_question(qset, for_output=False):
    """{('tossup'|'bonus', question id): [tag names]} for every tagged question
    of the set, in the tags' own order. One query per type rather than one per
    cell; the names alone, since a grid cell has no room for the axis.

    ``for_output`` narrows it to the tags an exported packet may print (see
    CategoryTag.show_in_output); the working pages pass nothing and see all."""
    out = {}
    tags = CategoryTag.objects.filter(question_set=qset)
    if for_output:
        tags = tags.filter(show_in_output=True)
    for tag in tags:
        for tid in tag.tossups.filter(question_set=qset).values_list('id', flat=True):
            out.setdefault(('tossup', tid), []).append(tag.name)
        for bid in tag.bonuses.filter(question_set=qset).values_list('id', flat=True):
            out.setdefault(('bonus', bid), []).append(tag.name)
    return out


def _grid_cell_payload(question, qtype, tag_names=None, warnings=None):
    """What the packet grid shows in one occupied slot. Shared by the page
    render and the live-refresh endpoint, so a cell repainted in place looks
    exactly like a freshly loaded one."""
    if qtype == 'tossup':
        answer = _grid_answer_preview(question.tossup_answer)
    else:
        answer = ' / '.join(filter(None, [
            _grid_answer_preview(question.part1_answer, 20),
            _grid_answer_preview(question.part2_answer, 20),
            _grid_answer_preview(question.part3_answer, 20)]))
    return {
        'id': question.id,
        'answer': answer,
        'category': html.unescape(str(question.category)) if question.category else '',
        'edit_url': '/edit_{0}/{1}/'.format(qtype, question.id),
        'edited': question.edited,
        'proofread': question.proofread,
        'tags': (tag_names or {}).get((qtype, question.id), []),
        'warnings': (warnings or {}).get('{0}-{1}'.format(qtype, question.id), []),
    }


def _per_packet_target(qset, qtype):
    """How many rows a packet should have room for in the grid.

    The distribution's per-packet quota total is the truest statement of how
    many questions belong in a packet — usually 20, or 21/22/24 when the set
    carries tiebreakers — so prefer it, ignoring implausible totals (an
    imported distribution can hold whole-set counts). Otherwise fall back to
    the set's packetization setting, then to 20."""
    field = 'min_tossups' if qtype == 'tossup' else 'min_bonuses'
    total = 0
    if qset.distribution_id:
        total = sum(getattr(e, field) or 0
                    for e in qset.distribution.distributionentry_set.all())
    if 1 <= total <= 60:
        return total
    configured = qset.tossups_per_packet if qtype == 'tossup' else qset.bonuses_per_packet
    return configured or 20


@login_required
def packet_grid(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)

    if not qset.is_owner(user) and user not in qset.editor.all() and user not in qset.writer.all():
        return render(request, 'failure.html',
                                 {'message': 'You are not authorized to view this set!',
                                  'message_class': 'alert-box alert'})

    read_only = not (qset.is_owner(user) or user in qset.editor.all())
    # Natural order (Round 2 before Round 10), extras last, honoring any
    # user-set custom order (Packet.sort_order).
    packets = sorted_packets(qset)

    packet_name_by_id = {p.id: p.packet_name for p in packets}

    # How many rows to lay out per packet, even where nothing is assigned yet —
    # an empty set showed an empty grid with nowhere to drop a question.
    tu_per_packet = _per_packet_target(qset, 'tossup')
    bs_per_packet = 0 if qset.tossups_only else _per_packet_target(qset, 'bonus')

    # One empty row past the bottom of every grid, so a packet can be given a
    # question beyond the set's per-packet count without first having to invent
    # the row somewhere else. Filling it grows the grid and a fresh spare row
    # appears under it, the way a spreadsheet always has one more line.
    # Read-only viewers get no spare row — there's nothing they could do with it.
    spare_rows = 0 if read_only else 1

    tag_names = _tag_names_by_question(qset)
    warnings = constraint_violation_map(qset)

    def build_rows(question_model, preview_func, edit_url, target_rows=0):
        qtype = 'tossup' if question_model is Tossup else 'bonus'
        vacancies = {(v.packet_id, v.question_number): v.category
                     for v in PacketSlotVacancy.objects.filter(question_set=qset, question_type=qtype)}
        cells_by_packet = {}
        unplaced = []  # questions that can't occupy a grid slot (see below)
        max_num = 0
        # Order by number so a duplicate's FIRST holder keeps the slot and the
        # later one is surfaced as unplaced (deterministic, not random).
        for question in (question_model.objects.filter(question_set=qset, packet__in=packets)
                         .select_related('category', 'packet').order_by('question_number', 'id')):
            number = question.question_number or 0
            cell = _grid_cell_payload(question, qtype, tag_names, warnings)
            # A question with no number, or a duplicate number within its packet,
            # can't be placed in the number-keyed grid — surfacing it here keeps
            # it from silently vanishing (this is how a tiebreaker could "not
            # show": a manually-added question reused an existing number).
            if number <= 0 or number in cells_by_packet.get(question.packet_id, {}):
                cell['packet_name'] = packet_name_by_id.get(question.packet_id, '')
                cell['reason'] = ('has no question number' if number <= 0
                                  else 'duplicates #{0} in this packet'.format(number))
                unplaced.append(cell)
                continue
            max_num = max(max_num, number)
            cells_by_packet.setdefault(question.packet_id, {})[number] = cell
        rows = []
        last_real = max(max_num, target_rows)
        for number in range(1, last_real + spare_rows + 1):
            cells = []
            for p in packets:
                cell = cells_by_packet.get(p.id, {}).get(number)
                # Empty slot: carry the packet id so the grid can offer an
                # "add a question here" link (handy for tiebreaker rows), plus
                # the category of whatever was last removed from this slot.
                cells.append(cell if cell is not None else
                             {'empty': True, 'packet_id': p.id,
                              'removed_category': vacancies.get((p.id, number), '')})
            rows.append({'num': number, 'cells': cells, 'spare': number > last_real})
        return rows, unplaced

    tossup_rows, unplaced_tu = build_rows(
        Tossup, lambda t: _grid_answer_preview(t.tossup_answer), '/edit_tossup/',
        target_rows=tu_per_packet)
    bonus_rows, unplaced_bs = build_rows(
        Bonus, lambda b: ' / '.join(filter(None, [
            _grid_answer_preview(b.part1_answer, 20),
            _grid_answer_preview(b.part2_answer, 20),
            _grid_answer_preview(b.part3_answer, 20)])), '/edit_bonus/',
        target_rows=bs_per_packet)

    def build_unpacketized(question_model, preview_func):
        items = []
        for question in (question_model.objects.filter(question_set=qset, packet=None)
                         .select_related('category').order_by('id')):
            items.append({
                'id': question.id,
                'answer': preview_func(question),
                'category': html.unescape(str(question.category)) if question.category else '',
                'edit_url': '/edit_{0}/{1}/'.format(
                    'tossup' if question_model is Tossup else 'bonus', question.id),
            })
        return items

    unpacketized_tu = build_unpacketized(
        Tossup, lambda t: _grid_answer_preview(t.tossup_answer))
    unpacketized_bs = build_unpacketized(
        Bonus, lambda b: ' / '.join(filter(None, [
            _grid_answer_preview(b.part1_answer, 20),
            _grid_answer_preview(b.part2_answer, 20),
            _grid_answer_preview(b.part3_answer, 20)])))

    return render(request, 'packet_grid.html',
                             {'qset': qset,
                              'user': user,
                              'packets': packets,
                              'tossup_rows': tossup_rows,
                              'bonus_rows': bonus_rows,
                              'unplaced_tu': unplaced_tu,
                              'unplaced_bs': unplaced_bs,
                              # The same numbers the rows were laid out from, so
                              # the "(TB)" marking matches the grid you see.
                              'tossups_per_packet': tu_per_packet,
                              'bonuses_per_packet': bs_per_packet,
                              'unassigned_tu': len(unpacketized_tu),
                              'unassigned_bs': len(unpacketized_bs),
                              'unpacketized_tu': unpacketized_tu,
                              'unpacketized_bs': unpacketized_bs,
                              'extra_crumb': 'Packet Grid',
                              'extra_crumb_url': '/packet_grid/{0}/'.format(qset.id),
                              'read_only': read_only})

def _log_packet_grid_change(qset, changer, description, prior_states):
    """Record a packet-grid change with the prior state of each affected
    question so it can be undone later."""
    PacketGridLog.objects.create(
        question_set=qset, changer=changer, description=description,
        undo_data=json.dumps(prior_states))


@login_required
def move_packet_question(request):
    user = request.user.writer
    message = ''
    success = False

    if request.method == 'POST':
        try:
            question_type = request.POST['question_type']
            question_id = int(request.POST['question_id'])
            target_packet_id = int(request.POST['target_packet_id'])
            target_number = int(request.POST['target_number'])

            model = Tossup if question_type == 'tossup' else Bonus
            question = model.objects.get(id=question_id)
            qset = question.question_set
            target_packet = Packet.objects.get(id=target_packet_id)

            if target_packet.question_set_id != qset.id:
                message = 'The target packet belongs to a different set!'
            elif not (qset.is_owner(user) or user in qset.editor.all()):
                message = 'You are not authorized to move questions in this set!'
            elif target_number < 1:
                message = 'Invalid question number!'
            else:
                occupant = model.objects.filter(
                    packet=target_packet, question_number=target_number).exclude(id=question.id).first()
                source_packet, source_number = question.packet, question.question_number
                # Capture prior state for undo before mutating.
                prior = [{'qtype': question_type, 'id': question.id,
                          'packet_id': source_packet.id if source_packet else None,
                          'number': source_number}]
                if occupant is not None:
                    prior.append({'qtype': question_type, 'id': occupant.id,
                                  'packet_id': target_packet.id, 'number': target_number})
                question.packet = target_packet
                question.question_number = target_number
                question.save()
                if occupant is not None:
                    occupant.packet = source_packet
                    occupant.question_number = source_number
                    occupant.save()
                src_name = source_packet.packet_name if source_packet else 'Unassigned'
                desc = 'Moved {0} from {1} #{2} to {3} #{4}'.format(
                    question_type, src_name, source_number or '?',
                    target_packet.packet_name, target_number)
                if occupant is not None:
                    desc += ' (swapped)'
                _log_packet_grid_change(qset, user, desc, prior)
                cache.clear()
                success = True
                message = 'Question moved'
        except (KeyError, ValueError):
            message = 'Invalid request!'
        except (Tossup.DoesNotExist, Bonus.DoesNotExist, Packet.DoesNotExist):
            message = 'Question or packet not found!'

    return HttpResponse(json.dumps({'success': success, 'message': message}))


@login_required
def set_packet_order(request):
    """Persist a user-defined packet order (drag-to-reorder on the grid).
    Body: qset_id, packet_ids[] in the desired order."""
    user = request.user.writer
    if request.method != 'POST':
        return HttpResponse(json.dumps({'success': False, 'message': 'Invalid request'}))
    try:
        qset = QuestionSet.objects.get(id=int(request.POST['qset_id']))
    except (KeyError, ValueError, QuestionSet.DoesNotExist):
        return HttpResponse(json.dumps({'success': False, 'message': 'Set not found'}))
    if not (qset.is_owner(user) or user in qset.editor.all()):
        return HttpResponse(json.dumps({'success': False, 'message': 'Not authorized'}))
    ids = request.POST.getlist('packet_ids[]')
    valid = {p.id: p for p in qset.packet_set.all()}
    order = 1
    to_update = []
    for pid in ids:
        try:
            p = valid.get(int(pid))
        except (TypeError, ValueError):
            p = None
        if p is not None:
            p.sort_order = order
            to_update.append(p)
            order += 1
    if to_update:
        Packet.objects.bulk_update(to_update, ['sort_order'])
        cache.clear()
    return HttpResponse(json.dumps({'success': True, 'message': 'Order saved'}))


@login_required
def unassign_packet_question(request):
    """Remove a question from its packet (back to the unpacketized pool)."""
    user = request.user.writer
    message = ''
    success = False
    if request.method == 'POST':
        try:
            question_type = request.POST['question_type']
            question_id = int(request.POST['question_id'])
            model = Tossup if question_type == 'tossup' else Bonus
            question = model.objects.get(id=question_id)
            qset = question.question_set
            if not (qset.is_owner(user) or user in qset.editor.all()):
                message = 'You are not authorized to move questions in this set!'
            elif question.packet_id is None:
                success = True
                message = 'Already unpacketized'
            else:
                prior = [{'qtype': question_type, 'id': question.id,
                          'packet_id': question.packet_id, 'number': question.question_number}]
                src_name = question.packet.packet_name
                src_num = question.question_number
                # Remember the category that used to fill this slot so the empty
                # grid cell can hint what belongs there.
                if src_num:
                    PacketSlotVacancy.objects.update_or_create(
                        packet_id=question.packet_id, question_number=src_num,
                        question_type=question_type,
                        defaults={'question_set': qset,
                                  'category': str(question.category) if question.category_id else ''})
                question.packet = None
                question.question_number = None
                question.save()
                _log_packet_grid_change(
                    qset, user, 'Unpacketized {0} from {1} #{2}'.format(
                        question_type, src_name, src_num or '?'), prior)
                cache.clear()
                success = True
                message = 'Question unpacketized'
        except (KeyError, ValueError):
            message = 'Invalid request!'
        except (Tossup.DoesNotExist, Bonus.DoesNotExist):
            message = 'Question not found!'
    return HttpResponse(json.dumps({'success': success, 'message': message}))


def _create_packets_continuing(qset, count, existing, user):
    """Create `count` new packets, continuing the existing naming scheme
    ("Round 01" -> "Round 11"), and return them. Mirrors _ensure_packets."""
    base = 'Packet'
    if existing:
        m = re.match(r'^(.*?)\s*\d+$', existing[-1].packet_name or '')
        if m and m.group(1).strip():
            base = m.group(1).strip()
    names = set(p.packet_name for p in qset.packet_set.all())
    created = []
    next_num = len(existing) + 1
    while len(created) < count:
        name = '{0} {1:02d}'.format(base, next_num)
        next_num += 1
        if name in names:
            continue
        created.append(Packet.objects.create(
            question_set=qset, packet_name=name, created_by=user))
        names.add(name)
    return created


@login_required
def add_packet(request, qset_id):
    """Add a single empty packet to a set (owner/editor), continuing the naming
    scheme. Doesn't touch existing packets, questions, or packetization — just
    adds one more packet. Redirects back to the page the button was on."""
    user = request.user.writer
    try:
        qset = QuestionSet.objects.get(id=int(qset_id))
    except (ValueError, QuestionSet.DoesNotExist):
        return HttpResponseRedirect('/main/')
    if not (qset.is_owner(user) or user in qset.editor.all()):
        return render(request, 'failure.html',
                      {'message': 'Only an owner or editor can add packets.',
                       'message_class': 'alert-box alert'})
    if request.method == 'POST':
        existing = [p for p in sorted_packets(qset) if p.packet_name != EXTRAS_PACKET_NAME]
        new = _create_packets_continuing(qset, 1, existing, user)
        qset.num_packets = (qset.num_packets or 0) + 1
        qset.save(update_fields=['num_packets'])
        cache.clear()
        if new:
            messages.success(request, 'Added packet "{0}".'.format(new[0].packet_name))
    return HttpResponseRedirect(request.META.get('HTTP_REFERER')
                                or '/packet_grid/{0}/'.format(qset.id))


@login_required
def assign_unpacketized(request):
    """Non-destructively place unpacketized questions: first into empty slots of
    existing packets, then into newly created packets for any overflow. Does not
    move questions that are already assigned."""
    user = request.user.writer
    message = ''
    success = False
    if request.method != 'POST':
        return HttpResponse(json.dumps({'success': False, 'message': 'Invalid request'}))
    try:
        qset = QuestionSet.objects.get(id=int(request.POST['qset_id']))
    except (KeyError, ValueError, QuestionSet.DoesNotExist):
        return HttpResponse(json.dumps({'success': False, 'message': 'Set not found'}))
    if not (qset.is_owner(user) or user in qset.editor.all()):
        return HttpResponse(json.dumps(
            {'success': False, 'message': 'You are not authorized to packetize this set!'}))

    def natural_key(p):
        nums = re.findall(r'\d+', p.packet_name or '')
        return (p.packet_name == EXTRAS_PACKET_NAME,
                int(nums[0]) if nums else float('inf'), p.packet_name or '')

    prior = []
    placed = 0

    def fill_existing(model, per_packet, qtype, packets):
        """Fill empty slots in existing packets; return questions left over."""
        nonlocal placed
        unassigned = list(model.objects.filter(question_set=qset, packet=None).order_by('id'))
        if not unassigned:
            return []
        occupied = defaultdict(set)
        for q in model.objects.filter(question_set=qset, packet__in=packets):
            if q.question_number:
                occupied[q.packet_id].add(q.question_number)
        qi = 0
        for p in packets:
            for n in range(1, per_packet + 1):
                if qi >= len(unassigned):
                    return []
                if n not in occupied[p.id]:
                    q = unassigned[qi]
                    qi += 1
                    prior.append({'qtype': qtype, 'id': q.id, 'packet_id': None, 'number': None})
                    q.packet = p
                    q.question_number = n
                    q.save()
                    placed += 1
        return unassigned[qi:]

    def fill_new(model, per_packet, qtype, leftovers, new_packets):
        nonlocal placed
        qi = 0
        for p in new_packets:
            for n in range(1, per_packet + 1):
                if qi >= len(leftovers):
                    return
                q = leftovers[qi]
                qi += 1
                prior.append({'qtype': qtype, 'id': q.id, 'packet_id': None, 'number': None})
                q.packet = p
                q.question_number = n
                q.save()
                placed += 1

    with transaction.atomic():
        existing = sorted(qset.packet_set.exclude(packet_name=EXTRAS_PACKET_NAME),
                          key=natural_key)
        tu_per = qset.tossups_per_packet or 20
        bs_per = qset.bonuses_per_packet or 20
        rem_tu = fill_existing(Tossup, tu_per, 'tossup', existing)
        rem_bs = fill_existing(Bonus, bs_per, 'bonus', existing)

        n_new = max(math.ceil(len(rem_tu) / tu_per) if rem_tu else 0,
                    math.ceil(len(rem_bs) / bs_per) if rem_bs else 0)
        if n_new:
            new_packets = _create_packets_continuing(qset, n_new, existing, user)
            fill_new(Tossup, tu_per, 'tossup', rem_tu, new_packets)
            fill_new(Bonus, bs_per, 'bonus', rem_bs, new_packets)
            qset.num_packets = max(qset.num_packets or 0, len(existing) + n_new)
            qset.save(update_fields=['num_packets'])

        if prior:
            _log_packet_grid_change(
                qset, user,
                'Assigned {0} unpacketized question(s){1}'.format(
                    placed, ' (+{0} new packet(s))'.format(n_new) if n_new else ''),
                prior)
            cache.clear()
            success = True
            message = 'Placed {0} question(s){1}.'.format(
                placed, ' and created {0} new packet(s)'.format(n_new) if n_new else '')
        else:
            success = True
            message = 'No unpacketized questions to place.'

    return HttpResponse(json.dumps({'success': success, 'message': message}))


def _packet_revision(packet):
    """A short token that changes whenever the packet's composition or any of
    its questions change — used by the document view to detect that someone else
    edited the packet (membership, order, or question text) so it can warn the
    viewer to reload. Cheap: two lightweight queries, no new DB columns."""
    import hashlib
    parts = []
    for qtype, qs in (
        ('t', packet.tossup_set.order_by('question_number')
              .values_list('id', 'question_number', 'last_changed_date')),
        ('b', packet.bonus_set.order_by('question_number')
              .values_list('id', 'question_number', 'last_changed_date'))):
        for qid, num, changed in qs:
            parts.append('{0}{1}:{2}:{3}'.format(qtype, qid, num, changed.isoformat() if changed else ''))
    return hashlib.md5('|'.join(parts).encode('utf-8')).hexdigest()[:16]


def _question_revision(question):
    """A token for one question's text, for the document view's inline editing.

    It is just the stamp `save_question` writes, which is what "somebody saved
    this question" means here -- a plain `.save()` (packetizing, renumbering)
    deliberately does not count, since it leaves the words alone.
    """
    changed = getattr(question, 'last_changed_date', None)
    return changed.isoformat() if changed else ''


def _packet_question_revisions(packet):
    """{'tossup-12': {'rev': ..., 'num': ...}} for every question in the packet.

    An open inline editor uses `rev` to be told its question moved without
    reloading the document; the "this packet changed" banner compares the whole
    map against the one the page loaded with, so a change the page made itself
    can be told apart from somebody else's.
    """
    out = {}
    for qtype, qs in (('tossup', packet.tossup_set.values_list('id', 'question_number', 'last_changed_date')),
                      ('bonus', packet.bonus_set.values_list('id', 'question_number', 'last_changed_date'))):
        for qid, num, changed in qs:
            out['{0}-{1}'.format(qtype, qid)] = {
                'rev': changed.isoformat() if changed else '', 'num': num}
    return out


@login_required
def packet_revision(request, packet_id):
    """JSON {revision} for the given packet, polled by the document view to
    detect concurrent changes. Also carries the per-question revisions, which
    inline editing uses to flag the one question you are actually typing in."""
    try:
        packet = Packet.objects.select_related('question_set').get(id=packet_id)
    except Packet.DoesNotExist:
        return HttpResponse(json.dumps({'error': 'not found'}), status=404)
    user = request.user.writer
    qset = packet.question_set
    if not qset.is_owner(user) and user not in qset.editor.all() and user not in qset.writer.all():
        return HttpResponse(json.dumps({'error': 'forbidden'}), status=403)
    return HttpResponse(json.dumps({'revision': _packet_revision(packet),
                                    'questions': _packet_question_revisions(packet)}))


def _inline_edit_fields(question, qtype):
    """The question's editable prose, as the inline editor shows it."""
    names = [f for f, _label in SUGGESTABLE_FIELDS[qtype]]
    return {f: (getattr(question, f, '') or '') for f in names}


def _question_changer_label(question, qtype):
    """Who saved this question last, for the conflict warning."""
    model = TossupHistory if qtype == 'tossup' else BonusHistory
    if not question.question_history_id:
        return ''
    h = (model.objects.filter(question_history_id=question.question_history_id)
         .order_by('-change_date').select_related('changer__user').first())
    if h is None or h.changer is None:
        return ''
    name = '{0} {1}'.format(h.changer.user.first_name, h.changer.user.last_name).strip()
    return name or h.changer.user.username


@login_required
def inline_save_question(request):
    """Save one question's prose from the document view's inline editor.

    The document view can put a question's own fields on the page and write
    them back here, so a packet can be corrected while it is being read rather
    than by opening each question's edit page in turn.

    Concurrency is the point of `baseline`: it is the revision the editor was
    handed when it opened the question, and a mismatch means somebody else
    saved in the meantime. That is refused with 409 and their current text,
    rather than silently overwriting them -- the client warns and lets the
    editor choose. `force=1` is that choice, made deliberately.
    """
    if request.method != 'POST':
        return HttpResponse(json.dumps({'ok': False, 'error': 'POST required'}), status=405)
    user = request.user.writer
    qtype = request.POST.get('question_type', '')
    qid = request.POST.get('question_id', '')
    if qtype not in SUGGESTABLE_FIELDS or not qid.isdigit():
        return HttpResponse(json.dumps({'ok': False, 'error': 'No such question'}), status=404)
    question = _style_question(qtype, qid)
    if question is None:
        return HttpResponse(json.dumps({'ok': False, 'error': 'No such question'}), status=404)
    qset = question.question_set
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Not authorized'}), status=403)
    if not _can_edit_question(user, qset, question):
        return HttpResponse(json.dumps({'ok': False, 'error': 'This question is locked'}), status=403)

    baseline = request.POST.get('baseline', '')
    current = _question_revision(question)
    if baseline and baseline != current and request.POST.get('force') != '1':
        return HttpResponse(json.dumps({
            'ok': False, 'conflict': True,
            'error': 'This question changed since you started editing it.',
            'revision': current,
            'changed_by': _question_changer_label(question, qtype),
            'changed_date': timezone.localtime(question.last_changed_date).strftime('%m/%d/%y %H:%M')
                            if question.last_changed_date else '',
            'fields': _inline_edit_fields(question, qtype),
            'html': question.to_html(),
        }), status=409)

    # Only the prose fields, and only the ones actually sent -- the editor
    # shows a bonus's parts but a VHSL bonus posts just the one.
    changed_any = False
    for field, _label in SUGGESTABLE_FIELDS[qtype]:
        if field not in request.POST:
            continue
        value = strip_markup(request.POST.get(field, ''))
        if (getattr(question, field, '') or '') != value:
            setattr(question, field, value)
            changed_any = True
    if not changed_any:
        return HttpResponse(json.dumps({
            'ok': True, 'unchanged': True, 'revision': current,
            'fields': _inline_edit_fields(question, qtype), 'html': question.to_html()}))

    try:
        question.is_valid()
    except (InvalidTossup, InvalidBonus) as e:
        return HttpResponse(json.dumps({'ok': False, 'error': str(e)}), status=400)

    question.save_question(edit_type=QUESTION_CHANGE, changer=user)
    cache.clear()

    length = question.character_count()
    if qtype == 'tossup':
        max_length = qset.max_acf_tossup_length
    else:
        max_length = (qset.max_vhsl_bonus_length if question.get_bonus_type() == VHSL_BONUS
                      else qset.max_acf_bonus_length)
    changed_label = timezone.localtime(question.last_changed_date).strftime('%m/%d/%y %H:%M')
    name = '{0} {1}'.format(user.user.first_name, user.user.last_name).strip() or user.user.username
    return HttpResponse(json.dumps({
        'ok': True,
        'revision': _question_revision(question),
        'fields': _inline_edit_fields(question, qtype),
        'html': question.to_html(),
        'length': length,
        'max_length': max_length,
        'over_length': bool(max_length and length > max_length),
        'changed_label': '{0} by {1}'.format(changed_label, name),
    }))


@login_required
def view_packet(request, packet_id):
    user = request.user.writer
    packet = Packet.objects.get(id=packet_id)
    qset = packet.question_set

    if not qset.is_owner(user) and user not in qset.editor.all() and user not in qset.writer.all():
        return render(request, 'failure.html',
                                 {'message': 'You are not authorized to view this packet!',
                                  'message_class': 'alert-box alert'})

    read_only = not (qset.is_owner(user) or user in qset.editor.all())
    order = request.GET.get('order', 'separate')
    if order not in ('separate', 'interleaved'):
        order = 'separate'

    def writer_label(writer):
        if writer is None:
            return ''
        name = '{0} {1}'.format(writer.user.first_name, writer.user.last_name).strip()
        return name or writer.user.username

    # Most recent changer per question, from the question history
    def latest_changers(history_model, questions):
        history_ids = [q.question_history_id for q in questions if q.question_history_id]
        changers = {}
        for h in history_model.objects.filter(question_history_id__in=history_ids) \
                .order_by('change_date').select_related('changer__user'):
            changers[h.question_history_id] = writer_label(h.changer)
        return changers

    def item(question, qtype, edit_url, per_packet, max_length, changers):
        number = question.question_number or 0
        return {
            'id': question.id,
            'qtype': qtype,
            'number': number,
            'html': question.to_html(),
            'category': str(question.category) if question.category else '',
            'tags': tag_names.get((qtype, question.id), []),
            'edit_url': '{0}{1}/'.format(edit_url, question.id),
            'is_tiebreaker': number > per_packet,
            'author': writer_label(question.author),
            'editor': writer_label(question.editor),
            'edited': question.edited,
            'length': question.character_count(),
            'max_length': max_length,
            'changed_date': question.last_changed_date,
            'changed_by': changers.get(question.question_history_id, ''),
            # Short answer line for the swap panel's compact list.
            'answer_preview': (_grid_answer_preview(question.tossup_answer, 44)
                               if qtype == 'tossup'
                               else _grid_answer_preview(question.part1_answer, 44)),
        }

    tag_names = _tag_names_by_question(qset)
    # question_set and question_type come along too: rendering a question asks
    # its set whether guides need quotes and asks a bonus what type it is, and
    # without them that is two more queries per question on the page.
    packet_tossups = list(packet.tossup_set.order_by('question_number')
                          .select_related('category', 'author__user', 'editor__user',
                                          'question_set', 'question_type'))
    packet_bonuses = list(packet.bonus_set.order_by('question_number')
                          .select_related('category', 'author__user', 'editor__user',
                                          'question_set', 'question_type'))
    tossup_changers = latest_changers(TossupHistory, packet_tossups)
    bonus_changers = latest_changers(BonusHistory, packet_bonuses)

    tossups = [item(t, 'tossup', '/edit_tossup/', qset.tossups_per_packet, qset.max_acf_tossup_length, tossup_changers)
               for t in packet_tossups]
    bonuses = [item(b, 'bonus', '/edit_bonus/', qset.bonuses_per_packet,
                    qset.max_vhsl_bonus_length if b.get_bonus_type() == VHSL_BONUS else qset.max_acf_bonus_length,
                    bonus_changers)
               for b in packet_bonuses]

    # Extras beyond the per-packet quota (tiebreakers). Summarized up top so the
    # moderator can see at a glance that the packet runs long and what the spare
    # question is. Guard on a positive quota so an unset (0) quota doesn't flag
    # every question.
    def _extra_summary(model_questions, view_items, qtype, per_packet):
        if not per_packet:
            return []
        out = []
        for q, view in zip(model_questions, view_items):
            if not view['is_tiebreaker']:
                continue
            if qtype == 'tossup':
                ans = _grid_answer_preview(q.tossup_answer)
            else:
                ans = _grid_answer_preview(q.part1_answer, 30)
            out.append({'label': '{0} {1}'.format(
                'Tossup' if qtype == 'tossup' else 'Bonus', view['number']),
                'answer': ans, 'edit_url': view['edit_url']})
        return out

    extras = (_extra_summary(packet_tossups, tossups, 'tossup', qset.tossups_per_packet)
              + _extra_summary(packet_bonuses, bonuses, 'bonus', qset.bonuses_per_packet))

    # Heads-up for the moderator: answer lines flagged "read answer carefully".
    def _careful_answer(q, qtype):
        if qtype == 'tossup':
            return get_formatted_question_html(q.tossup_answer, True, True, False, False)
        parts = [p for p in (q.part1_answer, q.part2_answer, q.part3_answer) if p]
        return ' / '.join(get_formatted_question_html(p, True, True, False, False) for p in parts)

    careful_notes = []
    for t in packet_tossups:
        if t.read_carefully:
            careful_notes.append({'label': 'Tossup', 'number': t.question_number,
                                  'answer': _careful_answer(t, 'tossup')})
    for b in packet_bonuses:
        if b.read_carefully:
            careful_notes.append({'label': 'Bonus', 'number': b.question_number,
                                  'answer': _careful_answer(b, 'bonus')})

    # Raw question data for the client-side Discord copy formatters
    discord_payload = {}
    for t, row in zip(packet_tossups, tossups):
        discord_payload['tossup-{0}'.format(t.id)] = {
            'qtype': 'tossup', 'text': t.tossup_text, 'answer': t.tossup_answer,
            'author': row['author'], 'category': row['category']}
    for b, row in zip(packet_bonuses, bonuses):
        discord_payload['bonus-{0}'.format(b.id)] = {
            'qtype': 'bonus', 'leadin': b.leadin,
            'parts': [{'text': b.part1_text or '', 'answer': b.part1_answer or '', 'diff': b.part1_difficulty or ''},
                      {'text': b.part2_text or '', 'answer': b.part2_answer or '', 'diff': b.part2_difficulty or ''},
                      {'text': b.part3_text or '', 'answer': b.part3_answer or '', 'diff': b.part3_difficulty or ''}],
            'author': row['author'], 'category': row['category']}

    # Raw fields for inline editing, alongside the revision each editor is
    # opened against (see inline_save_question).
    edit_payload = {}
    if not read_only:
        for t in packet_tossups:
            edit_payload['tossup-{0}'.format(t.id)] = {
                'qtype': 'tossup', 'fields': _inline_edit_fields(t, 'tossup'),
                'revision': _question_revision(t)}
        for b in packet_bonuses:
            edit_payload['bonus-{0}'.format(b.id)] = {
                'qtype': 'bonus', 'fields': _inline_edit_fields(b, 'bonus'),
                'bonus_type': 'vhsl' if b.get_bonus_type() == VHSL_BONUS else 'acf',
                'revision': _question_revision(b)}

    interleaved = []
    if order == 'interleaved':
        for i in range(max(len(tossups), len(bonuses))):
            if i < len(tossups):
                interleaved.append(tossups[i])
            if i < len(bonuses):
                interleaved.append(bonuses[i])

    # Comments for the Google-Docs-style margin
    def attach_comments(items, model):
        ct = ContentType.objects.get_for_model(model)
        comments = list(Comment.objects.filter(
            content_type=ct, object_pk__in=[str(q['id']) for q in items],
            is_removed=False).order_by('submit_date').select_related('user'))
        comment_ids = [c.id for c in comments]
        # Which comments are anchored to a text selection, and which are replies.
        anchors = {a.comment_id: a for a in CommentAnchor.objects.filter(comment_id__in=comment_ids)}
        anchored = {cid: a.selected_text for cid, a in anchors.items()}
        parent_of = {r.comment_id: r.parent_id
                     for r in CommentReply.objects.filter(comment_id__in=comment_ids)}
        resolved = set(CommentResolution.objects.filter(
            comment_id__in=comment_ids, resolved=True).values_list('comment_id', flat=True))

        def render_comment(c):
            label = ''
            if c.user is not None:
                label = '{0} {1}'.format(c.user.first_name, c.user.last_name).strip()
            if not label:
                label = c.user_name or (c.user.username if c.user else 'unknown')
            anchor = anchors.get(c.id)
            return {'id': c.id, 'user': label, 'text': c.comment, 'date': c.submit_date,
                    'anchored': c.id in anchored, 'selection': anchored.get(c.id, ''),
                    # The surrounding context the anchor was taken with, so
                    # hovering the comment can re-locate the span it is on --
                    # in the rendered question or in an open inline editor.
                    'anchor_prefix': anchor.prefix if anchor else '',
                    'anchor_suffix': anchor.suffix if anchor else '',
                    'resolved': c.id in resolved,
                    'can_edit': c.user_id == request.user.id,
                    'replies': []}

        rendered = {c.id: render_comment(c) for c in comments}
        by_q = {}
        for c in comments:
            by_q.setdefault(int(c.object_pk), []).append(c)
        for q in items:
            qcs = by_q.get(q['id'], [])
            present = {c.id for c in qcs}
            threads = []
            for c in qcs:
                pid = parent_of.get(c.id)
                if pid in present:
                    continue  # nested under its parent below
                node = rendered[c.id]
                node['replies'] = [rendered[r.id] for r in qcs if parent_of.get(r.id) == c.id]
                threads.append(node)
            q['comments'] = threads

    attach_comments(tossups, Tossup)
    attach_comments(bonuses, Bonus)
    comment_count = sum(len(q['comments']) for q in tossups + bonuses)

    siblings = sorted_packets(qset)
    index = next((i for i, p in enumerate(siblings) if p.id == packet.id), 0)
    prev_packet = siblings[index - 1] if index > 0 else None
    next_packet = siblings[index + 1] if index + 1 < len(siblings) else None

    from .audio import VOICE_CHOICES
    return render(request, 'view_packet.html',
                             {'qset': qset,
                              'packet': packet,
                              'order': order,
                              'tossups': tossups,
                              'bonuses': bonuses,
                              'interleaved': interleaved,
                              'prev_packet': prev_packet,
                              'next_packet': next_packet,
                              'packet_index': index + 1,
                              'packet_count': len(siblings),
                              'comment_count': comment_count,
                              'discord_payload': discord_payload,
                              'edit_payload': edit_payload,
                              'mp3_voices': VOICE_CHOICES,
                              'packet_revision': _packet_revision(packet),
                              'packet_question_revisions': _packet_question_revisions(packet),
                              'careful_notes': careful_notes,
                              'extras': extras,
                              'role': get_role_no_owner(user, qset),
                              'read_only': read_only,
                              'user': user})


def _packet_mp3_params(request, packet_id):
    """Shared setup for the MP3 endpoints. Returns (packet, qset, tossups,
    bonuses, interleaved, include_answers, voice) or an auth failure response."""
    from .audio import NATURAL_VOICES, DEFAULT_VOICE
    user = request.user.writer
    packet = Packet.objects.get(id=packet_id)
    qset = packet.question_set
    if not qset.is_owner(user) and user not in qset.editor.all() and user not in qset.writer.all():
        return render(request, 'failure.html',
                                 {'message': 'You are not authorized to view this packet!',
                                  'message_class': 'alert-box alert'})
    interleaved = request.GET.get('order') == 'interleaved'
    include_answers = request.GET.get('answers', '1') != '0'
    voice = request.GET.get('voice') or DEFAULT_VOICE
    if voice not in NATURAL_VOICES:
        voice = DEFAULT_VOICE
    tossups = list(packet.tossup_set.order_by('question_number'))
    bonuses = list(packet.bonus_set.order_by('question_number'))
    return packet, qset, tossups, bonuses, interleaved, include_answers, voice


@login_required
def packet_mp3(request, packet_id):
    """Serve an MP3 reading of a packet. If it is not ready yet, kick off a
    background render and show a page that polls until it can download.

    Query params: order=separate|interleaved, answers=1|0.
    """
    parsed = _packet_mp3_params(request, packet_id)
    if isinstance(parsed, HttpResponse):
        return parsed
    packet, qset, tossups, bonuses, interleaved, include_answers, voice = parsed

    from .audio import cache_file, packet_status, start_generation

    if packet_status(packet, tossups, bonuses, interleaved, include_answers, voice) == 'ready':
        order_label = 'interleaved' if interleaved else 'grouped'
        filename = '{0} ({1}).mp3'.format(packet.packet_name, order_label)
        with open(cache_file(packet.id, interleaved, include_answers, voice), 'rb') as f:
            response = HttpResponse(f.read(), content_type='audio/mpeg')
        response['Content-Disposition'] = 'attachment; filename="{0}"'.format(filename)
        return response

    # Not ready: make sure a background render is running, then show a waiting page.
    start_generation(packet, tossups, bonuses, interleaved=interleaved,
                     include_answers=include_answers, voice=voice)
    return render(request, 'packet_mp3_preparing.html',
                             {'packet': packet, 'qset': qset,
                              'order': 'interleaved' if interleaved else 'separate',
                              'answers': '1' if include_answers else '0',
                              'voice': voice},
                             status=202)


@login_required
def packet_mp3_status(request, packet_id):
    """JSON status for a packet MP3 render: ready | running | error | absent."""
    parsed = _packet_mp3_params(request, packet_id)
    if isinstance(parsed, HttpResponse):
        return parsed
    packet, qset, tossups, bonuses, interleaved, include_answers, voice = parsed

    from .audio import packet_status, error_message
    status = packet_status(packet, tossups, bonuses, interleaved, include_answers, voice)
    data = {'status': status}
    if status == 'error':
        data['message'] = error_message(packet.id, interleaved, include_answers, voice) or \
            'generation failed'
    response = JsonResponse(data)
    # Never cache status — each poll must reflect the live generation state.
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return response


@login_required
def reorder_packet_questions(request):
    user = request.user.writer
    message = ''
    success = False

    if request.method == 'POST':
        try:
            packet = Packet.objects.get(id=int(request.POST['packet_id']))
            qset = packet.question_set
            if not (qset.is_owner(user) or user in qset.editor.all()):
                message = 'You are not authorized to reorder questions in this set!'
            else:
                prior = []
                for key, model in (('tossup_ids[]', Tossup), ('bonus_ids[]', Bonus)):
                    qtype = 'tossup' if model is Tossup else 'bonus'
                    ids = [int(x) for x in request.POST.getlist(key)]
                    if not ids:
                        continue
                    questions = {q.id: q for q in model.objects.filter(packet=packet, id__in=ids)}
                    if len(questions) != len(ids):
                        raise ValueError('Question list does not match the packet')
                    changed = []
                    for number, qid in enumerate(ids, start=1):
                        question = questions[qid]
                        if question.question_number != number:
                            prior.append({'qtype': qtype, 'id': question.id,
                                          'packet_id': packet.id, 'number': question.question_number})
                            question.question_number = number
                            changed.append(question)
                    model.objects.bulk_update(changed, ['question_number'])
                if prior:
                    _log_packet_grid_change(
                        qset, user, 'Reordered questions in {0}'.format(packet.packet_name), prior)
                cache.clear()
                success = True
                message = 'Order saved'
        except (KeyError, ValueError):
            message = 'Invalid request!'
        except Packet.DoesNotExist:
            message = 'Packet not found!'

    return HttpResponse(json.dumps({'success': success, 'message': message}))


def _swap_search_norm(text):
    """Normalize question text for swap-dialog searching: decode HTML entities,
    drop tags and QEMS markup characters, strip diacritics, collapse whitespace,
    casefold. Makes "marc jacobs" match an answer stored as "Marc _Jacobs_" and
    "republique" match "République"."""
    t = html.unescape(text or '')
    t = re.sub(r'<[^>]+>', '', t)
    t = t.replace('\\P', '').replace('_', '').replace('~', '')
    t = unicodedata.normalize('NFKD', t)
    t = ''.join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r'\s+', ' ', t).casefold().strip()


def _swap_search_candidates(model, qset, question_type, search, exclude_id=None):
    """The set's questions matching a swap-dialog search. Matching happens in
    Python on normalized text (see _swap_search_norm) rather than SQL icontains,
    which fails whenever a query spans stored markup, an HTML entity, or a
    pronunciation guide. Every search word must appear somewhere in the
    question's combined answer/text; capped at 200 results."""
    terms = _swap_search_norm(search).split()
    qs = (model.objects.filter(question_set=qset)
          .select_related('category', 'packet')
          .order_by('packet__packet_name', 'question_number'))
    out = []
    for q in qs:
        if q.id == exclude_id:
            continue
        if question_type == 'tossup':
            fields = (q.tossup_answer, q.tossup_text)
        else:
            fields = (q.leadin, q.part1_text, q.part1_answer,
                      q.part2_text, q.part2_answer, q.part3_text, q.part3_answer)
        hay = _swap_search_norm(' '.join(f for f in fields if f))
        if all(t in hay for t in terms):
            out.append(q)
            if len(out) >= 200:
                break
    return out


#########################################################################
# Question order constraints
#
# "These two must not share a packet", "this one gives that one away, so it
# has to come later". A writer knows these at the moment they notice them;
# without somewhere to record them they are remembered until packetization,
# or not.
#########################################################################

def _constraint_question(qtype, qid, seen=None):
    """The question a constraint end points at, or None if it is gone."""
    if seen is not None and (qtype, qid) in seen:
        return seen[(qtype, qid)]
    model = Tossup if qtype == 'tossup' else Bonus if qtype == 'bonus' else None
    q = None
    if model is not None:
        q = model.objects.filter(id=qid).select_related('packet', 'category').first()
    if seen is not None:
        seen[(qtype, qid)] = q
    return q


def _packet_positions(qset):
    """Packet id -> its place in reading order, so 'earlier' has a meaning."""
    return {p.id: i for i, p in enumerate(sorted_packets(qset))}


def constraint_violations(qset):
    """Every constraint in the set that its questions currently break.

    Returns a list of dicts, and a question with nothing wrong appears in none
    of them. A question not yet in a packet cannot break an ordering rule --
    there is no order to break -- so it is reported as unplaced rather than as
    a violation, which keeps a set that has not been packetized from lighting
    up red.
    """
    positions = _packet_positions(qset)
    seen = {}
    out = []
    for c in qset.order_constraints.all().select_related('created_by__user'):
        src = _constraint_question(c.source_type, c.source_id, seen)
        dst = _constraint_question(c.target_type, c.target_id, seen)
        if src is None or dst is None:
            continue
        a = positions.get(src.packet_id) if src.packet_id else None
        b = positions.get(dst.packet_id) if dst.packet_id else None
        if a is None or b is None:
            continue
        if c.kind == QuestionOrderConstraint.BEFORE:
            ok = a < b
            problem = 'is not in an earlier packet'
        elif c.kind == QuestionOrderConstraint.AFTER:
            ok = a > b
            problem = 'is not in a later packet'
        else:
            gap = abs(a - b)
            ok = gap >= c.packets
            problem = ('is in the same packet' if gap == 0 else
                       'is only {0} packet{1} away'.format(gap, '' if gap == 1 else 's'))
        if ok:
            continue
        src_answer = _grid_answer_preview(_constraint_answer(src), 40)
        dst_answer = _grid_answer_preview(_constraint_answer(dst), 40)
        src_where = str(src.packet) if src.packet_id else ''
        dst_where = str(dst.packet) if dst.packet_id else ''
        out.append({
            'constraint': c, 'source': src, 'target': dst,
            'source_key': '{0}-{1}'.format(c.source_type, c.source_id),
            'target_key': '{0}-{1}'.format(c.target_type, c.target_id),
            'source_type': c.source_type, 'target_type': c.target_type,
            'source_answer': src_answer, 'target_answer': dst_answer,
            'source_where': src_where, 'target_where': dst_where,
            'source_url': '/edit_{0}/{1}/'.format(c.source_type, c.source_id),
            'target_url': '/edit_{0}/{1}/'.format(c.target_type, c.target_id),
            'problem': problem,
            'note': c.note,
            'source_message': 'Must be {0} "{1}" ({2}), but is in {3}.'.format(
                c.describe(), dst_answer, dst_where or 'no packet', src_where),
            'target_message': 'Must be {0} "{1}" ({2}), but is in {3}.'.format(
                _constraint_inverse_phrase(c), src_answer,
                src_where or 'no packet', dst_where),
            'description': '{0} {1} {2}'.format(src_answer, c.describe(), dst_answer),
        })
    return out


def _constraint_inverse_phrase(c):
    """The same rule read from the other question's side: if A must come before
    B, then B must come after A."""
    if c.kind == QuestionOrderConstraint.BEFORE:
        return 'in a later packet than'
    if c.kind == QuestionOrderConstraint.AFTER:
        return 'in an earlier packet than'
    return c.describe()


def _constraint_answer(question):
    """The answer line to show for either kind of question."""
    return (question.tossup_answer if isinstance(question, Tossup)
            else question.part1_answer)


def constraint_violation_map(qset):
    """'tossup-12' -> what that question is breaking, worded from its own side,
    for the grid's warning marks.

    Deliberately uncached: what makes a rule pass or fail is where the questions
    sit, and moving a question between packets doesn't touch the question-edit
    fingerprint the other reports cache on -- so a cached mark would go on
    contradicting the grid the writer is looking at. Sets almost always have no
    constraints at all, and that costs one count query."""
    if not qset.order_constraints.exists():
        return {}
    marks = {}
    for v in constraint_violations(qset):
        marks.setdefault(v['source_key'], []).append(v['source_message'])
        marks.setdefault(v['target_key'], []).append(v['target_message'])
    return marks


def _question_constraints(qset, qtype, qid):
    """Both directions, since a rule about A and B belongs to both of them."""
    return qset.order_constraints.filter(
        Q(source_type=qtype, source_id=qid) | Q(target_type=qtype, target_id=qid))


def _constraint_rows(qset, qtype, qid):
    """A question's constraints, described from its own side, for its edit
    page. A rule stored as 'B is after A' reads as 'before B' on A's page."""
    constraints = list(_question_constraints(qset, qtype, qid))
    # Most questions have no placement rules; don't make them pay for the ones
    # that do -- this runs on every edit-page load.
    if not constraints:
        return []
    positions = _packet_positions(qset)
    broken = {v['constraint'].id for v in constraint_violations(qset)}
    seen = {}
    rows = []
    for c in constraints:
        mine_is_source = (c.source_type == qtype and c.source_id == qid)
        other_type = c.target_type if mine_is_source else c.source_type
        other_id = c.target_id if mine_is_source else c.source_id
        other = _constraint_question(other_type, other_id, seen)
        if c.kind == QuestionOrderConstraint.APART:
            phrase = c.describe()
        elif mine_is_source:
            phrase = c.describe()
        else:
            phrase = ('in a later packet than' if c.kind == QuestionOrderConstraint.BEFORE
                      else 'in an earlier packet than')
        a = positions.get(_question_packet_id(qset, qtype, qid))
        b = positions.get(other.packet_id) if (other is not None and other.packet_id) else None
        rows.append({
            'constraint': c,
            'other': other,
            'other_type': other_type,
            'other_answer': (_grid_answer_preview(_constraint_answer(other), 60)
                             if other is not None else ''),
            'other_packet': (str(other.packet) if other is not None and other.packet_id
                             else ''),
            'other_edit_url': ('/edit_{0}/{1}/'.format(other_type, other_id)
                               if other is not None else ''),
            'phrase': phrase,
            'missing': other is None,
            'unplaced': other is not None and (a is None or b is None),
            'violated': c.id in broken,
        })
    return rows


def _question_packet_id(qset, qtype, qid):
    model = Tossup if qtype == 'tossup' else Bonus
    return model.objects.filter(id=qid).values_list('packet_id', flat=True).first()


@login_required
def question_constraint(request):
    """Add or remove an ordering constraint. POST only, answered as JSON so the
    edit page can keep its place."""
    user = request.user.writer
    if request.method != 'POST':
        return HttpResponse(json.dumps({'ok': False, 'error': 'POST required'}), status=405)
    action = request.POST.get('action', 'add')

    if action == 'delete':
        c = QuestionOrderConstraint.objects.filter(id=request.POST.get('id') or 0).first()
        if c is None:
            return HttpResponse(json.dumps({'ok': False, 'error': 'That rule is already gone.'}))
        qset = c.question_set
        if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
            return HttpResponse(json.dumps({'ok': False, 'error': 'Not authorized.'}), status=403)
        c.delete()
        return HttpResponse(json.dumps({'ok': True, 'message': 'Rule removed.'}))

    qtype = request.POST.get('question_type', '')
    other_type = request.POST.get('other_type', '')
    if qtype not in ('tossup', 'bonus') or other_type not in ('tossup', 'bonus'):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Unknown question type.'}))
    try:
        qid = int(request.POST['question_id'])
        other_id = int(request.POST['other_id'])
    except (KeyError, ValueError):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Pick a question to relate this to.'}))

    src = _constraint_question(qtype, qid)
    dst = _constraint_question(other_type, other_id)
    if src is None or dst is None:
        return HttpResponse(json.dumps({'ok': False, 'error': 'One of those questions no longer exists.'}))
    if qtype == other_type and qid == other_id:
        return HttpResponse(json.dumps({'ok': False, 'error': 'A question cannot be placed relative to itself.'}))
    qset = src.question_set
    if dst.question_set_id != qset.id:
        return HttpResponse(json.dumps({'ok': False, 'error': 'Both questions have to be in the same set.'}))
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Not authorized.'}), status=403)

    kind = request.POST.get('kind', QuestionOrderConstraint.APART)
    if kind not in dict(QuestionOrderConstraint.KINDS):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Unknown rule.'}))
    try:
        packets = max(1, int(request.POST.get('packets') or 1))
    except ValueError:
        packets = 1

    QuestionOrderConstraint.objects.update_or_create(
        question_set=qset, source_type=qtype, source_id=qid,
        target_type=other_type, target_id=other_id,
        defaults={'kind': kind, 'packets': packets,
                  'note': (request.POST.get('note') or '').strip()[:200],
                  'created_by': user})
    return HttpResponse(json.dumps({'ok': True, 'message': 'Rule saved.'}))


@login_required
def swap_candidates(request):
    """JSON list of questions that could go in a slot. With a source
    ``question_id`` it lists swap candidates filtered by category scope ('leaf',
    'sub', 'top') or free-text. Without one (filling an empty slot) it lists
    unpacketized questions, or — with a search — any matching question."""
    user = request.user.writer
    question_type = request.GET.get('question_type')
    if question_type not in ('tossup', 'bonus'):
        return HttpResponse(json.dumps({'error': 'Invalid question type.'}))
    model = Tossup if question_type == 'tossup' else Bonus
    search = request.GET.get('q', '').strip()
    raw_qid = request.GET.get('question_id')
    fill_mode = not raw_qid  # empty-slot mode: find any question to place

    if fill_mode:
        try:
            qset = QuestionSet.objects.get(id=int(request.GET['qset_id']))
        except (KeyError, ValueError, QuestionSet.DoesNotExist):
            return HttpResponse(json.dumps({'error': 'Set not found.'}))
        question = None
    else:
        try:
            question = model.objects.get(id=int(raw_qid))
        except (ValueError, Tossup.DoesNotExist, Bonus.DoesNotExist):
            return HttpResponse(json.dumps({'error': 'Question not found!'}))
        qset = question.question_set

    if not (qset.is_owner(user) or user in qset.editor.all()):
        return HttpResponse(json.dumps({'error': 'You are not authorized to swap questions in this set!'}))

    scope = request.GET.get('scope', 'leaf')
    if scope not in ('leaf', 'sub', 'top'):
        scope = 'leaf'

    if fill_mode:
        if search:
            candidates = _swap_search_candidates(model, qset, question_type, search)
            source_label = 'search: "{0}"'.format(search)
        else:
            # Default to the unpacketized pool — the questions you'd most want
            # to drop into an empty slot.
            candidates = (model.objects.filter(question_set=qset, packet=None)
                          .select_related('category', 'packet').order_by('id')[:200])
            source_label = 'unpacketized'
    elif search:
        candidates = _swap_search_candidates(model, qset, question_type, search,
                                             exclude_id=question.id)
        source_label = 'search: "{0}"'.format(search)
    else:
        entry = question.category
        if entry is None:
            return HttpResponse(json.dumps({'error': 'This question has no category. Type to search instead.'}))

        if scope == 'leaf':
            entry_ids = [entry.id]
        else:
            siblings = DistributionEntry.objects.filter(
                distribution=entry.distribution, category=entry.category)
            if scope == 'sub':
                sub_first = (entry.subcategory or '').split(' - ')[0].strip()
                entry_ids = [e.id for e in siblings
                             if (e.subcategory or '').split(' - ')[0].strip() == sub_first]
            else:
                entry_ids = [e.id for e in siblings]

        candidates = model.objects.filter(question_set=qset, category_id__in=entry_ids) \
            .exclude(packet=question.packet) \
            .select_related('category', 'packet') \
            .order_by('packet__packet_name', 'question_number')[:200]
        source_label = str(entry)

    def preview(q):
        if question_type == 'tossup':
            return _grid_answer_preview(q.tossup_answer)
        return ' / '.join(filter(None, [
            _grid_answer_preview(q.part1_answer, 20),
            _grid_answer_preview(q.part2_answer, 20),
            _grid_answer_preview(q.part3_answer, 20)]))

    per_packet = qset.tossups_per_packet if question_type == 'tossup' else qset.bonuses_per_packet
    # Show unpacketized candidates first so they're easy to grab.
    candidates = sorted(candidates, key=lambda q: (q.packet_id is not None,))
    data = [{
        'id': q.id,
        'packet_id': q.packet_id,
        'packet_name': q.packet.packet_name if q.packet_id else '(unpacketized)',
        'number': q.question_number,
        'answer': html.unescape(preview(q)),
        'category': html.unescape(str(q.category)) if q.category else '',
        'is_tiebreaker': (q.question_number or 0) > per_packet if q.packet_id else False,
        'unpacketized': q.packet_id is None,
    } for q in candidates]

    return HttpResponse(json.dumps({'candidates': data, 'source_category': source_label,
                                    'fill_mode': fill_mode,
                                    'source_answer': html.unescape(preview(question)) if question else ''}))


@login_required
def undo_packet_grid_change(request):
    """Undo the most recent (not-yet-undone) packet-grid change for a set,
    restoring each affected question's prior packet and number."""
    user = request.user.writer
    message = ''
    success = False
    if request.method == 'POST':
        try:
            qset = QuestionSet.objects.get(id=int(request.POST['qset_id']))
        except (KeyError, ValueError, QuestionSet.DoesNotExist):
            return HttpResponse(json.dumps({'success': False, 'message': 'Invalid request!'}))
        if not (qset.is_owner(user) or user in qset.editor.all()):
            return HttpResponse(json.dumps({'success': False, 'message': 'You are not authorized to change this set!'}))

        log = PacketGridLog.objects.filter(question_set=qset, undone=False).order_by('-change_date', '-id').first()
        if log is None:
            return HttpResponse(json.dumps({'success': False, 'message': 'Nothing to undo.'}))
        try:
            prior = json.loads(log.undo_data)
        except ValueError:
            prior = []
        for state in prior:
            model = Tossup if state.get('qtype') == 'tossup' else Bonus
            q = model.objects.filter(id=state.get('id'), question_set=qset).first()
            if q is not None:
                q.packet_id = state.get('packet_id')
                q.question_number = state.get('number')
                q.save()
        log.undone = True
        log.save(update_fields=['undone'])
        cache.clear()
        success = True
        message = 'Undid: {0}'.format(log.description)
    return HttpResponse(json.dumps({'success': success, 'message': message}))


@login_required
def packet_grid_state(request, qset_id):
    """A snapshot of what currently occupies every grid slot, polled by the
    packet grid so it keeps up with what other people are doing.

    Two sets of writers rearranging the same packets used to clobber each
    other's work: each was acting on a grid that had gone stale minutes ago.
    The page repaints changed cells from this, so a move someone else made shows
    up instead of being silently overwritten by the next drag.

    `shape` describes the grid's structure (which packets, how many rows, how
    many loose questions). A change there can't be patched cell-by-cell — a new
    packet is a whole new column — so the page reloads instead.
    """
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return JsonResponse({'ok': False, 'error': 'Not authorized for this set.'}, status=403)

    packets = sorted_packets(qset)
    cells = {}
    max_num = {'tossup': 0, 'bonus': 0}
    unplaced = 0
    unpacketized = {}
    tag_names = _tag_names_by_question(qset)
    warnings = constraint_violation_map(qset)
    for model, qtype in ((Tossup, 'tossup'), (Bonus, 'bonus')):
        # Same ordering as the page build, so a duplicate number resolves to the
        # same winner and the two views never disagree about who holds a slot.
        for question in (model.objects.filter(question_set=qset, packet__in=packets)
                         .select_related('category', 'packet')
                         .order_by('question_number', 'id')):
            number = question.question_number or 0
            key = '{0}|{1}|{2}'.format(qtype, question.packet_id, number)
            if number <= 0 or key in cells:
                unplaced += 1
                continue
            max_num[qtype] = max(max_num[qtype], number)
            cells[key] = _grid_cell_payload(question, qtype, tag_names, warnings)
        unpacketized[qtype] = model.objects.filter(question_set=qset, packet=None).count()

    tu_rows = max(max_num['tossup'], _per_packet_target(qset, 'tossup'))
    bs_rows = 0 if qset.tossups_only else max(max_num['bonus'],
                                              _per_packet_target(qset, 'bonus'))
    return JsonResponse({
        'ok': True,
        'cells': cells,
        'shape': {
            'packets': [p.id for p in packets],
            'tossup_rows': tu_rows,
            'bonus_rows': bs_rows,
            'unplaced': unplaced,
            'unpacketized_tossup': unpacketized['tossup'],
            'unpacketized_bonus': unpacketized['bonus'],
        },
    })


@login_required
def packet_grid_log(request, qset_id):
    """Show the history of packet-grid changes for a set."""
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return render(request, 'failure.html',
                      {'message': 'You are not authorized to view this set!',
                       'message_class': 'alert-box alert'})
    logs = (PacketGridLog.objects.filter(question_set=qset)
            .select_related('changer__user').order_by('-change_date', '-id')[:500])
    return render(request, 'packet_grid_log.html',
                  {'qset': qset, 'user': user, 'logs': logs,
                   'can_edit': qset.is_owner(user) or user in qset.editor.all()})


def _record_visit_and_summarize(user, qset):
    """Record that `user` loaded `qset`'s dashboard, and — on the first load of
    the day only — summarize what other people did to the set since their
    previous visit. Returns None when there's nothing to report (no earlier
    visit, not the first load today, or nobody else touched the set).

    Your own questions/edits/comments are left out: you already know about them.
    """
    now = timezone.now()
    visit = SetVisit.objects.filter(writer=user, question_set=qset).first()
    prev = visit.last_visit if visit else None
    SetVisit.objects.update_or_create(writer=user, question_set=qset,
                                      defaults={'last_visit': now})

    if prev is None:
        return None  # no baseline to measure against on a first-ever visit
    if timezone.localtime(prev).date() >= timezone.localtime(now).date():
        return None  # already loaded the set today

    new_questions = (
        Tossup.objects.filter(question_set=qset, created_date__gt=prev).exclude(author=user).count() +
        Bonus.objects.filter(question_set=qset, created_date__gt=prev).exclude(author=user).count())

    # Only questions that already existed at the last visit can have been
    # "edited" since — a question created after it is counted as new, and its
    # creation history row must not also read as an edit.
    edited_questions = 0
    for model, hist_model in ((Tossup, TossupHistory), (Bonus, BonusHistory)):
        hist_ids = [h for h in model.objects
                    .filter(question_set=qset, created_date__lte=prev)
                    .values_list('question_history_id', flat=True) if h]
        if not hist_ids:
            continue
        edited_questions += (hist_model.objects
                             .filter(question_history_id__in=hist_ids, change_date__gt=prev)
                             .exclude(changer=user)
                             .values('question_history_id').distinct().count())

    tu_ct = ContentType.objects.get_for_model(Tossup)
    bs_ct = ContentType.objects.get_for_model(Bonus)
    tu_ids = [str(i) for i in qset.tossup_set.values_list('id', flat=True)]
    bs_ids = [str(i) for i in qset.bonus_set.values_list('id', flat=True)]
    comments = (Comment.objects.filter(is_removed=False, submit_date__gt=prev)
                .filter(Q(content_type=tu_ct, object_pk__in=tu_ids) |
                        Q(content_type=bs_ct, object_pk__in=bs_ids))
                .exclude(user=user.user)
                # The Discord bot's comments aren't news -- the playtest was
                # the notification. (Its comments have no Django user.)
                .exclude(user__isnull=True, user_name=DISCORD_BOT_NAME)
                .exclude(user__isnull=True,
                         id__in=DiscordCommentRef.objects.values('comment_id'))
                .count())

    total = new_questions + edited_questions + comments
    if not total:
        return None
    parts = [{'count': n, 'label': label}
             for n, label in ((new_questions, 'new question'),
                              (edited_questions, 'edited question'),
                              (comments, 'new comment')) if n]
    for part in parts:
        if part['count'] != 1:
            part['label'] += 's'
    return {'since': prev, 'since_ts': int(prev.timestamp()), 'total': total, 'parts': parts,
            'new_questions': new_questions,
            'edited_questions': edited_questions, 'comments': comments}


def _activity_changes(user, qset, since=None, limit=100):
    """Changes other people made to questions of yours, newest first.

    A question is "yours" once you write or edit it — but becoming its editor
    must not hand you its whole past. Editing one question someone else wrote
    used to dump every earlier revision of it into your activity, which is
    noise you have already seen (you just read the question) and can't act on.
    So each question is only reported from *your own last change to it* onward:
    for one you wrote that is its creation, i.e. everything after; for one you
    edited it is the moment you took it on.

    `since` additionally drops anything at or before that time (the badge's
    "new since you last looked"). Yields at most `limit` rows, newest first —
    the queryset is ordered and consumed lazily, so a long history costs nothing
    beyond what's shown.
    """
    items = []
    for model, hist_model, edit_url in ((Tossup, TossupHistory, '/edit_tossup/'),
                                        (Bonus, BonusHistory, '/edit_bonus/')):
        mine = (model.objects.filter(question_set=qset)
                .filter(Q(author=user) | Q(editor=user))
                .select_related('category'))
        hist_to_q = {q.question_history_id: q for q in mine if q.question_history_id}
        if not hist_to_q:
            continue

        # When each question became yours: your latest change to it. A question
        # you wrote but never re-saved has no row here; nothing about it is
        # backfill, so it has no cutoff at all.
        own_last = {
            r['question_history_id']: r['last']
            for r in (hist_model.objects
                      .filter(question_history_id__in=hist_to_q.keys(), changer=user)
                      .values('question_history_id')
                      .annotate(last=Max('change_date')))
        }

        changes = (hist_model.objects.filter(question_history_id__in=hist_to_q.keys())
                   .exclude(changer=user).select_related('changer__user')
                   .order_by('-change_date'))
        if since is not None:
            changes = changes.filter(change_date__gt=since)
        kept = 0
        for h in changes.iterator():
            q = hist_to_q.get(h.question_history_id)
            if q is None:
                continue
            cutoff = own_last.get(h.question_history_id)
            if cutoff is None and q.author_id != user.id:
                # You appear only as its editor, with no save of your own on
                # record (an imported or bulk-stamped question): the editor
                # stamp is the moment you took it on.
                cutoff = q.edited_date
            if cutoff is not None and h.change_date <= cutoff:
                continue
            items.append((model, hist_model, edit_url, h, q))
            kept += 1
            if kept >= limit:
                break
    return _group_activity_changes(items)[:limit]


# Saves closer together than this, by the same person to the same question,
# read as one sitting rather than as a list of separate events.
from datetime import timedelta as _timedelta
ACTIVITY_GROUP_WINDOW = _timedelta(minutes=30)


def _group_activity_changes(items):
    """Fold a run of saves into one entry.

    Someone editing a question saves it five times in ten minutes; that is one
    edit to you, not five rows. Consecutive history rows by the same changer on
    the same question, each within ACTIVITY_GROUP_WINDOW of the one before,
    become a single (model, hist_model, edit_url, [rows oldest-first], q)
    tuple. Output is newest-first by each group's latest save.
    """
    items = sorted(items, key=lambda row: row[3].change_date)
    groups, open_by_key = [], {}
    for model, hist_model, edit_url, h, q in items:
        key = (model, q.id, h.changer_id)
        g = open_by_key.get(key)
        if g is not None and h.change_date - g[3][-1].change_date <= ACTIVITY_GROUP_WINDOW:
            g[3].append(h)
            continue
        g = (model, hist_model, edit_url, [h], q)
        open_by_key[key] = g
        groups.append(g)
    groups.sort(key=lambda g: g[3][-1].change_date, reverse=True)
    return groups


def _history_text_fields(model):
    """The history columns that hold question text, and those that hold answer
    lines, for a tossup or bonus history model."""
    if model is Tossup:
        return ['tossup_text'], ['tossup_answer']
    return (['leadin', 'part1_text', 'part2_text', 'part3_text'],
            ['part1_answer', 'part2_answer', 'part3_answer'])


def _chars_changed(before, after):
    """Characters inserted plus deleted between two strings, markup aside."""
    import difflib
    a = strip_markup(before or '')
    b = strip_markup(after or '')
    if a == b:
        return 0
    changed = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != 'equal':
            changed += (i2 - i1) + (j2 - j1)
    return changed


def _describe_change(model, before, rows):
    """Say roughly what a run of saves did to a question.

    ``before`` is the history row from just before the run (None if the run
    starts at the question's creation); ``rows`` are the run's saves, oldest
    first. The comparison is end-to-end, so a word changed and changed back
    across two saves counts as nothing -- which is what it was.
    """
    after = rows[-1]
    text_fields, answer_fields = _history_text_fields(model)
    if before is None:
        parts = ['created']
    else:
        text_delta = sum(_chars_changed(getattr(before, f), getattr(after, f)) for f in text_fields)
        answer_delta = sum(_chars_changed(getattr(before, f), getattr(after, f)) for f in answer_fields)
        lines = 'answer line' if model is Tossup else 'answer lines'
        parts = []
        if text_delta:
            parts.append('~{0} character{1} in the text'.format(text_delta, '' if text_delta == 1 else 's'))
        if answer_delta:
            if text_delta:
                parts.append('~{0} in the {1}'.format(answer_delta, lines))
            else:
                parts.append('~{0} character{1} in the {2}'.format(
                    answer_delta, '' if answer_delta == 1 else 's', lines))
        if before.question_type_id != after.question_type_id:
            parts.append('question type changed')
        if model is Bonus and any(getattr(before, f) != getattr(after, f) for f in
                                  ('part1_difficulty', 'part2_difficulty', 'part3_difficulty')):
            parts.append('part difficulties changed')
        if not parts:
            # The save changed something history does not record -- category,
            # lock, proofread flag -- or nothing at all.
            parts.append('metadata only, no text change')
    summary = ', '.join(parts)
    if len(rows) > 1:
        span = rows[-1].change_date - rows[0].change_date
        minutes = int(round(span.total_seconds() / 60))
        summary += ' ({0} saves over {1})'.format(
            len(rows), '{0} min'.format(minutes) if minutes >= 1 else 'a minute')
    return summary


def _watched_category_activity(user, qset, since=None, limit=100, exclude=None):
    """New or changed questions in the categories this writer follows.

    The companion to `_activity_changes`, which reports only questions of your
    own. This one is about the category, so it covers questions you have never
    touched and counts from each one's creation rather than from the moment it
    became yours. Nothing appears unless the writer ticked the category on their
    settings page for this set.

    `exclude` is a set of (model, question id) already reported as yours -- the
    stronger claim of the two, so those are not listed twice.
    """
    row = WriterQuestionSetSettings.objects.filter(
        question_set=qset, writer=user).first()
    if row is None:
        return []
    watched = set(PerCategoryWriterSettings.objects
                  .filter(writer_question_set_settings=row,
                          activity_on_question_changes=True)
                  .values_list('distribution_entry_id', flat=True))
    if not watched:
        return []

    exclude = exclude or set()
    items = []
    for model, hist_model, edit_url in ((Tossup, TossupHistory, '/edit_tossup/'),
                                        (Bonus, BonusHistory, '/edit_bonus/')):
        mine = (model.objects.filter(question_set=qset, category_id__in=watched)
                .select_related('category'))
        hist_to_q = {q.question_history_id: q for q in mine
                     if q.question_history_id and (model, q.id) not in exclude}
        if not hist_to_q:
            continue
        changes = (hist_model.objects.filter(question_history_id__in=hist_to_q.keys())
                   .exclude(changer=user).select_related('changer__user')
                   .order_by('-change_date'))
        if since is not None:
            changes = changes.filter(change_date__gt=since)
        kept = 0
        for h in changes.iterator():
            q = hist_to_q.get(h.question_history_id)
            if q is None:
                continue
            items.append((model, hist_model, edit_url, h, q))
            kept += 1
            if kept >= limit:
                break
    return _group_activity_changes(items)[:limit]


def _activity_feeds(user, qset, since=None, limit=100):
    """Both change feeds for a set, in the order the page shows them."""
    own = _activity_changes(user, qset, since=since, limit=limit)
    seen = set((model, q.id) for model, _h, _e, _rows, q in own)
    watched = _watched_category_activity(user, qset, since=since, limit=limit,
                                         exclude=seen)
    return own, watched


def _change_item(model, hist_model, edit_url, rows, q, user, with_role=True):
    """One row of a change feed: what changed, by whom, measured against the
    last save before the run."""
    first, last = rows[0], rows[-1]
    before = (hist_model.objects
              .filter(question_history_id=first.question_history_id,
                      change_date__lt=first.change_date)
              .order_by('-change_date', '-id').first())
    item = {
        'date': last.change_date,
        'by': str(last.changer) if last.changer else 'unknown',
        'qtype': 'tossup' if model is Tossup else 'bonus',
        'edit_url': '{0}{1}/'.format(edit_url, q.id),
        'preview': (_grid_answer_preview(q.tossup_answer) if model is Tossup
                    else _grid_answer_preview(q.part1_answer, 30)),
        'summary': _describe_change(model, before, rows),
        'saves': len(rows),
        'category': str(q.category) if q.category else '',
        # A run whose first save is the question's own first is a new question,
        # not an edit to one -- worth saying when the feed is a category you
        # follow rather than questions you already know about.
        'is_new': before is None,
    }
    if with_role:
        item['role'] = 'wrote' if q.author_id == user.id else 'edited'
    return item


def _mentions_of_user_in_set(user, qset, since=None):
    """The user's unresolved @mentions on this set's questions.

    Asked from the mentions end rather than the set's: a person has a handful of
    mentions, while naming every question in the set meant sending the database
    a list of eleven thousand ids to find them.
    """
    tu_ct = ContentType.objects.get_for_model(Tossup)
    bs_ct = ContentType.objects.get_for_model(Bonus)
    mentions = (CommentMention.objects.filter(mentioned=user)
                .filter(comment__content_type__in=(tu_ct, bs_ct))
                .exclude(comment__resolution__resolved=True))
    if since is not None:
        mentions = mentions.filter(created_date__gt=since)

    rows = list(mentions.values_list('id', 'comment__content_type_id', 'comment__object_pk'))
    if not rows:
        return []
    wanted = {tu_ct.id: set(), bs_ct.id: set()}
    for _mid, ct_id, pk in rows:
        try:
            wanted.setdefault(ct_id, set()).add(int(pk))
        except (TypeError, ValueError):
            continue
    in_set = {
        tu_ct.id: set(Tossup.objects.filter(question_set=qset, id__in=wanted[tu_ct.id])
                      .values_list('id', flat=True)),
        bs_ct.id: set(Bonus.objects.filter(question_set=qset, id__in=wanted[bs_ct.id])
                      .values_list('id', flat=True)),
    }
    keep = []
    for mid, ct_id, pk in rows:
        try:
            if int(pk) in in_set.get(ct_id, ()):
                keep.append(mid)
        except (TypeError, ValueError):
            continue
    return keep


def _new_activity_count(user, qset):
    """Count activity (mentions + others' changes to your questions) newer than
    the last time the user viewed their activity feed for this set.

    Cached for a minute. This runs from the nav context processor, so every page
    in the app pays for it, and answering it honestly means reading the set's
    questions and their history. The "last seen" time is part of the key, so
    opening the activity feed clears the badge immediately rather than a minute
    later; new activity arriving meanwhile shows up a moment late, which is what
    a badge is for.
    """
    seen = ActivitySeen.objects.filter(writer=user, question_set=qset).first()
    last = seen.last_seen if seen else None

    key = 'navactivity:{0}:{1}:{2}:{3}'.format(
        user.id, qset.id, last.isoformat() if last else '-',
        set_activity_version(qset.id))
    hit = cache.get(key)
    if hit is not None:
        return hit

    count = len(_mentions_of_user_in_set(user, qset, since=last))
    # Same rule as the feed, so the badge never promises items the page won't
    # show (and the cap matches the number the feed lists).
    own, watched = _activity_feeds(user, qset, since=last)
    count += len(own) + len(watched)
    cache.set(key, count, 60)
    return count


@login_required
def activity(request, qset_id):
    """Per-user activity on a set: @mentions of you, plus changes other people
    made to questions you wrote or edited."""
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return render(request, 'failure.html',
                      {'message': 'You are not authorized to view this set!',
                       'message_class': 'alert-box alert'})

    tu_ct = ContentType.objects.get_for_model(Tossup)
    bs_ct = ContentType.objects.get_for_model(Bonus)
    tu_ids = set(str(i) for i in qset.tossup_set.values_list('id', flat=True))
    bs_ids = set(str(i) for i in qset.bonus_set.values_list('id', flat=True))

    # --- @mentions of you on this set's questions ---
    mentions = (CommentMention.objects.filter(mentioned=user)
                .filter(Q(comment__content_type=tu_ct, comment__object_pk__in=tu_ids) |
                        Q(comment__content_type=bs_ct, comment__object_pk__in=bs_ids))
                .exclude(comment__resolution__resolved=True)
                .select_related('comment__user', 'comment__content_type')
                .order_by('-comment__submit_date')[:100])
    mention_items = []
    for m in mentions:
        c = m.comment
        is_tu = c.content_type_id == tu_ct.id
        by = str(c.user) if c.user else (c.user_name or 'unknown')
        mention_items.append({
            'date': c.submit_date, 'by': by, 'text': c.comment,
            'qtype': 'tossup' if is_tu else 'bonus',
            'edit_url': '{0}{1}/'.format('/edit_tossup/' if is_tu else '/edit_bonus/', c.object_pk),
        })

    # --- changes by others to questions you authored or edited, from the point
    # each one became yours, plus anything in a category you follow ---
    own, watched = _activity_feeds(user, qset)
    change_items = [_change_item(model, hist_model, edit, rows, q, user)
                    for model, hist_model, edit, rows, q in own]
    category_items = [_change_item(model, hist_model, edit, rows, q, user, with_role=False)
                      for model, hist_model, edit, rows, q in watched]

    # Mark this set's activity as seen (clears the notification badge).
    ActivitySeen.objects.update_or_create(
        writer=user, question_set=qset, defaults={'last_seen': timezone.now()})

    # What you follow, named on the page whether or not it has anything in it
    # today -- an empty section should still say what it is empty of. An empty
    # list is also what tells the page to offer the settings instead.
    watched_categories = sorted(
        str(p.distribution_entry) for p in PerCategoryWriterSettings.objects.filter(
            writer_question_set_settings__question_set=qset,
            writer_question_set_settings__writer=user,
            activity_on_question_changes=True).select_related('distribution_entry'))

    return render(request, 'activity.html',
                  {'qset': qset, 'user': user,
                   'mention_items': mention_items, 'change_items': change_items,
                   'category_items': category_items,
                   'watched_categories': watched_categories,
                   'following_categories': bool(watched_categories)})


@login_required
def set_members(request, qset_id):
    """JSON list of the set's members (username + real name) for the comment
    @mention autocomplete."""
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return HttpResponse(json.dumps({'members': []}))
    by_name = {}
    for w in list(qset.all_owners()) + list(qset.editor.all()) + list(qset.writer.all()):
        u = w.user
        name = '{0} {1}'.format(u.first_name, u.last_name).strip() or u.username
        by_name[u.username] = name
    members = [{'username': k, 'name': v}
               for k, v in sorted(by_name.items(), key=lambda kv: kv[1].lower())]
    return HttpResponse(json.dumps({'members': members}))


@login_required
def resolve_comment(request):
    """Toggle a comment's resolved status. Resolved comments drop off the
    mentioned writers' activity feeds."""
    user = request.user.writer
    if request.method != 'POST':
        return HttpResponse(json.dumps({'success': False, 'message': 'Invalid request'}))
    try:
        comment = Comment.objects.get(id=int(request.POST['comment_id']))
    except (KeyError, ValueError, Comment.DoesNotExist):
        return HttpResponse(json.dumps({'success': False, 'message': 'Comment not found'}))
    target = comment.content_object
    qset = getattr(target, 'question_set', None)
    if qset is None or not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return HttpResponse(json.dumps({'success': False, 'message': 'You are not authorized.'}))
    res, created = CommentResolution.objects.get_or_create(
        comment=comment, defaults={'resolved': True, 'resolved_by': user})
    if not created:
        res.resolved = not res.resolved
    res.resolved_by = user
    res.save()
    cache.clear()
    return HttpResponse(json.dumps({'success': True, 'resolved': res.resolved}))


@login_required
def edit_comment(request):
    """Reword your own comment. POST: comment_id, comment_text. Returns the
    re-rendered HTML so the caller can swap it in without a reload.

    Only the author edits the wording — editors have Delete and the strike-out
    tool for someone else's feedback. Mentions added by an edit aren't
    re-notified (the notification signals only fire on the original post)."""
    from django.utils.html import linebreaks
    from .templatetags.filters import comment_html
    user = request.user.writer
    if request.method != 'POST':
        return HttpResponse(json.dumps({'success': False, 'message': 'Invalid request'}))
    try:
        comment = Comment.objects.get(id=int(request.POST['comment_id']))
    except (KeyError, ValueError, Comment.DoesNotExist):
        return HttpResponse(json.dumps({'success': False, 'message': 'Comment not found'}))
    text = (request.POST.get('comment_text') or '').strip()
    if not text:
        return HttpResponse(json.dumps({'success': False, 'message': 'A comment cannot be empty.'}))
    target = comment.content_object
    qset = getattr(target, 'question_set', None)
    if qset is None or not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return HttpResponse(json.dumps({'success': False, 'message': 'You are not authorized.'}))
    if comment.user_id != request.user.id:
        return HttpResponse(json.dumps({'success': False,
                                        'message': 'You can only edit your own comments.'}))
    if comment.comment != text:
        comment.comment = text
        comment.save(update_fields=['comment'])
        cache.clear()
    return HttpResponse(json.dumps({'success': True, 'text': text,
                                    'html': linebreaks(str(comment_html(text)))}))


@login_required
def strike_comment(request):
    """Cross out (or un-cross) part of an existing comment so an editor can show
    which feedback has been handled. POST: comment_id, start, end (offsets into
    the displayed comment text), strike ('true'/'false')."""
    user = request.user.writer
    if request.method != 'POST':
        return HttpResponse(json.dumps({'success': False, 'message': 'Invalid request'}))
    try:
        comment = Comment.objects.get(id=int(request.POST['comment_id']))
        start = int(request.POST['start'])
        end = int(request.POST['end'])
    except (KeyError, ValueError, Comment.DoesNotExist):
        return HttpResponse(json.dumps({'success': False, 'message': 'Comment not found'}))
    target = comment.content_object
    qset = getattr(target, 'question_set', None)
    # Only editors/owners cross out comments (a writer marking their own feedback
    # done would be confusing); the strike is a "handled" signal from an editor.
    if qset is None or not (qset.is_owner(user) or user in qset.editor.all()):
        return HttpResponse(json.dumps({'success': False, 'message': 'Only editors can strike comments.'}))
    strike = request.POST.get('strike', 'true') == 'true'
    new_text = toggle_comment_strike(comment.comment, start, end, strike)
    if new_text != comment.comment:
        comment.comment = new_text
        comment.save(update_fields=['comment'])
        cache.clear()
    # Return the re-rendered comment body so the client can swap it in place
    # (no page reload). Mirrors the template's `comment.comment|comment_html|
    # linebreaks` in an autoescape-off context.
    from django.utils.html import linebreaks as _html_linebreaks
    from .templatetags.filters import comment_html
    rendered = _html_linebreaks(str(comment_html(comment.comment)))
    # `text` keeps the in-place editor's copy of the raw markup current.
    return HttpResponse(json.dumps({'success': True, 'html': rendered,
                                    'text': comment.comment}))


@login_required
def add_editor_tag(request):
    """Tag an editor with a category they cover, or a freeform note. POST:
    qset_id, editor_id, and either category=<overview path> or label=<text>.
    Only the set's owner/editors manage tags."""
    user = request.user.writer
    if request.method != 'POST':
        return HttpResponse(json.dumps({'success': False, 'message': 'Invalid request'}))
    try:
        qset = QuestionSet.objects.get(id=int(request.POST['qset_id']))
        editor = Writer.objects.get(id=int(request.POST['editor_id']))
    except (KeyError, ValueError, QuestionSet.DoesNotExist, Writer.DoesNotExist):
        return HttpResponse(json.dumps({'success': False, 'message': 'Not found'}))
    if not (qset.is_owner(user) or user in qset.editor.all()):
        return HttpResponse(json.dumps({'success': False, 'message': 'Only owners/editors can manage tags.'}))
    # The tag says what someone covers on this set, so they have to be on it.
    if not (qset.is_owner(editor) or editor in qset.editor.all() or editor in qset.writer.all()):
        return HttpResponse(json.dumps({'success': False,
                                        'message': 'That writer is not on this set.'}))
    category = (request.POST.get('category') or '').strip()
    label = (request.POST.get('label') or '').strip()[:200]
    if not category and not label:
        return HttpResponse(json.dumps({'success': False, 'message': 'Empty tag.'}))
    tag, _created = EditorTag.objects.get_or_create(
        question_set=qset, editor=editor, category=category, label=label)
    cache.clear()
    return HttpResponse(json.dumps({'success': True, 'id': tag.id, 'text': tag.text(),
                                    'is_category': tag.is_category(), 'editor_id': editor.id}))


@login_required
def delete_editor_tag(request):
    """Remove an editor tag (owner/editors only). POST: tag_id."""
    user = request.user.writer
    if request.method != 'POST':
        return HttpResponse(json.dumps({'success': False, 'message': 'Invalid request'}))
    try:
        tag = EditorTag.objects.select_related('question_set').get(id=int(request.POST['tag_id']))
    except (KeyError, ValueError, EditorTag.DoesNotExist):
        return HttpResponse(json.dumps({'success': False, 'message': 'Tag not found'}))
    qset = tag.question_set
    if not (qset.is_owner(user) or user in qset.editor.all()):
        return HttpResponse(json.dumps({'success': False, 'message': 'Only owners/editors can manage tags.'}))
    tag.delete()
    cache.clear()
    return HttpResponse(json.dumps({'success': True}))


def _question_issue_map(qset):
    """Map 'tossup-<id>'/'bonus-<id>' -> worst repeat-checker severity for every
    flagged question in the set. Cached by the dup-check fingerprint so it only
    recomputes when questions change."""
    if not qset.enable_duplicate_checks:
        return {}
    key = 'dupissues:{0}:{1}'.format(qset.id, _dup_fingerprint(qset))
    cached = cache.get(key)
    if cached is not None:
        return cached
    rank = {CRITICAL: 3, WARNING: 2, INFO: 1}
    issues = {}

    def add(qtype, qid, sev):
        k = '{0}-{1}'.format(qtype, qid)
        if k not in issues or rank.get(sev, 0) > rank.get(issues[k], 0):
            issues[k] = sev

    for group in find_duplicates(qset):
        for e in group['entries']:
            add(e['type'], e['id'], group['severity'])
    for group in find_topic_repeats(qset):
        for e in group['entries']:
            add(e['type'], e['id'], group['severity'])
    for issue in find_internal_issues(qset):
        add(issue['question_type'], issue['question_id'], issue['severity'])

    cache.set(key, issues, 1800)
    return issues


def _bonus_difficulty_orders(qset):
    """How the set's bonuses order their parts by difficulty -- e/m/h, m/h/e,
    and so on -- with the bonuses that carry no (or incomplete) difficulty
    tags listed so they can be given them. A set usually wants its orders
    mixed rather than always easy-first, and this is where the skew shows.
    VHSL bonuses have no parts to order and are left out."""
    from collections import OrderedDict
    letters = {'e': 'easy', 'm': 'medium', 'h': 'hard'}
    orders = {}
    untagged = []
    total = 0
    for b in (qset.bonus_set.select_related('packet', 'question_type')
              .order_by('packet__packet_name', 'question_number', 'id')):
        if b.get_bonus_type() == VHSL_BONUS:
            continue
        total += 1
        diffs = [(getattr(b, 'part{0}_difficulty'.format(i)) or '').strip().lower() for i in (1, 2, 3)]
        where = ('{0} #{1}'.format(b.packet.packet_name, b.question_number)
                 if b.packet_id else 'Unassigned')
        if all(d in letters for d in diffs):
            key = '/'.join(diffs)
            orders.setdefault(key, {'key': key, 'label': '/'.join(letters[d] for d in diffs),
                                    'count': 0, 'ids': []})
            orders[key]['count'] += 1
            orders[key]['ids'].append(b.id)
        else:
            untagged.append({'id': b.id, 'where': where,
                             'answer': _grid_answer_preview(b.part1_answer, 30),
                             'tags': ''.join(d if d in letters else '-' for d in diffs)})
    rows = sorted(orders.values(), key=lambda r: (-r['count'], r['key']))
    tagged = sum(r['count'] for r in rows)
    for r in rows:
        r['pct'] = round(100.0 * r['count'] / tagged, 1) if tagged else 0
    return {'rows': rows, 'untagged': untagged, 'total': total, 'tagged': tagged}


@login_required
def style_check(request, qset_id):
    """Run the style checker over a set's questions. The style guide is
    selectable via ?guide=; default is Minkowski."""
    from . import style_checker
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return render(request, 'failure.html',
                      {'message': 'You are not authorized to view this set!',
                       'message_class': 'alert-box alert'})

    guide = request.GET.get('guide', style_checker.DEFAULT_GUIDE)
    if guide not in style_checker.guide_keys():
        guide = style_checker.DEFAULT_GUIDE

    # Editors can turn individual rules on/off for this set.
    if request.method == 'POST' and request.POST.get('action') == 'save_rules':
        if qset.is_owner(user) or user in qset.editor.all():
            configurable = [c for c, _ in style_checker.configurable_rules(guide)]
            disabled = [c for c in configurable if request.POST.get('rule_' + c) != 'on']
            qset.disabled_style_rules = ','.join(disabled)
            qset.save()
            cache.clear()
        return HttpResponseRedirect('/style_check/{0}/?guide={1}'.format(qset.id, guide))

    disabled = qset.disabled_style_rule_set()
    show_dismissed = request.GET.get('show_dismissed') == '1'

    dismissed = set()
    for d in StyleIssueDismissal.objects.filter(question_set=qset):
        dismissed.add((d.question_type, d.question_id, d.code, d.token))
    # Set-wide dismissals hide a suggestion (code, token) on every question.
    rule_dismissed = set(StyleRuleDismissal.objects.filter(question_set=qset)
                         .values_list('code', 'token'))

    results = []
    counts = {'error': 0, 'warning': 0, 'info': 0}
    checked = 0
    flagged = 0
    dismissed_count = 0

    def collect(qtype, q, issues, label, packet, number, edit_url):
        nonlocal flagged, dismissed_count
        active, shown = [], []
        for i in issues:
            i = dict(i, fixable=('fix' in i))
            i.pop('fix', None)  # keep the transform server-side
            # What dismissing this one would actually silence, so the page can
            # say it before asking rather than after doing it.
            i['describe'] = style_checker.describe_dismissal(
                i['code'], i.get('token', ''))['summary']
            set_wide = (i['code'], i.get('token', '')) in rule_dismissed
            if ((qtype, q.id, i['code'], i.get('token', '')) in dismissed or set_wide):
                dismissed_count += 1
                if show_dismissed:
                    i['dismissed'] = True
                    # Undoing has to match how it was dismissed: a set-wide
                    # dismissal isn't stored against this question at all.
                    i['dismissed_scope'] = 'set' if set_wide else 'question'
                    shown.append(i)
            else:
                counts[i['severity']] = counts.get(i['severity'], 0) + 1
                active.append(i)
        if active:
            flagged += 1
        display = active + shown
        if display:
            results.append({'type': qtype, 'id': q.id, 'edit_url': edit_url, 'label': label,
                            'packet': packet, 'number': number, 'issues': display})

    for tu in qset.tossup_set.select_related('packet').order_by('packet__packet_name', 'question_number'):
        checked += 1
        collect('tossup', tu, style_checker.check_tossup(tu, guide, disabled),
                _grid_answer_preview(tu.tossup_answer),
                tu.packet.packet_name if tu.packet else '', tu.question_number,
                '/edit_tossup/{0}/'.format(tu.id))
    for b in qset.bonus_set.select_related('packet').order_by('packet__packet_name', 'question_number'):
        checked += 1
        collect('bonus', b, style_checker.check_bonus(b, guide, disabled),
                _grid_answer_preview(b.part1_answer, 30),
                b.packet.packet_name if b.packet else '', b.question_number,
                '/edit_bonus/{0}/'.format(b.id))

    placement = constraint_violations(qset)
    counts['warning'] += len(placement)

    return render(request, 'style_check.html',
                  {'qset': qset, 'user': user, 'results': results, 'counts': counts,
                   'placement_violations': placement,
                   'difficulty_orders': _bonus_difficulty_orders(qset),
                   'checked': checked, 'flagged': flagged, 'guide': guide,
                   'dismissed_count': dismissed_count, 'show_dismissed': show_dismissed,
                   'guides': style_checker.STYLE_GUIDES,
                   'guide_obj': next((g for g in style_checker.STYLE_GUIDES if g['key'] == guide), None),
                   'can_configure': qset.is_owner(user) or user in qset.editor.all(),
                   'is_ai_user': _is_ai_user(request.user),
                   'ai_findings_json': (json.dumps(_ai_grammar_findings_json(qset)).replace('<', '\\u003c')
                                        if _is_ai_user(request.user) else '[]'),
                   'rule_settings': [{'code': c, 'label': lbl, 'enabled': c not in disabled}
                                     for c, lbl in style_checker.configurable_rules(guide)]})


@login_required
def packet_issues(request, packet_id):
    """JSON map of repeat-checker issue severities for the questions in a packet
    (for the document-view "show issues" toggle)."""
    user = request.user.writer
    packet = Packet.objects.get(id=packet_id)
    qset = packet.question_set
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return HttpResponse(json.dumps({'issues': {}}))
    full = _question_issue_map(qset)
    keys = set()
    for t in packet.tossup_set.values_list('id', flat=True):
        keys.add('tossup-{0}'.format(t))
    for b in packet.bonus_set.values_list('id', flat=True):
        keys.add('bonus-{0}'.format(b))
    issues = {k: v for k, v in full.items() if k in keys}
    return HttpResponse(json.dumps({'issues': issues}))


@login_required
def packet_style_issues(request, packet_id):
    """JSON map of style-check issues (per question) for a packet, for the
    document-view "show style issues" toggle. Style guide via ?guide=."""
    from . import style_checker
    user = request.user.writer
    packet = Packet.objects.get(id=packet_id)
    qset = packet.question_set
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return HttpResponse(json.dumps({'issues': {}}))
    guide = request.GET.get('guide', style_checker.DEFAULT_GUIDE)
    if guide not in style_checker.guide_keys():
        guide = style_checker.DEFAULT_GUIDE
    disabled = qset.disabled_style_rule_set()
    issues = {}
    for tu in packet.tossup_set.all():
        found = style_checker.check_tossup(tu, guide, disabled)
        if found:
            issues['tossup-{0}'.format(tu.id)] = found
    for b in packet.bonus_set.all():
        found = style_checker.check_bonus(b, guide, disabled)
        if found:
            issues['bonus-{0}'.format(b.id)] = found
    return HttpResponse(json.dumps({'issues': issues, 'guide': guide}))


def _is_ai_user(user):
    """AI-assisted features are gated to superusers (was the literal 'admin'
    username, which broke if that account was renamed)."""
    return user.is_authenticated and user.is_superuser


def _clean_for_ai(text):
    """Strip QEMS markup and decode entities so the AI sees readable prose."""
    return re.sub(r'\\P|[_~]', '', html.unescape(text or '')).strip()


def _ai_answer_line(text):
    """Decode entities but KEEP the QEMS markup (underscores/tildes) so the AI
    can see which words are required and mirror the set's answer-line format."""
    return html.unescape(text or '').strip()


def _ai_grammar_ref_meta(qset):
    """Return {ref: {'edit_url', 'label'}} for every question in a set, keyed by
    the 'tossup-<id>' / 'bonus-<id>' refs the grammar checker uses."""
    refs = {}
    for tu in qset.tossup_set.all():
        refs['tossup-{0}'.format(tu.id)] = {
            'edit_url': '/edit_tossup/{0}/'.format(tu.id),
            'label': _grid_answer_preview(tu.tossup_answer)}
    for b in qset.bonus_set.all():
        refs['bonus-{0}'.format(b.id)] = {
            'edit_url': '/edit_bonus/{0}/'.format(b.id),
            'label': _grid_answer_preview(b.part1_answer, 30)}
    return refs


def _ai_grammar_findings_json(qset):
    """Serialize a set's persisted AI grammar findings for the template/JS,
    attaching each question's edit link and answer label."""
    found = list(qset.ai_grammar_findings.all())
    if not found:
        return []
    refs = _ai_grammar_ref_meta(qset)
    out = []
    for f in found:
        ref = '{0}-{1}'.format(f.question_type, f.question_id)
        meta = refs.get(ref, {})
        entry = {'id': f.id, 'kind': f.kind, 'severity': f.severity, 'excerpt': f.excerpt,
                 'suggestion': f.suggestion, 'explanation': f.explanation,
                 'edit_url': meta.get('edit_url', ''),
                 'label': meta.get('label', ref)}
        if f.kind == 'answer':
            # Render the suggestion's answer-line markup (_required_, ~titles~)
            # the same way answer lines render elsewhere. Escaped first: the
            # suggestion is model output, only the markup tags may be HTML.
            entry['suggestion_html'] = get_formatted_question_html(
                html.escape(f.suggestion or ''), True, False, False, False)
        out.append(entry)
    return out


@login_required
def ai_grammar_check(request, qset_id):
    """Admin-only AI grammar/spelling/error pass over a set's questions. Checks
    the whole set (batched), replaces any prior findings, persists the new ones
    so they survive a reload, and returns them as JSON."""
    from . import ai
    if not _is_ai_user(request.user):
        return HttpResponse(json.dumps({'ok': False, 'message': 'Not authorized.'}), status=403)
    if not ai.ai_enabled():
        return HttpResponse(json.dumps({'ok': False, 'message': 'AI features are not configured.'}))
    user = request.user.writer
    try:
        qset = QuestionSet.objects.get(id=qset_id)
    except QuestionSet.DoesNotExist:
        return HttpResponse(json.dumps({'ok': False, 'message': 'Set not found.'}))

    # `items` (markup stripped) feeds the grammar pass; `answer_items` keeps the
    # answer-line markup so the alternate-answer pass sees what's underlined and
    # can format its suggestions the same way.
    items, answer_items = [], []
    for tu in qset.tossup_set.select_related('packet').order_by('packet__packet_name', 'question_number'):
        ref = 'tossup-{0}'.format(tu.id)
        question = _clean_for_ai(tu.tossup_text)
        items.append({'ref': ref, 'text': question + '\nANSWER: ' + _clean_for_ai(tu.tossup_answer)})
        answer_items.append({'ref': ref, 'text': question + '\nANSWER: ' + _ai_answer_line(tu.tossup_answer)})
    for b in qset.bonus_set.select_related('packet').order_by('packet__packet_name', 'question_number'):
        ref = 'bonus-{0}'.format(b.id)
        for dest, answer in ((items, _clean_for_ai), (answer_items, _ai_answer_line)):
            parts = [_clean_for_ai(b.leadin),
                     _clean_for_ai(b.part1_text), 'ANSWER: ' + answer(b.part1_answer),
                     _clean_for_ai(b.part2_text), 'ANSWER: ' + answer(b.part2_answer),
                     _clean_for_ai(b.part3_text), 'ANSWER: ' + answer(b.part3_answer)]
            dest.append({'ref': ref, 'text': '\n'.join(p for p in parts if p.strip())})

    findings, error = ai.grammar_check_questions(items, answer_items=answer_items)
    if error:
        return HttpResponse(json.dumps({'ok': False, 'message': error}))

    # Rerun replaces the whole set's findings. Persist the fresh ones, mapping
    # each back to its question via the ref label.
    with transaction.atomic():
        qset.ai_grammar_findings.all().delete()
        for f in findings:
            ref = f.get('ref', '')
            if '-' not in ref:
                continue
            qtype, _, qid = ref.partition('-')
            if qtype not in ('tossup', 'bonus') or not qid.isdigit():
                continue
            AIGrammarFinding.objects.create(
                question_set=qset, question_type=qtype, question_id=int(qid),
                kind=f.get('kind', 'grammar'),
                severity=f.get('severity', 'warning'), excerpt=f.get('excerpt', ''),
                suggestion=f.get('suggestion', ''), explanation=f.get('explanation', ''),
                created_by=user)

    out = _ai_grammar_findings_json(qset)
    return HttpResponse(json.dumps({'ok': True, 'findings': out,
                                    'checked': len(items)}))


def _ai_tags_available(user):
    """Whether to offer the AI tag suggester at all: admin, and a key set."""
    from . import ai
    return _is_ai_user(user) and ai.ai_enabled()


def _ai_tag_question_text(q, qtype):
    """One question as the tag suggester reads it: the prose and every answer
    line, markup stripped."""
    if qtype == 'tossup':
        parts = [_clean_for_ai(q.tossup_text), 'ANSWER: ' + _clean_for_ai(q.tossup_answer)]
    else:
        parts = [_clean_for_ai(q.leadin),
                 _clean_for_ai(q.part1_text), 'ANSWER: ' + _clean_for_ai(q.part1_answer),
                 _clean_for_ai(q.part2_text), 'ANSWER: ' + _clean_for_ai(q.part2_answer),
                 _clean_for_ai(q.part3_text), 'ANSWER: ' + _clean_for_ai(q.part3_answer)]
    return '\n'.join(p for p in parts if p.strip())


def _ai_tag_description(tag):
    """What a tag is for, as far as the set records it: its group (the axis it
    runs along) and its quota. Both are what an editor reads to decide whether
    a tag fits, so the model gets them too."""
    bits = []
    if tag.group_name:
        bits.append('group: {0}'.format(tag.group_name))
    quota = []
    if tag.num_tossups:
        quota.append('{0}+ tossups'.format(tag.num_tossups))
    if tag.num_bonuses:
        quota.append('{0}+ bonuses'.format(tag.num_bonuses))
    if tag.num_questions:
        quota.append('{0}+ questions'.format(tag.num_questions))
    if quota:
        bits.append('needs ' + ', '.join(quota))
    return '; '.join(bits)


@login_required
def ai_suggest_tags(request, qset_id):
    """Admin-only: ask Claude which of a category's tags each of its questions
    should carry. Proposals only -- they are persisted and shown on the rows,
    and an editor accepts or dismisses each one. A rerun replaces the previous
    proposals for the same category."""
    from . import ai
    if request.method != 'POST':
        return HttpResponse(json.dumps({'ok': False, 'message': 'POST required'}), status=405)
    if not _is_ai_user(request.user):
        return HttpResponse(json.dumps({'ok': False, 'message': 'Not authorized.'}), status=403)
    if not ai.ai_enabled():
        return HttpResponse(json.dumps({'ok': False, 'message': 'AI features are not configured.'}))
    user = request.user.writer
    try:
        qset = QuestionSet.objects.get(id=qset_id)
    except QuestionSet.DoesNotExist:
        return HttpResponse(json.dumps({'ok': False, 'message': 'Set not found.'}))
    if not (qset.is_owner(user) or user in qset.editor.all()):
        return HttpResponse(json.dumps({'ok': False, 'message': 'Only editors can tag questions.'}),
                            status=403)

    raw_focus = (request.POST.get('category') or '').strip()
    if not raw_focus:
        return HttpResponse(json.dumps({'ok': False,
                                        'message': 'Open a category first — tags are suggested '
                                                   'one category at a time.'}))
    path = scope_from_token(raw_focus)

    # Exactly the tags this page can assign, so every proposal is one the
    # editor can accept with the button next to it.
    tags = list(CategoryTag.objects.filter(question_set=qset, category_path=path))
    if not tags:
        return HttpResponse(json.dumps({'ok': False,
                                        'message': 'This category has no tags to choose from yet.'}))
    by_name = {t.name.strip().lower(): t for t in tags}

    # The same questions the page lists under this category.
    category_ids = None
    if (path or '').strip():
        category_ids = [e.id for e in DistributionEntry.objects.filter(
            distribution_id=qset.distribution_id)
            if str(e) == path or str(e).startswith(path + ' - ')]

    items, questions = [], {}
    for model, qtype in ((Tossup, 'tossup'), (Bonus, 'bonus')):
        qs = model.objects.filter(question_set=qset).prefetch_related('category_tags')
        if category_ids is not None:
            qs = qs.filter(category_id__in=category_ids)
        for q in qs:
            ref = '{0}-{1}'.format(qtype, q.id)
            questions[ref] = q
            have = sorted(t.name for t in q.category_tags.all() if t.question_set_id == qset.id)
            text = _ai_tag_question_text(q, qtype)
            if have:
                text += '\nAlready tagged: ' + ', '.join(have)
            items.append({'ref': ref, 'text': text})

    if not items:
        return HttpResponse(json.dumps({'ok': False, 'message': 'No questions in this category.'}))

    assignments, error = ai.suggest_category_tags(
        items, [{'name': t.name, 'description': _ai_tag_description(t)} for t in tags])

    kept = 0
    with transaction.atomic():
        AITagSuggestion.objects.filter(question_set=qset, tag__in=tags).delete()
        for a in assignments:
            ref = a.get('ref', '')
            tag = by_name.get((a.get('tag') or '').strip().lower())
            q = questions.get(ref)
            # A name the set doesn't define, a ref for a question that wasn't in
            # the batch, or a tag the question already carries: all dropped
            # rather than shown. The model's output is checked, not trusted.
            if tag is None or q is None:
                continue
            if any(t.id == tag.id for t in q.category_tags.all()):
                continue
            qtype, _, qid = ref.partition('-')
            _, made = AITagSuggestion.objects.get_or_create(
                question_set=qset, question_type=qtype, question_id=int(qid), tag=tag,
                defaults={'confidence': a.get('confidence', 'medium'),
                          'explanation': a.get('explanation', ''),
                          'created_by': user})
            kept += 1 if made else 0

    if error:
        return HttpResponse(json.dumps({'ok': False, 'message': error, 'suggested': kept}))
    return HttpResponse(json.dumps({'ok': True, 'suggested': kept, 'checked': len(items)}))


@login_required
def dismiss_ai_grammar_finding(request):
    """Delete a single persisted AI grammar finding (admin only)."""
    if request.method != 'POST':
        return HttpResponse(json.dumps({'ok': False, 'error': 'POST required'}), status=405)
    if not _is_ai_user(request.user):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Not authorized'}), status=403)
    fid = request.POST.get('finding_id', '')
    AIGrammarFinding.objects.filter(id=fid).delete()
    return HttpResponse(json.dumps({'ok': True}))


def _style_question(qtype, qid):
    if qtype == 'tossup':
        return Tossup.objects.filter(id=qid).first()
    if qtype == 'bonus':
        return Bonus.objects.filter(id=qid).first()
    return None


def _can_edit_question(user, qset, question):
    """Edit rights for applying a fix: owners/editors can always edit; a writer
    can edit their own question while it is unlocked."""
    if qset.is_owner(user) or user in qset.editor.all():
        return True
    return (question.author_id == user.id) and not question.locked


def _record_suggestion_verdict(code, token, accepted):
    """Count one editor's verdict on one suggestion.

    Nothing about who, which question or which set is stored -- see
    SuggestionFeedback. Failures are swallowed: a bookkeeping row is never a
    reason to fail the edit the writer actually asked for.
    """
    from django.db.models import F
    if not code:
        return
    try:
        obj, created = SuggestionFeedback.objects.get_or_create(
            code=code, token=token or '',
            defaults={'accepted': 1 if accepted else 0,
                      'rejected': 0 if accepted else 1})
        if not created:
            field = 'accepted' if accepted else 'rejected'
            SuggestionFeedback.objects.filter(pk=obj.pk).update(
                **{field: F(field) + 1, 'last_action_date': timezone.now()})
    except Exception:
        print('Could not record suggestion verdict:', sys.exc_info()[0], sys.exc_info()[1])


def _unrecord_suggestion_rejection(code, token):
    """Undo a rejection when a dismissal is restored, so an editor changing
    their mind doesn't leave the suggestion looking worse than it is."""
    from django.db.models import F
    try:
        SuggestionFeedback.objects.filter(code=code, token=token or '', rejected__gt=0).update(
            rejected=F('rejected') - 1, last_action_date=timezone.now())
    except Exception:
        print('Could not unrecord suggestion verdict:', sys.exc_info()[0], sys.exc_info()[1])


@login_required
def apply_style_fix(request):
    """Auto-apply an easy style fix (e.g. insert a missing pronunciation guide)
    to a question. The fix transform is recomputed server-side from the issue's
    (code, token); the client only identifies which issue to fix."""
    from . import style_checker
    if request.method != 'POST':
        return HttpResponse(json.dumps({'ok': False, 'error': 'POST required'}), status=405)
    user = request.user.writer
    qtype = request.POST.get('question_type', '')
    qid = request.POST.get('question_id', '')
    code = request.POST.get('code', '')
    token = request.POST.get('token', '')
    guide = request.POST.get('guide', style_checker.DEFAULT_GUIDE)
    if guide not in style_checker.guide_keys():
        guide = style_checker.DEFAULT_GUIDE

    question = _style_question(qtype, qid)
    if question is None:
        return HttpResponse(json.dumps({'ok': False, 'error': 'No such question'}), status=404)
    qset = question.question_set
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Not authorized'}), status=403)
    if not _can_edit_question(user, qset, question):
        return HttpResponse(json.dumps({'ok': False, 'error': 'This question is locked'}), status=403)

    fix = style_checker.find_fix(question, qtype, code, token, guide)
    if not fix:
        return HttpResponse(json.dumps({'ok': False, 'error': 'Nothing to apply'}), status=400)
    old_text = getattr(question, fix.get('field', ''), '') or ''
    if not style_checker.apply_fix(question, fix):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Could not apply automatically'}), status=400)
    question.save_question(edit_type=QUESTION_EDIT, changer=user)
    _record_suggestion_verdict(code, token, accepted=True)
    # The edit page uses these to update the field in place instead of
    # reloading (a reload on a page rendered from a POST resubmits the stale
    # form and overwrites the fix).
    return HttpResponse(json.dumps({'ok': True, 'field': fix.get('field', ''),
                                    'old_text': old_text,
                                    'text': getattr(question, fix['field'], '') or ''}))


@login_required
def dismiss_style_issue(request):
    """Dismiss (or restore) a style-check issue for a question so it is hidden
    on future runs. Set-wide. POST action=restore to undo."""
    if request.method != 'POST':
        return HttpResponse(json.dumps({'ok': False, 'error': 'POST required'}), status=405)
    user = request.user.writer
    qtype = request.POST.get('question_type', '')
    qid = request.POST.get('question_id', '')
    code = request.POST.get('code', '')
    token = request.POST.get('token', '')
    restore = request.POST.get('action', '') == 'restore'

    question = _style_question(qtype, qid)
    if question is None:
        return HttpResponse(json.dumps({'ok': False, 'error': 'No such question'}), status=404)
    qset = question.question_set
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Not authorized'}), status=403)

    scope_all = request.POST.get('scope', '') == 'all'

    if scope_all:
        # Set-wide: hide every example of this suggestion across the set.
        if restore:
            StyleRuleDismissal.objects.filter(
                question_set=qset, code=code, token=token).delete()
        else:
            StyleRuleDismissal.objects.get_or_create(
                question_set=qset, code=code, token=token,
                defaults={'dismissed_by': user})
    elif restore:
        StyleIssueDismissal.objects.filter(
            question_type=qtype, question_id=question.id, code=code, token=token).delete()
    else:
        StyleIssueDismissal.objects.get_or_create(
            question_type=qtype, question_id=question.id, code=code, token=token,
            defaults={'question_set': qset, 'dismissed_by': user})

    # One verdict per act of dismissing, whether it covered this question or
    # the whole set: both are one editor deciding the suggestion is wrong.
    if restore:
        _unrecord_suggestion_rejection(code, token)
    else:
        _record_suggestion_verdict(code, token, accepted=False)
    # The description says what was actually silenced (one term's guide, not the
    # whole pronunciation rule), so the page can report it rather than leaving
    # the editor to guess how wide the dismissal went.
    from . import style_checker
    return HttpResponse(json.dumps({
        'ok': True,
        'scope': 'set' if scope_all else 'question',
        'description': style_checker.describe_dismissal(code, token)['summary'],
        'set_wide_count': StyleRuleDismissal.objects.filter(question_set=qset).count(),
    }))


@login_required
def restore_style_dismissal(request):
    """Un-ignore a style suggestion from the Ignored page.

    `dismiss_style_issue` authorizes through the question a suggestion was found
    on, which a set-wide dismissal doesn't have (and the question it was made on
    may since have been deleted). This one authorizes on the set itself, so the
    Ignored page can always take something back.
    """
    if request.method != 'POST':
        return HttpResponse(json.dumps({'ok': False, 'error': 'POST required'}), status=405)
    user = request.user.writer
    try:
        qset = QuestionSet.objects.get(id=int(request.POST.get('qset_id', '')))
    except (ValueError, QuestionSet.DoesNotExist):
        return HttpResponse(json.dumps({'ok': False, 'error': 'No such set'}), status=404)
    if not (qset.is_owner(user) or user in qset.editor.all()):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Not authorized'}), status=403)

    code = request.POST.get('code', '')
    token = request.POST.get('token', '')
    if request.POST.get('scope', '') == 'all':
        StyleRuleDismissal.objects.filter(
            question_set=qset, code=code, token=token).delete()
    else:
        qtype = request.POST.get('question_type', '')
        qid = request.POST.get('question_id', '')
        if not str(qid).isdigit():
            return HttpResponse(json.dumps({'ok': False, 'error': 'No such question'}), status=404)
        StyleIssueDismissal.objects.filter(
            question_set=qset, question_type=qtype, question_id=int(qid),
            code=code, token=token).delete()
    _unrecord_suggestion_rejection(code, token)
    return HttpResponse(json.dumps({'ok': True}))


@login_required
def style_ignored(request, qset_id):
    """Everything this set has silenced in the style checker: rules switched off
    outright, suggestions ignored set-wide, and the per-question dismissals.

    Without this the only record of a dismissal was its absence — a suggestion
    simply stopped appearing, with no way to see what had been silenced or to
    take it back except by finding the question it came from.
    """
    from . import style_checker
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return render(request, 'failure.html',
                      {'message': 'You are not authorized to view this set!',
                       'message_class': 'alert-box alert'})

    guide = request.GET.get('guide', style_checker.DEFAULT_GUIDE)
    if guide not in style_checker.guide_keys():
        guide = style_checker.DEFAULT_GUIDE

    disabled = qset.disabled_style_rule_set()
    off_rules = [{'code': code, 'label': label}
                 for code, label in style_checker.configurable_rules(guide)
                 if code in disabled]

    set_wide = []
    for d in (StyleRuleDismissal.objects.filter(question_set=qset)
              .select_related('dismissed_by__user').order_by('-dismissed_date')):
        info = style_checker.describe_dismissal(d.code, d.token)
        set_wide.append({'code': d.code, 'token': d.token, 'by': d.dismissed_by,
                         'date': d.dismissed_date, **info})

    # Per-question dismissals, grouped by the question they were made on so the
    # list reads as "this question, these suggestions".
    per_question = []
    dismissals = list(StyleIssueDismissal.objects.filter(question_set=qset)
                      .select_related('dismissed_by__user').order_by('-dismissed_date'))
    tu_ids = [d.question_id for d in dismissals if d.question_type == 'tossup']
    bs_ids = [d.question_id for d in dismissals if d.question_type == 'bonus']
    tossups = {t.id: t for t in Tossup.objects.filter(id__in=tu_ids)}
    bonuses = {b.id: b for b in Bonus.objects.filter(id__in=bs_ids)}
    by_question = {}
    for d in dismissals:
        question = (tossups if d.question_type == 'tossup' else bonuses).get(d.question_id)
        if question is None:
            continue        # the question was deleted; its dismissals are moot
        key = (d.question_type, d.question_id)
        if key not in by_question:
            answer = (question.tossup_answer if d.question_type == 'tossup'
                      else question.part1_answer)
            by_question[key] = {
                'qtype': d.question_type, 'qid': d.question_id,
                'label': _grid_answer_preview(answer, 60),
                'edit_url': '/edit_{0}/{1}/'.format(d.question_type, d.question_id),
                'items': []}
        by_question[key]['items'].append(
            {'code': d.code, 'token': d.token, 'by': d.dismissed_by,
             'date': d.dismissed_date, **style_checker.describe_dismissal(d.code, d.token)})
    per_question = list(by_question.values())

    return render(request, 'style_ignored.html',
                  {'qset': qset, 'user': user, 'guide': guide,
                   'off_rules': off_rules, 'set_wide': set_wide,
                   'per_question': per_question,
                   'per_question_count': sum(len(q['items']) for q in per_question),
                   'extra_crumb': 'Ignored',
                   'extra_crumb_url': '/style_ignored/{0}/'.format(qset.id),
                   'read_only': not (qset.is_owner(user) or user in qset.editor.all())})


@login_required
def question_style_issues(request):
    """JSON style-check issues for a single question — the panel on the edit
    tossup/bonus pages. Runs the same rules as /style_check/ (default guide),
    honoring the set's disabled rules and both per-question and set-wide
    dismissals."""
    from . import style_checker
    user = request.user.writer
    qtype = request.GET.get('question_type', '')
    qid = request.GET.get('question_id', '')
    if not qid.isdigit():
        return HttpResponse(json.dumps({'ok': False, 'error': 'No such question'}), status=404)
    question = _style_question(qtype, qid)
    if question is None:
        return HttpResponse(json.dumps({'ok': False, 'error': 'No such question'}), status=404)
    qset = question.question_set
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Not authorized'}), status=403)

    guide = request.GET.get('guide', style_checker.DEFAULT_GUIDE)
    if guide not in style_checker.guide_keys():
        guide = style_checker.DEFAULT_GUIDE
    disabled = qset.disabled_style_rule_set()
    found = (style_checker.check_tossup(question, guide, disabled) if qtype == 'tossup'
             else style_checker.check_bonus(question, guide, disabled))

    dismissed = set(StyleIssueDismissal.objects.filter(
        question_type=qtype, question_id=question.id).values_list('code', 'token'))
    rule_dismissed = set(StyleRuleDismissal.objects.filter(
        question_set=qset).values_list('code', 'token'))

    # The style-check page re-renders one question in place after an action, and
    # it can be showing dismissed issues — so it asks for them, flagged, rather
    # than having them silently disappear from a list that was displaying them.
    include_dismissed = request.GET.get('include_dismissed') == '1'

    issues = []
    for i in found:
        key = (i['code'], i.get('token', ''))
        is_dismissed = key in dismissed or key in rule_dismissed
        if is_dismissed and not include_dismissed:
            continue
        issues.append({'severity': i['severity'], 'message': i['message'],
                       'message_html': i.get('message_html', ''),
                       'code': i['code'], 'token': i.get('token', ''),
                       'fixable': 'fix' in i,
                       'dismissed': is_dismissed,
                       'dismissed_scope': 'set' if key in rule_dismissed else (
                           'question' if is_dismissed else '')})
    return HttpResponse(json.dumps({'ok': True, 'issues': issues, 'guide': guide,
                                    'qset_id': qset.id}))


QBREADER_QUERY_URL = 'https://www.qbreader.org/api/query'
QBREADER_DB_URL = 'https://www.qbreader.org/db/'


@login_required
def qbreader_freq(request):
    """How often a phrase appears in the qbreader question database.

    The edit pages' highlight-to-search popup asks here rather than qbreader
    directly (no CORS, no browser-side coupling to their API), with the same
    exact-phrase reading as the db page's checkbox. Responses are cached for a
    day: what a writer highlights is usually highlighted again moments later
    on another question, and frequencies move slowly.
    """
    import urllib.request
    from django.core.cache import cache as django_cache
    term = (request.GET.get('q') or '').strip()
    if not (3 <= len(term) <= 120):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Highlight 3-120 characters'}),
                            status=400)
    db_url = '{0}?{1}'.format(QBREADER_DB_URL, urllib.parse.urlencode(
        {'q': term, 'exactPhrase': 'true'}))
    key = 'qbfreq:' + term.lower()
    hit = django_cache.get(key)
    if hit is not None:
        hit = dict(hit, url=db_url)
        return HttpResponse(json.dumps(hit))
    api = '{0}?{1}'.format(QBREADER_QUERY_URL, urllib.parse.urlencode(
        {'queryString': term, 'exactPhrase': 'true', 'maxReturnLength': 1}))
    try:
        with urllib.request.urlopen(api, timeout=8) as r:
            data = json.load(r)
        result = {'ok': True,
                  'tossups': int(data['tossups']['count']),
                  'bonuses': int(data['bonuses']['count'])}
    except Exception:
        print('qbreader lookup failed:', sys.exc_info()[0], sys.exc_info()[1])
        return HttpResponse(json.dumps({'ok': False, 'error': 'qbreader did not answer',
                                        'url': db_url}), status=502)
    django_cache.set(key, result, 24 * 60 * 60)
    return HttpResponse(json.dumps(dict(result, url=db_url)))


_DRAFT_FIELDS = {
    'tossup': ('tossup_text', 'tossup_answer'),
    'bonus': ('leadin', 'part1_text', 'part1_answer', 'part2_text', 'part2_answer',
              'part3_text', 'part3_answer'),
}


@login_required
def draft_style_issues(request):
    """JSON style-check issues for text that hasn't been saved: the "Style
    check" button on the add/edit pages posts whatever is in the editor.
    Same rules as /question_style_issues/, run on a stand-in object built from
    the posted fields, so a writer can tidy a question before submitting it.
    Nothing is stored, so no fix can be applied from here — `fixable` only
    says one would exist once the question is saved; dismissals are still
    honored (they key on the issue, not the saved text)."""
    from types import SimpleNamespace
    from . import style_checker
    if request.method != 'POST':
        return HttpResponse(json.dumps({'ok': False, 'error': 'POST required'}), status=405)
    user = request.user.writer
    qtype = request.POST.get('question_type', '')
    if qtype not in _DRAFT_FIELDS:
        return HttpResponse(json.dumps({'ok': False, 'error': 'No such question type'}), status=400)
    qset_id = request.POST.get('qset_id', '')
    qset = QuestionSet.objects.filter(id=qset_id).first() if qset_id.isdigit() else None
    if qset is None:
        return HttpResponse(json.dumps({'ok': False, 'error': 'No such question set'}), status=404)
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Not authorized'}), status=403)

    draft = SimpleNamespace(**{f: request.POST.get(f, '') for f in _DRAFT_FIELDS[qtype]})
    draft.guides_require_quotes = lambda: bool(qset.guides_require_quotes)

    guide = request.POST.get('guide', style_checker.DEFAULT_GUIDE)
    if guide not in style_checker.guide_keys():
        guide = style_checker.DEFAULT_GUIDE
    disabled = qset.disabled_style_rule_set()
    found = (style_checker.check_tossup(draft, guide, disabled) if qtype == 'tossup'
             else style_checker.check_bonus(draft, guide, disabled))

    # On an edit page the draft belongs to a saved question, whose own
    # dismissals apply; on an add page there is no question yet.
    qid = request.POST.get('question_id', '')
    dismissed = set()
    if qid.isdigit():
        dismissed = set(StyleIssueDismissal.objects.filter(
            question_type=qtype, question_id=int(qid)).values_list('code', 'token'))
    dismissed |= set(StyleRuleDismissal.objects.filter(
        question_set=qset).values_list('code', 'token'))

    issues = [{'severity': i['severity'], 'message': i['message'],
               'message_html': i.get('message_html', ''),
               'code': i['code'], 'token': i.get('token', ''),
               'fixable': 'fix' in i, 'dismissed': False, 'dismissed_scope': ''}
              for i in found if (i['code'], i.get('token', '')) not in dismissed]
    return HttpResponse(json.dumps({'ok': True, 'issues': issues, 'guide': guide,
                                    'qset_id': qset.id}))


@login_required
def grammar_texts(request, qset_id):
    """JSON dump of a set's question prose for the client-side Harper grammar
    check on /style_check/. The linting itself happens in the browser (WASM);
    this just supplies the text. Answer lines are omitted — they aren't prose."""
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return HttpResponse(json.dumps({'ok': False, 'error': 'Not authorized'}), status=403)

    questions = []
    for tu in qset.tossup_set.select_related('packet').order_by('packet__packet_name', 'question_number'):
        questions.append({'qtype': 'tossup', 'id': tu.id,
                          'label': _grid_answer_preview(tu.tossup_answer),
                          'edit_url': '/edit_tossup/{0}/'.format(tu.id),
                          'fields': [{'name': 'Text', 'text': tu.tossup_text or ''}]})
    for b in qset.bonus_set.select_related('packet').order_by('packet__packet_name', 'question_number'):
        fields = [{'name': 'Leadin', 'text': b.leadin or ''}]
        for n in (1, 2, 3):
            fields.append({'name': 'Part {0}'.format(n),
                           'text': getattr(b, 'part{0}_text'.format(n)) or ''})
        questions.append({'qtype': 'bonus', 'id': b.id,
                          'label': _grid_answer_preview(b.part1_answer, 30),
                          'edit_url': '/edit_bonus/{0}/'.format(b.id),
                          'fields': fields})
    return HttpResponse(json.dumps({'ok': True, 'questions': questions}))


@login_required
def live_question_counts(request):
    """Character counts for the questions being typed into the bulk entry box.

    The box holds a whole run of questions, so one number for the lot would be
    meaningless: this reads the text the way the parser will (`packet_parser.
    outline`), works out for each question whether it is a tossup or a bonus,
    and counts it by that type's rules against that type's limit — a tossup by
    its stem, a bonus by its leadin plus its parts.
    """
    from .packet_parser import outline
    from .utils import get_character_count
    from .answer_structure import plain

    if request.method != 'POST':
        return HttpResponse(json.dumps({'questions': []}),
                            content_type='application/json')

    ignore_guides, quoted = True, False
    tossup_max = bonus_max = 0
    try:
        qset = QuestionSet.objects.get(id=int(request.POST.get('qset_id', '')))
        ignore_guides = qset.char_count_ignores_pronunciation_guides
        quoted = qset.guides_require_quotes
        tossup_max = qset.max_acf_tossup_length
        bonus_max = qset.max_acf_bonus_length
    except (ValueError, QuestionSet.DoesNotExist):
        pass

    text = request.POST.get('text', '') or ''
    questions = []
    tossups = bonuses = over = 0
    for index, q in enumerate(outline(text.split('\n')), start=1):
        is_tossup = q['kind'] == 'tossup'
        limit = tossup_max if is_tossup else bonus_max
        count = sum(get_character_count(t, ignore_guides, quoted) for t in q['text'])
        if is_tossup:
            tossups += 1
        else:
            bonuses += 1
        if limit and count > limit:
            over += 1
        # The answer is the useful label — it's how a writer knows which
        # question a row is about — with the markup taken off.
        answer = plain(re.sub(r'(?i)^a..?wers?:\s*', '', q['answer'] or '')).strip()
        questions.append({
            'number': index,
            'kind': q['kind'],
            'label': '{0} {1}'.format('Tossup' if is_tossup else 'Bonus', index),
            'answer': answer[:60],
            'count': count,
            'max': limit,
            'over': bool(limit and count > limit),
            'complete': q['complete'],
        })

    return HttpResponse(json.dumps({
        'questions': questions,
        'tossups': tossups,
        'bonuses': bonuses,
        'over': over,
    }), content_type='application/json')


@login_required
def live_char_count(request):
    """Character count for in-progress edit text, using the set's counting
    rules (pronunciation guides / moderator instructions excluded as configured).
    Body: qset_id and one or more text[] fields (summed, like the model does)."""
    from .utils import get_character_count
    if request.method != 'POST':
        return HttpResponse(json.dumps({'count': 0}))
    ignore = True
    quoted = False
    try:
        qset = QuestionSet.objects.get(id=int(request.POST['qset_id']))
        ignore = qset.char_count_ignores_pronunciation_guides
        quoted = qset.guides_require_quotes
    except (KeyError, ValueError, QuestionSet.DoesNotExist):
        pass
    texts = request.POST.getlist('text[]')
    if not texts:
        texts = [request.POST.get('text', '')]
    total = sum(get_character_count(t, ignore, quoted) for t in texts)
    return HttpResponse(json.dumps({'count': total}))


#########################################################################
# Category tags: editor-defined sub-distribution requirements
#########################################################################

# In a form or a URL a category has to be named, and "" already means "none
# chosen", so a set-wide tag travels under this token and is stored as "".
SET_WIDE_TOKEN = '__SET__'
SET_WIDE_LABEL = 'Whole set (every category)'


def scope_from_token(value):
    """Form/query value -> stored category_path ('' for a set-wide tag)."""
    value = (value or '').strip()
    return '' if value == SET_WIDE_TOKEN else value


def scope_token(path):
    """Stored category_path -> the token a link or form field should carry."""
    return SET_WIDE_TOKEN if not (path or '').strip() else path


def scope_label(path):
    return SET_WIDE_LABEL if not (path or '').strip() else path


def _tag_matches_path(tag_path, question_path):
    """A tag applies to a question when one path is a segment-wise prefix of
    the other: a 'Literature - World' tag covers questions in
    'Literature - World - Drama', and a tag on a deeper path than the
    question's leaf still applies to that leaf.

    A tag with no path at all is set-wide and applies to everything."""
    if not (tag_path or '').strip():
        return True
    return (question_path == tag_path or
            question_path.startswith(tag_path + ' - ') or
            tag_path.startswith(question_path + ' - '))

def get_applicable_tags(qset, dist_entry):
    """Tags of the set that apply to a question in the given category.

    With no category yet (a question being written before one is picked), the
    set-wide tags still apply -- they are exactly the ones that do not depend
    on the category."""
    if dist_entry is None:
        return [tag for tag in CategoryTag.objects.filter(question_set=qset,
                                                          category_path='')]
    path = str(dist_entry)
    return [tag for tag in CategoryTag.objects.filter(question_set=qset)
            if _tag_matches_path(tag.category_path, path)]

@login_required
def tags_for_category(request, qset_id):
    """The tag checkboxes for one category, as the HTML the edit pages use.

    The add-a-question pages don't know the category when they are built -- it
    is chosen on the page -- so they fetch this when the picker changes. Same
    partial as the edit pages, so a tag looks and posts the same wherever it is
    ticked.
    """
    from django.template.loader import render_to_string
    user = request.user.writer
    try:
        qset = QuestionSet.objects.get(id=int(qset_id))
    except (ValueError, QuestionSet.DoesNotExist):
        return HttpResponse(json.dumps({'html': ''}), content_type='application/json')
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return HttpResponse(json.dumps({'html': ''}), content_type='application/json')

    entry = None
    raw = (request.GET.get('category') or '').strip()
    if raw.isdigit():
        # Only a category of this set's own distribution: the id arrives from a
        # form field, so it is the caller's word for it, not to be trusted.
        entry = DistributionEntry.objects.filter(
            id=int(raw), distribution_id=qset.distribution_id).first()
    if entry is None:
        return HttpResponse(json.dumps({'html': ''}), content_type='application/json')

    # The question isn't written yet, so say what each tag still wants --
    # that is what decides what gets written.
    html = render_to_string('question_tags.html',
                            {'available_tags': build_tag_checkboxes(qset, None, entry),
                             'show_remaining': True},
                            request=request)
    return HttpResponse(json.dumps({'html': html}), content_type='application/json')


@login_required
def export_category_tags(request, qset_id):
    """The set's category tags as a .csv, for editing or for another set."""
    from . import tag_importer
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return render(request, 'failure.html',
                      {'message': 'You are not authorized to view this set!',
                       'message_class': 'alert-box alert'})
    resp = HttpResponse(tag_importer.export_csv(qset), content_type='text/csv; charset=utf-8')
    safe = re.sub(r'[^A-Za-z0-9_-]+', '_', qset.name).strip('_') or 'set'
    resp['Content-Disposition'] = 'attachment; filename="{0}_category_tags.csv"'.format(safe)
    return resp


@login_required
def import_category_tags(request, qset_id):
    """Create or update the set's tags from an uploaded sheet (the export
    layout), then return to the tags page."""
    from . import tag_importer
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    back = '/category_tags/{0}/'.format(qset.id)
    if not (qset.is_owner(user) or user in qset.editor.all()):
        messages.error(request, 'Only editors can import tags.')
        return HttpResponseRedirect(back)
    if request.method != 'POST':
        return HttpResponseRedirect(back)
    upload = request.FILES.get('sheet')
    if upload is None:
        messages.error(request, 'Choose a file to import.')
        return HttpResponseRedirect(back)
    try:
        entries = tag_importer.parse_tag_sheet(upload.name, upload.read())
    except tag_importer.DistributionImportError as ex:
        messages.error(request, str(ex))
        return HttpResponseRedirect(back)
    known = [row['path'] for row in get_packetization_rows(qset)]
    created, updated, skipped = tag_importer.import_tags(qset, entries, known)
    note = 'Imported tags from {0}: {1} created, {2} updated.'.format(upload.name, created, updated)
    if skipped:
        note += ' Skipped {0} categor{1} this set does not have: {2}.'.format(
            len(skipped), 'y' if len(skipped) == 1 else 'ies', ', '.join(skipped[:8]) + (' ...' if len(skipped) > 8 else ''))
        messages.warning(request, note)
    else:
        messages.success(request, note)
    return HttpResponseRedirect(back)


def _next_tag_sort_order(qset, path, group_name):
    """Where a tag arriving in this group goes: the end, if the editor has
    put the group in an order of their own; otherwise 0, which keeps an
    untouched group alphabetical as it always was."""
    last = (CategoryTag.objects.filter(question_set=qset, category_path=path, group_name=group_name)
            .order_by('-sort_order').values_list('sort_order', flat=True).first())
    return last + 10 if last else 0


def _move_tag(tag, direction):
    """Swap a tag with its neighbour in the group's current order.

    The group's tags are renumbered 10, 20, 30... on every move, so the order
    the page shows (sort_order, then name) is the order that gets edited --
    a group still at all-zeros moves exactly as it reads.
    """
    if direction not in ('up', 'down'):
        raise ValueError('Unknown direction')
    siblings = list(CategoryTag.objects.filter(
        question_set=tag.question_set, category_path=tag.category_path,
        group_name=tag.group_name))
    idx = next(i for i, t in enumerate(siblings) if t.id == tag.id)
    other = idx - 1 if direction == 'up' else idx + 1
    if 0 <= other < len(siblings):
        siblings[idx], siblings[other] = siblings[other], siblings[idx]
    for n, t in enumerate(siblings):
        if t.sort_order != (n + 1) * 10:
            t.sort_order = (n + 1) * 10
            t.save(update_fields=['sort_order'])


def _reorder_tags(qset, ids):
    """Put a group's tags in the order given (ids, first to last).

    The ids must all be one group's -- one set, one category path, one axis --
    otherwise the numbering would interleave two groups' orders. A tag of the
    group left out of the list keeps its place after the ones named.
    """
    if not ids:
        return
    tags = {t.id: t for t in CategoryTag.objects.filter(question_set=qset, id__in=ids)}
    if len(tags) != len(set(ids)):
        raise ValueError('Unknown tag in the order')
    keys = {(t.category_path, t.group_name) for t in tags.values()}
    if len(keys) != 1:
        raise ValueError('Tags from more than one group cannot be ordered together')
    path, group = keys.pop()
    ordered = [tags[i] for i in ids]
    rest = [t for t in CategoryTag.objects.filter(question_set=qset, category_path=path, group_name=group)
            if t.id not in tags]
    for n, t in enumerate(ordered + rest):
        if t.sort_order != (n + 1) * 10:
            t.sort_order = (n + 1) * 10
            t.save(update_fields=['sort_order'])


def _category_question_rows(qset, path, tag_rows, only_tag=None):
    """Every question in the category at ``path`` with the tags it carries.

    The tags offered for assignment are the ones on this page (those at the
    path itself); a tag a question carries from a parent or child path still
    shows, it just cannot be added from here.

    ``only_tag`` narrows the list to the questions carrying that one tag, which
    is what clicking a tag on the category overview asks for -- "show me these"
    is a different question from "show me everything here".
    """
    page_tags = [r['tag'] for r in tag_rows]
    page_tag_ids = {t.id for t in page_tags}
    set_wide = not (path or '').strip()

    # Any AI proposals standing for this category's tags, by question. They ride
    # on the rows they belong to, so a page refresh (which is how every action
    # here redraws) keeps them.
    suggested = {}
    for s in AITagSuggestion.objects.filter(
            question_set=qset, tag__id__in=page_tag_ids).select_related('tag'):
        suggested.setdefault((s.question_type, s.question_id), []).append(
            {'id': s.id, 'tag_id': s.tag_id, 'name': s.tag.name,
             'confidence': s.confidence, 'explanation': s.explanation})

    # Which categories count as "in" this path: the path itself and anything
    # under it. Worked out once against the distribution, so the questions can
    # be asked for by category instead of reading the whole set and throwing
    # most of it away -- which cost four seconds on an 11,000-question set, and
    # cost it again after every tag assigned, since the page refetches itself.
    category_ids = None
    if not set_wide:
        category_ids = [e.id for e in DistributionEntry.objects.filter(
            distribution_id=qset.distribution_id)
            if str(e) == path or str(e).startswith(path + ' - ')]

    rows = []
    for model, qtype, edit_url in ((Tossup, 'tossup', '/edit_tossup/'),
                                   (Bonus, 'bonus', '/edit_bonus/')):
        qs = (model.objects.filter(question_set=qset)
              .select_related('category', 'packet', 'author__user')
              .prefetch_related('category_tags'))
        if category_ids is not None:
            qs = qs.filter(category_id__in=category_ids)
        for q in qs:
            tags = sorted((t for t in q.category_tags.all() if t.question_set_id == qset.id),
                          key=lambda t: (t.group_name, t.sort_order, t.name))
            have = {t.id for t in tags}
            if only_tag is not None and only_tag not in have:
                continue
            rows.append({
                'qtype': qtype,
                'id': q.id,
                'edit_url': '{0}{1}/'.format(edit_url, q.id),
                'answer': (_grid_answer_preview(q.tossup_answer) if model is Tossup else
                           ' / '.join(filter(None, [_grid_answer_preview(q.part1_answer, 20),
                                                    _grid_answer_preview(q.part2_answer, 20),
                                                    _grid_answer_preview(q.part3_answer, 20)]))),
                'location': '{0} #{1}'.format(q.packet.packet_name, q.question_number) if q.packet_id else '',
                'packet_key': (0, q.packet.packet_name, q.question_number or 0) if q.packet_id else (1, '', q.id),
                'author': str(q.author) if q.author_id else '',
                'tags': tags,
                'suggested': suggested.get((qtype, q.id), []),
                'untagged': not any(t.id in page_tag_ids for t in tags),
            })
    rows.sort(key=lambda r: (r['packet_key'], r['qtype'] != 'tossup'))
    return {'rows': rows,
            'only_tag': only_tag,
            'suggestions': sum(len(r['suggested']) for r in rows),
            # The tags any row here can be given, listed once for the page
            # instead of once per row -- see the template.
            'assignable': [{'id': t.id,
                            'label': ('{0}: {1}'.format(t.group_name, t.name)
                                      if t.group_name else t.name)}
                           for t in page_tags],
            'total': len(rows),
            'untagged': sum(1 for r in rows if r['untagged']),
            'tossups': sum(1 for r in rows if r['qtype'] == 'tossup'),
            'bonuses': sum(1 for r in rows if r['qtype'] == 'bonus')}


def carry_tags_to_set(question, dest_qset):
    """A question moved to another set takes its tags along only where the
    destination has the same tag -- same category path, same name -- and
    drops the rest. A tag belongs to one set; left attached, a tag of the
    old set would count a question it no longer has and turn up on the new
    set's pages as something the set never defined."""
    old = list(question.category_tags.all())
    if not old:
        return
    keep = []
    for tag in old:
        if tag.question_set_id == dest_qset.id:
            keep.append(tag)
            continue
        twin = CategoryTag.objects.filter(
            question_set=dest_qset, category_path=tag.category_path, name=tag.name).first()
        if twin is not None:
            keep.append(twin)
    question.category_tags.set(keep)


def attach_tag_choices(qset, questions, preselected=None):
    """Give each parsed-but-unsaved question the tag checkboxes for its own
    category, so the Type Questions preview can offer them.

    The preview is the last point before the questions exist, and it is the one
    place in that flow where each question's category is already known -- which
    is what the checkboxes hang off. Anything ticked here is applied as the
    question is created, so a typed batch arrives tagged.

    `preselected` is {distribution entry id: {tag id}} from the entry page,
    where a tick means "every question I write in this category" -- there is no
    single question there to attach it to. It only sets what the boxes start
    as; the preview is still per question and still editable.
    """
    preselected = preselected or {}
    by_path = {}
    for q in questions:
        entry = getattr(q, 'category', None)
        if entry is None:
            q.tag_choices = []
            continue
        key = str(entry)
        if key not in by_path:
            by_path[key] = build_tag_checkboxes(
                qset, None, entry, checked_ids=preselected.get(entry.id, ()))
        q.tag_choices = by_path[key]


def _preselected_category_tags(request):
    """{distribution entry id: {tag id}} from the Type Questions tag panel.

    Only decides which boxes the preview opens with. Nothing is applied from
    it: the tags that reach a question are the ones ticked on the preview, and
    `_apply_typed_tags` checks those against the category the question was
    actually filed under.
    """
    raw = request.POST.get('preselected_tags', '')
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for entry_id, tag_ids in data.items():
        if not isinstance(tag_ids, list):
            continue
        try:
            entry = int(entry_id)
            tags = set(int(t) for t in tag_ids)
        except (TypeError, ValueError):
            continue
        if tags:
            out[entry] = tags
    return out


def build_tag_checkboxes(qset, question, dist_entry, checked_ids=None):
    """The tag checkboxes on the question edit pages, as sections.

    One section per category path the tags sit on (nearly always one), each
    with its tags grouped by axis the way the Category Tags page shows them,
    and each tag saying where it stands -- "1/6 tossups, 0/6 bonuses" -- so
    "6/6 needed" stops reading as if it were already done. The counts include
    this question when it is checked, which the template points out.

    `checked_ids` is for a question that doesn't exist yet, whose ticks come
    from somewhere other than the database (see `attach_tag_choices`); by
    default they are read off `question`.
    """
    tags = get_applicable_tags(qset, dist_entry)
    if not tags:
        return []
    if checked_ids is not None:
        checked_ids = set(checked_ids)
    elif question is None or question.id is None:
        checked_ids = set()
    else:
        checked_ids = set(question.category_tags.filter(question_set=qset)
                          .values_list('id', flat=True))

    def _progress_label(tag, p):
        """Reads "2/3 tossups", and for a tag that only caps a type, "5 of at
        most 7 tossups" -- with no minimum there is no denominator to count
        towards, so the ceiling goes in words."""
        bits = []
        for (lo, hi), done, noun in zip(tag.quota_pairs(),
                                        (p['tu_done'], p['bs_done'], p['q_done']),
                                        ('tossups', 'bonuses', 'of any type')):
            if lo:
                bit = '{0}/{1} {2}'.format(done, lo, noun)
                if hi is not None:
                    bit += ' (max {0})'.format(hi)
                bits.append(bit)
            elif hi is not None:
                bits.append('{0} of at most {1} {2}'.format(done, hi, noun))
        return ', '.join(bits)

    def _remaining_label(tag, p):
        """What the tag still wants, for someone deciding what to write:
        "needs 1 more tossup, 2 more bonuses", "met", or "1 tossup over" --
        the progress figures say where it stands, this says what to do."""
        needs, over = [], []
        for (lo, hi), done, (one, many) in zip(
                tag.quota_pairs(), (p['tu_done'], p['bs_done'], p['q_done']),
                (('tossup', 'tossups'), ('bonus', 'bonuses'), ('of any type', 'of any type'))):
            if lo and done < lo:
                n = lo - done
                needs.append('{0} more {1}'.format(n, one if n == 1 else many))
            elif hi is not None and done > hi:
                n = done - hi
                over.append('{0} {1} over'.format(n, one if n == 1 else many))
        bits = []
        if needs:
            bits.append('needs ' + ', '.join(needs))
        bits.extend(over)
        if bits:
            return '; '.join(bits)
        return 'met' if tag.has_quota else ''

    # How many questions each tag already has, for all of them at once.
    # tag.progress() counts on its own when it is not told, which is two
    # queries per tag -- 208 of them on a set with a hundred tags, on every
    # load *and every save* of a question page, since the checkboxes are part
    # of the form. That is most of what made saving slow against a database
    # across the network.
    tag_ids = [t.id for t in tags]
    tu_counts = dict(CategoryTag.objects.filter(id__in=tag_ids).annotate(
        n=Count('tossups', filter=Q(tossups__question_set=qset), distinct=True)
    ).values_list('id', 'n'))
    bs_counts = dict(CategoryTag.objects.filter(id__in=tag_ids).annotate(
        n=Count('bonuses', filter=Q(bonuses__question_set=qset), distinct=True)
    ).values_list('id', 'n'))

    sections, by_path = [], {}
    for tag in tags:
        p = tag.progress(tu_counts.get(tag.id, 0), bs_counts.get(tag.id, 0))
        item = {'tag': tag, 'checked': tag.id in checked_ids,
                'progress': p, 'complete': p['complete'] if tag.has_quota else None,
                'over': p['over'], 'label': _progress_label(tag, p),
                'remaining': _remaining_label(tag, p)}
        by_path.setdefault(tag.category_path, []).append(item)
    for path in sorted(by_path, key=lambda p: (bool(p), p)):
        by_group, order = {}, []
        for item in by_path[path]:
            key = (item['tag'].group_name or '').strip()
            if key not in by_group:
                by_group[key] = []
                order.append(key)
            by_group[key].append(item)
        order.sort(key=lambda k: (k == '', k.lower()))
        sections.append({
            'path': path,
            'label': scope_label(path),
            'set_wide': not path,
            'status_url': '/category_tags/{0}/?category={1}'.format(
                qset.id, urllib.parse.quote(scope_token(path))),
            'groups': [{'name': k, 'label': k or 'Other', 'items': by_group[k]} for k in order],
        })
    return sections

def _apply_typed_tags(request, qset, question, field, is_tossup):
    """Apply the tags ticked for one question on the Type Questions preview.

    Each question has its own field there (they have different categories), and
    only tags that actually apply to the category it was filed under are
    honoured -- the ids come from a form, so they are checked rather than
    trusted.
    """
    wanted = {int(v) for v in request.POST.getlist(field) if v.isdigit()}
    if not wanted:
        return
    for tag in get_applicable_tags(qset, question.category):
        if tag.id in wanted:
            (tag.tossups if is_tossup else tag.bonuses).add(question)


def save_tag_selection(request, qset, question, dist_entry, is_tossup):
    """Apply the 'category_tags' checkbox selection from a question edit POST."""
    selected = set()
    for value in request.POST.getlist('category_tags'):
        if value.isdigit():
            selected.add(int(value))
    for tag in get_applicable_tags(qset, dist_entry):
        relation = tag.tossups if is_tossup else tag.bonuses
        if tag.id in selected:
            relation.add(question)
        else:
            relation.remove(question)

def _restore_authors_from_files(qset, uploads, owner):
    """Credit the people the original packets named, for questions already here.

    An import before this change credited everything to whoever pressed Import,
    and the packet's own metadata — which names the author — wasn't kept. The
    files still have it, so matching each question back to its packet entry by
    its text restores the attribution without touching anything else.
    """
    from . import packet_set_importer as psi
    from . import set_importer as si

    by_text = {}
    unreadable = []
    for f in uploads:
        try:
            f.seek(0)
            payload = json.loads(f.read().decode('utf-8'))
        except Exception as ex:
            unreadable.append('{0} ({1})'.format(getattr(f, 'name', 'file'), ex))
            continue
        for key, is_bonus in (('tossups', False), ('bonuses', True)):
            for q in payload.get(key) or []:
                author = psi.metadata_author(q.get('metadata'))
                if not author:
                    continue
                body = q.get('leadin', '') if is_bonus else q.get('question', '')
                text = _plain_question_key(psi._html_to_qems(body, is_answer=False))
                if text:
                    by_text.setdefault(text, author)

    legacy_ids, resolve = si._author_resolver(owner)
    changed, names = 0, set()
    for tossup in qset.tossup_set.select_related('author'):
        author = by_text.get(_plain_question_key(tossup.tossup_text))
        if not author:
            continue
        writer = resolve(author)
        if writer and writer.id != tossup.author_id:
            tossup.author = writer
            tossup.save(update_fields=['author'])
            changed += 1
            names.add(author)
    for bonus in qset.bonus_set.select_related('author'):
        author = by_text.get(_plain_question_key(bonus.leadin))
        if not author:
            continue
        writer = resolve(author)
        if writer and writer.id != bonus.author_id:
            bonus.author = writer
            bonus.save(update_fields=['author'])
            changed += 1
            names.add(author)
    return changed, sorted(names), unreadable


def _plain_question_key(text):
    """Question text reduced to what identifies it: letters and digits only, so
    markup, spacing and punctuation differences between the file and what was
    stored don't stop a match."""
    return re.sub(r'[^a-z0-9]+', '', strip_markup(text or '').lower())[:400]


@login_required
def tidy_categories(request, qset_id):
    """Put imported questions back into the set's own categories.

    An import used to invent a category for every name it found, so a set could
    end up with its own distribution plus a shadow one carrying another set's
    names — and its question ids and editors, where the packet had put them all
    on one metadata line. This finds those, proposes the nearest real category
    for each (`category_mapper`), lets you change any of them, and clears the
    leftovers out.

    Applying moves the questions, then deletes the emptied categories — but only
    ones no question anywhere still uses, since a distribution can be shared.
    """
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if not (qset.is_owner(user) or user in qset.editor.all()):
        return render(request, 'failure.html',
                      {'message': 'Only an owner or editor can tidy a set\'s categories.',
                       'message_class': 'alert-box alert'})

    message = message_class = ''

    if request.method == 'POST' and request.POST.get('action') == 'authors':
        uploads = request.FILES.getlist('files')
        if not uploads:
            message, message_class = 'Choose the packet files first.', 'alert-box warning'
        else:
            changed, names, unreadable = _restore_authors_from_files(qset, uploads, user)
            cache.clear()
            parts = ['Credited {0} question{1} to the writer named in the packet.'.format(
                changed, '' if changed == 1 else 's')]
            if names:
                parts.append('Authors found: {0}.'.format(', '.join(names[:12])))
            if unreadable:
                parts.append('Could not read: {0}.'.format('; '.join(unreadable)))
            message = ' '.join(parts)
            message_class = 'alert-box success' if changed else 'alert-box warning'

    entries = list(qset.distribution.distributionentry_set.all()
                   .order_by('category', 'subcategory')) if qset.distribution_id else []
    debris = [e for e in entries if category_mapper.looks_like_debris(e)]
    keep = [e for e in entries if e not in debris]

    if request.method == 'POST' and request.POST.get('action') == 'apply':
        moved = deleted = stranded = 0
        by_id = {e.id: e for e in keep}
        with transaction.atomic():
            for entry in debris:
                raw = request.POST.get('target_{0}'.format(entry.id), '')
                if raw == 'leave':
                    continue
                target = None
                if raw.isdigit() and int(raw) in by_id:
                    target = by_id[int(raw)]
                elif not raw:
                    # No choice submitted for this row: take the proposal. The
                    # page always sends one, so this is the "apply what you
                    # suggested" case rather than a silent guess.
                    cat, sub = category_mapper.split_path(
                        '{0} - {1}'.format(entry.category, entry.subcategory).strip(' -'))
                    target = category_mapper.best_entry(cat, sub, keep)
                if target is not None:
                    moved += qset.tossup_set.filter(category=entry).update(category=target)
                    moved += qset.bonus_set.filter(category=entry).update(category=target)
                elif raw == 'none':
                    # Deliberately unfiled: the question shows up on Category
                    # Issues for a person to place.
                    stranded += qset.tossup_set.filter(category=entry).update(category=None)
                    stranded += qset.bonus_set.filter(category=entry).update(category=None)
                else:
                    continue
                still_used = (Tossup.objects.filter(category=entry).exists() or
                              Bonus.objects.filter(category=entry).exists())
                if not still_used:
                    SetWideDistributionEntry.objects.filter(dist_entry=entry).delete()
                    TieBreakDistributionEntry.objects.filter(dist_entry=entry).delete()
                    entry.delete()
                    deleted += 1
        cache.clear()
        parts = ['Moved {0} question{1} into the set\'s own categories.'.format(
            moved, '' if moved == 1 else 's')]
        if deleted:
            parts.append('Removed {0} leftover categor{1}.'.format(
                deleted, 'y' if deleted == 1 else 'ies'))
        if stranded:
            parts.append('{0} left uncategorized, as asked.'.format(stranded))
        message, message_class = ' '.join(parts), 'alert-box success'
        entries = list(qset.distribution.distributionentry_set.all()
                       .order_by('category', 'subcategory'))
        debris = [e for e in entries if category_mapper.looks_like_debris(e)]
        keep = [e for e in entries if e not in debris]

    plan = []
    for entry in debris:
        cat, sub = category_mapper.split_path(
            '{0} - {1}'.format(entry.category, entry.subcategory).strip(' -'))
        target = category_mapper.best_entry(cat, sub, keep)
        elsewhere = (Tossup.objects.filter(category=entry).exclude(question_set=qset).count() +
                     Bonus.objects.filter(category=entry).exclude(question_set=qset).count())
        plan.append({
            'entry': entry,
            'cleaned': '{0} - {1}'.format(cat, sub).strip(' -'),
            'target': target,
            'tossups': qset.tossup_set.filter(category=entry).count(),
            'bonuses': qset.bonus_set.filter(category=entry).count(),
            'elsewhere': elsewhere,
        })

    return render(request, 'tidy_categories.html',
                  {'user': user, 'qset': qset, 'plan': plan,
                   'keep': keep,
                   'keep_count': len(keep),
                   'movable': sum(1 for r in plan if r['target'] is not None),
                   'total_questions': sum(r['tossups'] + r['bonuses'] for r in plan),
                   'message': message, 'message_class': message_class})


@login_required
def category_problems(request, qset_id):
    """Questions whose category is missing or belongs to a different
    distribution than the set's current one (e.g. after switching
    distributions, or moving questions between sets), with bulk reassignment
    to a valid category. Any member can view; owners/editors can reassign."""
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return render(request, 'failure.html',
                      {'message': 'You are not authorized to view this set!',
                       'message_class': 'alert-box alert'})
    can_assign = qset.is_owner(user) or user in qset.editor.all()

    valid_entries = (list(qset.distribution.distributionentry_set.all()
                          .order_by('category', 'subcategory'))
                     if qset.distribution_id else [])
    # Categories an import left behind are a different problem from a question
    # with the wrong category, and have their own page; point at it from here,
    # which is where someone looking at a mess like that lands first.
    debris_count = sum(1 for e in valid_entries if category_mapper.looks_like_debris(e))

    message = ''
    message_class = ''
    if request.method == 'POST':
        if not can_assign:
            message = 'Only owners and editors can reassign categories.'
            message_class = 'alert-box alert'
        else:
            entry = None
            raw_entry = request.POST.get('category', '')
            if raw_entry.isdigit() and qset.distribution_id:
                # The target must be a category of THIS set's distribution.
                entry = qset.distribution.distributionentry_set.filter(id=int(raw_entry)).first()
            refs = request.POST.getlist('q')
            if entry is None:
                message = 'Pick a category to assign.'
                message_class = 'alert-box alert'
            elif not refs:
                message = 'Select one or more questions first.'
                message_class = 'alert-box alert'
            else:
                changed = 0
                for ref in refs:
                    qtype, _, qid = ref.partition('-')
                    model = Tossup if qtype == 'tossup' else Bonus if qtype == 'bonus' else None
                    if model is None or not qid.isdigit():
                        continue
                    q = model.objects.filter(id=int(qid), question_set=qset).first()
                    if q is None:
                        continue
                    q.category = entry
                    q.save()
                    changed += 1
                message = 'Assigned {0} question(s) to {1}.'.format(changed, html.unescape(str(entry)))
                message_class = 'alert-box success'

    problems = []

    def scan(model, qtype, answer_of):
        for q in (model.objects.filter(question_set=qset)
                  .select_related('category', 'packet', 'author__user')
                  .order_by('packet__packet_name', 'question_number')):
            entry = q.category
            if entry is not None and entry.distribution_id == qset.distribution_id:
                continue
            problems.append({
                'ref': '{0}-{1}'.format(qtype, q.id),
                'type': qtype,
                'answer': answer_of(q),
                'edit_url': '/edit_{0}/{1}/'.format(qtype, q.id),
                'packet': q.packet.packet_name if q.packet_id else '',
                'number': q.question_number or '',
                'author': str(q.author),
                'category': html.unescape(str(entry)) if entry else '',
            })

    scan(Tossup, 'tossup', lambda t: _grid_answer_preview(t.tossup_answer))
    scan(Bonus, 'bonus', lambda b: ' / '.join(filter(None, [
        _grid_answer_preview(b.part1_answer, 20),
        _grid_answer_preview(b.part2_answer, 20),
        _grid_answer_preview(b.part3_answer, 20)])))

    return render(request, 'category_problems.html',
                  {'qset': qset, 'user': user, 'problems': problems,
                   'valid_entries': valid_entries, 'can_assign': can_assign,
                   'debris_count': debris_count,
                   'message': message, 'message_class': message_class,
                   'extra_crumb': 'Category Issues',
                   'extra_crumb_url': '/category_problems/{0}/'.format(qset.id)})


def _category_comment_rows(qset, path, include_children=False):
    """Comments on a category, newest last, ready for the shared partial.

    `include_children` widens it to everything under a top-level category, so
    the page for all of Fine Arts shows what was said about Fine Arts - Audio
    as well as about Fine Arts itself.
    """
    comments = qset.category_comments.select_related('author__user')
    if include_children:
        comments = comments.filter(
            Q(category_path=path) | Q(category_path__startswith='{0} - '.format(path)))
    else:
        comments = comments.filter(category_path=path)
    return list(comments)


def _category_comment_context(request, qset, path, include_children=False):
    """What every page showing category comments needs."""
    user = request.user.writer
    return {
        'cat_comments': _category_comment_rows(qset, path, include_children),
        'cat_comment_path': path,
        'cat_comment_qset': qset,
        'cat_comment_user': user,
        'can_comment_on_category': (qset.is_owner(user) or user in qset.editor.all()
                                    or user in qset.writer.all()),
    }


@login_required
def category_comment(request, qset_id):
    """Add, reword, or remove a note on a category. POST: category_path and
    comment to add; action=edit with comment_id and comment to reword;
    action=delete with comment_id to remove. Any member of the set may
    comment; an author may reword or remove their own note, and an editor may
    remove anybody's -- rewording somebody else's note would put words in
    their mouth, so that stays with the author."""
    user = request.user.writer
    try:
        qset = QuestionSet.objects.get(id=int(qset_id))
    except (ValueError, QuestionSet.DoesNotExist):
        return render(request, 'failure.html',
                      {'message': 'That set no longer exists.',
                       'message_class': 'alert-box alert'})
    if not (qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all()):
        return render(request, 'failure.html',
                      {'message': 'You are not authorized to comment on this set!',
                       'message_class': 'alert-box alert'})

    back = (request.POST.get('next') or '').strip()
    if not back.startswith('/'):
        back = '/category_overview/{0}/'.format(qset.id)

    if request.method == 'POST':
        if request.POST.get('action') == 'delete':
            note = qset.category_comments.filter(id=request.POST.get('comment_id') or 0).first()
            if note is None:
                messages.error(request, 'That note no longer exists.')
            elif not (note.author_id == user.id or qset.is_owner(user)
                      or user in qset.editor.all()):
                messages.error(request, 'You can only remove your own notes.')
            else:
                note.delete()
                cache.clear()
                messages.success(request, 'Note removed.')
        elif request.POST.get('action') == 'edit':
            note = qset.category_comments.filter(id=request.POST.get('comment_id') or 0).first()
            text = (request.POST.get('comment') or '').strip()
            if note is None:
                messages.error(request, 'That note no longer exists.')
            elif note.author_id != user.id:
                messages.error(request, 'You can only reword your own notes.')
            elif not text:
                messages.error(request, 'A note needs some text.')
            else:
                if note.comment != text:
                    note.comment = text
                    note.edited_date = timezone.now()
                    note.save(update_fields=['comment', 'edited_date'])
                    cache.clear()
                messages.success(request, 'Note updated.')
        else:
            path = (request.POST.get('category_path') or '').strip()
            text = (request.POST.get('comment') or '').strip()
            if not path or not text:
                messages.error(request, 'A note needs a category and some text.')
            else:
                CategoryComment.objects.create(
                    question_set=qset, category_path=path, author=user, comment=text)
                cache.clear()
                messages.success(request, 'Note added.')

    return HttpResponseRedirect(back)


def _tag_maxima(post, minima):
    """The three ceilings from a tag form, as (max_tossups, max_bonuses,
    max_questions). An empty box is None -- "no ceiling" -- which is what
    every tag written before ceilings existed has, and is a different thing
    from 0, a ceiling of none at all. Raises ValueError on a negative
    ceiling or one below its own minimum."""
    out = []
    for field, label, low in (('max_tossups', 'tossups', minima[0]),
                              ('max_bonuses', 'bonuses', minima[1]),
                              ('max_questions', 'any type', minima[2])):
        raw = (post.get(field) or '').strip()
        if raw == '':
            out.append(None)
            continue
        value = int(raw)
        if value < 0:
            raise ValueError('Counts cannot be negative')
        if low and value < low:
            raise ValueError('The {0} maximum cannot be below its minimum'.format(label))
        out.append(value)
    return out


@login_required
def category_tags(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)

    if not qset.is_owner(user) and user not in qset.editor.all() and user not in qset.writer.all():
        return render(request, 'failure.html',
                                 {'message': 'You are not authorized to view this set!',
                                  'message_class': 'alert-box alert'})

    can_edit = qset.is_owner(user) or user in qset.editor.all()
    message = ''
    message_class = ''
    # Remember the category just used so the add form stays on it after saving
    selected_path = ''

    if request.method == 'POST':
        selected_path = request.POST.get('category_path', '')
        if not can_edit:
            message = 'Only editors can change tags!'
            message_class = 'alert-box warning'
        else:
            action = request.POST.get('action', '')
            try:
                if action == 'add':
                    raw_path = request.POST.get('category_path', '').strip()
                    path = scope_from_token(raw_path)
                    # tag_name, not name: a field called "name" is one a
                    # password manager offers to fill with the signed-in
                    # person's name and then offers to save as a login.
                    name = request.POST.get('tag_name', '').strip()
                    group_name = request.POST.get('group_name', '').strip()[:100]
                    num_tossups = int(request.POST.get('num_tossups') or 0)
                    num_bonuses = int(request.POST.get('num_bonuses') or 0)
                    num_questions = int(request.POST.get('num_questions') or 0)
                    if min(num_tossups, num_bonuses, num_questions) < 0:
                        raise ValueError('Counts cannot be negative')
                    maxima = _tag_maxima(request.POST,
                                         (num_tossups, num_bonuses, num_questions))
                    if not raw_path or not name:
                        raise ValueError('A category and a tag name are required')
                    tag, created = CategoryTag.objects.get_or_create(
                        question_set=qset, category_path=path, name=name,
                        defaults={'num_tossups': num_tossups, 'num_bonuses': num_bonuses,
                                  'num_questions': num_questions, 'group_name': group_name,
                                  'max_tossups': maxima[0], 'max_bonuses': maxima[1],
                                  'max_questions': maxima[2],
                                  'sort_order': _next_tag_sort_order(qset, path, group_name)})
                    if not created:
                        tag.num_tossups = num_tossups
                        tag.num_bonuses = num_bonuses
                        tag.num_questions = num_questions
                        tag.max_tossups, tag.max_bonuses, tag.max_questions = maxima
                        tag.group_name = group_name
                        tag.save()
                    message = 'Tag "{0}" saved'.format(name)
                    message_class = 'alert-box success'
                elif action == 'edit':
                    tag = CategoryTag.objects.get(question_set=qset, id=int(request.POST['tag_id']))
                    name = request.POST.get('tag_name', '').strip()[:200]
                    if not name:
                        raise ValueError('A tag needs a name')
                    clash = (CategoryTag.objects.filter(question_set=qset, category_path=tag.category_path,
                                                        name=name).exclude(id=tag.id).exists())
                    if clash:
                        raise ValueError('There is already a tag called "{0}" in {1}'.format(
                            name, scope_label(tag.category_path)))
                    counts = [int(request.POST.get(f) or 0)
                              for f in ('num_tossups', 'num_bonuses', 'num_questions')]
                    if min(counts) < 0:
                        raise ValueError('Counts cannot be negative')
                    new_group = request.POST.get('group_name', '').strip()[:100]
                    if new_group != (tag.group_name or ''):
                        # Moving between groups: take the end of the new one so
                        # the order the editor set there stays put.
                        tag.sort_order = _next_tag_sort_order(qset, tag.category_path, new_group)
                    tag.name, tag.group_name = name, new_group
                    tag.num_tossups, tag.num_bonuses, tag.num_questions = counts
                    (tag.max_tossups, tag.max_bonuses,
                     tag.max_questions) = _tag_maxima(request.POST, counts)
                    # Absent checkbox means "keep this tag out of exports"; the
                    # add form never sends the field and so never changes it.
                    tag.show_in_output = request.POST.get('show_in_output') == '1'
                    tag.save()
                    message = 'Tag "{0}" updated'.format(name)
                    message_class = 'alert-box success'
                elif action == 'move':
                    tag = CategoryTag.objects.get(question_set=qset, id=int(request.POST['tag_id']))
                    _move_tag(tag, request.POST.get('direction', ''))
                    message = ''
                elif action == 'reorder':
                    # A whole group's order at once, from a drag on the page.
                    ids = [int(x) for x in request.POST.getlist('order[]') or request.POST.getlist('order')]
                    _reorder_tags(qset, ids)
                    message = ''
                elif action in ('assign', 'unassign'):
                    tag = CategoryTag.objects.get(question_set=qset, id=int(request.POST['tag_id']))
                    qtype = request.POST.get('qtype', '')
                    qid = int(request.POST['question_id'])
                    if qtype == 'tossup':
                        q = Tossup.objects.get(question_set=qset, id=qid)
                        relation = tag.tossups
                    elif qtype == 'bonus':
                        q = Bonus.objects.get(question_set=qset, id=qid)
                        relation = tag.bonuses
                    else:
                        raise ValueError('Unknown question type')
                    if action == 'assign':
                        relation.add(q)
                        message = 'Tagged "{0}"'.format(tag.name)
                    else:
                        relation.remove(q)
                        message = 'Removed "{0}"'.format(tag.name)
                    message_class = 'alert-box success'
                elif action in ('accept_suggestion', 'reject_suggestion'):
                    # An AI proposal is only ever a proposal: accepting is the
                    # ordinary assign, and either verdict takes the row away.
                    sugg = AITagSuggestion.objects.select_related('tag').get(
                        question_set=qset, id=int(request.POST['suggestion_id']))
                    if action == 'accept_suggestion':
                        if sugg.question_type == 'tossup':
                            sugg.tag.tossups.add(Tossup.objects.get(question_set=qset,
                                                                    id=sugg.question_id))
                        else:
                            sugg.tag.bonuses.add(Bonus.objects.get(question_set=qset,
                                                                   id=sugg.question_id))
                        message = 'Tagged "{0}"'.format(sugg.tag.name)
                    else:
                        message = 'Suggestion dismissed'
                    sugg.delete()
                    message_class = 'alert-box success'
                elif action == 'clear_suggestions':
                    path = scope_from_token(request.POST.get('category_path', ''))
                    AITagSuggestion.objects.filter(
                        question_set=qset, tag__category_path=path).delete()
                    message = 'Suggestions cleared'
                    message_class = 'alert-box success'
                elif action == 'delete':
                    CategoryTag.objects.filter(question_set=qset, id=int(request.POST['tag_id'])).delete()
                    message = 'Tag deleted'
                    message_class = 'alert-box success'
            except (CategoryTag.DoesNotExist, Tossup.DoesNotExist, Bonus.DoesNotExist,
                    AITagSuggestion.DoesNotExist):
                message = 'That tag or question no longer exists'
                message_class = 'alert-box warning'
            except (ValueError, KeyError) as ex:
                message = str(ex) or 'Invalid request'
                message_class = 'alert-box warning'

        # Answering a scripted request here rather than re-rendering the page:
        # adding a tag, correcting one, and tagging a question are things you do
        # several of in a row, and a full reload after each loses your place on
        # a page that can be hundreds of rows long.
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return HttpResponse(
                json.dumps({'ok': not message_class.endswith('warning'),
                            'message': message, 'message_class': message_class}),
                content_type='application/json')

        # Tags are edited from the category overview as well as from here.
        nxt = (request.POST.get('next') or '').strip()
        if nxt.startswith('/category_overview/'):
            if message_class.endswith('warning'):
                messages.error(request, message)
            elif message:
                messages.success(request, message)
            return HttpResponseRedirect(nxt)

    # Category path choices from the set's distribution tree
    path_choices = [row['path'] for row in get_packetization_rows(qset)]

    # One category at a time, when the tree asks for it: a set with a few
    # hundred tags is unreadable as one page, and most of the time you are
    # working inside one category anyway.
    raw_focus = (request.GET.get('category') or '').strip()
    focus_set_wide = raw_focus == SET_WIDE_TOKEN
    focus_path = scope_from_token(raw_focus)
    # "focused" and "focused on the set-wide tags" are different things, and
    # both differ from the unfocused index -- hence the token rather than ''.
    focused = bool(raw_focus)

    # Group tags by category path with completion status
    groups = []
    tags_by_path = {}
    tag_qs = CategoryTag.objects.filter(question_set=qset)
    if focused:
        tag_qs = tag_qs.filter(category_path=focus_path)
    # Each tag's questions in one query per type rather than two per tag: a set
    # with a hundred tags was two hundred round trips before the page could be
    # drawn. The filters are the ones the rows used to apply one tag at a time.
    tag_qs = tag_qs.prefetch_related(
        Prefetch('tossups', queryset=Tossup.objects.filter(
            question_set=qset).select_related('packet')),
        Prefetch('bonuses', queryset=Bonus.objects.filter(
            question_set=qset).select_related('packet')))
    for tag in tag_qs:
        tags_by_path.setdefault(tag.category_path, []).append(tag)
    for path in sorted(tags_by_path, key=lambda p: (bool(p), p)):
        rows = []
        for tag in tags_by_path[path]:
            tossups = [{'id': t.id,
                        'answer': _grid_answer_preview(t.tossup_answer),
                        'location': '{0} #{1}'.format(t.packet.packet_name, t.question_number) if t.packet else 'Unassigned'}
                       for t in tag.tossups.all()]
            bonuses = [{'id': b.id,
                        'answer': ' / '.join(filter(None, [
                            _grid_answer_preview(b.part1_answer, 20),
                            _grid_answer_preview(b.part2_answer, 20),
                            _grid_answer_preview(b.part3_answer, 20)])),
                        'location': '{0} #{1}'.format(b.packet.packet_name, b.question_number) if b.packet else 'Unassigned'}
                       for b in tag.bonuses.all()]
            row = tag.progress(len(tossups), len(bonuses))
            row.update({'tag': tag, 'tossups': tossups, 'bonuses': bonuses})
            rows.append(row)
        # Within a category, tags are shown under the axis they belong to —
        # "Time", "Location" — with the unnamed ones last under their own head.
        by_group, order = {}, []
        for row in rows:
            key = (row['tag'].group_name or '').strip()
            if key not in by_group:
                by_group[key] = []
                order.append(key)
            by_group[key].append(row)
        order.sort(key=lambda k: (k == '', k.lower()))
        tag_groups = [{'name': k, 'label': k or 'Ungrouped', 'rows': by_group[k]}
                      for k in order]
        groups.append({
            'path': path,
            'token': scope_token(path),
            'label': scope_label(path),
            'set_wide': not path,
            'rows': rows,
            'tag_groups': tag_groups,
            'tag_count': len(rows),
            'tu_required': sum(r['tag'].num_tossups for r in rows),
            'tu_done': sum(r['tu_done'] for r in rows),
            'bs_required': sum(r['tag'].num_bonuses for r in rows),
            'bs_done': sum(r['bs_done'] for r in rows),
            'q_required': sum(r['tag'].num_questions for r in rows),
            'q_done': sum(r['q_done'] for r in rows if r['tag'].num_questions),
            'incomplete': sum(1 for r in rows if not r['complete']),
        })

    group_choices = sorted(set(
        CategoryTag.objects.filter(question_set=qset)
        .exclude(group_name='').values_list('group_name', flat=True)))

    # An index of the whole tree, not just the categories that already have
    # tags: a category with none is exactly the one somebody needs to open in
    # order to give it some, and it would otherwise be missing from this page.
    from django.db.models import Count
    tag_counts = dict(CategoryTag.objects.filter(question_set=qset)
                      .values_list('category_path')
                      .annotate(n=Count('id')).values_list('category_path', 'n'))
    category_index = [{'path': path, 'token': scope_token(path), 'label': path,
                       'set_wide': False, 'tag_count': tag_counts.get(path, 0)}
                      for path in path_choices]
    # The set-wide tags are a scope of their own, and belong at the top of the
    # index rather than being reachable only from the add form.
    category_index.insert(0, {'path': '', 'token': SET_WIDE_TOKEN,
                              'label': SET_WIDE_LABEL, 'set_wide': True,
                              'tag_count': tag_counts.get('', 0)})

    # With one category open, the page is also where that category's
    # questions get their tags: every question in it, what it carries, and a
    # way to add or drop a tag without opening each question.
    # ?tag=<id> narrows the question list to one tag -- where the category
    # overview sends you when you click a tag there.
    only_tag = None
    raw_tag = (request.GET.get('tag') or '').strip()
    if raw_tag.isdigit():
        only_tag = CategoryTag.objects.filter(
            question_set=qset, id=int(raw_tag)).values_list('id', flat=True).first()
    focus_questions = _category_question_rows(
        qset, focus_path, groups[0]['rows'] if groups else [], only_tag=only_tag) \
        if focused else None
    only_tag_obj = (CategoryTag.objects.filter(question_set=qset, id=only_tag).first()
                    if only_tag else None)

    context = {'qset': qset,
               'user': user,
               'groups': groups,
               'focused': focused,
               'focus_set_wide': focus_set_wide,
               'focus_label': scope_label(focus_path) if focused else '',
               'set_wide_token': SET_WIDE_TOKEN,
               'set_wide_label': SET_WIDE_LABEL,
               'focus_questions': focus_questions,
               'only_tag': only_tag_obj,
               'assignable_tags': (focus_questions or {}).get('assignable', []),
               'path_choices': path_choices,
               'category_index': category_index,
               'group_choices': group_choices,
               'can_edit': can_edit,
               'selected_path': selected_path,
               'focus_path': focus_path,
               # The AI tag suggester is the admin's, and only when a key is
               # configured — the button isn't rendered otherwise.
               'ai_tags_available': _ai_tags_available(request.user),
               'message': message,
               'message_class': message_class}
    # Notes belong with the category being worked on, so they show on the
    # focused view; the all-categories tree stays a list of categories.
    if focus_path:
        context.update(_category_comment_context(request, qset, focus_path))
    return render(request, 'category_tags.html', context)


def _member_or_403(request, qset):
    """Return the writer if they belong to the set, else None."""
    user = request.user.writer
    if qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all():
        return user
    return None


@login_required
def recap(request, qset_id):
    """Set-wide "what's changed lately" digest: new questions, recent edits,
    and recent comments across the whole set over a selectable time window."""
    qset = QuestionSet.objects.get(id=qset_id)
    user = _member_or_403(request, qset)
    if user is None:
        return render(request, 'failure.html',
                      {'message': 'You are not authorized to view this set!',
                       'message_class': 'alert-box alert'})

    # `?since=<epoch>` is how the "since your last visit" banner links here: the
    # window starts at that visit instead of a fixed number of days, and — like
    # the banner — the user's own activity is left out, so the counts match.
    from datetime import datetime as dt, timedelta, timezone as dt_timezone
    now = timezone.now()
    days = None
    since = None
    try:
        stamp = int(request.GET.get('since', ''))
        candidate = dt.fromtimestamp(stamp, dt_timezone.utc)
    except (ValueError, OverflowError, OSError):
        candidate = None
    if candidate is not None and now - timedelta(days=365) <= candidate <= now:
        since = candidate
    since_visit = since is not None

    if since is None:
        try:
            days = int(request.GET.get('days', 7))
        except ValueError:
            days = 7
        if days not in (1, 3, 7, 14, 30):
            days = 7
        since = now - timedelta(days=days)

    def location(q):
        if q.packet_id and q.question_number:
            return '{0} #{1}'.format(q.packet.packet_name, q.question_number)
        return 'Unassigned'

    # --- New questions created in the window ---
    new_questions = []
    new_tu = (Tossup.objects.filter(question_set=qset, created_date__gte=since)
              .select_related('packet', 'author__user', 'category').order_by('-created_date'))
    new_bs = (Bonus.objects.filter(question_set=qset, created_date__gte=since)
              .select_related('packet', 'author__user', 'category').order_by('-created_date'))
    if since_visit:
        new_tu = new_tu.exclude(author=user)
        new_bs = new_bs.exclude(author=user)
    for t in new_tu:
        new_questions.append({
            'date': t.created_date, 'qtype': 'tossup',
            'answer': _grid_answer_preview(t.tossup_answer),
            'author': str(t.author) if t.author else 'unknown',
            'category': str(t.category) if t.category else '',
            'location': location(t), 'edit_url': '/edit_tossup/{0}/'.format(t.id)})
    for b in new_bs:
        new_questions.append({
            'date': b.created_date, 'qtype': 'bonus',
            'answer': _grid_answer_preview(b.part1_answer),
            'author': str(b.author) if b.author else 'unknown',
            'category': str(b.category) if b.category else '',
            'location': location(b), 'edit_url': '/edit_bonus/{0}/'.format(b.id)})
    new_questions.sort(key=lambda x: x['date'], reverse=True)

    # --- Recent edits (history rows that aren't the question's creation) ---
    edit_items = []
    for model, hist_model, edit_url, preview in (
            (Tossup, TossupHistory, '/edit_tossup/', lambda q: _grid_answer_preview(q.tossup_answer)),
            (Bonus, BonusHistory, '/edit_bonus/', lambda q: _grid_answer_preview(q.part1_answer))):
        qs = model.objects.filter(question_set=qset).select_related('packet')
        hist_to_q = {q.question_history_id: q for q in qs if q.question_history_id}
        if not hist_to_q:
            continue
        histories = (hist_model.objects.filter(question_history_id__in=hist_to_q.keys(),
                                               change_date__gte=since)
                     .select_related('changer__user'))
        if since_visit:
            histories = histories.exclude(changer=user)
        histories = histories.order_by('-change_date')[:200]
        for h in histories:
            q = hist_to_q.get(h.question_history_id)
            # Skip the very first history row (creation) so this is edits only.
            if q is None or (q.created_date and h.change_date <= q.created_date):
                continue
            edit_items.append({
                'date': h.change_date, 'qtype': 'tossup' if model is Tossup else 'bonus',
                'by': str(h.changer) if h.changer else 'unknown',
                'answer': preview(q), 'location': location(q),
                'edit_url': '{0}{1}/'.format(edit_url, q.id)})
    edit_items.sort(key=lambda x: x['date'], reverse=True)
    edit_items = edit_items[:200]

    # --- Recent comments across the set ---
    tu_ct = ContentType.objects.get_for_model(Tossup)
    bs_ct = ContentType.objects.get_for_model(Bonus)
    tu_ids = [str(i) for i in qset.tossup_set.values_list('id', flat=True)]
    bs_ids = [str(i) for i in qset.bonus_set.values_list('id', flat=True)]
    comments = (Comment.objects.filter(is_removed=False, submit_date__gte=since)
                .filter(Q(content_type=tu_ct, object_pk__in=tu_ids) |
                        Q(content_type=bs_ct, object_pk__in=bs_ids))
                .select_related('user', 'content_type'))
    if since_visit:
        comments = comments.exclude(user=user.user)
    comments = comments.order_by('-submit_date')[:200]
    comment_items = []
    for c in comments:
        is_tu = c.content_type_id == tu_ct.id
        by = '{0} {1}'.format(c.user.first_name, c.user.last_name).strip() if c.user else ''
        by = by or (c.user_name or 'unknown')
        comment_items.append({
            'date': c.submit_date, 'by': by, 'text': c.comment,
            'qtype': 'tossup' if is_tu else 'bonus',
            'edit_url': '{0}{1}/'.format('/edit_tossup/' if is_tu else '/edit_bonus/', c.object_pk)})

    return render(request, 'recap.html',
                  {'qset': qset, 'user': user, 'days': days,
                   'since_visit': since_visit, 'since': since,
                   'new_questions': new_questions,
                   'edit_items': edit_items,
                   'comment_items': comment_items,
                   'new_question_count': len(new_questions),
                   'edit_count': len(edit_items),
                   'comment_count': len(comment_items)})


def _tossup_reading(tossup):
    """Turn a tossup into the data the play UI reads clue-by-clue: a list of
    display words, the power boundary word indices, and the answer as formatted
    HTML. `power_index` is the word containing the 15-point "(*)" mark and
    `superpower_index` the 20-point "(+)" mark (each -1 if unmarked)."""
    plain = strip_markup(tossup.tossup_text or '').replace('\n', ' ').strip()
    plain = plain.replace('\\P', '')  # PG-target markers aren't read
    raw_words = [w for w in plain.split(' ') if w != '']
    allow_superpower = tossup.superpower_enabled()
    power_index = -1
    superpower_index = -1
    words = []
    for w in raw_words:
        if '(+)' in w:
            # Only a superpower-enabled set treats (+) as a 20-point boundary;
            # either way the marker itself is stripped so it isn't read aloud.
            if allow_superpower and superpower_index == -1:
                superpower_index = len(words)
            w = w.replace('(+)', '').strip()
        if '(*)' in w and power_index == -1:
            power_index = len(words)
            w = w.replace('(*)', '').strip()
        if w == '':
            continue
        words.append(w)
    # An all-power tossup with no explicit mark: the whole stem is power, so
    # every clue is bold and any correct buzz scores 15.
    if power_index == -1 and compute_all_power(tossup.all_power, tossup.tossup_text):
        power_index = len(words)
        superpower_index = -1
    answer_html = get_formatted_question_html(tossup.tossup_answer, True, True, False, False)
    return {
        'id': tossup.id,
        'qtype': 'tossup',
        'number': tossup.question_number or 0,
        'words': words,
        'power_index': power_index,
        'superpower_index': superpower_index,
        'answer_html': answer_html,
        'category': str(tossup.category) if tossup.category else '',
    }


def _bonus_reading(bonus):
    """Turn a bonus into the data the play UI reveals part-by-part."""
    is_acf = bonus.get_bonus_type() == ACF_STYLE_BONUS
    leadin_html = get_formatted_question_html(bonus.leadin, False, True, False, False) if is_acf else ''
    parts = []
    fields = [(bonus.part1_text, bonus.part1_answer, bonus.part1_difficulty),
              (bonus.part2_text, bonus.part2_answer, bonus.part2_difficulty),
              (bonus.part3_text, bonus.part3_answer, bonus.part3_difficulty)]
    if not is_acf:
        fields = fields[:1]
    for text, answer, difficulty in fields:
        if text is None or text == '':
            continue
        parts.append({
            'text_html': get_formatted_question_html(text, False, True, False, False),
            'answer_html': get_formatted_question_html(answer, True, True, False, False),
            'difficulty': difficulty or '',
        })
    return {
        'id': bonus.id,
        'qtype': 'bonus',
        'number': bonus.question_number or 0,
        'leadin_html': leadin_html,
        'parts': parts,
        'category': str(bonus.category) if bonus.category else '',
    }


@login_required
def play(request, qset_id):
    """Play the set's questions clue-by-clue (tossups) / part-by-part (bonuses).
    The player chooses what to play: recent questions (optionally filtered by
    category) or questions by category, and whether to play tossups, bonuses, or
    both. Buzzes/results are recorded via AJAX (record_buzz/record_bonus_result)."""
    qset = QuestionSet.objects.get(id=qset_id)
    user = _member_or_403(request, qset)
    if user is None:
        return render(request, 'failure.html',
                      {'message': 'You are not authorized to play this set!',
                       'message_class': 'alert-box alert'})

    mode = request.GET.get('mode', 'recent')
    if mode not in ('recent', 'category'):
        mode = 'recent'
    qtypes = request.GET.get('qtypes', 'both')
    if qtypes not in ('both', 'tossups', 'bonuses'):
        qtypes = 'both'
    if qset.tossups_only:
        qtypes = 'tossups'
    try:
        limit = int(request.GET.get('limit', 30))
    except ValueError:
        limit = 30
    limit = max(1, min(limit, 200))
    # In recent mode, play either the most-recent N questions ('count') or every
    # question created in the last N days ('days').
    recent_by = request.GET.get('recent_by', 'count')
    if recent_by not in ('count', 'days'):
        recent_by = 'count'
    try:
        days = int(request.GET.get('days', 7))
    except ValueError:
        days = 7
    days = max(1, min(days, 365))
    selected_cat_ids = [c for c in request.GET.getlist('cats') if c.isdigit()]
    # Playing your own questions tells you nothing about how they play, so they
    # are skipped unless the player asks for them back.
    include_own = request.GET.get('include_own') == '1'

    # Categories actually used by this set's questions, for the filter UI.
    used_cat_ids = set(Tossup.objects.filter(question_set=qset, category__isnull=False)
                       .values_list('category_id', flat=True))
    used_cat_ids |= set(Bonus.objects.filter(question_set=qset, category__isnull=False)
                        .values_list('category_id', flat=True))
    categories = sorted(
        ({'id': de.id, 'name': str(de)}
         for de in DistributionEntry.objects.filter(id__in=used_cat_ids)),
        key=lambda c: c['name'].lower())

    def gather(model):
        qs = model.objects.filter(question_set=qset).select_related(
            'category', 'packet', 'question_type')
        if selected_cat_ids:
            qs = qs.filter(category_id__in=selected_cat_ids)
        if not include_own:
            qs = qs.exclude(author=user)
        if mode == 'recent':
            qs = qs.order_by('-created_date')
            if recent_by == 'days':
                from datetime import timedelta
                since = timezone.now() - timedelta(days=days)
                # Cap to keep the page reasonable; this is the most-recent slice.
                return qs.filter(created_date__gte=since)[:200]
            return qs[:limit]
        return qs.order_by('category__category', 'category__subcategory',
                           'packet__packet_name', 'question_number')[:200]

    tossups, bonuses = [], []
    if qtypes in ('both', 'tossups'):
        for t in gather(Tossup):
            r = _tossup_reading(t)
            r['edit_url'] = '/edit_tossup/{0}/'.format(t.id)
            tossups.append(r)
    if qtypes in ('both', 'bonuses'):
        for b in gather(Bonus):
            r = _bonus_reading(b)
            r['edit_url'] = '/edit_bonus/{0}/'.format(b.id)
            bonuses.append(r)

    return render(request, 'play.html',
                  {'qset': qset, 'user': user,
                   'mode': mode, 'qtypes': qtypes, 'limit': limit,
                   'recent_by': recent_by, 'days': days,
                   'categories': categories,
                   'selected_cat_ids': [int(c) for c in selected_cat_ids],
                   'include_own': include_own,
                   'tossups_only': qset.tossups_only,
                   'questions': {'tossups': tossups, 'bonuses': bonuses}})


def _get_or_create_session(request, qset, session_id):
    """Reuse the play session identified by session_id if it belongs to this
    user and set, otherwise start a new one."""
    user = request.user.writer
    if session_id:
        session = PlaytestSession.objects.filter(id=session_id, question_set=qset,
                                                 player=user).first()
        if session is not None:
            return session
    return PlaytestSession.objects.create(question_set=qset, player=user,
                                          source=PLAYTEST_SOURCE_WEB)


@login_required
def record_buzz(request):
    """Record a tossup buzz from the play UI. Returns the session id so the
    client can group later buzzes from the same sitting."""
    if request.method != 'POST':
        return HttpResponse(json.dumps({'success': False, 'message': 'Invalid request'}))
    user = request.user.writer
    try:
        tossup = Tossup.objects.select_related('question_set').get(id=int(request.POST['tossup_id']))
    except (KeyError, ValueError, Tossup.DoesNotExist):
        return HttpResponse(json.dumps({'success': False, 'message': 'Tossup not found'}))

    qset = tossup.question_set
    if _member_or_403(request, qset) is None:
        return HttpResponse(json.dumps({'success': False, 'message': 'Not authorized'}))

    dont_know = request.POST.get('dont_know') == 'true'
    correct = not dont_know and request.POST.get('correct') == 'true'
    # A 20-point superpower only counts when the set has enabled it.
    superpowered = request.POST.get('superpowered') == 'true' and qset.enable_superpower
    # A superpower buzz is also inside the regular power region.
    powered = superpowered or request.POST.get('powered') == 'true'
    # An "I don't know" is not a wrong buzz, so it never negs.
    neg = not dont_know and request.POST.get('neg') == 'true'
    try:
        buzz_word_index = int(request.POST.get('buzz_word_index') or 0)
        total_words = int(request.POST.get('total_words') or 0)
        char_position = int(request.POST.get('char_position') or 0)
    except ValueError:
        return HttpResponse(json.dumps({'success': False, 'message': 'Bad buzz position'}))

    if correct:
        value = 20 if superpowered else 15 if powered else 10
    elif neg:
        value = -5
    else:
        value = 0

    session = _get_or_create_session(request, qset, request.POST.get('session_id'))
    TossupBuzz.objects.create(
        tossup=tossup, session=session, player=user,
        buzz_word_index=buzz_word_index, total_words=total_words,
        char_position=char_position, correct=correct, powered=powered and correct,
        superpowered=superpowered and correct, dont_know=dont_know,
        value=value, answer_given=request.POST.get('answer_given', '')[:1000],
        tossup_history=tossup.latest_history(), source=PLAYTEST_SOURCE_WEB)

    return HttpResponse(json.dumps({'success': True, 'session_id': session.id, 'value': value}))


@login_required
def record_bonus_result(request):
    """Record the result of playing a bonus from the play UI."""
    if request.method != 'POST':
        return HttpResponse(json.dumps({'success': False, 'message': 'Invalid request'}))
    user = request.user.writer
    try:
        bonus = Bonus.objects.select_related('question_set').get(id=int(request.POST['bonus_id']))
    except (KeyError, ValueError, Bonus.DoesNotExist):
        return HttpResponse(json.dumps({'success': False, 'message': 'Bonus not found'}))

    qset = bonus.question_set
    if _member_or_403(request, qset) is None:
        return HttpResponse(json.dumps({'success': False, 'message': 'Not authorized'}))

    p1 = request.POST.get('part1_correct') == 'true'
    p2 = request.POST.get('part2_correct') == 'true'
    p3 = request.POST.get('part3_correct') == 'true'
    total = 10 * sum((p1, p2, p3))

    session = _get_or_create_session(request, qset, request.POST.get('session_id'))
    BonusResult.objects.create(
        bonus=bonus, session=session, player=user,
        part1_correct=p1, part2_correct=p2, part3_correct=p3, total=total,
        bonus_history=bonus.latest_history(), source=PLAYTEST_SOURCE_WEB)

    return HttpResponse(json.dumps({'success': True, 'session_id': session.id, 'total': total}))


def _question_buzz_data(question, qtype):
    """Aggregate playtest results for a single question, shown as a panel on its
    edit page (buzz stats are a property of the question, not a separate list).
    Returns None when nothing has been recorded yet."""
    if question is None:
        return None

    if qtype == 'tossup':
        buzzes = list(question.buzzes.select_related('player__user').order_by('buzz_date'))
        if not buzzes:
            return None
        correct = [b for b in buzzes if b.correct]
        powers = [b for b in correct if b.powered]
        superpowers = [b for b in correct if b.superpowered]
        negs = [b for b in buzzes if b.value < 0]
        dont_knows = [b for b in buzzes if b.dont_know]
        fracs = [b.buzz_fraction() for b in correct]
        words = _tossup_reading(question)['words']
        rows = []
        for b in buzzes:
            heard_words = words[:b.buzz_word_index] if words else []
            heard = ' '.join(heard_words)
            if len(heard) > 140:
                heard = '...' + heard[-140:]
            # Just the last few words the player heard before buzzing.
            heard_tail = ' '.join(heard_words[-3:])
            rows.append({
                'player': b.get_player_name(), 'date': b.buzz_date,
                'correct': b.correct, 'powered': b.powered,
                'superpowered': b.superpowered, 'dont_know': b.dont_know, 'value': b.value,
                'fraction': '{0:.0f}%'.format(100.0 * b.buzz_fraction()),
                'answer_given': b.answer_given, 'heard': heard, 'heard_tail': heard_tail,
                'source': b.source, 'history_url': b.history_url()})
        return {
            'qtype': 'tossup', 'plays': len(buzzes), 'correct': len(correct),
            'powers': len(powers), 'superpowers': len(superpowers), 'negs': len(negs),
            'dont_knows': len(dont_knows),
            'conversion': '{0:.0f}%'.format(100.0 * len(correct) / len(buzzes)),
            'avg_buzz': '{0:.0f}%'.format(100.0 * sum(fracs) / len(fracs)) if fracs else '—',
            'rows': rows}

    results = list(question.results.select_related('player__user').order_by('answered_date'))
    if not results:
        return None
    n = len(results)
    rows = [{
        'player': r.get_player_name(), 'date': r.answered_date,
        'p1': r.part1_correct, 'p2': r.part2_correct, 'p3': r.part3_correct,
        'total': r.total, 'source': r.source, 'history_url': r.history_url()}
        for r in results]
    return {
        'qtype': 'bonus', 'plays': n,
        'avg_points': '{0:.1f}'.format(sum(r.total for r in results) / n),
        'p1': '{0:.0f}%'.format(100.0 * sum(1 for r in results if r.part1_correct) / n),
        'p2': '{0:.0f}%'.format(100.0 * sum(1 for r in results if r.part2_correct) / n),
        'p3': '{0:.0f}%'.format(100.0 * sum(1 for r in results if r.part3_correct) / n),
        'rows': rows}


@login_required
def api_access(request, qset_id):
    """Owner/co-owner page to view and manage the set's Discord-bot API key."""
    qset = QuestionSet.objects.get(id=qset_id)
    user = request.user.writer
    if not qset.is_owner(user):
        return render(request, 'failure.html',
                      {'message': 'Only the set owner or a co-owner can manage API access.',
                       'message_class': 'alert-box alert'})
    api_key = SetApiKey.objects.filter(question_set=qset).first()
    base_url = request.build_absolute_uri('/').rstrip('/')
    return render(request, 'api_access.html',
                  {'qset': qset, 'user': user, 'api_key': api_key, 'base_url': base_url})


@login_required
def generate_set_api_key(request):
    """Create, rotate, or revoke a set's API key (owner/co-owner only)."""
    if request.method != 'POST':
        return HttpResponseRedirect('/main/')
    user = request.user.writer
    qset = QuestionSet.objects.get(id=int(request.POST['qset_id']))
    if not qset.is_owner(user):
        return render(request, 'failure.html',
                      {'message': 'Only the set owner or a co-owner can manage API access.',
                       'message_class': 'alert-box alert'})
    action = request.POST.get('action', 'generate')
    if action == 'revoke':
        SetApiKey.objects.filter(question_set=qset).delete()
        messages.success(request, 'API key revoked.')
    else:
        api_key, _ = SetApiKey.objects.get_or_create(
            question_set=qset, defaults={'key': SetApiKey.generate_token(), 'created_by': user})
        # Regenerate always issues a fresh token (revoking the old one).
        api_key.key = SetApiKey.generate_token()
        api_key.active = True
        api_key.created_by = user
        api_key.save()
        messages.success(request, 'A new API key has been generated.')
    return HttpResponseRedirect('/api_access/{0}/'.format(qset.id))


# ---------------------------------------------------------------------------
# Reference data (admin only): the bundled pronunciation dictionary and standard
# answer lines the style checker reads. Both ship as generated data files, so
# corrections are stored as ReferenceDataOverride rows layered on top — see
# qsub/reference_overrides.py.
# ---------------------------------------------------------------------------

from urllib.parse import quote


def _reference_admin_only(request):
    """None if the caller may manage reference data, else a response to return."""
    if not request.user.is_superuser:
        messages.error(request, 'Only an admin account may manage reference data.')
        return HttpResponseRedirect('/failure.html/')
    return None


def _reference_override_map(dataset):
    """{key: ReferenceDataOverride} for one dataset, to mark up search results."""
    return {o.key: o for o in ReferenceDataOverride.objects.filter(dataset=dataset)
            .select_related('changed_by__user')}


@login_required
def reference_data(request, dataset='pron'):
    """Admin screen for searching and fixing the bundled reference datasets."""
    from . import pron_dict, answer_db

    denied = _reference_admin_only(request)
    if denied is not None:
        return denied

    if dataset not in (ReferenceDataOverride.PRONUNCIATION, ReferenceDataOverride.ANSWER_LINE):
        dataset = ReferenceDataOverride.PRONUNCIATION

    query = (request.GET.get('q') or '').strip()
    overrides_by_key = _reference_override_map(dataset)

    rows = []
    total = 0
    if dataset == ReferenceDataOverride.PRONUNCIATION:
        # A blank search would walk 5,800 entries into the page; ask for a term.
        if query:
            found, total = pron_dict.search(query)
            for e in found:
                rows.append({'key': e['key'], 'term': e['term'], 'value': e['pron'],
                             'source': e['source'], 'override': overrides_by_key.get(e['key'])})
    else:
        if query:
            found, total = answer_db.search(query)
            for e in found:
                rows.append({'key': e['key'], 'term': e['answer'], 'value': e['line'],
                             'source': e['source'], 'override': overrides_by_key.get(e['key'])})

    # Suppressed entries have no bundled row to attach to, so list them on their
    # own — otherwise a withdrawn entry would be invisible and unrecoverable.
    suppressed = [o for o in overrides_by_key.values() if o.suppressed]
    suppressed.sort(key=lambda o: o.key)

    return render(request, 'reference_data.html',
                  {'user': request.user.writer,
                   'dataset': dataset,
                   'is_pron': dataset == ReferenceDataOverride.PRONUNCIATION,
                   'query': query,
                   'rows': rows,
                   'result_total': total,
                   'shown': len(rows),
                   'suppressed': suppressed,
                   'override_count': len(overrides_by_key)})


def _reference_key(dataset, term):
    """The dataset's own lookup key for a headword, so an override lines up with
    the bundled entry it corrects."""
    from . import pron_dict, answer_db
    if dataset == ReferenceDataOverride.PRONUNCIATION:
        return pron_dict.normalize_term(term)
    return answer_db.norm_key(answer_db._plain(term))


def _reference_reset_caches():
    from . import pron_dict, answer_db, reference_quality
    pron_dict.reset_cache()
    answer_db.reset_cache()
    reference_quality.reset_cache()


@login_required
def reference_data_save(request, dataset):
    """Add or correct one reference entry. `key` is supplied when editing an
    existing entry; otherwise it's derived from the headword."""
    denied = _reference_admin_only(request)
    if denied is not None:
        return denied
    if request.method != 'POST':
        return HttpResponseRedirect('/reference_data/{0}/'.format(dataset))

    term = (request.POST.get('term') or '').strip()
    value = (request.POST.get('value') or '').strip()
    key = (request.POST.get('key') or '').strip() or _reference_key(dataset, term)

    if not key or not term or not value:
        messages.error(request, 'A term and its replacement text are both required.')
    else:
        ReferenceDataOverride.objects.update_or_create(
            dataset=dataset, key=key,
            defaults={'term': term, 'value': value, 'suppressed': False,
                      'note': (request.POST.get('note') or '').strip(),
                      'changed_by': request.user.writer})
        _reference_reset_caches()
        messages.success(request, 'Saved "{0}".'.format(term))

    nxt = (request.POST.get('next') or '').strip()
    if nxt.startswith('/reference_review/'):
        return HttpResponseRedirect(nxt)
    return HttpResponseRedirect('/reference_data/{0}/?q={1}'.format(
        dataset, quote(request.POST.get('q') or term)))


@login_required
def reference_data_suppress(request, dataset):
    """Withdraw a bundled entry so it stops firing, or restore one."""
    denied = _reference_admin_only(request)
    if denied is not None:
        return denied
    if request.method != 'POST':
        return HttpResponseRedirect('/reference_data/{0}/'.format(dataset))

    key = (request.POST.get('key') or '').strip()
    term = (request.POST.get('term') or '').strip()
    if key:
        if request.POST.get('restore'):
            # Dropping the row hands the entry back to the bundled file. An
            # entry that was *added* here has nothing to fall back to, so it
            # simply goes away — which is what restoring it should mean.
            ReferenceDataOverride.objects.filter(dataset=dataset, key=key).delete()
            messages.success(request, 'Restored "{0}" to the bundled data.'.format(term or key))
        else:
            ReferenceDataOverride.objects.update_or_create(
                dataset=dataset, key=key,
                defaults={'term': term, 'value': '', 'suppressed': True,
                          'note': (request.POST.get('note') or '').strip(),
                          'changed_by': request.user.writer})
            messages.success(request, 'Withdrew "{0}".'.format(term or key))
        _reference_reset_caches()

    nxt = (request.POST.get('next') or '').strip()
    if nxt.startswith('/reference_review/'):
        return HttpResponseRedirect(nxt)
    return HttpResponseRedirect('/reference_data/{0}/?q={1}'.format(
        dataset, quote(request.POST.get('q') or '')))
