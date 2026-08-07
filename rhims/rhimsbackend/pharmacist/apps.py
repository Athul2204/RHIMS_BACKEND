from django.apps import AppConfig


class PharmacistConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'pharmacist'

    def ready(self):
        import pharmacist.signals  # noqa: F401

        # Generic audit-log safety net (see administration/audit.py).
        # Nothing in pharmacist/views.py ever called log_audit()/
        # log_admin_action() explicitly, so before this, medicines, batches,
        # bills, supplies, and general items simply weren't showing up in
        # the audit log at all. Excluded:
        #   - MedicineStockLog, StockAlert, SupplyUsageLog, SupplyStockAlert,
        #     GeneralItemStockLog, GeneralItemStockAlert: log/alert tables
        #     in their own right — an audit entry for these would just be
        #     logging a log.
        #   - PharmacyBillBatchAssignment, PharmacyBillMedicineItem,
        #     PharmacyBillProcedureItem, PharmacyBillGeneralItem: line
        #     items with no meaning outside their parent PharmacyBill — the
        #     bill's own CREATE/UPDATE entry already covers them.
        from administration.audit import register_audit_signals
        from .models import (
            MedicineStockLog, StockAlert, SupplyUsageLog, SupplyStockAlert,
            GeneralItemStockLog, GeneralItemStockAlert,
            PharmacyBillBatchAssignment, PharmacyBillMedicineItem,
            PharmacyBillProcedureItem, PharmacyBillGeneralItem,
        )
        register_audit_signals(self, exclude=(
            MedicineStockLog, StockAlert, SupplyUsageLog, SupplyStockAlert,
            GeneralItemStockLog, GeneralItemStockAlert,
            PharmacyBillBatchAssignment, PharmacyBillMedicineItem,
            PharmacyBillProcedureItem, PharmacyBillGeneralItem,
        ))
