# Generated manually, matching the style of 0004_auditlog_ip_address.py

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('administration', '0004_auditlog_ip_address'),
    ]

    operations = [
        migrations.AddField(
            model_name='auditlog',
            name='user_agent',
            field=models.CharField(blank=True, default='', help_text="Raw User-Agent header of the request this action came from — identifies browser/OS (e.g. 'Chrome 128 on Windows'). Resolved the same way as ip_address, from the in-flight request.", max_length=512),
        ),
        migrations.AddField(
            model_name='auditlog',
            name='device_id',
            field=models.CharField(blank=True, db_index=True, default='', help_text="Client-generated device identifier sent via the X-Device-Id header (see authentication frontend). Lets two staff sharing one IP/network be told apart, and lets a single account's actions be traced to a specific browser/device rather than just a network. Empty when the client didn't send one (older frontend build, or a request with no request in flight).", max_length=64),
        ),
        migrations.CreateModel(
            name='UserDevice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('device_id', models.CharField(max_length=64)),
                ('user_agent', models.CharField(blank=True, default='', max_length=512)),
                ('first_ip', models.GenericIPAddressField(blank=True, null=True)),
                ('last_ip', models.GenericIPAddressField(blank=True, null=True)),
                ('first_seen', models.DateTimeField(default=django.utils.timezone.now)),
                ('last_seen', models.DateTimeField(default=django.utils.timezone.now)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='known_devices', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-last_seen'],
                'unique_together': {('user', 'device_id')},
            },
        ),
    ]