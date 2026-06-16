"""Long-running process that runs `backup_db` once a day.

Launched in the background from entrypoint.sh (one process per container, not
per gunicorn worker). On startup it runs a catch-up backup if the newest one is
missing or stale, so a backup exists even when the container restarts before the
daily time. Then it sleeps until BACKUP_HOUR_UTC each day and backs up again.
Failures are logged but never kill the loop.
"""

import glob
import os
import time
from datetime import datetime, timedelta, timezone

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Run backup_db once per day at BACKUP_HOUR_UTC (default 08:00 UTC).'

    def handle(self, *args, **options):
        hour = int(os.environ.get('BACKUP_HOUR_UTC', '8'))
        self.stdout.write('[backup_scheduler] started; daily backup at {0:02d}:00 UTC'.format(hour))

        if self._latest_backup_age_hours() is None or self._latest_backup_age_hours() >= 20:
            self.stdout.write('[backup_scheduler] no recent backup found; running startup catch-up')
            self._run_backup()

        while True:
            now = datetime.now(timezone.utc)
            nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            time.sleep(max(60, (nxt - now).total_seconds()))
            self._run_backup()

    def _run_backup(self):
        try:
            call_command('backup_db')
        except Exception as ex:  # noqa: BLE001 - never let the loop die
            self.stderr.write('[backup_scheduler] backup failed: {0}'.format(ex))

    @staticmethod
    def _latest_backup_age_hours():
        backup_dir = os.environ.get('BACKUP_DIR', '/home/backups')
        files = glob.glob(os.path.join(backup_dir, 'qems2_*'))
        if not files:
            return None
        newest = max(os.path.getmtime(f) for f in files)
        return (time.time() - newest) / 3600.0
