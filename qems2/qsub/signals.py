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


def _send_mail_async(subject, body, recipients):
    """Send notification mail off the request thread so posting a comment or
    question never blocks on SMTP."""
    def _send():
        try:
            # Send from DEFAULT_FROM_EMAIL: transactional providers require the
            # From to be a verified sender, which the SMTP username (e.g.
            # "apikey"/"resend") is not.
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, list(recipients), fail_silently=True)
        except Exception:
            print("Error sending notification mail:", sys.exc_info()[0], sys.exc_info()[1])
    threading.Thread(target=_send, daemon=True).start()


def _question_url(question):
    domain = Site.objects.get_current().domain
    page = 'edit_tossup' if isinstance(question, Tossup) else 'edit_bonus'
    return 'http://{0}/{1}/{2}'.format(domain, page, question.id)


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

        commenter = (instance.user_name or '').strip() or (
            str(instance.user) if instance.user else 'someone')
        qset = str(target.question_set)
        subject = "New QEMS2 comment for " + str(target) + " in set " + qset
        body = ('The question on "{0!s}" for the set "{1!s}" has a new comment by {2!s}:\n\n{3!s}\n\n'
                'View the question at {4!s}.\n\n'
                'To opt out of these e-mails, change the settings in your profile.').format(
            str(target), qset, commenter, instance.comment, _question_url(target))
        _send_mail_async(subject, body, mail_set)
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
        subject = "New QEMS2 question for " + str(instance) + " in set " + qset
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
