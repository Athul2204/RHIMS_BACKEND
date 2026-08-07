import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('administration', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='auditlog',
            name='branch',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='audit_logs',
                to='administration.branch',
                help_text=(
                    "Branch this action is scoped to. Resolved at log-creation time "
                    "from the affected object's own branch, falling back to the "
                    "acting user's StaffProfile.branch. Null for group-admin/system "
                    "actions with no single branch (e.g. promoting a group admin)."
                ),
            ),
        ),
    ]
