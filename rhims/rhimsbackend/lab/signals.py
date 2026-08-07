from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import LabRequest, LabRequestStatus


@receiver(post_save, sender=LabRequest)
def sync_consultation_status(sender, instance, created, **kwargs):
    """
    Keep doctor.Consultation.status in sync with LabRequest transitions.

    REQUESTED   → Consultation moves to WAITING_FOR_LAB
    DELIVERED   → Consultation moves to LAB_COMPLETED
    """
    from doctor.models import Consultation, ConsultationStatus

    try:
        consultation = instance.consultation
    except Consultation.DoesNotExist:
        return

    # Don't touch an already-completed consultation
    if consultation.status == ConsultationStatus.COMPLETED:
        return

    if created and instance.status == LabRequestStatus.REQUESTED:
        if consultation.status not in (
            ConsultationStatus.WAITING_FOR_LAB,
            ConsultationStatus.LAB_COMPLETED,
        ):
            consultation.status = ConsultationStatus.WAITING_FOR_LAB
            consultation.save(update_fields=['status', 'updated_at'])

    elif (
        not created
        and instance.status == LabRequestStatus.DELIVERED
    ):
        # Only advance if all lab requests for this consultation are done
        all_requests = consultation.lab_requests.all()
        all_delivered = all(
            r.status == LabRequestStatus.DELIVERED
            for r in all_requests
        )
        if all_delivered:
            consultation.status = ConsultationStatus.LAB_COMPLETED
            consultation.save(update_fields=['status', 'updated_at'])