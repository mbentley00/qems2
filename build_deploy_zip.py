"""Build a deploy zip from the LOCAL working tree (includes gitignored
components/ that the app needs), excluding junk. Server collectstatic will
rebuild static/ from components/ + qsub/static."""
import os
import re
import zipfile

ROOT = r"C:\Users\mbent\claude\qems2\qems2"
OUT = r"C:\Users\mbent\claude\qems2\_deploy_local.zip"

# Top-level entries to skip entirely
SKIP_TOP = {".git", "static", ".claude", "_logs"}
SKIP_FILES = {"db.sqlite3", "secret", "anthropic_key", "nul", "_logs.zip", "build_deploy_zip.py"}
# The app only uses fontawesome's css/all.min.css + webfonts/; the rest of the
# package (svgs, sprites, js-packages, metadata, ...) is ~22k files / ~65 MB.
FA_PREFIX = "components/bower_components/fontawesome/"
FA_KEEP = {"css", "webfonts"}
TEST_DB_RE = re.compile(r"^default_\d+\.sqlite3$")

def skip(rel):
    rel = rel.replace("\\", "/")
    parts = rel.split("/")
    if parts[0] in SKIP_TOP:
        return True
    if ".git" in parts or "__pycache__" in parts or "whoosh_index" in parts:
        return True
    if rel.startswith(FA_PREFIX):
        sub = rel[len(FA_PREFIX):].split("/")
        if len(sub) > 1 and sub[0] not in FA_KEEP:
            return True
    base = parts[-1]
    if base in SKIP_FILES or base.endswith((".pyc", ".zip")):
        return True
    # Leftover databases from `manage.py test --parallel` (default_1.sqlite3, ...)
    if TEST_DB_RE.match(base):
        return True
    return False

n = 0
key_hits = []
if os.path.exists(OUT):
    os.remove(OUT)
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT)
        if rel_dir == ".":
            rel_dir = ""
        # prune skipped dirs in-place for speed
        dirnames[:] = [d for d in dirnames
                       if not skip(os.path.join(rel_dir, d) if rel_dir else d)]
        for f in filenames:
            rel = os.path.join(rel_dir, f) if rel_dir else f
            if skip(rel):
                continue
            z.write(os.path.join(dirpath, f), rel.replace("\\", "/"))
            n += 1
            r = rel.replace("\\", "/")
            if ("foundation/js/foundation.min.js" in r
                    or "fontawesome/css/all.min.css" in r
                    or r in ("manage.py", "requirements.txt", "entrypoint.sh")):
                key_hits.append(r)

size = os.path.getsize(OUT) / (1024 * 1024)
print("files:", n)
print("zip size: {0:.1f} MB".format(size))
print("key entries present:")
for k in key_hits:
    print("  ", k)
