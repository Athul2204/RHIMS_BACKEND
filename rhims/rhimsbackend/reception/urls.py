from django.urls import path
from .views import (
    CreatePatientView,
    PatientListView,
    PatientDetailView,
    RevisitCheckView,
    CreateConsultationBillView,
    ConsultationBillListView,
    ConsultationBillDetailView,
    MarkBillPaidView,
    PatientConsultationHistoryView,
    ActiveDoctorsForReceptionView,
    ReassignBillDoctorView,
    CancelBillView,
    ConsultationPreBookingListCreateView,
    ConsultationPreBookingDetailView,
    ConsultationPreBookingPayView,
    ConsultationPreBookingConvertView,
    ConsultationPreBookingCancelView,
    ReceptionPharmacyBillListView,
    ReceptionPharmacyBillDetailView,
)

urlpatterns = [
    # ── Patients ─────────────────────────────────────────────────
    path('patients/create/', CreatePatientView.as_view(),        name='patient-create'),
    path('patients/',        PatientListView.as_view(),          name='patient-list'),
    path('patients/<int:pk>/', PatientDetailView.as_view(),      name='patient-detail'),

    # ── Revisit check ─────────────────────────────────────────────
    path('revisit-check/', RevisitCheckView.as_view(),           name='revisit-check'),

    # ── Active doctors dropdown (for bill creation) ──────────────
    path('active-doctors/', ActiveDoctorsForReceptionView.as_view(), name='active-doctors'),

    # ── Consultation bills ───────────────────────────────────────
    path('bills/create/',        CreateConsultationBillView.as_view(), name='bill-create'),
    path('bills/',               ConsultationBillListView.as_view(),   name='bill-list'),
    path('bills/<int:pk>/',      ConsultationBillDetailView.as_view(), name='bill-detail'),
    path('bills/<int:pk>/pay/',  MarkBillPaidView.as_view(),           name='bill-pay'),
    path('bills/<int:pk>/cancel/', CancelBillView.as_view(),           name='bill-cancel'),
    path('bills/<int:pk>/reassign-doctor/', ReassignBillDoctorView.as_view(), name='bill-reassign-doctor'),

    # ── Pharmacy bills sent to reception ───────────────────────────
    path('pharmacy-bills/',         ReceptionPharmacyBillListView.as_view(),   name='reception-pharmacy-bill-list'),
    path('pharmacy-bills/<int:pk>/', ReceptionPharmacyBillDetailView.as_view(), name='reception-pharmacy-bill-detail'),


    # ── Patient consultation history ─────────────────────────────
    path('patients/<int:patient_id>/history/', PatientConsultationHistoryView.as_view(), name='patient-history'),

    # ── Consultation prebookings ───────────────────────────────────
    path('prebookings/',                ConsultationPreBookingListCreateView.as_view(), name='prebooking-list-create'),
    path('prebookings/<int:pk>/',       ConsultationPreBookingDetailView.as_view(),     name='prebooking-detail'),
    path('prebookings/<int:pk>/pay/',   ConsultationPreBookingPayView.as_view(),        name='prebooking-pay'),
    path('prebookings/<int:pk>/convert/', ConsultationPreBookingConvertView.as_view(),  name='prebooking-convert'),
    path('prebookings/<int:pk>/cancel/', ConsultationPreBookingCancelView.as_view(),    name='prebooking-cancel'),
]