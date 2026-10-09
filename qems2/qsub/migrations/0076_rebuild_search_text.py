"""Rebuild every question's stored search text with utils.search_text.

The fields used to keep QEMS markup like \\PGoethe\\P, which Postgres indexed as
"pgoethe", so a search for a word with a pronunciation guide missed it. The
fields are only refreshed on save, so the existing rows need it done here.
"""

from django.db import migrations

from qems2.qsub.utils import search_text


def rebuild(apps, schema_editor):
    Tossup = apps.get_model('qsub', 'Tossup')
    Bonus = apps.get_model('qsub', 'Bonus')
    fields = ['search_question_content', 'search_question_answers']

    batch = []
    for tu in Tossup.objects.only('id', 'tossup_text', 'tossup_answer').iterator(chunk_size=500):
        tu.search_question_content = search_text(tu.tossup_text)
        tu.search_question_answers = search_text(tu.tossup_answer)
        batch.append(tu)
        if len(batch) >= 500:
            Tossup.objects.bulk_update(batch, fields)
            batch = []
    if batch:
        Tossup.objects.bulk_update(batch, fields)

    batch = []
    for bo in Bonus.objects.only(
            'id', 'leadin', 'part1_text', 'part2_text', 'part3_text',
            'part1_answer', 'part2_answer', 'part3_answer').iterator(chunk_size=500):
        bo.search_question_content = ' '.join(
            search_text(t) for t in (bo.leadin, bo.part1_text, bo.part2_text, bo.part3_text))
        bo.search_question_answers = ' '.join(
            search_text(t) for t in (bo.part1_answer, bo.part2_answer, bo.part3_answer))
        batch.append(bo)
        if len(batch) >= 500:
            Bonus.objects.bulk_update(batch, fields)
            batch = []
    if batch:
        Bonus.objects.bulk_update(batch, fields)


class Migration(migrations.Migration):

    dependencies = [
        ('qsub', '0075_top_level_links'),
    ]

    operations = [
        migrations.RunPython(rebuild, migrations.RunPython.noop),
    ]
