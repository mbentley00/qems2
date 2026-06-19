# Deploying QEMS2 to Azure

This documents how QEMS2 is deployed to the Azure App Service that runs at
**https://qems2-bbhfewbrfzhyhvbk.westus3-01.azurewebsites.net**.

> ⚠️ **Deploy from your local working tree, _not_ from a git archive.** The
> front-end dependencies live in the git-ignored `components/` directory
> (bower: jQuery, Foundation, jQuery-UI, underscore, tablesorter, sprintf,
> expanding-textareas, Font Awesome). A `git archive` / "committed files only"
> deploy omits them, so every interactive feature (Foundation tabs, AJAX) and
> all Font Awesome icons break with 404s. See [Pitfalls](#pitfalls).

## TL;DR

```bash
az login                                   # once per machine/session
python build_deploy_zip.py                 # builds ..\_deploy_local.zip from the local tree
az webapp deploy \
  --resource-group qems2_group \
  --name qems2 \
  --src-path C:/Users/mbent/claude/qems2/_deploy_local.zip \
  --type zip
```

Then verify (see [Verifying a deploy](#verifying-a-deploy)).

## What it deploys to

| Thing | Value |
|---|---|
| Azure subscription | Microsoft Azure Sponsorship (`mbentley@pacensc.onmicrosoft.com`) |
| App Service name | `qems2` |
| Resource group | `qems2_group` |
| Region | West US 3 |
| Runtime | Code / Oryx, `PYTHON\|3.14` (NOT a container — the `Dockerfile` is unused in prod) |
| Startup command | `bash entrypoint.sh` (gunicorn, 3 workers) |
| Public URL | https://qems2-bbhfewbrfzhyhvbk.westus3-01.azurewebsites.net |
| Continuous deploy | **None** (`scmType=None`). Deploys are pushed manually with `az`. |

There is **no** GitHub Actions / CD hook wired up. `.github/workflows/deploy.yml`
exists but is not the live mechanism. The code repo is
`github.com/grapesmoker/qems2`; pushing there does **not** deploy. (Note:
`qems2.grapesmoker.net` is a separate, unrelated deployment.)

## Prerequisites

- **Azure CLI** logged in to the right subscription: `az account show` should
  list "Microsoft Azure Sponsorship".
- Your **local working tree is complete**, including the git-ignored
  `components/bower_components/` directory (the bower front-end libs). If it's
  missing, the UI will break after deploy.
- Python (to run the packaging script).

## Step-by-step

### 1. Build the deploy zip from local

```bash
python build_deploy_zip.py
```

`build_deploy_zip.py` zips the local tree at `C:\Users\mbent\claude\qems2\qems2`
to `C:\Users\mbent\claude\qems2\_deploy_local.zip` (~79 MB, ~24.7k files). It:

- **includes** the git-ignored `components/` (bower front-end deps) — this is
  the whole point of deploying from local;
- **excludes** junk: `.git/`, the top-level collected `static/` (the server
  rebuilds it), `db.sqlite3`, `secret`, `nul`, `.claude/`, `__pycache__/`,
  `*.pyc`, `*.zip`, and any nested `.git` dirs.

### 2. Push the zip to Azure

```bash
az webapp deploy \
  --resource-group qems2_group \
  --name qems2 \
  --src-path C:/Users/mbent/claude/qems2/_deploy_local.zip \
  --type zip
```

A successful run reports `"status": "RuntimeSuccessful"`.

### What happens on the server

1. **Oryx build** runs because `SCM_DO_BUILD_DURING_DEPLOYMENT=true`:
   - `pip install -r requirements.txt` (installs `edge-tts`, `imageio-ffmpeg`,
     etc.);
   - `python manage.py collectstatic` — the `djangobower.finders.BowerFinder`
     reads `components/bower_components/` and gathers jQuery / Foundation /
     Font Awesome / etc. into `STATIC_ROOT`. WhiteNoise
     (`CompressedManifestStaticFilesStorage`) then serves them.
2. **`entrypoint.sh`** runs on container start:
   - `python manage.py migrate` → **applies migrations to the production
     Postgres DB**;
   - `python manage.py bootstrap_deploy` (question types + admin user);
   - launches `gunicorn` (3 workers).

> Because step 2 runs migrations, a deploy with new migrations changes the
> production database. Review pending migrations before deploying.

## Verifying a deploy

```bash
A=https://qems2-bbhfewbrfzhyhvbk.westus3-01.azurewebsites.net
curl -s -o /dev/null -w "%{http_code}\n" $A/                                  # 302 (login redirect) = healthy
curl -s -o /dev/null -w "%{http_code}\n" $A/static/foundation/js/vendor/jquery.js   # 200 = bower assets present
curl -s -o /dev/null -w "%{http_code}\n" $A/static/fontawesome/css/all.min.css      # 200 = Font Awesome present
```

If the bower/Font Awesome URLs return **404**, `components/` did not make it
into the deploy — rebuild the zip from local and redeploy.

Then hard-refresh the site (Ctrl+Shift+R) to clear any cached broken assets.

## Logs & diagnostics

- The `.scm.` hostname (`qems2-bbhfewbrfzhyhvbk.scm.westus3-01.azurewebsites.net`)
  is the **Kudu** management console (SSH, file browser, log stream) — it is
  *not* the website; it always shows the Kudu UI.
- Enable detailed request logging:
  `az webapp log config --name qems2 --resource-group qems2_group --application-logging filesystem --level information --detailed-error-messages true`
- Download recent logs:
  `az webapp log download --name qems2 --resource-group qems2_group --log-file logs.zip`
- Live stream: `az webapp log tail --name qems2 --resource-group qems2_group`

## Pitfalls

1. **Deploying from `git archive` / committed files only.** Omits the
   git-ignored `components/` → all interactive JS and Font Awesome icons 404.
   **Always deploy from the local tree** with `build_deploy_zip.py`.
2. **`entrypoint.sh` CRLF line endings.** If the file has Windows line endings,
   commands run as e.g. `bootstrap_deploy\r` → "Unknown command". Keep
   `entrypoint.sh` LF. (The unused `Dockerfile` strips CRLF; the Oryx path does
   not, so it matters here.)
3. **Confusing the `.scm.` host with the app.** `.scm.` is Kudu, not the site.
4. **`ffmpeg`.** There's no system `ffmpeg` on the Oryx app; the packet→MP3
   feature uses the `imageio-ffmpeg` pip package's bundled binary (already in
   `requirements.txt`; `audio._ffmpeg()` falls back to it).
5. **Production migrations.** `entrypoint.sh` runs `migrate` on every start.

## Files

- `build_deploy_zip.py` — builds the from-local deploy zip (repo root).
- `entrypoint.sh` — container startup (migrate → bootstrap → gunicorn).
- `requirements.txt` — Python deps (installed by Oryx during the build).
