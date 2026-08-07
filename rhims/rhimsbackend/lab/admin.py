from django.contrib import admin
from .models import TestGroup, LabTest, LabRequest, LabRequestItem, LabResult, LabReport


# ─── Test Group (billing panel, e.g. LFT/KFT/LIPID) ──────────────
class LabTestGroupInline(admin.TabularInline):
    """Manage which tests belong to this panel directly from the TestGroup page."""
    model           = LabTest
    fk_name         = 'group'
    extra           = 0
    fields          = ('code', 'name', 'price', 'is_active')
    readonly_fields = ('code', 'name', 'price', 'is_active')
    can_delete      = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        # Sub-tests are assigned via the LabTest admin's `group` field, not added here.
        return False


@admin.register(TestGroup)
class TestGroupAdmin(admin.ModelAdmin):
    list_display    = ('code', 'name', 'price', 'is_active', 'created_at')
    list_filter     = ('is_active',)
    search_fields   = ('code', 'name')
    ordering        = ('name',)
    readonly_fields = ('created_at', 'updated_at')
    inlines         = [LabTestGroupInline]


# ─── Lab Test (Master Catalog) ───────────────────────────────────
@admin.register(LabTest)
class LabTestAdmin(admin.ModelAdmin):
    list_display    = ('code', 'name', 'unit', 'price', 'group', 'is_active', 'created_at')
    list_filter     = ('is_active', 'group')
    search_fields   = ('code', 'name')
    ordering        = ('name',)
    readonly_fields = ('created_at', 'updated_at')
    raw_id_fields   = ('group',)


# ─── Lab Request Item Inline ─────────────────────────────────────
class LabRequestItemInline(admin.TabularInline):
    model           = LabRequestItem
    extra           = 0
    raw_id_fields   = ('test', 'ordered_as_group')
    readonly_fields = ('item_id',)


# ─── Lab Report Inline ───────────────────────────────────────────
class LabReportInline(admin.StackedInline):
    model           = LabReport
    extra           = 0
    readonly_fields = ('report_id', 'created_at', 'updated_at')
    can_delete      = False


# ─── Lab Request ─────────────────────────────────────────────────
@admin.register(LabRequest)
class LabRequestAdmin(admin.ModelAdmin):
    list_display    = ('request_id', 'patient', 'consultation', 'status', 'request_date', 'requested_by')
    list_filter     = ('status', 'request_date')
    search_fields   = ('patient__mrd_number', 'patient__first_name', 'patient__last_name')
    ordering        = ('-request_id',)
    raw_id_fields   = ('patient', 'consultation', 'requested_by')
    readonly_fields = ('request_id', 'created_at', 'updated_at')
    inlines         = [LabRequestItemInline, LabReportInline]


# ─── Lab Request Item ────────────────────────────────────────────
@admin.register(LabRequestItem)
class LabRequestItemAdmin(admin.ModelAdmin):
    list_display    = ('item_id', 'lab_request', 'test', 'ordered_as_group')
    list_filter     = ('ordered_as_group',)
    raw_id_fields   = ('lab_request', 'test', 'ordered_as_group')
    readonly_fields = ('item_id',)


# ─── Lab Result ──────────────────────────────────────────────────
@admin.register(LabResult)
class LabResultAdmin(admin.ModelAdmin):
    list_display    = ('result_id', 'lab_request_item', 'result_value', 'is_abnormal', 'performed_by', 'performed_at')
    list_filter     = ('is_abnormal',)
    raw_id_fields   = ('lab_request_item', 'performed_by')
    readonly_fields = ('result_id', 'created_at', 'updated_at')


# ─── Lab Report ──────────────────────────────────────────────────
@admin.register(LabReport)
class LabReportAdmin(admin.ModelAdmin):
    list_display    = ('report_id', 'lab_request', 'verified_by', 'verified_at', 'delivered_at')
    raw_id_fields   = ('lab_request', 'verified_by')
    readonly_fields = ('report_id', 'created_at', 'updated_at')