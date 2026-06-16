"""Idempotent first-boot setup for a fresh deployment.

Ensures the three QuestionType rows the app depends on exist, and creates the
admin superuser from DJANGO_SUPERUSER_* environment variables if it isn't there
yet. Safe to run on every container start: it never deletes or overwrites data.
"""

import os

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

from qems2.qsub.models import QuestionType
from qems2.qsub.utils import ACF_STYLE_TOSSUP, ACF_STYLE_BONUS, VHSL_BONUS


class Command(BaseCommand):
    help = 'Idempotently set up question types and the admin user for a new deployment.'

    def handle(self, *args, **options):
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
