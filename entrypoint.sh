#!/usr/bin/env sh
# First-boot/startup sequence for the container. Idempotent: safe on every
# restart. App Service injects $PORT and the configured app settings as env vars.
set -e

echo "[entrypoint] applying database migrations"
python manage.py migrate --noinput

echo "[entrypoint] bootstrapping question types + admin user"
python manage.py bootstrap_deploy

# Daily database backup. Runs as a single background process (one per
# container, not per gunicorn worker) so it reaches Postgres from inside Azure
# without a public firewall rule. Dumps land in $BACKUP_DIR (default
# /home/backups, persistent App Service storage) with rotation.
echo "[entrypoint] launching daily backup scheduler"
python manage.py backup_scheduler &

echo "[entrypoint] starting gunicorn on port ${PORT:-8000}"
exec gunicorn qems2.wsgi:application --bind "0.0.0.0:${PORT:-8000}" --workers 3 --timeout 120
