from django.urls import path

from .views import (
    TestGroupListView,
    TestGroupDetailView,

    LabTestListView,
    LabTestDetailView,

    LabRequestListView,
    LabRequestCreateView,
    WalkInLabRequestCreateView,
    LabRequestDetailView,
    LabRequestStatusUpdateView,
    LabRequestClaimView,
    LabRequestUnclaimView,

    LabResultCreateView,
    LabResultDetailView,
    LabResultsByRequestView,

    LabReportDetailView,
    PatientLabHistoryView,
    LabDashboardView,

    LabBillListView,
    LabBillDetailView,
    LabBillByRequestView,
    LabBillGenerateView,
    LabBillPayView,

    LabEquipmentListView,
    LabEquipmentDetailView,

    LabMaintenanceListView,
    LabMaintenanceDetailView,

    LabOrderListView,
    LabOrderDetailView,
)

urlpatterns = [

    # ─────────────────────────────────────────────
    # LAB TESTS
    # ─────────────────────────────────────────────
    path(
        'tests/',
        LabTestListView.as_view(),
        name='lab-test-list'
    ),

    path(
        'tests/<int:pk>/',
        LabTestDetailView.as_view(),
        name='lab-test-detail'
    ),

    # ─────────────────────────────────────────────
    # TEST GROUPS (panels billed as one unit, e.g. LFT/KFT/LIPID)
    # ─────────────────────────────────────────────
    path(
        'test-groups/',
        TestGroupListView.as_view(),
        name='lab-test-group-list'
    ),

    path(
        'test-groups/<int:pk>/',
        TestGroupDetailView.as_view(),
        name='lab-test-group-detail'
    ),

    # ─────────────────────────────────────────────
    # LAB REQUESTS
    # ─────────────────────────────────────────────
    path(
        'requests/',
        LabRequestListView.as_view(),
        name='lab-request-list'
    ),

    path(
        'requests/create/',
        LabRequestCreateView.as_view(),
        name='lab-request-create'
    ),

    # Walk-in patient — created directly by the lab technician, no
    # doctor consultation / MRD registration required.
    path(
        'requests/walkin/create/',
        WalkInLabRequestCreateView.as_view(),
        name='lab-request-walkin-create'
    ),

    path(
        'requests/<int:pk>/',
        LabRequestDetailView.as_view(),
        name='lab-request-detail'
    ),

    path(
        'requests/<int:pk>/status/',
        LabRequestStatusUpdateView.as_view(),
        name='lab-request-status-update'
    ),

    path(
        'requests/<int:pk>/claim/',
        LabRequestClaimView.as_view(),
        name='lab-request-claim'
    ),

    path(
        'requests/<int:pk>/unclaim/',
        LabRequestUnclaimView.as_view(),
        name='lab-request-unclaim'
    ),

    # Bill for a specific request (get + regenerate)
    path(
        'requests/<int:request_pk>/bill/',
        LabBillByRequestView.as_view(),
        name='lab-bill-by-request'
    ),

    path(
        'requests/<int:request_pk>/bill/generate/',
        LabBillGenerateView.as_view(),
        name='lab-bill-generate'
    ),

    # ─────────────────────────────────────────────
    # LAB RESULTS
    # ─────────────────────────────────────────────
    path(
        'results/',
        LabResultCreateView.as_view(),
        name='lab-result-create'
    ),

    path(
        'results/<int:pk>/',
        LabResultDetailView.as_view(),
        name='lab-result-detail'
    ),

    path(
        'requests/<int:request_pk>/results/',
        LabResultsByRequestView.as_view(),
        name='lab-results-by-request'
    ),

    # ─────────────────────────────────────────────
    # LAB REPORTS
    # ─────────────────────────────────────────────
    path(
        'requests/<int:request_pk>/report/',
        LabReportDetailView.as_view(),
        name='lab-report-detail'
    ),

    # ─────────────────────────────────────────────
    # PATIENT HISTORY
    # ─────────────────────────────────────────────
    path(
        'patients/<int:patient_id>/history/',
        PatientLabHistoryView.as_view(),
        name='lab-patient-history'
    ),

    # ─────────────────────────────────────────────
    # DASHBOARD
    # ─────────────────────────────────────────────
    path(
        'dashboard/',
        LabDashboardView.as_view(),
        name='lab-dashboard'
    ),

    # ─────────────────────────────────────────────
    # LAB BILLS  (list / create / detail / pay)
    # ─────────────────────────────────────────────
    path(
        'bills/',
        LabBillListView.as_view(),
        name='lab-bill-list'
    ),

    path(
        'bills/<int:pk>/',
        LabBillDetailView.as_view(),
        name='lab-bill-detail'
    ),

    # Reception collects payment -> unlocks sample collection
    path(
        'bills/<int:pk>/pay/',
        LabBillPayView.as_view(),
        name='lab-bill-pay'
    ),

    # ─────────────────────────────────────────────
    # LAB EQUIPMENT
    # ─────────────────────────────────────────────
    path(
        'equipment/',
        LabEquipmentListView.as_view(),
        name='lab-equipment-list'
    ),

    path(
        'equipment/<int:pk>/',
        LabEquipmentDetailView.as_view(),
        name='lab-equipment-detail'
    ),

    # ─────────────────────────────────────────────
    # LAB MAINTENANCE
    # ─────────────────────────────────────────────
    path(
        'equipment/<int:equipment_pk>/maintenance/',
        LabMaintenanceListView.as_view(),
        name='lab-maintenance-list'
    ),

    path(
        'maintenance/<int:pk>/',
        LabMaintenanceDetailView.as_view(),
        name='lab-maintenance-detail'
    ),

    # ─────────────────────────────────────────────
    # LAB ORDERS
    # ─────────────────────────────────────────────
    path(
        'orders/',
        LabOrderListView.as_view(),
        name='lab-order-list'
    ),

    path(
        'orders/<int:pk>/',
        LabOrderDetailView.as_view(),
        name='lab-order-detail'
    ),
]