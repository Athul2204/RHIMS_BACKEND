from django.apps import AppConfig


class LabConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'lab'

    def ready(self):
        # Generic audit-log safety net (see administration/audit.py).
        # Nothing in lab/views.py ever called log_audit()/log_admin_action()
        # explicitly, so before this, lab requests/results/reports/bills
        # simply weren't showing up in the audit log at all. Excluded:
        #   - LabRequestItem: a line item with no meaning outside its
        #     parent LabRequest — the LabRequest's own CREATE/UPDATE entry
        #     already covers it.
        from administration.audit import register_audit_signals
        from .models import LabRequestItem
        register_audit_signals(self, exclude=(
            LabRequestItem,
        ))
