# rhimsbackend/doctor/admin.py — FIXED VERSION
# Removed FollowUpReminder which was not defined in models

from django.contrib import admin

from .models import (
    DoctorProfile,
    Consultation,
    ConsultationTimeline,
    Prescription,
    PrescriptionItem,
)


# ============================================================
# PRESCRIPTION ITEM INLINE
# ============================================================

class PrescriptionItemInline(admin.TabularInline):
    model = PrescriptionItem
    extra = 1

    fields = (
        'medicine',
        'medicine_name',
        'dose_quantity',
        'frequency',
        'meal_timing',
        'route',
        'is_route_overridden',
        'duration_days',
        'quantity',
        'is_manual_quantity',
        'instructions',
    )

    readonly_fields = (
        'medicine_name',
    )


# ============================================================
# CONSULTATION TIMELINE INLINE
# ============================================================

class ConsultationTimelineInline(admin.TabularInline):
    model = ConsultationTimeline
    extra = 0

    fields = (
        'actor',
        'event',
        'description',
        'timestamp',
    )

    readonly_fields = (
        'actor',
        'event',
        'description',
        'timestamp',
    )

    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


# ============================================================
# DOCTOR PROFILE
# ============================================================

@admin.register(DoctorProfile)
class DoctorProfileAdmin(admin.ModelAdmin):
    list_display = (
        'profile_id',
        'staff',
        'get_branch',
        'specialization',
        'department',
        'consultation_fee',
        'is_available',
    )

    list_filter = (
        'is_available',
        'department',
        'staff__branch',
    )

    search_fields = (
        'staff__user__first_name',
        'staff__user__last_name',
        'specialization',
        'registration_number',
    )

    readonly_fields = (
        'profile_id',
        'created_at',
        'updated_at',
    )

    @admin.display(description='Branch', ordering='staff__branch')
    def get_branch(self, obj):
        return obj.staff.branch if obj.staff_id else None


# ============================================================
# CONSULTATION
# ============================================================

@admin.register(Consultation)
class ConsultationAdmin(admin.ModelAdmin):
    inlines = [ConsultationTimelineInline]

    list_display = (
        'consultation_id',
        'patient',
        'doctor',
        'get_branch',
        'status',
        'consultation_date',
        'created_at',
    )

    list_filter = (
        'status',
        'consultation_date',
        'created_at',
        'patient__branch',
    )

    search_fields = (
        'patient__user__first_name',
        'patient__user__last_name',
        'patient__mrd_number',
        'doctor__staff__user__first_name',
        'doctor__staff__user__last_name',
    )

    readonly_fields = (
        'consultation_id',
        'started_at',
        'completed_at',
        'created_at',
        'updated_at',
    )

    @admin.display(description='Branch', ordering='patient__branch')
    def get_branch(self, obj):
        return obj.patient.branch if obj.patient_id else None


# ============================================================
# PRESCRIPTION
# ============================================================

@admin.register(Prescription)
class PrescriptionAdmin(admin.ModelAdmin):
    inlines = [PrescriptionItemInline]

    list_display = (
        'prescription_id',
        'consultation',
        'get_branch',
        'prescription_type',
        'prescribed_by',
        'is_sent_to_pharmacy',
        'created_at',
    )

    list_filter = (
        'prescription_type',
        'is_sent_to_pharmacy',
        'created_at',
        'consultation__patient__branch',
    )

    search_fields = (
        'consultation__patient__user__first_name',
        'consultation__patient__user__last_name',
        'consultation__patient__mrd_number',
    )

    readonly_fields = (
        'prescription_id',
        'created_at',
        'updated_at',
    )

    @admin.display(description='Branch', ordering='consultation__patient__branch')
    def get_branch(self, obj):
        try:
            return obj.consultation.patient.branch
        except Exception:
            return None


# ============================================================
# PRESCRIPTION ITEM
# ============================================================

@admin.register(PrescriptionItem)
class PrescriptionItemAdmin(admin.ModelAdmin):
    list_display = (
        'item_id',
        'medicine_name',
        'dose_quantity',
        'frequency',
        'meal_timing',
        'route',
        'duration_days',
        'quantity',
        'is_manual_quantity',
        'prescription',
    )

    list_filter = (
        'frequency',
        'meal_timing',
        'route',
        'is_manual_quantity',
        'is_route_overridden',
        'created_at',
    )

    search_fields = (
        'medicine_name',
        'medicine__name',
        'prescription__consultation__patient__user__first_name',
    )

    readonly_fields = (
        'item_id',
        'medicine_name',
        'calculated_quantity',
        'quantity_source',
        'created_at',
        'updated_at',
    )

    fieldsets = (
        (
            'Prescription & Medicine',
            {
                'fields': (
                    'prescription',
                    'medicine',
                    'medicine_name',
                )
            }
        ),
        (
            'Dosage & Route',
            {
                'fields': (
                    'dose_quantity',
                    'route',
                    'is_route_overridden',
                )
            }
        ),
        (
            'Frequency & Timing',
            {
                'fields': (
                    'frequency',
                    'meal_timing',
                )
            }
        ),
        (
            'Duration & Quantity',
            {
                'fields': (
                    'duration_days',
                    'quantity',
                    'calculated_quantity',
                    'is_manual_quantity',
                )
            }
        ),
        (
            'PRN/SOS Details (if applicable)',
            {
                'fields': (
                    'prn_reason',
                    'prn_reason_other',
                    'max_daily_dose',
                ),
                'classes': ('collapse',),
            }
        ),
        (
            'Additional Instructions',
            {
                'fields': (
                    'instructions',
                )
            }
        ),
        (
            'Metadata',
            {
                'fields': (
                    'created_at',
                    'updated_at',
                ),
                'classes': ('collapse',),
            }
        ),
    )


# ============================================================
# CONSULTATION TIMELINE
# ============================================================

@admin.register(ConsultationTimeline)
class ConsultationTimelineAdmin(admin.ModelAdmin):
    list_display = (
        'entry_id',
        'consultation',
        'event',
        'actor',
        'timestamp',
    )

    list_filter = (
        'event',
        'timestamp',
    )

    search_fields = (
        'consultation__consultation_id',
        'event',
        'actor__first_name',
        'actor__last_name',
    )

    readonly_fields = (
        'entry_id',
        'timestamp',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False