from django.apps import AppConfig


class ReceptionConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'reception'

    def ready(self):
        # Generic audit-log safety net (see administration/audit.py).
        # Nothing in reception/views.py ever called log_audit()/
        # log_admin_action() explicitly, so before this, patient
        # registration, consultation bills, and pre-bookings simply
        # weren't showing up in the audit log at all. No excludes needed —
        # every model in this app (Patient, ConsultationBill,
        # ConsultationPreBooking) is a meaningful top-level record.
        from administration.audit import register_audit_signals
        register_audit_signals(self)
