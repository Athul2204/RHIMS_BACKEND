# pharmacist/views.py - COMPLETE FIXED VERSION
# ═════════════════════════════════════════════════════════════════════════════
# 
# ✅ ALL DUPLICATES REMOVED
# ✅ PROPER IMPORT ORGANIZATION
# ✅ BatchReturnToProviderView INCLUDED
# ✅ MedicineReturnToProvider views properly implemented (best versions)
# ✅ All existing views maintained and functional
# ✅ Production Ready
#
# ═════════════════════════════════════════════════════════════════════════════
from rest_framework.decorators import api_view, permission_classes
from django.db import transaction, models, IntegrityError
from django.db.models import Q, Sum, Case, When, IntegerField
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError
from django.utils import timezone
from datetime import timedelta, date, datetime, time
import logging

logger = logging.getLogger(__name__)


def _safe_detail(exc):
    """Only expose exception text to the client when DEBUG=True; full detail
    always goes to the server log at the call site. See doctor/views.py's
    identical helper for rationale."""
    from django.conf import settings
    return str(exc) if settings.DEBUG else "An internal error occurred. Please try again or contact support."


def _scoped_bill_or_404(request, bill_id, qs=None):
    """
    Branch-scoped PharmacyBill lookup — every bill-action endpoint below
    (add/remove item, transition, complete, mark-paid, cancel, etc.) must
    use this instead of a raw get_object_or_404(PharmacyBill, pk=bill_id),
    otherwise an ordinary (non-group-admin) pharmacist at one branch can
    read/act on another branch's bill just by guessing/incrementing the id.
    """
    base = qs if qs is not None else PharmacyBill.objects.all()
    scoped = scope_queryset_to_branch(base, request.user, branch_field='branch')
    return get_object_or_404(scoped, pk=bill_id)


from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from authentication.permissions import IsAdminOrPharmacist
from authentication.utils import (
    scope_queryset_to_branch,
    resolve_branch_for_write,
    is_group_admin_user,
    get_user_branch,
)

from .models import (
    Medicine,
    MedicineBatch,
    MedicineStockLog,
    MedicineReturnToProvider,
    StockAlert,
    PharmacyBill,
    PharmacyBillMedicineItem,
    PharmacyBillProcedureItem,
    _recalculate_bill_totals,
    SupplyItem,
    SupplyBatch,
    SupplyUsageLog,
    SupplyStockAlert,
    SupplyReturn,
    _check_supply_alerts,
    GeneralItem,
    GeneralItemBatch,
    GeneralItemStockLog,
    GeneralItemStockAlert,
    PharmacyBillGeneralItem,
    GeneralItemReturn,
    _check_general_item_alerts,
    GeneralItemCategoryChoices,
)
from .serializers import (
    SupplyItemSerializer,
    SupplyItemWriteSerializer,
    SupplyBatchSerializer,
    SupplyBatchWriteSerializer,
    SupplyUsageLogSerializer,
    UseStockSerializer,
    SupplyStockAlertSerializer,
    SupplyReturnSerializer,
    ReturnToProviderSerializer,
)
from .serializers import (
    PharmacyBillSerializer,
    PharmacyBillMedicineItemSerializer,
    PharmacyBillProcedureItemSerializer,
    AddMedicineItemSerializer,
    AddProcedureItemSerializer,
    MarkBillPaidSerializer,
    CancelBillSerializer,
    ReopenBillSerializer,
    SetBillDiscountSerializer,
    CreateBillSerializer,
    MedicineWithBatchesSerializer,
    MedicineBatchDetailSerializer,
    MedicineBatchWriteSerializer,
    MedicineWriteSerializer,
    MedicineSerializer,
    StockAlertSerializer,
    MedicineStockLogSerializer,
    MedicineReturnToProviderSerializer,
    MedicineReturnToProviderCreateSerializer,
    PatientSearchResultSerializer,
    GeneralItemSerializer,
    GeneralItemWriteSerializer,
    GeneralItemWithBatchesSerializer,
    GeneralItemBatchDetailSerializer,
    GeneralItemBatchWriteSerializer,
    GeneralItemStockAlertSerializer,
    PharmacyBillGeneralItemSerializer,
    AddGeneralItemSerializer,
    GeneralItemReturnSerializer,
    GeneralItemReturnToProviderSerializer,
)

try:
    from administration.models import Procedure
except ImportError:
    Procedure = None


# ═══════════════════════════════════════════════════════════════════
# PATIENT SEARCH  (pharmacist looks up registered patient by MRD/name)
# GET /api/pharmacist/patients/search/
# ═══════════════════════════════════════════════════════════════════

class PatientSearchView(APIView):
    """
    GET /api/pharmacist/patients/search/

    Look up a registered patient for walk-in bill creation.

    Query params (provide at least one):
    - mrd       : exact MRD number (e.g. MRD-0042)
    - patient_id: exact patient PK
    - q         : free-text search on name or phone (partial match)

    Returns a list (usually 0-1 items for mrd/patient_id, 0-N for q).
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        from reception.models import Patient

        mrd        = request.query_params.get('mrd', '').strip()
        patient_id = request.query_params.get('patient_id', '').strip()
        q          = request.query_params.get('q', '').strip()

        if not any([mrd, patient_id, q]):
            return Response(
                {'error': 'Provide at least one of: mrd, patient_id, or q.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = Patient.objects.all()
        qs = scope_queryset_to_branch(qs, request.user, branch_field='branch', branch_id=request.query_params.get('branch'))

        if mrd:
            qs = qs.filter(mrd_number__iexact=mrd)
        elif patient_id:
            try:
                qs = qs.filter(pk=int(patient_id))
            except (ValueError, TypeError):
                return Response(
                    {'error': 'patient_id must be a valid integer.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        elif q:
            qs = qs.filter(
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q)  |
                Q(phone__icontains=q)      |
                Q(mrd_number__icontains=q)
            )

        count = qs.count()
        qs = qs.order_by('first_name', 'last_name')[:20]
        serializer = PatientSearchResultSerializer(qs, many=True)
        return Response({'count': count, 'results': serializer.data})


# ═══════════════════════════════════════════════════════════════════
# BILL LIST
# GET /api/pharmacist/bills/
# ═══════════════════════════════════════════════════════════════════

class BillListView(APIView):
    """
    GET /api/pharmacist/bills/

    Lists pharmacy bills.  Supports query params:
    - bill_status   : OPEN | COMPLETED | PAID | CANCELLED
    - payment_status: PENDING | PAID
    - patient_type  : registered | walkin   (walk-in filter)
    - patient_id    : filter by registered patient
    - prescription_id: filter by prescription
    - date_from     : YYYY-MM-DD — bills on or after this date
    - date_to       : YYYY-MM-DD — bills on or before this date
    - search        : patient name, bill number, or walk-in name
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        qs = (
            PharmacyBill.objects
            .select_related('patient', 'consultation_bill', 'prescription')
            .prefetch_related(
                'medicine_items__batch__medicine',
                'medicine_items__prescription_item',
                'procedure_items__procedure',
            )
            .order_by('-bill_id')
        )
        qs = scope_queryset_to_branch(qs, request.user, branch_field='branch', branch_id=request.query_params.get('branch'))

        # Accept both 'bill_status' and 'status' params (frontend sends 'status')
        bill_status = (
            request.query_params.get('bill_status', '')
            or request.query_params.get('status', '')
        ).upper()
        if bill_status:
            qs = qs.filter(bill_status=bill_status)

        payment_status = request.query_params.get('payment_status', '').upper()
        if payment_status:
            qs = qs.filter(payment_status=payment_status)

        # Walk-in filter
        patient_type = request.query_params.get('patient_type', '').lower()
        if patient_type == 'walkin':
            qs = qs.filter(is_walkin=True)
        elif patient_type == 'registered':
            qs = qs.filter(is_walkin=False)

        patient_id = request.query_params.get('patient_id')
        if patient_id:
            qs = qs.filter(patient_id=patient_id)

        prescription_id = request.query_params.get('prescription_id')
        if prescription_id:
            qs = qs.filter(prescription_id=prescription_id)

        date_from = request.query_params.get('date_from')
        if date_from:
            qs = qs.filter(bill_date__gte=date_from)

        date_to = request.query_params.get('date_to')
        if date_to:
            qs = qs.filter(bill_date__lte=date_to)

        # Search across registered and walk-in names
        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(bill_number__icontains=search)          |
                Q(patient_name__icontains=search)         |
                Q(walkin_name__icontains=search)          |
                Q(walkin_phone__icontains=search)         |
                Q(patient__first_name__icontains=search)  |
                Q(patient__last_name__icontains=search)
            )

        return Response(PharmacyBillSerializer(qs, many=True).data)


# ═══════════════════════════════════════════════════════════════════
# CREATE BILL  (walk-in + registered + prescription)
# POST /api/pharmacist/bills/create/
# ═══════════════════════════════════════════════════════════════════

class CreateBillView(APIView):
    """
    POST /api/pharmacist/bills/create/

    Creates a new DRAFT pharmacy bill.  Supports all patient scenarios:

    A) Registered patient by ID:
       { "patient_type": "registered", "patient_id": 12 }

    B) Registered patient by MRD:
       { "patient_type": "registered", "mrd_number": "MRD-0001" }

    C) From prescription (auto-links patient; optionally auto-adds medicines):
       { "patient_type": "registered", "prescription_id": 5,
         "auto_add_medicines": true }

    D) Walk-in patient:
       { "patient_type": "walkin",
         "walkin_name": "Rahul Kumar",
         "walkin_phone": "9876543210",
         "walkin_gender": "Male",
         "walkin_age": 45 }
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request):
        serializer = CreateBillSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data            = serializer.validated_data
        patient_type    = data['patient_type']
        notes           = data.get('notes', '')
        auto_add        = data.get('auto_add_medicines', False)

        # Walk-in path
        if patient_type == 'walkin':
            walkin_name  = (data.get('walkin_name') or '').strip()
            walkin_phone = (data.get('walkin_phone') or '').strip() or None

            # ✅ FIX: a walk-in bill has no patient to derive branch from
            # (PharmacyBill.save() only auto-sets branch from patient.branch),
            # and the model requires branch for is_walkin=True — so every
            # walk-in bill previously crashed at save() with either a
            # ValidationError or an AttributeError from the branch-prefixed
            # bill-number generator reading branch.code on None. Resolve it
            # the same way every other branch-scoped create() in this app
            # does (MedicineCreateView, SupplyItemCreateView, etc.).
            branch, error = resolve_branch_for_write(request, required=True)
            if error:
                return error

            # ── Duplicate-draft guard ─────────────────────────────────────
            # Block a new DRAFT if an active (non-CANCELLED, non-PAID) bill
            # already exists for this walk-in today.
            #
            # Matches on phone OR name (not "phone if given, else name") —
            # otherwise correcting/adding a phone number on a second attempt,
            # or fixing a typo'd name, would miss the earlier draft entirely
            # and mint a brand-new bill number instead of reusing it.
            #
            # ✅ FIX: scoped to `branch` — previously matched across every
            # branch, so a same-named/same-numbered walk-in at a different
            # branch could resume (and thus expose/edit) that other
            # branch's DRAFT bill.
            #
            # Either way return the existing bill so the frontend can resume
            # it instead of flooding the DB with orphan DRAFTs.
            from django.db.models import Q
            from django.utils import timezone as _tz
            today = _tz.localdate()

            active_statuses_excl = ['CANCELLED', 'PAID']

            existing = None
            if walkin_name or walkin_phone:
                if walkin_name and walkin_phone:
                    match_q = Q(walkin_name__iexact=walkin_name) | Q(walkin_phone=walkin_phone)
                elif walkin_phone:
                    match_q = Q(walkin_phone=walkin_phone)
                else:
                    match_q = Q(walkin_name__iexact=walkin_name)

                existing = (
                    PharmacyBill.objects
                    .filter(is_walkin=True, bill_date=today, branch=branch)
                    .filter(match_q)
                    .exclude(bill_status__in=active_statuses_excl)
                    .order_by('-bill_id')
                    .first()
                )

            if existing:
                # ── Reuse-draft logic ───────────────────────────────────────
                # If the matching bill is still sitting in DRAFT (no items
                # added / stock not yet allocated, nothing "completed" about
                # it yet), treat this as the same walk-in visit rather than
                # a conflict: refresh its details onto that same bill instead
                # of minting a new one. This keeps the pharm bill number
                # consistent even if the pharmacist re-enters/corrects the
                # walk-in's name, phone, age or gender before adding
                # medicines.
                if existing.bill_status == 'DRAFT':
                    existing.walkin_name = walkin_name or existing.walkin_name
                    existing.walkin_phone = walkin_phone or existing.walkin_phone
                    if data.get('walkin_gender'):
                        existing.walkin_gender = data.get('walkin_gender')
                    if data.get('walkin_age') is not None:
                        existing.walkin_age = data.get('walkin_age')
                    if notes:
                        existing.notes = notes
                    existing.save()

                    return Response(
                        {
                            'message': (
                                f'Existing draft bill #{existing.bill_number} updated with '
                                f'the latest details.'
                            ),
                            'bill': PharmacyBillSerializer(existing).data,
                            'resumed_draft': True,
                        },
                        status=status.HTTP_200_OK,
                    )

                # Bill has already progressed past DRAFT (OPEN/READY/COMPLETED).
                # Return 409 with the existing bill so the frontend can resume it.
                # The frontend's 409 handler reads existing_bill_id and can skip
                # re-creating the bill and jump straight to the billing/payment step.
                return Response(
                    {
                        'error': (
                            f'An active walk-in bill already exists for this patient today '
                            f'(Bill #{existing.bill_number}, status: {existing.bill_status}). '
                            f'Resuming existing bill.'
                        ),
                        'existing_bill_id':     existing.bill_id,
                        'existing_bill_number': existing.bill_number,
                        'existing_bill_status': existing.bill_status,
                        'bill': PharmacyBillSerializer(existing).data,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            # ─────────────────────────────────────────────────────────────

            try:
                bill = PharmacyBill.objects.create(
                    branch=branch,
                    is_walkin=True,
                    walkin_name=walkin_name,
                    walkin_phone=walkin_phone,
                    walkin_gender=data.get('walkin_gender') or None,
                    walkin_age=data.get('walkin_age'),
                    patient=None,
                    consultation_bill=None,
                    prescription=None,
                    bill_status='DRAFT',
                    payment_status='PENDING',
                    notes=notes,
                )
            except Exception as exc:
                return Response(
                    {'error': 'Failed to create walk-in bill.', 'detail': _safe_detail(exc)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            return Response(
                {'message': 'Walk-in pharmacy bill created.', 'bill': PharmacyBillSerializer(bill).data},
                status=status.HTTP_201_CREATED,
            )

        # Registered patient path
        prescription     = None
        consultation_bill = None
        patient          = None

        prescription_id = data.get('prescription_id')
        patient_id      = data.get('patient_id')
        mrd_number      = (data.get('mrd_number') or '').strip()

        # Resolve via prescription
        if prescription_id:
            try:
                from doctor.models import Prescription
                prescription_qs = scope_queryset_to_branch(
                    Prescription.objects.select_related(
                        'consultation__consultation_bill',
                        'consultation__patient',
                    ).prefetch_related('items__medicine'),
                    request.user, branch_field='consultation__patient__branch',
                )
                prescription = prescription_qs.get(pk=prescription_id)
            except Prescription.DoesNotExist:
                return Response(
                    {'error': f'Prescription #{prescription_id} not found.'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            except Exception as exc:
                return Response(
                    {'error': 'Database error fetching prescription.', 'detail': _safe_detail(exc)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            consultation = prescription.consultation
            try:
                consultation_bill = consultation.consultation_bill
            except Exception:
                consultation_bill = None
            patient = consultation.patient

        # Resolve via patient_id
        if patient_id:
            try:
                from reception.models import Patient
                patient_qs = scope_queryset_to_branch(Patient.objects.all(), request.user, branch_field='branch')
                patient = patient_qs.get(pk=patient_id)
            except Patient.DoesNotExist:
                return Response(
                    {'error': f'Patient #{patient_id} not found.'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            except Exception as exc:
                return Response(
                    {'error': 'Database error fetching patient.', 'detail': _safe_detail(exc)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        # Resolve via MRD number
        if mrd_number and patient is None:
            try:
                from reception.models import Patient
                mrd_qs = scope_queryset_to_branch(Patient.objects.all(), request.user, branch_field='branch')
                patient = mrd_qs.get(mrd_number__iexact=mrd_number)
            except Patient.DoesNotExist:
                return Response(
                    {'error': f"No patient found with MRD number '{mrd_number}'."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            except Exception as exc:
                return Response(
                    {'error': 'Database error fetching patient by MRD.', 'detail': _safe_detail(exc)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        # Guard: no duplicate bills for the same prescription
        if prescription:
            try:
                existing = PharmacyBill.objects.filter(
                    prescription=prescription
                ).exclude(bill_status='CANCELLED').first()
            except Exception as exc:
                return Response(
                    {
                        'error': (
                            'Database schema is outdated — prescription_id column missing. '
                            'Run: python manage.py migrate pharmacist'
                        ),
                        'detail': _safe_detail(exc),
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            if existing:
                return Response(
                    {
                        'error': (
                            f'A pharmacy bill already exists for Prescription #{prescription_id}. '
                            f'Bill #{existing.bill_id} ({existing.bill_number}) — status: {existing.bill_status}.'
                        ),
                        'existing_bill_id': existing.bill_id,
                        'existing_bill_number': existing.bill_number,
                        'existing_bill_status': existing.bill_status,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        # Guard: no duplicate open bills for the same registered patient (no prescription)
        if patient and not prescription:
            from django.utils import timezone as _tz
            today = _tz.localdate()
            existing_patient_bill = PharmacyBill.objects.filter(
                is_walkin=False,
                patient=patient,
                prescription=None,
                bill_date=today,
            ).exclude(bill_status__in=['CANCELLED', 'PAID']).first()

            if existing_patient_bill:
                return Response(
                    {
                        'error': (
                            f'An active bill already exists for this patient today '
                            f'(Bill #{existing_patient_bill.bill_number}, '
                            f'status: {existing_patient_bill.bill_status}). '
                            f'Complete or cancel it before creating a new one.'
                        ),
                        'existing_bill_id':     existing_patient_bill.bill_id,
                        'existing_bill_number': existing_patient_bill.bill_number,
                        'existing_bill_status': existing_patient_bill.bill_status,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        patient_name = f'{patient.first_name} {patient.last_name}'.strip() if patient else ''

        try:
            bill = PharmacyBill.objects.create(
                is_walkin=False,
                patient=patient,
                patient_name=patient_name,
                consultation_bill=consultation_bill,
                prescription=prescription,
                bill_status='DRAFT',
                payment_status='PENDING',
                notes=notes,
            )
        except Exception as exc:
            error_str = str(exc).lower()
            if 'prescription_id' in error_str or 'no column' in error_str or 'does not exist' in error_str:
                return Response(
                    {
                        'error': (
                            'Database schema is outdated. '
                            'Run: python manage.py migrate pharmacist'
                        ),
                        'detail': _safe_detail(exc),
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            raise

        # Auto-add prescribed medicines
        added_medicines  = []
        failed_medicines = []

        if auto_add and prescription:
            for rx_item in prescription.items.all():
                if not rx_item.medicine:
                    medicine_name_word = (rx_item.medicine_name or '').split()[0]
                    potential_qs = scope_queryset_to_branch(
                        Medicine.objects.filter(name__icontains=medicine_name_word, is_active=True) if medicine_name_word
                        else Medicine.objects.none(),
                        request.user, branch_field='branch',
                    )
                    potential = potential_qs[:5]
                    failed_medicines.append({
                        'prescription_item_id': rx_item.item_id,
                        'medicine_name': rx_item.medicine_name or 'Unknown',
                        'reason': 'Medicine not linked to a pharmacy medicine record.',
                        'suggestion': (
                            f'Doctor wrote "{rx_item.medicine_name}" but did not link it. '
                            'Ask the doctor to rewrite the prescription with a linked medicine.'
                        ),
                        'potential_matches': [
                            {'id': m.medicine_id, 'name': m.name, 'generic_name': m.generic_name}
                            for m in potential
                        ],
                    })
                    continue

                # Defensive branch check: rx_item.medicine is a
                # pharmacist.Medicine FK set by the doctor module. If it
                # somehow points at another branch's catalogue entry (e.g.
                # a doctor-side scoping slip), dispensing from it would pull
                # that other branch's stock/batch into this bill. Treat it
                # the same as "not linked" rather than trusting it blindly.
                if rx_item.medicine.branch_id != bill.branch_id:
                    failed_medicines.append({
                        'prescription_item_id': rx_item.item_id,
                        'medicine_name': rx_item.medicine_name or rx_item.medicine.name,
                        'reason': "Linked medicine belongs to a different branch's catalogue.",
                        'suggestion': 'Ask the doctor to re-link this item to a medicine from this branch.',
                    })
                    continue

                best_batch = MedicineBatch.objects.filter(
                    medicine=rx_item.medicine,
                    status='ACTIVE',
                    quantity__gte=rx_item.quantity,
                ).exclude(
                    expiry_date__lt=timezone.localdate()
                ).order_by('expiry_date', '-batch_id').first()

                if not best_batch:
                    current_stock = (
                        MedicineBatch.objects.filter(
                            medicine=rx_item.medicine, status='ACTIVE'
                        ).exclude(expiry_date__lt=timezone.localdate())
                        .aggregate(t=Sum('quantity'))['t'] or 0
                    )
                    failed_medicines.append({
                        'prescription_item_id': rx_item.item_id,
                        'medicine_name': rx_item.medicine_name or 'Unknown',
                        'reason': (
                            f'Insufficient stock (need: {rx_item.quantity}, available: {current_stock}).'
                        ),
                    })
                    continue

                try:
                    med_item = PharmacyBillMedicineItem.objects.create(
                        bill=bill,
                        batch=best_batch,
                        quantity=rx_item.quantity,
                        prescription_item=rx_item,
                    )
                    added_medicines.append({
                        'prescription_item_id': rx_item.item_id,
                        'medicine_name': rx_item.medicine_name or getattr(rx_item, 'display_name', ''),
                        'batch_number': best_batch.batch_number,
                        'quantity': rx_item.quantity,
                        'item_id': med_item.item_id,
                        'unit_mrp': str(med_item.unit_mrp),
                        'item_total': str(med_item.item_total),
                    })
                except Exception as exc:
                    logger.warning("Failed to dispense prescription item %s: %s", rx_item.item_id, exc)
                    failed_medicines.append({
                        'prescription_item_id': rx_item.item_id,
                        'medicine_name': rx_item.medicine_name or 'Unknown',
                        'reason': _safe_detail(exc),
                    })

        if added_medicines:
            _recalculate_bill_totals(bill)
            bill.refresh_from_db()

        response_data = {
            'message': 'Pharmacy bill created.',
            'bill': PharmacyBillSerializer(bill).data,
        }
        if auto_add and prescription:
            response_data['auto_add_result'] = {
                'added_count': len(added_medicines),
                'failed_count': len(failed_medicines),
                'added_medicines': added_medicines,
                'failed_medicines': failed_medicines,
            }

        return Response(response_data, status=status.HTTP_201_CREATED)


# ═══════════════════════════════════════════════════════════════════
# BILL DETAIL
# GET /api/pharmacist/bills/<id>/
# ═══════════════════════════════════════════════════════════════════

class BillDetailView(APIView):
    """
    GET /api/pharmacist/bills/<id>/

    Returns detailed bill information including available next actions.
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request, bill_id):
        bill = _scoped_bill_or_404(
            request, bill_id,
            qs=PharmacyBill.objects
                .select_related('patient', 'consultation_bill', 'prescription')
                .prefetch_related(
                    'medicine_items__batch__medicine',
                    'medicine_items__prescription_item',
                    'procedure_items__procedure',
                ),
        )

        if bill.bill_status == 'DRAFT':
            available_actions = [
                'add_medicine_item',
                'add_procedure_item',
                'remove_medicine_item',
                'remove_procedure_item',
                'transition_open',
                'cancel_bill',
            ]
        elif bill.bill_status == 'OPEN':
            available_actions = [
                'add_medicine_item',
                'add_procedure_item',
                'remove_medicine_item',
                'remove_procedure_item',
                'complete_bill',
                'cancel_bill',
            ]
        elif bill.bill_status == 'COMPLETED':
            available_actions = ['mark_paid', 'reopen_bill', 'cancel_bill']
        elif bill.bill_status == 'PAID':
            available_actions = ['confirm_dispensing']
        else:
            available_actions = []

        return Response(
            {
                'bill': PharmacyBillSerializer(bill).data,
                'available_actions': available_actions,
                'dispense_status': {
                    'is_dispensed': bill.is_dispensed,
                    'dispensed_date': bill.updated_at if bill.is_dispensed else None,
                    'dispensed_items_count': bill.medicine_items.filter(is_dispensed=True).count(),
                    'pending_items_count': bill.medicine_items.filter(is_dispensed=False).count(),
                },
            }
        )


# ═══════════════════════════════════════════════════════════════════
# SET BILL DISCOUNT
# POST /api/pharmacist/bills/<id>/discount/
# ═══════════════════════════════════════════════════════════════════

class SetBillDiscountView(APIView):
    """
    POST /api/pharmacist/bills/<id>/discount/

    Sets (or clears, with "discount_amount": 0) the flat discount_amount
    on a PharmacyBill. Flat amount only — not a percentage.

    Not available once the bill is PAID or CANCELLED: the amount actually
    collected/settled shouldn't change after the fact.

    { "discount_amount": "50.00" }
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request, bill_id):
        bill = _scoped_bill_or_404(request, bill_id)

        if bill.bill_status in ('PAID', 'CANCELLED'):
            return Response(
                {
                    'error': (
                        f"Cannot change the discount on a bill that is "
                        f"'{bill.bill_status}'."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SetBillDiscountSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        discount_amount = serializer.validated_data['discount_amount']

        # Re-validate against the bill's current subtotal server-side —
        # don't trust the frontend, and don't rely on _recalculate_bill_totals's
        # silent clamping below to catch this, since that would hide an
        # over-the-subtotal request instead of reporting it.
        if discount_amount > bill.subtotal:
            return Response(
                {
                    'discount_amount': (
                        f"Discount amount cannot exceed the bill subtotal "
                        f"(₹{bill.subtotal})."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        bill.discount_amount = discount_amount
        _recalculate_bill_totals(bill)
        bill.refresh_from_db()

        return Response(
            {
                'message': 'Discount updated.',
                'bill': PharmacyBillSerializer(bill).data,
            }
        )


# ═══════════════════════════════════════════════════════════════════
# ADD MEDICINE ITEM
# POST /api/pharmacist/bills/<id>/add-medicine/
# ═══════════════════════════════════════════════════════════════════

class AddMedicineItemView(APIView):
    """
    POST /api/pharmacist/bills/<id>/add-medicine/

    Adds a medicine item to an OPEN bill.
    Stock is only deducted when the bill is marked PAID.

    { "batch_id": 1, "quantity": 5 }
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request, bill_id):
        bill = _scoped_bill_or_404(request, bill_id)

        if not bill.can_add_items():
            return Response(
                {
                    'error': (
                        f"Cannot add items to bill in '{bill.bill_status}' status. "
                        f"Bill must be 'DRAFT' or 'OPEN'. "
                        f"Call POST /bills/{bill.bill_id}/reopen/ to modify a COMPLETED bill."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = AddMedicineItemSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        batch_id = serializer.validated_data['batch_id']
        quantity = serializer.validated_data['quantity']
        batch    = get_object_or_404(MedicineBatch, pk=batch_id, medicine__branch=bill.branch)

        if batch.quantity < quantity:
            return Response(
                {
                    'error': (
                        f"Insufficient stock for '{batch.medicine.name}'. "
                        f"Available: {batch.quantity}, Requested: {quantity}"
                    ),
                    'available_stock': batch.quantity,
                    'requested_quantity': quantity,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        today = timezone.localdate()
        if batch.expiry_date and batch.expiry_date <= today:
            return Response(
                {
                    'error': f'Batch {batch.batch_number} has expired ({batch.expiry_date}).',
                    'batch_number': batch.batch_number,
                    'expiry_date': str(batch.expiry_date),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Optional: link this bill line back to the prescription item it was
        # dispensed for, so dose/frequency/meal-timing/duration are available
        # on the bill and printed receipt (see PharmacyBillMedicineItemSerializer).
        prescription_item = None
        prescription_item_id = serializer.validated_data.get('prescription_item_id')
        if prescription_item_id:
            from doctor.models import PrescriptionItem
            pi_qs = scope_queryset_to_branch(
                PrescriptionItem.objects.all(), request.user,
                branch_field='prescription__consultation__patient__branch',
            )
            prescription_item = get_object_or_404(pi_qs, pk=prescription_item_id)
            if bill.prescription_id and prescription_item.prescription_id != bill.prescription_id:
                return Response(
                    {'error': 'This prescription item does not belong to the bill\'s prescription.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        med_item = PharmacyBillMedicineItem(
            bill=bill,
            batch=batch,
            quantity=quantity,
            prescription_item=prescription_item,
        )
        med_item.save()
        _recalculate_bill_totals(bill)
        bill.refresh_from_db()

        return Response(
            {
                'message': (
                    f"Medicine '{batch.medicine.name}' added to bill. "
                    "Stock will be deducted when bill is marked PAID."
                ),
                'item': PharmacyBillMedicineItemSerializer(med_item).data,
                'bill': PharmacyBillSerializer(bill).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ═══════════════════════════════════════════════════════════════════
# ADD PROCEDURE ITEM
# POST /api/pharmacist/bills/<id>/add-procedure/
# ═══════════════════════════════════════════════════════════════════

class AddProcedureItemView(APIView):
    """
    POST /api/pharmacist/bills/<id>/add-procedure/

    { "procedure_id": 3, "quantity": 1 }
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request, bill_id):
        bill = _scoped_bill_or_404(request, bill_id)

        if not bill.can_add_items():
            return Response(
                {'error': f"Cannot add items to bill in '{bill.bill_status}' status. Bill must be 'DRAFT' or 'OPEN'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = AddProcedureItemSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        if Procedure is None:
            return Response(
                {'error': 'Procedure model is not available.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        procedure_id = serializer.validated_data.get('procedure_id')
        quantity     = serializer.validated_data.get('quantity', 1)

        if procedure_id:
            procedure = get_object_or_404(Procedure, pk=procedure_id, branch=bill.branch)
            display_name = procedure.name

            # If this catalog procedure is already on the bill, bump its
            # quantity instead of adding a second row for the same thing —
            # otherwise the receipt shows "Blood pressure Monitoring" twice
            # (qty 1 each) instead of once at qty 2.
            existing = PharmacyBillProcedureItem.objects.filter(
                bill=bill, procedure=procedure,
            ).first()
            if existing:
                existing.quantity += quantity
                existing.save()
                proc_item = existing
                created = False
            else:
                proc_item = PharmacyBillProcedureItem(bill=bill, procedure=procedure, quantity=quantity)
                proc_item.save()
                created = True
        else:
            # Manual procedure line — the pharmacist typed a description and
            # amount directly on THIS bill. This is not "creating a
            # procedure": nothing is written to the admin-managed Procedure
            # catalog, so it never leaks into the picker and never lingers
            # around for other bills. It's just a line item on this bill.
            description = serializer.validated_data['description'].strip()
            amount      = serializer.validated_data['amount']
            display_name = description

            # Same merge behavior for manual lines: same description (case
            # insensitive) and same rate on this bill -> bump quantity
            # rather than creating a duplicate row.
            existing = PharmacyBillProcedureItem.objects.filter(
                bill=bill, procedure__isnull=True,
                manual_description__iexact=description,
                unit_charge=amount,
            ).first()
            if existing:
                existing.quantity += quantity
                existing.save()
                proc_item = existing
                created = False
            else:
                proc_item = PharmacyBillProcedureItem(
                    bill=bill,
                    procedure=None,
                    manual_description=description,
                    quantity=quantity,
                    unit_charge=amount,
                )
                proc_item.save()
                created = True

        _recalculate_bill_totals(bill)
        bill.refresh_from_db()

        return Response(
            {
                'message': (
                    f"Procedure '{display_name}' added to bill."
                    if created else
                    f"Procedure '{display_name}' quantity updated to {proc_item.quantity}."
                ),
                'item': PharmacyBillProcedureItemSerializer(proc_item).data,
                'bill': PharmacyBillSerializer(bill).data,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


# ═══════════════════════════════════════════════════════════════════
# TRANSITION BILL TO OPEN
# POST /api/pharmacist/bills/<id>/transition-open/
# ═══════════════════════════════════════════════════════════════════

class TransitionBillToOpenView(APIView):
    """DRAFT → OPEN
    
    Transition a bill from DRAFT to OPEN status, allowing it to be completed.
    This intermediate step ensures pharmacist has reviewed all items before final completion.
    
    Only allows DRAFT → OPEN transition.
    Blocks transitions if bill is already in a different status.
    Requires at least one medicine or procedure item.
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def post(self, request, bill_id):
        bill = _scoped_bill_or_404(request, bill_id)

        # If bill is already past DRAFT (OPEN/COMPLETED/PAID), treat as idempotent —
        # the frontend WalkIn flow calls this after flushing items; if the bill was
        # resumed from a 409 it may already be OPEN or further along.
        if bill.bill_status == 'OPEN':
            return Response(
                {
                    'message': 'Bill is already OPEN.',
                    'bill': PharmacyBillSerializer(bill).data,
                },
                status=status.HTTP_200_OK,
            )

        if bill.bill_status in ('COMPLETED', 'PAID'):
            return Response(
                {
                    'message': f'Bill is already {bill.bill_status}. No transition needed.',
                    'bill': PharmacyBillSerializer(bill).data,
                },
                status=status.HTTP_200_OK,
            )

        if bill.bill_status == 'CANCELLED':
            return Response(
                {
                    'error': 'Cannot reopen a CANCELLED bill.',
                    'current_status': bill.bill_status,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Only DRAFT bills get transitioned to OPEN here
        if bill.bill_status != 'DRAFT':
            return Response(
                {
                    'error': (
                        f"Bill must be in 'DRAFT' status to open. "
                        f"Current status: '{bill.bill_status}'."
                    ),
                    'current_status': bill.bill_status,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Ensure bill has at least one item
        has_items = bill.medicine_items.exists() or bill.procedure_items.exists() or bill.general_items.exists()
        if not has_items:
            return Response(
                {
                    'error': (
                        'Cannot open an empty bill. Add at least one medicine '
                        'or procedure item first.'
                    ),
                    'current_status': bill.bill_status,
                    'item_count': 0,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Transition to OPEN
        bill.bill_status = 'OPEN'
        bill.save(update_fields=['bill_status'])

        return Response(
            {
                'message': 'Bill transitioned to OPEN. You can now complete it.',
                'bill': PharmacyBillSerializer(bill).data,
            },
            status=status.HTTP_200_OK,
        )


# ═══════════════════════════════════════════════════════════════════
# COMPLETE BILL
# POST /api/pharmacist/bills/<id>/complete/
# ═══════════════════════════════════════════════════════════════════

class CompleteBillView(APIView):
    """OPEN → COMPLETED"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def post(self, request, bill_id):
        bill = _scoped_bill_or_404(request, bill_id)

        # Idempotent: already COMPLETED → treat as success so WalkIn retry works
        if bill.bill_status == 'COMPLETED':
            return Response(
                {
                    'message': 'Bill is already COMPLETED. Next: Mark as PAID.',
                    'bill': PharmacyBillSerializer(bill).data,
                }
            )

        if bill.bill_status == 'PAID':
            return Response(
                {
                    'message': 'Bill is already PAID.',
                    'bill': PharmacyBillSerializer(bill).data,
                }
            )

        if bill.bill_status != 'OPEN':
            return Response(
                {'error': f"Bill must be 'OPEN' to complete. Current status: '{bill.bill_status}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not (bill.medicine_items.exists() or bill.procedure_items.exists() or bill.general_items.exists()):
            return Response(
                {'error': 'Cannot complete an empty bill. Add at least one item first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        bill.bill_status = 'COMPLETED'
        bill.save(update_fields=['bill_status'])

        return Response(
            {
                'message': 'Bill COMPLETED. Next: Mark as PAID to dispense medicines and deduct stock.',
                'bill': PharmacyBillSerializer(bill).data,
            }
        )


# ═══════════════════════════════════════════════════════════════════
# MARK BILL PAID  (stock deduction happens here)
# POST /api/pharmacist/bills/<id>/mark-paid/
# ═══════════════════════════════════════════════════════════════════

class MarkBillPaidView(APIView):
    """
    POST /api/pharmacist/bills/<id>/mark-paid/

    COMPLETED → PAID.  Triggers finalize_dispense() — stock deducted here.

    { "payment_method": "CASH"|"CARD"|"UPI"|"OTHER",
      "upi_reference": "ref123" }
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request, bill_id):
        bill = _scoped_bill_or_404(request, bill_id)

        # Idempotent: already PAID → return success (WalkIn retry path)
        if bill.bill_status == 'PAID':
            return Response(
                {
                    'message': '✓ Bill is already PAID and dispensed.',
                    'bill': PharmacyBillSerializer(bill).data,
                    'dispensed_items': PharmacyBillMedicineItemSerializer(
                        bill.medicine_items.all(), many=True
                    ).data,
                }
            )

        if bill.bill_status != 'COMPLETED':
            return Response(
                {
                    'error': (
                        f"Bill must be 'COMPLETED' before marking PAID. "
                        f"Current status: '{bill.bill_status}'. "
                        f"Call POST /bills/{bill.bill_id}/complete/ first."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = MarkBillPaidSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        payment_method = serializer.validated_data.get('payment_method', bill.payment_method)
        upi_reference  = serializer.validated_data.get('upi_reference')

        bill.payment_status = 'PAID'
        bill.payment_method = payment_method
        if upi_reference:
            bill.upi_reference = upi_reference
        bill.bill_status = 'PAID'
        bill.save()

        try:
            bill.finalize_dispense()
        except ValidationError as exc:
            transaction.set_rollback(True)
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Auto-forward to reception now that the bill is PAID + dispensed —
        # this used to require a separate manual "Send to Reception" click
        # (see SendBillToReceptionView below), which is redundant once the
        # bill has already reached its final state here.
        if not bill.sent_to_reception:
            bill.sent_to_reception = True
            bill.sent_to_reception_at = timezone.now()
            bill.sent_to_reception_by = request.user
            bill.save(update_fields=['sent_to_reception', 'sent_to_reception_at', 'sent_to_reception_by'])

        bill.refresh_from_db()

        return Response(
            {
                'message': (
                    '✓ Bill marked PAID. '
                    '✓ Medicines DISPENSED (stock permanently deducted). '
                    '✓ All items marked as dispensed. '
                    '✓ Bill sent to reception. '
                    'This action is final and cannot be undone.'
                ),
                'bill': PharmacyBillSerializer(bill).data,
                'dispensed_items': PharmacyBillMedicineItemSerializer(
                    bill.medicine_items.all(), many=True
                ).data,
            }
        )


# ═══════════════════════════════════════════════════════════════════
# SEND BILL TO RECEPTION
# POST /api/pharmacist/bills/<id>/send-to-reception/
# ═══════════════════════════════════════════════════════════════════

class SendBillToReceptionView(APIView):
    """
    POST /api/pharmacist/bills/<id>/send-to-reception/

    Forwards a PAID (i.e. paid AND dispensed — see MarkBillPaidView,
    which is the only place a bill reaches 'PAID') bill to the
    reception module, where it'll show up in reception's own
    Pharmacy Bills list (filterable by today / this week / this
    month there) for review and printing.

    Idempotent: sending an already-sent bill again just re-confirms
    rather than erroring, matching MarkBillPaidView's pattern.
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def post(self, request, bill_id):
        bill = _scoped_bill_or_404(request, bill_id)

        if bill.sent_to_reception:
            return Response({
                'message': 'Bill was already sent to reception.',
                'bill': PharmacyBillSerializer(bill).data,
            })

        if bill.bill_status != 'PAID':
            return Response(
                {
                    'error': (
                        f"Bill must be PAID (paid and dispensed) before it can be sent to reception. "
                        f"Current status: '{bill.bill_status}'."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        bill.sent_to_reception = True
        bill.sent_to_reception_at = timezone.now()
        bill.sent_to_reception_by = request.user
        bill.save(update_fields=['sent_to_reception', 'sent_to_reception_at', 'sent_to_reception_by'])

        return Response({
            'message': '✓ Bill sent to reception.',
            'bill': PharmacyBillSerializer(bill).data,
        })


# ═══════════════════════════════════════════════════════════════════
# REOPEN BILL
# POST /api/pharmacist/bills/<id>/reopen/
# ═══════════════════════════════════════════════════════════════════

class ReopenBillView(APIView):
    """COMPLETED → OPEN"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def post(self, request, bill_id):
        bill = _scoped_bill_or_404(request, bill_id)

        if bill.is_dispensed:
            return Response(
                {'error': 'Cannot reopen a PAID bill — medicines already dispensed.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if bill.bill_status != 'COMPLETED':
            return Response(
                {'error': f"Bill must be 'COMPLETED' to reopen. Current status: '{bill.bill_status}'"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        bill.bill_status = 'OPEN'
        bill.save(update_fields=['bill_status'])

        return Response(
            {
                'message': 'Bill REOPENED. You can now add or remove items.',
                'bill': PharmacyBillSerializer(bill).data,
            }
        )


# ═══════════════════════════════════════════════════════════════════
# CANCEL BILL
# POST /api/pharmacist/bills/<id>/cancel/
# ═══════════════════════════════════════════════════════════════════

class CancelBillView(APIView):
    """OPEN | COMPLETED → CANCELLED"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def post(self, request, bill_id):
        bill = _scoped_bill_or_404(request, bill_id)

        serializer = CancelBillSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            bill.cancel_bill()
        except ValidationError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        reason = serializer.validated_data.get('reason', '')
        if reason:
            bill.notes = reason
            bill.save(update_fields=['notes'])

        bill.refresh_from_db()

        return Response(
            {
                'message': (
                    'Bill CANCELLED. No stock was deducted (medicines never physically dispensed). '
                    'This bill cannot be reopened.'
                ),
                'bill': PharmacyBillSerializer(bill).data,
            }
        )


# ═══════════════════════════════════════════════════════════════════
# REMOVE / UPDATE MEDICINE ITEM
# DELETE|PATCH /api/pharmacist/bills/<bill_id>/medicine-items/<item_id>/
# ═══════════════════════════════════════════════════════════════════

class RemoveMedicineItemView(APIView):
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def patch(self, request, bill_id, item_id):
        """Update quantity of medicine item in bill (only for DRAFT/OPEN bills)"""
        bill = _scoped_bill_or_404(request, bill_id)
        med_item = get_object_or_404(PharmacyBillMedicineItem, pk=item_id, bill=bill)

        # Check for both DRAFT and OPEN status
        if bill.bill_status not in ['DRAFT', 'OPEN']:
            return Response(
                {
                    'error': f"Cannot modify items on a '{bill.bill_status}' bill.",
                    'current_status': bill.bill_status,
                    'allowed_statuses': ['DRAFT', 'OPEN'],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        quantity = request.data.get('quantity')
        if quantity is None:
            return Response({'error': 'quantity is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            quantity = int(quantity)
        except (ValueError, TypeError):
            return Response({'error': 'quantity must be an integer.'}, status=status.HTTP_400_BAD_REQUEST)
        if quantity <= 0:
            return Response({'error': 'quantity must be at least 1.'}, status=status.HTTP_400_BAD_REQUEST)

        if med_item.batch.quantity < quantity:
            return Response(
                {
                    'error': (
                        f"Insufficient stock for '{med_item.batch.medicine.name}'. "
                        f"Available: {med_item.batch.quantity}, Requested: {quantity}"
                    ),
                    'available_stock': med_item.batch.quantity,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        med_item.quantity = quantity
        med_item.save()
        _recalculate_bill_totals(bill)
        bill.refresh_from_db()

        return Response(
            {
                'message': f'Quantity updated to {quantity}.',
                'item': PharmacyBillMedicineItemSerializer(med_item).data,
                'bill': PharmacyBillSerializer(bill).data,
            }
        )

    @transaction.atomic
    def delete(self, request, bill_id, item_id):
        """Remove medicine item from bill (only for DRAFT/OPEN bills)"""
        bill = _scoped_bill_or_404(request, bill_id)
        med_item = get_object_or_404(PharmacyBillMedicineItem, pk=item_id, bill=bill)

        # Check for both DRAFT and OPEN status
        if bill.bill_status not in ['DRAFT', 'OPEN']:
            return Response(
                {
                    'error': f"Cannot remove items from a '{bill.bill_status}' bill.",
                    'current_status': bill.bill_status,
                    'allowed_statuses': ['DRAFT', 'OPEN'],
                    'message': 'Only DRAFT and OPEN bills allow item removal.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        medicine_name = med_item.batch.medicine.name
        
        try:
            med_item.delete()
            _recalculate_bill_totals(bill)
            bill.refresh_from_db()

            return Response(
                {
                    'message': f"Medicine '{medicine_name}' removed from bill.",
                    'bill': PharmacyBillSerializer(bill).data,
                }
            )
        except Exception as e:
            logger.warning("Failed to remove medicine from bill: %s", e)
            return Response(
                {
                    'error': f"Failed to remove medicine: {_safe_detail(e)}",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )


# ═══════════════════════════════════════════════════════════════════
# REMOVE PROCEDURE ITEM
# DELETE /api/pharmacist/bills/<bill_id>/procedure-items/<item_id>/
# ═══════════════════════════════════════════════════════════════════

class RemoveProcedureItemView(APIView):
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def patch(self, request, bill_id, item_id):
        """Update quantity of a procedure item in bill (only for OPEN bills).

        ✅ FIX: this method did not exist before, so the frontend's
        updateProcedureItem() call (PATCH .../procedure-items/<id>/) always
        got a 405 Method Not Allowed — procedure quantities could never be
        edited after being added to a bill.
        """
        bill = _scoped_bill_or_404(request, bill_id)
        proc_item = get_object_or_404(PharmacyBillProcedureItem, pk=item_id, bill=bill)

        if bill.bill_status not in ['DRAFT', 'OPEN']:
            return Response(
                {'error': f"Cannot modify items on bill in '{bill.bill_status}' status. Only DRAFT/OPEN bills allow edits."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        quantity = request.data.get('quantity')
        if quantity is None:
            return Response({'error': 'quantity is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            quantity = int(quantity)
        except (ValueError, TypeError):
            return Response({'error': 'quantity must be an integer.'}, status=status.HTTP_400_BAD_REQUEST)
        if quantity <= 0:
            return Response({'error': 'quantity must be at least 1.'}, status=status.HTTP_400_BAD_REQUEST)

        proc_item.quantity = quantity
        proc_item.save()
        _recalculate_bill_totals(bill)
        bill.refresh_from_db()

        return Response(
            {
                'message': f'Quantity updated to {quantity}.',
                'item': PharmacyBillProcedureItemSerializer(proc_item).data,
                'bill': PharmacyBillSerializer(bill).data,
            }
        )

    @transaction.atomic
    def delete(self, request, bill_id, item_id):
        bill = _scoped_bill_or_404(request, bill_id)
        proc_item = get_object_or_404(PharmacyBillProcedureItem, pk=item_id, bill=bill)

        if bill.bill_status not in ['DRAFT', 'OPEN']:
            return Response(
                {'error': f"Cannot remove items from bill in '{bill.bill_status}' status. Only DRAFT/OPEN bills allow removal."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        procedure_name = proc_item.display_name
        proc_item.delete()
        _recalculate_bill_totals(bill)
        bill.refresh_from_db()

        return Response(
            {
                'message': f"Procedure '{procedure_name}' removed from bill.",
                'bill': PharmacyBillSerializer(bill).data,
            }
        )


# ═══════════════════════════════════════════════════════════════════
# MEDICINE LIST + DETAIL
# ═══════════════════════════════════════════════════════════════════

class MedicineListView(APIView):
    """
    GET /api/pharmacist/medicines/

    Query params: search, category, in_stock=true, show_inactive=true
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        qs = Medicine.objects.prefetch_related('batches').order_by('name')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='branch', branch_id=request.query_params.get('branch'))

        if request.query_params.get('show_inactive', 'false').lower() != 'true':
            qs = qs.filter(is_active=True)

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(generic_name__icontains=search) |
                Q(category__icontains=search) |
                Q(description__icontains=search)
            )

        category = request.query_params.get('category', '').strip()
        if category:
            qs = qs.filter(category__iexact=category)

        data = MedicineWithBatchesSerializer(qs, many=True).data

        if request.query_params.get('in_stock', 'false').lower() == 'true':
            data = [m for m in data if m['total_stock'] > 0]

        return Response(data)


class MedicineDetailView(APIView):
    """GET /api/pharmacist/medicines/<pk>/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request, pk):
        qs = scope_queryset_to_branch(
            Medicine.objects.prefetch_related('batches'), request.user, branch_field='branch'
        )
        medicine = get_object_or_404(qs, pk=pk)
        return Response(MedicineWithBatchesSerializer(medicine).data)


# ═══════════════════════════════════════════════════════════════════
# MEDICINE CREATE / UPDATE
# ═══════════════════════════════════════════════════════════════════

class MedicineCreateView(APIView):
    """POST /api/pharmacist/medicines/create/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def post(self, request):
        # Medicine.branch is a required FK with no default — without this,
        # Medicine.objects.create(**validated_data) below raises an
        # unhandled IntegrityError, since MedicineWriteSerializer never
        # collected a branch from the client. resolve_branch_for_write
        # pins ordinary staff to their own branch and requires a group
        # admin to pick one explicitly.
        branch, error = resolve_branch_for_write(request, required=True)
        if error:
            return error

        serializer = MedicineWriteSerializer(data=request.data, context={'branch': branch})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        medicine = Medicine.objects.create(branch=branch, **serializer.validated_data)
        return Response(
            {'message': 'Medicine created.', 'medicine': MedicineSerializer(medicine).data},
            status=status.HTTP_201_CREATED,
        )


class MedicineUpdateView(APIView):
    """PATCH /api/pharmacist/medicines/<pk>/update/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def patch(self, request, pk):
        qs = scope_queryset_to_branch(Medicine.objects.all(), request.user, branch_field='branch')
        medicine   = get_object_or_404(qs, pk=pk)
        serializer = MedicineWriteSerializer(
            data=request.data, partial=True,
            context={'branch': medicine.branch, 'instance': medicine},
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        for attr, value in serializer.validated_data.items():
            setattr(medicine, attr, value)
        medicine.save()
        return Response(
            {'message': 'Medicine updated.', 'medicine': MedicineSerializer(medicine).data}
        )


# ═══════════════════════════════════════════════════════════════════
# MEDICINE SEARCH  (autocomplete for doctor prescriptions)
# GET /api/pharmacist/medicines/search/
# ═══════════════════════════════════════════════════════════════════

class MedicineSearchView(APIView):
    """
    GET /api/pharmacist/medicines/search/

    Query params: q, recent (comma-sep IDs), limit (default 12 max 50)
    Permissions: any authenticated user (doctors need this too).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        q      = request.query_params.get('q', '').strip()
        recent = request.query_params.get('recent', '').strip()
        try:
            limit = min(int(request.query_params.get('limit', 12)), 50)
        except (ValueError, TypeError):
            limit = 12

        qs = Medicine.objects.filter(is_active=True).prefetch_related('batches')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='branch', branch_id=request.query_params.get('branch'))

        if q:
            qs = qs.filter(
                Q(name__icontains=q) |
                Q(generic_name__icontains=q) |
                Q(category__icontains=q)
            )

        recent_ids = []
        if recent:
            try:
                recent_ids = [int(i) for i in recent.split(',') if i.strip().isdigit()]
            except (ValueError, AttributeError):
                recent_ids = []

        if recent_ids:
            ordering = Case(
                *[When(pk=rid, then=i) for i, rid in enumerate(recent_ids)],
                default=len(recent_ids),
                output_field=IntegerField(),
            )
            qs = qs.annotate(_recent_order=ordering).order_by('_recent_order', 'name')
        else:
            qs = qs.order_by('name')

        qs = qs[:limit]
        recent_set = set(recent_ids)
        results = []
        for med in qs:
            active_batches = [b for b in med.batches.all() if b.quantity > 0]
            total_stock    = sum(b.quantity for b in active_batches)
            stock_status   = 'AVAILABLE' if total_stock > 10 else ('LOW' if total_stock > 0 else 'OUT_OF_STOCK')
            results.append({
                'id': med.medicine_id,
                'medicine_id': med.medicine_id,
                'name': med.name,
                'generic_name': med.generic_name or '',
                'category': med.category or '',
                'unit': med.unit or '',
                'medicine_type': med.medicine_type,
                'medicine_type_display': med.get_medicine_type_display(),
                'strength': med.strength or '',
                'stock_quantity': total_stock,
                'stock_status': stock_status,
                'route': med.default_route,
                'route_display': med.get_default_route_display(),
                'is_active': med.is_active,
                'is_recent': med.medicine_id in recent_set,
            })

        return Response({'count': len(results), 'results': results})


# ═══════════════════════════════════════════════════════════════════
# BATCH LIST / DETAIL / CREATE / UPDATE
# ═══════════════════════════════════════════════════════════════════

class BatchListView(APIView):
    """GET /api/pharmacist/batches/ — Query: medicine_id, status, available=true, search"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        qs = MedicineBatch.objects.select_related('medicine').order_by('medicine__name', 'expiry_date')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='medicine__branch', branch_id=request.query_params.get('branch'))

        medicine_id = request.query_params.get('medicine_id')
        if medicine_id:
            qs = qs.filter(medicine_id=medicine_id)

        # FIX: support comma-separated status values e.g. "ACTIVE,PENDING_APPROVAL"
        # so the pharmacist batch panel shows dealer-linked pending batches alongside
        # active stock, without matching the full CSV string as a single status value.
        batch_status_raw = request.query_params.get('status', '').strip()
        if batch_status_raw:
            statuses = [s.strip().upper() for s in batch_status_raw.split(',') if s.strip()]
            if len(statuses) == 1:
                qs = qs.filter(status=statuses[0])
            elif statuses:
                qs = qs.filter(status__in=statuses)

        if request.query_params.get('available', 'false').lower() == 'true':
            today = timezone.localdate()
            qs = qs.filter(status='ACTIVE', quantity__gt=0).exclude(expiry_date__lte=today)

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(batch_number__icontains=search) | Q(medicine__name__icontains=search)
            )

        return Response(MedicineBatchDetailSerializer(qs, many=True).data)


class BatchDetailView(APIView):
    """GET /api/pharmacist/batches/<pk>/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request, pk):
        qs = scope_queryset_to_branch(
            MedicineBatch.objects.select_related('medicine'), request.user, branch_field='medicine__branch'
        )
        batch = get_object_or_404(qs, pk=pk)
        return Response(MedicineBatchDetailSerializer(batch).data)


class BatchCreateView(APIView):
    """POST /api/pharmacist/batches/create/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request):
        serializer = MedicineBatchWriteSerializer(data=request.data, context={'user': request.user})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        medicine_id = serializer.validated_data.pop('medicine_id')
        medicine_qs = scope_queryset_to_branch(Medicine.objects.all(), request.user, branch_field='branch')
        medicine    = get_object_or_404(medicine_qs, pk=medicine_id)

        dealer_id = serializer.validated_data.get('dealer_id')
        if dealer_id:
            # Friendlier 404 than letting full_clean() raise on an
            # invalid FK further down. Scoped to the medicine's branch so a
            # branch-scoped pharmacist can't attach another branch's dealer.
            from manager.models import Dealer
            get_object_or_404(Dealer, pk=dealer_id, branch=medicine.branch)

        batch = MedicineBatch(medicine=medicine, **serializer.validated_data)
        # ✅ NEW: dealer-linked stock is not sellable until the manager
        # reviews the purchase — see MedicineBatch.BATCH_STATUS_CHOICES.
        # No dealer → unchanged, stays ACTIVE immediately.
        if batch.dealer_id:
            batch.status = 'PENDING_APPROVAL'
        batch.save()

        MedicineStockLog.objects.create(
            batch=batch,
            change_type='IN',
            quantity_changed=batch.quantity,
            remarks='Initial stock entry' + (' (pending manager approval)' if batch.dealer_id else ''),
        )

        # OPTIONAL dealer link → auto-log a PENDING purchase transaction
        # for the manager to review/finalize on the Dealers page. No-op
        # when no dealer was selected (existing behaviour unchanged).
        if batch.dealer_id:
            from manager.models import create_dealer_transaction
            create_dealer_transaction(
                dealer=batch.dealer,
                transaction_type='PURCHASE',
                amount=batch.cost_price * batch.quantity,
                source_model='MEDICINE_BATCH',
                source_id=batch.batch_id,
                created_by=request.user,
                settlement_method=batch.settlement_method,
                reference_number=batch.batch_number,
                notes=f"Stock purchase: {medicine.name} × {batch.quantity} (batch {batch.batch_number}).",
            )

        return Response(
            {'message': f"Batch created for '{medicine.name}'.", 'batch': MedicineBatchDetailSerializer(batch).data},
            status=status.HTTP_201_CREATED,
        )


class BatchUpdateView(APIView):
    """PATCH /api/pharmacist/batches/<pk>/update/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def patch(self, request, pk):
        batch_qs = scope_queryset_to_branch(MedicineBatch.objects.all(), request.user, branch_field='medicine__branch')
        batch   = get_object_or_404(batch_qs, pk=pk)
        old_qty = batch.quantity

        serializer = MedicineBatchWriteSerializer(data=request.data, partial=True, context={'user': request.user})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        serializer.validated_data.pop('medicine_id', None)
        for attr, value in serializer.validated_data.items():
            setattr(batch, attr, value)
        batch.save()

        if batch.quantity != old_qty:
            MedicineStockLog.objects.create(
                batch=batch,
                change_type='ADJUST',
                quantity_changed=batch.quantity - old_qty,
                remarks=f'Manual adjustment: {old_qty} → {batch.quantity}',
            )

        return Response(
            {'message': 'Batch updated.', 'batch': MedicineBatchDetailSerializer(batch).data}
        )


# ═══════════════════════════════════════════════════════════════════
# ✅ BATCH RETURN TO PROVIDER
# POST /api/pharmacist/batches/<batch_id>/return-to-provider/
# ═══════════════════════════════════════════════════════════════════

class BatchReturnToProviderView(APIView):
    """
    POST /api/pharmacist/batches/<batch_id>/return-to-provider/
    
    Create a return-to-provider request for a specific batch.
    
    Request body:
    {
      "quantity": 10,
      "reason": "Excess stock" | "Expired" | "Damaged" | "Defective" | "Wrong item" | "Other"
    }
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request, batch_id):
        batch_qs = scope_queryset_to_branch(MedicineBatch.objects.all(), request.user, branch_field='medicine__branch')
        batch = get_object_or_404(batch_qs, pk=batch_id)
        
        quantity = request.data.get('quantity')
        reason = request.data.get('reason', 'Other')
        
        if not quantity:
            return Response(
                {'error': 'Quantity is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            quantity = int(quantity)
        except (ValueError, TypeError):
            return Response(
                {'error': 'Quantity must be a valid integer'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if quantity <= 0:
            return Response(
                {'error': 'Quantity must be greater than 0'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        available = batch.quantity - batch.allocated_quantity
        if quantity > available:
            return Response(
                {
                    'error': f'Only {available} units available for return',
                    'available': available,
                    'requested': quantity
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        valid_reasons = ['Excess stock', 'Expired', 'Damaged', 'Defective', 'Wrong item', 'Other']
        if reason not in valid_reasons:
            return Response(
                {'error': f'Invalid reason. Must be one of: {", ".join(valid_reasons)}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            return_record = MedicineReturnToProvider.objects.create(
                batch=batch,
                quantity=quantity,
                reason=reason,
                status='PENDING',
                created_by=request.user
            )
            
            MedicineStockLog.objects.create(
                batch=batch,
                change_type='RETURN_TO_PROVIDER_REQUEST',
                quantity_changed=-quantity,
                remarks=f'Return request created: {reason}'
            )
            
            serializer = MedicineReturnToProviderSerializer(return_record)
            return Response(
                serializer.data,
                status=status.HTTP_201_CREATED
            )
        
        except Exception as e:
            logger.exception("Failed to create medicine return request")
            return Response(
                {'error': f'Failed to create return request: {_safe_detail(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# ═══════════════════════════════════════════════════════════════════
# STOCK ALERTS
# ═══════════════════════════════════════════════════════════════════

class StockAlertListView(APIView):
    """GET /api/pharmacist/stock-alerts/  — Query: alert_type, resolved=true"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        qs = StockAlert.objects.select_related('batch__medicine').order_by('-created_at')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='batch__medicine__branch', branch_id=request.query_params.get('branch'))

        if request.query_params.get('resolved', 'false').lower() != 'true':
            qs = qs.filter(is_resolved=False)

        alert_type = request.query_params.get('alert_type', '').upper()
        if alert_type:
            qs = qs.filter(alert_type=alert_type)

        return Response(StockAlertSerializer(qs, many=True).data)


class StockAlertResolveView(APIView):
    """POST|PATCH /api/pharmacist/stock-alerts/<id>/resolve/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def _resolve(self, request, alert_id):
        qs = scope_queryset_to_branch(StockAlert.objects.all(), request.user, branch_field='batch__medicine__branch')
        alert = get_object_or_404(qs, pk=alert_id)
        if alert.is_resolved:
            return Response({'message': 'Alert is already resolved.'})
        alert.is_resolved = True
        alert.save(update_fields=['is_resolved'])
        return Response({'message': 'Alert marked as resolved.', 'alert': StockAlertSerializer(alert).data})

    def post(self, request, alert_id):
        return self._resolve(request, alert_id)

    def patch(self, request, alert_id):
        return self._resolve(request, alert_id)


# ═══════════════════════════════════════════════════════════════════
# STOCK LOG
# GET /api/pharmacist/stock-logs/
# ═══════════════════════════════════════════════════════════════════

class StockLogListView(APIView):
    """Query: batch_id, medicine_id, change_type"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        qs = MedicineStockLog.objects.select_related('batch__medicine').order_by('-created_at')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='batch__medicine__branch', branch_id=request.query_params.get('branch'))

        batch_id = request.query_params.get('batch_id')
        if batch_id:
            qs = qs.filter(batch_id=batch_id)

        medicine_id = request.query_params.get('medicine_id')
        if medicine_id:
            qs = qs.filter(batch__medicine_id=medicine_id)

        change_type = request.query_params.get('change_type', '').upper()
        if change_type:
            qs = qs.filter(change_type=change_type)

        qs = qs[:200]
        return Response(MedicineStockLogSerializer(qs, many=True).data)


# ═══════════════════════════════════════════════════════════════════
# FETCH PRESCRIPTION MEDICINES
# ═══════════════════════════════════════════════════════════════════
# PRESCRIPTION QUEUE  (consultations with prescriptions sent to pharmacy)
# GET /api/pharmacist/prescriptions/
# ═══════════════════════════════════════════════════════════════════
#
# ✅ NEW: replaces the frontend's previous (broken) approach of calling
# GET /api/doctor/consultations/ directly.
#
# That was broken for two independent reasons:
#   1. doctor.views.ConsultationListView only returns data for users whose
#      role resolves to 'doctor' or 'admin'. A pharmacist's role resolves
#      to 'pharmacist', so that endpoint always handed back an empty list
#      — the Prescriptions page never had anything to show.
#   2. Even for the users who *could* see results, the frontend then called
#      GET /api/doctor/consultations/{id}/prescriptions/ per consultation
#      to fetch each one's prescriptions — that URL was never registered
#      anywhere in doctor/urls.py, so every one of those calls 404'd.
#
# This view lives in the pharmacist app (where IsAdminOrPharmacist already
# grants the right people access) and returns everything the Prescriptions
# page needs in a single request: one row per consultation that has a
# prescription marked is_sent_to_pharmacy=True, with prescription items and
# a computed prescription_status (PENDING / PARTIALLY_DISPENSED / DISPENSED)
# derived from whether a PharmacyBill exists yet and its bill_status.
# ═══════════════════════════════════════════════════════════════════

class PrescriptionQueueListView(APIView):
    """
    GET /api/pharmacist/prescriptions/

    Query params:
    - date   : YYYY-MM-DD — filter by consultation date (optional)
    - search : free text over patient name / MRD / OP number / doctor name
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        from doctor.models import Prescription

        qs = (
            Prescription.objects
            .filter(is_sent_to_pharmacy=True)
            .select_related(
                'consultation',
                'consultation__patient',
                'consultation__consultation_bill',
                'consultation__doctor',
                'consultation__doctor__staff',
                'consultation__doctor__staff__user',
                'consultation__guest_doctor',
                'consultation__doctor_user',
            )
            .prefetch_related('items__medicine')
            .order_by('-consultation__consultation_date', '-prescription_id')
        )
        qs = scope_queryset_to_branch(qs, request.user, branch_field='consultation__patient__branch', branch_id=request.query_params.get('branch'))

        date = request.query_params.get('date')
        if date:
            qs = qs.filter(consultation__consultation_date=date)

        prescription_ids = list(qs.values_list('prescription_id', flat=True))
        bills_by_prescription = {
            b.prescription_id: b
            for b in PharmacyBill.objects.filter(
                prescription_id__in=prescription_ids
            ).exclude(bill_status='CANCELLED')
        }

        consultations_by_id = {}
        results = []

        for rx in qs:
            consultation = rx.consultation
            if consultation is None:
                continue

            bill = bills_by_prescription.get(rx.prescription_id)
            if not bill:
                rx_status = 'PENDING'
            elif bill.bill_status == 'PAID':
                rx_status = 'DISPENSED'
            else:
                rx_status = 'PARTIALLY_DISPENSED'

            items_data = []
            for item in rx.items.all():
                items_data.append({
                    'item_id':   item.item_id,
                    'medicine': {
                        'medicine_id': item.medicine.medicine_id if item.medicine else None,
                        'name': item.medicine.name if item.medicine else (item.medicine_name or 'Unknown'),
                        'strength': item.medicine.strength if item.medicine else None,
                    },
                    'quantity':  item.quantity,
                    'unit':      item.medicine.unit if item.medicine else None,
                    'frequency': item.get_frequency_display() if item.frequency else None,
                    'route':     item.get_route_display() if item.route else None,
                })

            rx_data = {
                'prescription_id': rx.prescription_id,
                'prescription_status': rx_status,
                'notes': rx.notes or '',
                'items': items_data,
                'bill_id': bill.bill_id if bill else None,
                'bill_status': bill.bill_status if bill else None,
            }

            entry = consultations_by_id.get(consultation.consultation_id)
            if entry is None:
                patient = consultation.patient
                try:
                    op_number = consultation.consultation_bill.op_number
                except Exception:
                    op_number = ''

                entry = {
                    'consultation_id': consultation.consultation_id,
                    'consultation_date': consultation.consultation_date,
                    'op_number': op_number or '',
                    'patient_id': patient.patient_id if patient else None,
                    'patient_name': f'{patient.first_name} {patient.last_name}'.strip() if patient else 'Unknown',
                    'patient_mrd': patient.mrd_number if patient else '',
                    'doctor_name': consultation.get_doctor_name(),
                    'prescriptions': [],
                }
                consultations_by_id[consultation.consultation_id] = entry
                results.append(entry)

            entry['prescriptions'].append(rx_data)

        search = request.query_params.get('search', '').strip().lower()
        if search:
            results = [
                r for r in results
                if search in (r['patient_name'] or '').lower()
                or search in (r['patient_mrd'] or '').lower()
                or search in (r['op_number'] or '').lower()
                or search in (r['doctor_name'] or '').lower()
            ]

        return Response(results)


# ═══════════════════════════════════════════════════════════════════
# PRESCRIPTION MEDICINES  (single prescription, with batch availability)
# GET /api/pharmacist/prescriptions/<prescription_id>/medicines/
# ═══════════════════════════════════════════════════════════════════

class FetchPrescriptionMedicinesView(APIView):
    """
    GET /api/pharmacist/prescriptions/<prescription_id>/medicines/

    Fetches all medicines from a doctor's prescription along with
    available batches in the pharmacy.
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request, prescription_id):
        try:
            from doctor.models import Prescription
            rx_qs = scope_queryset_to_branch(
                Prescription.objects.all(), request.user, branch_field='consultation__patient__branch'
            )
            prescription = rx_qs.select_related(
                'consultation__patient',
                'consultation__consultation_bill',
            ).prefetch_related('items__medicine').get(pk=prescription_id)
        except Prescription.DoesNotExist:
            return Response(
                {'error': f'Prescription #{prescription_id} not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as exc:
            return Response(
                {'error': 'Database error fetching prescription.', 'detail': _safe_detail(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        medicines_data       = []
        medicines_with_stock = 0
        total_prescribed_qty = 0
        today = timezone.localdate()

        for item in prescription.items.all():
            total_prescribed_qty += item.quantity
            batches   = []
            has_stock = False

            if item.medicine and item.medicine.branch_id == prescription.consultation.patient.branch_id:
                active_batches = MedicineBatch.objects.filter(
                    medicine=item.medicine,
                    status='ACTIVE',
                    quantity__gt=0,
                ).exclude(expiry_date__lt=today).order_by('-expiry_date').values(
                    'batch_id', 'batch_number', 'quantity',
                    'mrp', 'gst_percentage', 'expiry_date',
                )

                for batch in active_batches:
                    is_avail = batch['quantity'] >= item.quantity
                    batches.append({
                        'batch_id':      batch['batch_id'],
                        'batch_number':  batch['batch_number'],
                        'quantity':      batch['quantity'],
                        'mrp':           str(batch['mrp']),
                        'gst_percentage': str(batch['gst_percentage']),
                        'expiry_date':   str(batch['expiry_date']),
                        'is_available':  is_avail,
                    })
                    if is_avail:
                        has_stock = True

                if has_stock:
                    medicines_with_stock += 1

            medicines_data.append({
                'prescription_item_id': item.item_id,
                'medicine_id':          item.medicine.medicine_id if item.medicine else None,
                'medicine_name':        item.medicine_name or getattr(item, 'display_name', ''),
                'dose_quantity':        str(item.dose_quantity),
                'prescribed_quantity':  item.quantity,
                'calculated_quantity':  item.calculated_quantity,
                'quantity_source':      item.quantity_source,
                'frequency':            item.frequency,
                'frequency_display':    item.get_frequency_display(),
                'meal_timing':          item.meal_timing,
                'meal_timing_display':  item.get_meal_timing_display(),
                'route':                item.route,
                'route_display':        item.get_route_display(),
                'duration_days':        item.duration_days,
                'prn_reason':           item.prn_reason,
                'prn_reason_display':   item.get_prn_reason_display() if item.prn_reason else '',
                'prn_reason_other':     item.prn_reason_other or '',
                'max_daily_dose':       item.max_daily_dose or '',
                'instructions':         item.instructions or '',
                'available_batches':    batches,
                'has_available_batches': has_stock,
            })

        response_data = {
            'prescription': {
                'prescription_id':   prescription.prescription_id,
                'prescription_type': prescription.prescription_type,
                'created_at':        prescription.created_at.isoformat(),
                'notes':             prescription.notes or '',
            },
            'medicines':   medicines_data,
            'total_items': len(medicines_data),
            'summary': {
                'total_medicines':          len(medicines_data),
                'medicines_with_stock':     medicines_with_stock,
                'medicines_without_stock':  len(medicines_data) - medicines_with_stock,
                'total_prescribed_quantity': total_prescribed_qty,
            },
        }

        if prescription.consultation:
            consultation = prescription.consultation
            patient      = consultation.patient
            response_data['consultation_details'] = {
                'consultation_id':   consultation.consultation_id,
                'consultation_date': str(consultation.consultation_date),
                'patient_id':        patient.patient_id if patient else None,
                'patient_name': (
                    f'{patient.first_name} {patient.last_name}'.strip() if patient else 'N/A'
                ),
            }

        return Response(response_data)


# ═══════════════════════════════════════════════════════════════════
# DASHBOARD SUMMARY
# GET /api/pharmacist/dashboard/
# ═══════════════════════════════════════════════════════════════════

class DashboardSummaryView(APIView):
    """
    GET /api/pharmacist/dashboard/

    Quick stats for the pharmacist dashboard:
    - today's bills breakdown
    - revenue today
    - stock alert counts
    - recent paid bills
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        today = timezone.localdate()

        bills_today = scope_queryset_to_branch(
            PharmacyBill.objects.filter(bill_date=today), request.user, branch_field='branch', branch_id=request.query_params.get('branch')
        )
        open_bills      = bills_today.filter(bill_status='OPEN').count()
        completed_bills = bills_today.filter(bill_status='COMPLETED').count()
        paid_bills      = bills_today.filter(bill_status='PAID').count()
        walkin_bills    = bills_today.filter(is_walkin=True).count()
        registered_bills = bills_today.filter(is_walkin=False).count()

        revenue_today = (
            bills_today.filter(bill_status='PAID')
            .aggregate(total=Sum('total_amount'))['total'] or 0
        )
        walkin_revenue = (
            bills_today.filter(bill_status='PAID', is_walkin=True)
            .aggregate(total=Sum('total_amount'))['total'] or 0
        )

        alerts_qs = scope_queryset_to_branch(
            StockAlert.objects.all(), request.user, branch_field='batch__medicine__branch', branch_id=request.query_params.get('branch')
        )
        low_stock_alerts = alerts_qs.filter(alert_type='LOW_STOCK', is_resolved=False).count()
        expiry_alerts    = alerts_qs.filter(alert_type='EXPIRY',    is_resolved=False).count()

        recent_bills = scope_queryset_to_branch(
            PharmacyBill.objects.filter(bill_status='PAID'), request.user, branch_field='branch', branch_id=request.query_params.get('branch')
        ).order_by('-bill_id')[:5]

        return Response({
            'today': str(today),
            'bills': {
                'open':       open_bills,
                'completed':  completed_bills,
                'paid':       paid_bills,
                'total':      open_bills + completed_bills + paid_bills,
                'walkin':     walkin_bills,
                'registered': registered_bills,
            },
            'revenue_today':        float(revenue_today),
            'walkin_revenue_today': float(walkin_revenue),
            'alerts': {
                'low_stock': low_stock_alerts,
                'expiry':    expiry_alerts,
                'total':     low_stock_alerts + expiry_alerts,
            },
            'recent_paid_bills': PharmacyBillSerializer(recent_bills, many=True).data,
        })


# ═══════════════════════════════════════════════════════════════════
# EXPIRING BATCHES
# ═══════════════════════════════════════════════════════════════════

class ExpiringBatchesListView(APIView):
    """
    GET /api/pharmacist/batches/expiring/
    
    List batches expiring within 30 days.
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        today = timezone.localdate()
        thirty_days_ahead = today + timedelta(days=30)

        qs = MedicineBatch.objects.filter(
            expiry_date__lte=thirty_days_ahead,
            expiry_date__gte=today,
            status='ACTIVE',
            quantity__gt=0
        ).order_by('expiry_date')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='medicine__branch', branch_id=request.query_params.get('branch'))

        serializer = MedicineBatchDetailSerializer(qs, many=True)
        return Response(serializer.data)


# ═══════════════════════════════════════════════════════════════════
# DEPLETED BATCHES AUDIT
# ═══════════════════════════════════════════════════════════════════

class DepletedBatchesAuditView(APIView):
    """
    GET /api/pharmacist/batches/depleted/
    
    Audit report: List all completely sold/depleted batches.
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        qs = MedicineBatch.objects.filter(status='DEPLETED').order_by('-updated_at')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='medicine__branch', branch_id=request.query_params.get('branch'))

        data = []
        for batch in qs:
            last_log = batch.stock_logs.order_by('-created_at').first()
            data.append({
                'batch_id': batch.batch_id,
                'medicine_name': batch.medicine.name,
                'batch_number': batch.batch_number,
                'original_mrp': float(batch.mrp),
                'status': batch.status,
                'depleted_date': batch.updated_at,
                'last_stock_movement': last_log.created_at if last_log else None,
                'last_stock_movement_type': last_log.change_type if last_log else None,
            })

        return Response(data)


# ═══════════════════════════════════════════════════════════════════
# MEDICINE RETURN TO PROVIDER - LIST VIEW
# ═══════════════════════════════════════════════════════════════════

class MedicineReturnToProviderListView(APIView):
    """
    GET /api/pharmacist/medicine-returns-to-provider/
    
    List all return to provider requests with filtering options
    
    Query Parameters:
    - status: Filter by status (REQUESTED, APPROVED, REJECTED, COMPLETED)
    - batch_id: Filter by batch
    - medicine_id: Filter by medicine
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        returns = MedicineReturnToProvider.objects.select_related(
            'batch__medicine',
            'requested_by',
            'approved_by'
        )
        returns = scope_queryset_to_branch(returns, request.user, branch_field='batch__medicine__branch', branch_id=request.query_params.get('branch'))
        
        status_filter = request.query_params.get('status', '').strip()
        if status_filter:
            returns = returns.filter(status=status_filter)
        
        batch_id = request.query_params.get('batch_id', '').strip()
        if batch_id:
            try:
                batch_id = int(batch_id)
                returns = returns.filter(batch_id=batch_id)
            except (ValueError, TypeError):
                pass
        
        medicine_id = request.query_params.get('medicine_id', '').strip()
        if medicine_id:
            try:
                medicine_id = int(medicine_id)
                returns = returns.filter(batch__medicine_id=medicine_id)
            except (ValueError, TypeError):
                pass
        
        returns = returns.order_by('-created_at')
        
        serializer = MedicineReturnToProviderSerializer(returns, many=True)
        return Response({
            'count': returns.count(),
            'results': serializer.data
        })


# ═══════════════════════════════════════════════════════════════════
# MEDICINE RETURN TO PROVIDER - CREATE VIEW
# ═══════════════════════════════════════════════════════════════════

class MedicineReturnToProviderCreateView(APIView):
    """
    POST /api/pharmacist/medicine-returns-to-provider/
    
    Create a new return to provider request
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request):
        branch = get_user_branch(request.user) if not is_group_admin_user(request.user) else None
        serializer = MedicineReturnToProviderCreateSerializer(data=request.data, context={'branch': branch})
        
        if not serializer.is_valid():
            return Response(
                {
                    'errors': serializer.errors,
                    'message': 'Invalid return to provider request'
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        medicine_return = serializer.save(requested_by=request.user)
        response_serializer = MedicineReturnToProviderSerializer(medicine_return)
        
        return Response(
            {
                'message': f"Return request created successfully. Refund due: ₹{medicine_return.refund_amount}.",
                'return': response_serializer.data
            },
            status=status.HTTP_201_CREATED
        )


# ═══════════════════════════════════════════════════════════════════
# MEDICINE RETURN TO PROVIDER - DETAIL VIEW
# ═══════════════════════════════════════════════════════════════════

class MedicineReturnToProviderDetailView(APIView):
    """
    GET /api/pharmacist/medicine-returns-to-provider/<return_id>/
    
    Get details of a specific return request
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request, return_id):
        try:
            return_id = int(return_id)
        except (ValueError, TypeError):
            return Response(
                {'error': 'Invalid return ID'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        qs = scope_queryset_to_branch(
            MedicineReturnToProvider.objects.all(), request.user, branch_field='batch__medicine__branch'
        )
        return_obj = get_object_or_404(qs, pk=return_id)
        serializer = MedicineReturnToProviderSerializer(return_obj)
        return Response(serializer.data)


# ═══════════════════════════════════════════════════════════════════
# MEDICINE RETURN TO PROVIDER - APPROVE VIEW
# ═══════════════════════════════════════════════════════════════════

class MedicineReturnToProviderApproveView(APIView):
    """
    POST /api/pharmacist/medicine-returns-to-provider/<return_id>/approve/
    
    Approve or reject a return request
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request, return_id):
        try:
            return_id = int(return_id)
        except (ValueError, TypeError):
            return Response(
                {'error': 'Invalid return ID'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        qs = scope_queryset_to_branch(
            MedicineReturnToProvider.objects.all(), request.user, branch_field='batch__medicine__branch'
        )
        return_obj = get_object_or_404(qs, pk=return_id)
        
        if return_obj.status != 'REQUESTED':
            return Response(
                {'error': f'Cannot approve: status is {return_obj.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        new_status = request.data.get('status', '').strip()
        if new_status not in ['APPROVED', 'REJECTED']:
            return Response(
                {'error': 'Status must be APPROVED or REJECTED'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        return_obj.status = new_status
        return_obj.approved_by = request.user
        return_obj.save()
        
        serializer = MedicineReturnToProviderSerializer(return_obj)
        return Response({
            'message': f'Return request {new_status.lower()} successfully',
            'return': serializer.data
        })


# ═══════════════════════════════════════════════════════════════════
# MEDICINE RETURN TO PROVIDER - COMPLETE VIEW
# ═══════════════════════════════════════════════════════════════════

class MedicineReturnToProviderCompleteView(APIView):
    """
    POST /api/pharmacist/medicine-returns-to-provider/<return_id>/complete/
    
    Mark an approved return as completed (deduct from stock)
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request, return_id):
        try:
            return_id = int(return_id)
        except (ValueError, TypeError):
            return Response(
                {'error': 'Invalid return ID'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        qs = scope_queryset_to_branch(
            MedicineReturnToProvider.objects.all(), request.user, branch_field='batch__medicine__branch'
        )
        return_obj = get_object_or_404(qs, pk=return_id)
        
        if return_obj.status != 'APPROVED':
            return Response(
                {'error': f'Cannot complete: status is {return_obj.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Note: stock is already deducted at creation time (see
        # MedicineReturnToProviderCreateSerializer.save()). This endpoint
        # only updates the workflow status - it is not currently used by
        # the frontend, which treats return creation as a single-step
        # completed action.
        return_obj.status = 'COMPLETED'
        return_obj.save()
        
        serializer = MedicineReturnToProviderSerializer(return_obj)
        return Response({
            'message': 'Return completed successfully',
            'return': serializer.data
        })


# ═══════════════════════════════════════════════════════════════════
# MEDICINES FOR RETURN LIST
# ═══════════════════════════════════════════════════════════════════

class MedicinesForReturnListView(APIView):
    """
    GET /api/pharmacist/medicines-for-return/
    
    Get list of medicines available for return (with active batches)
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        medicines = Medicine.objects.filter(is_active=True).prefetch_related('batches')
        medicines = scope_queryset_to_branch(medicines, request.user, branch_field='branch', branch_id=request.query_params.get('branch'))
        
        q = request.query_params.get('q', '').strip()
        if q:
            medicines = medicines.filter(
                Q(name__icontains=q) | Q(generic_name__icontains=q)
            )
        
        category = request.query_params.get('category', '').strip()
        if category:
            medicines = medicines.filter(category=category)
        
        medicines_with_batches = []
        for medicine in medicines:
            active_batches = medicine.batches.filter(status='ACTIVE')
            if active_batches.exists():
                medicines_with_batches.append(medicine)
        
        serializer = MedicineWithBatchesSerializer(medicines_with_batches, many=True)
        return Response({
            'count': len(medicines_with_batches),
            'results': serializer.data
        })


# ═══════════════════════════════════════════════════════════════════
# BATCHES FOR RETURN LIST
# ═══════════════════════════════════════════════════════════════════

class BatchesForReturnListView(APIView):
    """
    GET /api/pharmacist/batches-for-return/
    
    Get list of batches available for return
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        batches = MedicineBatch.objects.filter(status='ACTIVE').select_related('medicine')
        batches = scope_queryset_to_branch(batches, request.user, branch_field='medicine__branch', branch_id=request.query_params.get('branch'))
        
        medicine_id = request.query_params.get('medicine_id', '').strip()
        if medicine_id:
            try:
                medicine_id = int(medicine_id)
                batches = batches.filter(medicine_id=medicine_id)
            except (ValueError, TypeError):
                pass
        
        batch_number = request.query_params.get('batch_number', '').strip()
        if batch_number:
            batches = batches.filter(batch_number__icontains=batch_number)
        
        batches = [b for b in batches if (b.quantity - b.allocated_quantity) > 0]
        
        serializer = MedicineBatchDetailSerializer(batches, many=True)
        return Response({
            'count': len(batches),
            'results': serializer.data
        })


# ═══════════════════════════════════════════════════════════════════
# BATCH DETAIL FOR RETURN
# ═══════════════════════════════════════════════════════════════════

class BatchDetailForReturnView(APIView):
    """
    GET /api/pharmacist/batches/<batch_id>/for-return/
    
    Get detailed batch info for return purposes
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request, batch_id):
        try:
            batch_id = int(batch_id)
        except (ValueError, TypeError):
            return Response(
                {'error': 'Invalid batch ID'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        qs = scope_queryset_to_branch(MedicineBatch.objects.all(), request.user, branch_field='medicine__branch')
        batch = get_object_or_404(qs, pk=batch_id, status='ACTIVE')
        serializer = MedicineBatchDetailSerializer(batch)
        
        return Response({
            'batch': serializer.data,
            'available_for_return': batch.quantity - batch.allocated_quantity,
            'is_returnable': batch.quantity > batch.allocated_quantity
        })

# ════════════════════════════════════════════════════════════════
# ✓ CONFIRM DISPENSING ENDPOINT
# ════════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def confirm_dispensing_view(request, bill_id):
    """
    ✓ CONFIRM DISPENSING ENDPOINT
    
    Purpose:
    Mark a PAID bill as having been physically dispensed by the pharmacist.
    This records:
    - dispensing_confirmed = True
    - dispensing_confirmed_at = current timestamp
    - dispensing_confirmed_by = current user (pharmacist)
    
    Workflow:
    1. Bill is in PAID status
    2. Pharmacist clicks "Complete Dispensing" button in dispense modal
    3. Frontend calls this endpoint
    4. This endpoint marks dispensing_confirmed=True in database
    5. Button changes to "✓ Dispensing Complete - Stock Deducted" (persisted)
    
    Access:
    POST /api/pharmacist/bills/{bill_id}/confirm-dispensing/
    
    Response:
    {
        "message": "✓ Dispensing confirmed successfully",
        "bill": { ... full bill serialized data ... }
    }
    """
    try:
        bill = scope_queryset_to_branch(
            PharmacyBill.objects.all(), request.user, branch_field='branch'
        ).get(bill_id=bill_id)
        
        # ✓ Validation: Only PAID bills can have dispensing confirmed
        if bill.bill_status != 'PAID':
            return Response(
                {
                    'error': f'Cannot confirm dispensing for {bill.bill_status} bill. Only PAID bills can be confirmed.',
                    'current_status': bill.bill_status
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # ✓ Prevent double-confirmation
        if bill.dispensing_confirmed:
            return Response(
                {
                    'message': '✓ Dispensing already confirmed on ' + str(bill.dispensing_confirmed_at),
                    'bill': PharmacyBillSerializer(bill).data
                },
                status=status.HTTP_200_OK
            )
        
        # ✓ Mark as dispensing confirmed
        bill.dispensing_confirmed = True
        bill.dispensing_confirmed_at = timezone.now()
        bill.dispensing_confirmed_by = request.user
        bill.save(update_fields=['dispensing_confirmed', 'dispensing_confirmed_at', 'dispensing_confirmed_by'])
        
        serializer = PharmacyBillSerializer(bill)
        return Response(
            {
                'message': '✓ Dispensing confirmed successfully',
                'bill': serializer.data
            },
            status=status.HTTP_200_OK
        )
    
    except PharmacyBill.DoesNotExist:
        return Response(
            {'error': 'Bill not found', 'bill_id': bill_id},
            status=status.HTTP_404_NOT_FOUND
        )
    except Exception as e:
        logger.warning("Failed to confirm dispensing: %s", e)
        return Response(
            {'error': 'Failed to confirm dispensing: ' + _safe_detail(e)},
            status=status.HTTP_400_BAD_REQUEST
        )

# ═════════════════════════════════════════════════════════════════════════
# SUPPLIES & CONSUMABLES
# ═════════════════════════════════════════════════════════════════════════
# Stock system for hospital consumables (syringes, gloves, IV sets,
# dressings, PPE, etc) — separate from Medicine/MedicineBatch/PharmacyBill.
# ═════════════════════════════════════════════════════════════════════════

class SupplyDashboardSummaryView(APIView):
    """GET /api/pharmacist/supplies/dashboard/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        today = timezone.localdate()

        active_items = scope_queryset_to_branch(
            SupplyItem.objects.filter(is_active=True), request.user, branch_field='branch', branch_id=request.query_params.get('branch')
        )
        total_items = active_items.count()

        low_stock_count = sum(1 for item in active_items if item.is_low_stock)

        total_stock_value = scope_queryset_to_branch(
            SupplyBatch.objects.filter(supply_item__is_active=True, status='ACTIVE'),
            request.user, branch_field='supply_item__branch', branch_id=request.query_params.get('branch'),
        ).aggregate(
            value=Sum(models.F('quantity') * models.F('unit_cost'), output_field=models.DecimalField())
        )['value'] or 0

        alerts_open = scope_queryset_to_branch(
            SupplyStockAlert.objects.filter(is_resolved=False), request.user, branch_field='supply_item__branch', branch_id=request.query_params.get('branch')
        ).count()

        categories_breakdown = []
        for cat_key, cat_label in SupplyItem._meta.get_field('category').choices:
            cat_items = active_items.filter(category=cat_key)
            cat_item_count = cat_items.count()
            if cat_item_count == 0:
                continue
            cat_value = SupplyBatch.objects.filter(
                supply_item__in=cat_items, status='ACTIVE'
            ).aggregate(
                value=Sum(models.F('quantity') * models.F('unit_cost'), output_field=models.DecimalField())
            )['value'] or 0
            categories_breakdown.append({
                'category': cat_key,
                'category_display': cat_label,
                'item_count': cat_item_count,
                'total_stock_value': cat_value,
            })

        recent_usage = scope_queryset_to_branch(
            SupplyUsageLog.objects.select_related('supply_item', 'used_by'),
            request.user, branch_field='supply_item__branch', branch_id=request.query_params.get('branch'),
        ).order_by('-used_at')[:5]

        expiring_soon = scope_queryset_to_branch(
            SupplyBatch.objects.select_related('supply_item').filter(
                expiry_date__isnull=False,
                expiry_date__lte=today + timedelta(days=30),
                quantity__gt=0,
                status='ACTIVE',
            ),
            request.user, branch_field='supply_item__branch', branch_id=request.query_params.get('branch'),
        ).order_by('expiry_date')

        return Response({
            'total_items': total_items,
            'low_stock_count': low_stock_count,
            'total_stock_value': total_stock_value,
            'alerts_open': alerts_open,
            'categories_breakdown': categories_breakdown,
            'recent_usage': SupplyUsageLogSerializer(recent_usage, many=True).data,
            'expiring_soon': SupplyBatchSerializer(expiring_soon, many=True).data,
        })


class SupplyItemListView(APIView):
    """GET /api/pharmacist/supplies/items/ — Query: search, category, low_stock=true, is_active=true/false"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        qs = SupplyItem.objects.all()
        qs = scope_queryset_to_branch(qs, request.user, branch_field='branch', branch_id=request.query_params.get('branch'))

        is_active_param = request.query_params.get('is_active')
        if is_active_param is None:
            qs = qs.filter(is_active=True)
        elif is_active_param.lower() != 'all':
            qs = qs.filter(is_active=is_active_param.lower() == 'true')

        category = request.query_params.get('category')
        if category:
            qs = qs.filter(category=category.upper())

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(description__icontains=search))

        qs = qs.order_by('category', 'name')

        items = list(qs)
        if request.query_params.get('low_stock', '').lower() == 'true':
            items = [item for item in items if item.is_low_stock]

        return Response(SupplyItemSerializer(items, many=True).data)


class SupplyItemCreateView(APIView):
    """POST /api/pharmacist/supplies/items/create/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def post(self, request):
        branch, error = resolve_branch_for_write(request, required=True)
        if error:
            return error

        serializer = SupplyItemWriteSerializer(data=request.data, context={'branch': branch})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        item = SupplyItem.objects.create(branch=branch, **serializer.validated_data)
        return Response(
            {'message': f"Supply item '{item.name}' created.", 'item': SupplyItemSerializer(item).data},
            status=status.HTTP_201_CREATED,
        )


class SupplyItemDetailView(APIView):
    """GET /api/pharmacist/supplies/items/<item_id>/ — item + batches + recent usage"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request, item_id):
        qs = scope_queryset_to_branch(SupplyItem.objects.all(), request.user, branch_field='branch')
        item = get_object_or_404(qs, pk=item_id)

        batches = item.batches.order_by('-purchase_date', '-batch_id')
        non_empty = [b for b in batches if b.quantity > 0]
        empty = [b for b in batches if b.quantity == 0]
        ordered_batches = non_empty + empty

        recent_logs = item.usage_logs.select_related('used_by').order_by('-used_at')[:10]
        recent_returns = item.returns.select_related('batch', 'returned_by').order_by('-returned_at')[:10]

        data = SupplyItemSerializer(item).data
        data['batches'] = SupplyBatchSerializer(ordered_batches, many=True).data
        data['recent_usage'] = SupplyUsageLogSerializer(recent_logs, many=True).data
        data['recent_returns'] = SupplyReturnSerializer(recent_returns, many=True).data
        return Response(data)


class SupplyItemUpdateView(APIView):
    """PATCH /api/pharmacist/supplies/items/<item_id>/update/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def patch(self, request, item_id):
        qs = scope_queryset_to_branch(SupplyItem.objects.all(), request.user, branch_field='branch')
        item = get_object_or_404(qs, pk=item_id)

        serializer = SupplyItemWriteSerializer(
            data=request.data, partial=True, context={'instance': item, 'branch': item.branch}
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        threshold_changed = 'low_stock_threshold' in serializer.validated_data

        for attr, value in serializer.validated_data.items():
            setattr(item, attr, value)
        item.save()

        if threshold_changed:
            _check_supply_alerts(item)

        return Response({'message': 'Supply item updated.', 'item': SupplyItemSerializer(item).data})


class AddStockView(APIView):
    """POST /api/pharmacist/supplies/items/<item_id>/add-stock/ — create a purchase batch"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request, item_id):
        qs = scope_queryset_to_branch(SupplyItem.objects.all(), request.user, branch_field='branch')
        item = get_object_or_404(qs, pk=item_id)

        serializer = SupplyBatchWriteSerializer(data=request.data, context={'supply_item': item})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        quantity = data['quantity']

        dealer_id = data.get('dealer_id')
        if dealer_id:
            # Scoped to this supply item's branch — same rule as the
            # medicine-batch dealer lookup above.
            from manager.models import Dealer
            get_object_or_404(Dealer, pk=dealer_id, branch=item.branch)

        batch = SupplyBatch(
            supply_item=item,
            batch_number=data['batch_number'],
            quantity=quantity,
            original_quantity=quantity,
            unit_cost=data.get('unit_cost', 0),
            supplier_name=data.get('supplier_name'),
            supplier_contact=data.get('supplier_contact'),
            invoice_number=data.get('invoice_number'),
            dealer_id=dealer_id or None,
            settlement_method=data.get('settlement_method') or None,
            purchase_date=data.get('purchase_date') or timezone.localdate(),
            expiry_date=data.get('expiry_date'),
            notes=data.get('notes'),
            received_by=request.user if request.user and request.user.is_authenticated else None,
            # ✅ NEW: same manager-approval gate as MedicineBatch/
            # GeneralItemBatch — see SupplyBatch.BATCH_STATUS_CHOICES.
            status='PENDING_APPROVAL' if dealer_id else 'ACTIVE',
        )
        try:
            batch.save()
        except (ValidationError, IntegrityError) as exc:
            if hasattr(exc, "messages") and exc.messages:
                message = exc.messages[0]
            else:
                logger.warning("Batch save failed: %s", exc)
                message = _safe_detail(exc)
            return Response({'error': message}, status=status.HTTP_400_BAD_REQUEST)

        _check_supply_alerts(item)

        # OPTIONAL dealer link → auto-log a PENDING purchase transaction.
        if batch.dealer_id:
            from manager.models import create_dealer_transaction
            create_dealer_transaction(
                dealer=batch.dealer,
                transaction_type='PURCHASE',
                amount=batch.total_cost,
                source_model='SUPPLY_BATCH',
                source_id=batch.batch_id,
                created_by=request.user,
                settlement_method=batch.settlement_method,
                reference_number=batch.invoice_number,
                notes=f"Stock purchase: {item.name} × {quantity} (batch {batch.batch_number}).",
            )

        return Response(
            {
                'message': f"Stock added for '{item.name}'.",
                'batch': SupplyBatchSerializer(batch).data,
                'total_stock': item.total_stock,
            },
            status=status.HTTP_201_CREATED,
        )


class UseStockView(APIView):
    """POST /api/pharmacist/supplies/items/<item_id>/use/ — FIFO deduction"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request, item_id):
        qs = scope_queryset_to_branch(SupplyItem.objects.all(), request.user, branch_field='branch')
        item = get_object_or_404(qs, pk=item_id)

        serializer = UseStockSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        quantity_needed = data['quantity_used']

        available = item.total_stock
        if available < quantity_needed:
            return Response(
                {'error': 'Insufficient stock.', 'available_stock': available},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batches = list(
            item.batches.select_for_update()
            .filter(quantity__gt=0, status='ACTIVE')
            .order_by('batch_id')
        )

        remaining = quantity_needed
        primary_batch = batches[0] if batches else None

        for batch in batches:
            if remaining <= 0:
                break
            deduct = min(batch.quantity, remaining)
            batch.quantity -= deduct
            batch.save(update_fields=['quantity'])
            remaining -= deduct

        log = SupplyUsageLog.objects.create(
            supply_item=item,
            batch=primary_batch,
            quantity_used=quantity_needed,
            department=data.get('department'),
            used_by=request.user if request.user and request.user.is_authenticated else None,
            notes=data.get('notes'),
            balance_after=item.total_stock,
        )

        _check_supply_alerts(item)

        return Response({
            'message': f"{quantity_needed} of '{item.name}' issued.",
            'log': SupplyUsageLogSerializer(log).data,
            'total_stock': item.total_stock,
            'is_low_stock': item.is_low_stock,
        })


class SupplyBatchDetailView(APIView):
    """GET /api/pharmacist/supplies/batches/<batch_id>/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request, batch_id):
        qs = scope_queryset_to_branch(
            SupplyBatch.objects.select_related('supply_item', 'received_by'),
            request.user, branch_field='supply_item__branch',
        )
        batch = get_object_or_404(qs, pk=batch_id)
        return Response(SupplyBatchSerializer(batch).data)


class ReturnToProviderView(APIView):
    """POST /api/pharmacist/supplies/batches/<batch_id>/return/ — send stock back to the supplier."""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request, batch_id):
        qs = scope_queryset_to_branch(
            SupplyBatch.objects.select_for_update(), request.user, branch_field='supply_item__branch'
        )
        batch = get_object_or_404(qs, pk=batch_id)
        item = batch.supply_item

        serializer = ReturnToProviderSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        qty = data['quantity_returned']

        if qty > batch.quantity:
            return Response(
                {'error': f"Only {batch.quantity} unit(s) remain in this batch.", 'available_stock': batch.quantity},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch.quantity -= qty
        batch.save(update_fields=['quantity'])

        # Refund is what the supplier owes back — the purchase (unit) cost,
        # never an MRP/selling price (supplies don't have one anyway, but
        # this mirrors the same rule applied to medicine returns).
        refund_amount = qty * batch.unit_cost

        # OPTIONAL dealer link — defaults to whoever supplied this batch,
        # but can be overridden (e.g. returning to a different dealer).
        dealer_id = data.get('dealer_id') or batch.dealer_id
        if dealer_id:
            # Scoped to this supply item's branch — same rule as the
            # purchase-side dealer lookup.
            from manager.models import Dealer
            get_object_or_404(Dealer, pk=dealer_id, branch=item.branch)

        supply_return = SupplyReturn.objects.create(
            supply_item=item,
            batch=batch,
            quantity_returned=qty,
            refund_amount=refund_amount,
            reason=data['reason'],
            reason_details=data.get('reason_details') or '',
            reference_number=data.get('reference_number') or '',
            dealer_id=dealer_id or None,
            settlement_method=data.get('settlement_method') or None,
            returned_by=request.user if request.user and request.user.is_authenticated else None,
        )

        _check_supply_alerts(item)

        # OPTIONAL dealer link → auto-log a PENDING credit-note transaction.
        if supply_return.dealer_id:
            from manager.models import create_dealer_transaction
            create_dealer_transaction(
                dealer=supply_return.dealer,
                transaction_type='CREDIT_NOTE',
                amount=supply_return.refund_amount,
                source_model='SUPPLY_RETURN',
                source_id=supply_return.return_id,
                created_by=request.user,
                settlement_method=supply_return.settlement_method,
                reference_number=supply_return.reference_number,
                notes=f"Return: {item.name} × {qty} ({supply_return.get_reason_display()}), batch {batch.batch_number}.",
            )

        return Response(
            {
                'message': f"{qty} of '{item.name}' returned to provider. Refund due: ₹{refund_amount}.",
                'return': SupplyReturnSerializer(supply_return).data,
                'total_stock': item.total_stock,
                'is_low_stock': item.is_low_stock,
            },
            status=status.HTTP_201_CREATED,
        )


class SupplyReturnListView(APIView):
    """GET /api/pharmacist/supplies/returns/ — Query: item_id, date_from, date_to, limit"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        qs = SupplyReturn.objects.select_related('supply_item', 'batch', 'returned_by').order_by('-returned_at')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='supply_item__branch', branch_id=request.query_params.get('branch'))

        item_id = request.query_params.get('item_id')
        if item_id:
            qs = qs.filter(supply_item_id=item_id)

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(supply_item__name__icontains=search)

        # FIX: `__date__gte/lte` on a DateTimeField forces MySQL to run
        # DATE(CONVERT_TZ(...)) under the hood, which silently matches
        # nothing if the server's mysql.time_zone_name tables aren't loaded
        # (see manager/views.py::_local_day_range for the full story).
        # Parsing to an aware local-day boundary sidesteps CONVERT_TZ.
        date_from = request.query_params.get('date_from')
        if date_from:
            try:
                lo = timezone.make_aware(datetime.combine(date.fromisoformat(date_from), time.min))
                qs = qs.filter(returned_at__gte=lo)
            except ValueError:
                pass

        date_to = request.query_params.get('date_to')
        if date_to:
            try:
                hi = timezone.make_aware(datetime.combine(date.fromisoformat(date_to), time.max))
                qs = qs.filter(returned_at__lte=hi)
            except ValueError:
                pass

        try:
            limit = int(request.query_params.get('limit', 50))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 500))

        total_count = qs.count()
        qs = qs[:limit]

        return Response({
            'results': SupplyReturnSerializer(qs, many=True).data,
            'total_count': total_count,
        })


class UsageLogListView(APIView):
    """GET /api/pharmacist/supplies/usage-logs/ — Query: item_id, department, date_from, date_to, limit"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        qs = SupplyUsageLog.objects.select_related('supply_item', 'used_by').order_by('-used_at')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='supply_item__branch', branch_id=request.query_params.get('branch'))

        item_id = request.query_params.get('item_id')
        if item_id:
            qs = qs.filter(supply_item_id=item_id)

        department = request.query_params.get('department', '').strip()
        if department:
            qs = qs.filter(department__icontains=department)

        category = request.query_params.get('category')
        if category:
            qs = qs.filter(supply_item__category=category.upper())

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(supply_item__name__icontains=search)

        # FIX: see SupplyReturnListView above — avoid CONVERT_TZ-dependent
        # __date lookups on a DateTimeField.
        date_from = request.query_params.get('date_from')
        if date_from:
            try:
                lo = timezone.make_aware(datetime.combine(date.fromisoformat(date_from), time.min))
                qs = qs.filter(used_at__gte=lo)
            except ValueError:
                pass

        date_to = request.query_params.get('date_to')
        if date_to:
            try:
                hi = timezone.make_aware(datetime.combine(date.fromisoformat(date_to), time.max))
                qs = qs.filter(used_at__lte=hi)
            except ValueError:
                pass

        try:
            limit = int(request.query_params.get('limit', 50))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 500))

        total_count = qs.count()
        qs = qs[:limit]

        return Response({
            'results': SupplyUsageLogSerializer(qs, many=True).data,
            'total_count': total_count,
        })


class AlertListView(APIView):
    """GET /api/pharmacist/supplies/alerts/ — Query: resolved=true (default false)"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        resolved = request.query_params.get('resolved', 'false').lower() == 'true'
        qs = SupplyStockAlert.objects.select_related('supply_item').filter(
            is_resolved=resolved
        ).order_by('-created_at')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='supply_item__branch', branch_id=request.query_params.get('branch'))
        return Response(SupplyStockAlertSerializer(qs, many=True).data)


class AlertResolveView(APIView):
    """POST /api/pharmacist/supplies/alerts/<alert_id>/resolve/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def post(self, request, alert_id):
        qs = scope_queryset_to_branch(SupplyStockAlert.objects.all(), request.user, branch_field='supply_item__branch')
        alert = get_object_or_404(qs, pk=alert_id)
        alert.is_resolved = True
        alert.save(update_fields=['is_resolved'])
        return Response({'message': 'Alert resolved.', 'alert': SupplyStockAlertSerializer(alert).data})

# ═══════════════════════════════════════════════════════════════════
# GENERAL ITEMS — catalog (non-medicine retail products)
# ═══════════════════════════════════════════════════════════════════

class GeneralItemListView(APIView):
    """GET /api/pharmacist/general-items/  — Query: search, category, show_inactive=true"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        qs = GeneralItem.objects.prefetch_related('batches').order_by('name')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='branch', branch_id=request.query_params.get('branch'))

        if request.query_params.get('show_inactive', 'false').lower() != 'true':
            qs = qs.filter(is_active=True)

        search = request.query_params.get('search', '').strip()
        if search:
            # Category is stored as a short code (e.g. 'BABY_CARE'), so also match
            # against its human-readable label (e.g. 'Baby Care') for a natural search.
            matching_category_codes = [
                code for code, label in GeneralItemCategoryChoices.choices
                if search.lower() in label.lower()
            ]
            category_filter = Q(category__icontains=search)
            if matching_category_codes:
                category_filter |= Q(category__in=matching_category_codes)
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(brand__icontains=search) |
                Q(description__icontains=search) |
                category_filter
            )

        category = request.query_params.get('category', '').strip()
        if category:
            qs = qs.filter(category__iexact=category)

        data = GeneralItemWithBatchesSerializer(qs, many=True).data

        if request.query_params.get('in_stock', 'false').lower() == 'true':
            data = [i for i in data if i['total_stock'] > 0]

        return Response(data)


class GeneralItemDetailView(APIView):
    """GET /api/pharmacist/general-items/<pk>/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request, pk):
        qs = scope_queryset_to_branch(
            GeneralItem.objects.prefetch_related('batches'), request.user, branch_field='branch'
        )
        item = get_object_or_404(qs, pk=pk)
        return Response(GeneralItemWithBatchesSerializer(item).data)


class GeneralItemCreateView(APIView):
    """POST /api/pharmacist/general-items/create/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def post(self, request):
        branch, error = resolve_branch_for_write(request, required=True)
        if error:
            return error

        serializer = GeneralItemWriteSerializer(data=request.data, context={'branch': branch})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        item = GeneralItem.objects.create(branch=branch, **serializer.validated_data)
        return Response(
            {'message': 'Item created.', 'item': GeneralItemSerializer(item).data},
            status=status.HTTP_201_CREATED,
        )


class GeneralItemUpdateView(APIView):
    """PATCH /api/pharmacist/general-items/<pk>/update/ — also used to deactivate (is_active: false)"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def patch(self, request, pk):
        qs = scope_queryset_to_branch(GeneralItem.objects.all(), request.user, branch_field='branch')
        item = get_object_or_404(qs, pk=pk)
        serializer = GeneralItemWriteSerializer(
            data=request.data, partial=True, context={'instance': item, 'branch': item.branch}
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        for attr, value in serializer.validated_data.items():
            setattr(item, attr, value)
        item.save()
        return Response({'message': 'Item updated.', 'item': GeneralItemSerializer(item).data})


class GeneralItemSearchView(APIView):
    """
    GET /api/pharmacist/general-items/search/

    Autocomplete for the pharmacist's "Add General Item" picker on a bill.
    Query params: q, limit (default 12 max 50)
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        q = request.query_params.get('q', '').strip()
        try:
            limit = min(int(request.query_params.get('limit', 12)), 50)
        except (ValueError, TypeError):
            limit = 12

        qs = GeneralItem.objects.filter(is_active=True).prefetch_related('batches')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='branch', branch_id=request.query_params.get('branch'))
        if q:
            # Category is stored as a short code (e.g. 'BABY_CARE'), so also match
            # against its human-readable label (e.g. 'Baby Care') for a natural search.
            matching_category_codes = [
                code for code, label in GeneralItemCategoryChoices.choices
                if q.lower() in label.lower()
            ]
            category_filter = Q(category__icontains=q)
            if matching_category_codes:
                category_filter |= Q(category__in=matching_category_codes)
            qs = qs.filter(
                Q(name__icontains=q) |
                Q(brand__icontains=q) |
                Q(description__icontains=q) |
                category_filter
            )
        qs = qs.order_by('name')[:limit]

        results = []
        for gi in qs:
            active_batches = [b for b in gi.batches.all() if b.status == 'ACTIVE' and b.get_available_quantity() > 0]
            total_stock = sum(b.get_available_quantity() for b in active_batches)
            stock_status = 'AVAILABLE' if total_stock > 10 else ('LOW' if total_stock > 0 else 'OUT_OF_STOCK')
            results.append({
                'id': gi.item_id,
                'item_id': gi.item_id,
                'name': gi.name,
                'brand': gi.brand or '',
                'category': gi.category,
                'category_display': gi.get_category_display(),
                'description': gi.description or '',
                'unit': gi.unit or '',
                'stock_quantity': total_stock,
                'stock_status': stock_status,
                'is_active': gi.is_active,
                'batches': GeneralItemBatchDetailSerializer(active_batches, many=True).data,
            })
        return Response({'count': len(results), 'results': results})


# ═══════════════════════════════════════════════════════════════════
# GENERAL ITEM BATCHES
# ═══════════════════════════════════════════════════════════════════

class GeneralItemBatchListView(APIView):
    """GET /api/pharmacist/general-item-batches/ — Query: general_item_id, status, available=true, search"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        qs = GeneralItemBatch.objects.select_related('general_item').order_by('general_item__name', 'expiry_date')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='general_item__branch', branch_id=request.query_params.get('branch'))

        general_item_id = request.query_params.get('general_item_id')
        if general_item_id:
            qs = qs.filter(general_item_id=general_item_id)

        # FIX: support comma-separated status values e.g. "ACTIVE,PENDING_APPROVAL"
        # so the pharmacist batch panel shows dealer-linked pending batches alongside
        # active stock, without matching the full CSV string as a single status value.
        batch_status_raw = request.query_params.get('status', '').strip()
        if batch_status_raw:
            statuses = [s.strip().upper() for s in batch_status_raw.split(',') if s.strip()]
            if len(statuses) == 1:
                qs = qs.filter(status=statuses[0])
            elif statuses:
                qs = qs.filter(status__in=statuses)

        if request.query_params.get('available', 'false').lower() == 'true':
            today = timezone.localdate()
            qs = qs.filter(status='ACTIVE', quantity__gt=0).exclude(expiry_date__lte=today)

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(batch_number__icontains=search) | Q(general_item__name__icontains=search)
            )

        return Response(GeneralItemBatchDetailSerializer(qs, many=True).data)


class GeneralItemBatchDetailView(APIView):
    """GET /api/pharmacist/general-item-batches/<pk>/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request, pk):
        qs = scope_queryset_to_branch(
            GeneralItemBatch.objects.select_related('general_item'), request.user, branch_field='general_item__branch'
        )
        batch = get_object_or_404(qs, pk=pk)
        return Response(GeneralItemBatchDetailSerializer(batch).data)


class GeneralItemBatchCreateView(APIView):
    """POST /api/pharmacist/general-item-batches/create/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request):
        serializer = GeneralItemBatchWriteSerializer(data=request.data, context={'user': request.user})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        general_item_id = serializer.validated_data.pop('general_item_id')
        gi_qs = scope_queryset_to_branch(GeneralItem.objects.all(), request.user, branch_field='branch')
        general_item = get_object_or_404(gi_qs, pk=general_item_id)

        dealer_id = serializer.validated_data.get('dealer_id')
        if dealer_id:
            # Scoped to this general item's branch — same rule as the
            # medicine/supply dealer lookups above.
            from manager.models import Dealer
            get_object_or_404(Dealer, pk=dealer_id, branch=general_item.branch)

        batch = GeneralItemBatch(general_item=general_item, **serializer.validated_data)
        # ✅ NEW: same manager-approval gate as MedicineBatch — see
        # BatchCreateView above and GeneralItemBatch.BATCH_STATUS_CHOICES.
        if batch.dealer_id:
            batch.status = 'PENDING_APPROVAL'
        batch.save()

        GeneralItemStockLog.objects.create(
            batch=batch,
            change_type='IN',
            quantity_changed=batch.quantity,
            remarks='Initial stock entry' + (' (pending manager approval)' if batch.dealer_id else ''),
        )

        # OPTIONAL dealer link → auto-log a PENDING purchase transaction,
        # same as MedicineBatch — see BatchCreateView above.
        if batch.dealer_id:
            from manager.models import create_dealer_transaction
            create_dealer_transaction(
                dealer=batch.dealer,
                transaction_type='PURCHASE',
                amount=batch.cost_price * batch.quantity,
                source_model='GENERAL_ITEM_BATCH',
                source_id=batch.batch_id,
                created_by=request.user,
                settlement_method=batch.settlement_method,
                reference_number=batch.batch_number,
                notes=f"Stock purchase: {general_item.name} × {batch.quantity} (batch {batch.batch_number}).",
            )

        return Response(
            {'message': f"Batch created for '{general_item.name}'.", 'batch': GeneralItemBatchDetailSerializer(batch).data},
            status=status.HTTP_201_CREATED,
        )


class GeneralItemBatchUpdateView(APIView):
    """PATCH /api/pharmacist/general-item-batches/<pk>/update/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def patch(self, request, pk):
        qs = scope_queryset_to_branch(
            GeneralItemBatch.objects.all(), request.user, branch_field='general_item__branch'
        )
        batch = get_object_or_404(qs, pk=pk)
        old_qty = batch.quantity

        serializer = GeneralItemBatchWriteSerializer(data=request.data, partial=True, context={'user': request.user})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = dict(serializer.validated_data)
        data.pop('general_item_id', None)
        for attr, value in data.items():
            setattr(batch, attr, value)
        batch.save()

        new_qty = batch.quantity
        if new_qty != old_qty:
            GeneralItemStockLog.objects.create(
                batch=batch,
                change_type='ADJUST',
                quantity_changed=new_qty - old_qty,
                remarks='Manual stock adjustment',
            )

        return Response({'message': 'Batch updated.', 'batch': GeneralItemBatchDetailSerializer(batch).data})


# ═══════════════════════════════════════════════════════════════════
# GENERAL ITEM STOCK ALERTS
# ═══════════════════════════════════════════════════════════════════

class GeneralItemStockAlertListView(APIView):
    """GET /api/pharmacist/general-item-alerts/ — Query: resolved=true/false"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        qs = GeneralItemStockAlert.objects.select_related('batch__general_item').order_by('-created_at')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='batch__general_item__branch', branch_id=request.query_params.get('branch'))
        resolved = request.query_params.get('resolved')
        if resolved is not None:
            qs = qs.filter(is_resolved=resolved.lower() == 'true')
        else:
            qs = qs.filter(is_resolved=False)
        return Response(GeneralItemStockAlertSerializer(qs, many=True).data)


class GeneralItemStockAlertResolveView(APIView):
    """POST /api/pharmacist/general-item-alerts/<alert_id>/resolve/"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def post(self, request, alert_id):
        qs = scope_queryset_to_branch(
            GeneralItemStockAlert.objects.all(), request.user, branch_field='batch__general_item__branch'
        )
        alert = get_object_or_404(qs, pk=alert_id)
        alert.is_resolved = True
        alert.save(update_fields=['is_resolved'])
        return Response({'message': 'Alert resolved.', 'alert': GeneralItemStockAlertSerializer(alert).data})


# ═══════════════════════════════════════════════════════════════════
# ADD / REMOVE GENERAL ITEM ON A BILL
# ═══════════════════════════════════════════════════════════════════

class AddGeneralItemView(APIView):
    """
    POST /api/pharmacist/bills/<id>/add-general-item/

    Adds a general item (diapers, tissues, soap, etc.) to an OPEN bill.
    Stock is only deducted when the bill is marked PAID — same pattern as
    AddMedicineItemView.

    { "batch_id": 1, "quantity": 2 }
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request, bill_id):
        bill = _scoped_bill_or_404(request, bill_id)

        if not bill.can_add_items():
            return Response(
                {
                    'error': (
                        f"Cannot add items to bill in '{bill.bill_status}' status. "
                        f"Bill must be 'DRAFT' or 'OPEN'."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = AddGeneralItemSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        batch_id = serializer.validated_data['batch_id']
        quantity = serializer.validated_data['quantity']
        batch    = get_object_or_404(GeneralItemBatch, pk=batch_id, general_item__branch=bill.branch)

        # ✅ NEW: a batch still awaiting manager approval (or one the
        # manager rejected) isn't sellable yet — see
        # GeneralItemBatch.BATCH_STATUS_CHOICES.
        if batch.status != 'ACTIVE':
            return Response(
                {'error': f"'{batch.general_item.name}' (batch {batch.batch_number}) is not available for sale "
                          f"— its purchase is still {batch.get_status_display().lower()}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if batch.quantity < quantity:
            return Response(
                {
                    'error': (
                        f"Insufficient stock for '{batch.general_item.name}'. "
                        f"Available: {batch.quantity}, Requested: {quantity}"
                    ),
                    'available_stock': batch.quantity,
                    'requested_quantity': quantity,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        today = timezone.localdate()
        if batch.expiry_date and batch.expiry_date <= today:
            return Response(
                {
                    'error': f'Batch {batch.batch_number} has expired ({batch.expiry_date}).',
                    'batch_number': batch.batch_number,
                    'expiry_date': str(batch.expiry_date),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        gen_item = PharmacyBillGeneralItem(bill=bill, batch=batch, quantity=quantity)
        gen_item.save()
        _recalculate_bill_totals(bill)
        bill.refresh_from_db()

        return Response(
            {
                'message': (
                    f"'{batch.general_item.name}' added to bill. "
                    "Stock will be deducted when bill is marked PAID."
                ),
                'item': PharmacyBillGeneralItemSerializer(gen_item).data,
                'bill': PharmacyBillSerializer(bill).data,
            },
            status=status.HTTP_201_CREATED,
        )


class GeneralItemReturnToProviderView(APIView):
    """POST /api/pharmacist/general-item-batches/<batch_id>/return/ — send stock back to the dealer/provider."""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def post(self, request, batch_id):
        qs = scope_queryset_to_branch(
            GeneralItemBatch.objects.select_for_update(), request.user, branch_field='general_item__branch'
        )
        batch = get_object_or_404(qs, pk=batch_id)
        item = batch.general_item

        serializer = GeneralItemReturnToProviderSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        qty = data['quantity_returned']

        if qty > batch.quantity:
            return Response(
                {'error': f"Only {batch.quantity} unit(s) remain in this batch.", 'available_stock': batch.quantity},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch.quantity -= qty
        if batch.quantity == 0 and batch.status == 'ACTIVE':
            batch.status = 'DEPLETED'
        batch.save(update_fields=['quantity', 'status'])

        GeneralItemStockLog.objects.create(
            batch=batch,
            change_type='RETURN',
            quantity_changed=-qty,
            remarks=f"Returned to provider ({data['reason']})",
        )
        _check_general_item_alerts(batch)

        # Refund is what the dealer owes back — the cost (purchase) price,
        # never the MRP/selling price.
        refund_amount = qty * batch.cost_price

        # OPTIONAL dealer link — defaults to whoever supplied this batch,
        # but can be overridden (e.g. returning to a different dealer).
        dealer_id = data.get('dealer_id') or batch.dealer_id
        if dealer_id:
            # Scoped to this general item's branch — same rule as the
            # purchase-side dealer lookup.
            from manager.models import Dealer
            get_object_or_404(Dealer, pk=dealer_id, branch=item.branch)

        gi_return = GeneralItemReturn.objects.create(
            general_item=item,
            batch=batch,
            quantity_returned=qty,
            refund_amount=refund_amount,
            reason=data['reason'],
            reason_details=data.get('reason_details') or '',
            reference_number=data.get('reference_number') or '',
            dealer_id=dealer_id or None,
            settlement_method=data.get('settlement_method') or None,
            returned_by=request.user if request.user and request.user.is_authenticated else None,
        )

        # OPTIONAL dealer link → auto-log a PENDING credit-note transaction,
        # same pattern as Medicine/Supply returns.
        if gi_return.dealer_id:
            from manager.models import create_dealer_transaction
            create_dealer_transaction(
                dealer=gi_return.dealer,
                transaction_type='CREDIT_NOTE',
                amount=gi_return.refund_amount,
                source_model='GENERAL_ITEM_RETURN',
                source_id=gi_return.return_id,
                created_by=request.user,
                settlement_method=gi_return.settlement_method,
                reference_number=gi_return.reference_number,
                notes=f"Return: {item.name} × {qty} ({gi_return.get_reason_display()}), batch {batch.batch_number}.",
            )

        return Response(
            {
                'message': f"{qty} of '{item.name}' returned to provider. Refund due: ₹{refund_amount}.",
                'return': GeneralItemReturnSerializer(gi_return).data,
                'total_stock': item.total_stock,
            },
            status=status.HTTP_201_CREATED,
        )


class GeneralItemReturnListView(APIView):
    """GET /api/pharmacist/general-item-returns/ — Query: general_item_id, date_from, date_to, limit"""
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    def get(self, request):
        qs = GeneralItemReturn.objects.select_related('general_item', 'batch', 'dealer', 'returned_by')
        qs = scope_queryset_to_branch(qs, request.user, branch_field='general_item__branch', branch_id=request.query_params.get('branch'))

        general_item_id = request.query_params.get('general_item_id')
        if general_item_id:
            qs = qs.filter(general_item_id=general_item_id)

        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        if date_from:
            qs = qs.filter(returned_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(returned_at__date__lte=date_to)

        try:
            limit = min(int(request.query_params.get('limit', 100)), 500)
        except (ValueError, TypeError):
            limit = 100

        return Response(GeneralItemReturnSerializer(qs[:limit], many=True).data)


class RemoveGeneralItemView(APIView):
    """
    PATCH  /api/pharmacist/bills/<bill_id>/general-items/<item_id>/ — update quantity
    DELETE /api/pharmacist/bills/<bill_id>/general-items/<item_id>/ — remove item
    """
    permission_classes = [IsAuthenticated, IsAdminOrPharmacist]

    @transaction.atomic
    def patch(self, request, bill_id, item_id):
        bill = _scoped_bill_or_404(request, bill_id)
        gen_item = get_object_or_404(PharmacyBillGeneralItem, pk=item_id, bill=bill)

        if bill.bill_status not in ['DRAFT', 'OPEN']:
            return Response(
                {
                    'error': f"Cannot modify items on a '{bill.bill_status}' bill.",
                    'current_status': bill.bill_status,
                    'allowed_statuses': ['DRAFT', 'OPEN'],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        quantity = request.data.get('quantity')
        if quantity is None:
            return Response({'error': 'quantity is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            quantity = int(quantity)
        except (ValueError, TypeError):
            return Response({'error': 'quantity must be an integer.'}, status=status.HTTP_400_BAD_REQUEST)
        if quantity <= 0:
            return Response({'error': 'quantity must be at least 1.'}, status=status.HTTP_400_BAD_REQUEST)

        if gen_item.batch.quantity < quantity:
            return Response(
                {
                    'error': (
                        f"Insufficient stock for '{gen_item.batch.general_item.name}'. "
                        f"Available: {gen_item.batch.quantity}, Requested: {quantity}"
                    ),
                    'available_stock': gen_item.batch.quantity,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        gen_item.quantity = quantity
        gen_item.save()
        _recalculate_bill_totals(bill)
        bill.refresh_from_db()

        return Response(
            {
                'message': f'Quantity updated to {quantity}.',
                'item': PharmacyBillGeneralItemSerializer(gen_item).data,
                'bill': PharmacyBillSerializer(bill).data,
            }
        )

    @transaction.atomic
    def delete(self, request, bill_id, item_id):
        bill = _scoped_bill_or_404(request, bill_id)
        gen_item = get_object_or_404(PharmacyBillGeneralItem, pk=item_id, bill=bill)

        if bill.bill_status not in ['DRAFT', 'OPEN']:
            return Response(
                {
                    'error': f"Cannot remove items from a '{bill.bill_status}' bill.",
                    'current_status': bill.bill_status,
                    'allowed_statuses': ['DRAFT', 'OPEN'],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        item_name = gen_item.batch.general_item.name
        try:
            gen_item.delete()
            _recalculate_bill_totals(bill)
            bill.refresh_from_db()
            return Response(
                {
                    'message': f"'{item_name}' removed from bill.",
                    'bill': PharmacyBillSerializer(bill).data,
                }
            )
        except Exception as e:
            logger.warning("Failed to remove general item from bill: %s", e)
            return Response(
                {'error': f"Failed to remove item: {_safe_detail(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )