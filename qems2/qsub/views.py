import json
import csv
import io
import math
import random
import zipfile
import unicodecsv
import time
import datetime
import sys
from collections import defaultdict

from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment

from django.shortcuts import render
from django.forms.formsets import formset_factory
from django.http import HttpResponse, HttpResponseRedirect

from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import *
from .forms import *
from .model_utils import *
from .utils import *
from .packet_parser import parse_packet_data
from .duplicate_checker import find_duplicates, find_internal_issues, find_topic_repeats, CRITICAL, WARNING, INFO
from django.utils.safestring import mark_safe
from haystack.query import SearchQuerySet
from django_comments.models import Comment
from django.db.models import Q
from django.core.cache import cache

from django.contrib.contenttypes.models import ContentType


@login_required
def main (request):
    return question_sets(request)

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
    
    # Sets that are in the future
    upcoming_sets = {}
    
    # Sets that are in the past
    completed_sets = {}
        
    for qset in (all_sets):
        if (qset.date >= datetime.now().date()):
            upcoming_sets[qset.id] = qset
        else:
            completed_sets[qset.id] = qset
            
    upcoming_sets = upcoming_sets
    completed_sets = completed_sets
            
    upcoming_sets = upcoming_sets.values()
    completed_sets = completed_sets.values()
    
    upcoming_sets = sorted(upcoming_sets, key=lambda qset: qset.date)
    completed_sets = sorted(completed_sets, key=lambda qset: qset.date)

    all_sets  = [{'header': 'Upcoming question sets', 'qsets': upcoming_sets, 'id': 'qsets-write'},
                 {'header': 'Completed question sets', 'qsets': completed_sets, 'id': 'qsets-complete'}]

    print(all_sets)
    return render(request, 'question_sets.html', {'question_set_list': all_sets, 'user': writer})

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
        form = ImportSetForm(request.POST, request.FILES)
        if form.is_valid():
            from .set_importer import import_set_from_file, SetImportError
            try:
                summary = import_set_from_file(
                    form.cleaned_data['set_file'], form.cleaned_data['set_name'], user)
                qset = summary['question_set']
                message = ('Imported "{0}": {1} tossups, {2} bonuses, {3} comments.'
                           .format(qset.name, summary['tossups'], summary['bonuses'],
                                   summary['comments']))
                if summary.get('users_created'):
                    message += ' Created {0} placeholder commenter account(s).'.format(summary['users_created'])
                message_class = 'alert-box success'
            except SetImportError as ex:
                message = str(ex)
                message_class = 'alert-box alert'
            except Exception as ex:
                message = 'Import failed: {0}'.format(ex)
                message_class = 'alert-box alert'
    else:
        form = ImportSetForm()

    return render(request, 'import_set.html',
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

@login_required
def create_question_set (request):
    user = request.user.writer

    if request.method == 'POST':
        form = QuestionSetForm(data=request.POST)
        if form.is_valid():
            # for the moment, just use the default ACF Distribution
            #dist = Distribution.objects.get(id=1)
            question_set = form.save(commit=False)
            question_set.owner = user
            question_set.editors = []
            question_set.editors.append(user)
            #question_set.distribution = dist
            question_set.save()
            form.save_m2m()
            user.question_set_editor.add(question_set)
            user.save()

            dist = question_set.distribution
            dist_entries = dist.distributionentry_set.all()
            for entry in dist_entries:
                set_wide_entry = SetWideDistributionEntry()
                set_wide_entry.num_bonuses = question_set.num_packets * entry.min_tossups
                set_wide_entry.num_tossups = question_set.num_packets * entry.min_bonuses
                set_wide_entry.question_set = question_set
                set_wide_entry.dist_entry = entry
                set_wide_entry.save()

                tiebreak_entry = TieBreakDistributionEntry()
                tiebreak_entry.num_bonuses = 1
                tiebreak_entry.num_tossups = 1
                tiebreak_entry.question_set = question_set
                tiebreak_entry.dist_entry = entry
                tiebreak_entry.save()

            set_distro_formset = create_set_distro_formset(question_set)
            tiebreak_formset = create_tiebreak_formset(question_set)
            comment_tab_list = []

            return render(request, 'edit_question_set.html',
                                      {'message': 'Your question set has been successfully created!',
                                       'message_class': 'alert-box success',
                                       'qset': question_set,
                                       'user': user,
                                       'form': form,
                                       'set_distro_formset': set_distro_formset,
                                       'tiebreak_formset': tiebreak_formset,
                                       'editors': [ed for ed in question_set.editor.all() if ed != question_set.owner],
                                       'writers': question_set.writer.all(),
                                       'tossups': Tossup.objects.filter(question_set=question_set),
                                       'bonuses': Bonus.objects.filter(question_set=question_set),
                                       'comment_tab_list': comment_tab_list,
                                       'packets': question_set.packet_set.all(),})
        else:
            print(form.errors)
            distributions = Distribution.objects.all()
            return render(request, 'create_question_set.html',
                                      {'message': 'There was an error in creating your question set!',
                                       'message_class': 'alert-box warning',
                                       'form': form,
                                       'distributions': distributions,
                                       'user': user})
    else:
        form = QuestionSetForm()
        distributions = Distribution.objects.all()

    return render(request, 'create_question_set.html',
                              {'form': form,
                               'distributions': distributions,
                               'user': user})

@login_required
def edit_question_set(request, qset_id):
    read_only = False
    message = ''
    tossups = []
    bonuses = []
    
    qset = QuestionSet.objects.get(id=qset_id)
    qset_editors = qset.editor.all()
    qset_writers = qset.writer.all()
    user = request.user.writer
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

    if request.method == 'POST':        
        if (qset.is_owner(user) or user in qset_editors):
            form = QuestionSetForm(data=request.POST)
            if form.is_valid():
                qset = QuestionSet.objects.get(id=qset_id)
                qset.name = form.cleaned_data['name']
                qset.date = form.cleaned_data['date']
                qset.distribution = form.cleaned_data['distribution']
                qset.num_packets = form.cleaned_data['num_packets']
                qset.char_count_ignores_pronunciation_guides = form.cleaned_data['char_count_ignores_pronunciation_guides']
                qset.max_acf_tossup_length = form.cleaned_data['max_acf_tossup_length']
                qset.max_acf_bonus_length = form.cleaned_data['max_acf_bonus_length']
                qset.max_vhsl_bonus_length = form.cleaned_data['max_vhsl_bonus_length']
                qset.save()
                cache.clear()

                tossups, tossup_dict, bonuses, bonus_dict = get_tossup_and_bonuses_in_set(qset, question_limit=30, preview_only=True)

                if qset.is_owner(user):
                    read_only = False
                else:
                    read_only = True

                set_status, total_tu_req, total_bs_req, tu_needed, bs_needed, set_pct_complete = get_questions_remaining(qset)
                writer_stats = get_writer_questions_remaining(qset, total_tu_req, total_bs_req)
                                                                
                comment_tab_list = get_comment_tab_list(tossup_dict, bonus_dict)

                return render(request, 'edit_question_set.html',
                                          {'form': form,
                                           'qset': qset,
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
                                           'packets': qset.packet_set.all(),
                                           'comment_list': comment_tab_list,
                                           'role': role,
                                           'message': 'Your changes have been successfully saved.',
                                           'message_class': 'alert-success'})
            else:
                qset_editors = []
        else:
            render(request, 'failure.html', {'message': 'You are not authorized to change this set!'})
    else:
        print("Begin edit_question_set get", time.strftime("%H:%M:%S"))
        if user not in qset_editors and not qset.is_owner(user) and user not in qset.writer.all():
            # Just redirect to main in this case of no permissions
            # TODO: a better story
            return HttpResponseRedirect('/main.html')

        tossups, tossup_dict, bonuses, bonus_dict = get_tossup_and_bonuses_in_set(qset, question_limit=30, preview_only=True)
        
        if user not in qset_editors and not qset.is_owner(user):
            form = QuestionSetForm(instance=qset, read_only=True)
            read_only = True
            message = ''
        else:
            if qset.is_owner(user):
                read_only = False
            elif user in qset.writer.all() or user in qset.editor.all():
                read_only = True
            form = QuestionSetForm(instance=qset)

        set_status, total_tu_req, total_bs_req, tu_needed, bs_needed, set_pct_complete = get_questions_remaining(qset)
        writer_stats = get_writer_questions_remaining(qset, total_tu_req, total_bs_req)
                                                                
        comment_tab_list = get_comment_tab_list(tossup_dict, bonus_dict)                    

    print("End edit_question_set get", time.strftime("%H:%M:%S"))
        
    return render(request, 'edit_question_set.html',
                              {'form': form,
                               'user': user,
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
                               'packets': qset.packet_set.all(),
                               'comment_tab_list': comment_tab_list,
                               'qset': qset,
                               'role': role,
                               'read_only': read_only,
                               'message': message})

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
        tossups = list(Tossup.objects.filter(question_set=qset).filter(category=category_id)
                       .select_related(*QUESTION_LIST_RELATED))
        bonuses = list(Bonus.objects.filter(question_set=qset).filter(category=category_id)
                       .select_related(*QUESTION_LIST_RELATED))
        attach_question_comments({t.id: t for t in tossups}, {b.id: b for b in bonuses})

    return render(request, 'categories.html',
        {
        'user': user,
        'tossups': tossups,
        'bonuses': bonuses,
        'category_status': category_status,
        'qset': qset,
        'message': message,
        'category': category_object})

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
def duplicate_check(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)

    if not qset.is_owner(user) and user not in qset.editor.all() and user not in qset.writer.all():
        messages.error(request, 'You are not authorized to view this set.')
        return render(request, 'failure.html',
                      {'message': 'You are not authorized to view this set.',
                       'message_class': 'alert-box alert'})

    groups = find_duplicates(qset)

    # Add text preview and similarity percentage to each group for the template
    for group in groups:
        for entry in group['entries']:
            text = strip_markup(entry['text'])
            entry['text_preview'] = text[:120] + '...' if len(text) > 120 else text
        for pair in group['pairs']:
            pair['similarity_pct'] = int(pair['similarity'] * 100)

    critical_count = sum(1 for g in groups if g['severity'] == CRITICAL)
    warning_count = sum(1 for g in groups if g['severity'] == WARNING)
    info_count = sum(1 for g in groups if g['severity'] == INFO)
    total_questions = sum(len(g['entries']) for g in groups)

    # Internal issues: bonus repeat answers, tossup clue reuse
    internal_issues = find_internal_issues(qset)
    bonus_repeat_count = sum(1 for i in internal_issues if i['issue_type'] == 'bonus_repeat_answer')
    clue_reuse_count = sum(1 for i in internal_issues if i['issue_type'] == 'tossup_clue_reuse')

    # Topic repeats: contained answers, shared rare terms, mentions
    topic_groups = find_topic_repeats(qset)
    topic_critical = sum(1 for g in topic_groups if g['severity'] == CRITICAL)
    topic_warning = sum(1 for g in topic_groups if g['severity'] == WARNING)
    topic_info = sum(1 for g in topic_groups if g['severity'] == INFO)

    return render(request, 'duplicate_check.html', {
        'qset': qset,
        'groups': groups,
        'critical_count': critical_count,
        'warning_count': warning_count,
        'info_count': info_count,
        'total_groups': len(groups),
        'total_questions': total_questions,
        'internal_issues': internal_issues,
        'bonus_repeat_count': bonus_repeat_count,
        'clue_reuse_count': clue_reuse_count,
        'topic_groups': topic_groups,
        'topic_critical': topic_critical,
        'topic_warning': topic_warning,
        'topic_info': topic_info,
    })

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
        comment_tab_list = get_comment_tab_list(tossup_dict, bonus_dict, comment_limit=10000)
            
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

            available_editors = [writer for writer in Writer.objects.all().order_by('user__last_name', 'user__first_name', 'user__username') #exclude(is_active=False)
                                 if writer not in current_editors and
                                    not qset.is_owner(writer) and writer.id != 1
                                    and writer.user.is_active]
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
                available_editors = [writer for writer in Writer.objects.all().order_by('user__last_name', 'user__first_name', 'user__username') #exclude(is_active=False)
                                     if writer not in set_editors and
                                        not qset.is_owner(writer) and writer.id != 1
                                        and writer.user.is_active]
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
        return [writer for writer in Writer.objects.all().order_by('user__last_name', 'user__first_name', 'user__username')
                if writer not in current_owners and writer.id != 1 and writer.user.is_active]

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
            available_writers = [writer for writer in Writer.objects.all().order_by('user__last_name', 'user__first_name', 'user__username') #exclude(is_active=False)
                                 if writer not in set_writers and
                                    not qset.is_owner(writer) and writer.id != 1
                                    and writer.user.is_active]
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
                qset.save()
                cache.clear()
                set_writers = Writer.objects.filter(Q(question_set_writer=qset) | Q(question_set_editor=qset)).distinct().order_by('user__last_name', 'user__first_name', 'user__username')
                available_writers = [writer for writer in Writer.objects.all().order_by('user__last_name', 'user__first_name', 'user__username') #exclude(is_active=False)
                                     if writer not in set_writers and
                                        not qset.is_owner(writer) and writer.id != 1
                                        and writer.user.is_active]
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


@login_required
def edit_packet(request, packet_id):
    user = request.user.writer
    packet = Packet.objects.get(id=packet_id)
    qset = packet.question_set
    message = ''
    message_class = ''
    read_only = True
    tossup_status = {}
    bonus_status = {}

    if request.method == 'GET':
        if qset.is_owner(user) or user in qset.editor.all() or user in qset.writer.all():
            tossups = packet.tossup_set.order_by('question_number').all()
            bonuses = packet.bonus_set.order_by('question_number').all()
            if user not in qset.writer.all():
                read_only = False

            # Per-packet requirements by top-level category: use the
            # packetization quotas when defined, otherwise the set-wide
            # totals divided by the packet count
            num_packets = max(qset.num_packets, 1)
            quota_by_path = {e.path: e for e in PacketizationEntry.objects.filter(question_set=qset, depth=0)}

            top_totals = {}
            for swde in qset.setwidedistributionentry_set.select_related('dist_entry'):
                top = swde.dist_entry.category
                totals = top_totals.setdefault(top, [0, 0])
                totals[0] += swde.num_tossups or 0
                totals[1] += swde.num_bonuses or 0

            for top, (tu_total, bs_total) in top_totals.items():
                quota = quota_by_path.get(top)
                if quota is not None and quota.min_tossups is not None:
                    tossups_required = float(quota.min_tossups)
                else:
                    tossups_required = round(tu_total / float(num_packets), 1)
                if quota is not None and quota.min_bonuses is not None:
                    bonuses_required = float(quota.min_bonuses)
                else:
                    bonuses_required = round(bs_total / float(num_packets), 1)

                tu_in_cat = Tossup.objects.filter(packet=packet, category__category=top).count()
                bs_in_cat = Bonus.objects.filter(packet=packet, category__category=top).count()
                tossup_status[top] = {'tu_req': tossups_required,
                                      'tu_in_cat': tu_in_cat}
                bonus_status[top] = {'bs_req': bonuses_required,
                                     'bs_in_cat': bs_in_cat}


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
         'read_only': read_only,
         'user': user})

@login_required
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
            if user in qset.writer.all() and user not in qset.editor.all() and not qset.is_owner(user):
                tossup_form = TossupForm(qset_id=qset.id, packet_id=packet_id, role='writer', writer=user.user.username, initial={'question_type': question_type_id})
            else:
                tossup_form = TossupForm(qset_id=qset.id, packet_id=packet_id, writer=user.user.username, initial={'question_type': question_type_id})
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
                tossup.tossup_answer = strip_markup(tossup.tossup_answer)
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
                        tossup.question_number = Tossup.objects.filter(packet_id=packet_id).count() + 1

                    tossup.save_question(edit_type=QUESTION_CREATE, changer=user)
                    cache.clear()
                    message = 'Your tossup has been added to the set.'
                    message_class = 'alert-box info radius'

                    # In the success case, don't return the whole tossup object so as to clear the fields
                    return render(request, 'add_tossups.html',
                             {'form': TossupForm(qset_id=qset.id, packet_id=packet_id, initial={'question_type': question_type_id}, writer=user.user.username),
                             'message': message,
                             'message_class': message_class,
                             'tossup' : None,
                             'tossup_id': tossup.id,
                             'read_only': read_only,
                             'user': user,
                             'qset': qset})

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
            tossup_form = TossupForm(qset_id=qset.id, packet_id=packet_id, initial={'question_type': question_type_id})

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
            form = BonusForm(qset_id=qset.id, packet_id=packet_id, role=role, initial={'question_type': question_type_id}, writer=user.user.username, question_type=bonus_type)
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
                bonus.part1_answer = strip_markup(bonus.part1_answer)
                bonus.part2_text = strip_markup(bonus.part2_text)
                bonus.part2_answer = strip_markup(bonus.part2_answer)
                bonus.part3_text = strip_markup(bonus.part3_text)
                bonus.part3_answer = strip_markup(bonus.part3_answer)
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
                    bonus.question_number = Bonus.objects.filter(packet_id=packet_id).count() + 1

                try:
                    bonus.is_valid()
                    bonus.save_question(edit_type=QUESTION_CREATE, changer=user)
                    cache.clear()
                    message = 'Your bonus has been added to the set.'
                    message_class = 'alert-box success'

                    # On success case, don't return the full bonus so that field gets cleared
                    return render(request, 'add_bonuses.html',
                             {'form': BonusForm(qset_id=qset.id, packet_id=packet_id, initial={'question_type': question_type_id}, writer=user.user.username, question_type=bonus_type),
                             'message': message,
                             'message_class': message_class,
                             'bonus': None,
                             'bonus_id': bonus.id,
                             'read_only': read_only,
                             'question_type': bonus_type,
                             'user': user,
                             'qset': qset})

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
            bonus_form = BonusForm(qset_id=qset.id, packet_id=packet_id, initial={'question_type': question_type_id}, writer=user.user.username)

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

        elif user in qset.writer.all():
            read_only = True
            form = None
        else:
            read_only = True
            tossup = None
            form = None
            message = 'You are not authorized to view or edit this question!'
            message_class = 'alert-box alert'

        return render(request, 'edit_tossup.html',
            {'tossup': tossup,
             'tossup_length': tossup_length,
             'form': form,
             'qset': qset,
             'packet': packet,
             'available_tags': build_tag_checkboxes(qset, tossup, tossup.category if tossup else None),
             'message': message,
             'message_class': message_class,
             'read_only': read_only,
             'role': role,
             'user': user})

    elif request.method == 'POST':
        print("start post for edit tossup")
        if user == tossup.author or qset.is_owner(user) or user in qset.editor.all():
            form = TossupForm(request.POST, qset_id=qset.id, role=role)
            can_change = True
            if tossup.locked and not (qset.is_owner(user) or user in qset.editor.all()):
                can_change = False

            if form.is_valid() and can_change:
                read_only = False

                is_tossup_already_edited = tossup.edited
                is_tossup_already_proofread = tossup.proofread
                is_tossup_already_read_carefully = tossup.read_carefully

                tossup.tossup_text = strip_markup(form.cleaned_data['tossup_text'])
                tossup.tossup_answer = strip_markup(form.cleaned_data['tossup_answer'])
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
             'tossup_length': tossup_length,
             'form': form,
             'role': role,
             'qset': qset,
             'packet': packet,
             'available_tags': build_tag_checkboxes(qset, tossup, tossup.category if tossup else None),
             'message': message,
             'message_class': message_class,
             'read_only': read_only,
             'user': user})

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

        elif user in qset.writer.all():
            read_only = True
            form = None
        else:
            read_only = True
            bonus = None
            form = None
            message = 'You are not authorized to view or edit this question!'
            message_class = 'alert-box alert'

        return render(request, 'edit_bonus.html',
            {'bonus': bonus,
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
             'user': user})

    elif request.method == 'POST':
        if user == bonus.author or qset.is_owner(user) or user in qset.editor.all():
            form = BonusForm(request.POST, qset_id=qset.id, role=role, question_type=question_type)
            
            can_change = True
            if bonus.locked and not (qset.is_owner(user) or user in qset.editor.all()):
                can_change = False

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
             'user': user})

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
        role = get_role_no_owner(user, qset)
        if role == "editor":
            qset.delete()
            cache.clear()
            message = 'Set deleted'
            message_class = 'alert-box success'
        else:
            message = 'You are not authorized to delete this set!'
            message_class = 'alert-box warning'

    return HttpResponse(json.dumps({'message': message, 'message_class': message_class}))

@login_required
def delete_comment(request):
    user = request.user.writer
    message = ''
    message_class = ''
    read_only = True

    if request.method == 'POST':
        qset_id = request.POST['qset_id']
        qset = QuestionSet.objects.get(id=qset_id)
        qset_editors = qset.editor.all()
        comment_id = request.POST['comment_id']
        comment = Comment.objects.get(id=comment_id)

        if (comment is None):
            message = 'Error retrieving comment.'
            message_class = 'alert-box warning'
        else:
            if user in qset_editors:
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
            qset = QuestionSet.objects.get(id=qset_id)
        except (Comment.DoesNotExist, QuestionSet.DoesNotExist):
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
def delete_all_comments(request):
    user = request.user.writer
    message = ''
    message_class = ''
    read_only = True

    tossup_content_type_id = ContentType.objects.get_for_model(Tossup).id
    bonus_content_type_id = ContentType.objects.get_for_model(Bonus).id

    if request.method == 'POST':
        qset_id = request.POST['qset_id']
        qset = QuestionSet.objects.get(id=qset_id)
        qset_editors = qset.editor.all()
        question_type = request.POST['question_type']
        question_id = request.POST['question_id']

        if (question_type == 'tossup'):
            comment_list = Comment.objects.filter(content_type_id=tossup_content_type_id).filter(object_pk=question_id).order_by('submit_date')
        else:
            comment_list = Comment.objects.filter(content_type_id=bonus_content_type_id).filter(object_pk=question_id).order_by('submit_date')

        if (comment_list is None):
            message = 'Error retrieving comments.'
            message_class = 'alert-box warning'
        else:
            if user in qset_editors:
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
                tossup.question_number = Tossup.objects.filter(packet_id=packet_id).count() + 1
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
                bonus.question_number = Bonus.objects.filter(packet_id=packet_id).count() + 1
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

@login_required
def distributions (request):

    data = []
    all_dists = Distribution.objects.all()

    return render(request, 'distributions.html',
                             {'dists': all_dists,
                              'user': request.user.writer})

@login_required
def clone_distribution(request, dist_id):
    if request.method != 'POST':
        return HttpResponseRedirect('/distributions/')

    source = Distribution.objects.get(id=dist_id)
    new_dist = Distribution()
    new_dist.name = source.name + ' (Copy)'
    new_dist.acf_tossup_per_period_count = source.acf_tossup_per_period_count
    new_dist.acf_bonus_per_period_count = source.acf_bonus_per_period_count
    new_dist.vhsl_bonus_per_period_count = source.vhsl_bonus_per_period_count
    new_dist.save()

    for entry in DistributionEntry.objects.filter(distribution=source):
        DistributionEntry.objects.create(
            distribution=new_dist,
            category=entry.category,
            subcategory=entry.subcategory,
            min_tossups=entry.min_tossups,
            min_bonuses=entry.min_bonuses,
            max_tossups=entry.max_tossups,
            max_bonuses=entry.max_bonuses,
        )

    return HttpResponseRedirect('/edit_distribution/' + str(new_dist.id) + '/')

@login_required
def edit_distribution(request, dist_id=None):

    data = []
    message = ''
    message_class = ''

    if request.user.is_authenticated:
        DistributionEntryFormset = formset_factory(DistributionEntryForm, can_delete=True)
        if request.method == 'POST':
            # no dist_id supplied means new dist
            if dist_id is None:
                formset = DistributionEntryFormset(data=request.POST, prefix='distentry')
                dist_form = DistributionForm(data=request.POST)
                if dist_form.is_valid() and formset.is_valid():
                    new_dist = Distribution()
                    new_dist.name = dist_form.cleaned_data['name']
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
                        dist.save()

                        qsets = dist.questionset_set.all()
                        for form in formset:
                            if form.cleaned_data != {}:
                                if form.cleaned_data['entry_id'] is not None:
                                    entry_id = int(form.cleaned_data['entry_id'])
                                    entry = DistributionEntry.objects.get(id=entry_id)
                                    if form.cleaned_data['DELETE']:
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

                        entries = dist.distributionentry_set.all()
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

            return render(request, 'edit_distribution.html',
                                     {'form': dist_form,
                                      'formset': formset,
                                      'message': message,
                                      'message_class': message_class,
                                      'user': request.user.writer})
        else:
            if dist_id is not None:
                dist = Distribution.objects.get(id=dist_id)
                entries = dist.distributionentry_set.all()
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

            return render(request, 'edit_distribution.html',
                                     {'form': dist_form,
                                      'formset': formset,
                                      'message': message,
                                      'message_class': message_class,
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

                return render(request, 'type_questions_preview.html',
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

        new_tossups = []
        new_bonuses = []

        for tu_num in range(num_tossups):
            data="UTF-8 DATA"
            tu_text_name = 'tossup-text-{0}'.format(tu_num)
            tu_ans_name = 'tossup-answer-{0}'.format(tu_num)
            tu_cat_name = 'tossup-category-{0}'.format(tu_num)
            tu_type_name = 'tossup-type-{0}'.format(tu_num)

            tu_text = strip_markup(request.POST[tu_text_name])
            tu_ans = strip_markup(request.POST[tu_ans_name])
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
            new_bonus.part1_answer = strip_markup(request.POST[bs_ans1_name])
            new_bonus.part2_text = strip_markup(request.POST[bs_part2_name])
            new_bonus.part2_answer = strip_markup(request.POST[bs_ans2_name])
            new_bonus.part3_text = strip_markup(request.POST[bs_part3_name])
            new_bonus.part3_answer = strip_markup(request.POST[bs_ans3_name])
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
            new_bonuses.append(new_bonus)

        cache.clear()
        messages.success(request, 'Your questions have been uploaded.', extra_tags='alert-box success')        
        for tossup in new_tossups:
            messages.success(request, u'View your tossup on <a href="/edit_tossup/{0}">{1}.</a>'.format(tossup.id, get_answer_no_formatting(tossup.tossup_answer)), extra_tags='safe alert-box info')

        for bonus in new_bonuses:
            messages.success(request, u'View your bonus on <a href="/edit_bonus/{0}">{1}.</a>'.format(bonus.id, get_answer_no_formatting(bonus.part1_answer)), extra_tags='safe alert-box info')

        return HttpResponseRedirect('/edit_question_set/{0}'.format(qset_id))

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
                        'send_mail_on_comments': writer.send_mail_on_comments}

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
                if search_all_selected == 'checked':
                    search_query_set = SearchQuerySet().filter(question_set__in=question_sets)
                else:
                    search_query_set = SearchQuerySet().filter(question_set=qset)
                search_query_set = search_query_set.filter(Q(question_answers=query) | Q(question_content=query))

                if 'qsub.tossup' in search_models and 'qsub.bonus' not in search_models:
                    result_ids = [r.id for r in search_query_set.models(Tossup)]
                elif 'qsub.bonus' in search_models and 'qsub.tossup' not in search_models:
                    result_ids = [r.id for r in search_query_set.models(Bonus)]
                elif 'qsub.tossup' in search_models and 'qsub.bonus' in search_models:
                    result_ids = [r.id for r in search_query_set.models(Tossup, Bonus)]
                else:
                    result_ids = []

                # One bulk fetch per model instead of a query per result
                parsed_ids = []
                tossup_ids = []
                bonus_ids = []
                for q_id in result_ids:
                    try:
                        fields = q_id.split('.')
                        question_type = fields[1]
                        question_id = int(fields[2])
                    except (IndexError, ValueError):
                        print("Error parsing search result id", q_id)
                        continue
                    parsed_ids.append((question_type, question_id))
                    if question_type == 'tossup':
                        tossup_ids.append(question_id)
                    elif question_type == 'bonus':
                        bonus_ids.append(question_id)

                questions_by_key = {}
                for tossup in Tossup.objects.filter(id__in=tossup_ids).select_related(*QUESTION_LIST_RELATED):
                    questions_by_key[('tossup', tossup.id)] = tossup
                for bonus in Bonus.objects.filter(id__in=bonus_ids).select_related(*QUESTION_LIST_RELATED):
                    questions_by_key[('bonus', bonus.id)] = bonus

                questions = []
                for key in parsed_ids:
                    question = questions_by_key.get(key)
                    if question is None:
                        continue
                    if str(question.category) == search_category or search_category == 'All':
                        questions.append(question)

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
                                       'passed_q_set': passed_q_set,
                                       'message': message,
                                       'message_class': message_class})

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

    move_sets = user.question_set_editor.exclude(id=q_set_id)

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
                    tossup.question_set = dest_qset
                    tossup.packet = None

                    tossup.save()
                    cache.clear()
                    message = "Successfully moved tossup to " + str(dest_qset)
                    message_class = 'alert-box success'
                    return render(request, 'move_tossup_success.html',
                                        {'user': user,
                                         'q_set': q_set,
                                         'dest_q_set': dest_qset,
                                         'tossup': tossup,
                                         'message': message,
                                         'message_class': message_class})
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

    move_sets = user.question_set_editor.exclude(id=q_set_id)

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
                    bonus.question_set = dest_qset
                    bonus.packet = None

                    bonus.save()
                    cache.clear()
                    return render(request, 'move_bonus_success.html',
                                        {'user': user,
                                         'q_set': q_set,
                                         'dest_q_set': dest_qset,
                                         'bonus': bonus})
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

def add_qems_formatted_runs(paragraph, text, bold=False, is_answer=False):
    """Convert QEMS markup to python-docx runs on a paragraph.

    Markup rules (mirroring get_formatted_question_html in utils.py):
      _text_   → bold + underline (answer line)
      __text__ → underline only (prompt)
      ~text~   → italic
      (text)   → bold (pronunciation guide)
      (*)      → bold (power mark)
      \\s / \\S  → subscript / superscript (approximated with smaller font)
    """
    if text is None:
        return paragraph

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
    need_restore_italics = False

    if allow_powers:
        power_index = text.find("(*)")
        if power_index > -1:
            power_flag = True

    buf = ""
    # Current formatting state
    cur_bold = bold or power_flag
    cur_italic = False
    cur_underline = False
    cur_sub = False
    cur_super = False

    def flush(b=cur_bold, i=cur_italic, u=cur_underline, sub=cur_sub, sup=cur_super):
        nonlocal buf
        if buf:
            run = paragraph.add_run(buf)
            run.bold = b
            run.italic = i
            run.underline = u
            if sub:
                run.font.subscript = True
            if sup:
                run.font.superscript = True
            buf = ""

    def current_state():
        b = bold or power_flag or parens_flag or underline_flag
        i = italics_flag
        u = underline_flag or prompt_flag
        return b, i, u, sub_flag, super_flag

    index = 0
    prev = ""
    prev2 = ""

    while index < len(text):
        c = text[index]
        next_c = text[index + 1] if index < len(text) - 1 else ""

        new_bold, new_italic, new_underline, new_sub, new_sup = current_state()

        # Power mark
        if index == power_index and power_flag:
            flush(new_bold, new_italic, new_underline, new_sub, new_sup)
            run = paragraph.add_run("(*)")
            run.bold = True
            power_flag = False
            index += 3
            prev2, prev = prev, ")"
            continue

        # Tildes → italic toggle
        if c == "~":
            flush(new_bold, new_italic, new_underline, new_sub, new_sup)
            italics_flag = not italics_flag
            index += 1
            prev2, prev = prev, c
            continue

        # Open paren (pronunciation guide)
        if c == "(" and allow_parens and prev != "\\":
            flush(new_bold, new_italic, new_underline, new_sub, new_sup)
            if italics_flag:
                need_restore_italics = True
                italics_flag = False
            if not power_flag:
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
            new_b, new_i, new_u, new_sub2, new_sup2 = current_state()
            flush(new_b, new_i, new_u, new_sub2, new_sup2)
            if not power_flag:
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

                csv_writer.writerow(["Tossup Question", "Answer", "Category", "Author", "Edited", "Packet", "Question Number", "Comments","Id", "Editor", "Proofreader", "Read Carefully"])
                for tossup in tossups:
                    comment_string = build_comment_string(tossup_content_type_id, tossup.id)

                    editor_name = ""
                    if tossup.edited and tossup.editor is not None:
                        editor_name = safe_name(tossup.editor)

                    proofreader_name = ""
                    if tossup.proofread and tossup.proofreader is not None:
                        proofreader_name = safe_name(tossup.proofreader)

                    csv_writer.writerow([safe_text(tossup.tossup_text), safe_text(tossup.tossup_answer), safe_category(tossup.category), safe_name(tossup.author), tossup.edited, safe_packet(tossup.packet), tossup.question_number, safe_text(comment_string), tossup.id, editor_name, proofreader_name, tossup.read_carefully])

                csv_writer.writerow([])

                csv_writer.writerow(["Bonus Leadin", "Bonus Part 1", "Bonus Answer 1", "Part 1 Difficulty", "Bonus Part 2", "Bonus Answer 2", "Part 2 Difficulty", "Bonus Part 3", "Bonus Answer 3", "Part 3 Difficulty", "Category", "Author", "Edited", "Packet", "Question Number", "Comments", "Id", "Editor", "Proofreader", "Read Carefully"])
                for bonus in bonuses:
                    comment_string = build_comment_string(bonus_content_type_id, bonus.id)

                    editor_name = ""
                    if bonus.edited and bonus.editor is not None:
                        editor_name = safe_name(bonus.editor)

                    proofreader_name = ""
                    if bonus.proofread and bonus.proofreader is not None:
                        proofreader_name = safe_name(bonus.proofreader)

                    csv_writer.writerow([safe_text(bonus.leadin), safe_text(bonus.part1_text), safe_text(bonus.part1_answer), bonus.part1_difficulty, safe_text(bonus.part2_text), safe_text(bonus.part2_answer), bonus.part2_difficulty, safe_text(bonus.part3_text), safe_text(bonus.part3_answer), bonus.part3_difficulty, safe_category(bonus.category), safe_name(bonus.author), bonus.edited, safe_packet(bonus.packet), bonus.question_number, safe_text(comment_string), bonus.id, editor_name, proofreader_name, bonus.read_carefully])

                csv_writer.writerow([])
                entries = qset.setwidedistributionentry_set.all()
                csv_writer.writerow(["Category", "Subcategory", "Total Tossups", "Total Bonuses"])
                for entry in entries:
                    csv_writer.writerow([entry.dist_entry.category, entry.dist_entry.subcategory, entry.num_tossups, entry.num_bonuses])

                return response
            elif output_format in ("docx", "docx-by-category", "docx-packetized"):
                def question_meta(q):
                    """Build metadata line: category, writer, editor."""
                    parts = []
                    if q.category:
                        parts.append(safe_category(q.category))
                    parts.append(safe_name(q.author))
                    if q.edited and q.editor:
                        parts.append("Ed. " + safe_name(q.editor))
                    return " | ".join(p for p in parts if p)

                def new_docx():
                    document = Document()
                    style = document.styles['Normal']
                    style.font.size = Pt(10)
                    style.font.name = 'Times New Roman'
                    style.paragraph_format.space_before = Pt(0)
                    style.paragraph_format.space_after = Pt(0)
                    return document

                def add_tossup_to_doc(document, tossup, num):
                    p = document.add_paragraph()
                    p.add_run(f"{num}. ").bold = True
                    add_qems_formatted_runs(p, safe_text(tossup.tossup_text))
                    p_ans = document.add_paragraph()
                    p_ans.add_run("ANSWER: ").bold = True
                    add_qems_formatted_runs(p_ans, safe_text(tossup.tossup_answer), is_answer=True)
                    meta = question_meta(tossup)
                    if meta:
                        p_meta = document.add_paragraph()
                        run = p_meta.add_run(meta)
                        run.font.size = Pt(8)
                        run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

                def add_bonus_to_doc(document, bonus, num):
                    p = document.add_paragraph()
                    p.add_run(f"{num}. ").bold = True
                    add_qems_formatted_runs(p, safe_text(bonus.leadin))
                    for part_num in range(1, 4):
                        part_text = getattr(bonus, f'part{part_num}_text', None)
                        part_answer = getattr(bonus, f'part{part_num}_answer', None)
                        part_diff = getattr(bonus, f'part{part_num}_difficulty', '')
                        if part_text:
                            diff_tag = part_diff if part_diff else ''
                            p_part = document.add_paragraph()
                            p_part.add_run(f"[10{diff_tag}] ").bold = True
                            add_qems_formatted_runs(p_part, safe_text(part_text))
                            p_ans = document.add_paragraph()
                            p_ans.add_run("ANSWER: ").bold = True
                            add_qems_formatted_runs(p_ans, safe_text(part_answer), is_answer=True)
                    meta = question_meta(bonus)
                    if meta:
                        p_meta = document.add_paragraph()
                        run = p_meta.add_run(meta)
                        run.font.size = Pt(8)
                        run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

                def save_docx_bytes(document):
                    buf = io.BytesIO()
                    document.save(buf)
                    return buf.getvalue()

                def write_all_questions_to_doc(document, tossup_qs, bonus_qs):
                    """Write tossups then bonuses to a document."""
                    if tossup_qs.exists():
                        document.add_heading('Tossups', level=2)
                        for i, tossup in enumerate(tossup_qs, 1):
                            num = tossup.question_number if tossup.question_number else i
                            add_tossup_to_doc(document, tossup, num)
                    if bonus_qs.exists():
                        document.add_heading('Bonuses', level=2)
                        for i, bonus in enumerate(bonus_qs, 1):
                            num = bonus.question_number if bonus.question_number else i
                            add_bonus_to_doc(document, bonus, num)

                if output_format == "docx":
                    document = new_docx()

                    packets = Packet.objects.filter(question_set=qset).order_by('packet_name')
                    for packet in packets:
                        document.add_heading(packet.packet_name, level=1)
                        tossups = Tossup.objects.filter(
                            packet=packet, question_set=qset
                        ).order_by('question_number')
                        bonuses = Bonus.objects.filter(
                            packet=packet, question_set=qset
                        ).order_by('question_number')
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
                    dist = qset.distribution
                    tu_per_packet = dist.acf_tossup_per_period_count
                    bo_per_packet = dist.acf_bonus_per_period_count

                    all_tossups = list(
                        Tossup.objects.filter(question_set=qset)
                        .select_related('category', 'author', 'editor')
                    )
                    all_bonuses = list(
                        Bonus.objects.filter(question_set=qset)
                        .select_related('category', 'author', 'editor')
                    )

                    def get_top_cat(q):
                        return q.category.category if q.category else 'Uncategorized'

                    def deal_into_packets(questions, per_packet):
                        """Deal questions into packets with even category spread.

                        Computes per-packet targets from actual question counts,
                        then uses fractional accumulation to distribute each
                        category evenly across packets.
                        """
                        # Bucket by top-level category
                        by_cat = defaultdict(list)
                        for q in questions:
                            by_cat[get_top_cat(q)].append(q)
                        # Shuffle within each category
                        for cat_list in by_cat.values():
                            random.shuffle(cat_list)

                        n_packets = max(1, math.ceil(len(questions) / per_packet)) if per_packet else 1
                        packets = [[] for _ in range(n_packets)]

                        # Compute per-packet target from actual counts
                        cat_per_pkt = {}
                        for cat, qs in by_cat.items():
                            cat_per_pkt[cat] = len(qs) / n_packets

                        # Sort categories: largest quota first for best packing
                        sorted_cats = sorted(
                            by_cat.keys(),
                            key=lambda c: cat_per_pkt.get(c, 0),
                            reverse=True
                        )

                        # Track fractional accumulator per category
                        accum = {cat: 0.0 for cat in sorted_cats}

                        for pkt_idx in range(n_packets):
                            remaining = per_packet
                            for cat in sorted_cats:
                                if not by_cat[cat] or remaining <= 0:
                                    continue
                                accum[cat] += cat_per_pkt[cat]
                                take = min(int(round(accum[cat])), remaining, len(by_cat[cat]))
                                accum[cat] -= take
                                for _ in range(take):
                                    packets[pkt_idx].append(by_cat[cat].pop(0))
                                    remaining -= 1

                            # If packet not full, fill from largest remaining pools
                            if remaining > 0:
                                for cat in sorted(sorted_cats, key=lambda c: len(by_cat[c]), reverse=True):
                                    while by_cat[cat] and remaining > 0:
                                        packets[pkt_idx].append(by_cat[cat].pop(0))
                                        remaining -= 1

                        # Any leftovers go into the last packet
                        leftovers = []
                        for cat in sorted_cats:
                            leftovers.extend(by_cat[cat])
                        if leftovers:
                            packets[-1].extend(leftovers)

                        return packets

                    def order_for_packet(questions):
                        """Order questions within a packet for good quizbowl flow.

                        Goals:
                        - No back-to-back same category
                        - Major categories (>=2 questions) spread across both halves
                        - Maximize distance between same-category questions
                        Uses a greedy slot-filling approach with Monte Carlo fallback.
                        """
                        if len(questions) <= 2:
                            return questions

                        by_cat = defaultdict(list)
                        for q in questions:
                            by_cat[get_top_cat(q)].append(q)

                        n = len(questions)
                        half = n // 2

                        # Sort categories by count descending (major cats first)
                        sorted_cats = sorted(by_cat.keys(), key=lambda c: len(by_cat[c]), reverse=True)

                        # Pre-assign slots for major categories to ensure spread
                        slots = [None] * n
                        used = set()

                        for cat in sorted_cats:
                            cat_qs = by_cat[cat]
                            count = len(cat_qs)
                            if count >= 2:
                                # Spread evenly: place one in each segment
                                segment_size = n / count
                                for i, q in enumerate(cat_qs):
                                    ideal = int(i * segment_size + segment_size / 2)
                                    # Find nearest free slot
                                    best_slot = None
                                    best_dist = n + 1
                                    for s in range(n):
                                        if s in used:
                                            continue
                                        d = abs(s - ideal)
                                        if d < best_dist:
                                            best_dist = d
                                            best_slot = s
                                    if best_slot is not None:
                                        slots[best_slot] = q
                                        used.add(best_slot)

                        # Place remaining single-category questions in free slots
                        free_slots = [i for i in range(n) if i not in used]
                        remaining_qs = []
                        for cat in sorted_cats:
                            for q in by_cat[cat]:
                                if q not in [s for s in slots if s is not None]:
                                    remaining_qs.append(q)
                        random.shuffle(remaining_qs)
                        for slot, q in zip(free_slots, remaining_qs):
                            slots[slot] = q

                        # Monte Carlo refinement: swap to reduce back-to-back
                        def score(order):
                            """Higher is better: sum of distances between same-cat questions."""
                            total = 0
                            last = {}
                            for i, q in enumerate(order):
                                c = get_top_cat(q)
                                if c in last:
                                    total += i - last[c]
                                last[c] = i
                            # Penalty for adjacent same-category
                            for i in range(len(order) - 1):
                                if get_top_cat(order[i]) == get_top_cat(order[i + 1]):
                                    total -= 10
                            return total

                        best = list(slots)
                        best_score = score(best)
                        for _ in range(200):
                            candidate = list(best)
                            i, j = random.sample(range(n), 2)
                            candidate[i], candidate[j] = candidate[j], candidate[i]
                            s = score(candidate)
                            if s > best_score:
                                best = candidate
                                best_score = s

                        return best

                    tu_packets = deal_into_packets(all_tossups, tu_per_packet)
                    bo_packets = deal_into_packets(all_bonuses, bo_per_packet)

                    # Order within each packet
                    tu_packets = [order_for_packet(p) for p in tu_packets]
                    bo_packets = [order_for_packet(p) for p in bo_packets]

                    # Ensure same number of packets for tossups and bonuses
                    max_packets = max(len(tu_packets), len(bo_packets))
                    while len(tu_packets) < max_packets:
                        tu_packets.append([])
                    while len(bo_packets) < max_packets:
                        bo_packets.append([])

                    # Build answer matrix workbook
                    wb = Workbook()

                    # Tossup answers sheet
                    ws_tu = wb.active
                    ws_tu.title = "Tossup Answers"
                    header_font = Font(bold=True)
                    wrap = Alignment(wrap_text=True, vertical='top')
                    max_tu = max((len(p) for p in tu_packets), default=0)
                    # Header row
                    ws_tu.cell(row=1, column=1, value="Packet").font = header_font
                    for col in range(1, max_tu + 1):
                        ws_tu.cell(row=1, column=col + 1, value=col).font = header_font
                    for pkt_idx, tus in enumerate(tu_packets):
                        row = pkt_idx + 2
                        ws_tu.cell(row=row, column=1, value=f"Packet {pkt_idx + 1}").font = header_font
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
                    max_bo = max((len(p) for p in bo_packets), default=0)
                    ws_bo.cell(row=1, column=1, value="Packet").font = header_font
                    for col in range(1, max_bo + 1):
                        ws_bo.cell(row=1, column=col + 1, value=col).font = header_font
                    for pkt_idx, bos in enumerate(bo_packets):
                        row = pkt_idx + 2
                        ws_bo.cell(row=row, column=1, value=f"Packet {pkt_idx + 1}").font = header_font
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

                    # Package everything into a zip
                    zip_buf = io.BytesIO()
                    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                        for pkt_idx in range(max_packets):
                            document = new_docx()
                            document.add_heading(f"Packet {pkt_idx + 1}", level=1)

                            tus = tu_packets[pkt_idx]
                            if tus:
                                document.add_heading('Tossups', level=2)
                                for i, tossup in enumerate(tus, 1):
                                    add_tossup_to_doc(document, tossup, i)

                            bos = bo_packets[pkt_idx]
                            if bos:
                                document.add_heading('Bonuses', level=2)
                                for i, bonus in enumerate(bos, 1):
                                    add_bonus_to_doc(document, bonus, i)

                            zf.writestr(f"Packet {pkt_idx + 1}.docx",
                                        save_docx_bytes(document))

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
            elif (output_format == "pdf"):
                # TODO: Experiment with one of those PDF libraries
                message = 'Not supported yet.'
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
        tossup_history = TossupHistory.objects.get(id=th_id)
        tossup = Tossup.objects.get(question_history=tossup_history.question_history)
        if (tossup_history is None):
            message = 'Invalid tossup history restoration!'
            message_class = 'alert-box warning'
        else:
            qset_id = request.POST['qset_id']
            qset = QuestionSet.objects.get(id=qset_id)
            if user == tossup.author or qset.is_owner(user) or user in qset.editor.all():
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
        bonus_history = BonusHistory.objects.get(id=bh_id)
        bonus = Bonus.objects.get(question_history=bonus_history.question_history)
        if (bonus_history is None):
            message = 'Invalid bonus history restoration!'
            message_class = 'alert-box warning'
        else:
            qset_id = request.POST['qset_id']
            qset = QuestionSet.objects.get(id=qset_id)
            if user == bonus.author or qset.is_owner(user) or user in qset.editor.all():
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
        tossup = Tossup.objects.get(id=tossup_id)
        if (tossup is None):
            message = 'Invalid tossup!'
            message_class = 'alert-box warning'
        else:
            qset_id = request.POST['qset_id']
            qset = QuestionSet.objects.get(id=qset_id)
            if user == tossup.author or qset.is_owner(user) or user in qset.editor.all():
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
        bonus = Bonus.objects.get(id=bonus_id)
        if (bonus is None):
            message = 'Invalid bonus!'
            message_class = 'alert-box warning'
        else:
            qset_id = request.POST['qset_id']
            qset = QuestionSet.objects.get(id=qset_id)
            if user == bonus.author or qset.is_owner(user) or user in qset.editor.all():
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

    return render(request, 'category_overview.html',
                             {'user': user,
                              'overview_rows': overview_rows,
                              'set_pct_complete': '{0:0.2f}%'.format(set_pct_complete),
                              'set_pct_progress_bar': '{0:0.0f}%'.format(set_pct_complete),
                              'tu_needed': tu_needed,
                              'bs_needed': bs_needed,
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
                    new_sets = user.question_set_editor.exclude(id=qset_id)
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
                'email_on_new_comments': entry.email_on_new_comments})
                
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
            'max_tu': entry.max_tossups if entry else (rec_tu if is_top else None),
            'min_bs': entry.min_bonuses if entry else (rec_bs if is_top else None),
            'max_bs': entry.max_bonuses if entry else (rec_bs if is_top else None),
        })
    return rows

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
                if min_tu is None and max_tu is None and min_bs is None and max_bs is None:
                    continue
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
    answer = get_answer_no_formatting(get_primary_answer(text or '')).strip()
    if len(answer) > limit:
        answer = answer[:limit].rstrip() + '...'
    return answer

@login_required
def packet_grid(request, qset_id):
    user = request.user.writer
    qset = QuestionSet.objects.get(id=qset_id)

    if not qset.is_owner(user) and user not in qset.editor.all() and user not in qset.writer.all():
        return render(request, 'failure.html',
                                 {'message': 'You are not authorized to view this set!',
                                  'message_class': 'alert-box alert'})

    read_only = not (qset.is_owner(user) or user in qset.editor.all())
    packets = list(qset.packet_set.order_by('packet_name'))

    def build_rows(question_model, preview_func, edit_url):
        cells_by_packet = {}
        max_num = 0
        for question in question_model.objects.filter(question_set=qset, packet__in=packets).select_related('category', 'packet'):
            number = question.question_number or 0
            if number <= 0:
                continue
            max_num = max(max_num, number)
            cells_by_packet.setdefault(question.packet_id, {})[number] = {
                'id': question.id,
                'answer': preview_func(question),
                'category': str(question.category) if question.category else '',
                'edit_url': '{0}{1}/'.format(edit_url, question.id),
            }
        rows = []
        for number in range(1, max_num + 1):
            rows.append({
                'num': number,
                'cells': [cells_by_packet.get(p.id, {}).get(number) for p in packets],
            })
        return rows

    tossup_rows = build_rows(
        Tossup, lambda t: _grid_answer_preview(t.tossup_answer), '/edit_tossup/')
    bonus_rows = build_rows(
        Bonus, lambda b: ' / '.join(filter(None, [
            _grid_answer_preview(b.part1_answer, 20),
            _grid_answer_preview(b.part2_answer, 20),
            _grid_answer_preview(b.part3_answer, 20)])), '/edit_bonus/')

    unassigned_tu = qset.tossup_set.filter(packet=None).count()
    unassigned_bs = qset.bonus_set.filter(packet=None).count()

    return render(request, 'packet_grid.html',
                             {'qset': qset,
                              'user': user,
                              'packets': packets,
                              'tossup_rows': tossup_rows,
                              'bonus_rows': bonus_rows,
                              'tossups_per_packet': qset.tossups_per_packet,
                              'bonuses_per_packet': qset.bonuses_per_packet,
                              'unassigned_tu': unassigned_tu,
                              'unassigned_bs': unassigned_bs,
                              'read_only': read_only})

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
                question.packet = target_packet
                question.question_number = target_number
                question.save()
                if occupant is not None:
                    occupant.packet = source_packet
                    occupant.question_number = source_number
                    occupant.save()
                cache.clear()
                success = True
                message = 'Question moved'
        except (KeyError, ValueError):
            message = 'Invalid request!'
        except (Tossup.DoesNotExist, Bonus.DoesNotExist, Packet.DoesNotExist):
            message = 'Question or packet not found!'

    return HttpResponse(json.dumps({'success': success, 'message': message}))


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
            'edit_url': '{0}{1}/'.format(edit_url, question.id),
            'is_tiebreaker': number > per_packet,
            'author': writer_label(question.author),
            'editor': writer_label(question.editor),
            'edited': question.edited,
            'length': question.character_count(),
            'max_length': max_length,
            'changed_date': question.last_changed_date,
            'changed_by': changers.get(question.question_history_id, ''),
        }

    packet_tossups = list(packet.tossup_set.order_by('question_number')
                          .select_related('category', 'author__user', 'editor__user'))
    packet_bonuses = list(packet.bonus_set.order_by('question_number')
                          .select_related('category', 'author__user', 'editor__user'))
    tossup_changers = latest_changers(TossupHistory, packet_tossups)
    bonus_changers = latest_changers(BonusHistory, packet_bonuses)

    tossups = [item(t, 'tossup', '/edit_tossup/', qset.tossups_per_packet, qset.max_acf_tossup_length, tossup_changers)
               for t in packet_tossups]
    bonuses = [item(b, 'bonus', '/edit_bonus/', qset.bonuses_per_packet,
                    qset.max_vhsl_bonus_length if b.get_bonus_type() == VHSL_BONUS else qset.max_acf_bonus_length,
                    bonus_changers)
               for b in packet_bonuses]

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
        by_id = {}
        comments = Comment.objects.filter(
            content_type=ct, object_pk__in=[str(q['id']) for q in items],
            is_removed=False).order_by('submit_date').select_related('user')
        for c in comments:
            label = ''
            if c.user is not None:
                label = '{0} {1}'.format(c.user.first_name, c.user.last_name).strip()
            if not label:
                label = c.user_name or (c.user.username if c.user else 'unknown')
            by_id.setdefault(int(c.object_pk), []).append({'user': label, 'text': c.comment, 'date': c.submit_date})
        for q in items:
            q['comments'] = by_id.get(q['id'], [])

    attach_comments(tossups, Tossup)
    attach_comments(bonuses, Bonus)
    comment_count = sum(len(q['comments']) for q in tossups + bonuses)

    siblings = list(qset.packet_set.order_by('packet_name'))
    index = next((i for i, p in enumerate(siblings) if p.id == packet.id), 0)
    prev_packet = siblings[index - 1] if index > 0 else None
    next_packet = siblings[index + 1] if index + 1 < len(siblings) else None

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
                              'read_only': read_only,
                              'user': user})

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
                for key, model in (('tossup_ids[]', Tossup), ('bonus_ids[]', Bonus)):
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
                            question.question_number = number
                            changed.append(question)
                    model.objects.bulk_update(changed, ['question_number'])
                cache.clear()
                success = True
                message = 'Order saved'
        except (KeyError, ValueError):
            message = 'Invalid request!'
        except Packet.DoesNotExist:
            message = 'Packet not found!'

    return HttpResponse(json.dumps({'success': success, 'message': message}))


@login_required
def swap_candidates(request):
    """JSON list of questions in other packets that could be swapped with the
    given question, filtered by category scope: 'leaf' (same sub-subcategory),
    'sub' (same subcategory) or 'top' (same general category)."""
    user = request.user.writer

    try:
        question_type = request.GET['question_type']
        question_id = int(request.GET['question_id'])
        scope = request.GET.get('scope', 'leaf')
        if scope not in ('leaf', 'sub', 'top'):
            scope = 'leaf'
        model = Tossup if question_type == 'tossup' else Bonus
        question = model.objects.get(id=question_id)
    except (KeyError, ValueError, Tossup.DoesNotExist, Bonus.DoesNotExist):
        return HttpResponse(json.dumps({'error': 'Question not found!'}))

    qset = question.question_set
    if not (qset.is_owner(user) or user in qset.editor.all()):
        return HttpResponse(json.dumps({'error': 'You are not authorized to swap questions in this set!'}))

    entry = question.category
    if entry is None:
        return HttpResponse(json.dumps({'error': 'This question has no category.'}))

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
        .exclude(packet=question.packet).exclude(packet=None) \
        .select_related('category', 'packet') \
        .order_by('packet__packet_name', 'question_number')[:200]

    def preview(q):
        if question_type == 'tossup':
            return _grid_answer_preview(q.tossup_answer)
        return ' / '.join(filter(None, [
            _grid_answer_preview(q.part1_answer, 20),
            _grid_answer_preview(q.part2_answer, 20),
            _grid_answer_preview(q.part3_answer, 20)]))

    per_packet = qset.tossups_per_packet if question_type == 'tossup' else qset.bonuses_per_packet
    data = [{
        'id': q.id,
        'packet_id': q.packet_id,
        'packet_name': q.packet.packet_name,
        'number': q.question_number,
        'answer': preview(q),
        'category': str(q.category) if q.category else '',
        'is_tiebreaker': (q.question_number or 0) > per_packet,
    } for q in candidates]

    return HttpResponse(json.dumps({'candidates': data, 'source_category': str(entry)}))


#########################################################################
# Category tags: editor-defined sub-distribution requirements
#########################################################################

def _tag_matches_path(tag_path, question_path):
    """A tag applies to a question when one path is a segment-wise prefix of
    the other: a 'Literature - World' tag covers questions in
    'Literature - World - Drama', and a tag on a deeper path than the
    question's leaf still applies to that leaf."""
    return (question_path == tag_path or
            question_path.startswith(tag_path + ' - ') or
            tag_path.startswith(question_path + ' - '))

def get_applicable_tags(qset, dist_entry):
    """Tags of the set that apply to a question in the given category."""
    if dist_entry is None:
        return []
    path = str(dist_entry)
    return [tag for tag in CategoryTag.objects.filter(question_set=qset).order_by('category_path', 'name')
            if _tag_matches_path(tag.category_path, path)]

def build_tag_checkboxes(qset, question, dist_entry):
    """Context rows for the tag checkboxes on the question edit pages."""
    tags = get_applicable_tags(qset, dist_entry)
    if not tags:
        return []
    if question is None:
        checked_ids = set()
    elif isinstance(question, Tossup):
        checked_ids = set(question.category_tags.values_list('id', flat=True))
    else:
        checked_ids = set(question.category_tags.values_list('id', flat=True))
    return [{'tag': tag, 'checked': tag.id in checked_ids} for tag in tags]

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

    if request.method == 'POST':
        if not can_edit:
            message = 'Only editors can change tags!'
            message_class = 'alert-box warning'
        else:
            action = request.POST.get('action', '')
            try:
                if action == 'add':
                    path = request.POST.get('category_path', '').strip()
                    name = request.POST.get('name', '').strip()
                    num_tossups = int(request.POST.get('num_tossups') or 0)
                    num_bonuses = int(request.POST.get('num_bonuses') or 0)
                    if not path or not name:
                        raise ValueError('A category and a tag name are required')
                    tag, created = CategoryTag.objects.get_or_create(
                        question_set=qset, category_path=path, name=name,
                        defaults={'num_tossups': num_tossups, 'num_bonuses': num_bonuses})
                    if not created:
                        tag.num_tossups = num_tossups
                        tag.num_bonuses = num_bonuses
                        tag.save()
                    message = 'Tag "{0}" saved'.format(name)
                    message_class = 'alert-box success'
                elif action == 'delete':
                    CategoryTag.objects.filter(question_set=qset, id=int(request.POST['tag_id'])).delete()
                    message = 'Tag deleted'
                    message_class = 'alert-box success'
            except (ValueError, KeyError) as ex:
                message = str(ex) or 'Invalid request'
                message_class = 'alert-box warning'

    # Category path choices from the set's distribution tree
    path_choices = [row['path'] for row in get_packetization_rows(qset)]

    # Group tags by category path with completion status
    groups = []
    tags_by_path = {}
    for tag in CategoryTag.objects.filter(question_set=qset).order_by('category_path', 'name').prefetch_related('tossups', 'bonuses'):
        tags_by_path.setdefault(tag.category_path, []).append(tag)
    for path in sorted(tags_by_path):
        rows = []
        for tag in tags_by_path[path]:
            tossups = [{'id': t.id,
                        'answer': _grid_answer_preview(t.tossup_answer),
                        'location': '{0} #{1}'.format(t.packet.packet_name, t.question_number) if t.packet else 'Unassigned'}
                       for t in tag.tossups.all().select_related('packet')]
            bonuses = [{'id': b.id,
                        'answer': ' / '.join(filter(None, [
                            _grid_answer_preview(b.part1_answer, 20),
                            _grid_answer_preview(b.part2_answer, 20),
                            _grid_answer_preview(b.part3_answer, 20)])),
                        'location': '{0} #{1}'.format(b.packet.packet_name, b.question_number) if b.packet else 'Unassigned'}
                       for b in tag.bonuses.all().select_related('packet')]
            tu_done = len(tossups)
            bs_done = len(bonuses)
            rows.append({
                'tag': tag,
                'tossups': tossups,
                'bonuses': bonuses,
                'tu_done': tu_done,
                'bs_done': bs_done,
                'tu_complete': tag.num_tossups == 0 or tu_done >= tag.num_tossups,
                'bs_complete': tag.num_bonuses == 0 or bs_done >= tag.num_bonuses,
            })
        groups.append({'path': path, 'rows': rows})

    return render(request, 'category_tags.html',
                             {'qset': qset,
                              'user': user,
                              'groups': groups,
                              'path_choices': path_choices,
                              'can_edit': can_edit,
                              'message': message,
                              'message_class': message_class})
