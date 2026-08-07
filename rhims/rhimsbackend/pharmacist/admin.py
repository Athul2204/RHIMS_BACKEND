from django.contrib import admin
from .models import (
    Medicine, MedicineBatch, MedicineStockLog, StockAlert,
    PharmacyBill, PharmacyBillMedicineItem, PharmacyBillProcedureItem,
    MedicineReturnToProvider,
    SupplyItem, SupplyBatch, SupplyUsageLog, SupplyStockAlert,
)

admin.site.register(Medicine)
admin.site.register(MedicineBatch)
admin.site.register(MedicineStockLog)
admin.site.register(StockAlert)
admin.site.register(PharmacyBill)
admin.site.register(PharmacyBillMedicineItem)
admin.site.register(PharmacyBillProcedureItem)
admin.site.register(MedicineReturnToProvider)
admin.site.register(SupplyItem)
admin.site.register(SupplyBatch)
admin.site.register(SupplyUsageLog)
admin.site.register(SupplyStockAlert)