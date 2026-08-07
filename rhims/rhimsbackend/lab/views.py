import logging
from datetime import date, datetime, time
# pyrefly: ignore [missing-import]
from rest_framework.views import APIView
# pyrefly: ignore [missing-import]
from rest_framework.response import Response
# pyrefly: ignore [missing-import]
from rest_framework import status, exceptions as drf_exceptions
# pyrefly: ignore [missing-import]
from rest_framework.permissions import IsAuthenticated
# pyrefly: ignore [missing-import]
from rest_framework.pagination import PageNumberPagination
# pyrefly: ignore [missing-import]
from django.shortcuts import get_object_or_404
# pyrefly: ignore [missing-import]
from django.db import transaction
# pyrefly: ignore [missing-import]
from django.db.models import Q, Count
# pyrefly: ignore [missing-import]
from django.utils import timezone
# pyrefly: ignore [missing-import]
from django.core.exceptions import ObjectDoesNotExist

from .models import (
    TestGroup,
    LabTest,
    LabRequest,
    LabRequestItem,
    LabResult,
    LabReport,
    LabRequestStatus,
    LabBill,
    LabEquipment,
    LabMaintenance,
    LabOrder,
    calculate_lab_subtotal,
)

from .serializers import (
    TestGroupSerializer,
    LabTestSerializer,
    LabRequestSerializer,
    LabRequestWriteSerializer,
    WalkInLabRequestSerializer,
    LabRequestStatusSerializer,
    LabRequestItemSerializer,
    LabResultSerializer,
    LabResultWriteSerializer,
    LabReportSerializer,
    LabReportWriteSerializer,
    LabBillSerializer,
    LabBillWriteSerializer,
    LabBillPaySerializer,
    LabEquipmentSerializer,
    LabMaintenanceSerializer,
    LabMaintenanceWriteSerializer,
    LabOrderSerializer,
    LabOrderWriteSerializer,
)

from .models import BillPaymentStatus

from authentication.permissions import (
    IsAdminUser,
    IsAdminOrLabTechnician,
    IsAdminOrDoctor,
    IsAdminOrDoctorOrLabTechnician,
    IsAdminOrManager,
    _get_role,
)
from authentication.utils import (
    is_group_admin_user,
    get_user_branch,
    scope_queryset_to_branch,
    resolve_branch_for_write,
)


def _claim_guard(lab_request, user):
    """
    Enforces the lab claim workflow: once a request is claimed, only the
    claimant (or an admin/manager) may act on it (enter/edit results,
    change status). Unclaimed requests behave as before — any lab staff
    member with the normal role permission can act.

    Returns a 403 Response if the actor is blocked, otherwise None.
    """
    if lab_request.claimed_by_id is None:
        return None

    if lab_request.claimed_by_id == getattr(user, 'id', None):
        return None

    if _get_role(user) in ('admin', 'manager'):
        return None

    claimant_name = (
        lab_request.claimed_by.get_full_name()
        or lab_request.claimed_by.username
    )
    return Response(
        {'error': f'This request has been claimed by {claimant_name}. Only they (or an admin/manager) can act on it.'},
        status=status.HTTP_403_FORBIDDEN,
    )


# ═══════════════════════════════════════════════════════════════
# DATE-RANGE FILTER HELPERS
# Used by LabRequestListView, LabDashboardView and LabBillListView to add
# optional ?start=&end= (YYYY-MM-DD) filtering. Filtering is opt-in: if
# either param is missing/invalid, no date filter is applied and the
# existing "show everything" behaviour is preserved so lab staff don't
# lose visibility into a still-open backlog by default.
# ═══════════════════════════════════════════════════════════════

def _parse_date_range(request):
    """Returns (start, end) as date objects, or (None, None) if either
    query param is missing or not a valid YYYY-MM-DD date."""
    raw_start = request.query_params.get("start")
    raw_end = request.query_params.get("end")
    if not raw_start or not raw_end:
        return None, None
    try:
        start = date.fromisoformat(raw_start)
        end = date.fromisoformat(raw_end)
    except ValueError:
        return None, None
    return start, end


# For DateTimeField columns (e.g. LabBill.created_at): builds a
# timezone-aware [local midnight, local end-of-day] pair so the comparison
# lines up with what the user sees on their clock, rather than rolling
# over at UTC midnight.
def _local_day_range(start, end):
    lo = timezone.make_aware(datetime.combine(start, time.min))
    hi = timezone.make_aware(datetime.combine(end, time.max))
    return lo, hi


# ═══════════════════════════════════════════════════════════════
# STANDARD PAGINATION
# ═══════════════════════════════════════════════════════════════

class StandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 200


# ═══════════════════════════════════════════════════════════════
# SAFE RESPONSE MIXIN
# ═══════════════════════════════════════════════════════════════

import logging as _logging
from django.conf import settings as _settings

_lab_logger = _logging.getLogger(__name__)


def _safe_detail(exc):
    """Only expose exception text to the client when DEBUG=True (see doctor/views.py's
    identical helper for rationale)."""
    return str(exc) if _settings.DEBUG else "An internal error occurred. Please try again or contact support."


class SafeAPIView(APIView):

    def handle_exception(self, exc):
        if isinstance(exc, drf_exceptions.APIException):
            return super().handle_exception(exc)

        # SECURITY: full detail goes to the server log only. The client
        # response no longer includes str(exc) — in DEBUG we still surface
        # it for local development convenience.
        _lab_logger.exception("LAB API UNEXPECTED ERROR: %s", type(exc).__name__)

        body = {
            "success": False,
            "message": "Internal server error",
        }
        if _settings.DEBUG:
            body["error"] = str(exc)

        return Response(body, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ═══════════════════════════════════════════════════════════════
# OWNERSHIP HELPER
# Doctors may only access lab requests they created (requested_by == request.user).
# Lab techs and admins see all.
# Returns 404 (not 403) to avoid leaking whether a request ID exists.
# ═══════════════════════════════════════════════════════════════

def _get_lab_request_for_user(request, pk, select_related=None, prefetch_related=None):
    qs = LabRequest.objects.all()
    if select_related:
        qs = qs.select_related(*select_related)
    if prefetch_related:
        qs = qs.prefetch_related(*prefetch_related)

    # SECURITY: branch scoping first — a non-group-admin user (including
    # lab techs and branch "admin"/manager roles) is confined to their own
    # StaffProfile.branch, matching TestGroup/LabTest/LabRequestListView.
    # Previously this only filtered doctors by requested_by, so any lab
    # tech/branch-admin could reach another branch's request by pk.
    qs = scope_queryset_to_branch(qs, request.user, branch_field='branch')

    role = _get_role(request.user)
    if role == "doctor":
        qs = qs.filter(requested_by=request.user)

    return get_object_or_404(qs, pk=pk)


# ═══════════════════════════════════════════════════════════════
# 1. LAB TESTS
# ═══════════════════════════════════════════════════════════════

class TestGroupListView(SafeAPIView):
    """
    GET  /api/lab/test-groups/  — list panels (e.g. LFT, KFT, LIPID) for the
                                   test-picker UI, each with its sub_tests
                                   nested for the expandable preview.
    POST /api/lab/test-groups/  — admin/lab-tech only; primarily managed via
                                   Django admin (see lab/admin.py), but
                                   exposed here too for API-driven tooling.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            qs = TestGroup.objects.all().prefetch_related('sub_tests')
            qs = scope_queryset_to_branch(qs, request.user, branch_field='branch', branch_id=request.query_params.get('branch'))

            show_inactive = (
                request.query_params.get("show_inactive", "false").lower()
                == "true"
            )

            if not show_inactive:
                qs = qs.filter(is_active=True)

            search = request.query_params.get("search", "").strip()

            if search:
                qs = qs.filter(
                    Q(name__icontains=search)
                    | Q(code__icontains=search)
                )

            serializer = TestGroupSerializer(qs, many=True)

            return Response(serializer.data)

        except Exception as e:
            _lab_logger.exception("Error listing test groups")
            return Response(
                {"error": _safe_detail(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def post(self, request):

        if not IsAdminOrLabTechnician().has_permission(request, self):
            return Response(
                {"detail": "Permission denied"},
                status=status.HTTP_403_FORBIDDEN
            )

        branch, error = resolve_branch_for_write(request, required=True)
        if error:
            return error
        data = request.data.copy()
        data["branch"] = branch.pk

        serializer = TestGroupSerializer(data=data)

        if serializer.is_valid():
            group = serializer.save()

            return Response(
                {
                    "message": "Test group created",
                    "data": TestGroupSerializer(group).data
                },
                status=status.HTTP_201_CREATED
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )


class TestGroupDetailView(SafeAPIView):
    """
    GET    /api/lab/test-groups/{pk}/  — single panel detail (with sub_tests).
    PATCH  /api/lab/test-groups/{pk}/  — admin/lab-tech only; edit name, code,
                                          price, description, is_active.
    DELETE /api/lab/test-groups/{pk}/  — admin only. Sub-tests are NOT
                                          deleted; their `group` FK is simply
                                          set to NULL (on_delete=SET_NULL) so
                                          they revert to standalone tests.
                                          Historical LabRequestItem rows keep
                                          their `ordered_as_group` reference
                                          unaffected (also SET_NULL on delete),
                                          so past bills are untouched.
    """

    permission_classes = [IsAuthenticated]

    def get_object(self, request, pk):
        qs = scope_queryset_to_branch(TestGroup.objects.all(), request.user, branch_field='branch')
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):

        serializer = TestGroupSerializer(
            self.get_object(request, pk)
        )

        return Response(serializer.data)

    def patch(self, request, pk):

        if not IsAdminOrLabTechnician().has_permission(request, self):
            return Response(
                {"detail": "Permission denied"},
                status=status.HTTP_403_FORBIDDEN
            )

        group = self.get_object(request, pk)

        serializer = TestGroupSerializer(
            group,
            data=request.data,
            partial=True
        )

        if serializer.is_valid():
            serializer.save()

            return Response(
                {
                    "message": "Updated successfully",
                    "data": serializer.data
                }
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )

    def delete(self, request, pk):

        if not IsAdminUser().has_permission(request, self):
            return Response(
                {"detail": "Admin only"},
                status=status.HTTP_403_FORBIDDEN
            )

        self.get_object(request, pk).delete()

        return Response(
            {"message": "Test group deleted"},
            status=status.HTTP_204_NO_CONTENT
        )


class LabTestListView(SafeAPIView):

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            qs = LabTest.objects.all().select_related('group')
            qs = scope_queryset_to_branch(qs, request.user, branch_field='branch', branch_id=request.query_params.get('branch'))

            show_inactive = (
                request.query_params.get("show_inactive", "false").lower()
                == "true"
            )

            if not show_inactive:
                qs = qs.filter(is_active=True)

            search = request.query_params.get("search", "").strip()

            if search:
                qs = qs.filter(
                    Q(name__icontains=search)
                    | Q(code__icontains=search)
                )

            serializer = LabTestSerializer(qs, many=True)

            return Response(serializer.data)

        except Exception as e:
            _lab_logger.exception("Error listing lab tests")
            return Response(
                {"error": _safe_detail(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def post(self, request):

        if not IsAdminOrLabTechnician().has_permission(request, self):
            return Response(
                {"detail": "Permission denied"},
                status=status.HTTP_403_FORBIDDEN
            )

        branch, error = resolve_branch_for_write(request, required=True)
        if error:
            return error
        data = request.data.copy()
        data["branch"] = branch.pk

        serializer = LabTestSerializer(data=data)

        if serializer.is_valid():
            test = serializer.save()

            return Response(
                {
                    "message": "Lab test created",
                    "data": LabTestSerializer(test).data
                },
                status=status.HTTP_201_CREATED
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )


class LabTestDetailView(SafeAPIView):

    permission_classes = [IsAuthenticated]

    def get_object(self, request, pk):
        qs = scope_queryset_to_branch(LabTest.objects.all(), request.user, branch_field='branch')
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):

        serializer = LabTestSerializer(
            self.get_object(request, pk)
        )

        return Response(serializer.data)

    def patch(self, request, pk):

        if not IsAdminOrLabTechnician().has_permission(request, self):
            return Response(
                {"detail": "Permission denied"},
                status=status.HTTP_403_FORBIDDEN
            )

        test = self.get_object(request, pk)

        serializer = LabTestSerializer(
            test,
            data=request.data,
            partial=True
        )

        if serializer.is_valid():
            serializer.save()

            return Response(
                {
                    "message": "Updated successfully",
                    "data": serializer.data
                }
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )

    def delete(self, request, pk):

        if not IsAdminUser().has_permission(request, self):
            return Response(
                {"detail": "Admin only"},
                status=status.HTTP_403_FORBIDDEN
            )

        self.get_object(request, pk).delete()

        return Response(
            {"message": "Lab test deleted"},
            status=status.HTTP_204_NO_CONTENT
        )


# ═══════════════════════════════════════════════════════════════
# 2. LAB REQUESTS
# ═══════════════════════════════════════════════════════════════

class LabRequestListView(SafeAPIView):

    # SECURITY: previously IsAuthenticated only — ANY authenticated staff
    # (pharmacist, receptionist, manager) could read any patient's lab
    # requests/results/reports. Doctors are further scoped to their own
    # requests by _get_lab_request_for_user()'s queryset filtering.
    permission_classes = [IsAuthenticated, IsAdminOrDoctorOrLabTechnician]

    def get(self, request):

        qs = (
            LabRequest.objects
            .select_related(
                "patient",
                "consultation",
                "requested_by"
            )
            .prefetch_related(
                "items__test",
                "items__result",
                "report"
            )
            .order_by("-request_id")
        )
        qs = scope_queryset_to_branch(qs, request.user, branch_field='branch', branch_id=request.query_params.get('branch'))

        role = _get_role(request.user)

        # FIX: Doctors only see their own lab requests.
        # Lab techs and admins see all requests across all doctors.
        if role == "doctor":
            qs = qs.filter(requested_by=request.user)

        filter_status = request.query_params.get(
            "status",
            ""
        ).upper()

        if filter_status:
            qs = qs.filter(status=filter_status)

        patient_id = request.query_params.get("patient_id")

        if patient_id:
            qs = qs.filter(patient_id=patient_id)

        consultation_id = request.query_params.get(
            "consultation_id"
        )

        if consultation_id:
            qs = qs.filter(
                consultation_id=consultation_id
            )

        # Optional date-range filter (Dashboard / Lab Request pages' new
        # calendar filter). request_date is the canonical "when was this
        # request made" field — a plain DateField, so a direct range
        # comparison is safe without any timezone conversion.
        start, end = _parse_date_range(request)
        if start and end:
            qs = qs.filter(request_date__range=(start, end))

        paginator = StandardPagination()

        page = paginator.paginate_queryset(qs, request)

        serializer = LabRequestSerializer(
            page,
            many=True,
            context={'request': request}
        )

        return paginator.get_paginated_response(
            serializer.data
        )


class LabRequestCreateView(SafeAPIView):

    permission_classes = [
        IsAuthenticated,
        IsAdminOrDoctor
    ]

    @transaction.atomic
    def post(self, request):
        data = request.data.copy()
        data["requested_by"] = request.user.pk
        serializer = LabRequestWriteSerializer(data=data, context={'request': request})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        try:
            lab_request = serializer.save()

            # Auto-update consultation status to LAB_REQUESTED
            try:
                from doctor.models import Consultation, ConsultationStatus
                consultation = lab_request.consultation
                if consultation and consultation.status == ConsultationStatus.STARTED:
                    consultation.status = ConsultationStatus.LAB_REQUESTED
                    consultation.save(update_fields=['status'])
            except Exception:
                pass

            items = lab_request.items.select_related('test', 'ordered_as_group').all()
            subtotal = calculate_lab_subtotal(items)
            bill = LabBill.objects.create(
                lab_request=lab_request,
                patient=lab_request.patient,
                subtotal=subtotal,
                discount=0,
                paid_amount=0,
                payment_method='NONE',
                billed_by=request.user,
                notes='Auto-generated on lab request creation.',
            )

            response_data = LabRequestSerializer(lab_request).data
            response_data['bill'] = LabBillSerializer(bill).data
            return Response(response_data, status=status.HTTP_201_CREATED)
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.exception('Error creating LabRequest')
            return Response({'error': _safe_detail(e)}, status=status.HTTP_400_BAD_REQUEST)


class LabRequestDetailView(SafeAPIView):

    # SECURITY: previously IsAuthenticated only — ANY authenticated staff
    # (pharmacist, receptionist, manager) could read any patient's lab
    # requests/results/reports. Doctors are further scoped to their own
    # requests by _get_lab_request_for_user()'s queryset filtering.
    permission_classes = [IsAuthenticated, IsAdminOrDoctorOrLabTechnician]

    def get(self, request, pk):
        # FIX: Doctors can only fetch their own lab requests.
        lab_request = _get_lab_request_for_user(
            request, pk,
            select_related=["patient", "consultation", "requested_by"],
            prefetch_related=["items__test", "items__result", "report"],
        )

        serializer = LabRequestSerializer(lab_request, context={'request': request})

        return Response(serializer.data)


# ═══════════════════════════════════════════════════════════════
# LAB REQUEST CLAIM / UNCLAIM
# ═══════════════════════════════════════════════════════════════

class LabRequestClaimView(SafeAPIView):
    """
    POST /api/lab/requests/<id>/claim/

    First lab staff member to claim an unclaimed request becomes its sole
    claimant. The request stays visible to everyone, marked "Claimed by
    {name}", but only the claimant (or an admin/manager) can act on it
    afterwards — see _claim_guard().
    """

    permission_classes = [IsAuthenticated, IsAdminOrLabTechnician]

    @transaction.atomic
    def post(self, request, pk):
        # SECURITY: branch-scope so a lab tech can't claim another branch's request.
        qs = scope_queryset_to_branch(
            LabRequest.objects.select_for_update(), request.user, branch_field='branch'
        )
        lab_request = get_object_or_404(qs, pk=pk)

        if lab_request.claimed_by_id is not None:
            claimant_name = (
                lab_request.claimed_by.get_full_name()
                or lab_request.claimed_by.username
            )
            return Response(
                {'error': f'Already claimed by {claimant_name}.'},
                status=status.HTTP_409_CONFLICT,
            )

        lab_request.claimed_by = request.user
        lab_request.claimed_at = timezone.now()
        lab_request.save(update_fields=['claimed_by', 'claimed_at'])

        return Response(
            {
                'message': f'Request #{lab_request.request_id} claimed.',
                'data': LabRequestSerializer(lab_request, context={'request': request}).data,
            }
        )


class LabRequestUnclaimView(SafeAPIView):
    """
    POST /api/lab/requests/<id>/unclaim/

    Admin/manager-only escape hatch to release a stuck claim (e.g. the
    claimant went off shift mid-request).
    """

    permission_classes = [IsAuthenticated, IsAdminOrManager]

    @transaction.atomic
    def post(self, request, pk):
        # SECURITY: branch-scope so an admin/manager can't unclaim another branch's request.
        qs = scope_queryset_to_branch(
            LabRequest.objects.select_for_update(), request.user, branch_field='branch'
        )
        lab_request = get_object_or_404(qs, pk=pk)

        lab_request.claimed_by = None
        lab_request.claimed_at = None
        lab_request.save(update_fields=['claimed_by', 'claimed_at'])

        return Response(
            {
                'message': f'Request #{lab_request.request_id} unclaimed.',
                'data': LabRequestSerializer(lab_request, context={'request': request}).data,
            }
        )


# ═══════════════════════════════════════════════════════════════
# WALK-IN LAB REQUEST
# A patient who walks directly into the lab for a test — no doctor
# consultation, no MRD registration. Mirrors pharmacist.CreateBillView's
# walk-in path: the lab technician (or admin) enters basic patient
# details, picks the tests, and a LabRequest + LabBill are created
# together in one call.
# ═══════════════════════════════════════════════════════════════

class WalkInLabRequestCreateView(SafeAPIView):
    """
    POST /api/lab/requests/walkin/create/

    Payload:
      {
        "walkin_name":  "Rahul Kumar",       // required
        "walkin_phone": "9876543210",        // optional, 10 digits
        "walkin_gender": "Male",             // optional
        "walkin_age": 45,                    // optional
        "notes": "...",                      // optional
        "test_ids": [1, 4, 7]                // required, at least 1
      }

    Creates a REQUESTED LabRequest (is_walkin=True, no consultation/patient)
    plus its items, and auto-generates the matching LabBill (unpaid) —
    exactly like a walk-in pharmacy bill is created before payment.
    """

    permission_classes = [IsAuthenticated, IsAdminOrLabTechnician]

    @transaction.atomic
    def post(self, request):
        data = request.data.copy()

        # Resolve branch up front — needed both for the duplicate-visit
        # lookup below and for the eventual LabRequest.branch assignment.
        branch, error = resolve_branch_for_write(request, required=True)
        if error:
            return error

        walkin_name  = (data.get('walkin_name') or '').strip()
        walkin_phone = (data.get('walkin_phone') or '').strip() or None

        # ── Duplicate-visit guard ───────────────────────────────────────
        # If this same walk-in (by name or phone) already has a request
        # today whose bill hasn't been paid yet, don't create a second
        # one — surface the existing request/bill so the frontend can
        # resume it instead of double-billing the same visit.
        # SECURITY: branch-scoped — otherwise a same-named/same-phone
        # walk-in at another branch today would leak that branch's request
        # id/bill/status into this branch's 409 response.
        today = timezone.localdate()
        existing = None
        if walkin_name or walkin_phone:
            if walkin_name and walkin_phone:
                match_q = Q(walkin_name__iexact=walkin_name) | Q(walkin_phone=walkin_phone)
            elif walkin_phone:
                match_q = Q(walkin_phone=walkin_phone)
            else:
                match_q = Q(walkin_name__iexact=walkin_name)

            existing = (
                LabRequest.objects
                .filter(is_walkin=True, request_date=today, branch_id=branch.pk)
                .filter(match_q)
                .exclude(status=LabRequestStatus.DELIVERED)
                .select_related('bill')
                .order_by('-request_id')
                .first()
            )

        if existing:
            existing_bill = getattr(existing, 'bill', None)
            if existing.status == LabRequestStatus.REQUESTED and (
                not existing_bill or existing_bill.payment_status != BillPaymentStatus.PAID
            ):
                return Response(
                    {
                        'error': (
                            f'An active walk-in lab request already exists for this patient '
                            f'today (Request #{existing.request_id}'
                            + (f', payment: {existing_bill.payment_status}' if existing_bill else '')
                            + '). Resuming existing request.'
                        ),
                        'existing_request_id': existing.request_id,
                        'existing_bill_id': existing_bill.bill_id if existing_bill else None,
                        'request': LabRequestSerializer(existing).data,
                        'bill': LabBillSerializer(existing_bill).data if existing_bill else None,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
        # ─────────────────────────────────────────────────────────────────

        # SECURITY/BUG FIX: branch was never set for walk-in requests (no
        # patient to auto-derive it from — see LabRequest.save()), which
        # would fail full_clean()'s "branch is required for walk-in
        # requests" check. Resolved above (before the duplicate-visit
        # check) rather than trusting the client to supply it directly.
        data['requested_by'] = request.user.pk
        data['branch'] = branch.pk
        serializer = WalkInLabRequestSerializer(data=data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            lab_request = serializer.save()

            items = lab_request.items.select_related('test', 'ordered_as_group').all()
            subtotal = calculate_lab_subtotal(items)
            bill = LabBill.objects.create(
                lab_request=lab_request,
                patient=None,
                subtotal=subtotal,
                discount=0,
                paid_amount=0,
                payment_method='NONE',
                billed_by=request.user,
                notes='Auto-generated for walk-in lab request.',
            )
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.exception('Error creating walk-in LabRequest')
            return Response({'error': _safe_detail(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                'message': 'Walk-in lab request created.',
                'request': LabRequestSerializer(lab_request).data,
                'bill': LabBillSerializer(bill).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ═══════════════════════════════════════════════════════════════
# 3. LAB EQUIPMENT
# ═══════════════════════════════════════════════════════════════

class LabEquipmentListView(SafeAPIView):

    permission_classes = [
        IsAuthenticated,
        IsAdminOrLabTechnician
    ]

    def get(self, request):

        try:

            qs = LabEquipment.objects.prefetch_related(
                "maintenance_logs"
            ).all()
            # SECURITY: was unscoped — leaked every branch's equipment list.
            qs = scope_queryset_to_branch(
                qs, request.user, branch_field='branch',
                branch_id=request.query_params.get('branch'),
            )

            eq_status = request.query_params.get(
                "status",
                ""
            ).upper()

            if eq_status:
                qs = qs.filter(status=eq_status)

            search = request.query_params.get(
                "search",
                ""
            ).strip()

            if search:
                qs = qs.filter(
                    Q(name__icontains=search)
                    | Q(model_number__icontains=search)
                    | Q(serial_number__icontains=search)
                    | Q(manufacturer__icontains=search)
                )

            serializer = LabEquipmentSerializer(
                qs,
                many=True
            )

            return Response(serializer.data)

        except Exception as e:

            logging.getLogger(__name__).exception("LAB EQUIPMENT ERROR")

            return Response(
                {
                    "success": False,
                    "error": _safe_detail(e)
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def post(self, request):

        # SECURITY: resolve branch server-side (own branch for ordinary
        # staff; group admin must supply one) rather than trusting whatever
        # branch the client puts in the body.
        branch, error = resolve_branch_for_write(request, required=True)
        if error:
            return error
        data = request.data.copy()
        data["branch"] = branch.pk

        serializer = LabEquipmentSerializer(
            data=data
        )

        if serializer.is_valid():

            equipment = serializer.save()

            return Response(
                {
                    "message": "Equipment created",
                    "data": LabEquipmentSerializer(
                        equipment
                    ).data
                },
                status=status.HTTP_201_CREATED
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )


class LabOrderListView(SafeAPIView):

    permission_classes = [
        IsAuthenticated,
        IsAdminOrLabTechnician
    ]

    def get(self, request):

        try:

            qs = (
                LabOrder.objects
                .select_related("ordered_by")
                .order_by("-order_id")
            )
            # SECURITY: LabOrder now carries a branch FK — was unscoped,
            # leaking every branch's procurement orders.
            qs = scope_queryset_to_branch(
                qs, request.user, branch_field='branch',
                branch_id=request.query_params.get('branch'),
            )

            order_status = request.query_params.get(
                "status",
                ""
            ).upper()

            if order_status:
                qs = qs.filter(status=order_status)

            category = request.query_params.get(
                "category",
                ""
            ).upper()

            if category:
                qs = qs.filter(category=category)

            search = request.query_params.get(
                "search",
                ""
            ).strip()

            if search:
                qs = qs.filter(
                    Q(item_name__icontains=search)
                    | Q(supplier__icontains=search)
                )

            paginator = StandardPagination()

            page = paginator.paginate_queryset(
                qs,
                request
            )

            serializer = LabOrderSerializer(
                page,
                many=True
            )

            return paginator.get_paginated_response(
                serializer.data
            )

        except Exception as e:

            logging.getLogger(__name__).exception("LAB ORDER ERROR")

            return Response(
                {
                    "success": False,
                    "error": _safe_detail(e)
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @transaction.atomic
    def post(self, request):

        # SECURITY: resolve branch server-side (own branch for ordinary
        # staff; group admin must supply one) — LabOrder.branch is required.
        branch, error = resolve_branch_for_write(request, required=True)
        if error:
            return error
        data = request.data.copy()

        data["ordered_by"] = request.user.pk
        data["branch"] = branch.pk

        serializer = LabOrderWriteSerializer(
            data=data
        )

        if serializer.is_valid():

            order = serializer.save()

            return Response(
                {
                    "message": "Order created",
                    "data": LabOrderSerializer(
                        order
                    ).data
                },
                status=status.HTTP_201_CREATED
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )


# ═══════════════════════════════════════════════════════════════
# 6. DASHBOARD
# ═══════════════════════════════════════════════════════════════

class LabDashboardView(SafeAPIView):

    permission_classes = [
        IsAuthenticated,
        IsAdminOrLabTechnician
    ]

    def get(self, request):

        qs = LabRequest.objects.all()
        # SECURITY: was unscoped — dashboard counts included every branch's requests.
        qs = scope_queryset_to_branch(
            qs, request.user, branch_field='branch',
            branch_id=request.query_params.get('branch'),
        )

        # Optional date-range filter — same request_date field and same
        # opt-in behaviour as LabRequestListView, so switching the
        # dashboard's date filter and the Lab Requests page's filter to
        # the same range shows consistent numbers.
        start, end = _parse_date_range(request)
        if start and end:
            qs = qs.filter(request_date__range=(start, end))

        counts = (
            qs
            .values("status")
            .annotate(count=Count("request_id"))
        )

        summary = {
            row["status"]: row["count"]
            for row in counts
        }

        for s in LabRequestStatus.values:
            summary.setdefault(s, 0)

        return Response(
            {
                "total": sum(summary.values()),
                "by_status": summary
            }
        )

class LabRequestStatusUpdateView(APIView):
    permission_classes = [IsAuthenticated, IsAdminOrLabTechnician]

    @transaction.atomic
    def patch(self, request, pk):
        # SECURITY: branch-scope so a lab tech can't update another branch's request status.
        qs = scope_queryset_to_branch(LabRequest.objects.all(), request.user, branch_field='branch')
        lab_request = get_object_or_404(qs, pk=pk)

        guard_response = _claim_guard(lab_request, request.user)
        if guard_response is not None:
            return guard_response

        serializer = LabRequestStatusSerializer(
            data=request.data,
            context={'current_status': lab_request.status}
        )

        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )

        new_status = serializer.validated_data['status']

        if new_status == LabRequestStatus.SAMPLE_COLLECTED:
            bill = getattr(lab_request, 'bill', None)
            if not bill:
                return Response(
                    {
                        'error': (
                            'No lab bill found for this request. '
                            'A bill must be generated and paid before '
                            'sample collection can proceed.'
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if bill.payment_status != BillPaymentStatus.PAID:
                return Response(
                    {
                        'error': (
                            f'Lab bill #{bill.bill_id} must be PAID before '
                            f'sample collection. Current payment status: '
                            f'{bill.payment_status}. '
                            f'Total due: {bill.total_amount}'
                        ),
                        'bill_id': bill.bill_id,
                        'payment_status': bill.payment_status,
                        'total_amount': str(bill.total_amount),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if new_status == LabRequestStatus.COMPLETED:
            items_qs = lab_request.items.all()
            total_items = items_qs.count()
            items_missing_results = items_qs.filter(result__isnull=True)

            if total_items == 0 or items_missing_results.exists():
                missing_tests = list(
                    items_missing_results.values_list('test__name', flat=True)
                )
                return Response(
                    {
                        'error': (
                            'All test results must be entered before this '
                            'request can be marked as completed.'
                        ),
                        'missing_results_for': missing_tests,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        lab_request.status = new_status
        lab_request.save(update_fields=['status', 'updated_at'])

        if new_status == LabRequestStatus.COMPLETED:
            LabReport.objects.get_or_create(
                lab_request=lab_request
            )

        if new_status == LabRequestStatus.VERIFIED:
            report = getattr(lab_request, 'report', None)

            if report:
                report.verified_by = request.user
                report.verified_at = timezone.now()

                report.save(
                    update_fields=[
                        'verified_by',
                        'verified_at',
                        'updated_at'
                    ]
                )

        if new_status == LabRequestStatus.DELIVERED:
            report = getattr(lab_request, 'report', None)

            if report:
                report.delivered_at = timezone.now()

                report.save(
                    update_fields=[
                        'delivered_at',
                        'updated_at'
                    ]
                )

        # Sync consultation status based on lab progress
        try:
            from doctor.models import Consultation, ConsultationStatus
            consultation = lab_request.consultation
            if consultation and consultation.status != ConsultationStatus.COMPLETED:
                if new_status in (
                    LabRequestStatus.SAMPLE_COLLECTED,
                    LabRequestStatus.PROCESSING,
                ):
                    if consultation.status != ConsultationStatus.WAITING_FOR_LAB:
                        consultation.status = ConsultationStatus.WAITING_FOR_LAB
                        consultation.save(update_fields=['status'])
                elif new_status == LabRequestStatus.DELIVERED:
                    # All lab requests for this consultation must be DELIVERED
                    # before moving to LAB_COMPLETED
                    all_requests = consultation.lab_requests.exclude(
                        request_id=lab_request.request_id
                    )
                    all_delivered = all(
                        r.status == LabRequestStatus.DELIVERED
                        for r in all_requests
                    )
                    if all_delivered:
                        consultation.status = ConsultationStatus.LAB_COMPLETED
                        consultation.save(update_fields=['status'])
        except Exception:
            pass

        return Response({
            'message': f'Lab request status updated to {new_status}.',
            'request_id': lab_request.request_id,
            'status': new_status,
        })


# ═══════════════════════════════════════════════════════════════
# LAB RESULTS
# ═══════════════════════════════════════════════════════════════

# Results may only be entered once a sample is actually being processed
# (or amended while still COMPLETED, prior to verification). Entering a
# result while the request is only REQUESTED or SAMPLE_COLLECTED would mean
# reporting a value before testing has even started.
RESULT_ENTERABLE_STATUSES = (
    LabRequestStatus.PROCESSING,
    LabRequestStatus.COMPLETED,
)


class LabResultCreateView(SafeAPIView):

    permission_classes = [
        IsAuthenticated,
        IsAdminOrLabTechnician
    ]

    @transaction.atomic
    def post(self, request):

        data = request.data.copy()

        data["performed_by"] = request.user.pk

        lab_request_item_id = data.get("lab_request_item")
        # SECURITY: branch-scope via the parent lab_request's branch so a lab
        # tech can't enter a result for another branch's test item.
        item_qs = scope_queryset_to_branch(
            LabRequestItem.objects.all(), request.user, branch_field='lab_request__branch'
        )
        item = get_object_or_404(item_qs, pk=lab_request_item_id)

        guard_response = _claim_guard(item.lab_request, request.user)
        if guard_response is not None:
            return guard_response

        if item.lab_request.status not in RESULT_ENTERABLE_STATUSES:
            return Response(
                {
                    'error': (
                        'Results can only be entered once this request is '
                        'PROCESSING (or corrected while COMPLETED). '
                        f'Current status: {item.lab_request.status}.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = LabResultWriteSerializer(data=data)

        if serializer.is_valid():

            result = serializer.save()

            return Response(
                {
                    "message": "Lab result created",
                    "data": LabResultSerializer(result).data
                },
                status=status.HTTP_201_CREATED
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )


class LabResultDetailView(SafeAPIView):

    permission_classes = [
        IsAuthenticated,
        IsAdminOrLabTechnician
    ]

    def get_object(self, request, pk):
        # SECURITY: branch-scope via lab_request_item -> lab_request -> branch
        # so a lab tech can't read/edit another branch's result.
        qs = scope_queryset_to_branch(
            LabResult.objects.select_related(
                "lab_request_item",
                "performed_by"
            ),
            request.user,
            branch_field='lab_request_item__lab_request__branch',
        )
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):

        serializer = LabResultSerializer(
            self.get_object(request, pk)
        )

        return Response(serializer.data)

    @transaction.atomic
    def patch(self, request, pk):

        result = self.get_object(request, pk)
        lab_request = result.lab_request_item.lab_request

        guard_response = _claim_guard(lab_request, request.user)
        if guard_response is not None:
            return guard_response

        if lab_request.status not in RESULT_ENTERABLE_STATUSES:
            return Response(
                {
                    'error': (
                        'Results can only be edited while this request is '
                        'PROCESSING or COMPLETED (prior to verification). '
                        f'Current status: {lab_request.status}.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = LabResultWriteSerializer(
            result,
            data=request.data,
            partial=True
        )

        if serializer.is_valid():

            serializer.save()

            return Response(
                {
                    "message": "Lab result updated",
                    "data": serializer.data
                }
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )


# ═══════════════════════════════════════════════════════════════
# LAB RESULTS BY REQUEST
# ═══════════════════════════════════════════════════════════════

class LabResultsByRequestView(SafeAPIView):

    # SECURITY: previously IsAuthenticated only — ANY authenticated staff
    # (pharmacist, receptionist, manager) could read any patient's lab
    # requests/results/reports. Doctors are further scoped to their own
    # requests by _get_lab_request_for_user()'s queryset filtering.
    permission_classes = [IsAuthenticated, IsAdminOrDoctorOrLabTechnician]

    def get(self, request, request_pk):
        # FIX: Doctors can only read results for their own lab requests.
        # Lab techs and admins can read any request's results.
        lab_request = _get_lab_request_for_user(request, request_pk)

        items = (
            LabRequestItem.objects
            .filter(lab_request=lab_request)
            .select_related(
                "test",
                "result"
            )
        )

        serializer = LabRequestItemSerializer(
            items,
            many=True
        )

        return Response(
            {
                "request_id": lab_request.request_id,
                "status": lab_request.status,
                "items": serializer.data
            }
        )


# ═══════════════════════════════════════════════════════════════
# LAB REPORT
# ═══════════════════════════════════════════════════════════════

class LabReportDetailView(SafeAPIView):

    # SECURITY: previously IsAuthenticated only — ANY authenticated staff
    # (pharmacist, receptionist, manager) could read any patient's lab
    # requests/results/reports. Doctors are further scoped to their own
    # requests by _get_lab_request_for_user()'s queryset filtering.
    permission_classes = [IsAuthenticated, IsAdminOrDoctorOrLabTechnician]

    def get(self, request, request_pk):
        # FIX: Doctors can only read reports for their own lab requests.
        lab_request = _get_lab_request_for_user(request, request_pk)

        report = getattr(
            lab_request,
            "report",
            None
        )

        if not report:

            return Response(
                {
                    "message": "Report not generated"
                },
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = LabReportSerializer(report)

        return Response(serializer.data)

    @transaction.atomic
    def patch(self, request, request_pk):
        # Reports are written by lab techs/admins only; no doctor ownership check needed here.
        # SECURITY: branch-scope so a lab tech can't edit another branch's report.
        qs = scope_queryset_to_branch(LabRequest.objects.all(), request.user, branch_field='branch')
        lab_request = get_object_or_404(qs, pk=request_pk)

        report = getattr(
            lab_request,
            "report",
            None
        )

        if not report:

            return Response(
                {
                    "error": "Report not found"
                },
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = LabReportWriteSerializer(
            report,
            data=request.data,
            partial=True
        )

        if serializer.is_valid():

            serializer.save()

            return Response(
                {
                    "message": "Report updated",
                    "data": serializer.data
                }
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )


# ═══════════════════════════════════════════════════════════════
# PATIENT LAB HISTORY
# ═══════════════════════════════════════════════════════════════

class PatientLabHistoryView(SafeAPIView):

    # SECURITY: previously IsAuthenticated only — ANY authenticated staff
    # (pharmacist, receptionist, manager) could read any patient's lab
    # requests/results/reports. Doctors are further scoped to their own
    # requests by _get_lab_request_for_user()'s queryset filtering.
    permission_classes = [IsAuthenticated, IsAdminOrDoctorOrLabTechnician]

    def get(self, request, patient_id):

        qs = (
            LabRequest.objects
            .filter(patient_id=patient_id)
            .select_related(
                "consultation",
                "requested_by"
            )
            .prefetch_related(
                "items__test",
                "items__result",
                "report"
            )
            .order_by("-request_id")
        )

        # SECURITY: branch-scope first — otherwise any lab tech/branch-admin
        # sees a patient's full lab history regardless of which branch
        # treated them.
        qs = scope_queryset_to_branch(qs, request.user, branch_field='branch')

        # FIX: Doctors only see their own requests for this patient.
        # Lab techs and admins see all requests across all doctors.
        role = _get_role(request.user)
        if role == "doctor":
            qs = qs.filter(requested_by=request.user)

        paginator = StandardPagination()

        page = paginator.paginate_queryset(
            qs,
            request
        )

        serializer = LabRequestSerializer(
            page,
            many=True,
            context={'request': request}
        )

        return paginator.get_paginated_response(
            serializer.data
        )


# ═══════════════════════════════════════════════════════════════
# LAB BILL DETAIL
# ═══════════════════════════════════════════════════════════════

class LabBillDetailView(SafeAPIView):

    permission_classes = [
        IsAuthenticated,
        IsAdminOrLabTechnician
    ]

    def get_object(self, request, pk):
        # SECURITY: branch-scope — LabBill.branch is a direct FK — so a lab
        # tech can't read/edit another branch's bill.
        qs = scope_queryset_to_branch(
            LabBill.objects.select_related(
                "lab_request",
                "patient",
                "billed_by"
            ),
            request.user,
            branch_field='branch',
        )
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):

        serializer = LabBillSerializer(
            self.get_object(request, pk)
        )

        return Response(serializer.data)

    @transaction.atomic
    def patch(self, request, pk):

        bill = self.get_object(request, pk)

        # The discount can't be changed once the amount has actually been
        # settled — matches the frontend's isLocked check (LabBillsPage.jsx)
        # and PharmacyBill's SetBillDiscountView, but must also be enforced
        # here since the frontend lock is UI-only.
        if 'discount' in request.data and bill.payment_status == BillPaymentStatus.PAID:
            return Response(
                {
                    'discount': (
                        f"Cannot change the discount on a bill that is "
                        f"'{bill.payment_status}'."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = LabBillWriteSerializer(
            bill,
            data=request.data,
            partial=True
        )

        if serializer.is_valid():

            serializer.save()

            return Response(
                {
                    "message": "Bill updated",
                    "data": LabBillSerializer(bill).data
                }
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )


# ═══════════════════════════════════════════════════════════════
# LAB EQUIPMENT DETAIL
# ═══════════════════════════════════════════════════════════════

class LabEquipmentDetailView(SafeAPIView):

    permission_classes = [
        IsAuthenticated,
        IsAdminOrLabTechnician
    ]

    def get_object(self, request, pk):
        # SECURITY: was unscoped — full read/edit/delete leak across branches.
        qs = scope_queryset_to_branch(LabEquipment.objects.all(), request.user, branch_field='branch')
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):

        serializer = LabEquipmentSerializer(
            self.get_object(request, pk)
        )

        return Response(serializer.data)

    def patch(self, request, pk):

        equipment = self.get_object(request, pk)

        serializer = LabEquipmentSerializer(
            equipment,
            data=request.data,
            partial=True
        )

        if serializer.is_valid():

            serializer.save()

            return Response(
                {
                    "message": "Equipment updated",
                    "data": serializer.data
                }
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )

    def delete(self, request, pk):

        if not IsAdminUser().has_permission(request, self):

            return Response(
                {
                    "detail": "Admin only"
                },
                status=status.HTTP_403_FORBIDDEN
            )

        equipment = self.get_object(request, pk)

        equipment.delete()

        return Response(
            {
                "message": "Equipment deleted"
            },
            status=status.HTTP_204_NO_CONTENT
        )


# ═══════════════════════════════════════════════════════════════
# LAB MAINTENANCE
# ═══════════════════════════════════════════════════════════════

class LabMaintenanceListView(SafeAPIView):

    permission_classes = [
        IsAuthenticated,
        IsAdminOrLabTechnician
    ]

    def get(self, request, equipment_pk):

        # SECURITY: branch-scope the equipment lookup itself — otherwise a
        # lab tech can view another branch's maintenance logs just by
        # knowing/guessing its equipment_pk.
        equipment_qs = scope_queryset_to_branch(
            LabEquipment.objects.all(), request.user, branch_field='branch'
        )
        get_object_or_404(equipment_qs, pk=equipment_pk)

        qs = (
            LabMaintenance.objects
            .filter(equipment_id=equipment_pk)
            .select_related(
                "equipment",
                "performed_by"
            )
            .order_by("-maintenance_id")
        )

        serializer = LabMaintenanceSerializer(
            qs,
            many=True
        )

        return Response(serializer.data)

    def post(self, request, equipment_pk):

        # SECURITY: branch-scope so a lab tech can't log maintenance against
        # another branch's equipment.
        equipment_qs = scope_queryset_to_branch(
            LabEquipment.objects.all(), request.user, branch_field='branch'
        )
        get_object_or_404(equipment_qs, pk=equipment_pk)

        data = request.data.copy()

        data["equipment"] = equipment_pk
        data["performed_by"] = request.user.pk

        serializer = LabMaintenanceWriteSerializer(
            data=data
        )

        if serializer.is_valid():

            maintenance = serializer.save()

            return Response(
                {
                    "message": "Maintenance created",
                    "data": LabMaintenanceSerializer(
                        maintenance
                    ).data
                },
                status=status.HTTP_201_CREATED
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )


class LabMaintenanceDetailView(SafeAPIView):

    permission_classes = [
        IsAuthenticated,
        IsAdminOrLabTechnician
    ]

    def get_object(self, request, pk):
        # SECURITY: branch-scope via equipment__branch — was unscoped.
        qs = scope_queryset_to_branch(
            LabMaintenance.objects.select_related(
                "equipment",
                "performed_by"
            ),
            request.user,
            branch_field='equipment__branch',
        )
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):

        serializer = LabMaintenanceSerializer(
            self.get_object(request, pk)
        )

        return Response(serializer.data)

    def patch(self, request, pk):

        maintenance = self.get_object(request, pk)

        serializer = LabMaintenanceWriteSerializer(
            maintenance,
            data=request.data,
            partial=True
        )

        if serializer.is_valid():

            serializer.save()

            return Response(
                {
                    "message": "Maintenance updated",
                    "data": serializer.data
                }
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )

    def delete(self, request, pk):

        if not IsAdminUser().has_permission(request, self):
            return Response(
                {"detail": "Admin only"},
                status=status.HTTP_403_FORBIDDEN
            )

        self.get_object(request, pk).delete()

        return Response(
            {"message": "Maintenance record deleted"},
            status=status.HTTP_204_NO_CONTENT
        )


# ═══════════════════════════════════════════════════════════════
# LAB ORDER DETAIL
# ═══════════════════════════════════════════════════════════════

class LabOrderDetailView(SafeAPIView):

    permission_classes = [
        IsAuthenticated,
        IsAdminOrLabTechnician
    ]

    def get_object(self, request, pk):
        # SECURITY: was unscoped — leaked every branch's procurement orders.
        qs = scope_queryset_to_branch(
            LabOrder.objects.select_related("ordered_by"),
            request.user,
            branch_field='branch',
        )
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):

        serializer = LabOrderSerializer(
            self.get_object(request, pk)
        )

        return Response(serializer.data)

    def patch(self, request, pk):

        order = self.get_object(request, pk)

        serializer = LabOrderWriteSerializer(
            order,
            data=request.data,
            partial=True
        )

        if serializer.is_valid():

            serializer.save()

            return Response(
                {
                    "message": "Order updated",
                    "data": serializer.data
                }
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )

    def delete(self, request, pk):

        if not IsAdminUser().has_permission(request, self):

            return Response(
                {
                    "detail": "Admin only"
                },
                status=status.HTTP_403_FORBIDDEN
            )

        order = self.get_object(request, pk)

        order.delete()

        return Response(
            {
                "message": "Order deleted"
            },
            status=status.HTTP_204_NO_CONTENT
        )


class LabBillListView(SafeAPIView):

    permission_classes = [
        IsAuthenticated,
        IsAdminOrLabTechnician,
    ]

    def get(self, request):

        try:
            qs = (
                LabBill.objects
                .select_related('lab_request', 'patient', 'billed_by')
                .order_by('-bill_id')
            )
            # SECURITY: was unscoped — leaked every branch's bills.
            qs = scope_queryset_to_branch(
                qs, request.user, branch_field='branch',
                branch_id=request.query_params.get('branch'),
            )

            payment_status = request.query_params.get('payment_status', '').upper()
            if payment_status:
                qs = qs.filter(payment_status=payment_status)

            patient_id = request.query_params.get('patient_id')
            if patient_id:
                qs = qs.filter(patient_id=patient_id)

            request_id = request.query_params.get('request_id')
            if request_id:
                qs = qs.filter(lab_request_id=request_id)

            search = request.query_params.get('search', '').strip()
            if search:
                qs = qs.filter(
                    Q(bill_number__icontains=search)
                    | Q(patient__first_name__icontains=search)
                    | Q(patient__last_name__icontains=search)
                    | Q(patient__mrd_number__icontains=search)
                    | Q(lab_request__walkin_name__icontains=search)
                )

            # Optional date-range filter (period presets + calendar picker
            # on the Bills page, mirroring manager/views.py AllBillsView).
            # created_at is a DateTimeField, so build a timezone-aware
            # [local midnight, local end-of-day] pair rather than comparing
            # raw dates — otherwise the range silently rolls over at UTC
            # midnight instead of the user's local midnight.
            start, end = _parse_date_range(request)
            if start and end:
                lo, hi = _local_day_range(start, end)
                qs = qs.filter(created_at__range=(lo, hi))

            paginator = StandardPagination()
            page = paginator.paginate_queryset(qs, request)
            return paginator.get_paginated_response(
                LabBillSerializer(page, many=True).data
            )

        except Exception as e:
            return Response(
                {'success': False, 'error': _safe_detail(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @transaction.atomic
    def post(self, request):
        data = request.data.copy()
        data['billed_by'] = request.user.pk

        # SECURITY: this generic create endpoint took an arbitrary
        # lab_request id with no branch check — a lab tech could create a
        # bill against another branch's request (LabBill.branch is only
        # auto-derived from lab_request.branch, so it doesn't block this).
        lab_request_id = data.get('lab_request')
        if lab_request_id:
            lr_qs = scope_queryset_to_branch(
                LabRequest.objects.all(), request.user, branch_field='branch'
            )
            get_object_or_404(lr_qs, pk=lab_request_id)

        serializer = LabBillWriteSerializer(data=data)

        if serializer.is_valid():
            bill = serializer.save()
            return Response(
                {
                    'message': 'Lab bill created',
                    'data': LabBillSerializer(bill).data,
                },
                status=status.HTTP_201_CREATED,
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ═══════════════════════════════════════════════════════════════
# LAB BILL — BY REQUEST
# ═══════════════════════════════════════════════════════════════

class LabBillByRequestView(SafeAPIView):

    # SECURITY: was IsAuthenticated only. This uses _get_lab_request_for_user
    # (doctor-scoped), so give it the matching permission class.
    permission_classes = [IsAuthenticated, IsAdminOrDoctorOrLabTechnician]

    def get(self, request, request_pk):
        # FIX: Doctors can only view the bill for their own lab request.
        lab_request = _get_lab_request_for_user(request, request_pk)
        bill = getattr(lab_request, 'bill', None)
        if not bill:
            return Response(
                {'message': 'No bill generated for this lab request yet.'},
                status=status.HTTP_404_NOT_FOUND
            )
        return Response(LabBillSerializer(bill).data)


class LabBillGenerateView(SafeAPIView):

    permission_classes = [IsAuthenticated, IsAdminOrLabTechnician]

    @transaction.atomic
    def post(self, request, request_pk):
        # SECURITY: branch-scope so a lab tech can't generate a bill for
        # another branch's request.
        qs = scope_queryset_to_branch(
            LabRequest.objects.prefetch_related('items__test', 'items__ordered_as_group'),
            request.user,
            branch_field='branch',
        )
        lab_request = get_object_or_404(qs, pk=request_pk)

        subtotal = calculate_lab_subtotal(lab_request.items.all())

        existing_bill = getattr(lab_request, 'bill', None)

        if existing_bill:
            if existing_bill.payment_status == BillPaymentStatus.PAID:
                return Response(
                    {'error': 'Bill is already PAID and cannot be regenerated.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            existing_bill.subtotal = subtotal
            existing_bill.billed_by = request.user
            existing_bill.save()
            bill = existing_bill
            message = 'Lab bill recalculated.'
        else:
            bill = LabBill.objects.create(
                lab_request=lab_request,
                patient=lab_request.patient,
                subtotal=subtotal,
                discount=0,
                paid_amount=0,
                payment_method='NONE',
                billed_by=request.user,
                notes='Generated via bill/generate endpoint.',
            )
            message = 'Lab bill generated.'

        return Response(
            {
                'message': message,
                'data': LabBillSerializer(bill).data,
            },
            status=status.HTTP_201_CREATED
        )


# ═══════════════════════════════════════════════════════════════
# LAB BILL PAY
# ═══════════════════════════════════════════════════════════════

class LabBillPayView(SafeAPIView):

    # SECURITY: was IsAuthenticated only — any authenticated staff could
    # mark any lab bill as paid. Payment actions restricted to admin/lab tech.
    permission_classes = [IsAuthenticated, IsAdminOrLabTechnician]

    @transaction.atomic
    def patch(self, request, pk):
        # SECURITY: branch-scope so a lab tech can't pay another branch's bill.
        qs = scope_queryset_to_branch(
            LabBill.objects.select_related('lab_request', 'patient'),
            request.user,
            branch_field='branch',
        )
        bill = get_object_or_404(qs, pk=pk)

        if bill.payment_status == BillPaymentStatus.PAID:
            return Response(
                {'error': 'Bill is already marked as PAID.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = LabBillPaySerializer(
            data=request.data,
            context={'bill': bill}
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        vd = serializer.validated_data
        bill.payment_method = vd['payment_method']
        bill.paid_amount    = vd['paid_amount']
        if vd.get('notes'):
            bill.notes = vd['notes']

        bill.save()

        return Response(
            {
                'message': 'Payment collected. Lab bill marked as PAID. '
                           'Sample collection can now proceed.',
                'bill_id':        bill.bill_id,
                'payment_status': bill.payment_status,
                'total_amount':   str(bill.total_amount),
                'paid_amount':    str(bill.paid_amount),
                'lab_request_id': bill.lab_request_id,
            }
        )