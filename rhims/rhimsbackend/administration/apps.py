from django.apps import AppConfig


class AdministrationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'administration'

    def ready(self):
        import administration.signals  # noqa

        # Generic audit-log safety net (see administration/audit.py). Every
        # model in this app is excluded below because it already gets a
        # richer, explicitly-labelled log_admin_action() call from its own
        # view in administration/views.py (Branch, StaffProfile,
        # ReceptionistProfile, PharmacistProfile, GuestDoctorProfile,
        # CommonReceptionistProfile, CommonPharmacistProfile, Procedure,
        # HospitalSettings, ManagerBranchAccess) — wiring it up here anyway
        # means any *future* model added to this app without remembering to
        # add its own log_admin_action() call is still covered automatically
        # instead of silently falling through the cracks.
        from .audit import register_audit_signals
        from .models import (
            Branch, StaffProfile, ReceptionistProfile, PharmacistProfile,
            GuestDoctorProfile, CommonReceptionistProfile, CommonPharmacistProfile,
            Procedure, HospitalSettings, ManagerBranchAccess, AuditLog,
        )
        register_audit_signals(self, exclude=(
            Branch, StaffProfile, ReceptionistProfile, PharmacistProfile,
            GuestDoctorProfile, CommonReceptionistProfile, CommonPharmacistProfile,
            Procedure, HospitalSettings, ManagerBranchAccess, AuditLog,
        ))
