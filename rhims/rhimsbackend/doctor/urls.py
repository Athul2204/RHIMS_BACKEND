# pyrefly: ignore [missing-import]
from django.urls import path
from .views import (
    DoctorProfileView,
    ConsultationListView,
    ConsultationDetailView,
    ConsultationTimelineView,
    PrescriptionListView,
    PrescriptionDetailView,
    FollowUpReminderListView,
    FollowUpReminderDetailView,
    FollowUpReminderSummaryView,
    PatientListView,
    PatientConsultationHistoryView,
)

# Import views module for function-based endpoints
from . import views


urlpatterns = [

    # ─────────────────────────────────────────────
    # DOCTOR PROFILE
    # ─────────────────────────────────────────────
    path('profile/', DoctorProfileView.as_view(), name='doctor-profile'),

    # ─────────────────────────────────────────────
    # PATIENTS (Doctor can view assigned patients)
    # ─────────────────────────────────────────────
    path('patients/', PatientListView.as_view(), name='doctor-patient-list'),
    path('patients/<int:patient_id>/history/', PatientConsultationHistoryView.as_view(), name='patient-consultation-history'),

    # ─────────────────────────────────────────────
    # CONSULTATIONS
    # ─────────────────────────────────────────────
    path('consultations/', ConsultationListView.as_view(), name='consultation-list'),
    path('consultations/<int:consultation_id>/', ConsultationDetailView.as_view(), name='consultation-detail'),
    path('consultations/<int:consultation_id>/timeline/', ConsultationTimelineView.as_view(), name='consultation-timeline'),

    # ─────────────────────────────────────────────
    # PRESCRIPTIONS
    # ─────────────────────────────────────────────
    path('prescriptions/', PrescriptionListView.as_view(), name='prescription-list'),
    path('prescriptions/<int:prescription_id>/', PrescriptionDetailView.as_view(), name='prescription-detail'),

    # ─────────────────────────────────────────────
    # FOLLOW-UP REMINDERS
    # ─────────────────────────────────────────────
    path('reminders/', FollowUpReminderListView.as_view(), name='reminder-list'),
    path('reminders/<int:reminder_id>/', FollowUpReminderDetailView.as_view(), name='reminder-detail'),
    path('reminders/summary/', FollowUpReminderSummaryView.as_view(), name='reminder-summary'),
]


# ─────────────────────────────────────────────────────────────────────────────
# PRESCRIPTION OVERRIDE & VALIDATION ENDPOINTS (Function-Based)
# ─────────────────────────────────────────────────────────────────────────────

urlpatterns += [
    # Create a new prescription item
    path(
        'prescriptions/<int:prescription_id>/items/',
        views.prescription_item_create,
        name='prescription_item_create'
    ),

    # Delete a prescription item
    path(
        'prescriptions/<int:prescription_id>/items/<int:item_id>/',
        views.prescription_item_delete,
        name='prescription_item_delete'
    ),

    # Override route for a prescription item
    path(
        'prescriptions/<int:prescription_id>/items/<int:item_id>/override-route/',
        views.prescription_item_override_route,
        name='prescription_item_override_route'
    ),
 
    # Override quantity for a prescription item
    path(
        'prescriptions/<int:prescription_id>/items/<int:item_id>/override-quantity/',
        views.prescription_item_override_quantity,
        name='prescription_item_override_quantity'
    ),
 
    # Recalculate quantity without saving
    path(
        'prescriptions/<int:prescription_id>/items/<int:item_id>/recalculate-quantity/',
        views.prescription_item_recalculate_quantity,
        name='prescription_item_recalculate_quantity'
    ),
 
    # Validate prescription before sending to pharmacy
    path(
        'prescriptions/<int:prescription_id>/validate/',
        views.validate_prescription,
        name='validate_prescription'
    ),
]