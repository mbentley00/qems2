"""Re-key category notes saved under a broken path.

A category with no subcategory prints as "Literature - "; the add-note form
trimmed that to "Literature -", which no page looked up, so the note seemed to
vanish. Pages now use the clean path ("Literature"), so move those notes there.
"""
from django.db import migrations


def clean(path):
    return ' - '.join(p.strip() for p in (path or '').split(' - ') if p.strip()).rstrip(' -').strip()


def forwards(apps, schema_editor):
    CategoryComment = apps.get_model('qsub', 'CategoryComment')
    for note in CategoryComment.objects.all().only('id', 'category_path'):
        fixed = clean(note.category_path)
        if fixed and fixed != note.category_path:
            CategoryComment.objects.filter(id=note.id).update(category_path=fixed)


class Migration(migrations.Migration):

    dependencies = [
        ('qsub', '0080_ai_auto_review'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
