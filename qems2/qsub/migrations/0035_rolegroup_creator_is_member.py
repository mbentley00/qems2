from django.db import migrations


def add_creators_as_members(apps, schema_editor):
    """Backfill: a role group's creator belongs to it (so groups aren't
    "0 members" and the creator gets any role the group grants)."""
    RoleGroup = apps.get_model('qsub', 'RoleGroup')
    for group in RoleGroup.objects.exclude(created_by__isnull=True):
        if not group.members.filter(id=group.created_by_id).exists():
            group.members.add(group.created_by_id)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('qsub', '0034_editortag'),
    ]

    operations = [
        migrations.RunPython(add_creators_as_members, noop),
    ]
