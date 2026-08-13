# ═════════════════════════════════════════════════════════════════════════════
# FILE: reception/views.py
# COMPLETE VIEWS - PRODUCTION READY
#
# ✅ FIX APPLIED (Bug 3):
#    RevisitCheckView previously returned {"eligible": ..., "reason": ...}
#    but the frontend reads res.is_revisit_eligible and res.message.
#    Renamed keys so both existing bill list AND the BillCreator revisit
#    flow work correctly without any frontend changes.
# ═════════════════════════════════════════════════════════════════════════════

from rest_framework.views import APIView
from rest_framework.generics import (
    CreateAPIView,
    ListAPIView,
    RetrieveUpdateAPIView,
    RetrieveAPIView,
)
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
from decimal import Decimal
import logging

from .models import Patient, ConsultationBill, ConsultationPreBooking, patient_is_revisit_eligible
from .serializers import (
    PatientSerializer,
    ConsultationBillSerializer,
    RevisitCheckSerializer,
    ConsultationPreBookingSerializer,
    ConsultationPreBookingWriteSerializer,
)
from authentication.permissions import IsAdminOrReceptionist

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# PATIENT VIEWS
# ═════════════════════════════════════════════════════════════════════════════

class CreatePatientView(CreateAPIView):
    """
    POST /reception/patients/create/
    Register a new patient. Auto-generates MRD number.
    """
    serializer_class = PatientSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def perform_create(self, serializer):
        from authentication.utils import resolve_branch_for_write

        branch, error = resolve_branch_for_write(self.request, required=True)
        if error:
            # CreateAPIView has no built-in hook to short-circuit with a
            # custom Response from perform_create, so raise instead — DRF's
            # exception handler turns this into the same shape of error
            # response the rest of the branch-write helpers return.
            from rest_framework.exceptions import ValidationError
            raise ValidationError(error.data.get("errors", {"branch": "Branch is required."}))

        serializer.save(branch=branch)
        logger.info(
            "Patient %s created by %s",
            serializer.instance.mrd_number,
            self.request.user.username,
        )


class PatientListView(ListAPIView):
    """
    GET /reception/patients/
    List all patients with search & filter support.

    Query Parameters:
        search  - Search term
        field   - Which field to search: name | mrd | phone | place
                  (defaults to searching name + MRD + phone together)
        gender  - Filter by gender
        ordering - Sort field (default: -created_at)
    """
    serializer_class = PatientSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def get_queryset(self):
        from authentication.utils import scope_queryset_to_branch

        qs = (
            Patient.objects
            .select_related('assigned_doctor', 'assigned_doctor__staff', 'assigned_doctor__staff__user')
            .order_by('-created_at')
        )
        qs = scope_queryset_to_branch(qs, self.request.user, branch_id=self.request.query_params.get('branch'))

        search = self.request.query_params.get('search', '').strip()
        # ✅ FIX: the Patients page lets reception pick which field to search
        # (name / MRD / phone / place) and sends it as `field`, but this
        # view previously only ever read a combined `search` param and had
        # no place-name filtering at all — so "Search by place" (and any
        # field-scoped search) silently returned everything, looking broken.
        field = self.request.query_params.get('field', '').strip().lower()
        if search:
            if field == 'name':
                qs = qs.filter(Q(first_name__icontains=search) | Q(last_name__icontains=search))
            elif field == 'mrd':
                qs = qs.filter(mrd_number__icontains=search)
            elif field == 'phone':
                qs = qs.filter(phone__icontains=search)
            elif field == 'place':
                qs = qs.filter(place__icontains=search)
            else:
                qs = qs.filter(
                    Q(first_name__icontains=search)
                    | Q(last_name__icontains=search)
                    | Q(mrd_number__icontains=search)
                    | Q(phone__icontains=search)
                    | Q(place__icontains=search)
                )

        gender = self.request.query_params.get('gender', '').strip()
        if gender:
            qs = qs.filter(gender=gender)

        ordering = self.request.query_params.get('ordering', '-created_at')
        allowed_ordering = [
            'created_at', '-created_at',
            'first_name', '-first_name',
            'mrd_number', '-mrd_number',
        ]
        if ordering in allowed_ordering:
            qs = qs.order_by(ordering)

        return qs


class PatientDetailView(RetrieveUpdateAPIView):
    """
    GET / PATCH / PUT  /reception/patients/<pk>/
    Retrieve or update a single patient record.
    """
    serializer_class = PatientSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def get_queryset(self):
        from authentication.utils import scope_queryset_to_branch

        qs = Patient.objects.select_related(
            'assigned_doctor',
            'assigned_doctor__staff',
            'assigned_doctor__staff__user',
        )
        return scope_queryset_to_branch(qs, self.request.user)


# ═════════════════════════════════════════════════════════════════════════════
# REVISIT CHECK VIEW
# ═════════════════════════════════════════════════════════════════════════════

class RevisitCheckView(APIView):
    """
    GET /reception/revisit-check/?patient_id=<id>
    Check whether a patient is eligible for a free revisit.

    Returns the latest NEW consultation bill and a flag indicating
    whether the revisit window is still active.

    ✅ FIX (Bug 3): Response now uses keys that match the frontend:
        - "is_revisit_eligible"  (was "eligible")
        - "message"              (was "reason")
    """
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def get(self, request):
        patient_id = request.query_params.get('patient_id')
        if not patient_id:
            return Response(
                {"detail": "patient_id query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from authentication.utils import scope_queryset_to_branch

        try:
            patient = scope_queryset_to_branch(Patient.objects.all(), request.user).get(pk=patient_id)
        except Patient.DoesNotExist:
            return Response(
                {"detail": "Patient not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        today = timezone.localdate()

        # Find latest NEW bill with an active revisit window.
        # Cancelled appointments are excluded — a cancelled visit never
        # actually happened, so it can't seed a free-revisit window for
        # the patient's next real visit (see patient_is_revisit_eligible
        # docstring for the same rule, kept in sync here).
        latest_new_bill = (
            ConsultationBill.objects
            .filter(
                patient=patient,
                consultation_type='NEW',
            )
            .exclude(consultation__status='CANCELLED')
            .order_by('-consultation_date')
            .first()
        )

        # Separately check whether the patient's actual most recent NEW
        # bill (cancelled or not) was cancelled, purely so the message
        # explains *why* there's no window instead of leaving reception
        # guessing — e.g. after cancel+refund, the next visit correctly
        # shows not-eligible, and now says why.
        most_recent_new_bill = (
            ConsultationBill.objects
            .filter(patient=patient, consultation_type='NEW')
            .order_by('-consultation_date')
            .first()
        )
        most_recent_was_cancelled = bool(
            most_recent_new_bill
            and (latest_new_bill is None or most_recent_new_bill.bill_id != latest_new_bill.bill_id)
            and getattr(getattr(most_recent_new_bill, 'consultation', None), 'status', None) == 'CANCELLED'
        )

        if not latest_new_bill:
            # ✅ FIX: keys renamed to match frontend (is_revisit_eligible, message)
            return Response({
                "is_revisit_eligible": False,
                "message": (
                    "Patient's last New consultation was cancelled — this visit "
                    "will be billed as a fresh New Consultation."
                    if most_recent_was_cancelled else
                    "No previous NEW consultation found for this patient."
                ),
                "latest_bill": None,
            })

        eligible = (
            latest_new_bill.revisit_valid_until is not None
            and today <= latest_new_bill.revisit_valid_until
        )

        # ✅ FIX: keys renamed to match frontend (is_revisit_eligible, message)
        return Response({
            "is_revisit_eligible": eligible,
            "message": (
                "Patient is within the revisit window."
                if eligible
                else (
                    "Patient's last New consultation was cancelled — this visit "
                    "will be billed as a fresh New Consultation."
                    if most_recent_was_cancelled else
                    "Revisit window has expired or has not been activated."
                )
            ),
            "latest_bill": RevisitCheckSerializer(latest_new_bill).data,
        })


# ═════════════════════════════════════════════════════════════════════════════
# CONSULTATION BILL VIEWS
# ═════════════════════════════════════════════════════════════════════════════

class CreateConsultationBillView(CreateAPIView):
    """
    POST /reception/bills/create/
    Create a new consultation bill for a patient.
    Registration fee and total are auto-computed.
    """
    serializer_class = ConsultationBillSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def perform_create(self, serializer):
        serializer.save()
        logger.info(
            "Bill %s created for patient %s by %s",
            serializer.instance.bill_number,
            serializer.instance.patient.mrd_number,
            self.request.user.username,
        )


class ConsultationBillListView(ListAPIView):
    """
    GET /reception/bills/
    List consultation bills with search & filter support.

    Query Parameters:
        search           - Search by bill number, OP number, MRD, or patient name
        consultation_type - Filter: NEW or REVISIT
        payment_status    - Filter: PAID or PENDING
        date              - Filter by consultation_date (YYYY-MM-DD)
        ordering          - Sort field (default: -created_at)
    """
    serializer_class = ConsultationBillSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def get_queryset(self):
        from authentication.utils import scope_queryset_to_branch

        qs = (
            ConsultationBill.objects
            .select_related(
                'patient',
                'patient__assigned_doctor',
                'patient__assigned_doctor__staff',
                'patient__assigned_doctor__staff__user',
                'doctor',
                'doctor__staff',
                'doctor__staff__user',
                'guest_doctor',
                'billed_department',
                'consultation',
            )
            .order_by('-created_at')
        )
        qs = scope_queryset_to_branch(qs, self.request.user, branch_id=self.request.query_params.get('branch'))

        search = self.request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(bill_number__icontains=search)
                | Q(op_number__icontains=search)
                | Q(patient__mrd_number__icontains=search)
                | Q(patient__first_name__icontains=search)
                | Q(patient__last_name__icontains=search)
                | Q(doctor_name__icontains=search)
            )

        consultation_type = self.request.query_params.get('consultation_type', '').strip()
        if consultation_type in ('NEW', 'REVISIT', 'HOME_VISIT'):
            qs = qs.filter(consultation_type=consultation_type)

        payment_status = self.request.query_params.get('payment_status', '').strip()
        if payment_status in ('PAID', 'PENDING'):
            # CancelBillView deliberately leaves payment_status untouched on
            # cancel (see its docstring), so a cancelled appointment that was
            # already PAID/PENDING before cancellation would otherwise still
            # match here. ReceptionDashboardPage relies on this filter's
            # .count for its "Paid Bills" / "Pending Payment" stat cards, so
            # without this exclude those cards over-count cancelled
            # appointments — mirrors BillingPage.jsx's own activeBills
            # filter, which excludes consultation_status === "CANCELLED"
            # client-side for its full (unfiltered) bill list.
            qs = qs.filter(payment_status=payment_status).exclude(consultation__status='CANCELLED')

        date_str = self.request.query_params.get('date', '').strip()
        if date_str:
            qs = qs.filter(consultation_date=date_str)

        ordering = self.request.query_params.get('ordering', '-created_at')
        allowed_ordering = [
            'created_at', '-created_at',
            'consultation_date', '-consultation_date',
            'total_amount', '-total_amount',
        ]
        if ordering in allowed_ordering:
            qs = qs.order_by(ordering)

        return qs


class ConsultationBillDetailView(RetrieveUpdateAPIView):
    """
    GET / PATCH / PUT  /reception/bills/<pk>/
    Retrieve or update a single consultation bill.
    """
    serializer_class = ConsultationBillSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def get_queryset(self):
        from authentication.utils import scope_queryset_to_branch

        qs = ConsultationBill.objects.select_related(
            'patient',
            'patient__assigned_doctor',
            'doctor',
            'doctor__staff',
            'doctor__staff__user',
            'guest_doctor',
            'billed_department',
            'consultation',
        )
        return scope_queryset_to_branch(qs, self.request.user)


class MarkBillPaidView(APIView):
    """
    POST /reception/bills/<pk>/pay/
    Mark a pending bill as PAID.

    For NEW consultation bills, this also:
    - Opens the revisit window (3 days from consultation_date)
    - Sets patient.registration_fee_paid = True if registration fee was charged
    """
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def post(self, request, pk):
        from authentication.utils import scope_queryset_to_branch

        try:
            bill = (
                scope_queryset_to_branch(ConsultationBill.objects.all(), request.user)
                .select_related('patient')
                .get(pk=pk)
            )
        except ConsultationBill.DoesNotExist:
            return Response(
                {"detail": "Bill not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if bill.payment_status == 'PAID':
            return Response(
                {"detail": "This bill is already marked as paid."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Update payment method if provided
        payment_method = request.data.get('payment_method', bill.payment_method)
        if payment_method == 'UPI':
            upi_ref = request.data.get('upi_reference', bill.upi_reference)
            if not upi_ref:
                return Response(
                    {"upi_reference": "UPI reference number is required for UPI payment."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            bill.upi_reference = upi_ref
        bill.payment_method = payment_method

        # Mark as paid
        bill.payment_status = 'PAID'

        # For NEW consultations, set the revisit window: today (the
        # consultation day) + tomorrow + day after tomorrow = 3 calendar
        # days total, so revisit_valid_until is consultation_date + 2.
        # After this window closes, checkFollowUp()/RevisitCheckView reports
        # is_revisit_eligible=False and the frontend defaults the next bill
        # for this patient back to consultation_type=NEW automatically.
        if bill.consultation_type == 'NEW':
            consultation_date = bill.consultation_date or timezone.localdate()
            bill.revisit_valid_until = consultation_date + timedelta(days=2)

        # FIX: save the bill BEFORE flipping patient.registration_fee_paid.
        # ConsultationBill.save() recomputes registration_fee from
        # self.patient.registration_fee_paid on every save. Flipping the
        # flag first made that recompute see "already paid" and zero out
        # registration_fee (and total_amount) on the very bill meant to
        # carry it -- silently dropping the MRD fee the moment the bill
        # was marked PAID. Saving first preserves the fee that was already
        # computed at bill-creation time.
        bill.save()

        # If registration fee was charged, mark patient as fee-paid permanently
        # so it is never charged again on any future bill for this MRD.
        if bill.registration_fee and bill.registration_fee > Decimal('0'):
            patient = bill.patient
            if not patient.registration_fee_paid:
                patient.registration_fee_paid = True
                patient.save(update_fields=['registration_fee_paid'])

        logger.info(
            "Bill %s marked PAID by %s (method: %s)",
            bill.bill_number,
            request.user.username,
            bill.payment_method,
        )

        return Response(
            ConsultationBillSerializer(bill).data,
            status=status.HTTP_200_OK,
        )


# ═════════════════════════════════════════════════════════════════════════════
# PATIENT CONSULTATION HISTORY VIEW
# ═════════════════════════════════════════════════════════════════════════════

class PatientConsultationHistoryView(ListAPIView):
    """
    GET /reception/patients/<patient_id>/history/
    List all consultation bills for a specific patient.
    """
    serializer_class = ConsultationBillSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def get_queryset(self):
        from authentication.utils import scope_queryset_to_branch

        patient_id = self.kwargs.get('patient_id')
        qs = (
            ConsultationBill.objects
            .filter(patient_id=patient_id)
            .select_related(
                'patient',
                'doctor',
                'doctor__staff',
                'doctor__staff__user',
                'guest_doctor',
            )
            .order_by('-consultation_date', '-created_at')
        )
        return scope_queryset_to_branch(qs, self.request.user)


# ═════════════════════════════════════════════════════════════════════════════
# ACTIVE DOCTORS FOR RECEPTION DROPDOWN
# ═════════════════════════════════════════════════════════════════════════════

class ActiveDoctorsForReceptionView(APIView):
    """
    GET /reception/active-doctors/
    Return a list of active doctors (both registered and guest) for the
    bill-creation form dropdown.
    """
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def get(self, request):
        from doctor.models import DoctorProfile
        from administration.models import GuestDoctorProfile
        from authentication.utils import scope_queryset_to_branch, is_group_admin_user, get_user_branch
        from django.db.models import Q

        # Registered (staff) doctors
        registered = []
        doctor_qs = scope_queryset_to_branch(
            DoctorProfile.objects.select_related('staff', 'staff__user', 'specialty'),
            request.user,
            branch_field='staff__branch',
        ).filter(staff__is_active=True, is_available=True)
        for doc in doctor_qs:
            registered.append({
                'type': 'registered',
                'profile_id': doc.profile_id,
                'name': f"Dr. {doc.staff.user.get_full_name() or doc.staff.user.username}",
                # `specialization` (the deprecated free-text field on
                # DoctorProfile) is never read here — this sources from the
                # `specialty` FK, falling back to '' if it hasn't been set yet.
                'specialty_name': doc.specialty.name if doc.specialty_id else '',
                'department': doc.department or '',
                'consultation_fee': str(doc.consultation_fee),
            })

        # Guest / visiting doctors — same nullable-branch rule as
        # administration's guest doctor endpoints: a branch's reception
        # sees their own branch's guests plus the cross-branch ones.
        guest_qs = GuestDoctorProfile.objects.filter(is_active=True)
        if not is_group_admin_user(request.user):
            branch = get_user_branch(request.user)
            guest_qs = guest_qs.filter(Q(branch=branch) | Q(branch__isnull=True)) if branch else guest_qs.filter(branch__isnull=True)

        guests = []
        for guest in guest_qs:
            guests.append({
                'type': 'guest',
                'guest_doctor_id': guest.guest_doctor_id,
                'guest_code': guest.guest_code,
                'name': f"Dr. {guest.full_name}" if not guest.full_name.lower().startswith('dr') else guest.full_name,
                # GuestDoctorProfile.specialization is its own always-free-text
                # field (not part of the DoctorProfile.specialty migration) —
                # just surfaced under the same key name as the registered-
                # doctor branch above for a consistent response shape.
                'specialty_name': guest.specialization or '',
                'department': guest.department or '',
                'consultation_fee': str(guest.consultation_fee),
            })

        return Response({
            'registered_doctors': registered,
            'guest_doctors': guests,
            'total': len(registered) + len(guests),
        })

# ═════════════════════════════════════════════════════════════════════════════
# REASSIGN DOCTOR (mid-consultation handover)
# ═════════════════════════════════════════════════════════════════════════════
#
# Scenario this exists for: a patient is billed to Doctor A and their
# Consultation has already been created/started (see ConsultationBill.save()
# — a Consultation is auto-created the moment the bill is created, before
# any clinical work happens). Partway through, Doctor A becomes unavailable
# (called away, emergency, end of shift, etc.) and reception needs to hand
# the SAME patient — with everything already entered (vitals, symptoms,
# clinical notes, chief complaint, any lab requests already made) — to
# Doctor B, without starting a new bill/consultation from scratch.
#
# This deliberately does NOT create a new ConsultationBill or Consultation.
# It re-points the existing Consultation (and the bill's doctor/doctor_name,
# so billing records and doctor queues agree) to the new doctor, leaving
# every clinical field on the Consultation untouched. Doctor B then simply
# sees this consultation in their own queue, picking up exactly where
# Doctor A left off.
class ReassignBillDoctorView(APIView):
    """
    POST /reception/bills/<pk>/reassign-doctor/
    Body: { "doctor_id": <int> }        — reassign to a registered doctor, OR
          { "guest_doctor_id": <int> }  — reassign to a guest doctor
          "reason" (optional) — stored on the consultation timeline,
          e.g. "Dr. A became unavailable mid-consultation".

    Only allowed while the linked Consultation is not yet COMPLETED — once
    a consultation is finished there is nothing left to hand over.
    """
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def post(self, request, pk):
        from django.db import transaction as db_transaction
        from doctor.models import DoctorProfile, Consultation, ConsultationTimeline, ConsultationStatus
        from administration.models import GuestDoctorProfile
        from authentication.utils import scope_queryset_to_branch, is_group_admin_user, get_user_branch
        from .serializers import _with_prefix

        from authentication.utils import scope_queryset_to_branch

        try:
            bill = (
                scope_queryset_to_branch(ConsultationBill.objects.all(), request.user)
                .select_related('patient', 'doctor', 'guest_doctor')
                .get(pk=pk)
            )
        except ConsultationBill.DoesNotExist:
            return Response({"detail": "Bill not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            consultation = bill.consultation
        except Consultation.DoesNotExist:
            return Response(
                {"detail": "This bill has no linked consultation to reassign."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if consultation.status == ConsultationStatus.COMPLETED:
            return Response(
                {"detail": "This consultation is already completed and cannot be reassigned."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        doctor_id = request.data.get('doctor_id')
        guest_doctor_id = request.data.get('guest_doctor_id')
        reason = (request.data.get('reason') or '').strip()

        if doctor_id and guest_doctor_id:
            return Response(
                {"detail": "Provide either doctor_id or guest_doctor_id, not both."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not doctor_id and not guest_doctor_id:
            return Response(
                {"detail": "doctor_id or guest_doctor_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_doctor_name = bill.doctor_name
        old_doctor_profile_id = consultation.doctor_id

        if doctor_id:
            if bill.doctor_id and int(doctor_id) == bill.doctor_id:
                return Response(
                    {"detail": "This bill is already assigned to that doctor."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                new_doctor = (
                    scope_queryset_to_branch(DoctorProfile.objects.all(), request.user, branch_field='staff__branch')
                    .select_related('staff', 'staff__user')
                    .get(pk=doctor_id)
                )
            except DoctorProfile.DoesNotExist:
                return Response({"doctor_id": "Doctor not found."}, status=status.HTTP_404_NOT_FOUND)

            if not new_doctor.staff or not new_doctor.staff.is_active:
                return Response(
                    {"doctor_id": "The selected doctor's staff account is inactive."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not new_doctor.is_available:
                return Response(
                    {"doctor_id": "The selected doctor is currently marked unavailable."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            new_doctor_name = _with_prefix(
                new_doctor.staff.user.get_full_name() or new_doctor.staff.user.username
            )
            new_doctor_user = new_doctor.staff.user

            with db_transaction.atomic():
                bill.doctor = new_doctor
                bill.guest_doctor = None
                bill.doctor_name = new_doctor_name
                bill.save()

                # Keep Patient.assigned_doctor from going stale if it was
                # pointing at the outgoing doctor — see doctor/views.py
                # _can_access_patient() for why this isn't required for
                # access (that's gated on the Consultation itself below),
                # this is purely to stop reception's own patient views from
                # showing a doctor who's no longer actually treating them.
                patient = bill.patient
                if patient.assigned_doctor_id and patient.assigned_doctor_id == old_doctor_profile_id:
                    patient.assigned_doctor = new_doctor
                    patient.save(update_fields=['assigned_doctor'])

                consultation.doctor = new_doctor
                consultation.guest_doctor = None
                consultation.doctor_user = new_doctor_user
                consultation.save(update_fields=['doctor', 'guest_doctor', 'doctor_user', 'updated_at'])

                ConsultationTimeline.objects.create(
                    consultation=consultation,
                    actor=request.user,
                    event='OTHER',
                    description=(
                        f"Reassigned from {old_doctor_name or 'Unassigned'} to {new_doctor_name} "
                        f"by reception." + (f" Reason: {reason}" if reason else "")
                    ),
                )

        else:  # guest_doctor_id
            if bill.guest_doctor_id and int(guest_doctor_id) == bill.guest_doctor_id:
                return Response(
                    {"detail": "This bill is already assigned to that guest doctor."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                guest_qs = GuestDoctorProfile.objects.all()
                if not is_group_admin_user(request.user):
                    branch = get_user_branch(request.user)
                    guest_qs = guest_qs.filter(Q(branch=branch) | Q(branch__isnull=True)) if branch else guest_qs.filter(branch__isnull=True)
                new_guest = guest_qs.get(pk=guest_doctor_id)
            except GuestDoctorProfile.DoesNotExist:
                return Response({"guest_doctor_id": "Guest doctor not found."}, status=status.HTTP_404_NOT_FOUND)

            if not new_guest.is_active:
                return Response(
                    {"guest_doctor_id": "The selected guest doctor is not active."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            new_doctor_name = _with_prefix(new_guest.full_name)
            new_doctor_user = new_guest.user if new_guest.user_id else None

            with db_transaction.atomic():
                bill.doctor = None
                bill.guest_doctor = new_guest
                bill.doctor_name = new_doctor_name
                bill.save()

                patient = bill.patient
                if patient.assigned_doctor_id and patient.assigned_doctor_id == old_doctor_profile_id:
                    patient.assigned_doctor = None
                    patient.save(update_fields=['assigned_doctor'])

                consultation.doctor = None
                consultation.guest_doctor = new_guest
                consultation.doctor_user = new_doctor_user
                consultation.save(update_fields=['doctor', 'guest_doctor', 'doctor_user', 'updated_at'])

                ConsultationTimeline.objects.create(
                    consultation=consultation,
                    actor=request.user,
                    event='OTHER',
                    description=(
                        f"Reassigned from {old_doctor_name or 'Unassigned'} to {new_doctor_name} "
                        f"by reception." + (f" Reason: {reason}" if reason else "")
                    ),
                )

        logger.info(
            "Bill %s reassigned from %s to %s by %s%s",
            bill.bill_number, old_doctor_name, new_doctor_name, request.user.username,
            f" (reason: {reason})" if reason else "",
        )

        bill.refresh_from_db()
        return Response(
            {
                "message": f"Reassigned to {new_doctor_name}. All existing consultation data was preserved.",
                "data": ConsultationBillSerializer(bill).data,
            },
            status=status.HTTP_200_OK,
        )


class CancelBillView(APIView):
    """
    POST /reception/bills/<pk>/cancel/
    Body (optional):
      {
        "reason": "Patient did not show up",
        "refund_registration_fee": true | false
      }

    Cancels an appointment (consultation bill). Only allowed while the linked
    Consultation is still in its initial 'STARTED' state — i.e. the doctor
    has not yet opened/acted on it in any way (no lab request, no follow-up,
    not completed). Once a doctor has begun working the case there's clinical
    data on record and it should be handled as a real consultation outcome,
    not silently cancelled.

    Note: this does not touch payment_status itself — that still needs to be
    reconciled through the normal financial process (e.g. cash handed back).
    This endpoint only stops the appointment from being treated as active
    going forward, and — when the bill carried the one-time MRD registration
    fee and was already PAID — records whether that fee is being refunded:

      - refund_registration_fee=true:  patient.registration_fee_paid is
        reset to False, so the MRD registration fee will be charged again
        on this patient's next visit (the collected fee is being handed
        back, so it's treated as never having been paid).
      - refund_registration_fee=false (or omitted): patient.registration_fee_paid
        is left as-is (True) — the fee is kept, registration stands, and the
        patient will never be charged it again.

    This only applies when the bill actually carried and collected the
    registration fee (registration_fee > 0 and payment_status == 'PAID').
    For any other bill, refund_registration_fee is ignored.
    """
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def post(self, request, pk):
        from doctor.models import Consultation, ConsultationTimeline, ConsultationStatus
        from authentication.utils import scope_queryset_to_branch

        try:
            bill = (
                scope_queryset_to_branch(ConsultationBill.objects.all(), request.user)
                .select_related('patient')
                .get(pk=pk)
            )
        except ConsultationBill.DoesNotExist:
            return Response({"detail": "Bill not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            consultation = bill.consultation
        except Consultation.DoesNotExist:
            return Response(
                {"detail": "This bill has no linked consultation to cancel."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if consultation.status == ConsultationStatus.CANCELLED:
            return Response(
                {"detail": "This appointment is already cancelled."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if consultation.status != ConsultationStatus.STARTED:
            return Response(
                {"detail": "This consultation is already in progress and can no longer be cancelled."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reason = (request.data.get('reason') or '').strip()

        # MRD registration fee handling — only relevant when this bill
        # actually carried the fee and it was already collected.
        has_mrd_fee = bool(bill.registration_fee and bill.registration_fee > Decimal('0'))
        mrd_fee_collected = has_mrd_fee and bill.payment_status == 'PAID'
        refund_registration_fee = bool(request.data.get('refund_registration_fee', False))
        mrd_note = None

        if mrd_fee_collected:
            patient = bill.patient
            if refund_registration_fee:
                if patient.registration_fee_paid:
                    patient.registration_fee_paid = False
                    patient.save(update_fields=['registration_fee_paid'])
                mrd_note = f"MRD registration fee (₹{bill.registration_fee}) refunded on cancellation."
            else:
                mrd_note = f"MRD registration fee (₹{bill.registration_fee}) retained — no refund on cancellation."

        # A cancelled appointment never actually happened clinically, so it
        # must not be able to grant a free "revisit" window for the
        # patient's next real visit. Clear it here on the bill itself
        # (belt) in addition to patient_is_revisit_eligible() / the REVISIT
        # eligibility checks already excluding cancelled bills (braces) —
        # so if the patient comes back the very next day, the system
        # correctly defaults back to a NEW consultation and charges the
        # consultation fee (and, if refunded above, the MRD fee) again,
        # instead of silently treating it as a free revisit.
        if bill.revisit_valid_until is not None:
            bill.revisit_valid_until = None
            bill.save(update_fields=['revisit_valid_until'])

        consultation.status = ConsultationStatus.CANCELLED
        consultation.save(update_fields=['status', 'updated_at'])

        description = "Appointment cancelled by reception."
        if reason:
            description += f" Reason: {reason}"
        if mrd_note:
            description += f" {mrd_note}"

        ConsultationTimeline.objects.create(
            consultation=consultation,
            actor=request.user,
            event='CANCELLED',
            description=description,
        )

        logger.info(
            "Bill %s (consultation %s) cancelled by %s%s%s",
            bill.bill_number, consultation.consultation_id, request.user.username,
            f" (reason: {reason})" if reason else "",
            f" ({mrd_note})" if mrd_note else "",
        )

        bill.refresh_from_db()
        return Response(
            {
                "message": "Appointment cancelled.",
                "mrd_fee_refunded": bool(mrd_fee_collected and refund_registration_fee),
                "data": ConsultationBillSerializer(bill).data,
            },
            status=status.HTTP_200_OK,
        )



# ═════════════════════════════════════════════════════════════════════════════
# CONSULTATION PRE-BOOKING VIEWS
# ═════════════════════════════════════════════════════════════════════════════

class ConsultationPreBookingListCreateView(APIView):
    """
    GET  /reception/prebookings/  — list with filters: date, doctor_id, status, payment_status, booking_mode
    POST /reception/prebookings/  — create a booking (pay_now: true/false in body)
    """
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def get(self, request):
        from authentication.utils import scope_queryset_to_branch

        qs = (
            ConsultationPreBooking.objects
            .select_related('patient', 'doctor', 'doctor__staff', 'doctor__staff__user', 'guest_doctor', 'converted_bill', 'created_by')
            .order_by('-requested_date', '-requested_time')
        )
        qs = scope_queryset_to_branch(qs, request.user, branch_id=request.query_params.get('branch'))

        date_str = request.query_params.get('date', '').strip()
        if date_str:
            qs = qs.filter(requested_date=date_str)

        doctor_id = request.query_params.get('doctor_id', '').strip()
        if doctor_id:
            qs = qs.filter(doctor_id=doctor_id)

        # Lets reception isolate bookings placed by patients directly on the
        # public website (booking_mode='WEBSITE') from staff-entered
        # CALL/WALKIN bookings — see PreBookingsPage's Mode filter.
        booking_mode = request.query_params.get('booking_mode', '').strip().upper()
        if booking_mode:
            qs = qs.filter(booking_mode=booking_mode)

        booking_status = request.query_params.get('status', '').strip().upper()
        if booking_status:
            qs = qs.filter(status=booking_status)

        payment_status = request.query_params.get('payment_status', '').strip().upper()
        if payment_status:
            qs = qs.filter(payment_status=payment_status)

        serializer = ConsultationPreBookingSerializer(qs, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = ConsultationPreBookingWriteSerializer(
            data=request.data, context={'request': request}
        )
        if serializer.is_valid():
            booking = serializer.save()
            return Response(
                {
                    'message': f'Prebooking #{booking.prebooking_id} created.',
                    'data': ConsultationPreBookingSerializer(booking).data,
                },
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ConsultationPreBookingDetailView(APIView):
    """GET /reception/prebookings/<id>/"""
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def get_object(self, pk):
        from authentication.utils import scope_queryset_to_branch

        try:
            return (
                scope_queryset_to_branch(ConsultationPreBooking.objects.all(), self.request.user)
                .select_related('patient', 'doctor', 'guest_doctor', 'converted_bill', 'created_by')
                .get(pk=pk)
            )
        except ConsultationPreBooking.DoesNotExist:
            return None

    def get(self, request, pk):
        booking = self.get_object(pk)
        if booking is None:
            return Response({"detail": "Prebooking not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(ConsultationPreBookingSerializer(booking).data)


class ConsultationPreBookingPayView(APIView):
    """
    POST /reception/prebookings/<id>/pay/
    Collect payment for a booking when the patient arrives (or pays over
    the phone later). Sets payment_status=PAID, payment_method, paid_at.

    If this is the patient's first-ever payment (registration_fee_paid is
    still False on their Patient record — always true for a patient just
    auto-created by the public website's MRD-less booking flow), the
    one-time MRD registration fee is computed, added on top of
    consultation_fee, and locked into registration_fee on this row. This
    is the FIRST point money changes hands for the booking, so it's the
    right place to collect the registration fee rather than deferring it
    to Convert — the patient shouldn't be shown "consultation fee only"
    here and then hit with an extra charge later.
    """
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def post(self, request, pk):
        from authentication.utils import scope_queryset_to_branch

        try:
            booking = (
                scope_queryset_to_branch(ConsultationPreBooking.objects.all(), request.user)
                .select_related('patient')
                .get(pk=pk)
            )
        except ConsultationPreBooking.DoesNotExist:
            return Response({"detail": "Prebooking not found."}, status=status.HTTP_404_NOT_FOUND)

        if booking.status in ('CANCELLED', 'NO_SHOW'):
            return Response(
                {"detail": f"Cannot collect payment on a {booking.status.lower()} booking."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if booking.payment_status == 'PAID':
            return Response({"detail": "This booking is already paid."}, status=status.HTTP_400_BAD_REQUEST)

        payment_method = request.data.get('payment_method')
        if payment_method not in ('CASH', 'UPI'):
            return Response(
                {"payment_method": "payment_method must be CASH or UPI."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        registration_fee = booking.compute_registration_fee()

        from django.db import transaction as db_transaction
        with db_transaction.atomic():
            booking.registration_fee = registration_fee
            booking.payment_method = payment_method
            booking.payment_status = 'PAID'
            booking.paid_at = timezone.now()
            booking.save(update_fields=['registration_fee', 'payment_method', 'payment_status', 'paid_at', 'updated_at'])

            if registration_fee and registration_fee > Decimal('0') and booking.patient_id:
                if not booking.patient.registration_fee_paid:
                    booking.patient.registration_fee_paid = True
                    booking.patient.save(update_fields=['registration_fee_paid'])

        logger.info(
            "Prebooking #%s marked PAID by %s (method: %s, consultation_fee: %s, registration_fee: %s)",
            booking.prebooking_id, request.user.username, payment_method,
            booking.consultation_fee, registration_fee,
        )

        return Response(
            {
                "message": "Payment recorded.",
                "data": ConsultationPreBookingSerializer(booking).data,
            }
        )


class ConsultationPreBookingConvertView(APIView):
    """
    POST /reception/prebookings/<id>/convert/

    Creates the real ConsultationBill from the booking on the day of the
    visit. If the booking was already PAID, the resulting bill is created
    already paid (no double charge). If still PENDING, the caller must
    supply payment_method (and upi_reference if UPI) so reception can
    collect payment as part of this same call.

    If the booking was for a brand-new (not-yet-registered) patient, a
    Patient record is created first from the quick new_patient_* fields —
    this requires new_patient_phone to be present, since Patient.phone is
    a required field.
    """
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def post(self, request, pk):
        from authentication.utils import scope_queryset_to_branch

        try:
            booking = (
                scope_queryset_to_branch(ConsultationPreBooking.objects.all(), request.user)
                .select_related('patient', 'doctor', 'guest_doctor')
                .get(pk=pk)
            )
        except ConsultationPreBooking.DoesNotExist:
            return Response({"detail": "Prebooking not found."}, status=status.HTTP_404_NOT_FOUND)

        if booking.status == 'CONVERTED':
            return Response(
                {
                    "detail": "This booking has already been converted.",
                    "bill_id": booking.converted_bill_id,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if booking.status in ('CANCELLED', 'NO_SHOW'):
            return Response(
                {"detail": f"Cannot convert a {booking.status.lower()} booking."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve / create the patient
        patient = booking.patient
        if patient is None:
            if not booking.new_patient_phone:
                return Response(
                    {"new_patient_phone": "A phone number is required to register this new patient before converting."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            name_parts = (booking.new_patient_name or '').strip().split(None, 1)
            first_name = name_parts[0] if name_parts else booking.new_patient_name or 'Unknown'
            last_name = name_parts[1] if len(name_parts) > 1 else ''
            patient = Patient.objects.create(
                branch=booking.branch,
                first_name=first_name,
                last_name=last_name,
                phone=booking.new_patient_phone,
                gender=booking.new_patient_gender,
                age=booking.new_patient_age,
            )

        # Determine payment details for the new bill.
        #
        # ✅ FIX: this view previously forced every converted bill straight to
        # PAID, even when reception hadn't actually collected any money at
        # the counter — the "Convert to Bill" dialog always required picking
        # CASH/UPI and that alone was enough to mark the bill (and the
        # booking) paid. That meant a bill created from a prebooking could
        # never come out PENDING, so it could never show up under the
        # Billing page's "Pending" tab the way a normal walk-in bill does —
        # reception had no way to convert now and collect payment later.
        #
        # `collect_payment` (default True, for backward compatibility with
        # existing callers) lets the frontend explicitly say whether payment
        # is being collected as part of this conversion. When it's False,
        # the bill is created and left at its normal PENDING default (same
        # as any bill created via the regular "New Bill" flow) so it shows
        # up in Billing → Pending and can be marked paid from there.
        if booking.payment_status == 'PAID':
            payment_method = booking.payment_method or 'CASH'
            collect_payment = True
        else:
            payment_method = request.data.get('payment_method') or 'CASH'
            if payment_method not in ('CASH', 'UPI'):
                return Response(
                    {"payment_method": "payment_method must be CASH or UPI."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            collect_payment = bool(request.data.get('collect_payment', True))
            if collect_payment and payment_method == 'UPI' and not request.data.get('upi_reference'):
                return Response(
                    {"upi_reference": "UPI reference is required to collect payment now."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Determine consultation type and fee for the converted bill.
        # Allow the caller to override them (e.g. to charge a NEW fee even if inside revisit window).
        consultation_type = request.data.get('consultation_type')
        if not consultation_type:
            consultation_type = booking.consultation_type

        # Double check/enforce revisit eligibility if REVISIT is requested
        if consultation_type == 'REVISIT':
            is_eligible = patient.patient_id is not None and patient_is_revisit_eligible(patient)
            if not is_eligible:
                consultation_type = 'NEW'

        if consultation_type == 'REVISIT':
            fee = Decimal('0')
        elif consultation_type == 'HOME_VISIT':
            # Home Visit fee was locked in at booking time (mirrors the
            # guest-doctor branch below) — never re-derived from a doctor's
            # standard consultation_fee, since Home Visit pricing is
            # independent of the per-doctor rate.
            req_fee = request.data.get('consultation_fee')
            fee = Decimal(str(req_fee)) if req_fee is not None else (booking.consultation_fee or Decimal('0'))
        else:
            # For a NEW consultation, check if the client supplied a custom fee
            req_fee = request.data.get('consultation_fee')
            if req_fee is not None:
                fee = Decimal(str(req_fee))
            elif booking.doctor_id:
                # Registered doctor: fee MUST match doctor's current rate (enforced by serializer)
                fee = booking.doctor.consultation_fee
            elif booking.guest_doctor_id:
                # Guest doctor: honor the price locked in the booking, or default to profile rate
                fee = booking.consultation_fee if booking.consultation_fee and booking.consultation_fee > 0 else booking.guest_doctor.consultation_fee
            else:
                # Manual entry
                fee = booking.consultation_fee or Decimal('0')

        bill_payload = {
            'patient': patient.patient_id,
            'consultation_type': consultation_type,
            # REVISIT consultations must be billed at ₹0 (enforced by
            # ConsultationBill.clean()); NEW/HOME_VISIT carry the resolved
            # fee above.
            'consultation_fee': fee,
            'payment_method': payment_method,
        }
        if consultation_type == 'HOME_VISIT':
            req_travel = request.data.get('travel_charge')
            bill_payload['travel_charge'] = Decimal(str(req_travel)) if req_travel is not None else (booking.travel_charge or Decimal('0'))
        if booking.doctor_id:
            bill_payload['doctor'] = booking.doctor_id
        if booking.guest_doctor_id:
            bill_payload['guest_doctor'] = booking.guest_doctor_id
        if payment_method == 'UPI' and collect_payment:
            bill_payload['upi_reference'] = request.data.get('upi_reference', '') or 'PREBOOKING'

        bill_serializer = ConsultationBillSerializer(data=bill_payload, context={'request': request})
        if not bill_serializer.is_valid():
            return Response(bill_serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        bill = bill_serializer.save()

        # Only actually settle the bill when payment is being collected right
        # now (either it was already PAID ahead of time, or collect_payment
        # was explicitly requested for this call). Otherwise leave the bill
        # at its normal PENDING default so it behaves exactly like any other
        # freshly-created bill and shows up in Billing → Pending until
        # someone marks it paid from there.
        if collect_payment:
            if bill.payment_status != 'PAID':
                bill.payment_status = 'PAID'
                if bill.consultation_type == 'NEW':
                    bill.revisit_valid_until = (booking.requested_date) + timedelta(days=2)
                bill.save()
            if bill.registration_fee and bill.registration_fee > Decimal('0') and not patient.registration_fee_paid:
                patient.registration_fee_paid = True
                patient.save(update_fields=['registration_fee_paid'])

        booking.status = 'CONVERTED'
        booking.converted_bill = bill
        if collect_payment:
            booking.payment_status = 'PAID'
            booking.payment_method = payment_method
            if not booking.paid_at:
                booking.paid_at = timezone.now()
        if booking.patient_id is None:
            booking.patient = patient
        booking.save(update_fields=[
            'status', 'converted_bill', 'patient',
            'payment_status', 'payment_method', 'paid_at', 'updated_at',
        ])

        logger.info(
            "Prebooking #%s converted to bill %s by %s",
            booking.prebooking_id, bill.bill_number, request.user.username,
        )

        return Response(
            {
                "message": f"Prebooking converted to bill {bill.bill_number}.",
                "bill": ConsultationBillSerializer(bill).data,
                "data": ConsultationPreBookingSerializer(booking).data,
            },
            status=status.HTTP_200_OK,
        )


class ConsultationPreBookingCancelView(APIView):
    """POST /reception/prebookings/<id>/cancel/"""
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def post(self, request, pk):
        from authentication.utils import scope_queryset_to_branch

        try:
            booking = scope_queryset_to_branch(ConsultationPreBooking.objects.all(), request.user).get(pk=pk)
        except ConsultationPreBooking.DoesNotExist:
            return Response({"detail": "Prebooking not found."}, status=status.HTTP_404_NOT_FOUND)

        if booking.status == 'CONVERTED':
            return Response(
                {"detail": "This booking has already been converted and cannot be cancelled."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if booking.status == 'CANCELLED':
            return Response({"detail": "This booking is already cancelled."}, status=status.HTTP_400_BAD_REQUEST)

        booking.status = 'CANCELLED'
        booking.save(update_fields=['status', 'updated_at'])

        logger.info("Prebooking #%s cancelled by %s", booking.prebooking_id, request.user.username)

        return Response(
            {
                "message": "Prebooking cancelled.",
                "data": ConsultationPreBookingSerializer(booking).data,
            }
        )


# ═════════════════════════════════════════════════════════════════════════════
# PHARMACY BILLS (sent from the pharmacy module)
#
# A pharmacist can forward a PAID (paid + dispensed) pharmacy bill here
# via POST /api/pharmacist/bills/<id>/send-to-reception/. These two
# views let reception staff see and print what's been forwarded to
# them, without granting reception the full pharmacist bill-management
# permissions (create/edit/cancel bills, etc. stay pharmacist-only).
# ═════════════════════════════════════════════════════════════════════════════

class ReceptionPharmacyBillListView(APIView):
    """
    GET /api/reception/pharmacy-bills/

    Lists pharmacy bills that a pharmacist has sent to reception.
    Supports:
    - date_from / date_to : YYYY-MM-DD — filters on bill_date (inclusive)
    - search               : bill number, patient/walk-in name
    """
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def get(self, request):
        # Local import: keeps reception/pharmacist decoupled at module-load
        # time (pharmacist already depends on reception via its
        # PharmacyBill.patient/consultation_bill FKs, so importing the
        # other way at call time avoids any circular-import risk).
        from pharmacist.models import PharmacyBill
        from pharmacist.serializers import PharmacyBillSerializer
        from authentication.utils import scope_queryset_to_branch

        qs = (
            PharmacyBill.objects
            .filter(sent_to_reception=True)
            .select_related('patient', 'sent_to_reception_by')
            .prefetch_related('medicine_items', 'procedure_items', 'general_items')
            .order_by('-sent_to_reception_at')
        )
        qs = scope_queryset_to_branch(qs, request.user, branch_id=request.query_params.get('branch'))

        date_from = request.query_params.get('date_from')
        if date_from:
            qs = qs.filter(bill_date__gte=date_from)

        date_to = request.query_params.get('date_to')
        if date_to:
            qs = qs.filter(bill_date__lte=date_to)

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(bill_number__icontains=search)          |
                Q(patient_name__icontains=search)         |
                Q(walkin_name__icontains=search)          |
                Q(patient__first_name__icontains=search)  |
                Q(patient__last_name__icontains=search)
            )

        return Response(PharmacyBillSerializer(qs, many=True).data)


class ReceptionPharmacyBillDetailView(APIView):
    """
    GET /api/reception/pharmacy-bills/<id>/

    Full detail for one pharmacy bill (for the print page). 404s if
    this bill was never sent to reception — reception can only see
    bills a pharmacist explicitly forwarded, not the full pharmacy
    bill list.
    """
    permission_classes = [IsAuthenticated, IsAdminOrReceptionist]

    def get(self, request, pk):
        from django.shortcuts import get_object_or_404
        from pharmacist.models import PharmacyBill
        from pharmacist.serializers import PharmacyBillSerializer
        from authentication.utils import scope_queryset_to_branch

        qs = scope_queryset_to_branch(
            PharmacyBill.objects.filter(sent_to_reception=True), request.user
        )
        bill = get_object_or_404(qs, pk=pk)
        return Response(PharmacyBillSerializer(bill).data)