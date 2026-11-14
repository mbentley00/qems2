from django.db import migrations

# The create page didn't show the duplicate-check checkbox, so every set made
# through it was saved with the checks off. Turn them back on everywhere except
# production set 28 (Bentley Notes), which is kept off on purpose.
KEEP_OFF = [28]


def reenable(apps, schema_editor):
    QuestionSet = apps.get_model('qsub', 'QuestionSet')
    QuestionSet.objects.filter(enable_duplicate_checks=False).exclude(
        id__in=KEEP_OFF).update(enable_duplicate_checks=True)


class Migration(migrations.Migration):

    dependencies = [
        ('qsub', '0083_ai_review_run'),
    ]

    operations = [
        migrations.RunPython(reenable, migrations.RunPython.noop),
    ]
