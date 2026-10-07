"""Site news: what's unread for someone, and the bar that mentions it.

Everything posted after a person's ``news_seen_at`` (or, for someone who has
never looked, after they joined -- a new account isn't greeted with years of
old news) is unread. The bar shows the newest unread announcement on their
pages until they dismiss it or open the News page; if they do neither, it
gives up on its own after Announcement.NUDGE_PAGES pages or NUDGE_DAYS days.
"""

from datetime import timedelta

from django.utils import timezone

from qems2.qsub.models import Announcement


def _since(writer):
    return writer.news_seen_at or writer.user.date_joined


def unread(writer):
    return Announcement.objects.filter(created_date__gt=_since(writer))


def mark_seen(writer):
    writer.news_seen_at = timezone.now()
    writer.news_nudge_id = None
    writer.news_nudge_count = 0
    writer.news_nudge_started = None
    writer.save(update_fields=['news_seen_at', 'news_nudge_id', 'news_nudge_count', 'news_nudge_started'])


def banner_for(writer):
    """The announcement to show in the bar on this page, or None. Counts the
    showing, so call it once per full page."""
    latest = unread(writer).first()
    if latest is None:
        return None
    now = timezone.now()
    if writer.news_nudge_id != latest.id:
        # A newer announcement starts its own count.
        writer.news_nudge_id = latest.id
        writer.news_nudge_count = 0
        writer.news_nudge_started = now
    if (writer.news_nudge_count >= Announcement.NUDGE_PAGES or
            now - writer.news_nudge_started > timedelta(days=Announcement.NUDGE_DAYS)):
        return None
    writer.news_nudge_count += 1
    writer.save(update_fields=['news_nudge_id', 'news_nudge_count', 'news_nudge_started'])
    return latest
