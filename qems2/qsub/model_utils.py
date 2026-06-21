#from __future__ import unicode_literals

from bs4 import BeautifulSoup
from .models import *
from .forms import *
from django.forms.formsets import formset_factory
from django.forms.models import modelformset_factory
from django.contrib.contenttypes.models import ContentType, ContentTypeManager
from django_comments.models import Comment
from django.db.models import Q, Count

import os
import re

from qems2.qsub.utils import EXTRAS_PACKET_NAME


def _natural_packet_key(packet):
    """Natural sort key for a packet: by the first number in its name
    ("Round 2" before "Round 10"), with extras/tiebreaker packets last."""
    name = packet.packet_name or ''
    lower = name.lower()
    is_special = (name == EXTRAS_PACKET_NAME or 'extra' in lower
                  or 'tiebreak' in lower or 'tie-break' in lower or lower.endswith(' tb'))
    nums = re.findall(r'\d+', name)
    number = int(nums[0]) if nums else float('inf')
    return (1 if is_special else 0, number, lower)


def sorted_packets(qset):
    """Packets in display order: an explicit user-set order (Packet.sort_order)
    when present, otherwise a natural sort by name with extras/tiebreakers last."""
    packets = list(qset.packet_set.all())
    if any(p.sort_order is not None for p in packets):
        packets.sort(key=lambda p: (p.sort_order is None,
                                    p.sort_order if p.sort_order is not None else 0,
                                    _natural_packet_key(p)))
    else:
        packets.sort(key=_natural_packet_key)
    return packets


def compute_packet_requirements(qset):
    '''
    :param qset: a QuestionSet model object
    :return: a collection of SetWideDistributionEntry objects
    '''

    num_packets = qset.num_packets
    packets = qset.packet_set.all()
    dist = qset.distribution
    dist_entries = dist.distributionentry_set.all()

    set_wide_entries = []

    for dist_entry in dist_entries:
        req_tus = dist_entry.min_tossups
        req_bs = dist_entry.min_bonuses

        set_wide_entry = SetWideDistributionEntry()
        set_wide_entry.category = dist_entry.category
        set_wide_entry.subcategory = dist_entry.subcategory
        set_wide_entry.num_tossups = req_tus
        set_wide_entry.num_bonuses = req_bs

        set_wide_entries.append(set_wide_entry)

    return set_wide_entries

def create_set_distro_formset(qset):

    DistributionEntryFormset = formset_factory(SetWideDistributionEntryForm, can_delete=False, extra=0)
    entries = qset.setwidedistributionentry_set.all()
    initial_data = []
    for entry in entries:
        initial_data.append({'entry_id': entry.id,
        'dist_entry': entry.dist_entry,
        'category': entry.dist_entry.category,
        'subcategory': entry.dist_entry.subcategory,
        'num_tossups': entry.num_tossups,
        'num_bonuses': entry.num_bonuses})
    return DistributionEntryFormset(initial=initial_data, prefix='distentry')

def create_tiebreak_formset(qset):

    DistributionEntryFormset = formset_factory(TieBreakDistributionEntryForm, can_delete=False, extra=0)
    entries = qset.tiebreakdistributionentry_set.all()
    initial_data = []
    for entry in entries:
        initial_data.append({'entry_id': entry.id,
        'dist_entry': entry.dist_entry,
        'category': entry.dist_entry.category,
        'subcategory': entry.dist_entry.subcategory,
        'num_tossups': entry.num_tossups,
        'num_bonuses': entry.num_bonuses})
    return DistributionEntryFormset(initial=initial_data, prefix='tiebreak')

def reset_tiebreak_distro(qset):

    dist = qset.distribution
    dist_entries = dist.distributionentry_set.all()

    old_tiebreakers = TieBreakDistributionEntry.objects.filter(question_set=qset)
    for tb in old_tiebreakers:
        tb.delete()

    for entry in dist_entries:

        tiebreak_entry = TieBreakDistributionEntry()
        tiebreak_entry.num_bonuses = 1
        tiebreak_entry.num_tossups = 1
        tiebreak_entry.question_set = qset
        tiebreak_entry.dist_entry = entry
        tiebreak_entry.save()

def get_role(user, qset):

    role = 'none'
    qset_editors = qset.editor.all()
    qset_writers = qset.writer.all()

    if qset.is_owner(user):
        role = 'owner'
    elif user in qset_editors:
        role = 'editor'
    elif user in qset_writers:
        role = 'writer'

    return role

def get_role_no_owner(user, qset):

    role = 'none'
    qset_editors = qset.editor.all()
    qset_writers = qset.writer.all()

    if user in qset_editors:
        role = 'editor'
    elif user in qset_writers:
        role = 'writer'

    return role    

def export_packet(packet_id):

    packet = Packet.objects.get(id=packet_id)
    qset = packet.question_set

    tex_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "tex"),)

    print(tex_path)

    latex_preamble = r'''
\documentclass[10pt]{article}
\usepackage[top=1in, bottom=1in, left=1in, right=1in]{geometry}
\usepackage{parskip}
\usepackage[]{graphicx}
\usepackage[normalem]{ulem}
%\usepackage{ebgaramond}
\usepackage[utf8]{inputenc}

\begin{document}

\newcommand{\ans}[1]{{\sc \uline{#1}}}

\newcommand{\tossups}{\newcounter{TossupCounter} \noindent {\sc Tossups}\\}
\newcommand{\tossup}[2]{\stepcounter{TossupCounter}
    \arabic{TossupCounter}.~#1\\ANSWER: #2\\}

\newcommand{\bonuses}{\newcounter{BonusCounter} \noindent {\sc Bonuses} \\}
% bonus part is points - text - answer
\newcommand{\bonuspart}[3]{[#1]~#2\\ANSWER: #3\\}
% bonus is leadin - parts

\newenvironment{bonus}[1]{\stepcounter{BonusCounter}
    \arabic{BonusCounter}.~#1\\}{}


%\newcommand{\bonus}[2]{\stepcounter{BonusCounter}
%  \arabic{BonusCounter}.~#1\\#2}

\begin{center}
  %\includegraphics[scale=1]{acf-logo.pdf}\\
  {\sc tournament \\ packet }
\end{center}
'''

    latex_end = r'\end{document}'

    tossups = Tossup.objects.filter(packet=packet)
    bonuses = Bonus.objects.filter(packet=packet)

    tossup_latex = r'\tossups' + '\n'
    bonus_latex = r'\bonuses' + '\n'

    output_file = os.path.join(tex_path, '{0} - {1}.tex'.format(qset, packet))

    for tossup in tossups:
        tossup_latex += tossup.to_latex()

    for bonus in bonuses:
        bonus_latex += bonus.to_latex()

    packet_latex = latex_preamble + tossup_latex + bonus_latex + latex_end

    print(output_file)

    print(packet_latex)

    f = open(output_file, 'w')
    f.write(packet_latex.encode('utf-8'))
    f.close()

def export_packet_reportlab(packet_id):

    import pdfdocument as pdf
    import io

    packet = Packet.objects.get(id=packet_id)
    qset = packet.question_set

    pdf_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "pdf"),)
    
# Edits all of these questions
def bulk_edit_questions(is_edited, tossups, bonuses, qset, user):
    for tossup in tossups:
        tossup.edited = is_edited
        tossup.save_question(QUESTION_EDIT, user)
        
    for bonus in bonuses:
        bonus.edited = is_edited
        bonus.save_question(QUESTION_EDIT, user)
        
def bulk_lock_questions(is_locked, tossups, bonuses, qset, user):
    for tossup in tossups:
        tossup.locked = is_locked
        tossup.save_question(QUESTION_EDIT, user)
        
    for bonus in bonuses:
        bonus.locked = is_locked
        bonus.save_question(QUESTION_EDIT, user)    

def bulk_delete_questions(tossups, bonuses, qset, user):
    for tossup in tossups:
        tossup.delete()
        
    for bonus in bonuses:
        bonus.delete()

def bulk_convert_to_acf_style_tossup(tossups, bonuses, qset, user):
    for tossup in tossups:
        tossup_to_tossup(tossup, ACF_STYLE_TOSSUP)
    
    for bonus in bonuses:
        bonus_to_tossup(bonus, ACF_STYLE_TOSSUP)

def bulk_convert_to_acf_style_bonus(tossups, bonuses, qset, user):
    for tossup in tossups:
        tossup_to_bonus(tossup, ACF_STYLE_BONUS)
    
    for bonus in bonuses:
        bonus_to_bonus(bonus, ACF_STYLE_BONUS)

def bulk_convert_to_vhsl_bonus(tossups, bonuses, qset, user):
    for tossup in tossups:
        tossup_to_bonus(tossup, VHSL_BONUS)
    
    for bonus in bonuses:
        bonus_to_bonus(bonus, VHSL_BONUS)

def tossup_to_bonus(tossup, output_question_type):
    if (output_question_type == ACF_STYLE_BONUS):
        if (tossup.get_tossup_type() == ACF_STYLE_TOSSUP):
            bonus = copy_to_bonus(tossup)
            bonus.question_type = QuestionType.objects.get(question_type=ACF_STYLE_BONUS)
            bonus.leadin = tossup.tossup_text
            bonus.part1_answer = tossup.tossup_answer
            bonus.part1_text = ""
            bonus.part2_text = ""
            bonus.part2_answer = ""
            bonus.part3_text = ""
            bonus.part3_answer = ""
            bonus.save_question(QUESTION_CREATE, tossup.author)
            move_comments_to_bonus(tossup, bonus)
            tossup.delete()
            return bonus
    elif (output_question_type == VHSL_BONUS):
        if (tossup.get_tossup_type() == ACF_STYLE_TOSSUP):
            bonus = copy_to_bonus(tossup)
            bonus.part1_text = tossup.tossup_text
            bonus.part1_answer = tossup.tossup_answer
            bonus.question_type = QuestionType.objects.get(question_type=VHSL_BONUS)
            bonus.leadin = ""
            bonus.part2_text = ""
            bonus.part2_answer = ""
            bonus.part3_text = ""
            bonus.part3_answer = ""
            bonus.save_question(QUESTION_CREATE, tossup.author)
            move_comments_to_bonus(tossup, bonus)
            tossup.delete()
            return bonus
    return None
        
def tossup_to_tossup(tossup, output_question_type):
    pass # No-op for now since there's just one type of tossup

def bonus_to_bonus(bonus, output_question_type):
    if (output_question_type == ACF_STYLE_BONUS):
        if (bonus.get_bonus_type() == VHSL_BONUS):
            bonus.question_type = QuestionType.objects.get(question_type=ACF_STYLE_BONUS)
            bonus.leadin = ""
            bonus.part2_text = ""
            bonus.part2_answer = ""
            bonus.part3_text = ""
            bonus.part3_answer = ""
            bonus.save_question(QUESTION_CREATE, bonus.author)
            return bonus
    elif (output_question_type == VHSL_BONUS):
        if (bonus.get_bonus_type() == ACF_STYLE_BONUS):
            bonus.question_type = QuestionType.objects.get(question_type=VHSL_BONUS)
            bonus.part1_text = bonus.leadin + " " + bonus.part1_text + " " + bonus.part1_answer + " " + bonus.part2_text + " " + bonus.part2_answer + " " + bonus.part3_text + " " + bonus.part3_answer
            bonus.leadin = ""
            bonus.part2_text = ""
            bonus.part2_answer = ""
            bonus.part3_text = ""
            bonus.part3_answer = ""
            bonus.save_question(QUESTION_CREATE, bonus.author)
            return bonus
    return None

def bonus_to_tossup(bonus, output_question_type):
    if (output_question_type == ACF_STYLE_TOSSUP):
        if (bonus.get_bonus_type() == VHSL_BONUS):
            tossup = copy_to_tossup(bonus)
            tossup.question_type = QuestionType.objects.get(question_type=ACF_STYLE_TOSSUP)
            tossup.tossup_text = bonus.part1_text
            tossup.tossup_answer = bonus.part1_answer
            tossup.save_question(QUESTION_CREATE, bonus.author)
            move_comments_to_tossup(bonus, tossup)
            bonus.delete()
            return tossup
        elif (bonus.get_bonus_type() == ACF_STYLE_BONUS):
            tossup = copy_to_tossup(bonus)
            tossup.question_type = QuestionType.objects.get(question_type=ACF_STYLE_TOSSUP)
            tossup.tossup_text = bonus.leadin + " " + bonus.part1_text + " " + bonus.part1_answer + " " + bonus.part2_text + " " + bonus.part2_answer + " " + bonus.part3_text + " " + bonus.part3_answer
            tossup.tossup_answer = bonus.part1_answer
            tossup.save_question(QUESTION_CREATE, bonus.author)
            move_comments_to_tossup(bonus, tossup)
            bonus.delete()
            return tossup
    return None                        
        
def copy_to_tossup(bonus):
    tossup = Tossup()
    tossup.packet = bonus.packet
    tossup.question_set = bonus.question_set
    tossup.category = bonus.category
    tossup.subtype = bonus.subtype
    tossup.time_period = bonus.time_period
    tossup.location = bonus.location
    tossup.author = bonus.author
    tossup.question_history = bonus.question_history
    tossup.created_date = bonus.created_date
    tossup.last_changed_date = bonus.last_changed_date
    return tossup

def move_comments_to_tossup(bonus, tossup):
    # Change all of the comments to be associated with this new object
    tossup_content_type = ContentType.objects.get(app_label='qsub', model='tossup')
    bonus_content_type = ContentType.objects.get(app_label='qsub', model='bonus')
    for comment in Comment.objects.filter(object_pk=bonus.id).filter(content_type_id=bonus_content_type.id):
        comment.object_pk = tossup.id
        comment.content_type_id = tossup_content_type.id
        comment.save()

def copy_to_bonus(tossup):
    bonus = Bonus()
    bonus.packet = tossup.packet
    bonus.question_set = tossup.question_set
    bonus.category = tossup.category
    bonus.subtype = tossup.subtype
    bonus.time_period = tossup.time_period
    bonus.location = tossup.location
    bonus.author = tossup.author
    bonus.question_history = tossup.question_history
    bonus.created_date = tossup.created_date
    bonus.last_changed_date = tossup.last_changed_date
    return bonus

def move_comments_to_bonus(tossup, bonus):
    # Change all of the comments to be associated with this new object
    tossup_content_type = ContentType.objects.get(app_label='qsub', model='tossup')
    bonus_content_type = ContentType.objects.get(app_label='qsub', model='bonus')
    for comment in Comment.objects.filter(object_pk=tossup.id).filter(content_type_id=tossup_content_type.id):
        comment.object_pk = bonus.id
        comment.content_type_id = bonus_content_type.id
        comment.save()

def get_question_type_from_string(question_type):
    return QuestionType.objects.get(question_type=question_type)

QUESTION_LIST_RELATED = ('category', 'author__user', 'editor__user', 'packet',
                         'question_set', 'question_type')

def mark_discord_comments(comments):
    """Tag each comment with ``.is_discord`` and ``.discord_thread_url`` so
    templates can show Discord (bot) comments apart from human ones and link
    straight to the playtest thread.

    A comment is treated as a Discord comment when it has a DiscordCommentRef
    (the authoritative marker) or, as a fallback, when it was posted with no
    user under the bot's display name. The thread url is the Discord thread on
    the comment's own question, if any. Accepts comments spanning several
    questions; runs a small fixed number of queries. Returns the same list."""
    comments = list(comments)
    if not comments:
        return comments

    ref_ids = set(DiscordCommentRef.objects
                  .filter(comment_id__in=[c.id for c in comments])
                  .values_list('comment_id', flat=True))

    tu_ct = ContentType.objects.get_for_model(Tossup).id
    bs_ct = ContentType.objects.get_for_model(Bonus).id

    tu_ids, bs_ids = set(), set()
    for c in comments:
        c.is_discord = (c.id in ref_ids or
                        (c.user_id is None and c.user_name == DISCORD_BOT_NAME))
        if c.is_discord:
            try:
                pk = int(c.object_pk)
            except (TypeError, ValueError):
                continue
            (tu_ids if c.content_type_id == tu_ct else bs_ids).add(pk)

    thread_by_q = {}  # (content_type_id, question_id) -> first thread url
    if tu_ids:
        for th in DiscordThread.objects.filter(tossup_id__in=tu_ids).order_by('id'):
            thread_by_q.setdefault((tu_ct, th.tossup_id), th.url)
    if bs_ids:
        for th in DiscordThread.objects.filter(bonus_id__in=bs_ids).order_by('id'):
            thread_by_q.setdefault((bs_ct, th.bonus_id), th.url)

    for c in comments:
        url = ''
        if c.is_discord:
            try:
                url = thread_by_q.get((c.content_type_id, int(c.object_pk)), '')
            except (TypeError, ValueError):
                url = ''
        c.discord_thread_url = url
    return comments


def attach_question_comments(tossup_dict, bonus_dict):
    """Bulk-load the comments for the given questions and attach them as
    .cached_comments (non-removed, oldest first), replacing one query per
    question row with one query total.  filters.py and the question list
    templates read this attribute when present."""
    tossup_ct = ContentType.objects.get_for_model(Tossup).id
    bonus_ct = ContentType.objects.get_for_model(Bonus).id

    for question_dict in (tossup_dict, bonus_dict):
        for question in question_dict.values():
            question.cached_comments = []

    comment_filter = Q()
    if tossup_dict:
        comment_filter |= Q(content_type_id=tossup_ct, object_pk__in=[str(pk) for pk in tossup_dict])
    if bonus_dict:
        comment_filter |= Q(content_type_id=bonus_ct, object_pk__in=[str(pk) for pk in bonus_dict])
    if not comment_filter:
        return

    comments = (Comment.objects.filter(comment_filter, is_removed=False)
                .select_related('user').order_by('submit_date'))
    comments = mark_discord_comments(comments)
    for comment in comments:
        question_dict = tossup_dict if comment.content_type_id == tossup_ct else bonus_dict
        question = question_dict.get(int(comment.object_pk))
        if question is not None:
            question.cached_comments.append(comment)

def get_tossup_and_bonuses_in_set(qset, question_limit=30, preview_only=False):
    tossup_dict = {}
    tossups = []
    tossup_count = 0
    for tossup in Tossup.objects.filter(question_set=qset).order_by('-id').select_related(*QUESTION_LIST_RELATED):
        if (tossup_count < question_limit):
            tossup.question_length = tossup.character_count()
            if (preview_only):
                tossup.tossup_text = preview(tossup.tossup_text)
                tossup.tossup_answer = preview(get_primary_answer(tossup.tossup_answer))
            
            tossups.append(tossup)            
            tossup_count += 1
        tossup_dict[tossup.id] = tossup

    bonus_dict = {}
    bonuses = []
    short_bonuses = []
    bonus_count = 0
    for bonus in Bonus.objects.filter(question_set=qset).order_by('-id').select_related(*QUESTION_LIST_RELATED):
        if (bonus_count < question_limit):
            bonus.question_length = bonus.character_count()
            if (preview_only):
                bonus.leadin = preview(bonus.leadin)
                bonus.part1_text = preview(bonus.part1_text)
                bonus.part1_answer = preview(get_primary_answer(bonus.part1_answer))
                bonus.part2_text = preview(bonus.part2_text)
                bonus.part2_answer = preview(get_primary_answer(bonus.part2_answer))
                bonus.part3_text = preview(bonus.part3_text)
                bonus.part3_answer = preview(get_primary_answer(bonus.part3_answer))
            
            bonuses.append(bonus)
            bonus_count += 1
        bonus_dict[bonus.id] = bonus

    attach_question_comments(tossup_dict, bonus_dict)

    return tossups, tossup_dict, bonuses, bonus_dict

def get_comment_tab_list(tossup_dict, bonus_dict, comment_limit=60):
    comment_tab_list = []

    tossup_content_type_id = ContentType.objects.get_for_model(Tossup).id
    bonus_content_type_id = ContentType.objects.get_for_model(Bonus).id

    comment_filter = Q()
    if tossup_dict:
        comment_filter |= Q(content_type_id=tossup_content_type_id,
                            object_pk__in=[str(pk) for pk in tossup_dict])
    if bonus_dict:
        comment_filter |= Q(content_type_id=bonus_content_type_id,
                            object_pk__in=[str(pk) for pk in bonus_dict])
    if not comment_filter:
        return comment_tab_list

    comments = (Comment.objects.filter(comment_filter, is_removed=False)
                .select_related('user').order_by('-submit_date')[:comment_limit])
    comments = mark_discord_comments(comments)
    for comment in comments:
        if (comment.content_type_id == tossup_content_type_id):
            tossup = tossup_dict[int(comment.object_pk)]
            comment_tab_list.append({'comment': comment,
                                     'question_text': get_formatted_question_html(tossup.tossup_answer[0:80], True, True, False, False),
                                     'question_id': tossup.id,
                                     'question_type': 'tossup'})
        else:
            bonus = bonus_dict[int(comment.object_pk)]
            comment_tab_list.append({'comment': comment,
                                     'question_text': get_formatted_question_html_for_bonus_answers(bonus),
                                     'question_id': bonus.id,
                                     'question_type': 'bonus'})

    return comment_tab_list

def get_category_overview(qset):
    entries = qset.setwidedistributionentry_set.all().order_by('dist_entry__category', 'dist_entry__subcategory')

    # Build a tree of category stats
    # tree[path_tuple] = {'tu_req': ..., 'tu_in_cat': ..., 'bs_req': ..., 'bs_in_cat': ..., 'category_id': ..., 'is_leaf': bool}
    tree = {}
    tossups_only = getattr(qset, 'tossups_only', False)

    for entry in entries:
        tu_required = entry.num_tossups
        bs_required = 0 if tossups_only else entry.num_bonuses
        tu_written = qset.tossup_set.filter(category=entry.dist_entry).count()
        bs_written = qset.bonus_set.filter(category=entry.dist_entry).count()

        # Build path from category + subcategory parts
        parts = [entry.dist_entry.category]
        if entry.dist_entry.subcategory:
            for sub in entry.dist_entry.subcategory.split(' - '):
                sub = sub.strip()
                if sub:
                    parts.append(sub)

        leaf_key = tuple(parts)

        # Mark the leaf
        tree[leaf_key] = {
            'tu_req': tu_required,
            'tu_in_cat': tu_written,
            'bs_req': bs_required,
            'bs_in_cat': bs_written,
            'category_id': entry.dist_entry.id,
            'is_leaf': True,
        }

        # Accumulate into all ancestor prefixes
        for i in range(1, len(parts)):
            prefix = tuple(parts[:i])
            if prefix not in tree:
                tree[prefix] = {
                    'tu_req': 0, 'tu_in_cat': 0,
                    'bs_req': 0, 'bs_in_cat': 0,
                    'category_id': None, 'is_leaf': False,
                }
            tree[prefix]['tu_req'] += tu_required
            tree[prefix]['tu_in_cat'] += tu_written
            tree[prefix]['bs_req'] += bs_required
            tree[prefix]['bs_in_cat'] += bs_written

    # Remove group nodes that only have one child (they'd just duplicate the leaf)
    keys = sorted(tree.keys())
    groups_to_remove = set()
    for key in keys:
        if not tree[key]['is_leaf']:
            # Count direct children
            children = [k for k in keys if len(k) == len(key) + 1 and k[:len(key)] == key]
            if len(children) == 1:
                groups_to_remove.add(key)
    for key in groups_to_remove:
        del tree[key]

    # Build sorted rows
    rows = []
    for key in sorted(tree.keys()):
        node = tree[key]
        rows.append({
            'name': ' - '.join(key),
            'short_name': key[-1],
            'depth': len(key) - 1,
            'tu_req': node['tu_req'],
            'tu_in_cat': node['tu_in_cat'],
            'bs_req': node['bs_req'],
            'bs_in_cat': node['bs_in_cat'],
            'is_group': not node['is_leaf'],
            'category_id': node['category_id'],
            'padding': (len(key) - 1) * 30,
        })

    return rows

def get_questions_remaining(qset):
    total_tu_req = 0
    total_bs_req = 0
    total_tu_written = 0
    total_bs_written = 0
    set_status = {}    
    
    entries = (qset.setwidedistributionentry_set.all()
               .select_related('dist_entry').order_by('dist_entry__category', 'dist_entry__subcategory'))

    # Count questions per category in two grouped queries instead of two per entry
    tu_counts = {row['category']: row['n'] for row in
                 qset.tossup_set.values('category').annotate(n=Count('id'))}
    bs_counts = {row['category']: row['n'] for row in
                 qset.bonus_set.values('category').annotate(n=Count('id'))}

    tossups_only = getattr(qset, 'tossups_only', False)

    for entry in entries:
        tu_required = entry.num_tossups
        bs_required = 0 if tossups_only else entry.num_bonuses
        # TODO: really fix extra questions increasing set completion; this is temporary
        tu_written = tu_counts.get(entry.dist_entry_id, 0)
        bs_written = bs_counts.get(entry.dist_entry_id, 0)
        tu_written_for_total = min(tu_written, tu_required)
        bs_written_for_total = min(bs_written, bs_required)
        total_tu_req += tu_required
        total_bs_req += bs_required
        total_bs_written += bs_written_for_total
        total_tu_written += tu_written_for_total

        set_status[str(entry.dist_entry)] = {'tu_req': tu_required,
                                             'tu_in_cat': tu_written,
                                             'bs_req': bs_required,
                                             'bs_in_cat': bs_written,
                                             'category_id': entry.dist_entry.id
                                             }
    # Prevent divide by 0 errors
    total_qs_req = max(1, total_tu_req + total_bs_req)
    set_pct_complete = (float(total_tu_written + total_bs_written) * 100) / total_qs_req
    tu_needed = total_tu_req - total_tu_written
    bs_needed = total_bs_req - total_bs_written
    
    return set_status, total_tu_req, total_bs_req, tu_needed, bs_needed, set_pct_complete
    
def get_writer_questions_remaining(qset, total_tu_req, total_bs_req):
    qset_editors = qset.editor.all().select_related('user')
    qset_writers = qset.writer.all().select_related('user')
    writer_stats = {}
    total_qs_req = max(1, total_tu_req + total_bs_req)

    # Count questions per author in two grouped queries instead of two per writer
    tu_counts = {row['author']: row['n'] for row in
                 qset.tossup_set.values('author').annotate(n=Count('id'))}
    bs_counts = {row['author']: row['n'] for row in
                 qset.bonus_set.values('author').annotate(n=Count('id'))}

    for writer in list(qset_writers) + list(qset_editors):
        writer_tu_written = tu_counts.get(writer.id, 0)
        writer_bonus_written = bs_counts.get(writer.id, 0)
        writer_question_percent = (float(writer_tu_written + writer_bonus_written) * 100) / total_qs_req

        writer_stats[writer.user.username] = {'tu_written': writer_tu_written,
                                              'bonus_written': writer_bonus_written,
                                              'question_percent': writer_question_percent,
                                              'writer': writer}

    return writer_stats
