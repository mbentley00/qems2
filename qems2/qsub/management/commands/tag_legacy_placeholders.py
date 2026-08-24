"""Rename imported placeholder accounts that predate the "-legacy" convention.

The TSV importer creates a stand-in account for every comment handle it does
not recognize, so an imported comment keeps its author's name. Early imports
named those accounts after the handle alone, which makes them hard to tell
from a real person and means the real person can never register under their
own handle. This renames them to "<handle>-legacy", matching what imported
question authors already get.

A placeholder is an account that is *all* of:
  * inactive,
  * has no usable password,
  * has never logged in,
  * is not a staff account or superuser,
  * has no first/last name and no e-mail (the importer sets none), and
  * does not already end in "-legacy".

Dry run by default; pass --apply to write.

    python manage.py tag_legacy_placeholders
    python manage.py tag_legacy_placeholders --apply
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from qems2.qsub.set_importer import LEGACY_SUFFIX, legacy_username


def placeholder_users():
    """Every account that looks like an importer placeholder without the tag.

    Deliberately conservative: anything a person has touched (a name, an
    e-mail, a login, staff rights, a usable password) is left alone, since a
    rename would change how they sign in."""
    candidates = (User.objects
                  .filter(is_active=False, is_staff=False, is_superuser=False,
                          last_login__isnull=True, first_name='', last_name='', email='')
                  .exclude(username__endswith=LEGACY_SUFFIX)
                  .order_by('username'))
    return [u for u in candidates if not u.has_usable_password()]


class Command(BaseCommand):
    help = 'Rename pre-convention imported placeholder accounts to "<handle>-legacy".'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write the changes (otherwise the command only reports).')

    def handle(self, *args, **options):
        users = placeholder_users()
        if not users:
            self.stdout.write('No untagged placeholder accounts found.')
            return

        taken = set(User.objects.filter(
            username__in=[legacy_username(u.username) for u in users]
        ).values_list('username', flat=True))

        renamed, skipped = [], []
        for user in users:
            target = legacy_username(user.username)
            if target in taken:
                # A tagged placeholder for the same handle already exists;
                # merging accounts is not this command's business.
                skipped.append((user.username, target))
                continue
            renamed.append((user.username, target))
            taken.add(target)

        for old, new in renamed:
            self.stdout.write('{0}{1} -> {2}'.format(
                '' if options['apply'] else 'would rename ', old, new))
        for old, new in skipped:
            self.stdout.write(self.style.WARNING(
                'skipped {0}: {1} already exists'.format(old, new)))

        if not options['apply']:
            self.stdout.write(self.style.NOTICE(
                '\n{0} account(s) would be renamed, {1} skipped. '
                'Re-run with --apply to write.'.format(len(renamed), len(skipped))))
            return

        by_name = {u.username: u for u in users}
        with transaction.atomic():
            for old, new in renamed:
                user = by_name[old]
                user.username = new
                user.save(update_fields=['username'])
        self.stdout.write(self.style.SUCCESS(
            '\nRenamed {0} account(s); {1} skipped.'.format(len(renamed), len(skipped))))
