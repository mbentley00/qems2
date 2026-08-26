"""Write a database backup to BACKUP_DIR, copy it to blob storage, and rotate.

Uses pg_dump (custom format) when available; otherwise falls back to a gzipped
Django dumpdata fixture so a backup is always produced. Runs from inside Azure
(the app's container), so it reaches the Postgres server without a public
firewall rule. Dumps land on App Service's persistent /home by default.

Each dump is then uploaded to the container named by BACKUP_BLOB_CONTAINER in
the BACKUP_BLOB_ACCOUNT storage account, which is geo-redundant and outside the
App Service instance -- the local copies share their fate with the app. The
upload authenticates with the app's managed identity, so there is no key or
connection string to keep. It is best-effort: a storage outage must never cost
us the local backup, so a failure is reported and the command still succeeds.
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
        self._upload_to_blob(written)

        self.stdout.write('backup complete: {0} ({1} retained)'.format(
            os.path.basename(written), min(len(backups), retain)))

    def _upload_to_blob(self, path):
        """Copy one backup to blob storage. Never raises: the local dump is
        already written, and losing the off-site copy is not worth losing the
        run (or the daily loop that calls it)."""
        account = os.environ.get('BACKUP_BLOB_ACCOUNT')
        container = os.environ.get('BACKUP_BLOB_CONTAINER', 'qems2-backups')
        if not account:
            self.stdout.write('BACKUP_BLOB_ACCOUNT not set; keeping the local copy only.')
            return
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient
        except ImportError:
            self.stderr.write('azure-storage-blob/azure-identity not installed; '
                              'skipping the blob upload.')
            return
        name = os.path.basename(path)
        try:
            client = BlobServiceClient(
                account_url='https://{0}.blob.core.windows.net'.format(account),
                credential=DefaultAzureCredential())
            blob = client.get_blob_client(container=container, blob=name)
            with open(path, 'rb') as fh:
                blob.upload_blob(fh, overwrite=True)
            self.stdout.write('uploaded {0} to {1}/{2}'.format(name, account, container))
        except Exception as ex:  # noqa: BLE001 - see the docstring
            self.stderr.write('blob upload failed ({0}: {1}); the local copy is intact.'.format(
                type(ex).__name__, str(ex)[:200]))
