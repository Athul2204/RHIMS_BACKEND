# Generated manually to add BranchWebsiteProfile.branch_type

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('manager', '0004_patientquery_query_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='branchwebsiteprofile',
            name='branch_type',
            field=models.CharField(
                choices=[('HOSPITAL', 'Hospital'), ('MEDICAL_CENTRE', 'Medical Centre')],
                default='HOSPITAL',
                help_text="Which section of the public Locations page this branch is grouped "
                          "under -- full-service Hospitals get the larger banner-card listing, "
                          "smaller Medical Centres get the compact grid below it.",
                max_length=20,
            ),
        ),
    ]
