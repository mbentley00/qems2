"""Copy the production database backups from Azure down to this machine.

The app writes a dump to /home/backups every day at 08:00 UTC and keeps the
newest 14 (see qsub/management/commands/backup_db.py). Those live on the App
Service instance's own storage, in the same region and subscription as
everything else, so this script pulls them somewhere else entirely -- run it
weekly and a copy of the data survives losing the Azure account.

    python pull_backups.py                 # fetch anything new
    python pull_backups.py --all           # re-fetch even files already here
    python pull_backups.py --dest D:\\qems  # somewhere other than the default
    python pull_backups.py --keep 26       # how many local copies to keep

Needs the Azure CLI signed in (`az login`); it reads the site's publishing
credentials the same way a deploy does. Nothing is deleted on the server.
"""

import argparse
import json
import os
import subprocess
import sys
import time

import requests

APP = 'qems2'
RESOURCE_GROUP = 'qems2_group'
KUDU = 'https://qems2-bbhfewbrfzhyhvbk.scm.westus3-01.azurewebsites.net'
REMOTE_DIR = '/api/vfs/backups/'   # the VFS root is /home
DEFAULT_DEST = os.path.join(os.path.expanduser('~'), 'qems2-backups')
DEFAULT_KEEP = 26          # ~6 months of weekly pulls


def publishing_auth():
    """(user, password) for the site, from the Azure CLI."""
    try:
        raw = subprocess.check_output(
            ['az', 'webapp', 'deployment', 'list-publishing-credentials',
             '--name', APP, '--resource-group', RESOURCE_GROUP, '-o', 'json'],
            shell=(os.name == 'nt'), stderr=subprocess.PIPE)
    except FileNotFoundError:
        sys.exit('The Azure CLI (az) is not on PATH.')
    except subprocess.CalledProcessError as ex:
        detail = (ex.stderr or b'').decode('utf-8', 'replace').strip()
        sys.exit('Could not read publishing credentials -- is "az login" current?\n' + detail)
    creds = json.loads(raw)
    return creds['publishingUserName'], creds['publishingPassword']


def remote_backups(auth):
    """[{name, size, mtime}] for every backup on the server, newest last."""
    r = requests.get(KUDU + REMOTE_DIR, auth=auth, timeout=120)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    files = [f for f in r.json() if f.get('mime') != 'inode/directory'
             and f['name'].startswith('qems2_')]
    files.sort(key=lambda f: f['name'])
    return files


def download(auth, name, dest_dir):
    """Fetch one backup, writing through a .part file so an interrupted run
    never leaves a truncated backup looking complete."""
    target = os.path.join(dest_dir, name)
    part = target + '.part'
    with requests.get(KUDU + REMOTE_DIR + name, auth=auth, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(part, 'wb') as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    os.replace(part, target)
    return os.path.getsize(target)


def prune(dest_dir, keep):
    """Keep the newest `keep` local backups; report what went."""
    local = sorted(f for f in os.listdir(dest_dir) if f.startswith('qems2_') and not f.endswith('.part'))
    removed = []
    for name in local[:-keep] if keep > 0 else []:
        try:
            os.remove(os.path.join(dest_dir, name))
            removed.append(name)
        except OSError:
            pass
    return removed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dest', default=os.environ.get('QEMS_BACKUP_DEST', DEFAULT_DEST),
                    help='where to put them (default: %(default)s)')
    ap.add_argument('--keep', type=int, default=int(os.environ.get('QEMS_BACKUP_KEEP', DEFAULT_KEEP)),
                    help='how many local copies to keep (0 = never prune; default: %(default)s)')
    ap.add_argument('--all', action='store_true', help='re-download files already here')
    args = ap.parse_args()

    os.makedirs(args.dest, exist_ok=True)
    print('{0}  pulling {1} backups -> {2}'.format(
        time.strftime('%Y-%m-%d %H:%M:%S'), APP, args.dest))

    auth = publishing_auth()
    remote = remote_backups(auth)
    if not remote:
        sys.exit('No backups found on the server -- has the daily job run?')

    here = set(os.listdir(args.dest))
    wanted = [f for f in remote if args.all or f['name'] not in here]
    if not wanted:
        print('  already up to date ({0} on the server, newest {1})'.format(
            len(remote), remote[-1]['name']))
    for f in wanted:
        size = download(auth, f['name'], args.dest)
        print('  downloaded {0} ({1:.1f} MB)'.format(f['name'], size / 1048576.0))

    removed = prune(args.dest, args.keep)
    for name in removed:
        print('  pruned local ' + name)

    local = sorted(f for f in os.listdir(args.dest) if f.startswith('qems2_'))
    total = sum(os.path.getsize(os.path.join(args.dest, f)) for f in local)
    print('  {0} local backup(s), {1:.1f} MB, newest {2}'.format(
        len(local), total / 1048576.0, local[-1] if local else 'none'))
    # A backup nobody has restored is a hope, not a backup:
    print('  restore: createdb, then manage.py migrate, then '
          'manage.py loaddata <file> (these are gzipped dumpdata fixtures)')


if __name__ == '__main__':
    main()
