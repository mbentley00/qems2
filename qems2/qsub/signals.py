import re
import sys
import threading

from django.contrib.auth.models import User
from django.dispatch import receiver
from django.db.models.signals import post_save
from django.contrib.sites.models import Site
from django.core.mail import send_mail
from django.db.models import Q
from django.conf import settings
from django_comments.models import Comment

from qems2.qsub.models import (Tossup, Bonus, Writer, WriterQuestionSetSettings,
                               PerCategoryWriterSettings, CommentMention)


# @username mention (Django usernames: letters/digits and . @ + - _)
_MENTION_RE = re.compile(r'@([\w.@+\-]+)')


@receiver(post_save, sender=Comment)
def record_comment_mentions(sender, instance, created, **kwargs):
    """When a comment is posted, record @username mentions so the mentioned
    writers see them in their activity feed."""
    if not created or '@' not in (instance.comment or ''):
        return
    try:
        names = set(m.group(1).rstrip('.') for m in _MENTION_RE.finditer(instance.comment))
        if not names:
            return
        for user in User.objects.filter(username__in=names).select_related('writer'):
            writer = getattr(user, 'writer', None)
            if writer is None:
                continue  # skip @names with no writer profile
            CommentMention.objects.get_or_create(comment=instance, mentioned=writer)
    except Exception:
        print("Error recording comment mentions:", sys.exc_info()[0], sys.exc_info()[1])


def _send_mail_async(subject, body, recipients, html=None):
    """Send notification mail off the request thread so posting a comment or
    question never blocks on SMTP. Sends an HTML alternative when provided."""
    def _send():
        try:
            from django.core.mail import EmailMultiAlternatives
            # Send from DEFAULT_FROM_EMAIL: transactional providers require the
            # From to be a verified sender, which the SMTP username (e.g.
            # "apikey"/"resend") is not.
            msg = EmailMultiAlternatives(subject, body, settings.DEFAULT_FROM_EMAIL, list(recipients))
            if html:
                msg.attach_alternative(html, 'text/html')
            msg.send(fail_silently=True)
        except Exception:
            print("Error sending notification mail:", sys.exc_info()[0], sys.exc_info()[1])
    threading.Thread(target=_send, daemon=True).start()


def _question_url(question):
    page = 'edit_tossup' if isinstance(question, Tossup) else 'edit_bonus'
    return '{0}/{1}/{2}'.format(settings.BASE_URL, page, question.id)


def _commenter_display(user, user_name=''):
    """Friendly name for a comment's author: real name with username in quotes,
    just the username, or a bot's posted name."""
    if user is None:
        return (user_name or 'Someone').strip()
    real = '{0} {1}'.format(user.first_name or '', user.last_name or '').strip()
    return '{0} ("{1}")'.format(real, user.username) if real else user.username


@receiver(post_save, sender=Comment)
def email_on_comments(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        target = instance.content_object
        if not isinstance(target, (Tossup, Bonus)):
            return

        mail_set = set()
        author = target.author
        if author and author.user and author.user.email and author.send_mail_on_comments:
            mail_set.add(author.user.email)

        # Everyone on this comment thread (one query)
        thread_comments = (Comment.objects
                           .filter(object_pk=target.id, content_type_id=instance.content_type_id)
                           .select_related('user__writer'))
        for comment in thread_comments:
            # Bot-posted comments (e.g. from the Discord API) have no Django user.
            writer = getattr(comment.user, 'writer', None) if comment.user else None
            if writer is not None and writer.send_mail_on_comments:
                mail_set.add(comment.user.email)

        # Subscribers to all comments on the set, or to this category (two queries)
        set_subscribers = (WriterQuestionSetSettings.objects
                           .filter(question_set=target.question_set, email_on_all_new_comments=True)
                           .select_related('writer__user'))
        for set_settings in set_subscribers:
            mail_set.add(set_settings.writer.user.email)

        category_subscribers = (PerCategoryWriterSettings.objects
                                .filter(writer_question_set_settings__question_set=target.question_set,
                                        distribution_entry=target.category,
                                        email_on_new_comments=True)
                                .select_related('writer_question_set_settings__writer__user'))
        for category_settings in category_subscribers:
            mail_set.add(category_settings.writer_question_set_settings.writer.user.email)

        # No mail for your own comment (bot comments have no Django user).
        if instance.user is not None:
            mail_set.discard(instance.user.email)
        mail_set.discard(None)
        mail_set.discard('')
        if not mail_set:
            return

        from django.utils.html import escape
        commenter = _commenter_display(instance.user, instance.user_name)
        qset = str(target.question_set)
        kind = 'tossup' if isinstance(target, Tossup) else 'bonus'
        answer_label = str(target).replace('_', '').replace('~', '').strip()
        url = _question_url(target)
        try:
            question_text = target.to_plain_text()
        except Exception:
            question_text = ''

        # Full discussion (chronological), so the email carries the context.
        thread = list(Comment.objects.filter(
            object_pk=str(target.id), content_type_id=instance.content_type_id,
            is_removed=False).select_related('user').order_by('submit_date'))

        # ---- plain-text body ----
        lines = ['{0} commented on a {1} ("{2}") in the set "{3}":'.format(
            commenter, kind, answer_label, qset), '', instance.comment or '']
        if question_text:
            lines += ['', 'The question:', question_text]
        if len(thread) > 1:
            lines += ['', 'Full discussion:']
            for c in thread:
                lines.append('- {0}: {1}'.format(
                    _commenter_display(c.user, c.user_name), c.comment or ''))
        lines += ['', 'View and reply: ' + url,
                  '', 'To opt out of these e-mails, change the settings in your profile.']
        body = '\n'.join(lines)

        # ---- HTML body ----
        def esc(s):
            return escape(s or '')
        html = [
            '<div style="font-family:Arial,Helvetica,sans-serif;color:#222;max-width:640px;line-height:1.5;">',
            '<p><strong>{0}</strong> commented on a {1} in <strong>{2}</strong>:</p>'.format(
                esc(commenter), kind, esc(qset)),
            '<blockquote style="border-left:3px solid #008CBA;background:#f5f9ff;'
            'margin:0 0 18px;padding:8px 14px;">{0}</blockquote>'.format(esc(instance.comment)),
        ]
        if question_text:
            html.append('<h3 style="margin:0 0 6px;font-size:15px;color:#444;">The question</h3>'
                        '<div style="background:#fafafa;border:1px solid #eee;border-radius:4px;'
                        'padding:10px 12px;margin-bottom:18px;white-space:pre-wrap;">{0}</div>'.format(
                            esc(question_text)))
        if len(thread) > 1:
            html.append('<h3 style="margin:0 0 6px;font-size:15px;color:#444;">'
                        'Discussion ({0} comments)</h3>'.format(len(thread)))
            html.append('<div style="border:1px solid #eee;border-radius:4px;">')
            for i, c in enumerate(thread):
                bg = '#ffffff' if i % 2 == 0 else '#f7f7f7'
                html.append(
                    '<div style="padding:8px 12px;background:{0};border-bottom:1px solid #eee;">'
                    '<div style="color:#555;font-size:13px;"><strong>{1}</strong> '
                    '<span style="color:#999;">{2}</span></div>'
                    '<div>{3}</div></div>'.format(
                        bg, esc(_commenter_display(c.user, c.user_name)),
                        c.submit_date.strftime('%b %d, %Y %I:%M %p') if c.submit_date else '',
                        esc(c.comment)))
            html.append('</div>')
        html.append('<p style="margin:18px 0;"><a href="{0}" style="display:inline-block;'
                    'background:#008CBA;color:#fff;padding:9px 16px;border-radius:4px;'
                    'text-decoration:none;">View &amp; reply</a></p>'.format(esc(url)))
        html.append('<p style="color:#999;font-size:12px;">You\'re receiving this because you '
                    'wrote or follow this question or set. To opt out, change the settings in '
                    'your profile.</p></div>')
        html_body = '\n'.join(html)

        subject = 'New QEMS3 comment on "{0}" in {1}'.format(answer_label, qset)
        _send_mail_async(subject, body, mail_set, html=html_body)
    except Exception:
        print("Error sending mail for comments:", sys.exc_info()[0], sys.exc_info()[1])


def _email_on_new_question(instance):
    try:
        qset_obj = instance.question_set
        mail_set = set()

        set_subscribers = (WriterQuestionSetSettings.objects
                           .filter(question_set=qset_obj, email_on_all_new_questions=True)
                           .exclude(writer=instance.author)
                           .select_related('writer__user'))
        for set_settings in set_subscribers:
            mail_set.add(set_settings.writer.user.email)

        category_subscribers = (PerCategoryWriterSettings.objects
                                .filter(writer_question_set_settings__question_set=qset_obj,
                                        distribution_entry=instance.category,
                                        email_on_new_questions=True)
                                .exclude(writer_question_set_settings__writer=instance.author)
                                .select_related('writer_question_set_settings__writer__user'))
        for category_settings in category_subscribers:
            mail_set.add(category_settings.writer_question_set_settings.writer.user.email)

        if not mail_set:
            return

        qset = str(qset_obj)
        subject = "New QEMS3 question for " + str(instance) + " in set " + qset
        body = ('{0!s} has written a new question for the set {1!s}:\n\n{2!s}\n\n'
                'View the question at {3!s}.\n\n'
                'To opt out of these e-mails, change the settings in your profile.').format(
            str(instance.author), qset, instance.to_plain_text(), _question_url(instance))
        _send_mail_async(subject, body, mail_set)
    except Exception:
        print("Error sending mail for new question:", sys.exc_info()[0], sys.exc_info()[1])


@receiver(post_save, sender=Tossup)
def email_on_new_tossup(sender, instance, created, **kwargs):
    if created:
        _email_on_new_question(instance)


@receiver(post_save, sender=Bonus)
def email_on_new_bonus(sender, instance, created, **kwargs):
    if created:
        _email_on_new_question(instance)
