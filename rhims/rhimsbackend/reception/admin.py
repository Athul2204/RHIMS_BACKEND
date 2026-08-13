from django.contrib import admin
from .models import Patient, ConsultationBill


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = [
        'patient_id',
        'mrd_number',
        'first_name',
        'last_name',
        'phone',
        'place',
        'assigned_doctor',
        'created_at',
    ]
    list_filter = [
        'gender',
        'blood_group',
        'place',
        'assigned_doctor',
        'created_at',
    ]
    search_fields = [
        'mrd_number',
        'first_name',
        'last_name',
        'phone',
        'place',
    ]
    readonly_fields = [
        'patient_id',
        'mrd_number',
        'created_at',
    ]
    fieldsets = (
        ('Basic Information', {
            'fields': ('patient_id', 'mrd_number', 'first_name', 'last_name', 'phone')
        }),
        ('Personal Details', {
            'fields': ('date_of_birth', 'age', 'gender', 'blood_group')
        }),
        ('Address', {
            'fields': ('place', 'address')
        }),
        ('Doctor Assignment', {
            'fields': ('assigned_doctor',)
        }),
        ('Billing', {
            'fields': ('registration_fee_paid',)
        }),
        ('Metadata', {
            'fields': ('created_at',),
            'classes': ('collapse',)
        }),
    )


@admin.register(ConsultationBill)
class ConsultationBillAdmin(admin.ModelAdmin):
    list_display = [
        'bill_id',
        'bill_number',
        'patient',
        'total_amount',
        'payment_status',
        'consultation_type',
        'billed_department',
        'consultation_date',
        'created_at',
    ]
    list_filter = [
        'payment_status',
        'consultation_type',
        'billed_department',
        'consultation_date',
        'created_at',
        'payment_method',
    ]
    search_fields = [
        'bill_number',
        'op_number',
        'patient__mrd_number',
        'patient__first_name',
        'patient__last_name',
        'doctor_name',
    ]
    readonly_fields = [
        'bill_id',
        'bill_number',
        'op_number',
        'created_at',
    ]
    fieldsets = (
        ('Bill Information', {
            'fields': ('bill_id', 'bill_number', 'op_number')
        }),
        ('Patient', {
            'fields': ('patient',)
        }),
        ('Doctor Information', {
            'fields': ('doctor', 'guest_doctor', 'doctor_name')
        }),
        ('Consultation Details', {
            'fields': (
                'consultation_date',
                'consultation_type',
                'consultation_fee',
                'billed_department',
            )
        }),
        ('Billing', {
            'fields': (
                'registration_fee',
                'total_amount',
                'revisit_valid_until',
                'registration_fee_paid',
            )
        }),
        ('Payment', {
            'fields': (
                'payment_method',
                'payment_status',
                'upi_reference',
            )
        }),
        ('Notes', {
            'fields': ('notes',)
        }),
        ('Metadata', {
            'fields': ('created_at',),
            'classes': ('collapse',)
        }),
    )