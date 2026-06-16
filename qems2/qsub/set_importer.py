"""Create a new QuestionSet from an uploaded TSV/CSV in the same format that
export_question_set produces: a tossup section, a bonus section, and a
distribution section, each introduced by its header row and separated by a
blank row. Comments (including threaded replies) are recreated, and the new
set's questions are pushed into the search index.

Admin-only; wired up in views.import_set.
"""

import csv
import io
import re

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.models import Site
from django.db import transaction
from django.utils import timezone
from django_comments.models import Comment

from qems2.qsub.models import (QuestionSet, Distribution, DistributionEntry,
                               SetWideDistributionEntry, Tossup, Bonus,
                               QuestionType, CommentReply, Writer)
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


def _difficulty(val):
    """Bonus part difficulty is a CharField(max_length=1) limited to e/m/h.
    Postgres rejects anything longer (SQLite silently truncates), so pull the
    difficulty letter out of whatever the column holds (e.g. 'e', 'E', '10e')
    and otherwise return ''."""
    text = (val or '').strip().lower()
    if text in ('e', 'm', 'h'):
        return text
    for ch in text:
        if ch in 'emh':
            return ch
    return ''


def _clamp(val, max_length):
    """Truncate a value to fit a CharField, since Postgres errors on overflow."""
    text = val if val is not None else ''
    return text[:max_length]


def _row_is_blank(row):
    return not row or all((cell or '').strip() == '' for cell in row)


def _split_sections(rows):
    """Bucket data rows into tossup/bonus/distribution sections by their header
    rows, and capture each section's header so columns can be read by name
    (export formats differ: older files lack the per-part difficulty columns)."""
    sections = {'tossup': [], 'bonus': [], 'dist': []}
    headers = {'tossup': [], 'bonus': [], 'dist': []}
    current = None
    for row in rows:
        if _row_is_blank(row):
            current = None
            continue
        head = (row[0] or '').strip()
        if head == TOSSUP_HEADER:
            current = 'tossup'
            headers['tossup'] = row
            continue
        if head == BONUS_HEADER:
            current = 'bonus'
            headers['bonus'] = row
            continue
        if head == 'Category' and len(row) >= 2 and (row[1] or '').strip() == 'Subcategory':
            current = 'dist'
            headers['dist'] = row
            continue
        if current is not None:
            sections[current].append(row)
    return sections, headers


def _col_map(header_row):
    """Map normalized column name -> index for a section's header row."""
    return {(h or '').strip().lower(): i for i, h in enumerate(header_row)}


def _cell(row, colmap, name, default=''):
    """Read a field from a row by column name, tolerant of missing columns."""
    idx = colmap.get(name.strip().lower())
    if idx is None or idx >= len(row):
        return default
    val = row[idx]
    return val if val is not None else default


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


def _norm_name(s):
    return ' '.join((s or '').split()).lower()


def _make_legacy_writer(author_name):
    """Create (or reuse) an inactive placeholder Writer for an imported author
    with no current account, marked legacy via a '-legacy' username so it's
    clearly distinguishable from a real, current account of the same person."""
    parts = author_name.split()
    first = parts[0] if parts else author_name
    last = ' '.join(parts[1:]) if len(parts) > 1 else ''
    base = re.sub(r'[^A-Za-z0-9._-]+', '-', author_name.strip().lower()).strip('-') or 'author'
    username = '{0}-legacy'.format(base)[:150]
    user = User.objects.filter(username=username).first()
    if user is None:
        user = User(username=username, first_name=first[:150], last_name=last[:150], is_active=False)
        user.set_unusable_password()
        user.save()
    writer, _ = Writer.objects.get_or_create(user=user)
    return writer


def _author_resolver(owner):
    """Returns (legacy_ids, resolve). resolve(author_name) -> the Writer to
    attribute an imported question to: an existing account whose real name or
    username matches, otherwise a legacy placeholder Writer (whose id is added
    to legacy_ids). Empty author falls back to the importing owner."""
    by_name = {}
    for w in Writer.objects.select_related('user').all():
        rn = _norm_name(w.get_real_name())
        if rn:
            by_name.setdefault(rn, w)
        un = _norm_name(w.user.username)
        if un:
            by_name.setdefault(un, w)

    cache = {}
    legacy_ids = set()

    def resolve(author_name):
        key = (author_name or '').strip()
        if not key:
            return owner
        if key in cache:
            return cache[key]
        norm = _norm_name(key)
        writer = by_name.get(norm)
        if writer is None:
            writer = _make_legacy_writer(key)
            legacy_ids.add(writer.id)
            by_name[norm] = writer  # reuse for other questions by the same author
        cache[key] = writer
        return writer

    return legacy_ids, resolve


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

    sections, headers = _split_sections(rows)
    if not sections['tossup'] and not sections['bonus']:
        raise SetImportError(
            'No tossup or bonus rows found. The file must be in the format '
            'produced by "Export" (with "Tossup Question" / "Bonus Leadin" headers).')

    # Read columns by header name so both export layouts work (older files have
    # no per-part difficulty columns, which shifted every later column).
    tmap = _col_map(headers['tossup'])
    bmap = _col_map(headers['bonus'])

    acf_tossup_type = QuestionType.objects.filter(question_type=ACF_STYLE_TOSSUP).first()
    acf_bonus_type = QuestionType.objects.filter(question_type=ACF_STYLE_BONUS).first()
    vhsl_bonus_type = QuestionType.objects.filter(question_type=VHSL_BONUS).first()

    tossup_ct = ContentType.objects.get_for_model(Tossup)
    bonus_ct = ContentType.objects.get_for_model(Bonus)
    site = Site.objects.get_current()
    user_cache, resolve = _user_resolver(owner.user)
    legacy_author_ids, resolve_author = _author_resolver(owner)

    summary = {'tossups': 0, 'bonuses': 0, 'comments': 0, 'errors': []}
    created_tossups = []
    created_bonuses = []

    # Suppress the per-save email notifications (and the thread storm they'd
    # spawn) while bulk-importing.
    email_receivers = [
        (qems_signals.email_on_comments, Comment),
        (qems_signals.email_on_new_tossup, Tossup),
        (qems_signals.email_on_new_bonus, Bonus),
    ]
    for receiver, sender in email_receivers:
        post_save.disconnect(receiver, sender=sender)

    # Search uses Postgres full-text over the maintained search_question_*
    # fields (set in save_question), so there's nothing to index here.
    try:
        with transaction.atomic():
            distribution = Distribution.objects.create(name='{0} (imported)'.format(set_name)[:100])
            qset = QuestionSet.objects.create(
                name=set_name, date=timezone.now().date(), host='', address='',
                owner=owner, num_packets=0, distribution=distribution)
            # Add the owner as an editor too, matching create_question_set, so
            # the set is editable/deletable from the normal UI (not just admin).
            owner.question_set_editor.add(qset)
            category_lookup = _build_distribution(sections['dist'], qset)

            # Pre-create placeholder commenter accounts in the outer transaction
            # (not inside a per-row savepoint), so a row rollback can't leave the
            # user cache pointing at a User that no longer exists.
            for sec, cmap in (('tossup', tmap), ('bonus', bmap)):
                for crow in sections[sec]:
                    for uname, _text, _reply in _parse_comment_cell(_cell(crow, cmap, 'Comments')):
                        resolve(uname)
                    # Pre-create author writers (incl. legacy placeholders) in
                    # the outer transaction so a per-row rollback can't orphan them.
                    resolve_author(_cell(crow, cmap, 'Author'))

            for row in sections['tossup']:
                # Per-row savepoint: a failed row rolls back only itself, so it
                # can't poison the outer transaction (critical on Postgres).
                try:
                    with transaction.atomic():
                        tossup = Tossup(
                            question_set=qset,
                            tossup_text=_cell(row, tmap, 'Tossup Question'),
                            tossup_answer=_cell(row, tmap, 'Answer'),
                            category=category_lookup.get(_cell(row, tmap, 'Category').strip()),
                            author=resolve_author(_cell(row, tmap, 'Author')),
                            question_type=acf_tossup_type,
                            question_number=_to_int(_cell(row, tmap, 'Question Number')),
                            edited=_to_bool(_cell(row, tmap, 'Edited')),
                            proofread=bool(_cell(row, tmap, 'Proofreader').strip()),
                            read_carefully=_to_bool(_cell(row, tmap, 'Read Carefully')),
                            locked=False,
                        )
                        tossup.save_question(edit_type=QUESTION_CREATE, changer=owner)
                        summary['comments'] += _create_comments(tossup, tossup_ct, site, _cell(row, tmap, 'Comments'), resolve)
                    created_tossups.append(tossup)
                    summary['tossups'] += 1
                except Exception as ex:
                    summary['errors'].append('Tossup row skipped: {0}'.format(ex))

            for row in sections['bonus']:
                # Per-row savepoint (see tossup loop).
                try:
                    with transaction.atomic():
                        p2 = _cell(row, bmap, 'Bonus Part 2')
                        p3 = _cell(row, bmap, 'Bonus Part 3')
                        is_vhsl = not p2.strip() and not p3.strip()
                        bonus = Bonus(
                            question_set=qset,
                            leadin=_clamp(_cell(row, bmap, 'Bonus Leadin'), 500),
                            part1_text=_cell(row, bmap, 'Bonus Part 1'),
                            part1_answer=_cell(row, bmap, 'Bonus Answer 1'),
                            part1_difficulty=_difficulty(_cell(row, bmap, 'Part 1 Difficulty')),
                            part2_text=p2,
                            part2_answer=_cell(row, bmap, 'Bonus Answer 2'),
                            part2_difficulty=_difficulty(_cell(row, bmap, 'Part 2 Difficulty')),
                            part3_text=p3,
                            part3_answer=_cell(row, bmap, 'Bonus Answer 3'),
                            part3_difficulty=_difficulty(_cell(row, bmap, 'Part 3 Difficulty')),
                            category=category_lookup.get(_cell(row, bmap, 'Category').strip()),
                            author=resolve_author(_cell(row, bmap, 'Author')),
                            question_type=vhsl_bonus_type if is_vhsl else acf_bonus_type,
                            question_number=_to_int(_cell(row, bmap, 'Question Number')),
                            edited=_to_bool(_cell(row, bmap, 'Edited')),
                            proofread=bool(_cell(row, bmap, 'Proofreader').strip()),
                            read_carefully=_to_bool(_cell(row, bmap, 'Read Carefully')),
                            locked=False,
                        )
                        bonus.save_question(edit_type=QUESTION_CREATE, changer=owner)
                        summary['comments'] += _create_comments(bonus, bonus_ct, site, _cell(row, bmap, 'Comments'), resolve)
                    created_bonuses.append(bonus)
                    summary['bonuses'] += 1
                except Exception as ex:
                    summary['errors'].append('Bonus row skipped: {0}'.format(ex))

            # Estimate a packet count from the imported volume (default 20
            # questions/packet). Leaving num_packets at 0 would make the
            # packetize page's "recommended per packet" (set total / packets)
            # divide by 1 and show the whole set total per packet.
            est_packets = max(1, round(max(summary['tossups'], summary['bonuses']) / 20.0))
            qset.num_packets = est_packets
            qset.save(update_fields=['num_packets'])

            summary['question_set'] = qset
            summary['users_created'] = sum(1 for u in user_cache.values()
                                           if u.id and not u.is_active and u != owner.user)
            summary['legacy_authors'] = len(legacy_author_ids)
    finally:
        for receiver, sender in email_receivers:
            post_save.connect(receiver, sender=sender)

    return summary


def delete_question_set(qset):
    """Delete a set and all its content. Search is Postgres full-text over the
    questions' own fields, so deleting the rows is all that's needed (no index
    to purge). Also clears orphaned comments and the set's distribution."""
    tossup_ct = ContentType.objects.get_for_model(Tossup)
    bonus_ct = ContentType.objects.get_for_model(Bonus)
    tossup_ids = [str(i) for i in Tossup.objects.filter(question_set=qset).values_list('id', flat=True)]
    bonus_ids = [str(i) for i in Bonus.objects.filter(question_set=qset).values_list('id', flat=True)]

    # Comments are keyed by content type + object id, not a FK, so the cascade
    # won't remove them -- do it explicitly.
    Comment.objects.filter(content_type=tossup_ct, object_pk__in=tossup_ids).delete()
    Comment.objects.filter(content_type=bonus_ct, object_pk__in=bonus_ids).delete()

    distribution = qset.distribution
    with transaction.atomic():
        qset.delete()
        # Drop the set's distribution unless another set shares it.
        if distribution is not None and not QuestionSet.objects.filter(distribution=distribution).exists():
            distribution.delete()
