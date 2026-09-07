import re
from django.template.defaultfilters import register
from django.utils.html import escape
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
    
@register.filter(name='short_ago')
def short_ago(date):
    """How long ago, in as few characters as possible: "just now", "12m",
    "5h", "3d", then the date itself once a week has passed.

    A comment column is mostly names and text; a full timestamp on every one
    of them ("Feb. 7, 2026, 10:06 p.m.") crowds both out, and what a reader
    wants at a glance is how recent it is. The exact time goes in the title
    attribute wherever this is used.
    """
    if date is None:
        return ''
    import datetime as _dt
    from django.utils import timezone as _tz
    # The dates come from the database, so they are aware when the project is;
    # a naive one has to be compared against a naive now.
    now = _tz.now() if _tz.is_aware(date) else _dt.datetime.now()
    # Day and month spelled out rather than strftime: the no-padding flag is
    # %-d on Linux and %#d on Windows, and this runs on both.
    on_the_day = '{0} {1}'.format(date.strftime('%b'), date.day)
    seconds = (now - date).total_seconds()
    if seconds < 0:            # clock skew, or a date in the future
        return on_the_day
    if seconds < 60:
        return 'just now'
    if seconds < 3600:
        return '{0}m'.format(int(seconds // 60))
    if seconds < 86400:
        return '{0}h'.format(int(seconds // 3600))
    if seconds < 7 * 86400:
        return '{0}d'.format(int(seconds // 86400))
    return on_the_day


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
    excluded entirely (e.g. '100% (20/20 TU)').

    Reads the counts sorted_packets() attached, falling back to counting this
    packet alone -- a page that lists every packet in a set must not ask the
    database for them a packet at a time.
    """
    qset = packet.question_set
    tu = getattr(packet, 'tossup_count', None)
    if tu is None:
        tu = packet.tossup_set.count()
    tu_needed = qset.tossups_per_packet
    if getattr(qset, 'tossups_only', False):
        if tu_needed == 0:
            return ''
        pct = 100.0 * min(tu, tu_needed) / tu_needed
        return '{0:0.0f}% ({1}/{2} TU)'.format(pct, tu, tu_needed)
    bs = getattr(packet, 'bonus_count', None)
    if bs is None:
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


@register.filter(name='track_changes_diff')
def track_changes_diff(old, new):
    """Word-level track-changes HTML between two strings: removed text in <del>,
    added text in <ins>."""
    import difflib
    from html import escape
    old_tokens = (old or '').split(' ')
    new_tokens = (new or '').split(' ')
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_tokens, new_tokens).get_opcodes():
        old_chunk = escape(' '.join(old_tokens[i1:i2]))
        new_chunk = escape(' '.join(new_tokens[j1:j2]))
        if tag == 'equal':
            out.append(old_chunk)
        elif tag == 'delete':
            out.append('<del>' + old_chunk + '</del>')
        elif tag == 'insert':
            out.append('<ins>' + new_chunk + '</ins>')
        elif tag == 'replace':
            out.append('<del>' + old_chunk + '</del> <ins>' + new_chunk + '</ins>')
    return mark_safe(' '.join(p for p in out if p))


@register.filter(name='commenter_name')
def commenter_name(comment):
    """Display a comment author as their real name with username in quotes
    (e.g. Will Alston ("walston")), or just the username, or a bot's name.

    Escaped and marked safe: a name is user-supplied (and a bot's posted name
    doubly so), and several of the templates that show one have autoescaping
    turned off for the question markup around it."""
    user = getattr(comment, 'user', None)
    if user is None:
        return mark_safe(_escape_text(getattr(comment, 'user_name', '') or 'Anonymous'))
    real = '{0} {1}'.format(user.first_name or '', user.last_name or '').strip()
    name = '{0} ("{1}")'.format(real, user.username) if real else user.username
    return mark_safe(_escape_text(name))

@register.filter(name='commenter_short_name')
def commenter_short_name(comment):
    """Just the commenter's real name (username only when they have none),
    for the tables where a comment is one cell among many and the
    name-plus-handle-plus-tags form crowds out the comment itself."""
    user = getattr(comment, 'user', None)
    if user is None:
        return mark_safe(_escape_text(getattr(comment, 'user_name', '') or 'Anonymous'))
    real = '{0} {1}'.format(user.first_name or '', user.last_name or '').strip()
    return mark_safe(_escape_text(real or user.username))

COMMENT_PREVIEW_COUNT = 2


def _live_comments(comments):
    """The comment list a question table should count, newest last."""
    items = [c for c in (comments or []) if not getattr(c, 'is_removed', False)]
    try:
        items.sort(key=lambda c: (c.submit_date, c.id))
    except Exception:
        pass
    return items


@register.filter(name='recent_comments')
def recent_comments(comments):
    """The last few comments on a question, for a list view. A question in a
    busy playtest can carry a dozen; showing them all makes one row taller
    than the screen and buries every other column."""
    return _live_comments(comments)[-COMMENT_PREVIEW_COUNT:]


@register.filter(name='older_comment_count')
def older_comment_count(comments):
    """How many comments `recent_comments` left out (0 when none)."""
    return max(0, len(_live_comments(comments)) - COMMENT_PREVIEW_COUNT)


@register.filter(name='writer_name')
def writer_name(writer):
    """A writer's real name, falling back to their username. Escaped and marked
    safe for the same reason `commenter_name` is: it is user-supplied text and
    several templates around it turn autoescaping off."""
    if writer is None:
        return ''
    user = getattr(writer, 'user', None)
    if user is None:
        return mark_safe(_escape_text(str(writer)))
    real = '{0} {1}'.format(user.first_name or '', user.last_name or '').strip()
    return mark_safe(_escape_text(real or user.username))


@register.filter(name='commenter_tags')
def commenter_tags(comment, qset):
    """The commenter's editor tags on this set, as small chips to sit beside
    their name — so it's clear at a glance that the person asking for a change
    is, say, the Science editor.

    Usage: ``{{ comment|commenter_tags:qset }}``. The whole set's tags are
    fetched once and cached on the `qset` object, which is the same instance for
    every comment in a page render, so a long thread is still one query.
    """
    if comment is None or qset is None:
        return ''
    tags_by_user = getattr(qset, '_commenter_tag_cache', None)
    if tags_by_user is None:
        tags_by_user = {}
        for tag in EditorTag.objects.filter(question_set=qset).select_related('editor'):
            text = (tag.text() or '').strip()
            if text:
                tags_by_user.setdefault(tag.editor.user_id, []).append(text)
        qset._commenter_tag_cache = tags_by_user
    user = getattr(comment, 'user', None)
    if user is None:
        return ''
    tags = tags_by_user.get(user.id) or []
    return mark_safe(''.join(
        '<span class="commenter-tag">{0}</span>'.format(escape(t)) for t in tags))


# An @mention: "@" at the start or after whitespace/an open paren, then a
# username. The username may itself contain "@" and "." because some usernames
# are full email addresses (e.g. @william.t.alston@gmail.com must highlight
# whole, not stop at the second "@"). Mirrors the mention set in signals.py.
# The leading boundary still keeps an email written inline in prose
# ("mail a@b.com") from matching, since its "@" follows a letter.
_MENTION_RE = re.compile(r'(^|[\s(])@([A-Za-z0-9_][A-Za-z0-9_.\-@+]*)')


# Comment text is whatever somebody typed, and it is rendered as HTML (the
# formatter turns QEMS markup into tags, and the templates print the result
# unescaped). Neutralize the two characters that can start a tag before the
# formatter runs, so a comment can contain "<script>" and *say* "<script>"
# rather than being one.
#
# Only "<" and ">" — not "&". Comments imported from other systems carry
# entities like &#x27; already, and escaping the ampersand would turn those
# into visible gibberish in years of existing threads without making anything
# safer: an ampersand can't open a tag.
def _defuse_tags(text):
    return (text or '').replace('<', '&lt;').replace('>', '&gt;')


def _escape_text(text):
    """Escape a plain string for text position. Unlike django's `escape` this
    leaves quotation marks alone: the values it is used on are names, shown as
    `Will Alston ("walston")`, and never go inside an attribute."""
    return (text or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


@register.filter(name='comment_html')
def comment_html(comment):
    formatted = get_formatted_question_html(_defuse_tags(comment), False, False, True, False)
    highlighted = _MENTION_RE.sub(
        lambda m: '{0}<span class="at-mention" style="color:#1565c0;font-weight:bold;">@{1}</span>'.format(
            m.group(1), m.group(2)),
        formatted)
    return mark_safe(highlighted)

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
    return _threaded_comments(obj)


@register.simple_tag
def get_comment_history(obj):
    """Every comment ever left on `obj`, deleted ones included, threaded the same
    way as the live view. Deleting a comment only sets ``is_removed``, so the
    text survives — this is what the question's History page shows, so a
    discussion isn't lost when someone clears it off the editing page.

    Usage: {% get_comment_history question as comment_history %}
    """
    return _threaded_comments(obj, include_removed=True)


def _threaded_comments(obj, include_removed=False):
    from qems2.qsub.model_utils import mark_discord_comments
    content_type = ContentType.objects.get_for_model(obj)
    comment_filter = {'content_type': content_type, 'object_pk': str(obj.pk)}
    if not include_removed:
        comment_filter['is_removed'] = False
    all_comments = list(Comment.objects.filter(**comment_filter).order_by('submit_date'))
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
    # Only ever non-empty for the history view; the live view filters these out.
    removed = set(c.id for c in all_comments if c.is_removed)

    return {'top_level': top_level, 'replies': replies, 'anchors': anchors,
            'resolved': resolved, 'removed': removed, 'count': len(all_comments)}


#@register.filter(name='compare_categories'):
#def compare_categories(cat1, cat2):
