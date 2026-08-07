# manager/urls.py
from django.urls import path
from .views import (
    ManagerBranchesView,
    FinanceDashboardView,
    AllBillsView,
    BillDetailView,
    PurchasesAndRefundsView,
    SupportStaffListView, SupportStaffDetailView, SupportStaffActivateView,
    AllStaffListView,
    AttendanceListView, AttendanceBulkView, AttendanceSummaryView,
    LeaveRequestListView, LeaveRequestDetailView, LeaveApproveView, LeaveRejectView,
    SalaryListView, SalaryDetailView, SalaryMarkPaidView, SalaryGenerateView, SalaryDeleteView,
    SalaryEntryListCreateView, SalaryEntryDeleteView,
    ExpenseListView, ExpenseDetailView,
    IncomeListView, IncomeDetailView,
    HomeVisitFeeSettingsView,
    DealerListCreateView, DealerDetailView,
    DealerTransactionListView, DealerTransactionFinalizeView,
    DealerTransactionBulkFinalizeView, DealerTransactionVoidView,
    DealerTransactionScheduleUpdateView,
    ManagerEmrDoctorListView,
    WebsiteDoctorListView, WebsiteDoctorDetailView,
    WebsiteDoctorSearchView, WebsiteDoctorAttachBranchView,
    WebsiteDoctorWeeklyAvailabilityListView, WebsiteDoctorWeeklyAvailabilityDetailView,
    WebsiteDoctorAvailabilityExceptionListView, WebsiteDoctorAvailabilityExceptionDetailView,
    WebsiteBranchListView, WebsiteBranchDetailView,
    WebsiteQueryListView, WebsiteQueryDetailView,
    WebsiteTestimonialListView, WebsiteTestimonialDetailView,
    WebsiteTestimonialActivateView, WebsiteTestimonialDeactivateView, WebsiteTestimonialReorderView,
    WebsiteYoutubeVideoListView, WebsiteYoutubeVideoDetailView,
    WebsiteYoutubeVideoActivateView, WebsiteYoutubeVideoDeactivateView, WebsiteYoutubeVideoReorderView,
    WebsiteInstagramPostListView, WebsiteInstagramPostDetailView,
    WebsiteInstagramPostActivateView, WebsiteInstagramPostDeactivateView, WebsiteInstagramPostReorderView,
    WebsiteFacebookPostListView, WebsiteFacebookPostDetailView,
    WebsiteFacebookPostActivateView, WebsiteFacebookPostDeactivateView, WebsiteFacebookPostReorderView,
    WebsiteMediaEventListView, WebsiteMediaEventDetailView,
    WebsiteMediaEventActivateView, WebsiteMediaEventDeactivateView, WebsiteMediaEventReorderView,
    WebsiteGalleryImageListView, WebsiteGalleryImageDetailView,
    WebsiteGalleryImageActivateView, WebsiteGalleryImageDeactivateView, WebsiteGalleryImageReorderView,
    WebsiteSpecialtyListView, WebsiteSpecialtyDetailView,
    WebsiteSpecialtyActivateView, WebsiteSpecialtyDeactivateView, WebsiteSpecialtyReorderView,
    WebsiteTreatmentListView, WebsiteTreatmentDetailView,
    WebsiteTreatmentActivateView, WebsiteTreatmentDeactivateView, WebsiteTreatmentReorderView,
    WebsiteSpecialtySectionListView, WebsiteSpecialtySectionDetailView, WebsiteSpecialtySectionReorderView,
    WebsiteBlogListView, WebsiteBlogDetailView,
    WebsiteBlogActivateView, WebsiteBlogDeactivateView, WebsiteBlogReorderView,
)

urlpatterns = [
    # ── Branch access (self-service) ──────────────────────────────
    path("branches/",                   ManagerBranchesView.as_view(),   name="manager-branches"),

    # ── Finance dashboard ─────────────────────────────────────────
    path("finance/",                    FinanceDashboardView.as_view(),   name="manager-finance"),
    path("bills/",                      AllBillsView.as_view(),           name="manager-bills"),
    path("bills/detail/",               BillDetailView.as_view(),         name="manager-bill-detail"),
    path("purchases/",                  PurchasesAndRefundsView.as_view(), name="manager-purchases"),

    # ── Support staff (non-EMR) ───────────────────────────────────
    path("support-staff/",              SupportStaffListView.as_view(),   name="support-staff-list"),
    path("support-staff/<int:pk>/",     SupportStaffDetailView.as_view(), name="support-staff-detail"),
    path("support-staff/<int:pk>/activate/", SupportStaffActivateView.as_view(), name="support-staff-activate"),

    # ── All staff (EMR + non-EMR) ─────────────────────────────────
    path("all-staff/",                  AllStaffListView.as_view(),       name="all-staff-list"),

    # ── Attendance ────────────────────────────────────────────────
    path("attendance/",                 AttendanceListView.as_view(),     name="attendance-list"),
    path("attendance/bulk/",            AttendanceBulkView.as_view(),     name="attendance-bulk"),
    path("attendance/summary/",         AttendanceSummaryView.as_view(),  name="attendance-summary"),

    # ── Leave requests ────────────────────────────────────────────
    path("leaves/",                     LeaveRequestListView.as_view(),   name="leave-list"),
    path("leaves/<int:pk>/",            LeaveRequestDetailView.as_view(), name="leave-detail"),
    path("leaves/<int:pk>/approve/",    LeaveApproveView.as_view(),       name="leave-approve"),
    path("leaves/<int:pk>/reject/",     LeaveRejectView.as_view(),        name="leave-reject"),

    # ── Salary ────────────────────────────────────────────────────
    path("salary/",                     SalaryListView.as_view(),         name="salary-list"),
    path("salary/generate/",            SalaryGenerateView.as_view(),     name="salary-generate"),
    path("salary/<int:pk>/",            SalaryDetailView.as_view(),       name="salary-detail"),
    path("salary/<int:pk>/mark-paid/",  SalaryMarkPaidView.as_view(),     name="salary-mark-paid"),
    path("salary/<int:pk>/delete/",     SalaryDeleteView.as_view(),       name="salary-delete"),
    path("salary/<int:pk>/entries/",              SalaryEntryListCreateView.as_view(), name="salary-entries"),
    path("salary/<int:pk>/entries/<int:entry_id>/", SalaryEntryDeleteView.as_view(),   name="salary-entry-delete"),

    # ── Expenses ──────────────────────────────────────────────────
    path("expenses/",                   ExpenseListView.as_view(),        name="expense-list"),
    path("expenses/<int:pk>/",          ExpenseDetailView.as_view(),      name="expense-detail"),

    # ── Other Income ─────────────────────────────────────────────
    path("income/",                     IncomeListView.as_view(),         name="income-list"),
    path("income/<int:pk>/",            IncomeDetailView.as_view(),       name="income-detail"),

    # ── Home Visit fee defaults ─────────────────────────────────────
    path("home-visit-settings/",        HomeVisitFeeSettingsView.as_view(), name="home-visit-settings"),

    path("dealers/",                              DealerListCreateView.as_view(),        name="dealer-list"),
    path("dealers/<int:pk>/",                     DealerDetailView.as_view(),            name="dealer-detail"),
    path("dealers/transactions/",                 DealerTransactionListView.as_view(),   name="dealer-transaction-list"),
    path("dealers/transactions/bulk-finalize/",   DealerTransactionBulkFinalizeView.as_view(), name="dealer-transaction-bulk-finalize"),
    path("dealers/transactions/<int:pk>/finalize/", DealerTransactionFinalizeView.as_view(), name="dealer-transaction-finalize"),
    path("dealers/transactions/<int:pk>/void/",     DealerTransactionVoidView.as_view(),     name="dealer-transaction-void"),
    # ✅ FIX: DealerTransactionScheduleUpdateView existed in views.py (its
    # own docstring documents this exact route) but was never wired into
    # urls.py — the endpoint 404'd for every caller.
    path("dealers/transactions/<int:pk>/schedule/", DealerTransactionScheduleUpdateView.as_view(), name="dealer-transaction-schedule"),

    # ── Public website content (manager-curated) ───────────────────
    path("website/emr-doctors/",                ManagerEmrDoctorListView.as_view(), name="website-emr-doctor-list"),
    path("website/doctors/",                    WebsiteDoctorListView.as_view(),   name="website-doctor-list"),
    path("website/doctors/search/",             WebsiteDoctorSearchView.as_view(), name="website-doctor-search"),
    path("website/doctors/<int:pk>/",           WebsiteDoctorDetailView.as_view(), name="website-doctor-detail"),
    path("website/doctors/<int:pk>/attach-branch/",
         WebsiteDoctorAttachBranchView.as_view(), name="website-doctor-attach-branch"),

    path("website/doctors/<int:doctor_id>/weekly-availability/",
         WebsiteDoctorWeeklyAvailabilityListView.as_view(), name="website-doctor-weekly-availability-list"),
    path("website/doctors/<int:doctor_id>/weekly-availability/<int:pk>/",
         WebsiteDoctorWeeklyAvailabilityDetailView.as_view(), name="website-doctor-weekly-availability-detail"),

    path("website/doctors/<int:doctor_id>/availability-exceptions/",
         WebsiteDoctorAvailabilityExceptionListView.as_view(), name="website-doctor-availability-exception-list"),
    path("website/doctors/<int:doctor_id>/availability-exceptions/<int:pk>/",
         WebsiteDoctorAvailabilityExceptionDetailView.as_view(), name="website-doctor-availability-exception-detail"),

    path("website/branches/",                   WebsiteBranchListView.as_view(),   name="website-branch-list"),
    path("website/branches/<int:branch_id>/",   WebsiteBranchDetailView.as_view(), name="website-branch-detail"),

    path("website/queries/",                    WebsiteQueryListView.as_view(),   name="website-query-list"),
    path("website/queries/<int:pk>/",           WebsiteQueryDetailView.as_view(), name="website-query-detail"),

    path("website/testimonials/",                       WebsiteTestimonialListView.as_view(),   name="website-testimonial-list"),
    path("website/testimonials/reorder/",                WebsiteTestimonialReorderView.as_view(), name="website-testimonial-reorder"),
    path("website/testimonials/<int:pk>/",               WebsiteTestimonialDetailView.as_view(), name="website-testimonial-detail"),
    path("website/testimonials/<int:pk>/activate/",      WebsiteTestimonialActivateView.as_view(), name="website-testimonial-activate"),
    path("website/testimonials/<int:pk>/deactivate/",    WebsiteTestimonialDeactivateView.as_view(), name="website-testimonial-deactivate"),

    path("website/youtube-videos/",                      WebsiteYoutubeVideoListView.as_view(),   name="website-youtube-list"),
    path("website/youtube-videos/reorder/",              WebsiteYoutubeVideoReorderView.as_view(), name="website-youtube-reorder"),
    path("website/youtube-videos/<int:pk>/",             WebsiteYoutubeVideoDetailView.as_view(), name="website-youtube-detail"),
    path("website/youtube-videos/<int:pk>/activate/",    WebsiteYoutubeVideoActivateView.as_view(), name="website-youtube-activate"),
    path("website/youtube-videos/<int:pk>/deactivate/",  WebsiteYoutubeVideoDeactivateView.as_view(), name="website-youtube-deactivate"),

    path("website/instagram-posts/",                     WebsiteInstagramPostListView.as_view(),   name="website-instagram-list"),
    path("website/instagram-posts/reorder/",             WebsiteInstagramPostReorderView.as_view(), name="website-instagram-reorder"),
    path("website/instagram-posts/<int:pk>/",            WebsiteInstagramPostDetailView.as_view(), name="website-instagram-detail"),
    path("website/instagram-posts/<int:pk>/activate/",   WebsiteInstagramPostActivateView.as_view(), name="website-instagram-activate"),
    path("website/instagram-posts/<int:pk>/deactivate/", WebsiteInstagramPostDeactivateView.as_view(), name="website-instagram-deactivate"),

    path("website/facebook-posts/",                      WebsiteFacebookPostListView.as_view(),   name="website-facebook-list"),
    path("website/facebook-posts/reorder/",              WebsiteFacebookPostReorderView.as_view(), name="website-facebook-reorder"),
    path("website/facebook-posts/<int:pk>/",             WebsiteFacebookPostDetailView.as_view(), name="website-facebook-detail"),
    path("website/facebook-posts/<int:pk>/activate/",    WebsiteFacebookPostActivateView.as_view(), name="website-facebook-activate"),
    path("website/facebook-posts/<int:pk>/deactivate/",  WebsiteFacebookPostDeactivateView.as_view(), name="website-facebook-deactivate"),

    path("website/media-events/",                      WebsiteMediaEventListView.as_view(),   name="website-media-event-list"),
    path("website/media-events/reorder/",              WebsiteMediaEventReorderView.as_view(), name="website-media-event-reorder"),
    path("website/media-events/<int:pk>/",             WebsiteMediaEventDetailView.as_view(), name="website-media-event-detail"),
    path("website/media-events/<int:pk>/activate/",    WebsiteMediaEventActivateView.as_view(), name="website-media-event-activate"),
    path("website/media-events/<int:pk>/deactivate/",  WebsiteMediaEventDeactivateView.as_view(), name="website-media-event-deactivate"),

    path("website/gallery/",                      WebsiteGalleryImageListView.as_view(),   name="website-gallery-list"),
    path("website/gallery/reorder/",              WebsiteGalleryImageReorderView.as_view(), name="website-gallery-reorder"),
    path("website/gallery/<int:pk>/",             WebsiteGalleryImageDetailView.as_view(), name="website-gallery-detail"),
    path("website/gallery/<int:pk>/activate/",    WebsiteGalleryImageActivateView.as_view(), name="website-gallery-activate"),
    path("website/gallery/<int:pk>/deactivate/",  WebsiteGalleryImageDeactivateView.as_view(), name="website-gallery-deactivate"),

    path("website/specialities/",                       WebsiteSpecialtyListView.as_view(),   name="website-specialty-list"),
    path("website/specialities/reorder/",                WebsiteSpecialtyReorderView.as_view(), name="website-specialty-reorder"),
    path("website/specialities/<int:pk>/",               WebsiteSpecialtyDetailView.as_view(), name="website-specialty-detail"),
    path("website/specialities/<int:pk>/activate/",      WebsiteSpecialtyActivateView.as_view(), name="website-specialty-activate"),
    path("website/specialities/<int:pk>/deactivate/",    WebsiteSpecialtyDeactivateView.as_view(), name="website-specialty-deactivate"),

    path("website/treatments/",                          WebsiteTreatmentListView.as_view(),   name="website-treatment-list"),
    path("website/treatments/reorder/",                  WebsiteTreatmentReorderView.as_view(), name="website-treatment-reorder"),
    path("website/treatments/<int:pk>/",                 WebsiteTreatmentDetailView.as_view(), name="website-treatment-detail"),
    path("website/treatments/<int:pk>/activate/",        WebsiteTreatmentActivateView.as_view(), name="website-treatment-activate"),
    path("website/treatments/<int:pk>/deactivate/",      WebsiteTreatmentDeactivateView.as_view(), name="website-treatment-deactivate"),

    path("website/specialty-sections/",                  WebsiteSpecialtySectionListView.as_view(),   name="website-specialty-section-list"),
    path("website/specialty-sections/reorder/",          WebsiteSpecialtySectionReorderView.as_view(), name="website-specialty-section-reorder"),
    path("website/specialty-sections/<int:pk>/",         WebsiteSpecialtySectionDetailView.as_view(), name="website-specialty-section-detail"),

    path("website/blogs/",                               WebsiteBlogListView.as_view(),   name="website-blog-list"),
    path("website/blogs/reorder/",                       WebsiteBlogReorderView.as_view(), name="website-blog-reorder"),
    path("website/blogs/<int:pk>/",                      WebsiteBlogDetailView.as_view(), name="website-blog-detail"),
    path("website/blogs/<int:pk>/activate/",             WebsiteBlogActivateView.as_view(), name="website-blog-activate"),
    path("website/blogs/<int:pk>/deactivate/",           WebsiteBlogDeactivateView.as_view(), name="website-blog-deactivate"),
]