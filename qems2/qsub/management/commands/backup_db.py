"""Write a database backup to BACKUP_DIR and rotate old ones.

Uses pg_dump (custom format) when available; otherwise falls back to a gzipped
Django dumpdata fixture so a backup is always produced. Runs from inside Azure
(the app's container), so it reaches the Postgres server without a public
firewall rule. Dumps land on App Service's persistent /home by default.
"""

import glob
import gzip
import os
import subprocess
import time

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Back up the database to BACKUP_DIR (pg_dump, with dumpdata fallback) and rotate old backups.'

    def handle(self, *args, **options):
        backup_dir = os.environ.get('BACKUP_DIR', '/home/backups')
        retain = int(os.environ.get('BACKUP_RETAIN', '14'))
        os.makedirs(backup_dir, exist_ok=True)

        db = settings.DATABASES['default']
        stamp = time.strftime('%Y%m%d_%H%M%S', time.gmtime())
        written = None

        if 'postgresql' in db['ENGINE']:
            dump_path = os.path.join(backup_dir, 'qems2_{0}.dump'.format(stamp))
            env = dict(os.environ, PGPASSWORD=db.get('PASSWORD', '') or '')
            cmd = ['pg_dump', '-Fc', '--no-owner', '--no-privileges',
                   '-h', db['HOST'], '-p', str(db.get('PORT') or 5432),
                   '-U', db['USER'], '-d', db['NAME'], '-f', dump_path]
            try:
                subprocess.run(cmd, env=env, check=True, capture_output=True)
                written = dump_path
                self.stdout.write('pg_dump wrote ' + dump_path)
            except FileNotFoundError:
                self.stdout.write('pg_dump not installed; using dumpdata fallback')
            except subprocess.CalledProcessError as ex:
                err = ex.stderr.decode('utf-8', 'replace') if ex.stderr else str(ex)
                self.stderr.write('pg_dump failed ({0}); using dumpdata fallback'.format(err.strip()[:200]))

        if written is None:
            # Portable JSON fixture of the meaningful data, gzipped.
            written = os.path.join(backup_dir, 'qems2_{0}.json.gz'.format(stamp))
            with gzip.open(written, 'wt', encoding='utf-8') as fh:
                call_command('dumpdata', 'qsub', 'django_comments', 'auth.User', 'account', 'sites',
                             natural_foreign=True, natural_primary=True, indent=0, stdout=fh)
            self.stdout.write('dumpdata wrote ' + written)

        # Rotate: keep the newest `retain` backups.
        backups = sorted(glob.glob(os.path.join(backup_dir, 'qems2_*')), key=os.path.getmtime)
        for old in backups[:-retain] if retain > 0 else []:
            try:
                os.remove(old)
            except OSError:
                pass
        self.stdout.write('backup complete: {0} ({1} retained)'.format(
            os.path.basename(written), min(len(backups), retain)))
