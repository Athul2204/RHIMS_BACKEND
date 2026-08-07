# doctor/signals.py
"""
Auto-manages FollowUpReminder records whenever a Consultation's
followup_date is set, changed, or cleared by the doctor.

Rules:
  - followup_date set (was None → has value):  CREATE a new reminder.
  - followup_date changed (old value → new value): UPDATE the existing reminder's date
    and reset status to PENDING so reception sees it afresh.
  - followup_date cleared (had value → None): DELETE the reminder (if any).

The signal fires on post_save so it always works with the saved state.
We compare against the pre-save snapshot stored in _pre_followup_date
by a pre_save receiver.
"""

from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.utils import timezone


@receiver(pre_save, sender='doctor.Consultation')
def _snapshot_followup_date(sender, instance, **kwargs):
    """
    Capture the old followup_date before the save so post_save can compare.
    New objects (pk is None) have no prior value — treat as None.
    """
    if instance.pk:
        try:
            old = sender.objects.values_list('followup_date', flat=True).get(pk=instance.pk)
        except sender.DoesNotExist:
            old = None
    else:
        old = None
    instance._pre_followup_date = old


@receiver(post_save, sender='doctor.Consultation')
def _sync_followup_reminder(sender, instance, created, **kwargs):
    """
    After a Consultation is saved, reconcile its FollowUpReminder:
      - New date set   → create reminder
      - Date changed   → update reminder (reset to PENDING)
      - Date cleared   → delete reminder
      - No change      → do nothing
    """
    from .models import FollowUpReminder  # local import avoids circular issues

    old_date = getattr(instance, '_pre_followup_date', None)
    new_date = instance.followup_date

    # Nothing to do if the date hasn't changed
    if old_date == new_date:
        return

    if new_date is None:
        # Doctor cleared the follow-up date — remove the reminder
        FollowUpReminder.objects.filter(consultation=instance).delete()
        return

    # new_date is set — upsert the reminder
    reminder, reminder_created = FollowUpReminder.objects.get_or_create(
        consultation=instance,
        defaults={
            'patient':      instance.patient,
            'followup_date': new_date,
            'status':        'PENDING',
        },
    )

    if not reminder_created:
        # Date was updated — refresh date and reset status so reception
        # notices the change
        reminder.followup_date = new_date
        reminder.status        = 'PENDING'
        reminder.contacted_at  = None
        reminder.save(update_fields=['followup_date', 'status', 'contacted_at', 'updated_at'])
