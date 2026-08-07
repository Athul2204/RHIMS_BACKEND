from django.apps import AppConfig


class DoctorConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'doctor'

    def ready(self):
        import doctor.signals  # noqa: F401 — registers signal handlers

        # Generic audit-log safety net (see administration/audit.py).
        # Nothing in doctor/views.py ever called log_audit()/log_admin_action()
        # explicitly, so before this, consultations/prescriptions/follow-ups
        # simply weren't showing up in the audit log at all. Excluded:
        #   - DoctorProfile: already explicitly logged by
        #     administration/views.py's DoctorListView/DoctorDetailView.
        #   - ConsultationTimeline: a history/log table in its own right —
        #     an audit entry for it would just be logging a log.
        #   - PrescriptionItem: a line item with no meaning outside its
        #     parent Prescription — the Prescription's own CREATE/UPDATE
        #     entry already covers it.
        from administration.audit import register_audit_signals
        from .models import DoctorProfile, ConsultationTimeline, PrescriptionItem
        register_audit_signals(self, exclude=(
            DoctorProfile, ConsultationTimeline, PrescriptionItem,
        ))
