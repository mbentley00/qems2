from django.template.defaultfilters import register
from django.utils.safestring import mark_safe
from qems2.qsub.models import *
from qems2.qsub.utils import sanitize_html, strip_markup, get_formatted_question_html, get_answer_no_formatting
from django.contrib.contenttypes.models import ContentType, ContentTypeManager
from django_comments.models import *
from collections import OrderedDict

@register.filter(name='lookup')
def lookup(dict, key):
    if key in dict:
        return dict[key]
    else:
        return 0

@register.filter(name='tossup_or_bonus')
def tossup_or_bonus(type):
    return str(type)

@register.filter(name='is_set_owner')
def is_set_owner(qset, writer):
    """True if the writer is the set's owner or a co-owner (used to gate the
    API access link). Returns False on bad input."""
    try:
        return qset.is_owner(writer)
    except Exception:
        return False

@register.filter(name='tossups_or_bonuses')
def tossups_or_bonuses(type):
    if type == 'tossup':
        return 'tossups'
    if type == 'bonus':
        return 'bonuses'
    return type

@register.filter(name='get_editor_categories')
def get_editor_categories(editor, tour):

    if Role.objects.filter(player=editor, tournament=tour).exists():
        role = Role.objects.get(player=editor, tournament=tour)
        categories = role.category.split(';')

        cat_list = [cat_tuple[1] for cat_tuple in CATEGORIES if cat_tuple[0] in categories]
    else:
        cat_list = []

    return mark_safe('<p>' + '<br>'.join(cat_list) + '</p>')

@register.filter(name='preview')
def preview_filter(text):
    return preview(text)

@register.filter(name='short_preview')
def short_preview(text):
    return mark_safe(text[0:25])

@register.filter(name='answer_preview')
def answer_preview(text):
    return preview(get_primary_answer(text))

@register.filter(name='tossup_answer')
def tossup_answer(tossup):
    return mark_safe(answer_html(preview(get_primary_answer(tossup.tossup_answer))))

@register.filter(name='bonus_answers')
def bonus_answers(bonus):
     return mark_safe(answer_html(preview(get_primary_answer(bonus.part1_answer))) + '<br />'
        + answer_html(preview(get_primary_answer(bonus.part2_answer))) + '<br />'
        + answer_html(preview(get_primary_answer(bonus.part3_answer))))

@register.filter(name='to_short_datetime')
def to_short_datetime(date):
    if (date is None):
        return ""
    return date.strftime("%m-%d-%y %H:%M %p")
    
@register.filter(name='percent')
def percent(x, y):
    try:
        if float(y) != 0:
            val = 100 * float(x) / float(y)
            if (val > 100):
                val = 100
            return '{0:0.1f}%'.format(val)
        else:
            return None
    except Exception as ex:
        return None

@register.filter(name='fpercent')
def fpercent(x, y):
    try:
        if float(y) != 0:
            return 100 * float(x) / float(y)
        else:
            return None
    except Exception as ex:
        return None

@register.filter(name='packet_completion')
def packet_completion(packet):
    """Completion of a packet against the set's regular per-packet counts,
    e.g. '100% (20/20 TU, 20/20 B)'. For a tossups-only set, bonuses are
    excluded entirely (e.g. '100% (20/20 TU)')."""
    qset = packet.question_set
    tu = packet.tossup_set.count()
    tu_needed = qset.tossups_per_packet
    if getattr(qset, 'tossups_only', False):
        if tu_needed == 0:
            return ''
        pct = 100.0 * min(tu, tu_needed) / tu_needed
        return '{0:0.0f}% ({1}/{2} TU)'.format(pct, tu, tu_needed)
    bs = packet.bonus_set.count()
    bs_needed = qset.bonuses_per_packet
    total_needed = tu_needed + bs_needed
    if total_needed == 0:
        return ''
    pct = 100.0 * (min(tu, tu_needed) + min(bs, bs_needed)) / total_needed
    return '{0:0.0f}% ({1}/{2} TU, {3}/{4} B)'.format(pct, tu, tu_needed, bs, bs_needed)

@register.filter(name='tossups_remaining')
def tossups_remaining(entry):
    val = entry['tu_req'] - entry['tu_in_cat']
    if (val < 0):
        val = "0 (" + str((val * -1)) + " extra)"
        return val
    else:
        return val

@register.filter(name='bonuses_remaining')
def bonuses_remaining(entry):
    val = entry['bs_req'] - entry['bs_in_cat']
    if (val < 0):
        val = "0 (" + str((val * -1)) + " extra)"
        return val
    else:
        return val

@register.filter(name='overall_percent')
def overall_percent(entry):
    tu_in_cat = entry['tu_in_cat']
    bs_in_cat = entry['bs_in_cat']
    tu_req = entry['tu_req']
    bs_req = entry['bs_req']
    if (tu_in_cat is None):
        tu_in_cat = 0
    if (bs_in_cat is None):
        bs_in_cat = 0
    if (tu_req is None):
        tu_req = 0
    if (bs_req is None):
        bs_req = 0

    if (tu_in_cat > tu_req):
        tu_in_cat = tu_req
        
    if (bs_in_cat > bs_req):
        bs_in_cat = bs_req

    percentage = fpercent(tu_in_cat + bs_in_cat, tu_req + bs_req)
    if percentage == None:
        return mark_safe('<i class="fa fa-check"></i> ' + str(percentage))
    elif percentage >= 100:
        return mark_safe('<i class="fa fa-check"></i> ' + '{0:0.2f}%'.format(percentage))
    else:
        return mark_safe('<i class="fa fa-times"></i> ' + '{0:0.2f}%'.format(percentage))

@register.filter(name='check_mark_if_100_pct')
def check_mark_if_100_pct(x, y):
    percentage = fpercent(x, y)
    if percentage is None or percentage >= 100:
        return mark_safe('<i class="fa fa-check"></i>')
    else:
        return mark_safe('<i class="fa fa-times"></i>')

@register.filter(name='class_name')
def class_name(obj):
    return obj.__class__.__name__

@register.filter(name='sort')
def listsort(value):
    if isinstance(value, dict):
        print("Sorted dict called")
        new_dict = OrderedDict()
        key_list = sorted(value.keys())
        for key in key_list:
            new_dict[key] = value[key]
        return new_dict
    elif isinstance(value, list):
        print("List called")
        return sorted(value)
    else:
        print("Other called")
        return value
    listsort.is_safe = True

@register.filter(name='question_html')
def question_html(line):
    return get_formatted_question_html(line, False, True, False, False)

@register.filter(name='answer_html')
def answer_html(line):
    return get_formatted_question_html(line, True, True, False, False)

@register.filter(name='answer_no_formatting')
def answer_no_formatting(line):
    return get_answer_no_formatting(line)

@register.filter(name='comment_html')
def comment_html(comment):
    return get_formatted_question_html(comment, False, False, True, False)

@register.filter(name='tossup_html')
def tossup_html(tossup):
    return tossup.to_html()

@register.filter(name='tossup_html_verbose')
def tossup_html_verbose(tossup):
    return tossup.to_html(include_category=True, include_character_count=True)

@register.filter(name='bonus_html')
def bonus_html(bonus):
    return bonus.to_html()

@register.filter(name='bonus_leadin')
def bonus_leadin(bonus):
    return preview(bonus.leadin_to_html())

@register.filter(name='bonus_html_verbose')
def bonus_html_verbose(bonus):
    return bonus.to_html(include_category=True, include_character_count=True)

@register.filter(name='tossup_history_html')
def tossup_history_html(tossup):
    return tossup.to_html()

@register.filter(name='bonus_history_html')
def bonus_history_html(bonus):
    return bonus.to_html()

@register.filter(name='tossup_last_comment_date')
def tossup_last_comment_date(tossup):
    # Views that batch-load comments (model_utils.attach_question_comments)
    # leave them on the object; fall back to a query otherwise
    cached = getattr(tossup, 'cached_comments', None)
    if cached is not None:
        return cached[-1].submit_date if cached else None
    tossup_content_type_id = ContentType.objects.get_for_model(Tossup).id
    comments = Comment.objects.filter(object_pk=tossup.id).filter(content_type_id=tossup_content_type_id).order_by('-id')
    if (len(comments) > 0):
        return comments[0].submit_date
    else:
        return None

@register.filter(name='bonus_last_comment_date')
def bonus_last_comment_date(bonus):
    cached = getattr(bonus, 'cached_comments', None)
    if cached is not None:
        return cached[-1].submit_date if cached else None
    bonus_content_type_id = ContentType.objects.get_for_model(Bonus).id
    comments = Comment.objects.filter(object_pk=bonus.id).filter(content_type_id=bonus_content_type_id).order_by('-id')
    if (len(comments) > 0):
        return comments[0].submit_date
    else:
        return None

@register.filter(name='verbose_username')
def verbose_username(writer):
    return str(writer) + " - " + writer.user.email

@register.filter(name='question_set_id')
def question_set_id(question):
    return question.question_set.id
    
@register.filter(name='question_length')
def question_length(question):
    return question.character_count()


@register.filter(name='get_replies')
def get_replies(replies_dict, comment_id):
    """Get replies for a comment from the replies dictionary."""
    if isinstance(replies_dict, dict):
        return replies_dict.get(comment_id, [])
    return []


@register.filter(name='get_anchor')
def get_anchor(anchors_dict, comment_id):
    """Get the CommentAnchor for a comment from the anchors dictionary."""
    if isinstance(anchors_dict, dict):
        return anchors_dict.get(comment_id)
    return None


@register.simple_tag
def get_threaded_comments(obj):
    """
    Returns a dict with 'top_level' (list of comments) and 'replies' (dict of parent_id -> [comments]).
    Usage: {% get_threaded_comments obj as thread_data %}
    """
    from qems2.qsub.model_utils import mark_discord_comments
    content_type = ContentType.objects.get_for_model(obj)
    all_comments = list(Comment.objects.filter(
        content_type=content_type,
        object_pk=str(obj.pk),
        is_removed=False,
    ).order_by('submit_date'))
    # Tag bot comments (.is_discord / .discord_thread_url) for the template.
    mark_discord_comments(all_comments)

    # Get all reply mappings
    reply_comment_ids = set()
    parent_map = {}  # comment_id -> parent_id
    for cr in CommentReply.objects.filter(comment__in=all_comments):
        reply_comment_ids.add(cr.comment_id)
        parent_map[cr.comment_id] = cr.parent_id

    top_level = []
    replies = {}  # parent_id -> [comments]
    for comment in all_comments:
        if comment.id in reply_comment_ids:
            parent_id = parent_map[comment.id]
            replies.setdefault(parent_id, []).append(comment)
        else:
            top_level.append(comment)

    anchors = {ca.comment_id: ca for ca in CommentAnchor.objects.filter(comment__in=all_comments)}
    resolved = set(CommentResolution.objects.filter(comment__in=all_comments, resolved=True)
                   .values_list('comment_id', flat=True))

    return {'top_level': top_level, 'replies': replies, 'anchors': anchors, 'resolved': resolved}


#@register.filter(name='compare_categories'):
#def compare_categories(cat1, cat2):
