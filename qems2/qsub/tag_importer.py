"""Category tags as a spreadsheet: export a set's tags, import them into
another set (or back into the same one after editing).

Columns: Category | Group | Tag | Tossups | Bonuses | Any Type | Order

Category is the category path exactly as the set's tree names it
("History - World"). Group and the counts may be blank. Order is the tag's
position within its group (blank or 0 keeps alphabetical). Accepts .xlsx,
.csv and .tsv; exports .csv.
"""

import csv
import io

from .distribution_importer import DistributionImportError, _rows_from_upload, _norm_header, _int_or_none
from .models import CategoryTag


class TagImportError(DistributionImportError):
    pass


COLUMNS = [
    ('category', 'Category'),
    ('group', 'Group'),
    ('tag', 'Tag'),
    ('num_tossups', 'Tossups'),
    ('num_bonuses', 'Bonuses'),
    ('num_questions', 'Any Type'),
    ('order', 'Order'),
]

_HEADER_ALIASES = {
    'category_path': 'category', 'path': 'category',
    'group_name': 'group', 'axis': 'group',
    'name': 'tag', 'tag_name': 'tag',
    'max_tossups': 'num_tossups', 'num_tossups': 'num_tossups',
    'max_bonuses': 'num_bonuses', 'num_bonuses': 'num_bonuses',
    'any': 'num_questions', 'any_type': 'num_questions', 'questions': 'num_questions',
    'num_questions': 'num_questions', 'either': 'num_questions',
    'sort_order': 'order', 'position': 'order',
}


def export_rows(qset):
    """Header plus one row per tag, in the set's own order."""
    rows = [[label for _, label in COLUMNS]]
    for t in CategoryTag.objects.filter(question_set=qset):
        rows.append([t.category_path, t.group_name or '', t.name,
                     t.num_tossups or 0, t.num_bonuses or 0, t.num_questions or 0,
                     t.sort_order or 0])
    return rows


def export_csv(qset):
    out = io.StringIO()
    w = csv.writer(out)
    for row in export_rows(qset):
        w.writerow(row)
    return out.getvalue()


def parse_tag_sheet(name, data):
    """-> list of dicts (category_path, group_name, name, num_tossups,
    num_bonuses, num_questions, sort_order). Raises TagImportError."""
    try:
        rows = _rows_from_upload(name, data)
    except DistributionImportError as ex:
        raise TagImportError(str(ex))
    rows = [r for r in rows if any((c or '').strip() for c in r)]
    if not rows:
        raise TagImportError('The sheet is empty.')
    header = []
    for c in rows[0]:
        h = _norm_header(c)
        # _norm_header rewrites "tossups"/"bonuses" alone as max_*; undo that
        # and apply this sheet's own aliases.
        header.append(_HEADER_ALIASES.get(h, h))
    if 'category' not in header or 'tag' not in header:
        raise TagImportError('The first row must be a header with at least "Category" and "Tag" columns '
                             '-- export a set\'s tags to see the layout.')
    col = {key: header.index(key) for key, _ in COLUMNS if key in header}

    def cell(row, key):
        i = col.get(key)
        return (row[i] if i is not None and i < len(row) else '').strip()

    entries, seen = [], set()
    for n, row in enumerate(rows[1:], start=2):
        path = cell(row, 'category')
        tag = cell(row, 'tag')
        if not path or not tag:
            raise TagImportError('Line {0}: the category and tag name are both required'.format(n))
        key = (path.lower(), tag.lower())
        if key in seen:
            raise TagImportError('Line {0}: "{1}" in {2} appears more than once'.format(n, tag, path))
        seen.add(key)
        entries.append({
            'category_path': path,
            'group_name': cell(row, 'group')[:100],
            'name': tag[:200],
            'num_tossups': _int_or_none(cell(row, 'num_tossups'), 'tossups', n) or 0,
            'num_bonuses': _int_or_none(cell(row, 'num_bonuses'), 'bonuses', n) or 0,
            'num_questions': _int_or_none(cell(row, 'num_questions'), 'any type', n) or 0,
            'sort_order': _int_or_none(cell(row, 'order'), 'order', n) or 0,
        })
    if not entries:
        raise TagImportError('The sheet has a header but no tags under it.')
    return entries


def import_tags(qset, entries, known_paths):
    """Create or update the set's tags from parsed entries. A tag is matched
    by (category path, name); an existing one keeps its question
    assignments and takes the sheet's group, counts and order. Entries whose
    category path is not one of ``known_paths`` (the set's own tree) are
    skipped and returned, since a tag on a category the set does not have
    would never be offered to a writer.

    Returns (created, updated, skipped_paths)."""
    known = set(known_paths)
    created = updated = 0
    skipped = []
    for e in entries:
        if e['category_path'] not in known:
            if e['category_path'] not in skipped:
                skipped.append(e['category_path'])
            continue
        tag, was_created = CategoryTag.objects.get_or_create(
            question_set=qset, category_path=e['category_path'], name=e['name'],
            defaults={k: e[k] for k in ('group_name', 'num_tossups', 'num_bonuses',
                                        'num_questions', 'sort_order')})
        if was_created:
            created += 1
        else:
            changed = False
            for k in ('group_name', 'num_tossups', 'num_bonuses', 'num_questions', 'sort_order'):
                if getattr(tag, k) != e[k]:
                    setattr(tag, k, e[k])
                    changed = True
            if changed:
                tag.save()
                updated += 1
    return created, updated, skipped
