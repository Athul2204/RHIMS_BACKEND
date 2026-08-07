# manager/admin.py
from django.contrib import admin
from django.db.models import Q
from authentication.utils import is_group_admin_user, get_user_branch
from .models import (
    SupportStaff, Attendance, LeaveRequest, SalaryRecord, HospitalExpense, OtherIncome,
    DoctorWebsiteProfile, DoctorWeeklyAvailability, DoctorAvailabilityException,
    PatientQuery, Testimonial, YoutubeVideo, InstagramPost, FacebookPost,
    MediaEvent, GalleryImage,
    Specialty, SpecialtySection, Treatment, Blog,
    BranchWebsiteProfile,
)


# ✅ FIX: every StaffProfile/SupportStaff account gets is_staff=True (see
# administration/models.py), which is what grants Django admin login —
# so a branch-scoped Admin/Manager can reach /admin/ too, not just
# superusers. None of the ModelAdmins below filtered by branch, so any
# branch's staff/attendance/leave/salary/expense/income records were
# visible and editable to every other branch's staff through this door,
# even though the exact same data is correctly branch-scoped over the
# DRF API. These two mixins close that gap the same way the API does:
# group admins (is_group_admin) see everything, everyone else is pinned
# to their own StaffProfile.branch.
class BranchScopedAdminMixin:
    """Scopes get_queryset() to the caller's own branch for a ModelAdmin
    whose model has a direct `branch` FK."""
    branch_field = "branch"

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if is_group_admin_user(request.user):
            return qs
        branch = get_user_branch(request.user)
        if branch is None:
            return qs.none()
        return qs.filter(**{f"{self.branch_field}_id": branch.pk})


class DualStaffBranchScopedAdminMixin:
    """Same as BranchScopedAdminMixin, but for a model that links to a
    staff member via one of two mutually-exclusive FKs (staff_profile OR
    support_staff) — Attendance, LeaveRequest, SalaryRecord."""

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if is_group_admin_user(request.user):
            return qs
        branch = get_user_branch(request.user)
        if branch is None:
            return qs.none()
        return qs.filter(Q(staff_profile__branch=branch) | Q(support_staff__branch=branch))


@admin.register(SupportStaff)
class SupportStaffAdmin(BranchScopedAdminMixin, admin.ModelAdmin):
    list_display  = ["staff_code", "branch", "full_name", "role", "department", "salary_type", "monthly_salary", "is_active"]
    list_filter   = ["branch", "role", "is_active", "salary_type"]
    search_fields = ["full_name", "staff_code", "phone"]
    readonly_fields = ["staff_code", "created_at", "updated_at"]


@admin.register(Attendance)
class AttendanceAdmin(DualStaffBranchScopedAdminMixin, admin.ModelAdmin):
    list_display  = ["date", "get_staff", "status"]
    list_filter   = ["status", "date"]
    date_hierarchy = "date"

    def get_staff(self, obj):
        return obj.staff_profile or obj.support_staff
    get_staff.short_description = "Staff"


@admin.register(LeaveRequest)
class LeaveRequestAdmin(DualStaffBranchScopedAdminMixin, admin.ModelAdmin):
    list_display  = ["get_staff", "leave_type", "start_date", "end_date", "status"]
    list_filter   = ["status", "leave_type"]

    def get_staff(self, obj):
        return obj.staff_profile or obj.support_staff
    get_staff.short_description = "Staff"


@admin.register(SalaryRecord)
class SalaryRecordAdmin(DualStaffBranchScopedAdminMixin, admin.ModelAdmin):
    list_display  = ["get_staff", "month", "year", "net_salary", "bonus", "deductions", "is_paid"]
    list_filter   = ["month", "year", "is_paid"]

    def get_staff(self, obj):
        return obj.staff_profile or obj.support_staff
    get_staff.short_description = "Staff"


@admin.register(HospitalExpense)
class HospitalExpenseAdmin(BranchScopedAdminMixin, admin.ModelAdmin):
    list_display  = ["date", "branch", "category", "title", "amount", "added_by"]
    list_filter   = ["branch", "category"]
    date_hierarchy = "date"


@admin.register(OtherIncome)
class OtherIncomeAdmin(BranchScopedAdminMixin, admin.ModelAdmin):
    list_display  = ["date", "branch", "category", "title", "amount", "added_by"]
    list_filter   = ["branch", "category"]
    date_hierarchy = "date"


@admin.register(DoctorWebsiteProfile)
class DoctorWebsiteProfileAdmin(admin.ModelAdmin):
    list_display  = ["get_name", "is_published", "display_order", "get_branches"]
    list_filter   = ["is_published"]
    # ✅ FIX: doctors is now M2M (was a plain FK) — filter_horizontal gives
    # a searchable dual-list widget instead of Django's default giant
    # multi-select box, which gets unusable past a handful of doctors.
    filter_horizontal = ["doctors"]

    def get_queryset(self, request):
        # ✅ FIX: same M2M branch-scoping the API uses (WebsiteDoctorListView)
        # — a branch-scoped user only sees profiles with at least one
        # attached doctor in their own branch; a group admin sees every profile.
        qs = super().get_queryset(request)
        if is_group_admin_user(request.user):
            return qs
        branch = get_user_branch(request.user)
        if branch is None:
            return qs.none()
        return qs.filter(doctors__staff__branch=branch).distinct()

    def get_name(self, obj):
        return obj.get_name()
    get_name.short_description = "Doctor"

    def get_branches(self, obj):
        return ", ".join(b["branch_name"] for b in obj.branch_summaries() if b["branch_name"]) or "—"
    get_branches.short_description = "Branches"


@admin.register(BranchWebsiteProfile)
class BranchWebsiteProfileAdmin(BranchScopedAdminMixin, admin.ModelAdmin):
    """Where the team writes each branch's public "Locations" page copy
    (description/highlights/hours/map link/photo) and flips is_published
    when it's ready to go live — see BranchWebsiteProfile's docstring and
    PublicBranchListView for how this feeds the public site."""
    list_display  = ["branch", "is_published", "email", "hours_text", "display_order"]
    list_filter   = ["is_published"]
    fields = [
        "branch", "is_published", "photo", "description", "highlights",
        "email", "hours_text", "map_url", "display_order",
    ]


@admin.register(DoctorWeeklyAvailability)
class DoctorWeeklyAvailabilityAdmin(admin.ModelAdmin):
    list_display  = ["doctor", "day_of_week", "start_time", "end_time", "slot_duration_minutes"]
    list_filter   = ["day_of_week"]

    def get_queryset(self, request):
        # ✅ FIX: previously unscoped — a branch-scoped user could see
        # every branch's doctor availability here.
        qs = super().get_queryset(request)
        if is_group_admin_user(request.user):
            return qs
        branch = get_user_branch(request.user)
        if branch is None:
            return qs.none()
        return qs.filter(doctor__staff__branch=branch)


@admin.register(DoctorAvailabilityException)
class DoctorAvailabilityExceptionAdmin(admin.ModelAdmin):
    list_display  = ["doctor", "date", "is_unavailable", "start_time", "end_time"]
    list_filter   = ["is_unavailable"]
    date_hierarchy = "date"

    def get_queryset(self, request):
        # ✅ FIX: previously unscoped — a branch-scoped user could see
        # every branch's availability exceptions here.
        qs = super().get_queryset(request)
        if is_group_admin_user(request.user):
            return qs
        branch = get_user_branch(request.user)
        if branch is None:
            return qs.none()
        return qs.filter(doctor__staff__branch=branch)


@admin.register(PatientQuery)
class PatientQueryAdmin(admin.ModelAdmin):
    list_display  = ["name", "query_type", "phone", "preferred_branch", "status", "created_at"]
    list_filter   = ["query_type", "status", "preferred_branch"]
    search_fields = ["name", "phone", "email"]

    def get_queryset(self, request):
        # ✅ FIX: matches the API's _scope_patient_queries rule — a
        # branch-scoped user sees their own branch's enquiries plus every
        # general (preferred_branch=None) enquiry; a group admin sees all.
        qs = super().get_queryset(request)
        if is_group_admin_user(request.user):
            return qs
        branch = get_user_branch(request.user)
        if branch is None:
            return qs.none()
        return qs.filter(Q(preferred_branch=branch) | Q(preferred_branch__isnull=True))


@admin.register(Testimonial)
class TestimonialAdmin(admin.ModelAdmin):
    list_display  = ["patient_name", "designation", "rating", "is_active", "is_featured", "display_order"]
    list_filter   = ["is_active", "is_featured"]
    search_fields = ["patient_name", "review"]


@admin.register(YoutubeVideo)
class YoutubeVideoAdmin(admin.ModelAdmin):
    list_display  = ["title", "video_id", "doctor", "is_active", "is_featured", "display_order"]
    list_filter   = ["is_active", "is_featured"]
    search_fields = ["title", "video_id"]


@admin.register(InstagramPost)
class InstagramPostAdmin(admin.ModelAdmin):
    list_display  = ["instagram_url", "is_active", "is_featured", "display_order"]
    list_filter   = ["is_active", "is_featured"]


@admin.register(FacebookPost)
class FacebookPostAdmin(admin.ModelAdmin):
    list_display  = ["facebook_url", "is_active", "is_featured", "display_order"]
    list_filter   = ["is_active", "is_featured"]


@admin.register(MediaEvent)
class MediaEventAdmin(admin.ModelAdmin):
    list_display  = ["title", "event_date", "is_active", "is_featured", "display_order"]
    list_filter   = ["is_active", "is_featured"]
    search_fields = ["title", "excerpt"]
    prepopulated_fields = {"slug": ("title",)}


@admin.register(GalleryImage)
class GalleryImageAdmin(admin.ModelAdmin):
    list_display  = ["caption", "is_active", "display_order"]
    list_filter   = ["is_active"]


class SpecialtySectionInline(admin.TabularInline):
    model = SpecialtySection
    extra = 0
    fields = ["heading", "intro", "items", "subsections", "is_active", "display_order"]


@admin.register(Specialty)
class SpecialtyAdmin(admin.ModelAdmin):
    list_display  = ["name", "parent", "is_published", "display_order"]
    list_filter   = ["is_published", "parent"]
    search_fields = ["name", "short_description"]
    prepopulated_fields = {"slug": ("name",)}
    inlines = [SpecialtySectionInline]


@admin.register(Treatment)
class TreatmentAdmin(admin.ModelAdmin):
    list_display  = ["name", "specialty", "kind", "is_active", "display_order"]
    list_filter   = ["kind", "is_active", "specialty"]
    search_fields = ["name", "summary"]


@admin.register(Blog)
class BlogAdmin(admin.ModelAdmin):
    list_display  = ["title", "specialty", "author_doctor", "is_published", "display_order"]
    list_filter   = ["is_published", "specialty"]
    search_fields = ["title", "excerpt", "body"]
    prepopulated_fields = {"slug": ("title",)}