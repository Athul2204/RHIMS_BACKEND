from django.apps import AppConfig


class ManagerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'manager'

    def ready(self):
        # Generic audit-log safety net (see administration/audit.py).
        # Nothing in manager/views.py ever called log_audit()/log_admin_action()
        # explicitly, so before this, none of support-staff, attendance,
        # leave, salary, dealers, expenses, or the public-website CMS
        # content was showing up in the audit log at all. Excluded:
        #   - SalaryEntry: a line item with no meaning outside its parent
        #     SalaryRecord — the SalaryRecord's own CREATE/UPDATE entry
        #     already covers it.
        from administration.audit import register_audit_signals
        from .models import SalaryEntry
        register_audit_signals(self, exclude=(
            SalaryEntry,
        ))
