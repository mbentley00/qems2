"""Create a new QuestionSet from an uploaded TSV/CSV in the same format that
export_question_set produces: a tossup section, a bonus section, and a
distribution section, each introduced by its header row and separated by a
blank row. Comments (including threaded replies) are recreated, and the new
set's questions are pushed into the search index.

Admin-only; wired up in views.import_set.
"""

import csv
import io

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.models import Site
from django.db import transaction
from django.utils import timezone
from django_comments.models import Comment

from qems2.qsub.models import (QuestionSet, Distribution, DistributionEntry,
                               SetWideDistributionEntry, Tossup, Bonus,
                               QuestionType, CommentReply)
from qems2.qsub.utils import (ACF_STYLE_TOSSUP, ACF_STYLE_BONUS, VHSL_BONUS,
                              QUESTION_CREATE)
from qems2.qsub import signals as qems_signals
from django.db.models.signals import post_save


TOSSUP_HEADER = 'Tossup Question'
BONUS_HEADER = 'Bonus Leadin'
REPLY_PREFIX = '  > '


class SetImportError(Exception):
    pass


def _to_bool(val):
    return str(val).strip().lower() in ('true', '1', 'yes', 'on')


def _to_int(val, default=0):
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return default


def _row_is_blank(row):
    return not row or all((cell or '').strip() == '' for cell in row)


def _split_sections(rows):
    """Bucket data rows into tossup/bonus/distribution sections by their
    header rows."""
    sections = {'tossup': [], 'bonus': [], 'dist': []}
    current = None
    for row in rows:
        if _row_is_blank(row):
            current = None
            continue
        head = (row[0] or '').strip()
        if head == TOSSUP_HEADER:
            current = 'tossup'
            continue
        if head == BONUS_HEADER:
            current = 'bonus'
            continue
        if head == 'Category' and len(row) >= 2 and (row[1] or '').strip() == 'Subcategory':
            current = 'dist'
            continue
        if current is not None:
            sections[current].append(row)
    return sections


def _parse_comment_cell(cell):
    """Parse an exported comment cell into [(username, text, is_reply), ...].
    Export format: top-level "user: text", replies "  > user: text", joined
    by "||"."""
    cell = cell or ''
    parsed = []
    for piece in cell.split('||'):
        if not piece.strip():
            continue
        if piece.startswith(REPLY_PREFIX):
            is_reply = True
            body = piece[len(REPLY_PREFIX):]
        else:
            is_reply = False
            body = piece.strip()
        if ': ' in body:
            username, text = body.split(': ', 1)
        else:
            username, text = '', body
        parsed.append((username.strip(), text, is_reply))
    return parsed


def _user_resolver(fallback_user):
    """Returns a function that maps an exported username to a User, creating an
    inactive placeholder for unknown commenters (preserving attribution), and
    caching results."""
    cache = {}

    def resolve(username):
        if not username:
            return fallback_user
        if username in cache:
            return cache[username]
        username = username[:150]
        user = User.objects.filter(username=username).first()
        if user is None:
            try:
                user = User(username=username, is_active=False)
                user.set_unusable_password()
                user.save()
            except Exception:
                user = fallback_user
        cache[username] = user
        return user

    return cache, resolve


def _create_comments(question, content_type, site, comment_cell, resolve):
    """Recreate a question's comments and threaded replies. Returns the number
    of comments created."""
    entries = _parse_comment_cell(comment_cell)
    created = 0
    last_top = None
    for username, text, is_reply in entries:
        comment = Comment.objects.create(
            content_type=content_type,
            object_pk=str(question.id),
            site=site,
            user=resolve(username),
            comment=text,
            is_public=True,
            is_removed=False,
            submit_date=timezone.now(),
        )
        created += 1
        if is_reply and last_top is not None:
            CommentReply.objects.create(comment=comment, parent=last_top)
        else:
            last_top = comment
    return created


def _build_distribution(dist_rows, qset):
    """Create DistributionEntry + SetWideDistributionEntry rows from the
    distribution section, attached to the set's distribution. Returns a
    {str(dist_entry): dist_entry} lookup."""
    distribution = qset.distribution
    lookup = {}
    for row in dist_rows:
        category = (row[0] or '').strip() if len(row) > 0 else ''
        subcategory = (row[1] or '').strip() if len(row) > 1 else ''
        if not category:
            continue
        entry = DistributionEntry.objects.create(
            distribution=distribution, category=category, subcategory=subcategory)
        SetWideDistributionEntry.objects.create(
            question_set=qset, dist_entry=entry,
            num_tossups=_to_int(row[2] if len(row) > 2 else 0),
            num_bonuses=_to_int(row[3] if len(row) > 3 else 0))
        lookup['{0} - {1}'.format(category, subcategory)] = entry
    return lookup


def import_set_from_file(uploaded_file, set_name, owner):
    """Parse uploaded_file and create a new QuestionSet owned by `owner`.
    Returns a summary dict. Raises SetImportError on a fatal parse problem."""
    raw = uploaded_file.read()
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8-sig', errors='replace')

    name = (uploaded_file.name or '').lower()
    delimiter = ',' if name.endswith('.csv') else '\t'
    rows = list(csv.reader(io.StringIO(raw), delimiter=delimiter))
    if not rows:
        raise SetImportError('The uploaded file is empty.')

    sections = _split_sections(rows)
    if not sections['tossup'] and not sections['bonus']:
        raise SetImportError(
            'No tossup or bonus rows found. The file must be in the format '
            'produced by "Export" (with "Tossup Question" / "Bonus Leadin" headers).')

    acf_tossup_type = QuestionType.objects.filter(question_type=ACF_STYLE_TOSSUP).first()
    acf_bonus_type = QuestionType.objects.filter(question_type=ACF_STYLE_BONUS).first()
    vhsl_bonus_type = QuestionType.objects.filter(question_type=VHSL_BONUS).first()

    tossup_ct = ContentType.objects.get_for_model(Tossup)
    bonus_ct = ContentType.objects.get_for_model(Bonus)
    site = Site.objects.get_current()
    user_cache, resolve = _user_resolver(owner.user)

    summary = {'tossups': 0, 'bonuses': 0, 'comments': 0, 'errors': []}
    created_tossups = []
    created_bonuses = []

    # Suppress the per-save email notifications (and the thread storm they'd
    # spawn) while bulk-importing. Search indexing stays on.
    email_receivers = [
        (qems_signals.email_on_comments, Comment),
        (qems_signals.email_on_new_tossup, Tossup),
        (qems_signals.email_on_new_bonus, Bonus),
    ]
    for receiver, sender in email_receivers:
        post_save.disconnect(receiver, sender=sender)

    try:
        with transaction.atomic():
            distribution = Distribution.objects.create(name='{0} (imported)'.format(set_name)[:100])
            qset = QuestionSet.objects.create(
                name=set_name, date=timezone.now().date(), host='', address='',
                owner=owner, num_packets=0, distribution=distribution)
            category_lookup = _build_distribution(sections['dist'], qset)

            for row in sections['tossup']:
                try:
                    tossup = Tossup(
                        question_set=qset,
                        tossup_text=row[0] if len(row) > 0 else '',
                        tossup_answer=row[1] if len(row) > 1 else '',
                        category=category_lookup.get((row[2] or '').strip() if len(row) > 2 else ''),
                        author=owner,
                        question_type=acf_tossup_type,
                        question_number=_to_int(row[6] if len(row) > 6 else 0),
                        edited=_to_bool(row[4]) if len(row) > 4 else False,
                        proofread=bool((row[10] or '').strip()) if len(row) > 10 else False,
                        read_carefully=_to_bool(row[11]) if len(row) > 11 else False,
                        locked=False,
                    )
                    tossup.save_question(edit_type=QUESTION_CREATE, changer=owner)
                    created_tossups.append(tossup)
                    summary['tossups'] += 1
                    if len(row) > 7:
                        summary['comments'] += _create_comments(tossup, tossup_ct, site, row[7], resolve)
                except Exception as ex:
                    summary['errors'].append('Tossup row skipped: {0}'.format(ex))

            for row in sections['bonus']:
                try:
                    p2 = row[4] if len(row) > 4 else ''
                    p3 = row[7] if len(row) > 7 else ''
                    is_vhsl = not (p2 or '').strip() and not (p3 or '').strip()
                    bonus = Bonus(
                        question_set=qset,
                        leadin=row[0] if len(row) > 0 else '',
                        part1_text=row[1] if len(row) > 1 else '',
                        part1_answer=row[2] if len(row) > 2 else '',
                        part1_difficulty=(row[3] or '').strip() if len(row) > 3 else '',
                        part2_text=p2,
                        part2_answer=row[5] if len(row) > 5 else '',
                        part2_difficulty=(row[6] or '').strip() if len(row) > 6 else '',
                        part3_text=p3,
                        part3_answer=row[8] if len(row) > 8 else '',
                        part3_difficulty=(row[9] or '').strip() if len(row) > 9 else '',
                        category=category_lookup.get((row[10] or '').strip() if len(row) > 10 else ''),
                        author=owner,
                        question_type=vhsl_bonus_type if is_vhsl else acf_bonus_type,
                        question_number=_to_int(row[14] if len(row) > 14 else 0),
                        edited=_to_bool(row[12]) if len(row) > 12 else False,
                        proofread=bool((row[18] or '').strip()) if len(row) > 18 else False,
                        read_carefully=_to_bool(row[19]) if len(row) > 19 else False,
                        locked=False,
                    )
                    bonus.save_question(edit_type=QUESTION_CREATE, changer=owner)
                    created_bonuses.append(bonus)
                    summary['bonuses'] += 1
                    if len(row) > 15:
                        summary['comments'] += _create_comments(bonus, bonus_ct, site, row[15], resolve)
                except Exception as ex:
                    summary['errors'].append('Bonus row skipped: {0}'.format(ex))

            summary['question_set'] = qset
            summary['users_created'] = sum(1 for u in user_cache.values()
                                           if u.id and not u.is_active and u != owner.user)
    finally:
        for receiver, sender in email_receivers:
            post_save.connect(receiver, sender=sender)

    # Guarantee the new set is searchable even if realtime indexing is off.
    _index_questions(created_tossups, created_bonuses, summary)

    return summary


def _index_questions(tossups, bonuses, summary):
    try:
        from haystack import connections
        unified = connections['default'].get_unified_index()
        for model, objs in ((Tossup, tossups), (Bonus, bonuses)):
            if not objs:
                continue
            index = unified.get_index(model)
            for obj in objs:
                index.update_object(obj, using='default')
    except Exception as ex:
        summary['errors'].append('Created, but search indexing failed (run rebuild_index): {0}'.format(ex))
