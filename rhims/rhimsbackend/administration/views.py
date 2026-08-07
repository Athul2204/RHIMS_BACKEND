# administration/views.py
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated

from .models import StaffProfile, ReceptionistProfile, PharmacistProfile, GuestDoctorProfile, CommonReceptionistProfile, CommonPharmacistProfile, Procedure, AuditLog, HospitalSettings, Branch, ManagerBranchAccess
from doctor.models import DoctorProfile
from .serializers import (
    StaffProfileSerializer, ReceptionistProfileSerializer,
    PharmacistProfileSerializer, GuestDoctorProfileSerializer,
    CommonReceptionistProfileSerializer, CommonPharmacistProfileSerializer,
    DoctorProfileSerializer,
    ProcedureSerializer, AuditLogSerializer,
    HospitalSettingsSerializer, BranchSerializer,
    ManagerBranchAccessSerializer, ManagerBranchAccessGrantSerializer,
)
from authentication.permissions import IsAdminUser, IsReceptionist, IsPharmacist, IsAdminOrManager, IsAuthenticatedResolveManagerBranch
from authentication.throttling import SensitiveWriteThrottle
from authentication.utils import is_group_admin_user, get_user_branch

# ─── Audit logging ───────────────────────────────────────────────
# log_admin_action() and its branch-resolution helper now live in
# administration/audit.py, shared by every app (see that module's
# docstring). Imported under its original name here so none of the 20+
# call sites below needed to change.
from .audit import log_admin_action


# ─── Pagination ──────────────────────────────────────────────────
class StandardPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100


# ─── Base views ──────────────────────────────────────────────────
class AdminOnlyView(APIView):
    permission_classes = [IsAdminUser]

    # SECURITY: rate-limit mutating admin actions (staff/profile creation,
    # deactivate/reactivate, hospital settings updates, etc.) — see
    # authentication/throttling.py. GET requests are unaffected; only
    # POST/PUT/PATCH/DELETE count against "admin_write" in
    # settings.DEFAULT_THROTTLE_RATES.
    throttle_classes = [SensitiveWriteThrottle]
    throttle_scope = "admin_write"


class BranchListView(AdminOnlyView):
    """
    GET  /api/administration/branches/  — list branches.
        Group admins see every branch (optionally `?show_inactive=true` to
        include deactivated ones). An ordinary branch-scoped admin only ever
        sees their own single branch — there is no cross-branch visibility
        to leak here even for a read-only list.
    POST /api/administration/branches/  — create a new branch.
        Group-admin only: creating a branch is a whole-organization action,
        not something a single branch's admin should be able to do.
    """

    def get(self, request):
        if is_group_admin_user(request.user):
            qs = Branch.objects.all()
            if request.query_params.get("show_inactive", "").lower() != "true":
                qs = qs.filter(is_active=True)
        else:
            branch = get_user_branch(request.user)
            qs = Branch.objects.filter(pk=branch.pk) if branch else Branch.objects.none()

        return Response(BranchSerializer(qs, many=True).data)

    def post(self, request):
        if not is_group_admin_user(request.user):
            return Response(
                {"errors": {"detail": "Only a group admin can create a new branch."}},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = BranchSerializer(data=request.data)
        if serializer.is_valid():
            branch = serializer.save()
            log_admin_action(request, "CREATE", "Administration", branch, label=f"Branch {branch.code}")
            return Response(BranchSerializer(branch).data, status=status.HTTP_201_CREATED)
        return Response({"errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)


class BranchDetailView(AdminOnlyView):
    """
    GET   /api/administration/branches/<pk>/  — any admin can view their own
        branch; a group admin can view any branch.
    PATCH /api/administration/branches/<pk>/  — group-admin only. Branch
        identity/config (name, code, active flag) is org-wide configuration,
        not something a branch's own admin should be able to change —
        unlike ordinary staff/profile records, which branch admins do own.
    """

    def _get_branch(self, request, pk):
        if is_group_admin_user(request.user):
            return get_object_or_404(Branch, pk=pk)
        branch = get_user_branch(request.user)
        if branch is None or branch.pk != pk:
            from django.http import Http404
            raise Http404
        return branch

    def get(self, request, pk):
        return Response(BranchSerializer(self._get_branch(request, pk)).data)

    def patch(self, request, pk):
        if not is_group_admin_user(request.user):
            return Response(
                {"errors": {"detail": "Only a group admin can edit branch details."}},
                status=status.HTTP_403_FORBIDDEN,
            )

        branch = get_object_or_404(Branch, pk=pk)
        serializer = BranchSerializer(branch, data=request.data, partial=True)
        if serializer.is_valid():
            branch = serializer.save()
            log_admin_action(request, "UPDATE", "Administration", branch, label=f"Branch {branch.code}")
            return Response(BranchSerializer(branch).data)
        return Response({"errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)


class ListCreateView(AdminOnlyView):
    model = None
    serializer_class = None
    order_field = "pk"

    # Queryset path from `model` to its owning Branch, used to scope every
    # list/create endpoint to the caller's own branch (bypassed for
    # is_group_admin). None means the model isn't branch-scoped at all.
    # Override on subclasses whose model reaches Branch indirectly, e.g.
    # "staff__branch" for ReceptionistProfile/PharmacistProfile.
    branch_lookup = "branch"

    # Whether POST/create must resolve to a real branch. False only for
    # models where branch is genuinely optional (GuestDoctorProfile) —
    # everywhere else a missing branch is an error, not a silent null.
    branch_required_on_write = True

    # Field names that must never be settable through this generic
    # create/update path even by an otherwise-permitted Admin — e.g.
    # StaffProfile.is_group_admin, which grants superuser. Empty by
    # default; subclasses opt in. Stripped unless bypass_check(user) is
    # True (defaults to group-admin-only in strip_privileged_fields).
    protected_fields = []

    def get_queryset(self):
        qs = self.model.objects.all().order_by(f"-{self.order_field}")
        if self.branch_lookup:
            from authentication.utils import scope_queryset_to_branch
            branch_id = self.request.query_params.get("branch")
            qs = scope_queryset_to_branch(qs, self.request.user, branch_field=self.branch_lookup, branch_id=branch_id)
        return qs

    def get(self, request):
        qs = self.get_queryset()
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(
            self.serializer_class(page, many=True, context={"request": request}).data
        )

    def post(self, request):
        data = request.data

        if self.protected_fields:
            from authentication.utils import strip_privileged_fields
            data = strip_privileged_fields(data, self.protected_fields, request.user)

        # SECURITY: only models with a *direct* branch field get
        # server-side branch enforcement here (branch_lookup == "branch").
        # Indirect ones (e.g. "staff__branch" for Receptionist/Pharmacist
        # profiles) derive their branch from the staff_id they're linked
        # to instead — there's no "branch" field on those serializers to
        # inject into.
        if self.branch_lookup == "branch":
            from authentication.utils import resolve_branch_for_write
            branch, error = resolve_branch_for_write(request, required=self.branch_required_on_write)
            if error:
                return error
            data = data.copy()
            if branch is not None:
                # branch is either a Branch instance (branch admin) or an
                # id the group admin supplied — both work as the FK value.
                data["branch"] = getattr(branch, "pk", branch)
            else:
                data.pop("branch", None)

        serializer = self.serializer_class(data=data, context={"request": request})
        if serializer.is_valid():
            instance = serializer.save()
            log_admin_action(
                request, "CREATE", "Administration", instance,
                label=getattr(self, "label", None) or self.model.__name__,
            )
            return Response(
                {
                    "message": f"{self.model.__name__} created successfully.",
                    "id": instance.pk,
                    "data": serializer.data,
                },
                status=status.HTTP_201_CREATED,
            )
        return Response({"errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)


class RetrieveUpdateView(AdminOnlyView):
    """
    Retrieve, full-update (PUT), or partial-update (PATCH).
    No hard-delete — use the dedicated DeactivateView for staff.
    """
    serializer_class = None

    # Same purpose as ListCreateView.branch_lookup — scopes retrieve/update
    # so a branch Admin gets a 404 (not another branch's data) on a pk that
    # belongs to a different branch. Bypassed for is_group_admin.
    branch_lookup = "branch"

    # See ListCreateView.protected_fields — same purpose, applied on
    # PUT/PATCH here. Subclasses opt in (e.g. StaffDetailView).
    protected_fields = []

    def get_queryset(self):
        qs = self.serializer_class.Meta.model.objects.all()
        if self.branch_lookup:
            from authentication.utils import scope_queryset_to_branch
            qs = scope_queryset_to_branch(qs, self.request.user, branch_field=self.branch_lookup)
        return qs

    def _obj(self, pk):
        return get_object_or_404(self.get_queryset(), pk=pk)

    def _clean_branch_reassignment(self, request, data):
        # SECURITY: a branch admin can never move an existing record to a
        # different branch by PUT/PATCHing "branch" — silently drop it so
        # the update just keeps the object's current branch. Only the
        # group admin may reassign a record's branch.
        if self.branch_lookup == "branch" and "branch" in data:
            from authentication.utils import is_group_admin_user
            if not is_group_admin_user(request.user):
                data = data.copy()
                data.pop("branch", None)
        return data

    def _clean_protected_fields(self, request, data):
        if self.protected_fields:
            from authentication.utils import strip_privileged_fields
            data = strip_privileged_fields(data, self.protected_fields, request.user)
        return data

    def get(self, request, pk):
        return Response(self.serializer_class(self._obj(pk), context={"request": request}).data)

    def put(self, request, pk):
        data = self._clean_branch_reassignment(request, request.data)
        data = self._clean_protected_fields(request, data)
        s = self.serializer_class(self._obj(pk), data=data, context={"request": request})
        if s.is_valid():
            instance = s.save()
            log_admin_action(request, "UPDATE", "Administration", instance)
            return Response({"message": "Updated successfully.", "data": s.data})
        return Response({"errors": s.errors}, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        data = self._clean_branch_reassignment(request, request.data)
        data = self._clean_protected_fields(request, data)
        s = self.serializer_class(self._obj(pk), data=data, partial=True, context={"request": request})
        if s.is_valid():
            instance = s.save()
            log_admin_action(request, "UPDATE", "Administration", instance)
            return Response({"message": "Updated successfully.", "data": s.data})
        return Response({"errors": s.errors}, status=status.HTTP_400_BAD_REQUEST)


# ─── Admin Dashboard ─────────────────────────────────────────────
class AdminDashboardView(AdminOnlyView):
    def get(self, request):
        from reception.models import Patient
        from pharmacist.models import Medicine
        from authentication.utils import scope_queryset_to_branch, is_group_admin_user, get_user_branch

        branch_id = request.query_params.get("branch")
        staff_qs        = scope_queryset_to_branch(StaffProfile.objects.all(), request.user, branch_id=branch_id)
        guest_qs        = scope_queryset_to_branch(GuestDoctorProfile.objects.all(), request.user, branch_id=branch_id)
        patient_qs      = scope_queryset_to_branch(Patient.objects.all(), request.user, branch_id=branch_id)
        medicine_qs     = scope_queryset_to_branch(Medicine.objects.all(), request.user, branch_id=branch_id)
        procedure_qs    = scope_queryset_to_branch(Procedure.objects.all(), request.user, branch_id=branch_id)

        # AuditLog has no branch column of its own — best-effort scope it
        # through the actor's own branch (system-attributed entries, whose
        # actor has no StaffProfile, are only visible to the group admin).
        # For the group admin, an explicit ?branch= selector narrows this
        # the same way; with none given they see every branch's log.
        audit_qs = AuditLog.objects.all()
        if not is_group_admin_user(request.user):
            branch = get_user_branch(request.user)
            audit_qs = audit_qs.filter(user__staff_profile__branch=branch) if branch else audit_qs.none()
        elif branch_id:
            audit_qs = audit_qs.filter(user__staff_profile__branch_id=branch_id)

        recent_logs = audit_qs.select_related("user").order_by("-timestamp")[:10]

        return Response({
            "staff": {
                "total":   staff_qs.count(),
                "active":  staff_qs.filter(is_active=True).count(),
                "inactive": staff_qs.filter(is_active=False).count(),
            },
            # Count each role directly from StaffProfile — no cross-app imports
            # needed, all role strings live in StaffProfile.ROLE_CHOICES.
            "by_role": {
                "receptionists":   staff_qs.filter(role="Receptionist").count(),
                "pharmacists":     staff_qs.filter(role="Pharmacist").count(),
                "doctors":         staff_qs.filter(role="Doctor").count(),
                "lab_technicians": staff_qs.filter(role="Lab Technician").count(),
            },
            "guest_doctors": {
                "total":  guest_qs.count(),
                "active": guest_qs.filter(is_active=True).count(),
            },
            "patients": {
                "total": patient_qs.count(),
            },
            "inventory": {
                "total_medicines": medicine_qs.count(),
                "active_medicines": medicine_qs.filter(is_active=True).count(),
            },
            "procedures": {
                "active": procedure_qs.filter(is_active=True).count(),
                "total":  procedure_qs.count(),
            },
            # Expose total audit log count separately so the frontend
            # "Audit Events" stat card shows the real total, not the length
            # of the 10-item recent_audit_logs preview (which is always ≤ 10).
            "audit_total": audit_qs.count(),
            "recent_audit_logs": AuditLogSerializer(recent_logs, many=True).data,
        })


# ─── Staff ───────────────────────────────────────────────────────
class StaffListView(ListCreateView):
    """
    GET  /api/administration/staff/              → active staff only (default)
    GET  /api/administration/staff/?all=true     → all staff including inactive
    GET  /api/administration/staff/?role=Doctor  → filter by role
    POST /api/administration/staff/              → create new staff member
    """
    model = StaffProfile
    serializer_class = StaffProfileSerializer

    # SECURITY: belt-and-suspenders alongside StaffProfileSerializer's
    # is_group_admin read_only_fields entry — see that Meta comment. Only
    # StaffPromoteGroupAdminView (gated to existing group admins) may grant
    # this; no ordinary create/update path through this view ever should.
    protected_fields = ["is_group_admin"]

    def get_queryset(self):
        from authentication.utils import scope_queryset_to_branch

        qs = StaffProfile.objects.select_related("user").order_by("-id")
        branch_id = self.request.query_params.get("branch")
        qs = scope_queryset_to_branch(qs, self.request.user, branch_id=branch_id)

        if self.request.query_params.get("all", "").lower() != "true":
            qs = qs.filter(is_active=True)

        role = self.request.query_params.get("role")
        if role:
            qs = qs.filter(role__iexact=role)

        search = self.request.query_params.get("search")
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(user__first_name__icontains=search) |
                Q(user__last_name__icontains=search) |
                Q(user__username__icontains=search) |
                Q(staff_code__icontains=search)
            )

        return qs


class StaffDetailView(RetrieveUpdateView):
    serializer_class = StaffProfileSerializer
    # SECURITY: see ListCreateView.protected_fields / StaffListView comment —
    # is_group_admin must never be settable through PUT/PATCH here.
    protected_fields = ["is_group_admin"]


class StaffDeactivateView(AdminOnlyView):
    def post(self, request, pk):
        from authentication.utils import scope_queryset_to_branch
        staff = get_object_or_404(scope_queryset_to_branch(StaffProfile.objects.all(), request.user), pk=pk)

        if not staff.is_active:
            return Response(
                {"message": f"{staff.staff_code} is already inactive."},
                status=status.HTTP_200_OK,
            )

        staff.is_active = False
        staff.save(update_fields=["is_active", "updated_at"])

        log_admin_action(
            request, "DEACTIVATE", "Administration", staff,
            label=f"Staff {staff.staff_code} ({staff.role})",
        )

        return Response(
            {"message": f"Staff member {staff.staff_code} has been deactivated."},
            status=status.HTTP_200_OK,
        )


class StaffReactivateView(AdminOnlyView):
    def post(self, request, pk):
        from authentication.utils import scope_queryset_to_branch
        staff = get_object_or_404(scope_queryset_to_branch(StaffProfile.objects.all(), request.user), pk=pk)

        if staff.is_active:
            return Response(
                {"message": f"{staff.staff_code} is already active."},
                status=status.HTTP_200_OK,
            )

        staff.is_active = True
        staff.save(update_fields=["is_active", "updated_at"])

        log_admin_action(
            request, "REACTIVATE", "Administration", staff,
            label=f"Staff {staff.staff_code} ({staff.role})",
        )

        return Response(
            {"message": f"Staff member {staff.staff_code} has been reactivated."},
            status=status.HTTP_200_OK,
        )


class StaffPromoteGroupAdminView(AdminOnlyView):
    """
    POST /api/administration/staff/<pk>/promote-group-admin/
    Grants is_group_admin=True (and therefore is_superuser, via
    StaffProfile.save()) to another staff member. Gated to existing group
    admins only — this is the dedicated, audited path for what used to be
    an accidental side effect of creating a staff row with role='Admin'.
    """

    def post(self, request, pk):
        from authentication.utils import is_group_admin_user

        if not is_group_admin_user(request.user):
            return Response(
                {"errors": {"detail": "Only an existing group admin can promote another account."}},
                status=status.HTTP_403_FORBIDDEN,
            )

        staff = get_object_or_404(StaffProfile, pk=pk)
        if staff.is_group_admin:
            return Response({"message": f"{staff.staff_code} is already a group admin."}, status=status.HTTP_200_OK)

        staff.is_group_admin = True
        staff.save(update_fields=["is_group_admin", "updated_at"])

        log_admin_action(
            request, "UPDATE", "Administration", staff,
            label=f"Staff {staff.staff_code} promoted to group admin",
        )

        return Response(
            {"message": f"{staff.staff_code} has been promoted to group admin."},
            status=status.HTTP_200_OK,
        )


# ─── Manager branch access (group-admin only) ─────────────────────
class ManagerBranchAccessListView(AdminOnlyView):
    """
    GET  /api/administration/manager-branch-access/
        List branch-access grants. Group-admin only — a branch-scoped
        admin has no business seeing another branch's manager grants,
        and there's no meaningful "own branch" scoping for this model
        (a grant belongs to the *target* branch, not the admin's own).
        Optional ?manager=<staff_pk> narrows to one manager, which is how
        the frontend's "Branch Access" control on the Staff/Manager page
        loads a single manager's current grants.
    POST /api/administration/manager-branch-access/
        Grant a manager access to an additional branch. Body:
        {"manager": <staff_pk>, "branch": <branch_pk>}. `granted_by` is
        always taken from request.user, never client-supplied.
    """

    def get(self, request):
        if not is_group_admin_user(request.user):
            return Response(
                {"errors": {"detail": "Only a group admin can view branch access grants."}},
                status=status.HTTP_403_FORBIDDEN,
            )

        qs = ManagerBranchAccess.objects.select_related("manager", "manager__user", "branch", "granted_by")
        manager_id = request.query_params.get("manager")
        if manager_id:
            qs = qs.filter(manager_id=manager_id)

        return Response(ManagerBranchAccessSerializer(qs, many=True).data)

    def post(self, request):
        if not is_group_admin_user(request.user):
            return Response(
                {"errors": {"detail": "Only a group admin can grant branch access."}},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = ManagerBranchAccessGrantSerializer(data=request.data)
        if serializer.is_valid():
            grant = serializer.save(granted_by=request.user)
            log_admin_action(
                request, "CREATE", "Administration", grant,
                label=f"Granted {grant.manager.staff_code} access to branch {grant.branch.code}",
            )
            return Response(ManagerBranchAccessSerializer(grant).data, status=status.HTTP_201_CREATED)
        return Response({"errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)


class ManagerBranchAccessRevokeView(AdminOnlyView):
    """
    DELETE /api/administration/manager-branch-access/<pk>/revoke/
    Revokes one branch-access grant. Group-admin only. Never touches the
    manager's own home branch (StaffProfile.branch) — that isn't
    represented by a ManagerBranchAccess row at all, so there is nothing
    here that could accidentally lock a manager out of their home branch.
    """

    def delete(self, request, pk):
        if not is_group_admin_user(request.user):
            return Response(
                {"errors": {"detail": "Only a group admin can revoke branch access."}},
                status=status.HTTP_403_FORBIDDEN,
            )

        grant = get_object_or_404(ManagerBranchAccess, pk=pk)
        label = f"Revoked {grant.manager.staff_code} access to branch {grant.branch.code}"
        grant.delete()
        log_admin_action(request, "DELETE", "Administration", grant, label=label)

        return Response({"message": label}, status=status.HTTP_200_OK)


# ─── Receptionists ───────────────────────────────────────────────
class ReceptionistListView(ListCreateView):
    model = ReceptionistProfile
    serializer_class = ReceptionistProfileSerializer
    order_field = "profile_id"
    branch_lookup = "staff__branch"  # ReceptionistProfile has no branch of its own


class ReceptionistDetailView(RetrieveUpdateView):
    serializer_class = ReceptionistProfileSerializer
    branch_lookup = "staff__branch"


# ─── Pharmacists ─────────────────────────────────────────────────
class PharmacistListView(ListCreateView):
    model = PharmacistProfile
    serializer_class = PharmacistProfileSerializer
    order_field = "profile_id"
    branch_lookup = "staff__branch"


class PharmacistDetailView(RetrieveUpdateView):
    serializer_class = PharmacistProfileSerializer
    branch_lookup = "staff__branch"


# ─── Doctors ─────────────────────────────────────────────────────
class DoctorListView(ListCreateView):
    """
    GET  /api/administration/doctors/           → active doctors only (default)
    GET  /api/administration/doctors/?all=true  → include inactive (deactivated staff)
    GET  /api/administration/doctors/?search=   → search by name / staff code / reg. no.
    POST /api/administration/doctors/           → attach a DoctorProfile to an existing
                                                   Doctor-role staff member (staff_id).
                                                   Normally unused — DoctorProfile rows
                                                   are auto-created by a signal whenever
                                                   a Doctor-role StaffProfile is created.
    """
    model = DoctorProfile
    serializer_class = DoctorProfileSerializer
    order_field = "profile_id"
    branch_lookup = "staff__branch"

    def get_queryset(self):
        from authentication.utils import scope_queryset_to_branch

        qs = DoctorProfile.objects.select_related("staff", "staff__user", "specialty").order_by("-profile_id")
        branch_id = self.request.query_params.get("branch")
        qs = scope_queryset_to_branch(qs, self.request.user, branch_field="staff__branch", branch_id=branch_id)

        if self.request.query_params.get("all", "").lower() != "true":
            qs = qs.filter(staff__is_active=True)

        search = self.request.query_params.get("search")
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(staff__user__first_name__icontains=search) |
                Q(staff__user__last_name__icontains=search) |
                Q(staff__staff_code__icontains=search) |
                Q(specialty__name__icontains=search) |
                Q(registration_number__icontains=search)
            )

        return qs


class DoctorDetailView(RetrieveUpdateView):
    serializer_class = DoctorProfileSerializer
    branch_lookup = "staff__branch"


def _guest_doctor_queryset(user):
    """
    Shared branch-scoped queryset for guest-doctor detail/deactivate/
    reactivate lookups — same nullable-branch rule as GuestDoctorListView.
    """
    from authentication.utils import is_group_admin_user, get_user_branch
    from django.db.models import Q

    qs = GuestDoctorProfile.objects.all()
    if is_group_admin_user(user):
        return qs
    branch = get_user_branch(user)
    return qs.filter(Q(branch=branch) | Q(branch__isnull=True)) if branch else qs.filter(branch__isnull=True)


# ─── Guest Doctors ───────────────────────────────────────────────
class GuestDoctorListView(ListCreateView):
    """
    GET  /api/administration/guest-doctors/           → active guest doctors (default)
    GET  /api/administration/guest-doctors/?all=true  → include inactive
    GET  /api/administration/guest-doctors/?search=   → search by name / specialization
    POST /api/administration/guest-doctors/           → register a new guest doctor
    """
    model = GuestDoctorProfile
    serializer_class = GuestDoctorProfileSerializer
    order_field = "guest_doctor_id"
    branch_required_on_write = False  # branch is optional/nullable for a cross-branch guest doctor

    def get_queryset(self):
        from authentication.utils import is_group_admin_user, get_user_branch

        qs = GuestDoctorProfile.objects.all().order_by("-guest_doctor_id")

        # Unlike most branch-scoped models, GuestDoctorProfile.branch is
        # nullable — a reference-only guest doctor (branch=None) covers
        # multiple branches, so it should stay visible to every branch's
        # admin, not just the group admin. A branch Admin therefore sees
        # their own branch's guest doctors *plus* the cross-branch ones.
        if not is_group_admin_user(self.request.user):
            branch = get_user_branch(self.request.user)
            from django.db.models import Q
            qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True)) if branch else qs.filter(branch__isnull=True)
        else:
            # Group admin: optional ?branch= selector from the frontend
            # narrows to one branch's guest doctors + the cross-branch
            # ones; with no selector they see everything, as elsewhere.
            branch_id = self.request.query_params.get("branch")
            if branch_id:
                from django.db.models import Q
                qs = qs.filter(Q(branch_id=branch_id) | Q(branch__isnull=True))

        if self.request.query_params.get("all", "").lower() != "true":
            qs = qs.filter(is_active=True)

        search = self.request.query_params.get("search")
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(full_name__icontains=search) |
                Q(specialization__icontains=search) |
                Q(department__icontains=search) |
                Q(guest_code__icontains=search)
            )

        return qs


class GuestDoctorDetailView(AdminOnlyView):
    """
    GET   /api/administration/guest-doctors/<pk>/  → fetch one guest doctor
    PATCH /api/administration/guest-doctors/<pk>/  → partial update
    PUT   /api/administration/guest-doctors/<pk>/  → full update
    """

    def _obj(self, pk):
        return get_object_or_404(_guest_doctor_queryset(self.request.user), pk=pk)

    def _clean_branch_reassignment(self, request, data):
        if "branch" in data:
            from authentication.utils import is_group_admin_user
            if not is_group_admin_user(request.user):
                data = data.copy()
                data.pop("branch", None)
        return data

    def get(self, request, pk):
        return Response(GuestDoctorProfileSerializer(self._obj(pk), context={"request": request}).data)

    def put(self, request, pk):
        data = self._clean_branch_reassignment(request, request.data)
        s = GuestDoctorProfileSerializer(self._obj(pk), data=data, context={"request": request})
        if s.is_valid():
            s.save()
            return Response({"message": "Guest doctor updated.", "data": s.data})
        return Response({"errors": s.errors}, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        data = self._clean_branch_reassignment(request, request.data)
        s = GuestDoctorProfileSerializer(self._obj(pk), data=data, partial=True, context={"request": request})
        if s.is_valid():
            s.save()
            return Response({"message": "Guest doctor updated.", "data": s.data})
        return Response({"errors": s.errors}, status=status.HTTP_400_BAD_REQUEST)


class GuestDoctorDeactivateView(AdminOnlyView):
    """
    POST /api/administration/guest-doctors/<pk>/deactivate/
    Soft-deactivates a guest doctor (sets is_active=False).
    All existing bills referencing this guest remain intact.
    """

    def post(self, request, pk):
        guest = get_object_or_404(_guest_doctor_queryset(request.user), pk=pk)

        if not guest.is_active:
            return Response(
                {"message": f"{guest.guest_code} is already inactive."},
                status=status.HTTP_200_OK,
            )

        guest.is_active = False
        guest.save(update_fields=["is_active", "updated_at"])

        log_admin_action(
            request, "DEACTIVATE", "Administration", guest,
            label=f"Guest doctor {guest.guest_code} ({guest.full_name})",
        )

        return Response(
            {"message": f"Guest doctor {guest.guest_code} has been deactivated."},
            status=status.HTTP_200_OK,
        )


class GuestDoctorReactivateView(AdminOnlyView):
    """
    POST /api/administration/guest-doctors/<pk>/reactivate/
    Re-enables a previously deactivated guest doctor.
    """

    def post(self, request, pk):
        guest = get_object_or_404(_guest_doctor_queryset(request.user), pk=pk)

        if guest.is_active:
            return Response(
                {"message": f"{guest.guest_code} is already active."},
                status=status.HTTP_200_OK,
            )

        guest.is_active = True
        guest.save(update_fields=["is_active", "updated_at"])

        log_admin_action(
            request, "REACTIVATE", "Administration", guest,
            label=f"Guest doctor {guest.guest_code} ({guest.full_name})",
        )

        return Response(
            {"message": f"Guest doctor {guest.guest_code} has been reactivated."},
            status=status.HTTP_200_OK,
        )


# ─── Common Staff (Common Receptionist / Common Pharmacist) ───────
# Two separate single-role account types, each admin-managed like a Guest
# Doctor. The four base classes below hold the shared list/detail/
# deactivate/reactivate logic (parametrized by `model` + `serializer_class`
# + `label`); the concrete pairs underneath just plug in their model.

class _CommonStaffListViewBase(ListCreateView):
    model = None
    serializer_class = None
    order_field = "pk"
    label = "Common staff"

    def get_queryset(self):
        from authentication.utils import scope_queryset_to_branch

        qs = self.model.objects.all().order_by(f"-{self.order_field}")
        branch_id = self.request.query_params.get("branch")
        qs = scope_queryset_to_branch(qs, self.request.user, branch_id=branch_id)

        if self.request.query_params.get("all", "").lower() != "true":
            qs = qs.filter(is_active=True)

        search = self.request.query_params.get("search")
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(full_name__icontains=search) |
                Q(common_code__icontains=search) |
                Q(user__username__icontains=search)
            )

        return qs


class _CommonStaffDetailViewBase(AdminOnlyView):
    model = None
    serializer_class = None
    label = "Common staff"

    def _obj(self, pk):
        from authentication.utils import scope_queryset_to_branch
        return get_object_or_404(scope_queryset_to_branch(self.model.objects.all(), self.request.user), pk=pk)

    def _clean_branch_reassignment(self, request, data):
        if "branch" in data:
            from authentication.utils import is_group_admin_user
            if not is_group_admin_user(request.user):
                data = data.copy()
                data.pop("branch", None)
        return data

    def get(self, request, pk):
        return Response(self.serializer_class(self._obj(pk), context={"request": request}).data)

    def put(self, request, pk):
        data = self._clean_branch_reassignment(request, request.data)
        s = self.serializer_class(self._obj(pk), data=data, context={"request": request})
        if s.is_valid():
            instance = s.save()
            log_admin_action(request, "UPDATE", "Administration", instance, label=self.label)
            return Response({"message": f"{self.label} updated.", "data": s.data})
        return Response({"errors": s.errors}, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        data = self._clean_branch_reassignment(request, request.data)
        s = self.serializer_class(self._obj(pk), data=data, partial=True, context={"request": request})
        if s.is_valid():
            instance = s.save()
            log_admin_action(request, "UPDATE", "Administration", instance, label=self.label)
            return Response({"message": f"{self.label} updated.", "data": s.data})
        return Response({"errors": s.errors}, status=status.HTTP_400_BAD_REQUEST)


class _CommonStaffDeactivateViewBase(AdminOnlyView):
    model = None
    label = "Common staff"

    def post(self, request, pk):
        from authentication.utils import scope_queryset_to_branch
        staff = get_object_or_404(scope_queryset_to_branch(self.model.objects.all(), request.user), pk=pk)

        if not staff.is_active:
            return Response(
                {"message": f"{staff.common_code} is already inactive."},
                status=status.HTTP_200_OK,
            )

        staff.is_active = False
        staff.save(update_fields=["is_active", "updated_at"])

        log_admin_action(
            request, "DEACTIVATE", "Administration", staff,
            label=f"{self.label} {staff.common_code} ({staff.full_name})",
        )

        return Response(
            {"message": f"{self.label} {staff.common_code} has been deactivated."},
            status=status.HTTP_200_OK,
        )


class _CommonStaffReactivateViewBase(AdminOnlyView):
    model = None
    label = "Common staff"

    def post(self, request, pk):
        from authentication.utils import scope_queryset_to_branch
        staff = get_object_or_404(scope_queryset_to_branch(self.model.objects.all(), request.user), pk=pk)

        if staff.is_active:
            return Response(
                {"message": f"{staff.common_code} is already active."},
                status=status.HTTP_200_OK,
            )

        staff.is_active = True
        staff.save(update_fields=["is_active", "updated_at"])

        log_admin_action(
            request, "REACTIVATE", "Administration", staff,
            label=f"{self.label} {staff.common_code} ({staff.full_name})",
        )

        return Response(
            {"message": f"{self.label} {staff.common_code} has been reactivated."},
            status=status.HTTP_200_OK,
        )


# ── Common Receptionist ──
class CommonReceptionistListView(_CommonStaffListViewBase):
    """
    GET  /api/administration/common-receptionists/           → active (default)
    GET  /api/administration/common-receptionists/?all=true  → include inactive
    GET  /api/administration/common-receptionists/?search=   → search by name / code
    POST /api/administration/common-receptionists/           → register a new common receptionist
    """
    model = CommonReceptionistProfile
    serializer_class = CommonReceptionistProfileSerializer
    order_field = "common_receptionist_id"
    label = "Common receptionist"


class CommonReceptionistDetailView(_CommonStaffDetailViewBase):
    model = CommonReceptionistProfile
    serializer_class = CommonReceptionistProfileSerializer
    label = "Common receptionist"


class CommonReceptionistDeactivateView(_CommonStaffDeactivateViewBase):
    model = CommonReceptionistProfile
    label = "Common receptionist"


class CommonReceptionistReactivateView(_CommonStaffReactivateViewBase):
    model = CommonReceptionistProfile
    label = "Common receptionist"


# ── Common Pharmacist ──
class CommonPharmacistListView(_CommonStaffListViewBase):
    """
    GET  /api/administration/common-pharmacists/           → active (default)
    GET  /api/administration/common-pharmacists/?all=true  → include inactive
    GET  /api/administration/common-pharmacists/?search=   → search by name / code
    POST /api/administration/common-pharmacists/           → register a new common pharmacist
    """
    model = CommonPharmacistProfile
    serializer_class = CommonPharmacistProfileSerializer
    order_field = "common_pharmacist_id"
    label = "Common pharmacist"


class CommonPharmacistDetailView(_CommonStaffDetailViewBase):
    model = CommonPharmacistProfile
    serializer_class = CommonPharmacistProfileSerializer
    label = "Common pharmacist"


class CommonPharmacistDeactivateView(_CommonStaffDeactivateViewBase):
    model = CommonPharmacistProfile
    label = "Common pharmacist"


class CommonPharmacistReactivateView(_CommonStaffReactivateViewBase):
    model = CommonPharmacistProfile
    label = "Common pharmacist"


# ─── Procedures ──────────────────────────────────────────────────
class ProcedureListView(APIView):
    """
    GET:  Any authenticated user (reception and pharmacy need it for billing).
    POST: Admin or Manager (Manager has the same full catalog access as Admin).
    """

    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAuthenticatedResolveManagerBranch()]
        return [IsAdminOrManager()]

    def get(self, request):
        from authentication.utils import scope_queryset_to_branch

        qs = Procedure.objects.order_by("name")
        qs = scope_queryset_to_branch(qs, request.user, branch_id=request.query_params.get("branch"))

        # Never surface pharmacist "instant procedure" one-offs in the
        # picker/catalog - their charge is patient-specific, not a real
        # standard price. This is independent of is_active so that flag
        # remains a clean, admin/manager-only toggle for real catalog procedures.
        if request.query_params.get("include_instant", "").lower() != "true":
            qs = qs.filter(created_by_pharmacist=False)

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(name__icontains=search)

        from authentication.permissions import _get_role
        if _get_role(request.user) not in ("admin", "manager"):
            qs = qs.filter(is_active=True)
        elif request.query_params.get("include_inactive", "").lower() != "true":
            qs = qs.filter(is_active=True)

        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(ProcedureSerializer(page, many=True, context={"request": request}).data)

    def post(self, request):
        from authentication.utils import resolve_branch_for_write

        branch, error = resolve_branch_for_write(request, required=True)
        if error:
            return error
        data = request.data.copy()
        data["branch"] = getattr(branch, "pk", branch)

        s = ProcedureSerializer(data=data, context={"request": request})
        if s.is_valid():
            instance = s.save()
            log_admin_action(request, "CREATE", "Administration", instance, label=f"Procedure: {instance.name}")
            return Response(
                {"message": "Procedure created successfully.", "id": instance.pk, "data": s.data},
                status=status.HTTP_201_CREATED,
            )
        return Response({"errors": s.errors}, status=status.HTTP_400_BAD_REQUEST)


class ProcedureDetailView(APIView):
    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAuthenticatedResolveManagerBranch()]
        return [IsAdminOrManager()]

    def _obj(self, pk):
        from authentication.utils import scope_queryset_to_branch
        return get_object_or_404(scope_queryset_to_branch(Procedure.objects.all(), self.request.user), pk=pk)

    def get(self, request, pk):
        return Response(ProcedureSerializer(self._obj(pk), context={"request": request}).data)

    def _clean_branch_reassignment(self, request, data):
        if "branch" in data:
            from authentication.utils import is_group_admin_user
            if not is_group_admin_user(request.user):
                data = data.copy()
                data.pop("branch", None)
        return data

    def put(self, request, pk):
        data = self._clean_branch_reassignment(request, request.data)
        s = ProcedureSerializer(self._obj(pk), data=data, context={"request": request})
        if s.is_valid():
            instance = s.save()
            log_admin_action(request, "UPDATE", "Administration", instance, label=f"Procedure: {instance.name}")
            return Response({"message": "Procedure updated.", "data": s.data})
        return Response({"errors": s.errors}, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, pk):
        data = self._clean_branch_reassignment(request, request.data)
        s = ProcedureSerializer(self._obj(pk), data=data, partial=True, context={"request": request})
        if s.is_valid():
            instance = s.save()
            log_admin_action(request, "UPDATE", "Administration", instance, label=f"Procedure: {instance.name}")
            return Response({"message": "Procedure updated.", "data": s.data})
        return Response({"errors": s.errors}, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        obj = self._obj(pk)
        if not obj.is_active:
            return Response(
                {"message": "Procedure is already inactive."},
                status=status.HTTP_200_OK,
            )
        obj.is_active = False
        obj.save(update_fields=["is_active", "updated_at"])
        log_admin_action(request, "DEACTIVATE", "Administration", obj, label=f"Procedure: {obj.name}")
        return Response(
            {"message": f"Procedure '{obj.name}' has been deactivated."},
            status=status.HTTP_200_OK,
        )


# ─── Audit Log ───────────────────────────────────────────────────
class AuditLogListView(AdminOnlyView):
    def get(self, request):
        # SECURITY: previously unscoped — any user with role=='Admin' (not
        # just a group admin) could read every branch's audit trail. An
        # ordinary branch admin is now confined to log rows attributed to
        # their own branch (see _resolve_log_branch); entries with no
        # resolvable branch (group-admin-only actions) are group-admin-only
        # too. A group admin sees everything, optionally narrowed with the
        # same ?branch= drill-down used elsewhere.
        #
        # Secondary "-log_id" tiebreak (on top of "-timestamp") makes the
        # ordering fully deterministic even if two rows share a timestamp —
        # required so the serial_number annotation below always lines up
        # with the order rows are actually returned in, on every page.
        qs = AuditLog.objects.select_related("user", "branch").order_by("-timestamp", "-log_id")

        if is_group_admin_user(request.user):
            if branch_id := request.query_params.get("branch"):
                qs = qs.filter(branch_id=branch_id)
        else:
            branch = get_user_branch(request.user)
            qs = qs.filter(branch=branch) if branch else qs.none()

        if module := request.query_params.get("module"):
            qs = qs.filter(module__iexact=module)

        if action := request.query_params.get("action"):
            qs = qs.filter(action__iexact=action)

        if username := request.query_params.get("user"):
            qs = qs.filter(user__username__icontains=username)

        # Free-text search box on the frontend (placeholder: "User,
        # description…") — was silently ignored before since no filter
        # here ever read it, so typing into it did nothing. Matches
        # against username/name, description text, and IP address so an
        # admin can also just paste an IP to pull up everything from it.
        if search := request.query_params.get("search"):
            from django.db.models import Q
            qs = qs.filter(
                Q(user__username__icontains=search) |
                Q(user__first_name__icontains=search) |
                Q(user__last_name__icontains=search) |
                Q(description__icontains=search) |
                Q(ip_address__icontains=search)
            )

        # Row number over the full filtered/branch-scoped result set. The
        # window's own order_by is intentionally the *opposite* of the
        # display order above: rows are still shown newest-first, but the
        # oldest matching entry is numbered 1 and it counts up from there,
        # so the newest entry (top of page 1) carries the highest number —
        # numbering reads bottom-to-top rather than top-to-bottom. This is
        # computed in the database rather than guessed from page/page_size
        # on the frontend, so it stays correct regardless of what page_size
        # is requested and survives filtering/branch-scoping/pagination.
        from django.db.models import F, Window
        from django.db.models.functions import RowNumber
        qs = qs.annotate(
            serial_number=Window(
                expression=RowNumber(),
                order_by=[F("timestamp").asc(), F("log_id").asc()],
            )
        )

        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(AuditLogSerializer(page, many=True).data)


# ─── Self-profile endpoints ──────────────────────────────────────
class ReceptionistSelfView(APIView):
    permission_classes = [IsReceptionist]

    def get(self, request):
        profile = get_object_or_404(ReceptionistProfile, staff__user=request.user)
        return Response(ReceptionistProfileSerializer(profile).data)


class PharmacistSelfView(APIView):
    permission_classes = [IsPharmacist]

    def get(self, request):
        profile = get_object_or_404(PharmacistProfile, staff__user=request.user)
        return Response(PharmacistProfileSerializer(profile).data)

# ─────────────────────────────────────────────
# HOSPITAL SETTINGS  (admin only, singleton)
# GET  /api/admin/settings/
# PATCH /api/admin/settings/
# ─────────────────────────────────────────────
class HospitalSettingsView(AdminOnlyView):
    """
    GET  — returns current settings (mrd_registration_fee) for the caller's branch.
    PATCH — admin updates mrd_registration_fee for the caller's branch.

    Operates on the requesting staff member's own branch row. The
    is_group_admin superuser has no branch of their own, so must pass
    ?branch=<id> to act on behalf of a specific branch.
    """

    def _target_branch(self, request):
        from authentication.utils import is_group_admin_user, get_user_branch
        from administration.models import Branch

        if is_group_admin_user(request.user):
            branch_id = request.query_params.get("branch") or request.data.get("branch")
            if not branch_id:
                return None, Response(
                    {"errors": {"branch": "Group admin must specify ?branch=<id>."}},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            branch = get_object_or_404(Branch, pk=branch_id)
            return branch, None

        branch = get_user_branch(request.user)
        if branch is None:
            return None, Response(
                {"errors": {"branch": "Your account has no branch assigned."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return branch, None

    def get(self, request):
        branch, error = self._target_branch(request)
        if error:
            return error
        settings = HospitalSettings.get(branch)
        return Response(HospitalSettingsSerializer(settings).data)

    def patch(self, request):
        branch, error = self._target_branch(request)
        if error:
            return error
        settings = HospitalSettings.get(branch)
        serializer = HospitalSettingsSerializer(settings, data=request.data, partial=True)
        if serializer.is_valid():
            instance = serializer.save()
            log_admin_action(request, "UPDATE", "Administration", instance, label=f"Hospital settings ({branch.code})")
            return Response({
                'message': 'Hospital settings updated.',
                'data': serializer.data,
            })
        return Response(serializer.errors, status=400)

    def put(self, request):
        return self.patch(request)