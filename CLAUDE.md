# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

QEMS2 is a Django web app for quizbowl question submission, editing, and packetization. Writers submit questions, editors review/lock them, and owners manage question sets and export packets.

**Stack:** Python 3.14, Django 6.0, SQLite, jQuery/Foundation frontend, Haystack/Whoosh search

## Commands

```bash
# Run dev server (http://localhost:8000, admin/admin)
python manage.py runserver

# Run tests
python manage.py test qems2.tests

# Database migrations
python manage.py makemigrations
python manage.py migrate

# Populate default data (categories, question types, etc.)
python manage.py shell < qems2/qsub/populate_db_with_default_data.py

# Frontend dependencies (django-bower is broken with Django 6.0, use bower directly)
cd components && bower install
python manage.py collectstatic
```

## Architecture

Single Django app (`qems2.qsub`) with all logic:

- **`qems2/qsub/models.py`** — Core models: `QuestionSet`, `Tossup`, `Bonus`, `Packet`, `Writer`, `Distribution`, `CategoryEntry`, `Period`, `Role`, `QuestionHistory`. Writer has a OneToOne to Django's User.
- **`qems2/qsub/views.py`** — All views (~4300 lines, 65+ view functions). Function-based views, no class-based views. Handles both page rendering and AJAX/JSON endpoints.
- **`qems2/urls.py`** — All URL routing (~89 patterns using `re_path`). JSON API endpoints prefixed with comments.
- **`qems2/qsub/forms.py`** — Django forms for registration, question sets, roles, writer settings.
- **`qems2/qsub/model_utils.py`** — Database utilities, question type conversion (tossup↔bonus, ACF↔VHSL).
- **`qems2/qsub/utils.py`** — HTML sanitization, character counting, smart quote conversion. Defines question type constants (`ACF_STYLE_TOSSUP`, `ACF_STYLE_BONUS`, `VHSL_BONUS`).
- **`qems2/qsub/packet_parser.py`** — Parses uploaded plain-text question files into Tossup/Bonus objects.
- **`qems2/qsub/packetizer.py`** — PDF/packet generation and export logic.
- **`qems2/qsub/signals.py`** — Post-save signal handlers for model updates.
- **`qems2/qsub/templatetags/filters.py`** — Custom template filters.

## Key Domain Concepts

**Roles hierarchy:** Owner > Editor > Writer. Owners create question sets and assign editors/writers. Editors can edit/lock any question. Writers can only edit their own unlocked questions.

**Question types:** Three `QuestionType` records: "ACF-style tossup", "ACF-style bonus", "VHSL bonus". Questions can be converted between types via `model_utils.py`.

**Distribution system:** Distributions define per-packet category quotas (min/max tossups/bonuses). `CategoryEntry` → `CategoryEntryForDistribution` → `PeriodWideEntry` → `PeriodWideCategoryEntry` tracks fulfillment at set and packet levels.

**Categories:** Defined as tuples in `models.py` (e.g., `CATEGORIES`, `RELIGION_SUBTYPES`, `FINE_ARTS_SUBTYPES`). Codes like `'S-P'` for Science-Physics, `'L-AM'` for Literature-American.

## Frontend

Templates in `qems2/qsub/templates/`. Base template loads Foundation CSS, jQuery, jQuery UI, Underscore, tablesorter, and custom `qsub.js`. Bower components in `components/bower_components/`. App CSS in `qems2/qsub/static/css/base.css`.

## Important Notes

- The `secret` file in the project root contains the Django SECRET_KEY (not in git).
- Email verification is disabled (`ACCOUNT_EMAIL_VERIFICATION='none'`).
- Database was migrated from MySQL to SQLite as part of the Python 2 → 3.14 / Django 1.x → 6.0 migration.
- `django-bower`'s management command is incompatible with Django 6.0; run `bower` directly from the `components/` directory.
- `ACCOUNT_EMAIL_REQUIRED` deprecation warning from django-allauth 65.x is cosmetic.
