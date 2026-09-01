"""Category tags as a spreadsheet: export a set's tags, import them into
another set (or back into the same one after editing).

Columns: Category | Group | Tag | Tossups | Bonuses | Any Type |
Max Tossups | Max Bonuses | Max Any Type | Order | In Exports

Category is the category path exactly as the set's tree names it
("History - World"). Group and the counts may be blank. The typed columns are
minimums (0 or blank asks for none); the Max columns are ceilings, and a blank
one is no ceiling at all -- unlike 0, which allows none. Order is the tag's
position within its group (blank or 0 keeps alphabetical). "In Exports" is
yes/no for whether the tag's name is printed in exported packets. Accepts
.xlsx, .csv and .tsv; exports .csv.
"""

import csv
import io
import re

from .distribution_importer import DistributionImportError, _rows_from_upload, _int_or_none
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
    ('max_tossups', 'Max Tossups'),
    ('max_bonuses', 'Max Bonuses'),
    ('max_questions', 'Max Any Type'),
    ('order', 'Order'),
    ('in_output', 'In Exports'),
]

# Header text (lower case, runs of non-letters squeezed to one space) to the
# column it names. A tag sheet's typed columns are minimums, so a bare
# "Tossups" is the floor and only an explicit "Max Tossups" is the ceiling --
# which is why these are matched here rather than through the distribution
# sheet's _norm_header, where a bare "Tossups" means the ceiling.
_HEADER_ALIASES = {
    'category': 'category', 'category path': 'category', 'path': 'category',
    'group': 'group', 'group name': 'group', 'axis': 'group',
    'tag': 'tag', 'name': 'tag', 'tag name': 'tag',
    'tossups': 'num_tossups', 'tossup': 'num_tossups', 'tu': 'num_tossups',
    'tus': 'num_tossups', 'num tossups': 'num_tossups', 'min tossups': 'num_tossups',
    'minimum tossups': 'num_tossups',
    'bonuses': 'num_bonuses', 'bonus': 'num_bonuses', 'b': 'num_bonuses',
    'num bonuses': 'num_bonuses', 'min bonuses': 'num_bonuses',
    'minimum bonuses': 'num_bonuses',
    'any': 'num_questions', 'any type': 'num_questions', 'questions': 'num_questions',
    'num questions': 'num_questions', 'either': 'num_questions',
    'min any type': 'num_questions', 'min questions': 'num_questions',
    'max tossups': 'max_tossups', 'maximum tossups': 'max_tossups', 'max tu': 'max_tossups',
    'max bonuses': 'max_bonuses', 'maximum bonuses': 'max_bonuses', 'max b': 'max_bonuses',
    'max any type': 'max_questions', 'max any': 'max_questions',
    'max questions': 'max_questions', 'max either': 'max_questions',
    'maximum any type': 'max_questions',
    'order': 'order', 'sort order': 'order', 'position': 'order',
    'in exports': 'in_output', 'in export': 'in_output', 'exports': 'in_output',
    'export': 'in_output', 'in output': 'in_output', 'show in output': 'in_output',
}


def _tag_header(text):
    """Which column a header cell names, or '' for one this sheet has no use
    for."""
    t = re.sub(r'[^a-z]+', ' ', (text or '').strip().lower()).strip()
    return _HEADER_ALIASES.get(t, '')


# What a sheet may write in the "In Exports" column for "keep this tag out of
# exported packets". Anything else -- including an empty cell, which is what a
# sheet made before the column existed has -- leaves the tag printed.
_NO_WORDS = ('no', 'n', 'false', 'f', '0', 'off', 'hidden', 'hide')


def export_rows(qset):
    """Header plus one row per tag, in the set's own order."""
    rows = [[label for _, label in COLUMNS]]
    for t in CategoryTag.objects.filter(question_set=qset):
        rows.append([t.category_path, t.group_name or '', t.name,
                     t.num_tossups or 0, t.num_bonuses or 0, t.num_questions or 0,
                     '' if t.max_tossups is None else t.max_tossups,
                     '' if t.max_bonuses is None else t.max_bonuses,
                     '' if t.max_questions is None else t.max_questions,
                     t.sort_order or 0, 'yes' if t.show_in_output else 'no'])
    return rows


def export_csv(qset):
    out = io.StringIO()
    w = csv.writer(out)
    for row in export_rows(qset):
        w.writerow(row)
    return out.getvalue()


def parse_tag_sheet(name, data):
    """-> list of dicts (category_path, group_name, name, num_tossups,
    num_bonuses, num_questions, max_tossups, max_bonuses, max_questions,
    sort_order, show_in_output). The max_* are None when the sheet leaves the
    column empty -- no ceiling. Raises TagImportError."""
    try:
        rows = _rows_from_upload(name, data)
    except DistributionImportError as ex:
        raise TagImportError(str(ex))
    rows = [r for r in rows if any((c or '').strip() for c in r)]
    if not rows:
        raise TagImportError('The sheet is empty.')
    header = [_tag_header(c) for c in rows[0]]
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
        if not tag:
            raise TagImportError('Line {0}: a tag name is required'.format(n))
        key = (path.lower(), tag.lower())
        if key in seen:
            raise TagImportError('Line {0}: "{1}" in {2} appears more than once'.format(
                n, tag, path or 'the whole set'))
        seen.add(key)
        entries.append({
            'category_path': path,
            'group_name': cell(row, 'group')[:100],
            'name': tag[:200],
            'num_tossups': _int_or_none(cell(row, 'num_tossups'), 'tossups', n) or 0,
            'num_bonuses': _int_or_none(cell(row, 'num_bonuses'), 'bonuses', n) or 0,
            'num_questions': _int_or_none(cell(row, 'num_questions'), 'any type', n) or 0,
            'max_tossups': _int_or_none(cell(row, 'max_tossups'), 'max tossups', n),
            'max_bonuses': _int_or_none(cell(row, 'max_bonuses'), 'max bonuses', n),
            'max_questions': _int_or_none(cell(row, 'max_questions'), 'max any type', n),
            'sort_order': _int_or_none(cell(row, 'order'), 'order', n) or 0,
            'show_in_output': cell(row, 'in_output').lower() not in _NO_WORDS,
        })
    for n, e in enumerate(entries, start=2):
        for lo_key, hi_key, label in (('num_tossups', 'max_tossups', 'tossups'),
                                      ('num_bonuses', 'max_bonuses', 'bonuses'),
                                      ('num_questions', 'max_questions', 'any type')):
            hi = e[hi_key]
            if hi is not None and e[lo_key] and hi < e[lo_key]:
                raise TagImportError(
                    'Line {0}: the {1} maximum ({2}) is below its minimum ({3})'.format(
                        n, label, hi, e[lo_key]))
    if not entries:
        raise TagImportError('The sheet has a header but no tags under it.')
    return entries


def import_tags(qset, entries, known_paths):
    """Create or update the set's tags from parsed entries. A tag is matched
    by (category path, name); an existing one keeps its question
    assignments and takes the sheet's group, counts and order. Entries whose
    category path is not one of ``known_paths`` (the set's own tree) are
    skipped and returned, since a tag on a category the set does not have
    would never be offered to a writer. A blank category path is a set-wide
    tag and is always accepted.

    Returns (created, updated, skipped_paths)."""
    known = set(known_paths)
    created = updated = 0
    skipped = []
    for e in entries:
        # A blank category column means the tag applies to the whole set, so
        # it is not measured against the set's category list.
        if e['category_path'] and e['category_path'] not in known:
            if e['category_path'] not in skipped:
                skipped.append(e['category_path'])
            continue
        tag, was_created = CategoryTag.objects.get_or_create(
            question_set=qset, category_path=e['category_path'], name=e['name'],
            defaults={k: e[k] for k in ('group_name', 'num_tossups', 'num_bonuses',
                                        'num_questions', 'max_tossups', 'max_bonuses',
                                        'max_questions', 'sort_order',
                                        'show_in_output')})
        if was_created:
            created += 1
        else:
            changed = False
            for k in ('group_name', 'num_tossups', 'num_bonuses', 'num_questions',
                      'max_tossups', 'max_bonuses', 'max_questions',
                      'sort_order', 'show_in_output'):
                if getattr(tag, k) != e[k]:
                    setattr(tag, k, e[k])
                    changed = True
            if changed:
                tag.save()
                updated += 1
    return created, updated, skipped
