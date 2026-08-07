# services/consultation_engine.py

from doctor.models import Consultation, ConsultationStatus


class ConsultationEngine:

    @staticmethod
    def on_create(consultation):
        consultation.status = ConsultationStatus.STARTED
        consultation.save(update_fields=["status"])

    @staticmethod
    def request_lab(consultation):
        consultation.status = ConsultationStatus.LAB_REQUESTED
        consultation.save(update_fields=["status"])

    @staticmethod
    def waiting_for_lab(consultation):
        consultation.status = ConsultationStatus.WAITING_FOR_LAB
        consultation.save(update_fields=["status"])

    @staticmethod
    def lab_completed(consultation):
        consultation.status = ConsultationStatus.LAB_COMPLETED
        consultation.save(update_fields=["status"])

    @staticmethod
    def auto_check_after_prescription(consultation):
        """
        Optional rule:
        If prescription is FINAL → auto complete
        """
        has_final = consultation.prescriptions.filter(
            prescription_type="FINAL"
        ).exists()

        if has_final:
            consultation.status = ConsultationStatus.COMPLETED
            consultation.save(update_fields=["status"])