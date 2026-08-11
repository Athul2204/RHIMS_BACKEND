# manager/views.py
from decimal import Decimal
from datetime import date, timedelta, datetime, time
import calendar
import logging

from dateutil.relativedelta import relativedelta   # FIX 2: replaces timedelta(28) drift

from django.db import transaction
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Sum, Count, Q, F, DecimalField
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.contrib.auth.models import User

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from rest_framework.permissions import AllowAny

from authentication.permissions import IsAdminOrManager, IsManager, IsAdminOrManagerOrPharmacistRead, IsAdminOrManagerOrReceptionistRead
from authentication.utils import is_group_admin_user, get_user_branch, scope_queryset_to_branch, resolve_branch_for_write
from authentication.throttling import PublicWriteThrottle
from administration.models import StaffProfile, HospitalSettings, Branch
from administration.serializers import DoctorProfileSerializer as EmrDoctorProfileSerializer
from doctor.models import DoctorProfile as EmrDoctorProfile
from .models import (
    SupportStaff, Attendance, LeaveRequest, SalaryRecord, SalaryEntry, HospitalExpense,
    OtherIncome, Dealer, DealerTransaction,
    DoctorWebsiteProfile, DoctorWeeklyAvailability, DoctorAvailabilityException,
    PatientQuery, Testimonial, YoutubeVideo, InstagramPost, FacebookPost,
    MediaEvent, GalleryImage,
    Specialty, SpecialtySection, Treatment, Blog,
    BranchWebsiteProfile,
)
from .serializers import (
    SupportStaffSerializer,
    AttendanceSerializer,
    BulkAttendanceSerializer,
    LeaveRequestSerializer,
    SalaryRecordSerializer,
    SalaryEntrySerializer,
    HospitalExpenseSerializer,
    OtherIncomeSerializer,
    DealerSerializer,
    DealerTransactionSerializer,
    DealerTransactionFinalizeSerializer,
    DealerTransactionBulkFinalizeSerializer,
    DealerTransactionScheduleUpdateSerializer,
    _staff_profile_display_name,
    DoctorWebsiteProfileSerializer,
    DoctorWeeklyAvailabilitySerializer,
    DoctorAvailabilityExceptionSerializer,
    PatientQuerySerializer,
    TestimonialSerializer,
    YoutubeVideoSerializer,
    InstagramPostSerializer,
    FacebookPostSerializer,
    MediaEventSerializer,
    GalleryImageSerializer,
    ReorderSerializer,
    SpecialtySerializer,
    SpecialtySectionSerializer,
    TreatmentSerializer,
    BlogSerializer,
    PublicDoctorSerializer,
    PublicDoctorDetailSerializer,
    PublicTestimonialSerializer,
    PublicYoutubeVideoSerializer,
    PublicInstagramPostSerializer,
    PublicFacebookPostSerializer,
    PublicMediaEventSerializer,
    PublicMediaEventDetailSerializer,
    PublicGalleryImageSerializer,
    PublicSpecialtySerializer,
    PublicSpecialtyDetailSerializer,
    PublicTreatmentDetailSerializer,
    PublicBlogSerializer,
    PublicBlogDetailSerializer,
    PublicPreBookingSerializer,
    PublicContactInquirySerializer,
    PublicBranchSerializer,
    PublicBranchChoiceSerializer,
    ManagerBranchWebsiteSerializer,
    BranchWebsiteProfileContentSerializer,
)

logger = logging.getLogger(__name__)


def _safe_detail(exc):
    """Only expose exception text to the client when DEBUG=True; full detail
    always goes to the server log at the call site. See doctor/views.py's
    identical helper for rationale."""
    from django.conf import settings
    return str(exc) if settings.DEBUG else "An internal error occurred. Please try again or contact support."


# FIX 3: Timezone-safe day-range bounds for filtering DateTimeField columns.
#
# `some_datetime_field__date__range=(start, end)` looks innocent, but on
# MySQL with USE_TZ=True and TIME_ZONE != 'UTC' (this project uses
# 'Asia/Kolkata'), Django compiles that lookup to
#   DATE(CONVERT_TZ(col, 'UTC', 'Asia/Kolkata')) BETWEEN start AND end
# CONVERT_TZ silently returns NULL for every row if the server's
# mysql.time_zone_name tables haven't been loaded — a one-time setup step
# (`mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql -u root mysql`) that is
# very easy to skip and easy not to notice, because the query raises no
# error: it just quietly matches zero rows.
#
# That's exactly what was happening to pharmacy and lab revenue everywhere
# in this module — both are recognized off `created_at`, a DateTimeField.
# Reception revenue kept working because ConsultationBill.consultation_date
# is a plain DateField, so no CONVERT_TZ was ever involved.
#
# Converting the local date range to an aware UTC datetime range in Python
# up front avoids CONVERT_TZ entirely: Django sends two plain UTC literals
# and the DB does a normal indexed range comparison on the raw column.
def _local_day_range(start, end):
    lo = timezone.make_aware(datetime.combine(start, time.min))
    hi = timezone.make_aware(datetime.combine(end, time.max))
    return lo, hi


def _scope_dual_staff_qs(qs, user, sp_field="staff_profile", ss_field="support_staff"):
    """
    Branch-scope a queryset whose model links to a staff member via one of
    two mutually-exclusive FKs (staff_profile OR support_staff) — the
    pattern used by Attendance, LeaveRequest, and SalaryRecord. A group
    admin sees everything; an ordinary user is restricted to rows whose
    linked staff member (whichever FK is set) belongs to their own branch.
    Fails closed (returns none()) for a branch-scoped user with no branch
    of their own.
    """
    if is_group_admin_user(user):
        return qs
    branch = get_user_branch(user)
    if branch is None:
        return qs.none()
    return qs.filter(Q(**{f"{sp_field}__branch": branch}) | Q(**{f"{ss_field}__branch": branch}))


def _user_in_branch(user, branch_id):
    """True if `branch_id` (a StaffProfile/SupportStaff pk's branch_id) is
    the caller's own branch, or the caller is a group admin. Used to reject
    cross-branch staff_id references on write (e.g. AttendanceBulkView,
    SalaryGenerateView) instead of only filtering read-side querysets."""
    if is_group_admin_user(user):
        return True
    branch = get_user_branch(user)
    return branch is not None and branch.pk == branch_id


# Lazy imports so we don't break if a module isn't installed
def _get_reception_revenue(start, end, branch=None):
    try:
        from reception.models import ConsultationBill
        qs = ConsultationBill.objects.filter(
            consultation_date__range=(start, end),   # DateField — safe as-is
            payment_status="PAID",
        ).exclude(consultation__status="CANCELLED")
        if branch is not None:
            qs = qs.filter(branch=branch)
        # CancelBillView deliberately leaves payment_status untouched when an
        # already-PAID appointment is cancelled (see its docstring) — the
        # refund is reconciled outside the app, not by flipping this field.
        # So payment_status="PAID" alone is not enough to know a bill is
        # still real revenue; we also have to exclude bills whose linked
        # Consultation was cancelled, same as BillingPage.jsx's activeBills
        # filter and patient_is_revisit_eligible() do on the frontend/model
        # side. Bills with no linked Consultation at all (consultation IS
        # NULL) are unaffected by exclude() and still count normally.
        total = qs.aggregate(t=Sum("total_amount"))["t"] or Decimal("0")
        count = qs.count()
        return total, count
    except Exception:
        logger.exception("_get_reception_revenue failed")
        return Decimal("0"), 0


def _get_pharmacy_revenue(start, end, branch=None):
    try:
        from pharmacist.models import PharmacyBill
        lo, hi = _local_day_range(start, end)
        qs = PharmacyBill.objects.filter(
            created_at__range=(lo, hi),   # FIX 3: was created_at__date__range (see _local_day_range)
            bill_status="PAID",           # FIX 1: was payment_status – wrong field, always returned 0
        )
        if branch is not None:
            qs = qs.filter(branch=branch)
        total = qs.aggregate(t=Sum("total_amount"))["t"] or Decimal("0")
        count = qs.count()
        return total, count
    except Exception:
        logger.exception("_get_pharmacy_revenue failed")
        return Decimal("0"), 0


def _get_lab_revenue(start, end, branch=None):
    try:
        from lab.models import LabBill
        lo, hi = _local_day_range(start, end)
        qs = LabBill.objects.filter(
            created_at__range=(lo, hi),   # FIX 3: was created_at__date__range (see _local_day_range)
            payment_status="PAID",
        )
        if branch is not None:
            qs = qs.filter(branch=branch)
        total = qs.aggregate(t=Sum("total_amount"))["t"] or Decimal("0")
        count = qs.count()
        return total, count
    except Exception:
        logger.exception("_get_lab_revenue failed")
        return Decimal("0"), 0


def _get_home_visit_revenue(start, end, branch=None):
    """
    Informational sub-figure only — Home Visit bills are already a
    ConsultationBill (consultation_type='HOME_VISIT'), so their money is
    already inside _get_reception_revenue()'s total. This just isolates
    that subset for display, and must never be added into total_revenue
    a second time.
    """
    try:
        from reception.models import ConsultationBill
        qs = ConsultationBill.objects.filter(
            consultation_date__range=(start, end),
            payment_status="PAID",
            consultation_type="HOME_VISIT",
        ).exclude(consultation__status="CANCELLED")
        if branch is not None:
            qs = qs.filter(branch=branch)
        total = qs.aggregate(t=Sum("total_amount"))["t"] or Decimal("0")
        count = qs.count()
        return total, count
    except Exception:
        logger.exception("_get_home_visit_revenue failed")
        return Decimal("0"), 0


def _income_breakdown(start, end, branch=None):
    """
    Other Income (Lab Commission, Donations, etc.) for a period — folded
    into total_revenue in FinanceDashboardView, and also broken out here
    by category (Lab Commission called out specifically) for display.
    """
    qs = OtherIncome.objects.filter(date__range=(start, end))
    if branch is not None:
        qs = qs.filter(branch=branch)
    total = qs.aggregate(t=Sum("amount"))["t"] or Decimal("0")
    by_category = list(qs.values("category").annotate(total=Sum("amount"), count=Count("income_id")).order_by("-total"))
    lab_commission = qs.filter(category="LAB_COMMISSION").aggregate(t=Sum("amount"))["t"] or Decimal("0")
    return {
        "total":          total,
        "by_category":    by_category,
        "lab_commission": lab_commission,
        "count":          qs.count(),
    }


# ── Purchases (medicine + supplies) ─────────────────────────────
def _rejected_purchase_source_ids(source_model, source_ids):
    """Among source_ids (batch PKs), which ones have a REJECTED PURCHASE
    DealerTransaction against them.

    A REJECTED purchase transaction means the manager disowned that
    dealer-linked entry entirely — Dealer.balance already drops it from
    what's owed (see DealerTransactionVoidView), and _dealer_settlement_map
    reports its amount_due as 0 for the same reason. But the accrual
    purchase totals below (_get_medicine_purchases etc.) used to ignore
    DealerTransaction status completely and count every batch's cost
    regardless — so rejecting a purchase changed the Dealers page (₹0,
    "Fully settled") but left the Finance Dashboard's Total Expenses/Loss
    completely unchanged, showing the two screens visibly disagreeing
    about the same event. Batches excluded here are still visible on the
    Dealer's own Ledger History for audit — this only removes them from
    the hospital's expense/purchases accounting.
    Batches with no dealer link never have a DealerTransaction at all, so
    they're untouched (empty set) and always count in full, as before.
    """
    if not source_ids:
        return set()
    return set(
        DealerTransaction.objects.filter(
            source_model=source_model, source_id__in=source_ids,
            transaction_type="PURCHASE", status="REJECTED",
        ).values_list("source_id", flat=True)
    )


def _get_medicine_purchases(start, end, branch=None):
    """
    Cost of medicine stock received in the period.
    NOTE: MedicineBatch only stores a live `quantity` (reduced as stock is
    dispensed/returned), not an immutable "quantity received" snapshot like
    SupplyBatch does. This is therefore cost_price × current quantity for
    batches *created* in the period — an approximation that undercounts a
    batch's true purchase cost once some of it has already been dispensed
    or returned. Treat this figure as indicative, not an exact ledger.
    Batches whose dealer PURCHASE transaction was REJECTED are excluded —
    see _rejected_purchase_source_ids.
    """
    try:
        from pharmacist.models import MedicineBatch
        lo, hi = _local_day_range(start, end)
        qs = MedicineBatch.objects.filter(created_at__range=(lo, hi))   # FIX 3 (see _local_day_range)
        if branch is not None:
            qs = qs.filter(medicine__branch=branch)
        rejected = _rejected_purchase_source_ids(
            "MEDICINE_BATCH", list(qs.values_list("batch_id", flat=True))
        )
        if rejected:
            qs = qs.exclude(batch_id__in=rejected)
        total = qs.aggregate(
            t=Sum(F("cost_price") * F("quantity"), output_field=DecimalField())
        )["t"] or Decimal("0")
        return total, qs.count()
    except Exception:
        logger.exception("_get_medicine_purchases failed")
        return Decimal("0"), 0


def _get_supply_purchases(start, end, branch=None):
    """Cost of supplies/consumables received in the period (accurate — SupplyBatch.total_cost is an immutable snapshot).
    Batches whose dealer PURCHASE transaction was REJECTED are excluded —
    see _rejected_purchase_source_ids."""
    try:
        from pharmacist.models import SupplyBatch
        qs = SupplyBatch.objects.filter(purchase_date__range=(start, end))
        if branch is not None:
            qs = qs.filter(supply_item__branch=branch)
        rejected = _rejected_purchase_source_ids(
            "SUPPLY_BATCH", list(qs.values_list("batch_id", flat=True))
        )
        if rejected:
            qs = qs.exclude(batch_id__in=rejected)
        total = qs.aggregate(t=Sum("total_cost"))["t"] or Decimal("0")
        return total, qs.count()
    except Exception:
        logger.exception("_get_supply_purchases failed")
        return Decimal("0"), 0


def _get_medicine_purchases_paid_due(start, end, branch=None):
    """Of the medicine purchases counted above, how much has actually been
    paid to the dealer vs. is still outstanding — reconciled against the
    Dealers ledger via _dealer_settlement_map. A batch with no dealer link
    is treated as fully paid (nothing to track through this system)."""
    try:
        from pharmacist.models import MedicineBatch
        lo, hi = _local_day_range(start, end)
        qs = MedicineBatch.objects.filter(created_at__range=(lo, hi))
        if branch is not None:
            qs = qs.filter(medicine__branch=branch)
        ids = list(qs.values_list("batch_id", flat=True))
        settle_map = _dealer_settlement_map("MEDICINE_BATCH", ids, "PURCHASE")
        paid = sum((v["amount_paid"] for v in settle_map.values()), Decimal("0.00"))
        due = sum((v["amount_due"] for v in settle_map.values()), Decimal("0.00"))
        pending_review = sum(1 for v in settle_map.values() if v["txn_status"] == "PENDING")
        return paid, due, pending_review
    except Exception:
        logger.exception("_get_medicine_purchases_paid_due failed")
        return Decimal("0"), Decimal("0"), 0


def _get_supply_purchases_paid_due(start, end, branch=None):
    """Supply-side equivalent of _get_medicine_purchases_paid_due."""
    try:
        from pharmacist.models import SupplyBatch
        qs = SupplyBatch.objects.filter(purchase_date__range=(start, end))
        if branch is not None:
            qs = qs.filter(supply_item__branch=branch)
        ids = list(qs.values_list("batch_id", flat=True))
        settle_map = _dealer_settlement_map("SUPPLY_BATCH", ids, "PURCHASE")
        paid = sum((v["amount_paid"] for v in settle_map.values()), Decimal("0.00"))
        due = sum((v["amount_due"] for v in settle_map.values()), Decimal("0.00"))
        pending_review = sum(1 for v in settle_map.values() if v["txn_status"] == "PENDING")
        return paid, due, pending_review
    except Exception:
        logger.exception("_get_supply_purchases_paid_due failed")
        return Decimal("0"), Decimal("0"), 0


# ── Reconcile accrual figures (full batch/return cost) against what's
# actually moved on the Dealers ledger ───────────────────────────────
# _get_medicine_purchases / _get_supply_purchases below report the FULL
# invoice value of stock the moment it's received — correct for expense
# accrual, but it used to be the ONLY figure shown on the Finance
# Dashboard and the Purchases & Refunds tab, with no indication that it
# has nothing to do with whether the dealer has actually been paid.
# A manager confirming a ₹150 purchase as "Paid Now" but lowering
# "Paying Now" to ₹100 (a genuine partial payment, see FinalizeModal)
# would then see "₹150" here and "₹100 paid" on the Dealers page, with
# no obvious reason why they disagreed.
# This maps each batch/return onto its DealerTransaction (via the loose
# source_model/source_id reference — see DealerTransaction docstring)
# and sums the CONFIRMED PAYMENT/CASH_REFUND legs actually linked to it,
# in bulk (two queries total, not one per row) so PurchasesAndRefundsView
# and _expenses_breakdown can show "paid so far" / "still due" next to
# the accrual total instead of implying the whole invoice was settled.
def _dealer_settlement_map(source_model, source_ids, txn_type):
    """
    {source_id: {"txn_status", "settlement_method", "amount_paid", "amount_due"}}
    for every DealerTransaction of `txn_type` (PURCHASE or CREDIT_NOTE)
    tied to source_model + one of source_ids.
    - A batch/return with NO dealer at all (dealer is optional on both
      MedicineBatch and SupplyBatch) has no DealerTransaction and simply
      won't appear in the returned dict — callers should treat a missing
      entry as "not tracked on the dealer ledger" (nothing owed through
      this system), not as "unpaid".
    - A PENDING transaction (not yet reviewed by the manager) hasn't had
      any settlement leg auto-paired yet, so amount_paid is reported as
      0 and amount_due as the full amount even though nothing is
      actually overdue yet — callers surface txn_status separately so
      "Pending review" can be shown instead of "Due".
    - A REJECTED transaction (rejected on review, or later voided) is
      reported as amount_paid=0, amount_due=0 — it's dropped out
      entirely, same as Dealer.balance already does for REJECTED rows.
      Callers surface txn_status="REJECTED" separately (see
      _settlement_status_label) so it still shows a distinct badge
      instead of looking "Paid in full".
    """
    if not source_ids:
        return {}
    parents = list(
        DealerTransaction.objects.filter(
            source_model=source_model, source_id__in=source_ids, transaction_type=txn_type,
        )
    )
    if not parents:
        return {}
    parent_ids = [p.transaction_id for p in parents]
    paid_by_parent = dict(
        DealerTransaction.objects.filter(
            linked_transaction_id__in=parent_ids, status="CONFIRMED",
            transaction_type__in=("PAYMENT", "CASH_REFUND"),
        ).values("linked_transaction_id").annotate(t=Sum("amount")).values_list("linked_transaction_id", "t")
    )
    result = {}
    for p in parents:
        # A REJECTED parent (rejected on review, or voided later — see
        # DealerTransactionVoidView) is deliberately treated the same way
        # Dealer.balance already treats it: dropped out entirely, not
        # counted as still-due. Previously this fell through to the
        # generic `due = p.amount - paid` branch below like a PENDING row
        # would, so rejecting/voiding a purchase or credit-note changed
        # nothing about "amount_due" on the Finance Dashboard / Purchases
        # tab — a manager rejecting a bad ₹500 general-item purchase (or
        # medicine/supply — this map is shared) still saw ₹500 "still due
        # to dealers" with no way to tell the rejection had any effect.
        if p.status == "REJECTED":
            paid = Decimal("0.00")
            due = Decimal("0.00")
        else:
            paid = paid_by_parent.get(p.transaction_id) or Decimal("0.00") if p.status == "CONFIRMED" else Decimal("0.00")
            due = p.amount - paid
            if due < 0:
                due = Decimal("0.00")
        # A batch can (rarely) have more than one DealerTransaction row
        # tied to it — keep the most recent/relevant by simply letting a
        # later parent in the queryset win, same tie-break as elsewhere.
        result[p.source_id] = {
            "txn_status": p.status,
            "settlement_method": p.settlement_method,
            "amount_paid": paid,
            "amount_due": due,
        }
    return result


def _settlement_status_label(settle_entry):
    """Human label for a _dealer_settlement_map() entry (or None, meaning
    no dealer was attached to this batch/return at all — nothing to
    reconcile through the ledger, shown as N/A rather than 'Due')."""
    if settle_entry is None:
        return "N/A — no dealer linked"
    if settle_entry["txn_status"] == "PENDING":
        return "Pending review"
    if settle_entry["txn_status"] == "REJECTED":
        return "Rejected"
    if settle_entry["amount_due"] <= 0:
        return "Paid in full"
    if settle_entry["amount_paid"] > 0:
        return "Partially paid"
    return "Due"


# ── Refunds (medicine + supplies returned to provider) ──────────
# Sourced from confirmed CASH_REFUND rows on the dealer ledger, NOT from
# the return record's mere existence. Previously this counted a return's
# full refund_amount the moment MedicineReturnToProvider/SupplyReturn was
# created — before the manager had even reviewed it on the Dealers page,
# regardless of whether it was later left as running credit or actually
# collected in cash. That let the Finance Dashboard "book" a refund that
# still showed as Due/uncollected on the Dealers ledger. confirmed_at
# (not created_at) is used so this reflects when the cash refund was
# actually confirmed as received, matching what the Dealers page shows.
def _get_medicine_refunds(start, end, branch=None):
    try:
        lo, hi = _local_day_range(start, end)
        qs = DealerTransaction.objects.filter(
            transaction_type="CASH_REFUND", status="CONFIRMED",
            source_model="MEDICINE_RETURN", confirmed_at__range=(lo, hi),
        )
        if branch is not None:
            qs = qs.filter(dealer__branch=branch)
        total = qs.aggregate(t=Sum("amount"))["t"] or Decimal("0")
        return total, qs.count()
    except Exception:
        logger.exception("_get_medicine_refunds failed")
        return Decimal("0"), 0


def _get_supply_refunds(start, end, branch=None):
    try:
        lo, hi = _local_day_range(start, end)
        qs = DealerTransaction.objects.filter(
            transaction_type="CASH_REFUND", status="CONFIRMED",
            source_model="SUPPLY_RETURN", confirmed_at__range=(lo, hi),
        )
        if branch is not None:
            qs = qs.filter(dealer__branch=branch)
        total = qs.aggregate(t=Sum("amount"))["t"] or Decimal("0")
        return total, qs.count()
    except Exception:
        logger.exception("_get_supply_refunds failed")
        return Decimal("0"), 0


# ── General items (FMCG/non-medicine stock) — mirrors medicine/supply
# above. Previously not counted anywhere in the Finance Dashboard at all.
def _get_general_item_purchases(start, end, branch=None):
    """Cost of general-item stock received in the period.
    NOTE: like MedicineBatch, GeneralItemBatch only stores a live
    `quantity` (not an immutable received-quantity snapshot), so this is
    cost_price × current quantity for batches *created* in the period —
    indicative, not an exact ledger. See _get_medicine_purchases.
    Batches whose dealer PURCHASE transaction was REJECTED are excluded —
    see _rejected_purchase_source_ids."""
    try:
        from pharmacist.models import GeneralItemBatch
        lo, hi = _local_day_range(start, end)
        qs = GeneralItemBatch.objects.filter(created_at__range=(lo, hi))
        if branch is not None:
            qs = qs.filter(general_item__branch=branch)
        rejected = _rejected_purchase_source_ids(
            "GENERAL_ITEM_BATCH", list(qs.values_list("batch_id", flat=True))
        )
        if rejected:
            qs = qs.exclude(batch_id__in=rejected)
        total = qs.aggregate(
            t=Sum(F("cost_price") * F("quantity"), output_field=DecimalField())
        )["t"] or Decimal("0")
        return total, qs.count()
    except Exception:
        logger.exception("_get_general_item_purchases failed")
        return Decimal("0"), 0


def _get_general_item_purchases_paid_due(start, end, branch=None):
    """Paid/due reconciliation for general-item purchases — see
    _get_medicine_purchases_paid_due."""
    try:
        from pharmacist.models import GeneralItemBatch
        lo, hi = _local_day_range(start, end)
        qs = GeneralItemBatch.objects.filter(created_at__range=(lo, hi))
        if branch is not None:
            qs = qs.filter(general_item__branch=branch)
        ids = list(qs.values_list("batch_id", flat=True))
        settle_map = _dealer_settlement_map("GENERAL_ITEM_BATCH", ids, "PURCHASE")
        paid = sum((v["amount_paid"] for v in settle_map.values()), Decimal("0.00"))
        due = sum((v["amount_due"] for v in settle_map.values()), Decimal("0.00"))
        pending_review = sum(1 for v in settle_map.values() if v["txn_status"] == "PENDING")
        return paid, due, pending_review
    except Exception:
        logger.exception("_get_general_item_purchases_paid_due failed")
        return Decimal("0"), Decimal("0"), 0


def _get_general_item_refunds(start, end, branch=None):
    try:
        lo, hi = _local_day_range(start, end)
        qs = DealerTransaction.objects.filter(
            transaction_type="CASH_REFUND", status="CONFIRMED",
            source_model="GENERAL_ITEM_RETURN", confirmed_at__range=(lo, hi),
        )
        if branch is not None:
            qs = qs.filter(dealer__branch=branch)
        total = qs.aggregate(t=Sum("amount"))["t"] or Decimal("0")
        return total, qs.count()
    except Exception:
        logger.exception("_get_general_item_refunds failed")
        return Decimal("0"), 0


# ── Salary actually paid out in the period (cash-basis, by paid_at date) ──
def _get_salary_paid(start, end, branch=None):
    lo, hi = _local_day_range(start, end)
    qs = SalaryRecord.objects.filter(is_paid=True, paid_at__range=(lo, hi))   # FIX 3 (see _local_day_range)
    if branch is not None:
        qs = qs.filter(Q(staff_profile__branch=branch) | Q(support_staff__branch=branch))
    total = qs.aggregate(t=Sum("net_salary"))["t"] or Decimal("0")
    return total, qs.count()


def _period_bounds(period, today, request):
    """Shared start/end resolution for finance-style endpoints. Returns (start, end) or a Response on error."""
    if period == "today":
        return today, today
    if period == "week":
        return today - timedelta(days=today.weekday()), today
    if period == "year":
        return date(today.year, 1, 1), today
    if period == "custom":
        try:
            start = date.fromisoformat(request.query_params["start"])
            end   = date.fromisoformat(request.query_params["end"])
            return start, end
        except (KeyError, ValueError):
            return None
    # default: month
    return today.replace(day=1), today


def _expenses_breakdown(start, end, branch=None):
    """
    Full expense picture for a period. `manual` excludes HospitalExpense
    rows in category Salary/Supplies since those now come from the live
    sources below (salary_paid, medicine/supply purchases) and would
    double-count otherwise. `refunds` is money owed back from returning
    medicine/supplies to a provider - shown as its own line, not hidden.
    """
    manual_qs = HospitalExpense.objects.filter(date__range=(start, end)).exclude(category__in=["Salary", "Supplies"])
    if branch is not None:
        manual_qs = manual_qs.filter(branch=branch)
    manual_total = manual_qs.aggregate(t=Sum("amount"))["t"] or Decimal("0")
    by_category = list(manual_qs.values("category").annotate(total=Sum("amount"), count=Count("expense_id")).order_by("-total"))

    salary_paid, salary_count = _get_salary_paid(start, end, branch)
    med_purchases, med_count = _get_medicine_purchases(start, end, branch)
    supply_purchases, supply_count = _get_supply_purchases(start, end, branch)
    gi_purchases, gi_count = _get_general_item_purchases(start, end, branch)
    med_refunds, med_refund_count = _get_medicine_refunds(start, end, branch)
    supply_refunds, supply_refund_count = _get_supply_refunds(start, end, branch)
    gi_refunds, gi_refund_count = _get_general_item_refunds(start, end, branch)

    # ✅ FIX: `medicine_purchases`/`supply_purchases`/`general_item_purchases`
    # above are the FULL invoice value the moment stock is received
    # (correct for accrual expense recognition — gross_expenses/
    # net_expenses/profit below are deliberately left on that basis and
    # NOT changed here). But that figure alone, shown on the dashboard as
    # "Medicine/Supply/General Item Purchases" right next to cash figures
    # like "Salary Paid", looked like it meant "paid" — so a ₹150 purchase
    # confirmed as Paid Now with only ₹100 actually paid (a partial
    # payment, see FinalizeModal) showed "₹150" here while the Dealers
    # ledger correctly showed "₹100 paid, ₹50 due", with nothing
    # explaining the mismatch. These reconcile the same batches against
    # the Dealers ledger's confirmed PAYMENT legs so the dashboard can
    # show "paid so far" / "still due" alongside the accrual total
    # instead of implying it was all settled immediately.
    med_paid, med_due, med_pending_review = _get_medicine_purchases_paid_due(start, end, branch)
    supply_paid, supply_due, supply_pending_review = _get_supply_purchases_paid_due(start, end, branch)
    gi_paid, gi_due, gi_pending_review = _get_general_item_purchases_paid_due(start, end, branch)

    total_refunds = med_refunds + supply_refunds + gi_refunds
    gross_expenses = manual_total + salary_paid + med_purchases + supply_purchases + gi_purchases
    net_expenses = gross_expenses - total_refunds

    return {
        "manual_total": manual_total,
        "by_category": by_category,
        "salary_paid": salary_paid,
        "salary_count": salary_count,
        "medicine_purchases": med_purchases,
        "medicine_purchase_count": med_count,
        "medicine_purchases_paid": med_paid,
        "medicine_purchases_due": med_due,
        "medicine_purchases_pending_review": med_pending_review,
        "supply_purchases": supply_purchases,
        "supply_purchase_count": supply_count,
        "supply_purchases_paid": supply_paid,
        "supply_purchases_due": supply_due,
        "supply_purchases_pending_review": supply_pending_review,
        "general_item_purchases": gi_purchases,
        "general_item_purchase_count": gi_count,
        "general_item_purchases_paid": gi_paid,
        "general_item_purchases_due": gi_due,
        "general_item_purchases_pending_review": gi_pending_review,
        "medicine_refunds": med_refunds,
        "medicine_refund_count": med_refund_count,
        "supply_refunds": supply_refunds,
        "supply_refund_count": supply_refund_count,
        "general_item_refunds": gi_refunds,
        "general_item_refund_count": gi_refund_count,
        "total_refunds": total_refunds,
        "gross_expenses": gross_expenses,
        "net_expenses": net_expenses,
    }


def _single_point_trend(start, end, label, branch=None):
    cr, _ = _get_reception_revenue(start, end, branch)
    pr, _ = _get_pharmacy_revenue(start, end, branch)
    rev = float(cr + pr)
    exp = _expenses_breakdown(start, end, branch)
    expf = float(exp["net_expenses"])
    return {"label": label, "month": label, "revenue": rev, "expenses": expf, "profit": rev - expf}


def _point_for_range(rs, re_, label, branch=None):
    cr, _ = _get_reception_revenue(rs, re_, branch)
    pr, _ = _get_pharmacy_revenue(rs, re_, branch)
    lr, _ = _get_lab_revenue(rs, re_, branch)
    rev = float(cr + pr)
    exp = _expenses_breakdown(rs, re_, branch)
    expf = float(exp["net_expenses"])
    return {
        "label": label,
        "month": label,  # backward-compat key for the existing frontend chart
        "revenue": rev,
        "lab_revenue": float(lr),
        "expenses": expf,
        "refunds": float(exp["total_refunds"]),
        "profit": rev - expf,
    }


# ── Batched (grouped) versions of the sources above ──────────────────────
#
# BUG FIX: _daily_trend_series/_monthly_trend_series used to build their
# series by looping over every day (or month) in the range and calling
# _point_for_range for each one — which itself calls _get_reception_revenue,
# _get_pharmacy_revenue, _get_lab_revenue and _expenses_breakdown (which
# alone fires ~6 more queries). That's ~9 queries per point. For a "month"
# period (up to 31 points) or a "custom" range up to 62 days, that's
# 250-550+ small queries in a single request — easily enough to blow past
# a gunicorn/reverse-proxy timeout under real load, which the frontend
# would only ever see as a bare 500.
#
# These helpers instead fetch each revenue/expense source ONCE for the
# whole range, grouped by day (or month), and the trend builders below
# just look values up in a dict per point — one fixed, small number of
# queries no matter how many days the range spans.
#
# BUG FIX 2 (CONVERT_TZ crash): Django's TruncDate / TruncMonth emit
# CONVERT_TZ(..., 'UTC', 'Asia/Kolkata') in MySQL when USE_TZ=True and
# TIME_ZONE != 'UTC'. MySQL silently returns NULL — or raises ValueError —
# when its timezone tables haven't been loaded (mysql_tzinfo_to_sql), which
# is the exact same class of bug that _local_day_range fixed for __date__range
# lookups. The fix here is to skip Django's Trunc* entirely and instead:
#   - fetch rows with raw pk + datetime + amount via .values()
#   - convert each UTC datetime to local date/month in Python using
#     timezone.localtime(), which only does Python-level arithmetic
#   - accumulate totals into a plain dict
# This is zero extra queries vs the old TruncDate approach, avoids all
# server-side CONVERT_TZ, and produces identical output.

def _bucket_key_day(dt):
    """UTC-aware datetime → local calendar date (date object)."""
    return timezone.localtime(dt).date()


def _bucket_key_month(dt):
    """UTC-aware datetime → first day of the local calendar month."""
    d = timezone.localtime(dt).date()
    return d.replace(day=1)


def _grouped_by_bucket_py(qs, dt_field, amount_field, bucket_fn):
    """
    Python-side grouping for DateTimeField columns.

    Fetches (pk, <dt_field>, <amount_field>) for all rows in `qs`, converts
    each UTC datetime to a local bucket key via `bucket_fn`, and returns
    {bucket_key: (Decimal total, int count)}.

    This avoids Django's TruncDate/TruncMonth ORM functions, which internally
    emit CONVERT_TZ in MySQL and crash when timezone tables are absent.
    """
    result = {}
    for row in qs.values("pk", dt_field, amount_field):
        raw_dt = row[dt_field]
        amt    = row[amount_field] or Decimal("0")
        if raw_dt is None:
            continue
        key = bucket_fn(raw_dt)
        if key in result:
            result[key] = (result[key][0] + Decimal(str(amt)), result[key][1] + 1)
        else:
            result[key] = (Decimal(str(amt)), 1)
    return result


def _grouped_by_bucket(qs, date_field, amount_field, truncator=None):
    """Returns {bucket_date: (Decimal total, int count)}.

    For plain DateField columns (truncator=None) we do the grouping in the DB
    as before — no CONVERT_TZ involved so it's safe. For DateTimeField columns
    (truncator supplied) we use the Python-side helper above instead, which
    avoids the MySQL CONVERT_TZ crash entirely.
    """
    if truncator is not None:
        # Determine bucket granularity from the truncator class and delegate
        # to the Python-side implementation to avoid CONVERT_TZ.
        bucket_fn = _bucket_key_month if truncator is TruncMonth else _bucket_key_day
        return _grouped_by_bucket_py(qs, date_field, amount_field, bucket_fn)
    # DateField path: safe to aggregate in the DB.
    qs = qs.annotate(_bucket=F(date_field))
    rows = qs.values("_bucket").annotate(total=Sum(amount_field), count=Count("pk"))
    return {row["_bucket"]: (row["total"] or Decimal("0"), row["count"]) for row in rows}


def _reception_revenue_by_day(start, end, trunc=TruncDate, branch=None):
    try:
        from reception.models import ConsultationBill
        qs = ConsultationBill.objects.filter(
            consultation_date__range=(start, end), payment_status="PAID",
        ).exclude(consultation__status="CANCELLED")  # same gap as _get_reception_revenue — see that function's comment
        if branch is not None:
            qs = qs.filter(branch=branch)
        return _grouped_by_bucket(qs, "consultation_date", "total_amount")
    except Exception:
        logger.exception("_reception_revenue_by_day failed")
        return {}


def _pharmacy_revenue_by_day(start, end, trunc=TruncDate, branch=None):
    try:
        from pharmacist.models import PharmacyBill
        lo, hi = _local_day_range(start, end)
        qs = PharmacyBill.objects.filter(created_at__range=(lo, hi), bill_status="PAID")
        if branch is not None:
            qs = qs.filter(branch=branch)
        return _grouped_by_bucket(qs, "created_at", "total_amount", truncator=trunc)
    except Exception:
        logger.exception("_pharmacy_revenue_by_day failed")
        return {}


def _lab_revenue_by_day(start, end, trunc=TruncDate, branch=None):
    try:
        from lab.models import LabBill
        lo, hi = _local_day_range(start, end)
        qs = LabBill.objects.filter(created_at__range=(lo, hi), payment_status="PAID")
        if branch is not None:
            qs = qs.filter(branch=branch)
        return _grouped_by_bucket(qs, "created_at", "total_amount", truncator=trunc)
    except Exception:
        logger.exception("_lab_revenue_by_day failed")
        return {}


def _medicine_purchases_by_day(start, end, trunc=TruncDate, branch=None):
    try:
        from pharmacist.models import MedicineBatch
        lo, hi = _local_day_range(start, end)
        qs = MedicineBatch.objects.filter(created_at__range=(lo, hi))
        if branch is not None:
            qs = qs.filter(medicine__branch=branch)
        # Python-side: compute cost per row manually (cost_price * quantity)
        if trunc is not None:
            bucket_fn = _bucket_key_month if trunc is TruncMonth else _bucket_key_day
            result = {}
            for row in qs.values("pk", "created_at", "cost_price", "quantity"):
                raw_dt = row["created_at"]
                if raw_dt is None:
                    continue
                amt = (row["cost_price"] or Decimal("0")) * (row["quantity"] or 0)
                key = bucket_fn(raw_dt)
                if key in result:
                    result[key] = (result[key][0] + amt, result[key][1] + 1)
                else:
                    result[key] = (amt, 1)
            return result
        # DateField fallback (not used for created_at, but kept for safety)
        qs2 = qs.annotate(_cost=F("cost_price") * F("quantity"))
        return _grouped_by_bucket(qs2, "created_at", "_cost")
    except Exception:
        logger.exception("_medicine_purchases_by_day failed")
        return {}


def _supply_purchases_by_day(start, end, trunc=TruncDate, branch=None):
    try:
        from pharmacist.models import SupplyBatch
        qs = SupplyBatch.objects.filter(purchase_date__range=(start, end))
        if branch is not None:
            qs = qs.filter(supply_item__branch=branch)
        return _grouped_by_bucket(qs, "purchase_date", "total_cost")
    except Exception:
        logger.exception("_supply_purchases_by_day failed")
        return {}


def _medicine_refunds_by_day(start, end, trunc=TruncDate, branch=None):
    try:
        lo, hi = _local_day_range(start, end)
        qs = DealerTransaction.objects.filter(
            transaction_type="CASH_REFUND", status="CONFIRMED",
            source_model="MEDICINE_RETURN", confirmed_at__range=(lo, hi),
        )
        if branch is not None:
            qs = qs.filter(dealer__branch=branch)
        return _grouped_by_bucket(qs, "confirmed_at", "amount", truncator=trunc)
    except Exception:
        logger.exception("_medicine_refunds_by_day failed")
        return {}


def _supply_refunds_by_day(start, end, trunc=TruncDate, branch=None):
    try:
        lo, hi = _local_day_range(start, end)
        qs = DealerTransaction.objects.filter(
            transaction_type="CASH_REFUND", status="CONFIRMED",
            source_model="SUPPLY_RETURN", confirmed_at__range=(lo, hi),
        )
        if branch is not None:
            qs = qs.filter(dealer__branch=branch)
        return _grouped_by_bucket(qs, "confirmed_at", "amount", truncator=trunc)
    except Exception:
        logger.exception("_supply_refunds_by_day failed")
        return {}


def _salary_paid_by_day(start, end, trunc=TruncDate, branch=None):
    lo, hi = _local_day_range(start, end)
    qs = SalaryRecord.objects.filter(is_paid=True, paid_at__range=(lo, hi))
    if branch is not None:
        qs = qs.filter(Q(staff_profile__branch=branch) | Q(support_staff__branch=branch))
    return _grouped_by_bucket(qs, "paid_at", "net_salary", truncator=trunc)


def _manual_expenses_by_day(start, end, trunc=TruncDate, branch=None):
    qs = HospitalExpense.objects.filter(date__range=(start, end)).exclude(category__in=["Salary", "Supplies"])
    if branch is not None:
        qs = qs.filter(branch=branch)
    return _grouped_by_bucket(qs, "date", "amount")


def _build_trend_series(start, end, buckets, label_fn, trunc, branch=None):
    """
    Shared engine for both the daily and monthly trend series: fetches
    every revenue/expense source once (grouped by `trunc`), then assembles
    one point per entry in `buckets` via plain dict lookups (no further
    queries).
    """
    reception = _reception_revenue_by_day(start, end, trunc, branch)
    pharmacy = _pharmacy_revenue_by_day(start, end, trunc, branch)
    lab = _lab_revenue_by_day(start, end, trunc, branch)
    med_purchases = _medicine_purchases_by_day(start, end, trunc, branch)
    supply_purchases = _supply_purchases_by_day(start, end, trunc, branch)
    med_refunds = _medicine_refunds_by_day(start, end, trunc, branch)
    supply_refunds = _supply_refunds_by_day(start, end, trunc, branch)
    salary_paid = _salary_paid_by_day(start, end, trunc, branch)
    manual = _manual_expenses_by_day(start, end, trunc, branch)

    zero = (Decimal("0"), 0)
    trend = []
    for bucket_key, label in buckets:
        cr, _ = reception.get(bucket_key, zero)
        pr, _ = pharmacy.get(bucket_key, zero)
        lr, _ = lab.get(bucket_key, zero)
        med_p, _ = med_purchases.get(bucket_key, zero)
        sup_p, _ = supply_purchases.get(bucket_key, zero)
        med_r, _ = med_refunds.get(bucket_key, zero)
        sup_r, _ = supply_refunds.get(bucket_key, zero)
        sal, _ = salary_paid.get(bucket_key, zero)
        man, _ = manual.get(bucket_key, zero)

        rev = float(cr + pr)
        total_refunds = med_r + sup_r
        gross_expenses = man + sal + med_p + sup_p
        net_expenses = gross_expenses - total_refunds
        expf = float(net_expenses)

        trend.append({
            "label": label,
            "month": label,  # backward-compat key for the existing frontend chart
            "revenue": rev,
            "lab_revenue": float(lr),
            "expenses": expf,
            "refunds": float(total_refunds),
            "profit": rev - expf,
        })
    return trend


def _daily_trend_series(start, end, branch=None):
    buckets = []
    d = start
    while d <= end:
        buckets.append((d, d.strftime("%d %b")))
        d += timedelta(days=1)
    return _build_trend_series(start, end, buckets, lambda d: d.strftime("%d %b"), TruncDate, branch)


def _monthly_trend_series(start, end, branch=None):
    buckets = []
    ms = start.replace(day=1)
    end_month_start = end.replace(day=1)
    while ms <= end_month_start:
        buckets.append((ms, ms.strftime("%b %Y")))
        ms = ms + relativedelta(months=1)
    return _build_trend_series(start, end, buckets, lambda d: d.strftime("%b %Y"), TruncMonth, branch)


def _resolve_dashboard_branch(request):
    """
    Shared branch resolution for the Finance section (FinanceDashboardView,
    AllBillsView, BillDetailView, PurchasesAndRefundsView). Returns a
    Branch instance for an ordinary branch-scoped user (never None for
    them — they always see only their own branch), or None for a group
    admin who hasn't narrowed with ?branch=<id> (meaning "every branch").
    An ordinary user with no branch of their own gets a 400 the caller
    should surface, so this returns (branch, error_response).
    """
    if is_group_admin_user(request.user):
        branch_id = request.query_params.get("branch")
        if branch_id:
            from administration.models import Branch
            return get_object_or_404(Branch, pk=branch_id), None
        return None, None

    branch = get_user_branch(request.user)
    if branch is None:
        return None, Response(
            {"error": "Your account is not assigned to a branch. Contact a group admin."},
            status=400,
        )
    return branch, None


# ═══════════════════════════════════════════════════════
# MANAGER BRANCH ACCESS (self-service)
# ═══════════════════════════════════════════════════════
class ManagerBranchesView(APIView):
    """
    GET /api/manager/branches/

    Lets a logged-in manager fetch their own accessible branch list — home
    branch first, then anything a group admin has granted via
    ManagerBranchAccess (authentication/utils.py:get_manager_accessible_branches).
    This is what BranchSwitcher.jsx calls to render the manager's own
    switcher (no "All Branches" option — only their specific branches).

    Also reports which branch this request is currently active in
    (IsManager's permission check already resolved and stashed this via
    resolve_manager_active_branch — see authentication/permissions.py),
    so the frontend can highlight the current selection without a second
    round trip.
    """
    permission_classes = [IsManager]

    def get(self, request):
        from authentication.utils import get_manager_accessible_branches, get_user_branch
        from administration.serializers import BranchSerializer

        branches = get_manager_accessible_branches(request.user)
        active = get_user_branch(request.user)

        return Response({
            "branches": BranchSerializer(branches, many=True).data,
            "active_branch_id": active.pk if active else None,
        })


# ═══════════════════════════════════════════════════════
# FINANCE DASHBOARD
# ═══════════════════════════════════════════════════════
class FinanceDashboardView(APIView):
    """
    GET /api/manager/finance/?period=today|week|month|year|custom
         &start=YYYY-MM-DD&end=YYYY-MM-DD

    Revenue, a full expense breakdown (manual expenses, salary actually
    paid, medicine purchases, supply purchases, refunds from returning
    medicine/supplies to providers), and profit/loss — plus a trend series
    whose granularity adapts to the period:
      week/month  -> daily points
      year        -> monthly points (12)
      custom      -> daily if range <= 62 days, else monthly
      today       -> single summary point
    """
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        period = request.query_params.get("period", "month")
        today  = timezone.localdate()

        bounds = _period_bounds(period, today, request)
        if bounds is None:
            return Response({"error": "Provide start and end dates for custom period."}, status=400)
        start, end = bounds
        if start > end:
            return Response({"error": "start date must be before end date."}, status=400)

        # ✅ FIX: this whole dashboard used to aggregate every source across
        # every branch with no filter at all, so a branch-scoped
        # Admin/Manager saw the entire group's revenue/expenses instead of
        # just their own branch's. A group admin (is_group_admin) still
        # sees everything, optionally narrowed with ?branch=<id>; everyone
        # else is always forced to their own StaffProfile.branch.
        branch, error = _resolve_dashboard_branch(request)
        if error:
            return error

        cons_rev,  cons_count  = _get_reception_revenue(start, end, branch)
        pharm_rev, pharm_count = _get_pharmacy_revenue(start, end, branch)
        lab_rev,   lab_count   = _get_lab_revenue(start, end, branch)
        home_visit_rev, home_visit_count = _get_home_visit_revenue(start, end, branch)  # subset of cons_rev, informational only
        income = _income_breakdown(start, end, branch)

        # ✅ FIX: "revenue.total" is what the frontend's Revenue card
        # displays, labeled "Consultation + Pharmacy" — it must only ever
        # be those two. Other Income (lab commission, donations, etc.) is
        # real money in and still correctly counts toward Profit/Loss
        # below, but it has its own dedicated "Other Income" stat card and
        # must not be silently folded into Revenue too, or the card's own
        # label becomes wrong.
        revenue_total = cons_rev + pharm_rev

        exp = _expenses_breakdown(start, end, branch)
        profit_loss = (revenue_total + income["total"]) - exp["net_expenses"]

        span_days = (end - start).days
        if period == "year" or (period == "custom" and span_days > 62):
            trend = _monthly_trend_series(start, end, branch)
        elif period == "today":
            trend = [_single_point_trend(start, end, today.strftime("%d %b"), branch)]
        else:
            trend = _daily_trend_series(start, end, branch)

        recent_expenses_qs = HospitalExpense.objects.filter(date__range=(start, end))
        if branch is not None:
            recent_expenses_qs = recent_expenses_qs.filter(branch=branch)

        return Response({
            "period": {"start": start, "end": end, "label": period},
            "revenue": {
                "total":        float(revenue_total),
                "consultation": {"amount": float(cons_rev),  "count": cons_count},
                "pharmacy":     {"amount": float(pharm_rev), "count": pharm_count},
                "laboratory":   {"amount": float(lab_rev),   "count": lab_count},
                "home_visit":   {"amount": float(home_visit_rev), "count": home_visit_count},  # subset of consultation, informational
                "other_income": {
                    "amount":        float(income["total"]),
                    "count":         income["count"],
                    "lab_commission": float(income["lab_commission"]),
                    "by_category":   income["by_category"],
                },
            },
            "expenses": {
                "total":              float(exp["net_expenses"]),
                "gross_total":        float(exp["gross_expenses"]),
                "manual":             {"total": float(exp["manual_total"]), "by_category": exp["by_category"]},
                "salary_paid":        {"amount": float(exp["salary_paid"]), "count": exp["salary_count"]},
                "medicine_purchases": {
                    "amount": float(exp["medicine_purchases"]), "count": exp["medicine_purchase_count"],
                    # Cash-basis reconciliation against the Dealers ledger — see
                    # _get_medicine_purchases_paid_due. "amount" above stays the
                    # full accrual value; these two never sum back to it exactly
                    # unless everything's fully settled, and that's expected.
                    "paid":  float(exp["medicine_purchases_paid"]),
                    "due":   float(exp["medicine_purchases_due"]),
                    "pending_review": exp["medicine_purchases_pending_review"],
                },
                "supply_purchases":   {
                    "amount": float(exp["supply_purchases"]),   "count": exp["supply_purchase_count"],
                    "paid":  float(exp["supply_purchases_paid"]),
                    "due":   float(exp["supply_purchases_due"]),
                    "pending_review": exp["supply_purchases_pending_review"],
                },
                "general_item_purchases": {
                    "amount": float(exp["general_item_purchases"]), "count": exp["general_item_purchase_count"],
                    "paid":  float(exp["general_item_purchases_paid"]),
                    "due":   float(exp["general_item_purchases_due"]),
                    "pending_review": exp["general_item_purchases_pending_review"],
                },
                "recent": HospitalExpenseSerializer(
                    recent_expenses_qs.order_by("-date")[:5], many=True
                ).data,
            },
            "refunds": {
                "medicine": {"amount": float(exp["medicine_refunds"]), "count": exp["medicine_refund_count"]},
                "supply":   {"amount": float(exp["supply_refunds"]),   "count": exp["supply_refund_count"]},
                "general_item": {"amount": float(exp["general_item_refunds"]), "count": exp["general_item_refund_count"]},
                "total":    float(exp["total_refunds"]),
            },
            "profit_loss": float(profit_loss),
            "trend": trend,
            "monthly_trend": trend if period == "year" else _monthly_trend_series(
                today.replace(day=1) - relativedelta(months=5), today, branch
            ),
        })


# ═══════════════════════════════════════════════════════
# BILLS OVERVIEW (All bills — reception, pharmacy, lab)
# ═══════════════════════════════════════════════════════
class AllBillsView(APIView):
    """
    GET /api/manager/bills/?start=&end=&source=all|reception|pharmacy|lab|prebooking
    """
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        today  = timezone.localdate()
        source = request.query_params.get("source", "all")
        try:
            start = date.fromisoformat(request.query_params.get("start", str(today.replace(day=1))))
            end   = date.fromisoformat(request.query_params.get("end",   str(today)))
        except ValueError:
            return Response({"error": "Invalid date format."}, status=400)

        # ✅ FIX: previously unscoped — a branch Admin/Manager could see
        # every branch's bills here. See _resolve_dashboard_branch.
        branch, error = _resolve_dashboard_branch(request)
        if error:
            return error

        bills = []
        failed_sources = []  # tracks which source queries raised, so the frontend
                              # can tell "genuinely no data" apart from "fetch broke"
                              # instead of both silently rendering as an empty tab.

        if source in ("all", "reception"):
            try:
                from reception.models import ConsultationBill
                from doctor.models import ConsultationStatus
                cb_qs = ConsultationBill.objects.filter(consultation_date__range=(start, end))
                if branch is not None:
                    cb_qs = cb_qs.filter(branch=branch)
                for b in cb_qs.select_related("patient", "consultation"):
                    # If reception has cancelled the linked appointment, surface that
                    # here as the bill's status (mirrors how Pharmacy's bill_status
                    # lifecycle, including CANCELLED, is surfaced via payment_status
                    # below) so a cancellation in Reception is reflected in the
                    # Manager's consolidated Bills view.
                    consultation = getattr(b, "consultation", None)
                    is_cancelled = bool(consultation and consultation.status == ConsultationStatus.CANCELLED)
                    bills.append({
                        "source":        "Reception",
                        "id":            b.bill_id,
                        "bill_number":   b.bill_number,
                        "date":          str(b.consultation_date),
                        "patient":       f"{b.patient.first_name} {b.patient.last_name}".strip() if b.patient else "—",
                        "amount":        float(b.total_amount),
                        "payment_status": "CANCELLED" if is_cancelled else b.payment_status,
                        "raw_payment_status": b.payment_status,
                        "consultation_status": consultation.status if consultation else None,
                        "payment_method": b.payment_method,
                        "consultation_type": b.consultation_type,
                        "home_visit_fee": float(b.consultation_fee) if b.consultation_type == "HOME_VISIT" else None,
                        "travel_charge": float(b.travel_charge) if b.consultation_type == "HOME_VISIT" else None,
                    })
            except Exception:
                logger.exception("AllBillsView: failed to load reception bills")
                failed_sources.append("reception")

        if source in ("all", "prebooking"):
            try:
                from reception.models import ConsultationPreBooking
                # CONVERTED bookings are excluded here: once converted, the
                # money is already represented by the resulting ConsultationBill
                # (see "Reception" above) — including both would double-list
                # the same payment under two rows. A booking still shows up
                # here right up until the moment it's converted.
                qs = ConsultationPreBooking.objects.filter(
                    requested_date__range=(start, end),
                ).exclude(status="CONVERTED").select_related("patient", "doctor", "guest_doctor")
                if branch is not None:
                    qs = qs.filter(branch=branch)
                for pb in qs:
                    bills.append({
                        "source":        "Prebooking",
                        "id":            pb.prebooking_id,
                        "bill_number":   f"PB-{pb.prebooking_id}",
                        "date":          str(pb.requested_date),
                        "patient":       pb.get_patient_name(),
                        "patient_type":  "registered" if pb.patient_id else "new (unregistered)",
                        "amount":        float(pb.consultation_fee),
                        "payment_status": pb.payment_status,
                        "booking_status": pb.status,
                        "payment_method": pb.payment_method,
                    })
            except Exception:
                logger.exception("AllBillsView: failed to load prebookings")
                failed_sources.append("prebooking")

        if source in ("all", "pharmacy"):
            try:
                from pharmacist.models import PharmacyBill
                lo, hi = _local_day_range(start, end)
                pb_qs = PharmacyBill.objects.filter(created_at__range=(lo, hi))
                if branch is not None:
                    pb_qs = pb_qs.filter(branch=branch)
                for b in pb_qs.select_related("patient"):   # FIX 3
                    bills.append({
                        "source":        "Pharmacy",
                        "id":            b.bill_id,
                        "bill_number":   b.bill_number,
                        "date":          str(timezone.localtime(b.created_at).date()),
                        "patient":       b.get_patient_name(),
                        "patient_type":  "walk-in" if b.is_walkin else "registered",
                        "amount":        float(b.total_amount),
                        # Pharmacy revenue is recognized off bill_status == "PAID" everywhere
                        # else in this module (see _get_pharmacy_revenue), so `payment_status`
                        # here reflects bill_status for consistency with the Finance Dashboard.
                        # `invoice_status` is the raw bill_status (fuller lifecycle: DRAFT/OPEN/
                        # READY/COMPLETED/PAID/CANCELLED). `raw_payment_status` is the underlying
                        # PharmacyBill.payment_status field, kept for reference since the two
                        # can differ (e.g. an OPEN bill may still show payment_status PENDING).
                        "payment_status": b.bill_status,
                        "invoice_status": b.bill_status,
                        "raw_payment_status": b.payment_status,
                        "payment_method": getattr(b, "payment_method", "—"),
                    })
            except Exception:
                logger.exception("AllBillsView: failed to load pharmacy bills")
                failed_sources.append("pharmacy")

        if source in ("all", "lab"):
            try:
                from lab.models import LabBill
                lo, hi = _local_day_range(start, end)
                lb_qs = LabBill.objects.filter(created_at__range=(lo, hi))
                if branch is not None:
                    lb_qs = lb_qs.filter(branch=branch)
                for b in lb_qs.select_related("patient", "lab_request"):   # FIX 3
                    is_walkin = bool(b.lab_request and b.lab_request.is_walkin)
                    if is_walkin:
                        patient_name = b.lab_request.get_patient_name()
                    elif b.patient:
                        patient_name = f"{b.patient.first_name} {b.patient.last_name}".strip()
                    else:
                        patient_name = "—"
                    bills.append({
                        "source":        "Laboratory",
                        "id":            b.bill_id,
                        "bill_number":   b.bill_number,
                        "date":          str(timezone.localtime(b.created_at).date()),
                        "patient":       patient_name,
                        "patient_type":  "walk-in" if is_walkin else "registered",
                        "amount":        float(b.total_amount),
                        "payment_status": b.payment_status,
                        "payment_method": b.payment_method,
                    })
            except Exception:
                logger.exception("AllBillsView: failed to load lab bills")
                failed_sources.append("lab")

        bills.sort(key=lambda x: x["date"], reverse=True)
        return Response({"bills": bills, "total": len(bills), "failed_sources": failed_sources})


class BillDetailView(APIView):
    """
    GET /api/manager/bills/detail/?source=reception|pharmacy|lab|prebooking&id=<bill_id>

    Full single-bill detail (line items included) for the "view" action on
    AllBillsView's list. Reads models directly, same pattern as AllBillsView,
    so it works for any source regardless of the manager's other role
    permissions.
    """
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        source = (request.query_params.get("source") or "").lower()
        bill_id = request.query_params.get("id")
        if not bill_id:
            return Response({"error": "id is required."}, status=400)

        # ✅ FIX: previously unscoped — a branch Admin/Manager could view
        # another branch's bill just by guessing/incrementing its id.
        branch, error = _resolve_dashboard_branch(request)
        if error:
            return error
        branch_filter = {"branch": branch} if branch is not None else {}

        if source == "prebooking":
            from reception.models import ConsultationPreBooking
            pb = get_object_or_404(
                ConsultationPreBooking.objects.select_related("patient", "doctor", "guest_doctor").filter(**branch_filter),
                pk=bill_id,
            )
            return Response({
                "source":              "Prebooking",
                "bill_number":         f"PB-{pb.prebooking_id}",
                "date":                str(pb.requested_date),
                "requested_time":      str(pb.requested_time),
                "patient": {
                    "name":  pb.get_patient_name(),
                    "mrd":   pb.patient.mrd_number if pb.patient_id else None,
                    "phone": pb.get_patient_phone(),
                },
                "patient_type":        "registered" if pb.patient_id else "new (unregistered)",
                "doctor_name":         pb.get_doctor_name(),
                "booking_mode":        pb.booking_mode,
                "consultation_type":   pb.consultation_type,
                "consultation_fee":    float(pb.consultation_fee),
                "total_amount":        float(pb.consultation_fee),
                "payment_method":      pb.payment_method,
                "payment_status":      pb.payment_status,
                "booking_status":      pb.status,
                "converted_bill_id":   pb.converted_bill_id,
                "line_items": [
                    {"label": "Consultation fee (advance)", "amount": float(pb.consultation_fee)},
                ],
            })

        if source == "reception":
            from reception.models import ConsultationBill
            from doctor.models import ConsultationStatus
            b = get_object_or_404(
                ConsultationBill.objects.select_related("patient", "doctor", "guest_doctor", "consultation").filter(**branch_filter),
                pk=bill_id,
            )
            consultation = getattr(b, "consultation", None)
            is_cancelled = bool(consultation and consultation.status == ConsultationStatus.CANCELLED)
            return Response({
                "source":              "Reception",
                "bill_number":         b.bill_number,
                "op_number":           b.op_number,
                "date":                str(b.consultation_date),
                "patient": {
                    "name":  f"{b.patient.first_name} {b.patient.last_name}".strip() if b.patient else "—",
                    "mrd":   b.patient.mrd_number if b.patient else None,
                    "phone": b.patient.phone if b.patient else None,
                },
                "doctor_name":         b.doctor_name,
                "consultation_type":   b.consultation_type,
                "consultation_fee":    float(b.consultation_fee),
                "registration_fee":    float(b.registration_fee),
                "total_amount":        float(b.total_amount),
                "payment_method":      b.payment_method,
                "upi_reference":       b.upi_reference,
                "payment_status":      "CANCELLED" if is_cancelled else b.payment_status,
                "bill_status":         "CANCELLED" if is_cancelled else None,
                "consultation_status": consultation.status if consultation else None,
                "notes":               b.notes,
                "line_items": [
                    {"label": "Registration fee", "amount": float(b.registration_fee)},
                    {"label": "Consultation fee", "amount": float(b.consultation_fee)},
                ],
            })

        if source == "pharmacy":
            from pharmacist.models import PharmacyBill
            b = get_object_or_404(
                PharmacyBill.objects.select_related("patient").prefetch_related(
                    "medicine_items__batch__medicine", "procedure_items__procedure",
                    "general_items__batch__general_item",
                ).filter(**branch_filter),
                pk=bill_id,
            )
            return Response({
                "source":              "Pharmacy",
                "bill_number":         b.bill_number,
                "date":                str(timezone.localtime(b.created_at).date()),
                "patient": {
                    "name":  b.get_patient_name(),
                    "phone": b.get_patient_phone(),
                    "type":  "walk-in" if b.is_walkin else "registered",
                },
                "bill_status":         b.bill_status,
                "payment_status":      b.payment_status,
                "payment_method":      b.payment_method,
                "upi_reference":       b.upi_reference,
                "bill_type":           b.bill_type,
                "subtotal":            float(b.subtotal),
                "gst_amount":          float(b.gst_amount),
                "margin_adjustment":   float(b.margin_adjustment),
                "total_amount":        float(b.total_amount),
                "notes":               b.notes,
                "line_items": [
                    {
                        "label":  f"{i.batch.medicine.name} × {i.quantity}",
                        "amount": float(i.item_total),
                    }
                    for i in b.medicine_items.all()
                ] + [
                    {
                        "label":  f"{i.display_name} × {i.quantity}",
                        "amount": float(i.item_total),
                    }
                    for i in b.procedure_items.all()
                ] + [
                    {
                        "label":  f"{i.batch.general_item.name} × {i.quantity}",
                        "amount": float(i.item_total),
                    }
                    for i in b.general_items.all()
                ],
            })

        if source == "lab":
            from lab.models import LabBill
            b = get_object_or_404(
                LabBill.objects.select_related("patient", "lab_request").prefetch_related(
                    "lab_request__items__test"
                ).filter(**branch_filter),
                pk=bill_id,
            )
            is_walkin = bool(b.lab_request and b.lab_request.is_walkin)
            if is_walkin:
                patient_name = b.lab_request.get_patient_name()
                patient_phone = b.lab_request.get_patient_phone()
            elif b.patient:
                patient_name = f"{b.patient.first_name} {b.patient.last_name}".strip()
                patient_phone = b.patient.phone
            else:
                patient_name = "—"
                patient_phone = None
            return Response({
                "source":              "Laboratory",
                "bill_number":         b.bill_number,
                "date":                str(timezone.localtime(b.created_at).date()),
                "patient": {
                    "name":  patient_name,
                    "phone": patient_phone,
                    "type":  "walk-in" if is_walkin else "registered",
                },
                "subtotal":            float(b.subtotal),
                "discount":            float(b.discount),
                "total_amount":        float(b.total_amount),
                "paid_amount":         float(b.paid_amount),
                "payment_status":      b.payment_status,
                "payment_method":      b.payment_method,
                "notes":               b.notes,
                "line_items": [
                    {"label": f"{item.test.code} — {item.test.name}", "amount": float(item.test.price)}
                    for item in b.lab_request.items.all()
                ] if b.lab_request_id else [],
            })

        return Response({"error": "Invalid source. Use reception, pharmacy, lab, or prebooking."}, status=400)


# ═══════════════════════════════════════════════════════
# PURCHASES & REFUNDS (medicine + supplies, read from pharmacist app)
# ═══════════════════════════════════════════════════════
class PurchasesAndRefundsView(APIView):
    """
    GET /api/manager/purchases/?start=&end=&type=all|medicine|supply|general_item

    Itemized ledger backing the totals shown on the finance dashboard:
    every medicine/supply/general-item batch received in the period
    (purchases), and every medicine/supply/general-item return-to-provider
    in the period (refunds). Read-only — purchases and returns are
    entered/actioned in the pharmacist module; the manager views them here.
    """
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        today = timezone.localdate()
        kind  = request.query_params.get("type", "all")
        try:
            start = date.fromisoformat(request.query_params.get("start", str(today.replace(day=1))))
            end   = date.fromisoformat(request.query_params.get("end",   str(today)))
        except ValueError:
            return Response({"error": "Invalid date format."}, status=400)

        # ✅ FIX: previously unscoped — a branch Admin/Manager could see
        # every branch's purchases/refunds here.
        branch, error = _resolve_dashboard_branch(request)
        if error:
            return error

        purchases, refunds = [], []

        # ✅ FIX: every row here used to show only the batch's/return's own
        # full-invoice `amount`, with nothing indicating whether the dealer
        # had actually been paid/refunded that amount yet — a manager
        # confirming a ₹150 purchase as Paid Now but only actually paying
        # ₹100 (a partial payment) would see "₹150" here with no sign that
        # ₹50 of it is still due, even though the Dealers page correctly
        # showed "₹100 paid, ₹50 due". `amount_paid`/`amount_due` for
        # purchases and `amount_received`/`amount_pending` for refunds are
        # reconciled from the Dealers ledger's confirmed PAYMENT/CASH_REFUND
        # legs via _dealer_settlement_map, same as the dashboard summary.
        # `amount` itself is left untouched — it's still the correct full
        # invoice/expected-refund value for accrual accounting.
        if kind in ("all", "medicine"):
            try:
                from pharmacist.models import MedicineBatch, MedicineReturnToProvider
                lo, hi = _local_day_range(start, end)
                mb_qs = MedicineBatch.objects.filter(created_at__range=(lo, hi))
                mr_qs = MedicineReturnToProvider.objects.filter(created_at__range=(lo, hi))
                if branch is not None:
                    mb_qs = mb_qs.filter(medicine__branch=branch)
                    mr_qs = mr_qs.filter(batch__medicine__branch=branch)
                mb_list = list(mb_qs.select_related("medicine"))   # FIX 3
                mr_list = list(mr_qs.select_related("batch__medicine"))   # FIX 3
                mb_settle = _dealer_settlement_map(
                    "MEDICINE_BATCH", [b.batch_id for b in mb_list], "PURCHASE"
                )
                mr_settle = _dealer_settlement_map(
                    "MEDICINE_RETURN", [r.return_id for r in mr_list], "CREDIT_NOTE"
                )
                for b in mb_list:
                    s = mb_settle.get(b.batch_id)
                    # REJECTED purchase — dropped from purchases the same way
                    # the accrual totals drop it (see _rejected_purchase_source_ids).
                    # Still visible on the Dealer's own Ledger History for audit.
                    if s and s["txn_status"] == "REJECTED":
                        continue
                    amount = float(b.cost_price * b.quantity)
                    purchases.append({
                        "type": "Medicine", "item": b.medicine.name if b.medicine else "—",
                        "batch_number": b.batch_number, "quantity": b.quantity,
                        "unit_cost": float(b.cost_price), "amount": amount,
                        "amount_paid": float(s["amount_paid"]) if s else amount,
                        "amount_due": float(s["amount_due"]) if s else 0.0,
                        "settlement_status": _settlement_status_label(s),
                        "date": str(timezone.localtime(b.created_at).date()),
                    })
                for r in mr_list:
                    s = mr_settle.get(r.return_id)
                    amount = float(r.refund_amount)
                    refunds.append({
                        "type": "Medicine", "item": r.batch.medicine.name if r.batch and r.batch.medicine else "—",
                        "quantity": r.quantity, "amount": amount,
                        "amount_received": float(s["amount_paid"]) if s else 0.0,
                        "amount_pending": float(s["amount_due"]) if s else amount,
                        "settlement_status": _settlement_status_label(s),
                        "reason": r.get_reason_display(), "status": r.status,
                        "date": str(timezone.localtime(r.created_at).date()),
                    })
            except Exception:
                logger.exception("_monthly_trend_series failed")
                pass

        if kind in ("all", "supply"):
            try:
                from pharmacist.models import SupplyBatch, SupplyReturn
                sb_qs = SupplyBatch.objects.filter(purchase_date__range=(start, end))
                sr_qs = SupplyReturn.objects.filter(returned_at__range=_local_day_range(start, end))
                if branch is not None:
                    sb_qs = sb_qs.filter(supply_item__branch=branch)
                    sr_qs = sr_qs.filter(supply_item__branch=branch)
                sb_list = list(sb_qs.select_related("supply_item"))
                sr_list = list(sr_qs.select_related("supply_item"))   # FIX 3
                sb_settle = _dealer_settlement_map(
                    "SUPPLY_BATCH", [b.batch_id for b in sb_list], "PURCHASE"
                )
                sr_settle = _dealer_settlement_map(
                    "SUPPLY_RETURN", [r.return_id for r in sr_list], "CREDIT_NOTE"
                )
                for b in sb_list:
                    s = sb_settle.get(b.batch_id)
                    if s and s["txn_status"] == "REJECTED":
                        continue
                    amount = float(b.total_cost)
                    purchases.append({
                        "type": "Supply", "item": b.supply_item.name if b.supply_item else "—",
                        "batch_number": b.batch_number, "quantity": b.original_quantity,
                        "unit_cost": float(b.unit_cost), "amount": amount,
                        "amount_paid": float(s["amount_paid"]) if s else amount,
                        "amount_due": float(s["amount_due"]) if s else 0.0,
                        "settlement_status": _settlement_status_label(s),
                        "date": str(b.purchase_date),
                    })
                for r in sr_list:
                    s = sr_settle.get(r.return_id)
                    amount = float(r.refund_amount)
                    refunds.append({
                        "type": "Supply", "item": r.supply_item.name if r.supply_item else "—",
                        "quantity": r.quantity_returned, "amount": amount,
                        "amount_received": float(s["amount_paid"]) if s else 0.0,
                        "amount_pending": float(s["amount_due"]) if s else amount,
                        "settlement_status": _settlement_status_label(s),
                        "reason": r.get_reason_display(),
                        # ✅ FIX: unlike MedicineReturnToProvider, SupplyReturn has no
                        # separate approval-workflow `status` field — it never did (see
                        # migrations), so `r.status` here raised AttributeError on every
                        # single supply return, silently aborting this whole loop via the
                        # broad except below and making supply refunds vanish from this
                        # tab entirely. `settlement_status` (from the Dealers ledger,
                        # above) already tells the manager where this return stands.
                        "date": str(timezone.localtime(r.returned_at).date()),
                    })
            except Exception:
                logger.exception("_monthly_trend_series failed")
                pass

        # ✅ NEW: general items (FMCG/non-medicine retail stock — diapers,
        # soap, etc., see GeneralItem in pharmacist/models.py) were never
        # included in this view at all before, despite having the exact
        # same optional-dealer purchase/return shape as medicine and
        # supply. Mirrors the medicine block above (GeneralItemBatch, like
        # MedicineBatch, has only a live `quantity`, not an immutable
        # received-quantity snapshot — same "indicative, not exact"
        # caveat applies) and the supply block for refunds (GeneralItemReturn,
        # like SupplyReturn, has no separate `status` workflow field).
        if kind in ("all", "general_item"):
            try:
                from pharmacist.models import GeneralItemBatch, GeneralItemReturn
                lo, hi = _local_day_range(start, end)
                gb_qs = GeneralItemBatch.objects.filter(created_at__range=(lo, hi))
                gr_qs = GeneralItemReturn.objects.filter(returned_at__range=(lo, hi))
                if branch is not None:
                    gb_qs = gb_qs.filter(general_item__branch=branch)
                    gr_qs = gr_qs.filter(general_item__branch=branch)
                gb_list = list(gb_qs.select_related("general_item"))
                gr_list = list(gr_qs.select_related("general_item"))
                gb_settle = _dealer_settlement_map(
                    "GENERAL_ITEM_BATCH", [b.batch_id for b in gb_list], "PURCHASE"
                )
                gr_settle = _dealer_settlement_map(
                    "GENERAL_ITEM_RETURN", [r.return_id for r in gr_list], "CREDIT_NOTE"
                )
                for b in gb_list:
                    s = gb_settle.get(b.batch_id)
                    if s and s["txn_status"] == "REJECTED":
                        continue
                    amount = float(b.cost_price * b.quantity)
                    purchases.append({
                        "type": "General Item", "item": b.general_item.name if b.general_item else "—",
                        "batch_number": b.batch_number, "quantity": b.quantity,
                        "unit_cost": float(b.cost_price), "amount": amount,
                        "amount_paid": float(s["amount_paid"]) if s else amount,
                        "amount_due": float(s["amount_due"]) if s else 0.0,
                        "settlement_status": _settlement_status_label(s),
                        "date": str(timezone.localtime(b.created_at).date()),
                    })
                for r in gr_list:
                    s = gr_settle.get(r.return_id)
                    amount = float(r.refund_amount)
                    refunds.append({
                        "type": "General Item", "item": r.general_item.name if r.general_item else "—",
                        "quantity": r.quantity_returned, "amount": amount,
                        "amount_received": float(s["amount_paid"]) if s else 0.0,
                        "amount_pending": float(s["amount_due"]) if s else amount,
                        "settlement_status": _settlement_status_label(s),
                        "reason": r.get_reason_display(),
                        "date": str(timezone.localtime(r.returned_at).date()),
                    })
            except Exception:
                logger.exception("_monthly_trend_series failed")
                pass

        purchases.sort(key=lambda x: x["date"], reverse=True)
        refunds.sort(key=lambda x: x["date"], reverse=True)
        return Response({
            "purchases": purchases,
            "purchases_total": sum(p["amount"] for p in purchases),
            "purchases_paid_total": sum(p["amount_paid"] for p in purchases),
            "purchases_due_total": sum(p["amount_due"] for p in purchases),
            "refunds": refunds,
            "refunds_total": sum(r["amount"] for r in refunds),
            "refunds_received_total": sum(r["amount_received"] for r in refunds),
            "refunds_pending_total": sum(r["amount_pending"] for r in refunds),
        })


# ═══════════════════════════════════════════════════════
# SUPPORT STAFF
# ═══════════════════════════════════════════════════════
class SupportStaffListView(APIView):
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        qs = scope_queryset_to_branch(
            SupportStaff.objects.all(), request.user,
            branch_field="branch", branch_id=request.query_params.get("branch"),
        )
        search = request.query_params.get("search")
        role   = request.query_params.get("role")
        active = request.query_params.get("active")

        if search:
            qs = qs.filter(
                Q(full_name__icontains=search) |
                Q(staff_code__icontains=search) |
                Q(phone__icontains=search)
            )
        if role:
            qs = qs.filter(role=role)
        if active is not None:
            qs = qs.filter(is_active=(active.lower() == "true"))

        return Response(SupportStaffSerializer(qs, many=True).data)

    def post(self, request):
        # ✅ FIX: branch is a required model field but was never resolved
        # here — an ordinary user is always pinned to their own branch; a
        # group admin must pass branch explicitly.
        branch, error = resolve_branch_for_write(request, required=True)
        if error:
            return error
        data = request.data.copy()
        data["branch"] = getattr(branch, "pk", branch)

        ser = SupportStaffSerializer(data=data)
        if ser.is_valid():
            ser.save()
            return Response({"message": "Support staff created.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class SupportStaffDetailView(APIView):
    permission_classes = [IsAdminOrManager]

    def _get(self, request, pk):
        qs = scope_queryset_to_branch(SupportStaff.objects.all(), request.user, branch_field="branch")
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):
        return Response(SupportStaffSerializer(self._get(request, pk)).data)

    def put(self, request, pk):
        obj = self._get(request, pk)
        data = request.data
        # A branch-scoped user can never reassign an existing row to a
        # different branch by PUT/PATCHing "branch" — only a group admin can.
        if "branch" in data and not is_group_admin_user(request.user):
            data = data.copy()
            data.pop("branch", None)
        ser = SupportStaffSerializer(obj, data=data)
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def patch(self, request, pk):
        obj = self._get(request, pk)
        data = request.data
        if "branch" in data and not is_group_admin_user(request.user):
            data = data.copy()
            data.pop("branch", None)
        ser = SupportStaffSerializer(obj, data=data, partial=True)
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        obj = self._get(request, pk)
        obj.is_active = False
        obj.save(update_fields=["is_active", "updated_at"])
        return Response({"message": "Support staff deactivated."})


class SupportStaffActivateView(APIView):
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        qs = scope_queryset_to_branch(SupportStaff.objects.all(), request.user, branch_field="branch")
        obj = get_object_or_404(qs, pk=pk)
        obj.is_active = True
        obj.save(update_fields=["is_active", "updated_at"])
        return Response({"message": "Support staff reactivated."})


# ═══════════════════════════════════════════════════════
# ALL STAFF LIST (both StaffProfile + SupportStaff)
# Used for attendance/leave/salary dropdowns
# ═══════════════════════════════════════════════════════
class AllStaffListView(APIView):
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        include_inactive = request.query_params.get("include_inactive", "false").lower() == "true"

        # ✅ FIX: previously unscoped — a branch Manager/Admin saw every
        # branch's staff in this dropdown-feeding list.
        staff_profiles = scope_queryset_to_branch(
            StaffProfile.objects.select_related("user"), request.user,
            branch_field="branch", branch_id=request.query_params.get("branch"),
        )
        support_staff = scope_queryset_to_branch(
            SupportStaff.objects.all(), request.user,
            branch_field="branch", branch_id=request.query_params.get("branch"),
        )

        if not include_inactive:
            staff_profiles = staff_profiles.filter(is_active=True)
            support_staff  = support_staff.filter(is_active=True)

        result = []

        for sp in staff_profiles:
            result.append({
                "id":         sp.id,
                "type":       "staff_profile",
                "staff_code": sp.staff_code,
                "full_name":  _staff_profile_display_name(sp),
                "role":       sp.role,
                "salary":     float(sp.salary or 0),
                "is_active":  sp.is_active,
            })

        for ss in support_staff:
            result.append({
                "id":           ss.staff_id,
                "type":         "support_staff",
                "staff_code":   ss.staff_code,
                "full_name":    ss.full_name,
                "role":         ss.role,
                "salary":       float(ss.monthly_salary or 0),
                "salary_type":  ss.salary_type,
                "daily_rate":   float(ss.daily_rate or 0),
                "is_active":    ss.is_active,
            })

        result.sort(key=lambda x: x["full_name"])
        return Response(result)


# ═══════════════════════════════════════════════════════
# ATTENDANCE
# ═══════════════════════════════════════════════════════
class AttendanceListView(APIView):
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        today = timezone.localdate()
        try:
            # Accept ?date=YYYY-MM-DD (single day) OR ?start=&end= (range).
            # The frontend AttendancePage sends { date } so we must read it.
            single_date = request.query_params.get("date")
            if single_date:
                start = end = date.fromisoformat(single_date)
            else:
                start = date.fromisoformat(request.query_params.get("start", str(today)))
                end   = date.fromisoformat(request.query_params.get("end",   str(today)))
        except ValueError:
            return Response({"error": "Invalid date."}, status=400)

        # ✅ FIX: previously unscoped — a branch Admin/Manager could see
        # every branch's attendance here.
        qs = _scope_dual_staff_qs(
            Attendance.objects.filter(date__range=(start, end)).select_related(
                "staff_profile__user", "support_staff"
            ),
            request.user,
        )

        staff_type = request.query_params.get("staff_type")
        if staff_type == "staff_profile":
            qs = qs.filter(staff_profile__isnull=False)
        elif staff_type == "support_staff":
            qs = qs.filter(support_staff__isnull=False)

        return Response(AttendanceSerializer(qs, many=True).data)

    def post(self, request):
        """Create or update a single attendance record."""
        ser = AttendanceSerializer(data=request.data)
        if ser.is_valid():
            # Upsert logic
            sp = ser.validated_data.get("staff_profile")
            ss = ser.validated_data.get("support_staff")
            dt = ser.validated_data["date"]

            # ✅ FIX: reject cross-branch staff_id references on write — a
            # branch-scoped user could otherwise mark attendance for staff
            # belonging to a different branch.
            target_branch_id = sp.branch_id if sp else (ss.branch_id if ss else None)
            if target_branch_id is not None and not _user_in_branch(request.user, target_branch_id):
                return Response(
                    {"error": "You can only mark attendance for staff in your own branch."},
                    status=403,
                )

            try:
                if sp:
                    obj = Attendance.objects.get(date=dt, staff_profile=sp)
                else:
                    obj = Attendance.objects.get(date=dt, support_staff=ss)
                update_ser = AttendanceSerializer(obj, data=request.data, partial=True)
                if update_ser.is_valid():
                    update_ser.save(marked_by=request.user)
                    return Response(update_ser.data)
                return Response(update_ser.errors, status=400)
            except Attendance.DoesNotExist:
                ser.save(marked_by=request.user)
                return Response(ser.data, status=201)

        return Response(ser.errors, status=400)


class AttendanceBulkView(APIView):
    """POST /api/manager/attendance/bulk/ — mark attendance for all staff in one shot."""
    permission_classes = [IsAdminOrManager]

    def post(self, request):
        """
        Body:
        {
          "date": "2025-06-01",
          "records": [
            {"staff_type": "staff_profile", "staff_id": 3, "status": "Present"},
            {"staff_type": "support_staff", "staff_id": 1, "status": "Absent"},
            ...
          ]
        }
        """
        try:
            att_date = date.fromisoformat(request.data["date"])
        except (KeyError, ValueError):
            return Response({"error": "Valid date is required."}, status=400)

        records = request.data.get("records", [])
        if not records:
            return Response({"error": "No records provided."}, status=400)

        created, updated, errors = 0, 0, []

        for rec in records:
            try:
                staff_type = rec["staff_type"]
                staff_id   = rec["staff_id"]
                att_status = rec.get("status", "Present")

                if staff_type == "staff_profile":
                    sp = StaffProfile.objects.get(pk=staff_id)
                    # ✅ FIX: reject cross-branch staff_id references — a
                    # branch-scoped user could otherwise bulk-mark
                    # attendance for another branch's staff.
                    if not _user_in_branch(request.user, sp.branch_id):
                        raise PermissionError("You can only mark attendance for staff in your own branch.")
                    obj, was_created = Attendance.objects.update_or_create(
                        date=att_date, staff_profile=sp,
                        defaults={"status": att_status, "notes": rec.get("notes", ""), "marked_by": request.user},
                    )
                else:
                    ss = SupportStaff.objects.get(pk=staff_id)
                    if not _user_in_branch(request.user, ss.branch_id):
                        raise PermissionError("You can only mark attendance for staff in your own branch.")
                    obj, was_created = Attendance.objects.update_or_create(
                        date=att_date, support_staff=ss,
                        defaults={"status": att_status, "notes": rec.get("notes", ""), "marked_by": request.user},
                    )

                if was_created:
                    created += 1
                else:
                    updated += 1

            except Exception as e:
                logger.warning("Attendance bulk-mark row failed: %s", e)
                errors.append({"rec": rec, "error": _safe_detail(e)})

        return Response({
            "message": f"Attendance marked: {created} created, {updated} updated.",
            "errors":  errors,
        })


class AttendanceSummaryView(APIView):
    """GET /api/manager/attendance/summary/?month=6&year=2025"""
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        today = timezone.localdate()
        month = int(request.query_params.get("month", today.month))
        year  = int(request.query_params.get("year",  today.year))

        _, last_day = calendar.monthrange(year, month)
        start = date(year, month, 1)
        end   = date(year, month, last_day)

        # ✅ FIX: previously unscoped — a branch Admin/Manager could see
        # every branch's attendance summary here.
        qs = _scope_dual_staff_qs(
            Attendance.objects.filter(date__range=(start, end)).select_related(
                "staff_profile__user", "support_staff"
            ),
            request.user,
        )

        # Build a map: staff → {status: count}
        summary = {}

        for att in qs:
            if att.staff_profile_id:
                key  = f"sp-{att.staff_profile_id}"
                name = _staff_profile_display_name(att.staff_profile)
                role = att.staff_profile.role
                code = att.staff_profile.staff_code
                stype = "staff_profile"
            else:
                key  = f"ss-{att.support_staff_id}"
                name = att.support_staff.full_name
                role = att.support_staff.role
                code = att.support_staff.staff_code
                stype = "support_staff"

            if key not in summary:
                summary[key] = {
                    "staff_key": key, "name": name, "role": role,
                    "staff_code": code, "staff_type": stype,
                    "present": 0, "absent": 0, "paid_leave": 0,
                    "unpaid_leave": 0, "holiday": 0, "half_day": 0,
                }

            s = att.status
            if s == "Present":         summary[key]["present"]      += 1
            elif s == "Absent":        summary[key]["absent"]       += 1
            elif s == "Paid Leave":    summary[key]["paid_leave"]   += 1
            elif s == "Unpaid Leave":  summary[key]["unpaid_leave"] += 1
            elif s == "Holiday":       summary[key]["holiday"]      += 1
            elif s == "Half Day":      summary[key]["half_day"]     += 1

        return Response({
            "month": month, "year": year,
            "start": start, "end": end,
            "summary": sorted(summary.values(), key=lambda x: x["name"]),
        })


# ═══════════════════════════════════════════════════════
# LEAVE MANAGEMENT
# ═══════════════════════════════════════════════════════
class LeaveRequestListView(APIView):
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        # ✅ FIX: previously unscoped — a branch Admin/Manager could see
        # every branch's leave requests here.
        qs = _scope_dual_staff_qs(
            LeaveRequest.objects.select_related("staff_profile__user", "support_staff", "approved_by"),
            request.user,
        )

        status_f = request.query_params.get("status")
        ltype    = request.query_params.get("leave_type")
        if status_f:
            qs = qs.filter(status=status_f)
        if ltype:
            qs = qs.filter(leave_type=ltype)

        return Response(LeaveRequestSerializer(qs, many=True).data)

    def post(self, request):
        ser = LeaveRequestSerializer(data=request.data)
        if ser.is_valid():
            # ✅ FIX: reject cross-branch staff_id references — a
            # branch-scoped user could otherwise file a leave request
            # against another branch's staff member.
            sp = ser.validated_data.get("staff_profile")
            ss = ser.validated_data.get("support_staff")
            target_branch_id = sp.branch_id if sp else (ss.branch_id if ss else None)
            if target_branch_id is not None and not _user_in_branch(request.user, target_branch_id):
                return Response(
                    {"error": "You can only create leave requests for staff in your own branch."},
                    status=403,
                )
            ser.save()
            return Response({"message": "Leave request created.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class LeaveRequestDetailView(APIView):
    permission_classes = [IsAdminOrManager]

    def _get(self, request, pk):
        # ✅ FIX: previously unscoped — a branch Admin/Manager could view
        # or edit another branch's leave request just by guessing its id.
        qs = _scope_dual_staff_qs(LeaveRequest.objects.all(), request.user)
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):
        return Response(LeaveRequestSerializer(self._get(request, pk)).data)

    def patch(self, request, pk):
        obj = self._get(request, pk)
        ser = LeaveRequestSerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=400)


class LeaveApproveView(APIView):
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        # ✅ FIX: previously unscoped — a branch Admin/Manager could
        # approve another branch's leave request.
        qs = _scope_dual_staff_qs(LeaveRequest.objects.all(), request.user)
        obj = get_object_or_404(qs, pk=pk)
        if obj.status != "Pending":
            return Response({"error": "Only pending leaves can be approved."}, status=400)
        obj.status      = "Approved"
        obj.approved_by = request.user
        obj.approved_at = timezone.now()
        obj.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])

        # Auto-create attendance records for the leave dates
        _auto_mark_leave_attendance(obj, request.user)

        return Response({"message": "Leave approved."})


class LeaveRejectView(APIView):
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        # ✅ FIX: previously unscoped — a branch Admin/Manager could
        # reject another branch's leave request.
        qs = _scope_dual_staff_qs(LeaveRequest.objects.all(), request.user)
        obj = get_object_or_404(qs, pk=pk)
        if obj.status != "Pending":
            return Response({"error": "Only pending leaves can be rejected."}, status=400)
        obj.status           = "Rejected"
        obj.rejection_reason = request.data.get("rejection_reason", "")
        obj.approved_by      = request.user
        obj.approved_at      = timezone.now()
        obj.save(update_fields=["status", "rejection_reason", "approved_by", "approved_at", "updated_at"])
        return Response({"message": "Leave rejected."})


def _auto_mark_leave_attendance(leave: LeaveRequest, marked_by):
    """When a leave is approved, create/update attendance records."""
    att_status = "Paid Leave" if leave.leave_type == "Paid" else "Unpaid Leave"
    d = leave.start_date
    while d <= leave.end_date:
        if leave.staff_profile_id:
            Attendance.objects.update_or_create(
                date=d, staff_profile_id=leave.staff_profile_id,
                defaults={"status": att_status, "marked_by": marked_by,
                          "notes": f"Auto-marked from leave #{leave.leave_id}"},
            )
        elif leave.support_staff_id:
            Attendance.objects.update_or_create(
                date=d, support_staff_id=leave.support_staff_id,
                defaults={"status": att_status, "marked_by": marked_by,
                          "notes": f"Auto-marked from leave #{leave.leave_id}"},
            )
        d += timedelta(days=1)


# ═══════════════════════════════════════════════════════
# SALARY MANAGEMENT
# ═══════════════════════════════════════════════════════
class SalaryListView(APIView):
    # Salary is decided exclusively by the Manager (Admin handles staff/EMR
    # creation instead) — restricted to IsManager, not IsAdminOrManager.
    permission_classes = [IsManager]

    def get(self, request):
        # Lab Technicians are only issued login credentials to mark attendance
        # and record their work — the hospital does not pay their salary
        # through this system, so they're excluded here even if a legacy
        # record exists from before this was enforced at creation time.
        # ✅ FIX: previously unscoped — a branch Manager could see every
        # branch's salary records here.
        qs = _scope_dual_staff_qs(
            SalaryRecord.objects.select_related("staff_profile__user", "support_staff")
            .exclude(staff_profile__role="Lab Technician"),
            request.user,
        )
        month       = request.query_params.get("month")
        year        = request.query_params.get("year")
        is_paid     = request.query_params.get("is_paid")
        search      = request.query_params.get("search")
        staff_type  = request.query_params.get("staff_type")

        if month:   qs = qs.filter(month=int(month))
        if year:    qs = qs.filter(year=int(year))
        if is_paid is not None:
            qs = qs.filter(is_paid=(is_paid.lower() == "true"))
        if staff_type == "doctor":
            qs = qs.filter(staff_profile__role="Doctor")
        elif staff_type == "manager":
            qs = qs.filter(staff_profile__role="Manager")
        elif staff_type == "support_staff":
            qs = qs.filter(support_staff__isnull=False)
        elif staff_type == "other":
            qs = qs.filter(staff_profile__isnull=False).exclude(staff_profile__role__in=["Doctor", "Manager"])
        if search:
            qs = qs.filter(
                Q(staff_profile__user__first_name__icontains=search)  |
                Q(staff_profile__user__last_name__icontains=search)   |
                Q(staff_profile__staff_code__icontains=search)        |
                Q(support_staff__full_name__icontains=search)         |
                Q(support_staff__staff_code__icontains=search)
            )
        return Response(SalaryRecordSerializer(qs, many=True).data)

    def post(self, request):
        ser = SalaryRecordSerializer(data=request.data)
        if ser.is_valid():
            # ✅ FIX: reject cross-branch staff_id references — a
            # branch-scoped manager could otherwise create a salary
            # record for another branch's staff member.
            sp = ser.validated_data.get("staff_profile")
            ss = ser.validated_data.get("support_staff")
            target_branch_id = sp.branch_id if sp else (ss.branch_id if ss else None)
            if target_branch_id is not None and not _user_in_branch(request.user, target_branch_id):
                return Response(
                    {"error": "You can only create salary records for staff in your own branch."},
                    status=403,
                )
            ser.save()
            return Response({"message": "Salary record created.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class SalaryDetailView(APIView):
    # Salary is decided exclusively by the Manager (Admin handles staff/EMR
    # creation instead) — restricted to IsManager, not IsAdminOrManager.
    permission_classes = [IsManager]

    def _get(self, request, pk):
        # ✅ FIX: previously unscoped — a branch Manager could view or
        # edit another branch's salary record just by guessing its id.
        qs = _scope_dual_staff_qs(SalaryRecord.objects.all(), request.user)
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):
        return Response(SalaryRecordSerializer(self._get(request, pk)).data)

    def patch(self, request, pk):
        obj = self._get(request, pk)
        ser = SalaryRecordSerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=400)


class SalaryMarkPaidView(APIView):
    # Salary is decided exclusively by the Manager (Admin handles staff/EMR
    # creation instead) — restricted to IsManager, not IsAdminOrManager.
    permission_classes = [IsManager]

    def post(self, request, pk):
        # ✅ FIX: previously unscoped — a branch Manager could mark
        # another branch's salary record as paid.
        qs = _scope_dual_staff_qs(SalaryRecord.objects.all(), request.user)
        obj = get_object_or_404(qs, pk=pk)
        obj.is_paid = True
        obj.paid_at = timezone.now()
        obj.save(update_fields=["is_paid", "paid_at", "updated_at"])
        return Response({"message": "Salary marked as paid."})


class SalaryGenerateView(APIView):
    """
    POST /api/manager/salary/generate/

    MANUAL-ENTRY WORKFLOW — does NOT calculate or fetch any salary amount.
    It only creates a blank (net_salary=0, is_paid=False) row for every
    active staff member (doctors/managers/all StaffProfile roles EXCEPT
    Lab Technician, plus SupportStaff) for the given calendar month, if one
    doesn't already exist, so the manager has a ready sheet to add
    Salary/Bonus/Deduction entries to. Existing rows are left untouched.

    Lab Technicians are excluded on purpose: they're only issued login
    credentials to mark attendance and record their work — the hospital
    does not pay their salary through this system — so no row is ever
    generated for them here.

    Attendance counts for the month are copied in purely as read-only,
    informational context; they never drive net_salary. Doctor pay is
    never read from administration.StaffProfile.salary — that field is
    only ever used to derive the *reference_rate* the serializer exposes,
    never written back into net_salary.

    Body: {"month": 6, "year": 2025}

    A staff member can have at most one row per calendar month —
    uniqueness is keyed on (month, year, staff).
    """
    # Salary is decided exclusively by the Manager (Admin handles staff/EMR
    # creation instead) — restricted to IsManager, not IsAdminOrManager.
    permission_classes = [IsManager]

    def post(self, request):
        try:
            month = int(request.data["month"])
            year  = int(request.data["year"])
        except (KeyError, ValueError, TypeError):
            return Response({"error": "month and year are required integers."}, status=400)
        if not (1 <= month <= 12):
            return Response({"error": "month must be between 1 and 12."}, status=400)
        _, last_day = calendar.monthrange(year, month)
        start = date(year, month, 1)
        end   = date(year, month, last_day)

        created, skipped = 0, 0
        errors = []

        # ✅ FIX: previously unscoped — a branch Manager could generate
        # (and thus see/create) blank salary rows for every branch's
        # active staff in one call, not just their own branch.
        staff_qs = scope_queryset_to_branch(
            StaffProfile.objects.filter(is_active=True).exclude(role="Lab Technician").select_related("user"),
            request.user, branch_field="branch", branch_id=request.data.get("branch"),
        )
        support_qs = scope_queryset_to_branch(
            SupportStaff.objects.filter(is_active=True),
            request.user, branch_field="branch", branch_id=request.data.get("branch"),
        )

        for sp in staff_qs:
            try:
                if SalaryRecord.objects.filter(month=month, year=year, staff_profile=sp).exists():
                    skipped += 1
                    continue
                att = Attendance.objects.filter(date__range=(start, end), staff_profile=sp)
                counts = _count_attendance(att)
                SalaryRecord.objects.create(
                    month=month, year=year,
                    staff_profile=sp, support_staff=None,
                    net_salary=Decimal("0.00"),
                    **counts,
                )
                created += 1
            except Exception as e:
                logger.warning("Salary sheet row failed for staff %s: %s", sp, e)
                errors.append({"staff": str(sp), "error": _safe_detail(e)})

        for ss in support_qs:
            try:
                if SalaryRecord.objects.filter(month=month, year=year, support_staff=ss).exists():
                    skipped += 1
                    continue
                att = Attendance.objects.filter(date__range=(start, end), support_staff=ss)
                counts = _count_attendance(att)
                SalaryRecord.objects.create(
                    month=month, year=year,
                    support_staff=ss, staff_profile=None,
                    net_salary=Decimal("0.00"),
                    **counts,
                )
                created += 1
            except Exception as e:
                logger.warning("Salary sheet row failed for support staff %s: %s", ss, e)
                errors.append({"staff": str(ss), "error": _safe_detail(e)})

        period_label = f"{calendar.month_name[month]} {year}"
        message = f"Sheet ready for {period_label}: {created} new row(s) added, {skipped} already existed. Add Salary/Bonus/Deduction entries to build up each total."
        if errors:
            message += f" ⚠️ {len(errors)} staff row(s) could not be created — see errors below."
        return Response({
            "message": message,
            "created": created,
            "skipped": skipped,
            "errors":  errors,
            "month": month,
            "year":  year,
        })


class SalaryDeleteView(APIView):
    """
    DELETE /api/manager/salary/<pk>/delete/

    Allows a manager to remove a salary record (e.g. generated in error).
    Salary is decided exclusively by the Manager (Admin handles staff/EMR
    creation instead) — restricted to IsManager, not IsAdminOrManager.
    """
    permission_classes = [IsManager]

    def delete(self, request, pk):
        # ✅ FIX: previously unscoped — a branch Manager could delete
        # another branch's salary record just by guessing its id.
        qs = _scope_dual_staff_qs(SalaryRecord.objects.all(), request.user)
        try:
            salary = qs.get(pk=pk)
        except SalaryRecord.DoesNotExist:
            return Response({"detail": "Salary record not found."},
                            status=status.HTTP_404_NOT_FOUND)
        salary.delete()
        return Response({"detail": f"Salary record {pk} deleted."},
                        status=status.HTTP_204_NO_CONTENT)


class SalaryEntryListCreateView(APIView):
    """
    GET  /api/manager/salary/<pk>/entries/   — list this record's pay entries
    POST /api/manager/salary/<pk>/entries/   — add a Salary, Bonus, or
                                                Deduction entry; the record's
                                                net_salary/bonus/deductions
                                                totals are recalculated
                                                automatically so the manager
                                                builds up pay item by item
                                                instead of typing one final
                                                figure.

    Salary is decided exclusively by the Manager — restricted to IsManager.
    """
    permission_classes = [IsManager]

    def get(self, request, pk):
        # ✅ FIX: previously unscoped — a branch Manager could view
        # another branch's salary entries just by guessing the record id.
        qs = _scope_dual_staff_qs(SalaryRecord.objects.all(), request.user)
        salary = get_object_or_404(qs, pk=pk)
        entries = salary.entries.all()
        return Response(SalaryEntrySerializer(entries, many=True).data)

    def post(self, request, pk):
        qs = _scope_dual_staff_qs(SalaryRecord.objects.all(), request.user)
        salary = get_object_or_404(qs, pk=pk)
        if salary.is_paid:
            return Response(
                {"error": "Cannot add an entry — this salary record is already marked paid."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser = SalaryEntrySerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                entry = SalaryEntry(salary_record=salary, **ser.validated_data)
                entry.full_clean()
                entry.save()
                # Lock the row and bump the relevant totals atomically so
                # concurrent "add entry" clicks can't race and drop an amount.
                locked = SalaryRecord.objects.select_for_update().get(pk=salary.pk)
                if entry.entry_type == "Salary":
                    locked.net_salary = F("net_salary") + entry.amount
                elif entry.entry_type == "Bonus":
                    locked.net_salary = F("net_salary") + entry.amount
                    locked.bonus = F("bonus") + entry.amount
                else:  # Deduction
                    locked.net_salary = F("net_salary") - entry.amount
                    locked.deductions = F("deductions") + entry.amount
                locked.save(update_fields=["net_salary", "bonus", "deductions", "updated_at"])
                locked.refresh_from_db()
                if locked.net_salary < 0:
                    locked.net_salary = Decimal("0.00")
                    locked.save(update_fields=["net_salary", "updated_at"])
        except DjangoValidationError as e:
            return Response({"error": "; ".join(e.messages) if hasattr(e, "messages") else _safe_detail(e)},
                            status=status.HTTP_400_BAD_REQUEST)

        salary.refresh_from_db()
        return Response({
            "entry": SalaryEntrySerializer(entry).data,
            "net_salary": salary.net_salary,
            "bonus": salary.bonus,
            "deductions": salary.deductions,
        }, status=status.HTTP_201_CREATED)


class SalaryEntryDeleteView(APIView):
    """
    DELETE /api/manager/salary/<pk>/entries/<int:entry_id>/

    Removes a single pay entry and reverses its effect on the record's
    net_salary/bonus/deductions totals, keeping them consistent.
    """
    permission_classes = [IsManager]

    def delete(self, request, pk, entry_id):
        # ✅ FIX: previously unscoped — a branch Manager could delete
        # another branch's salary entry just by guessing the record id.
        qs = _scope_dual_staff_qs(SalaryRecord.objects.all(), request.user)
        salary = get_object_or_404(qs, pk=pk)
        entry = get_object_or_404(SalaryEntry, pk=entry_id, salary_record=salary)

        if salary.is_paid:
            return Response(
                {"error": "Cannot remove an entry — this salary record is already marked paid."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            amount = entry.amount
            entry_type = entry.entry_type
            entry.delete()
            locked = SalaryRecord.objects.select_for_update().get(pk=salary.pk)
            # max(0, ...) guards against ever going negative from rounding
            # or out-of-order deletes.
            if entry_type == "Salary":
                locked.net_salary = F("net_salary") - amount
            elif entry_type == "Bonus":
                locked.net_salary = F("net_salary") - amount
                locked.bonus = F("bonus") - amount
            else:  # Deduction
                locked.net_salary = F("net_salary") + amount
                locked.deductions = F("deductions") - amount
            locked.save(update_fields=["net_salary", "bonus", "deductions", "updated_at"])
            locked.refresh_from_db()
            fixups = {}
            if locked.net_salary < 0:
                fixups["net_salary"] = Decimal("0.00")
            if locked.bonus < 0:
                fixups["bonus"] = Decimal("0.00")
            if locked.deductions < 0:
                fixups["deductions"] = Decimal("0.00")
            if fixups:
                for field, val in fixups.items():
                    setattr(locked, field, val)
                locked.save(update_fields=[*fixups.keys(), "updated_at"])

        return Response({"detail": "Entry removed."}, status=status.HTTP_204_NO_CONTENT)


def _count_attendance(qs):
    """Return dict of attendance counts from an Attendance queryset."""
    present = absent = paid_leave = unpaid_leave = half_day = 0
    for att in qs:
        s = att.status
        if s == "Present":         present      += 1
        elif s == "Absent":        absent        += 1
        elif s == "Paid Leave":    paid_leave    += 1
        elif s == "Unpaid Leave":  unpaid_leave  += 1
        elif s == "Half Day":      half_day      += 1
        elif s == "Holiday":       pass           # FIX 3: holidays don't affect salary; explicit branch prevents any future else-fallthrough counting them as absent
    return {
        "present_days":      present,
        "absent_days":       absent,
        "paid_leave_days":   paid_leave,
        "unpaid_leave_days": unpaid_leave,
        "half_days":         half_day,
    }


# ═══════════════════════════════════════════════════════
# EXPENSES
# ═══════════════════════════════════════════════════════
class ExpenseListView(APIView):
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        today = timezone.localdate()
        try:
            start = date.fromisoformat(request.query_params.get("start", str(today.replace(day=1))))
            end   = date.fromisoformat(request.query_params.get("end",   str(today)))
        except ValueError:
            return Response({"error": "Invalid date."}, status=400)

        # ✅ FIX: previously unscoped — a branch Admin/Manager could see
        # every branch's expenses here.
        qs = scope_queryset_to_branch(
            HospitalExpense.objects.filter(date__range=(start, end)),
            request.user, branch_field="branch", branch_id=request.query_params.get("branch"),
        )

        cat = request.query_params.get("category")
        if cat:
            qs = qs.filter(category=cat)

        total = qs.aggregate(t=Sum("amount"))["t"] or Decimal("0")

        return Response({
            "total":    float(total),
            "expenses": HospitalExpenseSerializer(qs, many=True).data,
        })

    def post(self, request):
        # ✅ FIX: branch is a required model field but was never resolved
        # here — an ordinary user is always pinned to their own branch; a
        # group admin must pass branch explicitly.
        branch, error = resolve_branch_for_write(request, required=True)
        if error:
            return error
        data = request.data.copy()
        data["branch"] = getattr(branch, "pk", branch)

        ser = HospitalExpenseSerializer(data=data)
        if ser.is_valid():
            ser.save(added_by=request.user)
            return Response({"message": "Expense recorded.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class ExpenseDetailView(APIView):
    permission_classes = [IsAdminOrManager]

    def _get(self, request, pk):
        # ✅ FIX: previously unscoped — a branch Admin/Manager could view
        # or edit another branch's expense just by guessing its id.
        qs = scope_queryset_to_branch(HospitalExpense.objects.all(), request.user, branch_field="branch")
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):
        return Response(HospitalExpenseSerializer(self._get(request, pk)).data)

    def patch(self, request, pk):
        obj = self._get(request, pk)
        data = request.data
        if "branch" in data and not is_group_admin_user(request.user):
            data = data.copy()
            data.pop("branch", None)
        ser = HospitalExpenseSerializer(obj, data=data, partial=True)
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        self._get(request, pk).delete()
        return Response({"message": "Expense deleted."})


# ═══════════════════════════════════════════════════════
# OTHER INCOME (Lab Commission, Donations, etc.)
# ═══════════════════════════════════════════════════════
class IncomeListView(APIView):
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        today = timezone.localdate()
        try:
            start = date.fromisoformat(request.query_params.get("start", str(today.replace(day=1))))
            end   = date.fromisoformat(request.query_params.get("end",   str(today)))
        except ValueError:
            return Response({"error": "Invalid date."}, status=400)

        # ✅ FIX: previously unscoped — a branch Admin/Manager could see
        # every branch's income here.
        qs = scope_queryset_to_branch(
            OtherIncome.objects.filter(date__range=(start, end)),
            request.user, branch_field="branch", branch_id=request.query_params.get("branch"),
        )

        cat = request.query_params.get("category")
        if cat:
            qs = qs.filter(category=cat)

        total = qs.aggregate(t=Sum("amount"))["t"] or Decimal("0")

        return Response({
            "total":  float(total),
            "income": OtherIncomeSerializer(qs, many=True).data,
        })

    def post(self, request):
        # ✅ FIX: branch is a required model field but was never resolved
        # here — an ordinary user is always pinned to their own branch; a
        # group admin must pass branch explicitly.
        branch, error = resolve_branch_for_write(request, required=True)
        if error:
            return error
        data = request.data.copy()
        data["branch"] = getattr(branch, "pk", branch)

        ser = OtherIncomeSerializer(data=data)
        if ser.is_valid():
            ser.save(added_by=request.user)
            return Response({"message": "Income recorded.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class IncomeDetailView(APIView):
    permission_classes = [IsAdminOrManager]

    def _get(self, request, pk):
        # ✅ FIX: previously unscoped — a branch Admin/Manager could view
        # or edit another branch's income row just by guessing its id.
        qs = scope_queryset_to_branch(OtherIncome.objects.all(), request.user, branch_field="branch")
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):
        return Response(OtherIncomeSerializer(self._get(request, pk)).data)

    def patch(self, request, pk):
        obj = self._get(request, pk)
        data = request.data
        if "branch" in data and not is_group_admin_user(request.user):
            data = data.copy()
            data.pop("branch", None)
        ser = OtherIncomeSerializer(obj, data=data, partial=True)
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        self._get(request, pk).delete()
        return Response({"message": "Income deleted."})


# ═══════════════════════════════════════════════════════
# HOME VISIT FEE DEFAULTS
# ═══════════════════════════════════════════════════════
class HomeVisitFeeSettingsView(APIView):
    """
    GET  — anyone in (admin, manager, receptionist) can read the current
           defaults, so reception's BillingPage can pre-fill a new
           Home Visit bill, and preview the one-time MRD registration
           fee before generating any new-patient bill.
    PATCH — admin/manager only, to change the Home Visit defaults.
           mrd_registration_fee is READ-ONLY here regardless of role —
           it's only ever writable via the admin-only settings endpoint
           below; a receptionist can see it, not set it.

    Lives on the same HospitalSettings singleton row as
    mrd_registration_fee (administration app), but is deliberately a
    separate endpoint with its own permission scope — the existing
    admin-only settings endpoint for mrd_registration_fee (its write
    path) is untouched.

    Branch resolution uses the same _resolve_dashboard_branch helper as
    the Finance section: an ordinary branch-scoped user is always pinned
    to their own StaffProfile.branch, and a group admin must pass
    ?branch=<id> to say which branch's defaults they mean.

    ✅ FIX: previously read request.user.staff_profile.branch directly,
    which raised an unhandled 500 for a true group admin — either a bare
    Django superuser with no StaffProfile row at all (staff_profile is a
    reverse one-to-one accessor that raises RelatedObjectDoesNotExist,
    not None), or a promoted is_group_admin account whose StaffProfile is
    branch-less by design (see get_user_branch's docstring), since
    HospitalSettings.get(None) fails full_clean() on the required branch
    field.
    """
    permission_classes = [IsAdminOrManagerOrReceptionistRead]

    def get(self, request):
        branch, error = _resolve_dashboard_branch(request)
        if error:
            return error
        if branch is None:
            return Response(
                {"error": "Group admin must specify ?branch=<id> to view a branch's home visit fee defaults."},
                status=400,
            )
        settings_obj = HospitalSettings.get(branch)
        return Response({
            "default_home_visit_fee":           float(settings_obj.default_home_visit_fee),
            "default_home_visit_travel_charge": float(settings_obj.default_home_visit_travel_charge),
            "mrd_registration_fee":              float(settings_obj.mrd_registration_fee),
        })

    def patch(self, request):
        branch, error = _resolve_dashboard_branch(request)
        if error:
            return error
        if branch is None:
            return Response(
                {"error": "Group admin must specify ?branch=<id> to update a branch's home visit fee defaults."},
                status=400,
            )
        settings_obj = HospitalSettings.get(branch)
        fee     = request.data.get("default_home_visit_fee")
        travel  = request.data.get("default_home_visit_travel_charge")

        if fee is not None:
            try:
                fee = Decimal(str(fee))
            except Exception:
                return Response({"default_home_visit_fee": "Must be a valid number."}, status=400)
            if fee < 0:
                return Response({"default_home_visit_fee": "Cannot be negative."}, status=400)
            settings_obj.default_home_visit_fee = fee

        if travel is not None:
            try:
                travel = Decimal(str(travel))
            except Exception:
                return Response({"default_home_visit_travel_charge": "Must be a valid number."}, status=400)
            if travel < 0:
                return Response({"default_home_visit_travel_charge": "Cannot be negative."}, status=400)
            settings_obj.default_home_visit_travel_charge = travel

        settings_obj.save()
        return Response({
            "message": "Home visit fee settings updated.",
            "default_home_visit_fee":           float(settings_obj.default_home_visit_fee),
            "default_home_visit_travel_charge": float(settings_obj.default_home_visit_travel_charge),
        })


# ═══════════════════════════════════════════════════════
# DEALERS + CREDIT LEDGER
# ═══════════════════════════════════════════════════════
class DealerListCreateView(APIView):
    """
    GET  /api/manager/dealers/?search=&active=&deals_in=
    POST /api/manager/dealers/

    Admin/Manager: full access. Pharmacist: read-only (needs the list to
    pick a dealer when logging stock or a return) — enforced by
    IsAdminOrManagerOrPharmacistRead.
    """
    permission_classes = [IsAdminOrManagerOrPharmacistRead]

    def get(self, request):
        # ✅ FIX: previously unscoped — a branch Admin/Manager/Pharmacist
        # could see every branch's dealer list here.
        qs = scope_queryset_to_branch(
            Dealer.objects.all(), request.user,
            branch_field="branch", branch_id=request.query_params.get("branch"),
        )

        search = request.query_params.get("search")
        if search:
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(contact_person__icontains=search) |
                Q(phone__icontains=search) |
                Q(gst_number__icontains=search)
            )

        active = request.query_params.get("active")
        if active is not None:
            qs = qs.filter(is_active=(active.lower() == "true"))
        else:
            qs = qs.filter(is_active=True)  # default: hide deactivated dealers

        deals_in = request.query_params.get("deals_in")
        if deals_in:
            # deals_in may now be stored as an exact single code, a combo
            # like "MEDICINE,SUPPLY", or the legacy "BOTH" (= all three).
            # icontains is safe here since none of MEDICINE/SUPPLY/GENERAL
            # is a substring of another.
            qs = qs.filter(
                Q(deals_in__icontains=deals_in) | Q(deals_in="BOTH")
            )

        return Response(DealerSerializer(qs, many=True).data)

    def post(self, request):
        # ✅ FIX: branch is a required model field but was never resolved
        # here — an ordinary user is always pinned to their own branch; a
        # group admin must pass branch explicitly.
        branch, error = resolve_branch_for_write(request, required=True)
        if error:
            return error
        data = request.data.copy()
        data["branch"] = getattr(branch, "pk", branch)

        ser = DealerSerializer(data=data)
        if ser.is_valid():
            ser.save(added_by=request.user)
            return Response({"message": "Dealer added.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class DealerDetailView(APIView):
    """
    GET/PATCH  /api/manager/dealers/<pk>/     — includes recent ledger entries
    DELETE     /api/manager/dealers/<pk>/     — soft-deactivate only (a
                                                 dealer with ledger history
                                                 is never hard-deleted)
    """
    permission_classes = [IsAdminOrManagerOrPharmacistRead]

    def _get(self, request, pk):
        # ✅ FIX: previously unscoped — a branch Admin/Manager/Pharmacist
        # could view or edit another branch's dealer just by guessing its id.
        qs = scope_queryset_to_branch(Dealer.objects.all(), request.user, branch_field="branch")
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):
        dealer = self._get(request, pk)
        data = DealerSerializer(dealer).data
        txns = dealer.transactions.select_related("created_by", "confirmed_by")[:300]
        data["transactions"] = DealerTransactionSerializer(txns, many=True).data
        return Response(data)

    def patch(self, request, pk):
        dealer = self._get(request, pk)
        data = request.data
        if "branch" in data and not is_group_admin_user(request.user):
            data = data.copy()
            data.pop("branch", None)
        ser = DealerSerializer(dealer, data=data, partial=True)
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        dealer = self._get(request, pk)
        dealer.is_active = False
        dealer.save()
        return Response({"message": "Dealer deactivated."})


class DealerTransactionListView(APIView):
    """
    GET  /api/manager/dealers/transactions/?dealer=&status=
    POST /api/manager/dealers/transactions/   — manually log a transaction
         not tied to any batch/return (e.g. a cash payment made to a
         dealer outside of a stock order, or a one-off balance
         adjustment). Manager/Admin only — pharmacists never write here
         directly; their entries come in automatically via stock/return.
    """
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        # ✅ FIX: previously unscoped — a branch Admin/Manager could see
        # every branch's dealer ledger entries here.
        qs = scope_queryset_to_branch(
            DealerTransaction.objects.select_related("dealer", "created_by", "confirmed_by"),
            request.user, branch_field="dealer__branch",
        )

        dealer_id = request.query_params.get("dealer")
        if dealer_id:
            qs = qs.filter(dealer_id=dealer_id)

        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        return Response(DealerTransactionSerializer(qs, many=True).data)

    def post(self, request):
        ser = DealerTransactionSerializer(data=request.data, context={"request": request})
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        # ✅ FIX: reject a cross-branch dealer reference — a branch-scoped
        # user could otherwise log a transaction against another branch's
        # dealer ledger.
        dealer = ser.validated_data["dealer"]
        if not _user_in_branch(request.user, dealer.branch_id):
            return Response(
                {"error": "You can only log transactions for dealers in your own branch."},
                status=403,
            )
        txn = ser.save(status="PENDING", created_by=request.user)
        # A manually logged entry that links back to an earlier row (e.g.
        # the "Pay Now" / "Collect Refund" actions on the Dealers page,
        # which create a PAYMENT/CASH_REFUND linked to the original
        # PURCHASE/CREDIT_NOTE) should inherit that origin's source_model
        # and source_id instead of sitting as source_model="MANUAL" (the
        # model default). Without this, a manually-collected cash refund
        # for a medicine/supply return would silently fall outside the
        # Finance Dashboard's medicine/supply refund totals, which key off
        # source_model — even though real cash for a real return was
        # actually collected. Only fills it in if the client didn't
        # already set an explicit source_model of its own.
        if txn.linked_transaction_id and txn.source_model == "MANUAL":
            origin = txn.linked_transaction
            if origin.source_model != "MANUAL":
                txn.source_model = origin.source_model
                txn.source_id = origin.source_id
                txn.save(update_fields=["source_model", "source_id"])
        return Response(
            {"message": "Transaction logged.", "data": DealerTransactionSerializer(txn).data},
            status=201,
        )


def _sync_linked_return_status(txn, outcome, user):
    """
    When a CREDIT_NOTE dealer transaction that was auto-created from a
    medicine return gets finalized, push the same outcome onto the
    underlying return record's OWN workflow status (MedicineReturnToProvider).
    That status used to sit at "Return Requested" forever (its own
    approve/complete endpoints are never called by the frontend), even
    after the manager had fully confirmed the refund on the Dealers page.
    That's what made the Expenses page and the Dealers page disagree about
    the same return. Deliberately a local/deferred import (not at module
    load time) so manager never has a hard import-time dependency on
    pharmacist — matches the pattern already used in PurchasesAndRefundsView.
    Best-effort: never blocks the ledger finalize if the source row is
    missing or was hard-deleted.

    ✅ FIX: this used to also try `SupplyReturn.objects.filter(...).update(
    status=..., approved_by=user)` — but SupplyReturn (and GeneralItemReturn,
    same shape) has never had a `status`/`approved_by` field, so that call
    raised FieldError every single time a supply-return credit note was
    confirmed or rejected. The broad except below silently swallowed it,
    so nothing broke visibly, but it also silently accomplished nothing.
    Supply/General-item returns don't carry a separate approval workflow —
    their settlement state is tracked entirely on the DealerTransaction
    (see settlement_status via _dealer_settlement_map) — so there's nothing
    to sync for them here; only MEDICINE_RETURN needs this push.
    """
    if txn.transaction_type != "CREDIT_NOTE" or not txn.source_id:
        return
    new_status = "COMPLETED" if outcome == "CONFIRMED" else "REJECTED"
    try:
        if txn.source_model == "MEDICINE_RETURN":
            from pharmacist.models import MedicineReturnToProvider
            MedicineReturnToProvider.objects.filter(pk=txn.source_id).update(
                status=new_status, approved_by=user,
            )
    except Exception:
        logger.exception("Failed to sync return status for DealerTransaction #%s", txn.transaction_id)


def _sync_batch_approval_status(txn, outcome, user):
    """
    When a PURCHASE DealerTransaction is finalized (or later voided), push
    the manager's decision onto the batch it came from. A dealer-linked
    batch is created PENDING_APPROVAL — not sellable/dispensable, excluded
    from every stock-availability query — until the manager reviews this
    transaction on the Dealers page:

      • CONFIRMED → the manager has signed off on the purchase, so the
        batch becomes ACTIVE (or DEPLETED if it's already down to zero)
        and is now sellable. This is the "allow to sell" step.
      • REJECTED  → the manager has disowned the purchase entirely. The
        batch is marked REJECTED and whatever quantity is STILL
        PHYSICALLY IN STOCK is zeroed out — i.e. returned to the dealer.
        Quantity already dispensed to a patient before the rejection is
        untouched (it's already gone and can't be clawed back — `quantity`
        only ever tracks what's still on the shelf). Quantity currently
        allocated to an in-progress bill is left alone too, rather than
        pulled out from under a sale that's already underway.

    No-op for anything that isn't a batch-sourced PURCHASE (e.g. a
    CREDIT_NOTE from a return — see _sync_linked_return_status instead —
    or a MANUAL entry with no batch). Deliberately a local/deferred
    import, same reasoning as _sync_linked_return_status. Best-effort:
    never blocks the ledger finalize/void if the batch is missing.
    """
    if txn.transaction_type != "PURCHASE" or not txn.source_id:
        return
    try:
        if txn.source_model == "MEDICINE_BATCH":
            from pharmacist.models import MedicineBatch, MedicineStockLog
            batch = MedicineBatch.objects.select_for_update().filter(pk=txn.source_id).first()
            if not batch:
                return
            if outcome == "CONFIRMED":
                if batch.status == "PENDING_APPROVAL":
                    batch.status = "DEPLETED" if batch.quantity == 0 else "ACTIVE"
                    batch.save(update_fields=["status"])
            elif batch.status in ("PENDING_APPROVAL", "ACTIVE"):
                keep = min(batch.quantity, batch.allocated_quantity)
                removed = batch.quantity - keep
                batch.quantity = keep
                batch.status = "REJECTED"
                batch.save(update_fields=["quantity", "status"])
                if removed:
                    MedicineStockLog.objects.create(
                        batch=batch, change_type="RETURN", quantity_changed=-removed,
                        remarks=f"Purchase rejected by manager — {removed} unit(s) returned to "
                                f"dealer (transaction #{txn.transaction_id}).",
                    )

        elif txn.source_model == "SUPPLY_BATCH":
            from pharmacist.models import SupplyBatch
            batch = SupplyBatch.objects.select_for_update().filter(pk=txn.source_id).first()
            if not batch:
                return
            if outcome == "CONFIRMED":
                if batch.status == "PENDING_APPROVAL":
                    batch.status = "DEPLETED" if batch.quantity == 0 else "ACTIVE"
                    batch.save(update_fields=["status"])
            elif batch.status in ("PENDING_APPROVAL", "ACTIVE"):
                # SupplyBatch has no allocated_quantity concept (no
                # bill-time reservation step) — the full remaining
                # quantity is returned.
                removed = batch.quantity
                batch.quantity = 0
                batch.status = "REJECTED"
                batch.save(update_fields=["quantity", "status"])

        elif txn.source_model == "GENERAL_ITEM_BATCH":
            from pharmacist.models import GeneralItemBatch, GeneralItemStockLog
            batch = GeneralItemBatch.objects.select_for_update().filter(pk=txn.source_id).first()
            if not batch:
                return
            if outcome == "CONFIRMED":
                if batch.status == "PENDING_APPROVAL":
                    batch.status = "DEPLETED" if batch.quantity == 0 else "ACTIVE"
                    batch.save(update_fields=["status"])
            elif batch.status in ("PENDING_APPROVAL", "ACTIVE"):
                keep = min(batch.quantity, batch.allocated_quantity)
                removed = batch.quantity - keep
                batch.quantity = keep
                batch.status = "REJECTED"
                batch.save(update_fields=["quantity", "status"])
                if removed:
                    GeneralItemStockLog.objects.create(
                        batch=batch, change_type="RETURN", quantity_changed=-removed,
                        remarks=f"Purchase rejected by manager — {removed} unit(s) returned to "
                                f"dealer (transaction #{txn.transaction_id}).",
                    )
    except Exception:
        logger.exception("Failed to sync batch approval status for DealerTransaction #%s", txn.transaction_id)


def _finalize_dealer_transaction(txn, data, user):
    """
    Shared core for confirming/rejecting ONE PENDING DealerTransaction.
    Used by both the single-transaction and the bulk finalize endpoints so
    the two can never drift out of sync with each other. `txn` must already
    be locked (select_for_update) and `data` is a validated_data dict from
    either DealerTransactionFinalizeSerializer or
    DealerTransactionBulkFinalizeSerializer (bulk strips 'amount' and
    'linked_transaction_id' before calling this, so each row keeps its own
    original amount and its own independently auto-paired settlement leg).

    Returns (txn, auto_payment_or_None). Raises ValueError on a bad state
    — the caller turns that into the appropriate HTTP response.
    """
    if txn.status != "PENDING":
        raise ValueError(f"Cannot finalize: status is already {txn.status}.")

    if data["action"] == "REJECT":
        txn.status = "REJECTED"
        txn.confirmed_by = user
        txn.confirmed_at = timezone.now()
        if data.get("notes"):
            txn.notes = data["notes"]
        txn.save()
        _sync_linked_return_status(txn, "REJECTED", user)
        _sync_batch_approval_status(txn, "REJECTED", user)
        return txn, None

    # ── CONFIRM ──────────────────────────────────────────────────
    # transaction_type is intentionally NEVER updated here — see
    # DealerTransactionFinalizeSerializer docstring. Only settlement
    # details are editable; the underlying nature of the event
    # (purchase / credit note / etc.) stays exactly as it was created.
    if "settlement_method" in data:
        txn.settlement_method = data["settlement_method"]
    if "amount" in data:
        txn.amount = data["amount"]
    if "reference_number" in data:
        txn.reference_number = data["reference_number"]
    if "due_date" in data:
        txn.due_date = data["due_date"]
    if "notes" in data:
        txn.notes = data["notes"]
    if data.get("linked_transaction_id"):
        linked = get_object_or_404(
            DealerTransaction, pk=data["linked_transaction_id"], dealer=txn.dealer
        )
        txn.linked_transaction = linked

    # The settlement method as actually chosen this round (PAID / REFUND /
    # CREDIT / EXCHANGE) — captured before a partial payment below may
    # flip txn.settlement_method back to CREDIT for the *remaining*
    # balance. This is what decides whether a PAYMENT/CASH_REFUND leg
    # gets auto-paired at all, further down.
    chosen_settlement_method = txn.settlement_method
    PAID_NOW_METHODS = {"PAID", "REFUND"}

    # ✅ FIX: `amount` here corrects the TRUE value of the purchase/return
    # itself (e.g. the dealer's invoice was actually ₹140, not ₹150) — see
    # the serializer docstring. It was previously also being reused as
    # "how much I'm paying today", which meant confirming a ₹150 purchase
    # as Paid Now with amount=₹100 silently rewrote the purchase down to
    # ₹100 *and* auto-logged a full ₹100 payment against it — netting to
    # ₹0 and making the ledger report "settled, no balance owed" even
    # though ₹50 was still genuinely outstanding. `paid_amount` is the
    # explicit, unambiguous field for that instead: how much is actually
    # changing hands right now. Left unset (or equal to `amount`), nothing
    # changes from the original behaviour — full settlement.
    paid_amount = data.get("paid_amount")
    if paid_amount is not None and paid_amount > txn.amount:
        raise ValueError(
            f"Paid amount (\u20b9{paid_amount}) can't exceed this transaction's amount (\u20b9{txn.amount})."
        )
    is_partial = (
        chosen_settlement_method in PAID_NOW_METHODS
        and paid_amount is not None
        and paid_amount < txn.amount
    )
    settle_amount = paid_amount if is_partial else txn.amount

    if is_partial:
        # Only part of this purchase/return is being settled right now.
        # Its true amount (corrected above, if at all) is left exactly as
        # it is — a partial payment is not evidence the purchase cost
        # less, so it must never quietly rewrite what was actually bought
        # or returned. The entry is left open (CREDIT) for the remainder
        # instead of being marked PAID/REFUND while money is still owed —
        # so it keeps showing up in the Dealers page's "Due" list — and
        # the note keeps a clear record since settlement_method alone can
        # no longer say "paid in full".
        remaining = (txn.amount - settle_amount).quantize(Decimal("0.01"))
        partial_note = (
            f"[Partially settled: \u20b9{settle_amount} paid now of \u20b9{txn.amount} "
            f"\u2014 \u20b9{remaining} still outstanding.]"
        )
        txn.notes = f"{txn.notes.strip() + ' ' if txn.notes else ''}{partial_note}"
        txn.settlement_method = "CREDIT"

    txn.status = "CONFIRMED"
    txn.confirmed_by = user
    txn.confirmed_at = timezone.now()
    txn.save()
    # Snapshot the running balance now that this row counts.
    txn.balance_after = txn.dealer.balance
    txn.save(update_fields=["balance_after"])

    _sync_linked_return_status(txn, "CONFIRMED", user)
    _sync_batch_approval_status(txn, "CONFIRMED", user)

    # Auto-pair a matching settlement leg so the event nets to zero (or,
    # for a partial payment, nets down by exactly what was actually paid)
    # on the ledger while keeping both halves on record:
    #   PURCHASE   confirmed as PAID   → auto-log a PAYMENT
    #   CREDIT_NOTE confirmed as REFUND → auto-log a CASH_REFUND
    # A CREDIT_NOTE (from a return) confirmed with settlement_method=CREDIT
    # (i.e. "pending — pay later") deliberately does NOT auto-pair — it
    # stays open on the ledger, optionally with a due_date, until whoever
    # settles it later confirms that leg for real. `chosen_settlement_method`
    # (captured above, before a partial payment flips txn.settlement_method
    # back to CREDIT for the open remainder) is what decides this — the
    # manager still chose "Paid now", they just haven't paid all of it yet.
    AUTO_SETTLE_PAIRS = {
        ("PURCHASE", "PAID"):    "PAYMENT",
        ("CREDIT_NOTE", "REFUND"): "CASH_REFUND",
    }
    paired_type = AUTO_SETTLE_PAIRS.get((txn.transaction_type, chosen_settlement_method))

    auto_payment = None
    if paired_type and data.get("auto_settle_payment", True):
        paired = DealerTransaction.objects.create(
            dealer=txn.dealer,
            transaction_type=paired_type,
            settlement_method=chosen_settlement_method,
            amount=settle_amount,
            status="CONFIRMED",
            source_model=txn.source_model,
            source_id=txn.source_id,
            linked_transaction=txn,
            reference_number=txn.reference_number,
            notes=f"Auto-logged: matching {paired_type.replace('_', ' ').lower()} for #{txn.transaction_id}."
                  + (f" (partial — \u20b9{remaining} still due)" if is_partial else ""),
            created_by=user,
            confirmed_by=user,
            confirmed_at=timezone.now(),
        )
        paired.balance_after = txn.dealer.balance
        paired.save(update_fields=["balance_after"])
        auto_payment = paired

    return txn, auto_payment


class DealerTransactionFinalizeView(APIView):
    """
    POST /api/manager/dealers/transactions/<pk>/finalize/

    Manager-only. Confirms or rejects a PENDING dealer transaction that
    was auto-created when a pharmacist linked a batch/return to a dealer
    (or manually logged above). On CONFIRM, the manager has the final say
    on settlement_method/amount/due_date (pre-filled from what the
    pharmacist requested, but editable — e.g. if the dealer only partially
    honours a return, or pays late). transaction_type is fixed and NOT
    editable here — see DealerTransactionFinalizeSerializer for why.

    If a PURCHASE is confirmed with settlement_method=PAID, a matching
    PAYMENT row is auto-logged alongside it. If a CREDIT_NOTE (from a
    return) is confirmed with settlement_method=REFUND, a matching
    CASH_REFUND row is auto-logged the same way (see auto_settle_payment),
    so the ledger keeps both halves of each event on record while netting
    to zero. If the source was a medicine/supply return, its own workflow
    status is synced to COMPLETED/REJECTED too — see
    _sync_linked_return_status.
    """
    permission_classes = [IsAdminOrManager]

    @transaction.atomic
    def post(self, request, pk):
        # ✅ FIX: previously unscoped — a branch Admin/Manager could
        # finalize another branch's dealer transaction just by guessing its id.
        qs = scope_queryset_to_branch(
            DealerTransaction.objects.select_for_update(), request.user, branch_field="dealer__branch",
        )
        txn = get_object_or_404(qs, pk=pk)
        ser = DealerTransactionFinalizeSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)

        try:
            txn, auto_payment = _finalize_dealer_transaction(txn, ser.validated_data, request.user)
        except ValueError as e:
            return Response({"error": _safe_detail(e)}, status=400)

        response_data = {
            "message": "Transaction rejected." if txn.status == "REJECTED" else "Transaction confirmed.",
            "data": DealerTransactionSerializer(txn).data,
        }
        if auto_payment:
            response_data["auto_payment"] = DealerTransactionSerializer(auto_payment).data
        return Response(response_data)


class DealerTransactionBulkFinalizeView(APIView):
    """
    POST /api/manager/dealers/transactions/bulk-finalize/
    Body: { transaction_ids: [1,2,3], action, settlement_method?, due_date?,
            reference_number?, notes?, auto_settle_payment? }

    Settles a whole batch of one dealer's PENDING purchases/returns in a
    single action — "bill-based" settlement, e.g. every unpaid item from
    this month's order marked Paid together — instead of one at a time.
    Every id must currently be PENDING and belong to the SAME dealer
    (mixing dealers in one bulk call is rejected, so each bulk-settle
    reads cleanly as "this dealer's outstanding batch was closed out").
    Each row keeps its own original amount and gets its own independently
    auto-paired settlement leg via _finalize_dealer_transaction — nothing
    here forces every row to the same amount. Manager/Admin only. Atomic:
    if any row fails, nothing is written.
    """
    permission_classes = [IsAdminOrManager]

    @transaction.atomic
    def post(self, request):
        ser = DealerTransactionBulkFinalizeSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        data = ser.validated_data
        ids = data["transaction_ids"]

        # ✅ FIX: previously unscoped — a branch Admin/Manager could bulk
        # finalize another branch's dealer transactions just by supplying
        # their ids.
        txn_qs = scope_queryset_to_branch(
            DealerTransaction.objects.select_for_update(), request.user, branch_field="dealer__branch",
        )
        txns = list(txn_qs.filter(pk__in=ids))
        found_ids = {t.transaction_id for t in txns}
        missing = [i for i in ids if i not in found_ids]
        if missing:
            return Response({"error": f"Transaction(s) not found: {missing}"}, status=400)

        dealer_ids = {t.dealer_id for t in txns}
        if len(dealer_ids) > 1:
            return Response({"error": "All selected transactions must belong to the same dealer."}, status=400)

        not_pending = [t.transaction_id for t in txns if t.status != "PENDING"]
        if not_pending:
            return Response(
                {"error": f"Already finalized, refresh and retry: {not_pending}"}, status=400
            )

        # Each row keeps its own amount and linked_transaction — those two
        # keys are intentionally absent from this serializer, so nothing
        # to strip here; row_data is the same settlement details for every
        # row in the batch.
        row_data = {"action": data["action"]}
        for key in ("settlement_method", "due_date", "reference_number", "notes", "auto_settle_payment"):
            if key in data:
                row_data[key] = data[key]

        finalized, auto_payments = [], []
        for txn in txns:
            try:
                txn, auto_payment = _finalize_dealer_transaction(txn, row_data, request.user)
            except ValueError as e:
                return Response({"error": f"#{txn.transaction_id}: {e}"}, status=400)
            finalized.append(txn)
            if auto_payment:
                auto_payments.append(auto_payment)

        verb = "confirmed" if data["action"] == "CONFIRM" else "rejected"
        return Response({
            "message": f"{len(finalized)} transaction(s) {verb}.",
            "data": DealerTransactionSerializer(finalized, many=True).data,
            "auto_payments": DealerTransactionSerializer(auto_payments, many=True).data,
        })


class DealerTransactionVoidView(APIView):
    """
    POST /api/manager/dealers/transactions/<pk>/void/
    Body: { reason? }

    Corrects a CONFIRMED transaction that turns out to be wrong — e.g. a
    duplicate or stray manual entry that's throwing the dealer's balance
    off (see the "700 owed to us" case: an unlinked manual PAYMENT with
    no matching purchase). Until now there was no way to walk back a
    CONFIRMED row at all.

    Voiding sets status to REJECTED rather than deleting the row —
    Dealer.balance already only sums CONFIRMED transactions, so a
    REJECTED row drops out of the balance immediately while staying on
    the ledger for audit history (shown with the existing red
    "REJECTED" badge the frontend already renders).

    If this row has its own auto-paired settlement leg (e.g. a PURCHASE
    that auto-created a PAYMENT), that paired leg is voided together with
    it so the pair doesn't end up half-reversed. If this row IS itself a
    paired leg, only it is voided — the original stays untouched.
    Manager/Admin only.
    """
    permission_classes = [IsAdminOrManager]

    @transaction.atomic
    def post(self, request, pk):
        # ✅ FIX: previously unscoped — a branch Admin/Manager could void
        # another branch's confirmed dealer transaction just by guessing its id.
        qs = scope_queryset_to_branch(
            DealerTransaction.objects.select_for_update(), request.user, branch_field="dealer__branch",
        )
        txn = get_object_or_404(qs, pk=pk)
        if txn.status != "CONFIRMED":
            return Response(
                {"error": f"Only CONFIRMED transactions can be voided (this one is {txn.status})."},
                status=400,
            )

        reason = (request.data.get("reason") or "").strip()
        stamp = f"[VOIDED by {request.user.get_username()} on {timezone.now().date()}" + (f": {reason}" if reason else "") + "]"

        def _void(t):
            t.status = "REJECTED"
            t.notes = f"{t.notes.strip() + ' ' if t.notes else ''}{stamp}"
            t.save(update_fields=["status", "notes"])
            # A voided PURCHASE is the manager walking back a purchase they'd
            # previously approved — same treatment as an outright REJECT:
            # whatever's still physically in stock goes back to the dealer.
            _sync_batch_approval_status(t, "REJECTED", request.user)

        voided = [txn]
        _void(txn)
        for child in txn.linked_from.filter(status="CONFIRMED"):
            _void(child)
            voided.append(child)

        return Response({
            "message": f"{len(voided)} transaction(s) voided.",
            "data": DealerTransactionSerializer(voided, many=True).data,
            "new_balance": txn.dealer.balance,
        })


class DealerTransactionScheduleUpdateView(APIView):
    """
    POST /api/manager/dealers/transactions/<pk>/schedule/

    Lets a manager go back and edit the "soft" details of an entry —
    due_date, reference_number, notes — regardless of its status, without
    touching amount, transaction_type, or status. Useful for e.g. pushing
    back a due_date on something already confirmed as credit, or fixing a
    typo'd reference number after the fact. Manager/Admin only.
    """
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        # ✅ FIX: previously unscoped — a branch Admin/Manager could edit
        # another branch's dealer transaction just by guessing its id.
        qs = scope_queryset_to_branch(DealerTransaction.objects.all(), request.user, branch_field="dealer__branch")
        txn = get_object_or_404(qs, pk=pk)
        ser = DealerTransactionScheduleUpdateSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        data = ser.validated_data

        for key in ("due_date", "reference_number", "notes"):
            if key in data:
                setattr(txn, key, data[key])
        txn.save()
        return Response({
            "message": "Transaction details updated.",
            "data": DealerTransactionSerializer(txn).data,
        })
# ═══════════════════════════════════════════════════════
# PUBLIC WEBSITE — manager-side CRUD
# ═══════════════════════════════════════════════════════

def _doctor_branch_write_ok(request, doctor_id):
    """
    Gate for POSTs that attach a new record (website profile, weekly
    availability, availability exception) to an EMR doctor_id.

    A group admin may attach to any doctor. A branch-scoped manager may
    only attach to a doctor whose own StaffProfile.branch matches theirs
    -- mirrors doctor/views.py's _admin_branch_ok, but for the write side
    here (the read side is handled by scope_queryset_to_branch below,
    which already returns an empty/404 result for a cross-branch doctor_id
    without needing this check).

    Returns None on success, or a ready-to-return Response otherwise.
    """
    if is_group_admin_user(request.user):
        return None
    doctor = get_object_or_404(EmrDoctorProfile.objects.select_related("staff"), pk=doctor_id)
    user_branch = get_user_branch(request.user)
    if user_branch is None or doctor.staff.branch_id != user_branch.pk:
        return Response(
            {"errors": {"doctor": "You can only manage doctors in your own branch."}},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


class ManagerEmrDoctorListView(APIView):
    """
    GET /api/manager/website/emr-doctors/?all=true&search=

    Read-only list of EMR doctors (doctor.DoctorProfile), for the
    "which doctor should this website profile / featured YouTube video
    point at" pickers in the Website Management CMS. Deliberately
    separate from /api/administration/doctors/, which is Admin-only
    (AdminOnlyView) — Manager needs read access here without gaining
    the create/attach abilities that endpoint also exposes.

    Branch-scoped: a branch manager should only be picking from their own
    branch's doctors here — a group admin sees every branch.
    """
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        qs = EmrDoctorProfile.objects.select_related("staff", "staff__user", "specialty").order_by("-profile_id")
        qs = scope_queryset_to_branch(qs, request.user, branch_field="staff__branch")

        if request.query_params.get("all", "").lower() != "true":
            qs = qs.filter(staff__is_active=True)

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(staff__user__first_name__icontains=search) |
                Q(staff__user__last_name__icontains=search) |
                Q(staff__staff_code__icontains=search) |
                Q(specialty__name__icontains=search) |
                Q(registration_number__icontains=search)
            )

        return Response(EmrDoctorProfileSerializer(qs, many=True).data)


class WebsiteDoctorListView(APIView):
    """
    GET/POST /api/manager/website/doctors/

    Branch-scoped: a manager only lists/creates website profiles that
    have at least one attached doctor in their own branch (a group admin
    sees/creates for any). A profile already spanning two branches stays
    visible to both branches' managers -- that's intentional, since it's
    now one shared record, not two.

    POST here always creates a brand-new profile for one starting
    DoctorProfile. Adding a *second* branch to an existing doctor's
    profile is WebsiteDoctorAttachBranchView below, not this endpoint --
    keeping them separate means a manager can't accidentally spin up a
    duplicate profile for a doctor who already has one at another branch.
    """
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        qs = DoctorWebsiteProfile.objects.prefetch_related(
            "doctors", "doctors__staff", "doctors__staff__user", "doctors__staff__branch",
        )
        # .distinct() matters here (unlike the old OneToOne version) --
        # scoping through an M2M can otherwise return the same profile
        # once per matching attached doctor.
        qs = scope_queryset_to_branch(qs, request.user, branch_field="doctors__staff__branch").distinct()
        return Response(DoctorWebsiteProfileSerializer(qs, many=True, context={"request": request}).data)

    def post(self, request):
        branch_error = _doctor_branch_write_ok(request, request.data.get("doctor"))
        if branch_error:
            return branch_error
        ser = DoctorWebsiteProfileSerializer(data=request.data, context={"request": request})
        if ser.is_valid():
            ser.save()
            return Response({"message": "Doctor website profile created.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class WebsiteDoctorSearchView(APIView):
    """
    GET /api/manager/website/doctors/search/?registration_number=&name=

    Used by the "add a doctor's second branch" flow: before creating a
    new website profile, the frontend checks whether this real-world
    doctor already has one (via regno, the natural cross-branch key now
    that doctor.DoctorProfile.registration_number is no longer DB-unique
    -- see its model docstring) so the manager can attach to it instead
    of creating a duplicate. Group-admin-and-branch-manager visible, same
    as the rest of Web Management CRUD -- a manager needs to find a
    profile created by a *different* branch's manager to attach to it.
    """
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        registration_number = request.query_params.get("registration_number", "").strip()
        name = request.query_params.get("name", "").strip()
        if not registration_number and not name:
            return Response({"error": "Provide registration_number or name to search."}, status=400)

        qs = DoctorWebsiteProfile.objects.prefetch_related(
            "doctors", "doctors__staff", "doctors__staff__user", "doctors__staff__branch",
        ).distinct()
        if registration_number:
            qs = qs.filter(doctors__registration_number__iexact=registration_number)
        elif name:
            qs = qs.filter(
                Q(doctors__staff__user__first_name__icontains=name)
                | Q(doctors__staff__user__last_name__icontains=name)
            ).distinct()

        return Response(DoctorWebsiteProfileSerializer(qs, many=True, context={"request": request}).data)


class WebsiteDoctorAttachBranchView(APIView):
    """
    POST /api/manager/website/doctors/<pk>/attach-branch/  {"doctor": <DoctorProfile id>}

    Attaches another branch's DoctorProfile to an existing, already-shared
    DoctorWebsiteProfile -- the "no duplication" path for a doctor's
    second (third, ...) branch. Name/photo/regno/specialty/bio/education/
    awards/designation are never re-entered; only that branch's own
    DoctorWeeklyAvailability/DoctorAvailabilityException rows (added
    separately, via the existing per-doctor availability endpoints) make
    that branch's hours show up on the shared public card.

    Write access: a branch manager may only attach a DoctorProfile from
    their own branch (mirrors _doctor_branch_write_ok); a group admin can
    attach any branch. Either way, the *profile* being attached to must
    already be visible to the caller under the normal branch-scoping
    rules above -- a branch manager can't blindly attach to an arbitrary
    pk from another hospital group's data.
    """
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        qs = DoctorWebsiteProfile.objects.all()
        qs = scope_queryset_to_branch(qs, request.user, branch_field="doctors__staff__branch").distinct()
        profile = get_object_or_404(qs, pk=pk)

        doctor_id = request.data.get("doctor")
        branch_error = _doctor_branch_write_ok(request, doctor_id)
        if branch_error:
            return branch_error

        doctor = get_object_or_404(EmrDoctorProfile.objects.select_related("staff"), pk=doctor_id)
        try:
            DoctorWebsiteProfile.validate_doctor_membership(doctor)
        except DjangoValidationError as exc:
            return Response({"error": _safe_detail(exc)}, status=400)

        if profile.doctors.filter(pk=doctor.pk).exists():
            return Response({"error": "This branch is already attached to this profile."}, status=400)
        profile.doctors.add(doctor)

        return Response(
            {"message": "Branch attached.", "data": DoctorWebsiteProfileSerializer(profile, context={"request": request}).data}
        )

    def delete(self, request, pk):
        """DELETE with {"doctor": <id>} detaches one branch -- e.g. the
        doctor stopped practicing at that location. Refuses to detach the
        last remaining doctor (that's a delete of the whole profile, via
        WebsiteDoctorDetailView, not a detach)."""
        qs = DoctorWebsiteProfile.objects.all()
        qs = scope_queryset_to_branch(qs, request.user, branch_field="doctors__staff__branch").distinct()
        profile = get_object_or_404(qs, pk=pk)

        doctor_id = request.data.get("doctor")
        branch_error = _doctor_branch_write_ok(request, doctor_id)
        if branch_error:
            return branch_error

        if profile.doctors.count() <= 1:
            return Response(
                {"error": "Can't detach the last branch — delete the profile instead."}, status=400
            )
        profile.doctors.remove(doctor_id)
        return Response({"message": "Branch detached."})


class WebsiteDoctorDetailView(APIView):
    """GET/PATCH/DELETE /api/manager/website/doctors/<pk>/"""
    permission_classes = [IsAdminOrManager]

    def _get(self, request, pk):
        qs = DoctorWebsiteProfile.objects.prefetch_related(
            "doctors", "doctors__staff", "doctors__staff__user", "doctors__staff__branch",
        )
        qs = scope_queryset_to_branch(qs, request.user, branch_field="doctors__staff__branch").distinct()
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):
        return Response(DoctorWebsiteProfileSerializer(self._get(request, pk), context={"request": request}).data)

    def patch(self, request, pk):
        obj = self._get(request, pk)
        ser = DoctorWebsiteProfileSerializer(obj, data=request.data, partial=True, context={"request": request})
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        self._get(request, pk).delete()
        return Response({"message": "Doctor website profile deleted."})


class WebsiteDoctorWeeklyAvailabilityListView(APIView):
    """
    GET/POST /api/manager/website/doctors/<doctor_id>/weekly-availability/

    Branch-scoped: GET only returns rows for a doctor_id in the caller's
    own branch (empty for a cross-branch doctor_id, unless group admin);
    POST is explicitly rejected for a cross-branch doctor_id rather than
    silently returning nothing, since a create shouldn't fail quietly.
    """
    permission_classes = [IsAdminOrManager]

    def get(self, request, doctor_id):
        qs = DoctorWeeklyAvailability.objects.filter(doctor_id=doctor_id)
        qs = scope_queryset_to_branch(qs, request.user, branch_field="doctor__staff__branch")
        return Response(DoctorWeeklyAvailabilitySerializer(qs, many=True).data)

    def post(self, request, doctor_id):
        branch_error = _doctor_branch_write_ok(request, doctor_id)
        if branch_error:
            return branch_error
        data = request.data.copy()
        data["doctor"] = doctor_id
        ser = DoctorWeeklyAvailabilitySerializer(data=data)
        if ser.is_valid():
            ser.save()
            return Response({"message": "Weekly availability added.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class WebsiteDoctorWeeklyAvailabilityDetailView(APIView):
    """GET/PATCH/DELETE /api/manager/website/doctors/<doctor_id>/weekly-availability/<pk>/"""
    permission_classes = [IsAdminOrManager]

    def _get(self, request, doctor_id, pk):
        qs = DoctorWeeklyAvailability.objects.filter(pk=pk, doctor_id=doctor_id)
        qs = scope_queryset_to_branch(qs, request.user, branch_field="doctor__staff__branch")
        return get_object_or_404(qs)

    def get(self, request, doctor_id, pk):
        return Response(DoctorWeeklyAvailabilitySerializer(self._get(request, doctor_id, pk)).data)

    def patch(self, request, doctor_id, pk):
        obj = self._get(request, doctor_id, pk)
        ser = DoctorWeeklyAvailabilitySerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, doctor_id, pk):
        self._get(request, doctor_id, pk).delete()
        return Response({"message": "Weekly availability removed."})


class WebsiteDoctorAvailabilityExceptionListView(APIView):
    """
    GET/POST /api/manager/website/doctors/<doctor_id>/availability-exceptions/

    Same branch-scoping treatment as WebsiteDoctorWeeklyAvailabilityListView.
    """
    permission_classes = [IsAdminOrManager]

    def get(self, request, doctor_id):
        qs = DoctorAvailabilityException.objects.filter(doctor_id=doctor_id)
        qs = scope_queryset_to_branch(qs, request.user, branch_field="doctor__staff__branch")
        return Response(DoctorAvailabilityExceptionSerializer(qs, many=True).data)

    def post(self, request, doctor_id):
        branch_error = _doctor_branch_write_ok(request, doctor_id)
        if branch_error:
            return branch_error
        data = request.data.copy()
        data["doctor"] = doctor_id
        ser = DoctorAvailabilityExceptionSerializer(data=data)
        if ser.is_valid():
            ser.save()
            return Response({"message": "Availability exception added.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class WebsiteDoctorAvailabilityExceptionDetailView(APIView):
    """GET/PATCH/DELETE /api/manager/website/doctors/<doctor_id>/availability-exceptions/<pk>/"""
    permission_classes = [IsAdminOrManager]

    def _get(self, request, doctor_id, pk):
        qs = DoctorAvailabilityException.objects.filter(pk=pk, doctor_id=doctor_id)
        qs = scope_queryset_to_branch(qs, request.user, branch_field="doctor__staff__branch")
        return get_object_or_404(qs)

    def get(self, request, doctor_id, pk):
        return Response(DoctorAvailabilityExceptionSerializer(self._get(request, doctor_id, pk)).data)

    def patch(self, request, doctor_id, pk):
        obj = self._get(request, doctor_id, pk)
        ser = DoctorAvailabilityExceptionSerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, doctor_id, pk):
        self._get(request, doctor_id, pk).delete()
        return Response({"message": "Availability exception removed."})


def _accessible_branch_ids_for_website(user):
    """Which branches this caller may list/edit Locations-page content
    for. Returns None as a sentinel for "all branches" (group admin --
    same unrestricted access as everywhere else in Website Management).
    A Manager gets their normal multi-branch set (home + granted, via
    get_manager_accessible_branches -- same source BranchSwitcher.jsx
    uses). Any other branch-scoped account (an ordinary, non-group-admin
    Admin) is restricted to just its own branch, since a branch's own
    "About this branch" page is exactly the kind of thing a branch-level
    Admin should be able to fill in without needing group-admin rights."""
    if is_group_admin_user(user):
        return None
    staff = getattr(user, "staff_profile", None)
    if staff and staff.role == "Manager":
        from authentication.utils import get_manager_accessible_branches
        return [b.pk for b in get_manager_accessible_branches(user)]
    from authentication.utils import get_user_branch_any
    branch = get_user_branch_any(user)
    return [branch.pk] if branch else []


class WebsiteBranchListView(APIView):
    """
    GET /api/manager/website/branches/

    Lists every branch the caller manages (see
    _accessible_branch_ids_for_website), each with its Locations-page
    content if a BranchWebsiteProfile exists yet -- `has_profile: false`
    plus empty/default field values otherwise. There's no POST here:
    branches themselves are created from Admin > Branches (an
    operational action), not from Website Management -- this tab only
    ever fills in the public-content layer for a branch that already
    exists. See WebsiteBranchDetailView for the write side.
    """
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        ids = _accessible_branch_ids_for_website(request.user)
        qs = Branch.objects.select_related("website_profile").order_by("name")
        if ids is not None:
            qs = qs.filter(pk__in=ids)
        return Response(ManagerBranchWebsiteSerializer(qs, many=True, context={"request": request}).data)


class WebsiteBranchDetailView(APIView):
    """
    GET/PATCH /api/manager/website/branches/<branch_id>/

    PATCH is an upsert: the first PATCH for a branch with no
    BranchWebsiteProfile yet get_or_create's one, then partially updates
    it -- a manager filling in a brand-new branch's Locations page never
    has to make a separate "create" call first. Takes multipart/form-data
    because of the photo upload (see BranchWebsiteProfileContentSerializer).
    No DELETE: unpublishing (PATCH {"is_published": false}) is how a
    branch comes off the public Locations page -- the content itself
    stays saved, matching every other Website Management resource's
    activate/deactivate-not-delete convention for taking something down.
    """
    permission_classes = [IsAdminOrManager]

    def _get_branch(self, request, branch_id):
        ids = _accessible_branch_ids_for_website(request.user)
        qs = Branch.objects.select_related("website_profile")
        if ids is not None:
            qs = qs.filter(pk__in=ids)
        return get_object_or_404(qs, pk=branch_id)

    def get(self, request, branch_id):
        branch = self._get_branch(request, branch_id)
        return Response(ManagerBranchWebsiteSerializer(branch, context={"request": request}).data)

    def patch(self, request, branch_id):
        branch = self._get_branch(request, branch_id)
        profile, _ = BranchWebsiteProfile.objects.get_or_create(branch=branch)
        ser = BranchWebsiteProfileContentSerializer(
            profile, data=request.data, partial=True, context={"request": request}
        )
        if ser.is_valid():
            ser.save()
            return Response(ManagerBranchWebsiteSerializer(branch, context={"request": request}).data)
        return Response(ser.errors, status=400)


class WebsiteQueryListView(APIView):
    """GET /api/manager/website/queries/ — inbox from the public Contact form."""
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        qs = PatientQuery.objects.all()
        qs = _scope_patient_queries(qs, request.user)
        query_status = request.query_params.get("status", "").strip().upper()
        if query_status:
            qs = qs.filter(status=query_status)
        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(phone__icontains=search) | Q(email__icontains=search))
        return Response(PatientQuerySerializer(qs, many=True).data)


def _scope_patient_queries(qs, user):
    """
    PatientQuery (the public contact-form inbox) doesn't fit
    scope_queryset_to_branch's plain equality filter, because
    preferred_branch is nullable and general enquiries (no branch named)
    should stay visible to every branch's manager, not just be excluded.

    - Group admin: sees everything, branch-tagged or general.
    - Branch-scoped manager: sees their own branch's enquiries PLUS every
      general (preferred_branch=None) enquiry.

    NOTE: this is the default chosen absent an explicit answer on whether
    general enquiries should instead be group-admin-only, or globally
    visible to every manager regardless of branch — easy one-line change
    in whichever direction if that's not the intended behavior.
    """
    if is_group_admin_user(user):
        return qs
    branch = get_user_branch(user)
    if branch is None:
        return qs.none()
    return qs.filter(Q(preferred_branch=branch) | Q(preferred_branch__isnull=True))


class WebsiteQueryDetailView(APIView):
    """GET/PATCH /api/manager/website/queries/<pk>/ — status transitions only."""
    permission_classes = [IsAdminOrManager]

    def _get(self, request, pk):
        qs = _scope_patient_queries(PatientQuery.objects.all(), request.user)
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):
        return Response(PatientQuerySerializer(self._get(request, pk)).data)

    def patch(self, request, pk):
        obj = self._get(request, pk)
        ser = PatientQuerySerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=400)


# ── Testimonials ──────────────────────────────────────────────────────

class WebsiteTestimonialListView(APIView):
    """GET/POST /api/manager/website/testimonials/
    GET supports ?search=&ordering=&page=&page_size= for the manager
    dashboard's list/search/sort/paginate requirements."""
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        qs = Testimonial.objects.all()

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(patient_name__icontains=search) | Q(review__icontains=search))

        ordering = request.query_params.get("ordering", "").strip()
        allowed_ordering = {
            "display_order", "-display_order", "created_at", "-created_at",
            "patient_name", "-patient_name", "rating", "-rating",
        }
        if ordering in allowed_ordering:
            qs = qs.order_by(ordering)

        try:
            page = max(int(request.query_params.get("page", 1)), 1)
            page_size = min(max(int(request.query_params.get("page_size", 20)), 1), 100)
        except ValueError:
            page, page_size = 1, 20

        total = qs.count()
        start = (page - 1) * page_size
        page_qs = qs[start:start + page_size]

        return Response({
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": TestimonialSerializer(page_qs, many=True).data,
        })

    def post(self, request):
        ser = TestimonialSerializer(data=request.data)
        if ser.is_valid():
            ser.save()
            return Response({"message": "Testimonial created.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class WebsiteTestimonialDetailView(APIView):
    """GET/PATCH/DELETE /api/manager/website/testimonials/<pk>/"""
    permission_classes = [IsAdminOrManager]

    def _get(self, pk):
        return get_object_or_404(Testimonial, pk=pk)

    def get(self, request, pk):
        return Response(TestimonialSerializer(self._get(pk)).data)

    def patch(self, request, pk):
        obj = self._get(pk)
        ser = TestimonialSerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            ser.save()
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response({"message": "Testimonial deleted."})


class WebsiteTestimonialActivateView(APIView):
    """POST /api/manager/website/testimonials/<pk>/activate/"""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(Testimonial, pk=pk)
        obj.is_active = True
        obj.save()
        return Response({"message": "Testimonial activated.", "data": TestimonialSerializer(obj).data})


class WebsiteTestimonialDeactivateView(APIView):
    """POST /api/manager/website/testimonials/<pk>/deactivate/"""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(Testimonial, pk=pk)
        obj.is_active = False
        obj.save()
        return Response({"message": "Testimonial deactivated.", "data": TestimonialSerializer(obj).data})


class WebsiteTestimonialReorderView(APIView):
    """POST /api/manager/website/testimonials/reorder/  body: {"order": [id, id, ...]}"""
    permission_classes = [IsAdminOrManager]

    def post(self, request):
        ser = ReorderSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        with transaction.atomic():
            for index, pk in enumerate(ser.validated_data["order"]):
                Testimonial.objects.filter(pk=pk).update(display_order=index)
        return Response({"message": "Testimonials reordered."})


# ── YouTube Videos ────────────────────────────────────────────────────

class WebsiteYoutubeVideoListView(APIView):
    """GET/POST /api/manager/website/youtube-videos/"""
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        qs = YoutubeVideo.objects.select_related("doctor").prefetch_related("doctor__doctors", "doctor__doctors__staff", "doctor__doctors__staff__user")

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(title__icontains=search) | Q(description__icontains=search))

        ordering = request.query_params.get("ordering", "").strip()
        allowed_ordering = {"display_order", "-display_order", "created_at", "-created_at", "title", "-title"}
        if ordering in allowed_ordering:
            qs = qs.order_by(ordering)

        return Response(YoutubeVideoSerializer(qs, many=True).data)

    def post(self, request):
        ser = YoutubeVideoSerializer(data=request.data)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response({"message": "YouTube video added.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class WebsiteYoutubeVideoDetailView(APIView):
    """GET/PATCH/DELETE /api/manager/website/youtube-videos/<pk>/"""
    permission_classes = [IsAdminOrManager]

    def _get(self, pk):
        return get_object_or_404(
            YoutubeVideo.objects.select_related("doctor").prefetch_related(
                "doctor__doctors", "doctor__doctors__staff", "doctor__doctors__staff__user"
            ), pk=pk
        )

    def get(self, request, pk):
        return Response(YoutubeVideoSerializer(self._get(pk)).data)

    def patch(self, request, pk):
        obj = self._get(pk)
        ser = YoutubeVideoSerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response({"message": "YouTube video deleted."})


class WebsiteYoutubeVideoActivateView(APIView):
    """POST /api/manager/website/youtube-videos/<pk>/activate/"""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(YoutubeVideo, pk=pk)
        obj.is_active = True
        obj.save()
        return Response({"message": "Video activated.", "data": YoutubeVideoSerializer(obj).data})


class WebsiteYoutubeVideoDeactivateView(APIView):
    """POST /api/manager/website/youtube-videos/<pk>/deactivate/"""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(YoutubeVideo, pk=pk)
        obj.is_active = False
        obj.save()
        return Response({"message": "Video deactivated.", "data": YoutubeVideoSerializer(obj).data})


class WebsiteYoutubeVideoReorderView(APIView):
    """POST /api/manager/website/youtube-videos/reorder/  body: {"order": [id, id, ...]}"""
    permission_classes = [IsAdminOrManager]

    def post(self, request):
        ser = ReorderSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        with transaction.atomic():
            for index, pk in enumerate(ser.validated_data["order"]):
                YoutubeVideo.objects.filter(pk=pk).update(display_order=index)
        return Response({"message": "Videos reordered."})


# ── Instagram Posts ───────────────────────────────────────────────────

class WebsiteInstagramPostListView(APIView):
    """GET/POST /api/manager/website/instagram-posts/"""
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        qs = InstagramPost.objects.all()

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(caption__icontains=search) | Q(instagram_url__icontains=search))

        ordering = request.query_params.get("ordering", "").strip()
        allowed_ordering = {"display_order", "-display_order", "created_at", "-created_at"}
        if ordering in allowed_ordering:
            qs = qs.order_by(ordering)

        return Response(InstagramPostSerializer(qs, many=True).data)

    def post(self, request):
        ser = InstagramPostSerializer(data=request.data)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response({"message": "Instagram post added.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class WebsiteInstagramPostDetailView(APIView):
    """GET/PATCH/DELETE /api/manager/website/instagram-posts/<pk>/"""
    permission_classes = [IsAdminOrManager]

    def _get(self, pk):
        return get_object_or_404(InstagramPost, pk=pk)

    def get(self, request, pk):
        return Response(InstagramPostSerializer(self._get(pk)).data)

    def patch(self, request, pk):
        obj = self._get(pk)
        ser = InstagramPostSerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response({"message": "Instagram post deleted."})


class WebsiteInstagramPostActivateView(APIView):
    """POST /api/manager/website/instagram-posts/<pk>/activate/"""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(InstagramPost, pk=pk)
        obj.is_active = True
        obj.save()
        return Response({"message": "Post activated.", "data": InstagramPostSerializer(obj).data})


class WebsiteInstagramPostDeactivateView(APIView):
    """POST /api/manager/website/instagram-posts/<pk>/deactivate/"""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(InstagramPost, pk=pk)
        obj.is_active = False
        obj.save()
        return Response({"message": "Post deactivated.", "data": InstagramPostSerializer(obj).data})


class WebsiteInstagramPostReorderView(APIView):
    """POST /api/manager/website/instagram-posts/reorder/  body: {"order": [id, id, ...]}"""
    permission_classes = [IsAdminOrManager]

    def post(self, request):
        ser = ReorderSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        with transaction.atomic():
            for index, pk in enumerate(ser.validated_data["order"]):
                InstagramPost.objects.filter(pk=pk).update(display_order=index)
        return Response({"message": "Posts reordered."})


# ── Facebook Posts ────────────────────────────────────────────────────

class WebsiteFacebookPostListView(APIView):
    """GET/POST /api/manager/website/facebook-posts/"""
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        qs = FacebookPost.objects.all()

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(caption__icontains=search) | Q(facebook_url__icontains=search))

        ordering = request.query_params.get("ordering", "").strip()
        allowed_ordering = {"display_order", "-display_order", "created_at", "-created_at"}
        if ordering in allowed_ordering:
            qs = qs.order_by(ordering)

        return Response(FacebookPostSerializer(qs, many=True).data)

    def post(self, request):
        ser = FacebookPostSerializer(data=request.data)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response({"message": "Facebook post added.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class WebsiteFacebookPostDetailView(APIView):
    """GET/PATCH/DELETE /api/manager/website/facebook-posts/<pk>/"""
    permission_classes = [IsAdminOrManager]

    def _get(self, pk):
        return get_object_or_404(FacebookPost, pk=pk)

    def get(self, request, pk):
        return Response(FacebookPostSerializer(self._get(pk)).data)

    def patch(self, request, pk):
        obj = self._get(pk)
        ser = FacebookPostSerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response({"message": "Facebook post deleted."})


class WebsiteFacebookPostActivateView(APIView):
    """POST /api/manager/website/facebook-posts/<pk>/activate/"""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(FacebookPost, pk=pk)
        obj.is_active = True
        obj.save()
        return Response({"message": "Post activated.", "data": FacebookPostSerializer(obj).data})


class WebsiteFacebookPostDeactivateView(APIView):
    """POST /api/manager/website/facebook-posts/<pk>/deactivate/"""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(FacebookPost, pk=pk)
        obj.is_active = False
        obj.save()
        return Response({"message": "Post deactivated.", "data": FacebookPostSerializer(obj).data})


class WebsiteFacebookPostReorderView(APIView):
    """POST /api/manager/website/facebook-posts/reorder/  body: {"order": [id, id, ...]}"""
    permission_classes = [IsAdminOrManager]

    def post(self, request):
        ser = ReorderSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        with transaction.atomic():
            for index, pk in enumerate(ser.validated_data["order"]):
                FacebookPost.objects.filter(pk=pk).update(display_order=index)
        return Response({"message": "Posts reordered."})


# ── Media & Events ───────────────────────────────────────────────────

class WebsiteMediaEventListView(APIView):
    """GET/POST /api/manager/website/media-events/"""
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        qs = MediaEvent.objects.all()

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(title__icontains=search) | Q(excerpt__icontains=search))

        ordering = request.query_params.get("ordering", "").strip()
        allowed_ordering = {"display_order", "-display_order", "created_at", "-created_at"}
        if ordering in allowed_ordering:
            qs = qs.order_by(ordering)

        return Response(MediaEventSerializer(qs, many=True).data)

    def post(self, request):
        ser = MediaEventSerializer(data=request.data)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response({"message": "Media & Events item added.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class WebsiteMediaEventDetailView(APIView):
    """GET/PATCH/DELETE /api/manager/website/media-events/<pk>/"""
    permission_classes = [IsAdminOrManager]

    def _get(self, pk):
        return get_object_or_404(MediaEvent, pk=pk)

    def get(self, request, pk):
        return Response(MediaEventSerializer(self._get(pk)).data)

    def patch(self, request, pk):
        obj = self._get(pk)
        ser = MediaEventSerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response({"message": "Media & Events item deleted."})


class WebsiteMediaEventActivateView(APIView):
    """POST /api/manager/website/media-events/<pk>/activate/"""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(MediaEvent, pk=pk)
        obj.is_active = True
        obj.save()
        return Response({"message": "Item activated.", "data": MediaEventSerializer(obj).data})


class WebsiteMediaEventDeactivateView(APIView):
    """POST /api/manager/website/media-events/<pk>/deactivate/"""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(MediaEvent, pk=pk)
        obj.is_active = False
        obj.save()
        return Response({"message": "Item deactivated.", "data": MediaEventSerializer(obj).data})


class WebsiteMediaEventReorderView(APIView):
    """POST /api/manager/website/media-events/reorder/  body: {"order": [id, id, ...]}"""
    permission_classes = [IsAdminOrManager]

    def post(self, request):
        ser = ReorderSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        with transaction.atomic():
            for index, pk in enumerate(ser.validated_data["order"]):
                MediaEvent.objects.filter(pk=pk).update(display_order=index)
        return Response({"message": "Items reordered."})


# ── Gallery ───────────────────────────────────────────────────────────

class WebsiteGalleryImageListView(APIView):
    """GET/POST /api/manager/website/gallery/"""
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        qs = GalleryImage.objects.all()

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(caption__icontains=search)

        ordering = request.query_params.get("ordering", "").strip()
        allowed_ordering = {"display_order", "-display_order", "created_at", "-created_at"}
        if ordering in allowed_ordering:
            qs = qs.order_by(ordering)

        return Response(GalleryImageSerializer(qs, many=True).data)

    def post(self, request):
        ser = GalleryImageSerializer(data=request.data)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response({"message": "Image added.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class WebsiteGalleryImageDetailView(APIView):
    """GET/PATCH/DELETE /api/manager/website/gallery/<pk>/"""
    permission_classes = [IsAdminOrManager]

    def _get(self, pk):
        return get_object_or_404(GalleryImage, pk=pk)

    def get(self, request, pk):
        return Response(GalleryImageSerializer(self._get(pk)).data)

    def patch(self, request, pk):
        obj = self._get(pk)
        ser = GalleryImageSerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response({"message": "Image deleted."})


class WebsiteGalleryImageActivateView(APIView):
    """POST /api/manager/website/gallery/<pk>/activate/"""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(GalleryImage, pk=pk)
        obj.is_active = True
        obj.save()
        return Response({"message": "Image activated.", "data": GalleryImageSerializer(obj).data})


class WebsiteGalleryImageDeactivateView(APIView):
    """POST /api/manager/website/gallery/<pk>/deactivate/"""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(GalleryImage, pk=pk)
        obj.is_active = False
        obj.save()
        return Response({"message": "Image deactivated.", "data": GalleryImageSerializer(obj).data})


class WebsiteGalleryImageReorderView(APIView):
    """POST /api/manager/website/gallery/reorder/  body: {"order": [id, id, ...]}"""
    permission_classes = [IsAdminOrManager]

    def post(self, request):
        ser = ReorderSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        with transaction.atomic():
            for index, pk in enumerate(ser.validated_data["order"]):
                GalleryImage.objects.filter(pk=pk).update(display_order=index)
        return Response({"message": "Images reordered."})


# ── Specialties ───────────────────────────────────────────────────────

class WebsiteSpecialtyListView(APIView):
    """GET/POST /api/manager/website/specialities/

    GET supports ?search=&ordering=. By default only top-level
    specialties are returned (parent__isnull=True), matching the tree
    shape the CMS list page needs (each with its sub_specialties
    nested). ?all=true returns every specialty flat, ignoring the
    top-level-only filter — used to populate the specialty picker
    dropdown when creating/editing a DoctorProfile, the same role
    ?all=true plays for ManagerEmrDoctorListView's doctor picker."""
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        qs = Specialty.objects.select_related("parent").prefetch_related("sub_specialties")

        if request.query_params.get("all", "").lower() != "true":
            qs = qs.filter(parent__isnull=True)

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(short_description__icontains=search))

        ordering = request.query_params.get("ordering", "").strip()
        allowed_ordering = {"display_order", "-display_order", "name", "-name", "created_at", "-created_at"}
        if ordering in allowed_ordering:
            qs = qs.order_by(ordering)

        return Response(SpecialtySerializer(qs, many=True).data)

    def post(self, request):
        ser = SpecialtySerializer(data=request.data)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response({"message": "Specialty created.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class WebsiteSpecialtyDetailView(APIView):
    """GET/PATCH/DELETE /api/manager/website/specialities/<pk>/"""
    permission_classes = [IsAdminOrManager]

    def _get(self, pk):
        return get_object_or_404(
            Specialty.objects.select_related("parent").prefetch_related("sub_specialties"), pk=pk
        )

    def get(self, request, pk):
        return Response(SpecialtySerializer(self._get(pk)).data)

    def patch(self, request, pk):
        obj = self._get(pk)
        ser = SpecialtySerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response({"message": "Specialty deleted."})


class WebsiteSpecialtyActivateView(APIView):
    """POST /api/manager/website/specialities/<pk>/activate/ — publishes it."""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(Specialty, pk=pk)
        obj.is_published = True
        obj.save()
        return Response({"message": "Specialty published.", "data": SpecialtySerializer(obj).data})


class WebsiteSpecialtyDeactivateView(APIView):
    """POST /api/manager/website/specialities/<pk>/deactivate/ — unpublishes it."""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(Specialty, pk=pk)
        obj.is_published = False
        obj.save()
        return Response({"message": "Specialty unpublished.", "data": SpecialtySerializer(obj).data})


class WebsiteSpecialtyReorderView(APIView):
    """POST /api/manager/website/specialities/reorder/  body: {"order": [id, id, ...]}"""
    permission_classes = [IsAdminOrManager]

    def post(self, request):
        ser = ReorderSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        with transaction.atomic():
            for index, pk in enumerate(ser.validated_data["order"]):
                Specialty.objects.filter(pk=pk).update(display_order=index)
        return Response({"message": "Specialties reordered."})


# ── Treatments / Diseases ─────────────────────────────────────────────

class WebsiteTreatmentListView(APIView):
    """GET/POST /api/manager/website/procedures/
    GET supports ?specialty=<id>&search=&ordering=."""
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        qs = Treatment.objects.select_related("specialty")

        specialty_id = request.query_params.get("specialty")
        if specialty_id:
            qs = qs.filter(specialty_id=specialty_id)

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(summary__icontains=search))

        ordering = request.query_params.get("ordering", "").strip()
        allowed_ordering = {"display_order", "-display_order", "name", "-name", "created_at", "-created_at"}
        if ordering in allowed_ordering:
            qs = qs.order_by(ordering)

        return Response(TreatmentSerializer(qs, many=True).data)

    def post(self, request):
        ser = TreatmentSerializer(data=request.data)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response({"message": "Treatment created.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class WebsiteTreatmentDetailView(APIView):
    """GET/PATCH/DELETE /api/manager/website/procedures/<pk>/"""
    permission_classes = [IsAdminOrManager]

    def _get(self, pk):
        return get_object_or_404(Treatment.objects.select_related("specialty"), pk=pk)

    def get(self, request, pk):
        return Response(TreatmentSerializer(self._get(pk)).data)

    def patch(self, request, pk):
        obj = self._get(pk)
        ser = TreatmentSerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response({"message": "Treatment deleted."})


class WebsiteTreatmentActivateView(APIView):
    """POST /api/manager/website/procedures/<pk>/activate/"""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(Treatment, pk=pk)
        obj.is_active = True
        obj.save()
        return Response({"message": "Treatment activated.", "data": TreatmentSerializer(obj).data})


class WebsiteTreatmentDeactivateView(APIView):
    """POST /api/manager/website/procedures/<pk>/deactivate/"""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(Treatment, pk=pk)
        obj.is_active = False
        obj.save()
        return Response({"message": "Treatment deactivated.", "data": TreatmentSerializer(obj).data})


class WebsiteTreatmentReorderView(APIView):
    """POST /api/manager/website/procedures/reorder/  body: {"order": [id, id, ...]}"""
    permission_classes = [IsAdminOrManager]

    def post(self, request):
        ser = ReorderSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        with transaction.atomic():
            for index, pk in enumerate(ser.validated_data["order"]):
                Treatment.objects.filter(pk=pk).update(display_order=index)
        return Response({"message": "Treatments reordered."})


# ── Specialty content sections ("Why Choose Us?", etc.) ────────────────

class WebsiteSpecialtySectionListView(APIView):
    """GET/POST /api/manager/website/specialty-sections/
    GET requires ?specialty=<id> — these blocks only ever make sense
    scoped to one specialty's edit screen, unlike procedures which also
    support an unscoped listing."""
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        qs = SpecialtySection.objects.select_related("specialty")
        specialty_id = request.query_params.get("specialty")
        if specialty_id:
            qs = qs.filter(specialty_id=specialty_id)

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(heading__icontains=search) | Q(intro__icontains=search))

        ordering = request.query_params.get("ordering", "").strip()
        allowed_ordering = {"display_order", "-display_order", "heading", "-heading", "created_at", "-created_at"}
        if ordering in allowed_ordering:
            qs = qs.order_by(ordering)

        return Response(SpecialtySectionSerializer(qs, many=True).data)

    def post(self, request):
        ser = SpecialtySectionSerializer(data=request.data)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response({"message": "Section created.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class WebsiteSpecialtySectionDetailView(APIView):
    """GET/PATCH/DELETE /api/manager/website/specialty-sections/<pk>/"""
    permission_classes = [IsAdminOrManager]

    def _get(self, pk):
        return get_object_or_404(SpecialtySection.objects.select_related("specialty"), pk=pk)

    def get(self, request, pk):
        return Response(SpecialtySectionSerializer(self._get(pk)).data)

    def patch(self, request, pk):
        obj = self._get(pk)
        ser = SpecialtySectionSerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response({"message": "Section deleted."})


class WebsiteSpecialtySectionReorderView(APIView):
    """POST /api/manager/website/specialty-sections/reorder/  body: {"order": [id, id, ...]}"""
    permission_classes = [IsAdminOrManager]

    def post(self, request):
        ser = ReorderSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        with transaction.atomic():
            for index, pk in enumerate(ser.validated_data["order"]):
                SpecialtySection.objects.filter(pk=pk).update(display_order=index)
        return Response({"message": "Sections reordered."})


# ── Blogs ─────────────────────────────────────────────────────────────

class WebsiteBlogListView(APIView):
    """GET/POST /api/manager/website/blogs/
    GET supports ?specialty=<id>&search=&ordering=&page=&page_size=."""
    permission_classes = [IsAdminOrManager]

    def get(self, request):
        qs = Blog.objects.select_related("specialty", "author_doctor").prefetch_related(
            "author_doctor__doctors", "author_doctor__doctors__staff", "author_doctor__doctors__staff__user"
        )

        specialty_id = request.query_params.get("specialty")
        if specialty_id:
            qs = qs.filter(specialty_id=specialty_id)

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(title__icontains=search) | Q(excerpt__icontains=search))

        ordering = request.query_params.get("ordering", "").strip()
        allowed_ordering = {"display_order", "-display_order", "title", "-title", "created_at", "-created_at"}
        if ordering in allowed_ordering:
            qs = qs.order_by(ordering)

        try:
            page = max(int(request.query_params.get("page", 1)), 1)
            page_size = min(max(int(request.query_params.get("page_size", 20)), 1), 100)
        except ValueError:
            page, page_size = 1, 20

        total = qs.count()
        start = (page - 1) * page_size
        page_qs = qs[start:start + page_size]

        return Response({
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": BlogSerializer(page_qs, many=True).data,
        })

    def post(self, request):
        ser = BlogSerializer(data=request.data)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response({"message": "Blog created.", "data": ser.data}, status=201)
        return Response(ser.errors, status=400)


class WebsiteBlogDetailView(APIView):
    """GET/PATCH/DELETE /api/manager/website/blogs/<pk>/"""
    permission_classes = [IsAdminOrManager]

    def _get(self, pk):
        return get_object_or_404(
            Blog.objects.select_related("specialty", "author_doctor").prefetch_related(
                "author_doctor__doctors", "author_doctor__doctors__staff", "author_doctor__doctors__staff__user"
            ),
            pk=pk,
        )

    def get(self, request, pk):
        return Response(BlogSerializer(self._get(pk)).data)

    def patch(self, request, pk):
        obj = self._get(pk)
        ser = BlogSerializer(obj, data=request.data, partial=True)
        if ser.is_valid():
            try:
                ser.save()
            except DjangoValidationError as exc:
                return Response({"error": _safe_detail(exc)}, status=400)
            return Response(ser.data)
        return Response(ser.errors, status=400)

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response({"message": "Blog deleted."})


class WebsiteBlogActivateView(APIView):
    """POST /api/manager/website/blogs/<pk>/activate/ — publishes it."""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(Blog, pk=pk)
        obj.is_published = True
        obj.save()
        return Response({"message": "Blog published.", "data": BlogSerializer(obj).data})


class WebsiteBlogDeactivateView(APIView):
    """POST /api/manager/website/blogs/<pk>/deactivate/ — unpublishes it."""
    permission_classes = [IsAdminOrManager]

    def post(self, request, pk):
        obj = get_object_or_404(Blog, pk=pk)
        obj.is_published = False
        obj.save()
        return Response({"message": "Blog unpublished.", "data": BlogSerializer(obj).data})


class WebsiteBlogReorderView(APIView):
    """POST /api/manager/website/blogs/reorder/  body: {"order": [id, id, ...]}"""
    permission_classes = [IsAdminOrManager]

    def post(self, request):
        ser = ReorderSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        with transaction.atomic():
            for index, pk in enumerate(ser.validated_data["order"]):
                Blog.objects.filter(pk=pk).update(display_order=index)
        return Response({"message": "Blogs reordered."})


# ═══════════════════════════════════════════════════════
# PUBLIC WEBSITE — read-only + write endpoints for the
# unauthenticated marketing site. Lives in this same `manager`
# app (no separate Django app) — mounted at /api/public/ via
# manager/public_urls.py, included from rhimsbackend/urls.py.
# ═══════════════════════════════════════════════════════

def _compute_available_slots(doctor_id, target_date):
    """
    Returns a sorted list of "HH:MM" slot-start strings still open for
    booking on `target_date` for `doctor_id`.

    Precedence: DoctorAvailabilityException for that exact date wins over
    the normal weekly rule —
      • is_unavailable=True            -> no slots at all that day.
      • start_time/end_time overridden -> use that single window instead
        of the weekly windows. Slot duration for that window: the
        exception's own slot_duration_minutes if the manager set one,
        else the weekly rule's duration for that weekday if one exists,
        else a 15-minute default — so a one-off "duty day" exception
        works standalone even when the doctor has no weekly rule at all.
      • no exception row for that date -> use every DoctorWeeklyAvailability
        window defined for that weekday (a doctor can have more than one
        window per day, e.g. morning + evening).
    Already-booked (non-cancelled) ConsultationPreBooking slots for that
    doctor+date are then removed from the result. Past times are removed
    if target_date is today.
    """
    from doctor.models import DoctorProfile
    from reception.models import ConsultationPreBooking

    get_object_or_404(DoctorProfile, pk=doctor_id)

    weekday = target_date.weekday()  # Monday=0 ... Sunday=6, matches DAY_CHOICES

    exception = DoctorAvailabilityException.objects.filter(doctor_id=doctor_id, date=target_date).first()

    windows = []  # list of (start_time, end_time, slot_duration_minutes)

    if exception:
        if exception.is_unavailable:
            return []
        if exception.slot_duration_minutes:
            duration = exception.slot_duration_minutes
        else:
            weekly_for_day = DoctorWeeklyAvailability.objects.filter(doctor_id=doctor_id, day_of_week=weekday).first()
            duration = weekly_for_day.slot_duration_minutes if weekly_for_day else 15
        windows.append((exception.start_time, exception.end_time, duration))
    else:
        for rule in DoctorWeeklyAvailability.objects.filter(doctor_id=doctor_id, day_of_week=weekday):
            windows.append((rule.start_time, rule.end_time, rule.slot_duration_minutes))

    if not windows:
        return []

    booked_times = set(
        ConsultationPreBooking.objects
        .filter(doctor_id=doctor_id, requested_date=target_date)
        .exclude(status='CANCELLED')
        .values_list('requested_time', flat=True)
    )

    now = timezone.localtime()
    is_today = target_date == now.date()

    slots = []
    for start_time, end_time, duration in windows:
        current = datetime.combine(target_date, start_time)
        window_end = datetime.combine(target_date, end_time)
        step = timedelta(minutes=duration)
        while current + step <= window_end:
            slot_time = current.time()
            if slot_time not in booked_times and not (is_today and current.time() < now.time()):
                slots.append(slot_time)
            current += step

    slots = sorted(set(slots))
    return [t.strftime("%H:%M") for t in slots]


class PublicDoctorListView(APIView):
    """GET /api/public/doctors/ — published doctors only, ordered by
    display_order. AllowAny, no throttling beyond the default anon rate
    since this is a read. One card per profile regardless of how many
    branches it covers -- multi-branch doctors are no longer listed
    twice (see DoctorWebsiteProfile's doctors M2M docstring)."""
    permission_classes = [AllowAny]

    def get(self, request):
        qs = (
            DoctorWebsiteProfile.objects
            .filter(is_published=True)
            .prefetch_related("doctors", "doctors__staff", "doctors__staff__user", "doctors__staff__branch")
            .order_by("display_order")
        )
        data = PublicDoctorSerializer(qs, many=True, context={"request": request}).data
        return Response(data)


class PublicDoctorDetailView(APIView):
    """GET /api/public/doctors/<doctor_id>/ — the "View Profile" page for
    one published doctor: everything PublicDoctorListView returns plus
    designation, experience summary, area of expertise, education,
    awards, languages known, and a 7-day duty schedule (today + next 6
    days, real dates not generic weekdays) derived from date-specific
    availability with weekly-recurring hours as fallback — same
    precedence _compute_available_slots() uses for booking, so this can
    never disagree with what's actually bookable. 404s for unpublished/unknown doctors,
    same as the list view silently omitting them.

    ✅ FIX: `doctor_id` in the URL is still a DoctorProfile pk (so
    existing frontend links from doctor cards keep working unchanged),
    but the lookup now goes through the M2M (`doctors__profile_id=`,
    DoctorProfile's actual pk field) with .distinct() rather than the
    old direct `doctor_id=` equality, since a profile can be reached
    via any of its attached branches' ids. Whichever branch id the
    visitor clicked from, they land on the same shared profile with
    every branch's timing shown (see get_timing)."""
    permission_classes = [AllowAny]

    def get(self, request, doctor_id):
        profile = get_object_or_404(
            DoctorWebsiteProfile.objects
            .prefetch_related("doctors", "doctors__staff", "doctors__staff__user", "doctors__staff__branch")
            .filter(is_published=True, doctors__profile_id=doctor_id)
            .distinct(),
        )
        data = PublicDoctorDetailSerializer(profile, context={"request": request}).data
        return Response(data)


class PublicAvailabilityView(APIView):
    """GET /api/public/availability/?doctor_id=&date=YYYY-MM-DD"""
    permission_classes = [AllowAny]

    def get(self, request):
        doctor_id = request.query_params.get("doctor_id")
        date_str = request.query_params.get("date")
        if not doctor_id or not date_str:
            return Response({"error": "doctor_id and date are required."}, status=400)
        try:
            doctor_id = int(doctor_id)
        except ValueError:
            return Response({"error": "doctor_id must be an integer."}, status=400)
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            return Response({"error": "date must be in YYYY-MM-DD format."}, status=400)

        # Only consider doctors actually published on the site. `doctor_id`
        # is a specific branch's DoctorProfile pk -- booking stays
        # per-branch even though the public profile is now shared (see
        # DoctorWebsiteProfile's doctors M2M docstring).
        if not DoctorWebsiteProfile.objects.filter(doctors__profile_id=doctor_id, is_published=True).exists():
            return Response({"error": "Doctor not found."}, status=404)

        slots = _compute_available_slots(doctor_id, target_date)
        return Response({"doctor_id": doctor_id, "date": date_str, "available_slots": slots})


class PublicTestimonialListView(APIView):
    """GET /api/public/testimonials/ — active only."""
    permission_classes = [AllowAny]

    def get(self, request):
        qs = Testimonial.objects.filter(is_active=True).order_by("display_order")
        return Response(PublicTestimonialSerializer(qs, many=True, context={"request": request}).data)


class PublicYoutubeVideoListView(APIView):
    """GET /api/public/youtube-videos/ — active only."""
    permission_classes = [AllowAny]

    def get(self, request):
        qs = YoutubeVideo.objects.filter(is_active=True).order_by("display_order")
        return Response(PublicYoutubeVideoSerializer(qs, many=True).data)


class PublicInstagramPostListView(APIView):
    """GET /api/public/instagram-posts/ — active only."""
    permission_classes = [AllowAny]

    def get(self, request):
        qs = InstagramPost.objects.filter(is_active=True).order_by("display_order")
        return Response(PublicInstagramPostSerializer(qs, many=True, context={"request": request}).data)


class PublicFacebookPostListView(APIView):
    """GET /api/public/facebook-posts/ — active only."""
    permission_classes = [AllowAny]

    def get(self, request):
        qs = FacebookPost.objects.filter(is_active=True).order_by("display_order")
        return Response(PublicFacebookPostSerializer(qs, many=True, context={"request": request}).data)


class PublicMediaEventListView(APIView):
    """GET /api/public/media-events/ — active only."""
    permission_classes = [AllowAny]

    def get(self, request):
        qs = MediaEvent.objects.filter(is_active=True).order_by("display_order", "-event_date", "-created_at")
        return Response(PublicMediaEventSerializer(qs, many=True, context={"request": request}).data)


class PublicMediaEventDetailView(APIView):
    """GET /api/public/media-events/<slug>/ — a single active item's detail page."""
    permission_classes = [AllowAny]

    def get(self, request, slug):
        obj = get_object_or_404(MediaEvent.objects.filter(is_active=True), slug=slug)
        data = PublicMediaEventDetailSerializer(obj, context={"request": request}).data
        return Response(data)


class PublicGalleryImageListView(APIView):
    """GET /api/public/gallery/ — active only."""
    permission_classes = [AllowAny]

    def get(self, request):
        qs = GalleryImage.objects.filter(is_active=True).order_by("display_order", "-created_at")
        return Response(PublicGalleryImageSerializer(qs, many=True, context={"request": request}).data)


class PublicBranchListView(APIView):
    """GET /api/public/branches/ — the "Our Locations" page's data.

    A branch appears here only when BOTH are true: it's operationally
    active (Branch.is_active) AND a manager has published its public
    content (BranchWebsiteProfile.is_published). Either flag alone isn't
    enough -- an active-but-unpublished branch might be mid-setup with no
    real copy yet, and a published-but-inactive branch might be a
    location that's closed but its old content hasn't been cleaned up.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        qs = (
            Branch.objects
            .filter(is_active=True, website_profile__is_published=True)
            .select_related("website_profile")
            .order_by("website_profile__display_order", "name")
        )
        return Response(PublicBranchSerializer(qs, many=True, context={"request": request}).data)


class PublicBranchChoicesListView(APIView):
    """GET /api/public/branches/choices/ — every operationally active
    branch as {branch_id, name}, for the Contact form's "Hospitals"
    selector. See PublicBranchChoiceSerializer for why this is separate
    from PublicBranchListView."""
    permission_classes = [AllowAny]

    def get(self, request):
        qs = Branch.objects.filter(is_active=True).order_by("name")
        return Response(PublicBranchChoiceSerializer(qs, many=True).data)


class PublicSpecialtyListView(APIView):
    """GET /api/public/specialities/ — published top-level specialties,
    each with its published sub_specialties nested."""
    permission_classes = [AllowAny]

    def get(self, request):
        qs = (
            Specialty.objects
            .filter(is_published=True, parent__isnull=True)
            .prefetch_related("sub_specialties")
            .order_by("display_order", "name")
        )
        data = PublicSpecialtySerializer(qs, many=True, context={"request": request}).data
        return Response(data)


class PublicSpecialtyDetailView(APIView):
    """GET /api/public/specialities/<slug>/ — the full specialty detail
    page payload (hero, sub-specialties, procedures/diseases, doctors,
    testimonials, youtube videos, blogs) in one request. 404s for
    unpublished/unknown specialties, same as PublicDoctorDetailView."""
    permission_classes = [AllowAny]

    def get(self, request, slug):
        specialty = get_object_or_404(
            Specialty.objects.filter(is_published=True).prefetch_related("sub_specialties"),
            slug=slug,
        )
        data = PublicSpecialtyDetailSerializer(specialty, context={"request": request}).data
        return Response(data)


class PublicTreatmentDetailView(APIView):
    """GET /api/public/procedures/<slug>/ — a single active disease or
    procedure's public detail page (hero image + structured body
    sections + parent specialty breadcrumb info). 404s for an inactive
    or unknown slug, same as PublicBlogDetailView. Kind (Disease vs
    Treatment) comes back in the payload — the frontend can mount this
    under /diseases/<slug> or /procedures/<slug> as it prefers, both
    routes hitting this same endpoint."""
    permission_classes = [AllowAny]

    def get(self, request, slug):
        procedure = get_object_or_404(
            Treatment.objects.filter(is_active=True).select_related("specialty"),
            slug=slug,
        )
        data = PublicTreatmentDetailSerializer(procedure, context={"request": request}).data
        return Response(data)


class PublicBlogListView(APIView):
    """GET /api/public/blogs/ — published blogs, general listing (not
    specialty-scoped). Supports optional ?specialty=<slug> filter."""
    permission_classes = [AllowAny]

    def get(self, request):
        qs = Blog.objects.filter(is_published=True).select_related("specialty", "author_doctor")

        specialty_slug = request.query_params.get("specialty", "").strip()
        if specialty_slug:
            qs = qs.filter(specialty__slug=specialty_slug)

        qs = qs.order_by("display_order", "-created_at")
        data = PublicBlogSerializer(qs, many=True, context={"request": request}).data
        return Response(data)


class PublicBlogDetailView(APIView):
    """GET /api/public/blogs/<slug>/ — a single published blog's detail page."""
    permission_classes = [AllowAny]

    def get(self, request, slug):
        blog = get_object_or_404(Blog.objects.filter(is_published=True), slug=slug)
        data = PublicBlogDetailSerializer(blog, context={"request": request}).data
        return Response(data)


class PublicPreBookingCreateView(APIView):
    """
    POST /api/public/prebook/

    Validates the public booking form, re-checks the requested slot is
    still open (defends against a race between GET availability/ and this
    POST — two visitors could both be looking at the same open slot), then
    creates a ConsultationPreBooking with booking_mode='WEBSITE'. Returns
    the booking reference number plus a full summary of what was recorded
    (echoed straight from the saved Patient/ConsultationPreBooking rows,
    not just the raw request body) so the confirmation screen can show
    exactly what was booked.
    """
    permission_classes = [AllowAny]
    throttle_classes = [PublicWriteThrottle]
    throttle_scope = "public_write"

    def post(self, request):
        from doctor.models import DoctorProfile
        from reception.models import ConsultationPreBooking, Patient, patient_is_revisit_eligible

        ser = PublicPreBookingSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        data = ser.validated_data

        try:
            doctor = DoctorProfile.objects.get(pk=data["doctor_id"])
        except DoctorProfile.DoesNotExist:
            return Response({"error": "Doctor not found."}, status=404)

        # `doctor` here is a specific branch's DoctorProfile (the patient
        # is booking a slot at that branch) -- the M2M lookup finds the
        # shared public profile it belongs to, purely to echo the doctor's
        # display name back on the confirmation screen below.
        website_profile = DoctorWebsiteProfile.objects.filter(doctors=doctor, is_published=True).first()
        if website_profile is None:
            return Response({"error": "Doctor not found."}, status=404)

        open_slots = _compute_available_slots(doctor.pk, data["date"])
        requested_slot_str = data["time"].strftime("%H:%M")
        if requested_slot_str not in open_slots:
            return Response(
                {"error": "That slot is no longer available. Please choose another time."}, status=409
            )

        mrd_number = data.get("mrd_number")

        try:
            with transaction.atomic():
                if mrd_number:
                    # Already validated in the serializer (exists + name
                    # matches) — just re-fetch inside the transaction.
                    patient = Patient.objects.select_for_update().get(mrd_number__iexact=mrd_number)
                else:
                    # New patient — creating the Patient row here (rather
                    # than deferring to reception's later conversion, as
                    # before) generates a fresh MRD number immediately so
                    # the visitor has it on their confirmation screen.
                    name_parts = data["name"].split(None, 1)
                    patient = Patient.objects.create(
                        first_name=name_parts[0],
                        last_name=name_parts[1] if len(name_parts) > 1 else "",
                        phone=data["phone"],
                        gender=data.get("gender") or None,
                        age=data.get("age"),
                    )

                # Free-revisit eligibility is opt-in (data["is_revisit"]
                # defaults to False -> normal paid NEW consultation) and
                # always re-checked here server-side -- the client's flag
                # is only ever a request, never trusted on its own. If the
                # visitor asked for it but doesn't actually qualify (e.g.
                # the window closed between the frontend's live check and
                # this submit), we silently fall back to a normal paid
                # NEW booking rather than erroring.
                book_as_revisit = bool(data.get("is_revisit")) and patient_is_revisit_eligible(patient)
                consultation_type = "REVISIT" if book_as_revisit else "NEW"
                consultation_fee = Decimal("0") if book_as_revisit else doctor.consultation_fee

                booking = ConsultationPreBooking.objects.create(
                    patient=patient,
                    doctor=doctor,
                    booking_mode="WEBSITE",
                    consultation_type=consultation_type,
                    requested_date=data["date"],
                    requested_time=data["time"],
                    consultation_fee=consultation_fee,
                )
        except DjangoValidationError as exc:
            return Response({"error": _safe_detail(exc)}, status=400)

        return Response({
            "message": "Appointment requested successfully.",
            "reference_number": booking.reference_number,
            "status": booking.status,
            "mrd_number": patient.mrd_number,
            "patient_name": f"{patient.first_name} {patient.last_name}".strip(),
            "phone": patient.phone,
            "gender": patient.gender,
            "consultation_type": booking.consultation_type,
            "consultation_fee": booking.consultation_fee,
            "age": patient.age,
            "doctor_name": website_profile.get_name(),
            "specialty": doctor.specialty.name if doctor.specialty else "",
            "date": booking.requested_date.isoformat(),
            "time": booking.requested_time.strftime("%H:%M"),
        }, status=201)


class PublicMrdCheckView(APIView):
    """
    POST /api/public/mrd-check/

    Live, pre-submit check as the visitor fills in MRD Number / Name /
    Phone / Gender on the booking form -- lets the page show a "you're
    eligible for a free revisit" message (or a name/phone/gender
    mismatch) before they get to the Submit button, without yet knowing
    doctor/date/time. Read-only: never creates or modifies anything.
    """
    permission_classes = [AllowAny]
    throttle_classes = [PublicWriteThrottle]
    throttle_scope = "public_write"

    def post(self, request):
        from reception.models import patient_is_revisit_eligible

        ser = PublicMrdCheckSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)

        patient = ser._existing_patient
        is_eligible = patient_is_revisit_eligible(patient)

        return Response({
            "valid": True,
            "mrd_number": patient.mrd_number,
            "patient_name": f"{patient.first_name} {patient.last_name}".strip(),
            "is_revisit_eligible": is_eligible,
            "message": (
                "You're eligible for a free revisit consultation with this doctor."
                if is_eligible else None
            ),
        })


class PublicContactInquiryCreateView(APIView):
    """POST /api/public/contact/ — creates a PatientQuery for the manager inbox."""
    permission_classes = [AllowAny]
    throttle_classes = [PublicWriteThrottle]
    throttle_scope = "public_write"

    def post(self, request):
        ser = PublicContactInquirySerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        try:
            ser.save()
        except DjangoValidationError as exc:
            return Response({"error": _safe_detail(exc)}, status=400)
        return Response({"message": "Thank you — we'll be in touch soon."}, status=201)