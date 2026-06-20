"""Repair questions whose text was stored HTML-escaped by an earlier YAPP import
(apostrophes as ``&#x27;``, ampersands as ``&amp;``, etc.).

Hand-written QEMS questions store literal characters; the renderer treats the
text as safe HTML. The old importer ran Django's ``escape()`` over the text, so
imported questions show ``&#x27;`` in the editor and exports. This command
decodes those entities in place.

Only entities the importer produced for ordinary punctuation are decoded; ``&lt;``
and ``&gt;`` are left alone so stored text can't suddenly inject an HTML tag.

Usage:
    python manage.py fix_question_entities            # apply
    python manage.py fix_question_entities --dry-run  # report only
    python manage.py fix_question_entities --set 123  # limit to one question set
"""

from django.core.management.base import BaseCommand

from qems2.qsub.models import Tossup, Bonus, TossupHistory, BonusHistory


# Order matters: decode the named/numeric punctuation entities, then &amp; last
# so "&amp;#x27;" style double-escapes collapse correctly.
_REPLACEMENTS = [
    ('&#x27;', "'"), ('&#39;', "'"), ('&apos;', "'"),
    ('&quot;', '"'), ('&#34;', '"'),
    ('&amp;', '&'),
]

_FIELDS = {
    Tossup: ['tossup_text', 'tossup_answer'],
    TossupHistory: ['tossup_text', 'tossup_answer'],
    Bonus: ['leadin', 'part1_text', 'part1_answer', 'part2_text', 'part2_answer',
            'part3_text', 'part3_answer'],
    BonusHistory: ['leadin', 'part1_text', 'part1_answer', 'part2_text', 'part2_answer',
                   'part3_text', 'part3_answer'],
}


def _clean(value):
    if not value:
        return value, False
    out = value
    for entity, char in _REPLACEMENTS:
        out = out.replace(entity, char)
    return out, (out != value)


class Command(BaseCommand):
    help = 'Decode HTML-escaped punctuation left in question text by an older import.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change without saving.')
        parser.add_argument('--set', type=int, default=None,
                            help='Limit to a single question_set id.')

    def handle(self, *args, **options):
        dry = options['dry_run']
        set_id = options['set']

        for model, fields in _FIELDS.items():
            qs = model.objects.all()
            # History rows have no question_set; filter via the live question.
            if set_id is not None and hasattr(model, 'question_set'):
                qs = qs.filter(question_set_id=set_id)

            changed_rows = []
            for obj in qs.iterator():
                touched = False
                for f in fields:
                    new_val, did = _clean(getattr(obj, f))
                    if did:
                        setattr(obj, f, new_val)
                        touched = True
                if touched:
                    changed_rows.append(obj)

            label = model.__name__
            if dry:
                self.stdout.write('{0}: {1} row(s) would change'.format(label, len(changed_rows)))
                continue

            # Update only the text fields, in batches, to skip save() signals
            # (search reindex, notification emails).
            for i in range(0, len(changed_rows), 500):
                model.objects.bulk_update(changed_rows[i:i + 500], fields)
            self.stdout.write('{0}: fixed {1} row(s)'.format(label, len(changed_rows)))

        self.stdout.write('Done.')
