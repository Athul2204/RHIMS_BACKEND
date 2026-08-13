from django.urls import path
from .views import (
    AdminDashboardView,
    BranchListView, BranchDetailView,
    StaffListView, StaffDetailView, StaffDeactivateView, StaffReactivateView,
    StaffPromoteGroupAdminView,
    ReceptionistListView, ReceptionistDetailView,
    PharmacistListView, PharmacistDetailView,
    DoctorListView, DoctorDetailView,
    GuestDoctorListView, GuestDoctorDetailView,
    GuestDoctorDeactivateView, GuestDoctorReactivateView,
    CommonReceptionistListView, CommonReceptionistDetailView,
    CommonReceptionistDeactivateView, CommonReceptionistReactivateView,
    CommonPharmacistListView, CommonPharmacistDetailView,
    CommonPharmacistDeactivateView, CommonPharmacistReactivateView,
    ProcedureListView, ProcedureDetailView,
    BillingDepartmentListView, BillingDepartmentDetailView,
    AuditLogListView,
    ReceptionistSelfView, PharmacistSelfView,
    HospitalSettingsView,
    ManagerBranchAccessListView, ManagerBranchAccessRevokeView,)

urlpatterns = [
    # ── Dashboard ────────────────────────────────────────────────
    path("dashboard/", AdminDashboardView.as_view(), name="admin-dashboard"),

    # ── Branches ─────────────────────────────────────────────────
    path("branches/",           BranchListView.as_view(),   name="branch-list"),
    path("branches/<int:pk>/",  BranchDetailView.as_view(), name="branch-detail"),

    # ── Staff ────────────────────────────────────────────────────
    path("staff/",                        StaffListView.as_view(),       name="staff-list"),
    path("staff/<int:pk>/",               StaffDetailView.as_view(),     name="staff-detail"),
    path("staff/<int:pk>/deactivate/",    StaffDeactivateView.as_view(), name="staff-deactivate"),
    path("staff/<int:pk>/reactivate/",    StaffReactivateView.as_view(), name="staff-reactivate"),
    path("staff/<int:pk>/promote-group-admin/", StaffPromoteGroupAdminView.as_view(), name="staff-promote-group-admin"),

    # ── Receptionist ─────────────────────────────────────────────
    path("receptionist/me/",              ReceptionistSelfView.as_view(),   name="receptionist-self"),
    path("receptionist/",                 ReceptionistListView.as_view(),   name="receptionist-list"),
    path("receptionist/<int:pk>/",        ReceptionistDetailView.as_view(), name="receptionist-detail"),

    # ── Pharmacist ───────────────────────────────────────────────
    path("pharmacist/me/",                PharmacistSelfView.as_view(),   name="pharmacist-self"),
    path("pharmacist/",                   PharmacistListView.as_view(),   name="pharmacist-list"),
    path("pharmacist/<int:pk>/",          PharmacistDetailView.as_view(), name="pharmacist-detail"),

    # ── Doctors ──────────────────────────────────────────────────
    path("doctors/",                      DoctorListView.as_view(),   name="doctor-list"),
    path("doctors/<int:pk>/",             DoctorDetailView.as_view(), name="doctor-detail"),

    # ── Guest Doctors ─────────────────────────────────────────────
    # Action routes before /<pk>/ to avoid shadowing
    path("guest-doctors/<int:pk>/deactivate/", GuestDoctorDeactivateView.as_view(), name="guest-doctor-deactivate"),
    path("guest-doctors/<int:pk>/reactivate/", GuestDoctorReactivateView.as_view(), name="guest-doctor-reactivate"),
    path("guest-doctors/",                     GuestDoctorListView.as_view(),       name="guest-doctor-list"),
    path("guest-doctors/<int:pk>/",            GuestDoctorDetailView.as_view(),     name="guest-doctor-detail"),

    # ── Common Receptionist ────────────────────────────────────────
    # Action routes before /<pk>/ to avoid shadowing
    path("common-receptionists/<int:pk>/deactivate/", CommonReceptionistDeactivateView.as_view(), name="common-receptionist-deactivate"),
    path("common-receptionists/<int:pk>/reactivate/", CommonReceptionistReactivateView.as_view(), name="common-receptionist-reactivate"),
    path("common-receptionists/",                     CommonReceptionistListView.as_view(),       name="common-receptionist-list"),
    path("common-receptionists/<int:pk>/",            CommonReceptionistDetailView.as_view(),     name="common-receptionist-detail"),

    # ── Common Pharmacist ──────────────────────────────────────────
    path("common-pharmacists/<int:pk>/deactivate/",   CommonPharmacistDeactivateView.as_view(),   name="common-pharmacist-deactivate"),
    path("common-pharmacists/<int:pk>/reactivate/",   CommonPharmacistReactivateView.as_view(),   name="common-pharmacist-reactivate"),
    path("common-pharmacists/",                       CommonPharmacistListView.as_view(),         name="common-pharmacist-list"),
    path("common-pharmacists/<int:pk>/",              CommonPharmacistDetailView.as_view(),       name="common-pharmacist-detail"),

    # ── Procedures ───────────────────────────────────────────────
    path("procedures/",                   ProcedureListView.as_view(),   name="procedure-list"),
    path("procedures/<int:pk>/",          ProcedureDetailView.as_view(), name="procedure-detail"),

    # ── Billing Departments ─────────────────────────────────────────
    path("billing-departments/",          BillingDepartmentListView.as_view(),   name="billing-department-list"),
    path("billing-departments/<int:pk>/", BillingDepartmentDetailView.as_view(), name="billing-department-detail"),

    # ── Audit log ────────────────────────────────────────────────
    path("audit/",                        AuditLogListView.as_view(),    name="audit-log"),

    # ── Hospital Settings ─────────────────────────────────────────────
    path('settings/', HospitalSettingsView.as_view(), name='hospital-settings'),

    # ── Manager branch access (group-admin only) ───────────────────
    path("manager-branch-access/",             ManagerBranchAccessListView.as_view(),   name="manager-branch-access-list"),
    path("manager-branch-access/<int:pk>/revoke/", ManagerBranchAccessRevokeView.as_view(), name="manager-branch-access-revoke"),
]