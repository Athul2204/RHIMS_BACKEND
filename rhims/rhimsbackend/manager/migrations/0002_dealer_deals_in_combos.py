# Generated manually: widen Dealer.deals_in to allow storing a
# comma-separated combination of categories (e.g. "MEDICINE,SUPPLY"),
# not just a single choice or the legacy "BOTH".

import manager.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('manager', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dealer',
            name='deals_in',
            field=models.CharField(
                default='BOTH',
                max_length=30,
                validators=[manager.models.validate_deals_in],
                help_text="'BOTH' (all three), a single code, or a comma-separated "
                           "combination, e.g. 'MEDICINE,SUPPLY'.",
            ),
        ),
    ]