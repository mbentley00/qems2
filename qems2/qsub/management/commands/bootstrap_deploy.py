"""Idempotent first-boot setup for a fresh deployment.

Ensures the three QuestionType rows the app depends on exist, points the
django.contrib.sites record at this deployment, and creates the admin
superuser from DJANGO_SUPERUSER_* environment variables if it isn't there yet.
Safe to run on every container start: it never deletes or overwrites data.
"""

import os
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.contrib.sites.models import Site

from qems2.qsub.models import QuestionType
from qems2.qsub.utils import ACF_STYLE_TOSSUP, ACF_STYLE_BONUS, VHSL_BONUS


def sync_site(stdout=None):
    """Point the Site row at BASE_URL.

    allauth builds its e-mails (password reset, e-mail confirmation) from the
    current Site, so a stale row is what made a reset mail read "Hello from
    example.com!" with example.com links -- example.com being the row Django
    ships with. QEMS's own notification mail uses settings.BASE_URL directly
    and was never affected.
    """
    domain = (urlparse(settings.BASE_URL).netloc or settings.BASE_URL).strip('/')
    if not domain:
        return None
    name = getattr(settings, 'SITE_NAME', '') or 'QEMS3'
    site, _created = Site.objects.get_or_create(
        pk=getattr(settings, 'SITE_ID', 1), defaults={'domain': domain, 'name': name})
    if site.domain != domain or site.name != name:
        site.domain, site.name = domain, name
        site.save(update_fields=['domain', 'name'])
        if stdout:
            stdout.write('site set to "{0}" ({1}).'.format(name, domain))
    elif stdout:
        stdout.write('site already "{0}" ({1}).'.format(name, domain))
    return site


class Command(BaseCommand):
    help = 'Idempotently set up question types and the admin user for a new deployment.'

    def handle(self, *args, **options):
        sync_site(self.stdout)

        for qtype in (ACF_STYLE_TOSSUP, ACF_STYLE_BONUS, VHSL_BONUS):
            obj, created = QuestionType.objects.get_or_create(question_type=qtype)
            self.stdout.write(('created' if created else 'exists') + ' question type: ' + qtype)

        username = os.environ.get('DJANGO_SUPERUSER_USERNAME')
        password = os.environ.get('DJANGO_SUPERUSER_PASSWORD')
        email = os.environ.get('DJANGO_SUPERUSER_EMAIL', '')

        if not username or not password:
            self.stdout.write('DJANGO_SUPERUSER_USERNAME/PASSWORD not set; skipping admin creation.')
            return

        if User.objects.filter(username=username).exists():
            self.stdout.write('superuser "{0}" already exists; leaving it unchanged.'.format(username))
        else:
            User.objects.create_superuser(username=username, email=email, password=password)
            self.stdout.write('created superuser "{0}".'.format(username))
