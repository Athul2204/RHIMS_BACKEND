# manager/serializers.py
from decimal import Decimal
import json
from rest_framework import serializers
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q, Sum
from django.utils import timezone
from .models import (
    SupportStaff, Attendance, LeaveRequest, SalaryRecord, SalaryEntry, HospitalExpense,
    OtherIncome, Dealer, DealerTransaction,
    DoctorWebsiteProfile, DoctorWeeklyAvailability, DoctorAvailabilityException,
    PatientQuery, Testimonial, YoutubeVideo, InstagramPost, FacebookPost,
    MediaEvent, GalleryImage,
    Specialty, SpecialtySection, Treatment, Blog,
    BranchWebsiteProfile,
    validate_deals_in,
)
# Imported directly (not via manager.models) same as views.py's EmrDoctorProfile
# alias -- avoids a circular import at module load time since doctor.models
# doesn't import anything from manager.serializers.
from doctor.models import DoctorProfile as EmrDoctorProfile
from administration.models import Branch


def _staff_profile_display_name(staff_profile):
    """
    Safely resolve a display name for a StaffProfile's linked user.

    staff_profile.user is a required OneToOne in normal operation, but a
    data-integrity gap (e.g. a User row removed without its StaffProfile/
    history being cleaned up) leaves the FK pointing nowhere. Accessing
    `.user` in that case raises RelatedObjectDoesNotExist, which is NOT a
    DRF-handled exception — it previously surfaced as an unhandled 500 on
    every list endpoint (attendance, leave, salary) that happened to
    include that record. Fall back to the staff_code instead of crashing.
    """
    try:
        user = staff_profile.user
    except User.DoesNotExist:
        return f"{staff_profile.staff_code} (user removed)"
    return user.get_full_name() or user.username


# ─────────────────────────────────────────────
# SUPPORT STAFF
# ─────────────────────────────────────────────
class SupportStaffSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source="branch.name", read_only=True)

    class Meta:
        model  = SupportStaff
        fields = [
            "staff_id", "staff_code", "branch", "branch_name", "full_name", "role", "department",
            "phone", "date_of_birth", "address", "joining_date",
            "salary_type", "monthly_salary", "daily_rate", "weekly_rate",
            "mandatory_working_days",
            "is_active", "notes", "created_at", "updated_at",
        ]
        # ✅ FIX: "branch" is a required model field but was missing from
        # this serializer entirely, so POST would fail with an
        # IntegrityError. It's writable here — the view is responsible for
        # injecting/enforcing the correct value from request.user before
        # validation (see resolve_branch_for_write in SupportStaffListView),
        # not left for the client to set arbitrarily.
        read_only_fields = ["staff_id", "staff_code", "created_at", "updated_at"]
        extra_kwargs = {
            "salary_type":            {"required": False},
            "monthly_salary":         {"required": False},
            "daily_rate":             {"required": False},
            "weekly_rate":            {"required": False},
            "mandatory_working_days": {"required": False},
        }


# ─────────────────────────────────────────────
# ATTENDANCE
# ─────────────────────────────────────────────
class AttendanceSerializer(serializers.ModelSerializer):
    # Readable display fields
    staff_name      = serializers.SerializerMethodField()
    staff_role      = serializers.SerializerMethodField()
    staff_code      = serializers.SerializerMethodField()
    staff_type      = serializers.SerializerMethodField()   # "staff_profile" | "support_staff"
    marked_by_name  = serializers.SerializerMethodField()

    class Meta:
        model  = Attendance
        fields = [
            "attendance_id", "date",
            "staff_profile", "support_staff",
            "status", "notes",
            "marked_by", "marked_by_name",
            "staff_name", "staff_role", "staff_code", "staff_type",
            "created_at", "updated_at",
        ]
        read_only_fields = ["attendance_id", "marked_by", "created_at", "updated_at"]

    def get_staff_name(self, obj):
        if obj.staff_profile:
            return _staff_profile_display_name(obj.staff_profile)
        if obj.support_staff:
            return obj.support_staff.full_name
        return None

    def get_staff_role(self, obj):
        if obj.staff_profile:
            return obj.staff_profile.role
        if obj.support_staff:
            return obj.support_staff.role
        return None

    def get_staff_code(self, obj):
        if obj.staff_profile:
            return obj.staff_profile.staff_code
        if obj.support_staff:
            return obj.support_staff.staff_code
        return None

    def get_staff_type(self, obj):
        if obj.staff_profile_id:
            return "staff_profile"
        if obj.support_staff_id:
            return "support_staff"
        return None

    def get_marked_by_name(self, obj):
        if obj.marked_by:
            return obj.marked_by.get_full_name() or obj.marked_by.username
        return None


class BulkAttendanceSerializer(serializers.Serializer):
    """Used by the bulk-mark endpoint."""
    date    = serializers.DateField()
    records = serializers.ListField(
        child=serializers.DictField(),
        min_length=1,
    )


# ─────────────────────────────────────────────
# LEAVE REQUEST
# ─────────────────────────────────────────────
class LeaveRequestSerializer(serializers.ModelSerializer):
    staff_name = serializers.SerializerMethodField()
    staff_code = serializers.SerializerMethodField()
    staff_role = serializers.SerializerMethodField()
    staff_type = serializers.SerializerMethodField()
    total_days = serializers.ReadOnlyField()

    class Meta:
        model  = LeaveRequest
        fields = [
            "leave_id",
            "staff_profile", "support_staff",
            "leave_type", "start_date", "end_date", "reason",
            "status", "approved_by", "approved_at", "rejection_reason",
            "total_days",
            "staff_name", "staff_code", "staff_role", "staff_type",
            "created_at", "updated_at",
        ]
        read_only_fields = ["leave_id", "approved_by", "approved_at", "created_at", "updated_at"]

    def get_staff_name(self, obj):
        if obj.staff_profile:
            return _staff_profile_display_name(obj.staff_profile)
        if obj.support_staff:
            return obj.support_staff.full_name
        return None

    def get_staff_code(self, obj):
        if obj.staff_profile:
            return obj.staff_profile.staff_code
        if obj.support_staff:
            return obj.support_staff.staff_code
        return None

    def get_staff_role(self, obj):
        if obj.staff_profile:
            return obj.staff_profile.role
        if obj.support_staff:
            return obj.support_staff.role
        return None

    def get_staff_type(self, obj):
        if obj.staff_profile_id:
            return "staff_profile"
        if obj.support_staff_id:
            return "support_staff"
        return None


def _reference_rate_and_basis(obj):
    """
    Returns (reference_rate, pay_basis) for a SalaryRecord's linked staff
    member — informational only, shown on the Salary sheet so a manager
    can sanity-check the totals their entries add up to. Never used to
    auto-fill net_salary. Salary is tracked exclusively by calendar month
    now, so the reference figure is always the Monthly rate.
    """
    if obj.support_staff:
        return (obj.support_staff.reference_rate("Monthly"), obj.support_staff.salary_type)

    if obj.staff_profile:
        # StaffProfile now carries its own salary_type/daily_rate/weekly_rate
        # (mirrors SupportStaff), so delegate to its reference_rate() instead
        # of assuming Monthly.
        return (obj.staff_profile.reference_rate("Monthly"), obj.staff_profile.salary_type)

    return (0, None)


def _mandatory_working_days(obj):
    """Minimum monthly duty days for the linked staff member — reference-only,
    shown next to Present/Absent on the Salary sheet. Only SupportStaff
    carries this concept currently; StaffProfile-linked records return None."""
    if obj.support_staff:
        return obj.support_staff.mandatory_working_days
    return None


# ─────────────────────────────────────────────
# SALARY RECORD
# ─────────────────────────────────────────────
class SalaryRecordSerializer(serializers.ModelSerializer):
    staff_name    = serializers.SerializerMethodField()
    staff_code    = serializers.SerializerMethodField()
    staff_role    = serializers.SerializerMethodField()
    staff_type    = serializers.SerializerMethodField()
    pay_basis     = serializers.SerializerMethodField()
    reference_rate = serializers.SerializerMethodField()
    mandatory_working_days = serializers.SerializerMethodField()

    class Meta:
        model  = SalaryRecord
        fields = [
            "record_id",
            "staff_profile", "support_staff",
            "month", "year",
            "present_days", "paid_leave_days", "unpaid_leave_days",
            "half_days", "absent_days",
            "net_salary", "bonus", "deductions",
            "pay_basis", "reference_rate", "mandatory_working_days",
            "is_paid", "paid_at", "notes",
            "staff_name", "staff_code", "staff_role", "staff_type",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "record_id", "month", "year", "present_days", "paid_leave_days",
            "unpaid_leave_days", "half_days", "absent_days",
            "created_at", "updated_at",
        ]

    def validate(self, attrs):
        sp = attrs.get("staff_profile", getattr(self.instance, "staff_profile", None))
        ss = attrs.get("support_staff", getattr(self.instance, "support_staff", None))
        if sp and ss:
            raise serializers.ValidationError("Link a salary record to either a staff profile or support staff, not both.")
        if not sp and not ss:
            raise serializers.ValidationError("A salary record must link to a staff member.")

        # Lab Technicians are only issued login credentials to mark attendance
        # and record their work — the hospital does not pay their salary
        # through this system, so no salary record may be created for them.
        if sp and sp.role == "Lab Technician":
            raise serializers.ValidationError(
                "Lab Technicians are not paid through the hospital's salary system."
            )

        # Salary figures are typed in manually by the manager and locked
        # once a record is marked paid, so a paid month can't be edited
        # out from under an already-issued payment.
        if self.instance and self.instance.is_paid:
            locked_fields = {"net_salary", "bonus", "deductions"} & set(attrs.keys())
            if locked_fields:
                raise serializers.ValidationError(
                    "This salary record is already marked paid — pay figures are locked."
                )
        return attrs

    def get_staff_name(self, obj):
        if obj.staff_profile:
            return _staff_profile_display_name(obj.staff_profile)
        if obj.support_staff:
            return obj.support_staff.full_name
        return None

    def get_staff_code(self, obj):
        if obj.staff_profile:
            return obj.staff_profile.staff_code
        if obj.support_staff:
            return obj.support_staff.staff_code
        return None

    def get_staff_role(self, obj):
        if obj.staff_profile:
            return obj.staff_profile.role
        if obj.support_staff:
            return obj.support_staff.role
        return None

    def get_staff_type(self, obj):
        if obj.staff_profile_id:
            return "staff_profile"
        if obj.support_staff_id:
            return "support_staff"
        return None

    def get_pay_basis(self, obj):
        _, basis = _reference_rate_and_basis(obj)
        return basis

    def get_reference_rate(self, obj):
        rate, _ = _reference_rate_and_basis(obj)
        return rate

    def get_mandatory_working_days(self, obj):
        return _mandatory_working_days(obj)


# ─────────────────────────────────────────────
# SALARY ENTRY
# ─────────────────────────────────────────────
class SalaryEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model  = SalaryEntry
        fields = ["id", "salary_record", "entry_type", "amount", "note", "created_at"]
        read_only_fields = ["id", "salary_record", "created_at"]


# ─────────────────────────────────────────────
# HOSPITAL EXPENSE
# ─────────────────────────────────────────────
class HospitalExpenseSerializer(serializers.ModelSerializer):
    added_by_name = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source="branch.name", read_only=True)

    class Meta:
        model  = HospitalExpense
        fields = [
            "expense_id", "branch", "branch_name", "title", "category", "amount",
            "date", "description", "receipt_number",
            "added_by", "added_by_name",
            "created_at", "updated_at",
        ]
        # ✅ FIX: "branch" was missing here — required model field, so
        # POST failed. Writable; view injects/enforces it (see
        # resolve_branch_for_write in ExpenseListView).
        read_only_fields = ["expense_id", "added_by", "created_at", "updated_at"]

    def get_added_by_name(self, obj):
        if obj.added_by:
            return obj.added_by.get_full_name() or obj.added_by.username
        return None


class OtherIncomeSerializer(serializers.ModelSerializer):
    added_by_name = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source="branch.name", read_only=True)

    class Meta:
        model  = OtherIncome
        fields = [
            "income_id", "branch", "branch_name", "title", "category", "amount",
            "date", "description", "receipt_number",
            "added_by", "added_by_name",
            "created_at", "updated_at",
        ]
        # ✅ FIX: same gap as HospitalExpenseSerializer — see its comment.
        read_only_fields = ["income_id", "added_by", "created_at", "updated_at"]

    def get_added_by_name(self, obj):
        if obj.added_by:
            return obj.added_by.get_full_name() or obj.added_by.username
        return None


# ─────────────────────────────────────────────
# DEALERS + CREDIT LEDGER
# ─────────────────────────────────────────────
class DealerSerializer(serializers.ModelSerializer):
    balance       = serializers.SerializerMethodField()
    pending_count = serializers.SerializerMethodField()
    added_by_name = serializers.SerializerMethodField()
    branch_name   = serializers.CharField(source="branch.name", read_only=True)

    class Meta:
        model  = Dealer
        fields = [
            "dealer_id", "branch", "branch_name", "name", "contact_person", "phone", "email", "address",
            "gst_number", "deals_in", "opening_balance",
            "is_active", "notes",
            "balance", "pending_count",
            "added_by", "added_by_name", "created_at", "updated_at",
        ]
        # ✅ FIX: "branch" was missing here — required model field, so
        # POST failed. Writable; view injects/enforces it (see
        # resolve_branch_for_write in DealerListCreateView).
        read_only_fields = ["dealer_id", "added_by", "created_at", "updated_at"]

    def validate_deals_in(self, value):
        try:
            validate_deals_in(value)
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.messages)
        return value

    def get_balance(self, obj):
        return float(obj.balance)

    def get_pending_count(self, obj):
        return obj.pending_count

    def get_added_by_name(self, obj):
        if obj.added_by:
            return obj.added_by.get_full_name() or obj.added_by.username
        return None


class DealerTransactionSerializer(serializers.ModelSerializer):
    dealer_name       = serializers.CharField(source="dealer.name", read_only=True)
    created_by_name    = serializers.SerializerMethodField()
    confirmed_by_name  = serializers.SerializerMethodField()
    source_label       = serializers.SerializerMethodField()
    is_overdue         = serializers.SerializerMethodField()
    is_settled         = serializers.SerializerMethodField()
    amount_paid        = serializers.SerializerMethodField()
    amount_due         = serializers.SerializerMethodField()

    class Meta:
        model  = DealerTransaction
        fields = [
            "transaction_id", "dealer", "dealer_name",
            "transaction_type", "settlement_method", "amount", "status",
            "source_model", "source_id", "source_label",
            "linked_transaction", "reference_number", "notes", "balance_after",
            "due_date", "is_overdue", "is_settled", "amount_paid", "amount_due",
            "created_by", "created_by_name", "confirmed_by", "confirmed_by_name",
            "created_at", "confirmed_at", "updated_at",
        ]
        read_only_fields = [
            "transaction_id", "dealer_name", "status", "balance_after",
            "created_by", "confirmed_by", "created_at", "confirmed_at", "updated_at",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # ✅ FIX: "dealer" was an unrestricted PrimaryKeyRelatedField —
        # a branch-scoped manager could log a manual transaction against
        # any branch's dealer by id. Narrow the choices to the caller's
        # own branch (group admins keep the full list).
        request = self.context.get("request")
        if request is not None:
            from authentication.utils import is_group_admin_user, get_user_branch
            if not is_group_admin_user(request.user):
                branch = get_user_branch(request.user)
                self.fields["dealer"].queryset = (
                    Dealer.objects.filter(branch=branch) if branch else Dealer.objects.none()
                )

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.get_full_name() or obj.created_by.username
        return None

    def get_confirmed_by_name(self, obj):
        if obj.confirmed_by:
            return obj.confirmed_by.get_full_name() or obj.confirmed_by.username
        return None

    def get_source_label(self, obj):
        return dict(DealerTransaction.SOURCE_MODEL_CHOICES).get(obj.source_model, obj.source_model)

    def get_is_overdue(self, obj):
        """True only for a CONFIRMED entry left as CREDIT whose due_date has
        passed and which hasn't been FULLY offset by matching entries yet.
        ✅ FIX: used to treat "any confirmed linked counterpart exists" as
        "not overdue", so a single ₹100 payment linked against a ₹150
        purchase due last month would hide the overdue flag entirely even
        though ₹50 of it is still unpaid and past due."""
        if not obj.due_date or obj.status != "CONFIRMED" or obj.settlement_method != "CREDIT":
            return False
        if self._confirmed_linked_total(obj) >= obj.amount:
            return False
        return obj.due_date < timezone.localdate()

    def _confirmed_linked_total(self, obj):
        """Sum of every CONFIRMED counterpart (PAYMENT/CASH_REFUND/etc.)
        that points back at this row via linked_transaction — i.e. how
        much of this entry has actually been settled so far, whether that
        happened via a single auto-paired leg or several partial ones.
        Cached on the instance since is_overdue/is_settled/amount_paid/
        amount_due each need it — without this, serializing a list of N
        transactions would run this query up to 4×N times instead of N."""
        cached = getattr(obj, "_dealer_txn_confirmed_linked_total", None)
        if cached is None:
            cached = obj.linked_from.filter(status="CONFIRMED").aggregate(t=Sum("amount"))["t"] or Decimal("0.00")
            obj._dealer_txn_confirmed_linked_total = cached
        return cached

    def get_amount_paid(self, obj):
        return float(self._confirmed_linked_total(obj))

    def get_amount_due(self, obj):
        """How much of this PURCHASE/CREDIT_NOTE still hasn't been
        matched by a confirmed settlement leg. Clamped at 0 — an
        over-settlement (paid more than the original amount) shouldn't
        show as a negative 'due' figure here."""
        if obj.status != "CONFIRMED" or obj.transaction_type not in ("PURCHASE", "CREDIT_NOTE"):
            return 0.0
        remaining = obj.amount - self._confirmed_linked_total(obj)
        return float(remaining) if remaining > 0 else 0.0

    def get_is_settled(self, obj):
        """True once this entry's CONFIRMED counterpart(s) — the
        auto-paired leg, or one or more manually-linked partial
        payments/refunds — add up to the FULL original amount, not just
        "some counterpart exists". A ₹100 payment linked to a ₹150
        purchase used to flip this straight to True (and hide the ₹50
        still owed) the moment any confirmed counterpart showed up at
        all, regardless of whether it actually covered the balance."""
        if obj.status != "CONFIRMED":
            return False
        return self._confirmed_linked_total(obj) >= obj.amount


class DealerTransactionFinalizeSerializer(serializers.Serializer):
    """Input for POST /manager/dealers/transactions/<id>/finalize/

    NOTE: transaction_type is deliberately NOT accepted here. Every
    transaction already carries a fixed nature — PURCHASE for a stock
    batch, CREDIT_NOTE for a return, or whatever was explicitly chosen
    when manually logging one — and that nature must never change at
    confirm time. Letting a manager "correct" it here used to silently
    convert e.g. a PURCHASE into a lone PAYMENT (discarding the fact
    that stock was actually received), which made the dealer's balance
    look like unexplained credit even though nothing was wrong with the
    real-world transaction. What manager CAN and should adjust here is
    how it was settled (settlement_method) — see auto_settle_payment
    below, which now auto-pairs both PURCHASE→PAID and
    CREDIT_NOTE→REFUND so each event still nets to zero without ever
    touching transaction_type.
    """
    action = serializers.ChoiceField(choices=["CONFIRM", "REJECT"])
    settlement_method = serializers.ChoiceField(
        choices=[c[0] for c in DealerTransaction.SETTLEMENT_CHOICES], required=False, allow_null=True,
    )
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=Decimal("0.01"))
    # How much is actually changing hands RIGHT NOW, as opposed to `amount`
    # above (which corrects the underlying purchase/return's true value).
    # Optional — omitted or equal to `amount` means "settled in full", the
    # existing behaviour. If it's LESS than `amount`, this is a genuine
    # partial payment: the purchase/return keeps its real amount (nothing
    # about what was bought/returned changes), only `paid_amount` is
    # auto-paired as the matching PAYMENT/CASH_REFUND leg, and the entry
    # is left open (settlement_method flipped to CREDIT) for the
    # remainder — see _finalize_dealer_transaction. Without this, editing
    # `amount` down to "how much I'm paying today" used to silently
    # rewrite the purchase itself to that lower figure and mark the whole
    # thing settled, losing track of what was still owed.
    paid_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=Decimal("0.01"))
    # Simple "paid now vs pending, pay later" front-end toggle. When set to
    # CREDIT, an optional due_date can be attached below so a left-as-credit
    # entry isn't just open-ended. Purely a convenience for the manager —
    # like settlement_method, it never drives the balance math.
    due_date = serializers.DateField(required=False, allow_null=True)
    linked_transaction_id = serializers.IntegerField(required=False, allow_null=True)
    reference_number = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    # If PURCHASE is confirmed with settlement_method=PAID, OR a CREDIT_NOTE
    # is confirmed with settlement_method=REFUND, also auto-log a matching
    # PAYMENT / CASH_REFUND transaction so the event nets to zero on the
    # ledger while still keeping both halves on record. Defaults to True.
    auto_settle_payment = serializers.BooleanField(required=False, default=True)


class DealerTransactionBulkFinalizeSerializer(serializers.Serializer):
    """Input for POST /manager/dealers/transactions/bulk-finalize/

    Same settlement fields as the single-transaction finalize above, plus
    the list of PENDING transaction ids being settled together as one
    batch ("bill"). `amount` and `linked_transaction_id` are deliberately
    NOT accepted here — each row keeps its own original amount, and each
    row is only ever auto-paired with its own settlement leg, never a
    shared one. See views._finalize_dealer_transaction / views.py.
    """
    transaction_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False,
    )
    action = serializers.ChoiceField(choices=["CONFIRM", "REJECT"])
    settlement_method = serializers.ChoiceField(
        choices=[c[0] for c in DealerTransaction.SETTLEMENT_CHOICES], required=False, allow_null=True,
    )
    due_date = serializers.DateField(required=False, allow_null=True)
    reference_number = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    auto_settle_payment = serializers.BooleanField(required=False, default=True)


class DealerTransactionScheduleUpdateSerializer(serializers.Serializer):
    """Input for POST /manager/dealers/transactions/<pk>/schedule/

    Lets a manager go back and edit the "soft" details of an entry —
    when it's due, its reference number, its notes — without touching
    amount, transaction_type, or status. Works on an entry in any status,
    since a due_date is just as useful to correct on something already
    confirmed as credit as it is before confirming.
    """
    due_date = serializers.DateField(required=False, allow_null=True)
    reference_number = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)


# ═══════════════════════════════════════════════════════
# PUBLIC WEBSITE — manager-curated content serializers
# ═══════════════════════════════════════════════════════

class FlexibleJSONField(serializers.JSONField):
    """A JSONField that accepts a value either as a native JSON type (a
    normal application/json request body) or as a JSON-encoded string (a
    multipart/form-data field — browsers can't send nested structures as
    real form fields, so the frontend JSON.stringifies list/object values
    before appending them to FormData). The doctor website profile
    endpoint takes multipart because of the photo upload, so its list
    fields (area_of_expertise, education, awards, languages_known) need
    this rather than the plain JSONField, which only parses strings when
    binary=True — and binary=True would break a normal JSON body where
    the value already arrives as a real list."""

    def to_internal_value(self, data):
        if isinstance(data, (bytes, str)):
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                self.fail("invalid")
        return super().to_internal_value(data)


class DoctorWebsiteProfileSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField(read_only=True)
    specialty = serializers.SerializerMethodField(read_only=True)
    qualifications = serializers.SerializerMethodField(read_only=True)
    # ✅ FIX: "doctor" (singular write field) replaces the old direct
    # `doctor` model field now that the model holds `doctors` (M2M).
    # Create takes exactly one starting DoctorProfile -- attaching a
    # *second* branch to an already-existing profile goes through
    # WebsiteDoctorAttachBranchView instead, not through this field, so
    # a stray PATCH here can't silently detach/replace branches already
    # on the profile.
    doctor = serializers.PrimaryKeyRelatedField(
        queryset=EmrDoctorProfile.objects.all(), write_only=True,
        help_text="DoctorProfile to attach on create. Ignored on update -- "
                  "use the attach-branch endpoint to add further branches.",
    )
    # Read-only view of every branch this profile currently covers.
    branches = serializers.SerializerMethodField(read_only=True)
    # This endpoint takes multipart/form-data (because of the photo
    # upload), so these four list fields need FlexibleJSONField rather
    # than the plain auto-generated JSONField — see its docstring above.
    area_of_expertise = FlexibleJSONField(required=False)
    education = FlexibleJSONField(required=False)
    awards = FlexibleJSONField(required=False)
    languages_known = FlexibleJSONField(required=False)
    # Write-only flag: PATCH {"remove_photo": true} explicitly clears an
    # existing photo. Plain multipart form semantics can't distinguish
    # "no new file attached, leave the old one" from "clear it" — DRF's
    # ImageField.to_internal_value() rejects an empty-string value rather
    # than treating it as "unset" — so this flag is the explicit signal.
    remove_photo = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = DoctorWebsiteProfile
        fields = [
            "id", "doctor", "branches", "name", "specialty", "qualifications",
            "is_published", "bio", "photo", "display_order",
            "designation", "experience_summary", "area_of_expertise",
            "education", "awards", "languages_known",
            "remove_photo", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_doctor(self, doctor):
        DoctorWebsiteProfile.validate_doctor_membership(doctor)
        return doctor

    def create(self, validated_data):
        validated_data.pop("remove_photo", None)  # meaningless on create — nothing to remove yet
        doctor = validated_data.pop("doctor")
        instance = DoctorWebsiteProfile.objects.create(**validated_data)
        instance.doctors.add(doctor)
        return instance

    def update(self, instance, validated_data):
        # "doctor" is write_only and create-only by design (see the field
        # docstring above) -- drop it silently on update rather than
        # erroring, so a client that always sends the full payload back
        # on PATCH doesn't get rejected.
        validated_data.pop("doctor", None)
        if validated_data.pop("remove_photo", False) and instance.photo:
            instance.photo.delete(save=False)
            instance.photo = None
        return super().update(instance, validated_data)

    def get_name(self, obj):
        return obj.get_name()

    def get_specialty(self, obj):
        doctor = obj.primary_doctor()
        return doctor.specialty.name if doctor and doctor.specialty else ""

    def get_qualifications(self, obj):
        # DoctorProfile (EMR) has no dedicated qualifications field. The
        # public-facing "Education" list on the website profile itself is
        # the real source for this now — comma-joined for the older
        # single-string "qualifications" shape some callers still expect.
        return ", ".join(obj.education) if obj.education else ""

    def get_branches(self, obj):
        return obj.branch_summaries()


class DoctorWeeklyAvailabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = DoctorWeeklyAvailability
        fields = [
            "id", "doctor", "day_of_week", "start_time", "end_time",
            "slot_duration_minutes", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class DoctorAvailabilityExceptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DoctorAvailabilityException
        fields = [
            "id", "doctor", "date", "is_unavailable", "start_time", "end_time",
            "slot_duration_minutes", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class PatientQuerySerializer(serializers.ModelSerializer):
    preferred_branch_name = serializers.CharField(source="preferred_branch.name", read_only=True, default=None)

    class Meta:
        model = PatientQuery
        fields = [
            "id", "query_type", "name", "phone", "email", "message",
            "preferred_branch", "preferred_branch_name", "status",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "query_type", "name", "phone", "email", "message",
            "preferred_branch", "preferred_branch_name", "created_at", "updated_at",
        ]


class TestimonialSerializer(serializers.ModelSerializer):
    remove_photo = serializers.BooleanField(write_only=True, required=False, default=False)
    specialty_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Testimonial
        fields = [
            "id", "patient_name", "designation", "review", "photo", "rating",
            "specialty", "specialty_name",
            "is_active", "is_featured", "display_order", "remove_photo",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_specialty_name(self, obj):
        return obj.specialty.name if obj.specialty_id else None

    def update(self, instance, validated_data):
        if validated_data.pop("remove_photo", False) and instance.photo:
            instance.photo.delete(save=False)
            instance.photo = None
        return super().update(instance, validated_data)

    def create(self, validated_data):
        validated_data.pop("remove_photo", None)  # meaningless on create — nothing to remove yet
        return super().create(validated_data)


class YoutubeVideoSerializer(serializers.ModelSerializer):
    doctor_name = serializers.SerializerMethodField(read_only=True)
    specialty_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = YoutubeVideo
        fields = [
            "id", "youtube_url", "video_id", "thumbnail_url", "title", "description",
            "doctor", "doctor_name", "specialty", "specialty_name",
            "is_active", "is_featured", "display_order",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "video_id", "thumbnail_url", "created_at", "updated_at"]

    def get_doctor_name(self, obj):
        if not obj.doctor_id:
            return None
        try:
            return obj.doctor.get_name()
        except Exception:
            return None

    def get_specialty_name(self, obj):
        return obj.specialty.name if obj.specialty_id else None

    def validate_youtube_url(self, value):
        from .models import extract_youtube_video_id
        if not extract_youtube_video_id(value):
            raise serializers.ValidationError("That doesn't look like a valid YouTube URL.")
        return value


class InstagramPostSerializer(serializers.ModelSerializer):
    remove_thumbnail = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = InstagramPost
        fields = [
            "id", "instagram_url", "caption", "thumbnail", "is_active", "is_featured",
            "display_order", "remove_thumbnail", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_instagram_url(self, value):
        from .models import is_valid_instagram_url
        if not is_valid_instagram_url(value):
            raise serializers.ValidationError("That doesn't look like a valid Instagram post/reel URL.")
        return value

    def update(self, instance, validated_data):
        if validated_data.pop("remove_thumbnail", False) and instance.thumbnail:
            instance.thumbnail.delete(save=False)
            instance.thumbnail = None
        return super().update(instance, validated_data)

    def create(self, validated_data):
        validated_data.pop("remove_thumbnail", None)  # meaningless on create — nothing to remove yet
        return super().create(validated_data)


class FacebookPostSerializer(serializers.ModelSerializer):
    remove_thumbnail = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = FacebookPost
        fields = [
            "id", "facebook_url", "caption", "thumbnail", "is_active", "is_featured",
            "display_order", "remove_thumbnail", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_facebook_url(self, value):
        from .models import is_valid_facebook_url
        if not is_valid_facebook_url(value):
            raise serializers.ValidationError("That doesn't look like a valid Facebook URL.")
        return value

    def update(self, instance, validated_data):
        if validated_data.pop("remove_thumbnail", False) and instance.thumbnail:
            instance.thumbnail.delete(save=False)
            instance.thumbnail = None
        return super().update(instance, validated_data)

    def create(self, validated_data):
        validated_data.pop("remove_thumbnail", None)  # meaningless on create — nothing to remove yet
        return super().create(validated_data)


class MediaEventSerializer(serializers.ModelSerializer):
    remove_cover_image = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = MediaEvent
        fields = [
            "id", "title", "slug", "excerpt", "body", "cover_image", "remove_cover_image",
            "event_date", "is_active", "is_featured", "display_order", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "slug", "created_at", "updated_at"]

    def update(self, instance, validated_data):
        if validated_data.pop("remove_cover_image", False) and instance.cover_image:
            instance.cover_image.delete(save=False)
            instance.cover_image = None
        return super().update(instance, validated_data)

    def create(self, validated_data):
        validated_data.pop("remove_cover_image", None)  # meaningless on create — nothing to remove yet
        return super().create(validated_data)


class GalleryImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = GalleryImage
        fields = ["id", "image", "caption", "is_active", "display_order", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]



# ── Specialty / Treatment / Blog (manager CRUD) ──────────────────────

class SpecialtySubSerializer(serializers.ModelSerializer):
    """Compact shape for a specialty nested as someone else's
    sub_specialties — deliberately not recursive (no further-nested
    sub_specialties of its own) to keep this a flat one-level-deep list."""

    class Meta:
        model = Specialty
        fields = ["id", "name", "slug", "short_description", "is_published", "display_order"]


class SpecialtySerializer(serializers.ModelSerializer):
    sub_specialties = SpecialtySubSerializer(many=True, read_only=True)
    parent_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Specialty
        fields = [
            "id", "name", "slug", "parent", "parent_name", "sub_specialties",
            "short_description", "long_description", "hero_image",
            "is_published", "display_order", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_parent_name(self, obj):
        return obj.parent.name if obj.parent_id else None


class SpecialtySectionSerializer(serializers.ModelSerializer):
    specialty_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = SpecialtySection
        fields = [
            "id", "specialty", "specialty_name", "heading", "intro", "items", "subsections",
            "is_active", "display_order", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_specialty_name(self, obj):
        return obj.specialty.name if obj.specialty_id else None


class TreatmentSerializer(serializers.ModelSerializer):
    specialty_name = serializers.SerializerMethodField(read_only=True)
    # This endpoint takes multipart/form-data (because of the hero_image
    # upload), so `sections` needs FlexibleJSONField rather than the
    # plain auto-generated JSONField — see its docstring above. The
    # frontend JSON.stringifies the list of {heading, description,
    # items, alphabet_list} blocks before appending it to FormData.
    sections = FlexibleJSONField(required=False)
    # Write-only flag: PATCH/POST with remove_hero_image=true explicitly
    # clears an existing hero image. Plain multipart form semantics
    # can't distinguish "no new file attached, leave the old one" from
    # "clear it" — same pattern as Blog.remove_cover_image below.
    remove_hero_image = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Treatment
        fields = [
            "id", "specialty", "specialty_name", "name", "slug", "kind", "summary",
            "hero_image", "sections", "remove_hero_image",
            "is_active", "display_order", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "slug", "created_at", "updated_at"]

    def get_specialty_name(self, obj):
        return obj.specialty.name if obj.specialty_id else None

    def update(self, instance, validated_data):
        if validated_data.pop("remove_hero_image", False) and instance.hero_image:
            instance.hero_image.delete(save=False)
            instance.hero_image = None
        return super().update(instance, validated_data)

    def create(self, validated_data):
        validated_data.pop("remove_hero_image", None)
        return super().create(validated_data)


class BlogSerializer(serializers.ModelSerializer):
    specialty_name = serializers.SerializerMethodField(read_only=True)
    author_doctor_name = serializers.SerializerMethodField(read_only=True)
    remove_cover_image = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Blog
        fields = [
            "id", "specialty", "specialty_name", "author_doctor", "author_doctor_name",
            "title", "slug", "excerpt", "body", "cover_image", "remove_cover_image",
            "is_published", "display_order", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "slug", "created_at", "updated_at"]

    def get_specialty_name(self, obj):
        return obj.specialty.name if obj.specialty_id else None

    def get_author_doctor_name(self, obj):
        if not obj.author_doctor_id:
            return None
        try:
            return obj.author_doctor.get_name()
        except Exception:
            return None

    def update(self, instance, validated_data):
        if validated_data.pop("remove_cover_image", False) and instance.cover_image:
            instance.cover_image.delete(save=False)
            instance.cover_image = None
        return super().update(instance, validated_data)

    def create(self, validated_data):
        validated_data.pop("remove_cover_image", None)
        return super().create(validated_data)


class ReorderSerializer(serializers.Serializer):
    """Generic body for bulk display_order updates: {"order": [3, 1, 2, ...]}
    — a list of primary keys in the new desired order. Used by the
    testimonials/youtube-videos/instagram-posts 'reorder' actions."""
    order = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)


# ═══════════════════════════════════════════════════════
# PUBLIC WEBSITE — read-only serializers (compact shape for
# the unauthenticated marketing site) and public write serializers
# (prebooking / contact form).
# ═══════════════════════════════════════════════════════

class PublicDoctorSerializer(serializers.ModelSerializer):
    """Shape consumed by the public site's Doctors/BookAppointment cards:
    {id, name, specialty, qualifications, bio, photo, branches}. See
    PublicDoctorDetailSerializer for the full "View Profile" page shape.

    ✅ FIX: `id` now returns the DoctorWebsiteProfile's own pk, not a
    DoctorProfile pk. Under the old OneToOne there was exactly one
    DoctorProfile per profile, so the two ids were interchangeable and
    doctor_id doubled as the public card id. Under the M2M a profile can
    have several DoctorProfile ids (one per branch) -- the profile itself
    is the one stable public identity, so it's the one exposed as `id`.
    `branches` lets the frontend show "Available at TVM, KOL" on the card
    and pick which branch's doctor_id to book against."""
    name = serializers.SerializerMethodField()
    specialty = serializers.SerializerMethodField()
    qualifications = serializers.SerializerMethodField()
    photo = serializers.SerializerMethodField()
    branches = serializers.SerializerMethodField()

    class Meta:
        model = DoctorWebsiteProfile
        fields = ["id", "name", "specialty", "qualifications", "bio", "photo", "branches"]

    def get_name(self, obj):
        return obj.get_name()

    def get_specialty(self, obj):
        doctor = obj.primary_doctor()
        return doctor.specialty.name if doctor and doctor.specialty else ""

    def get_qualifications(self, obj):
        return ", ".join(obj.education) if obj.education else ""

    def get_photo(self, obj):
        if not obj.photo:
            return None
        request = self.context.get("request")
        url = obj.photo.url
        return request.build_absolute_uri(url) if request else url

    def get_branches(self, obj):
        return obj.branch_summaries()


class PublicDoctorDetailSerializer(PublicDoctorSerializer):
    """Shape consumed by the public site's per-doctor "View Profile" page
    (Work Experience / Area of Expertise / Education / Awards / Languages
    Known / Timing sections). Timing is computed from this doctor's
    DoctorWeeklyAvailability rows rather than stored here, so the profile
    page and the booking calendar can never disagree about a doctor's
    recurring hours."""
    designation = serializers.SerializerMethodField()
    experience_summary = serializers.SerializerMethodField()
    timing = serializers.SerializerMethodField()

    class Meta(PublicDoctorSerializer.Meta):
        fields = PublicDoctorSerializer.Meta.fields + [
            "designation", "experience_summary", "area_of_expertise",
            "education", "awards", "languages_known", "timing",
        ]

    def get_designation(self, obj):
        return obj.designation or self.get_specialty(obj)

    def get_experience_summary(self, obj):
        return obj.experience_summary or ""

    def get_timing(self, obj):
        """7-day duty schedule starting today (not a generic Monday-Sunday
        table) — each entry is a real calendar date, broken down per
        branch. Uses the exact same precedence as
        _compute_available_slots() so this display can never disagree
        with what's actually bookable:
          • a DoctorAvailabilityException on that date wins outright —
            is_unavailable=True means an explicit day off (marked as
            such, distinct from "no schedule configured"), an explicit
            start/end means exactly that one window.
          • no exception for that date -> fall back to every
            DoctorWeeklyAvailability window for that weekday (a doctor
            can have more than one window per day).
          • neither -> no hours that day, and no explicit day-off either
            (just nothing scheduled).

        ✅ FIX: aggregated across every branch DoctorProfile attached to
        this website profile (previously a single doctor_id, back when
        the model was OneToOne). A doctor at two branches now shows one
        "hours" list per branch for each date rather than a single
        undifferentiated block — e.g. a visitor booking Tuesday sees
        "TVM Branch: 10:00 AM - 1:00 PM" and "KOL Branch: 2:00 PM - 5:00
        PM" as distinct options, not one merged/ambiguous window.
        """
        from datetime import timedelta

        branch_doctors = list(
            obj.doctors.select_related("staff", "staff__branch").order_by("staff__branch__name")
        )
        today = timezone.localdate()
        horizon_end = today + timedelta(days=7)  # exclusive

        def fmt(t):
            return t.strftime("%I:%M %p").lstrip("0")

        # Pre-fetch per branch-doctor so a doctor at N branches costs
        # 2N queries total, not 2N*7.
        per_doctor = {}
        for bd in branch_doctors:
            branch = bd.staff.branch if getattr(bd, "staff", None) and bd.staff.branch_id else None
            exceptions_by_date = {
                row.date: row
                for row in DoctorAvailabilityException.objects.filter(
                    doctor_id=bd.pk, date__gte=today, date__lt=horizon_end,
                )
            }
            weekly_windows_by_weekday = {}
            for row in DoctorWeeklyAvailability.objects.filter(doctor_id=bd.pk).order_by("day_of_week", "start_time"):
                weekly_windows_by_weekday.setdefault(row.day_of_week, []).append(row)
            per_doctor[bd.pk] = {
                "branch_id": branch.pk if branch else None,
                "branch_name": branch.name if branch else "",
                "exceptions_by_date": exceptions_by_date,
                "weekly_windows_by_weekday": weekly_windows_by_weekday,
            }

        result = []
        for offset in range(7):
            d = today + timedelta(days=offset)
            branch_blocks = []
            for bd in branch_doctors:
                info = per_doctor[bd.pk]
                exception = info["exceptions_by_date"].get(d)
                is_day_off = False
                if exception:
                    if exception.is_unavailable:
                        hours = None
                        is_day_off = True
                    else:
                        hours = [f"{fmt(exception.start_time)} - {fmt(exception.end_time)}"]
                else:
                    windows = info["weekly_windows_by_weekday"].get(d.weekday(), [])
                    hours = [f"{fmt(w.start_time)} - {fmt(w.end_time)}" for w in windows] or None
                if hours or is_day_off:
                    branch_blocks.append({
                        "doctor_id": bd.pk,
                        "branch_id": info["branch_id"],
                        "branch_name": info["branch_name"],
                        "is_day_off": is_day_off,
                        "hours": hours,
                    })
            result.append({
                "date": d.isoformat(),
                "day": d.strftime("%A"),
                "is_today": offset == 0,
                "branches": branch_blocks,
            })
        return result


class PublicTestimonialSerializer(serializers.ModelSerializer):
    photo = serializers.SerializerMethodField()

    class Meta:
        model = Testimonial
        fields = ["id", "patient_name", "designation", "review", "photo", "rating", "is_featured"]

    def get_photo(self, obj):
        if not obj.photo:
            return None
        request = self.context.get("request")
        url = obj.photo.url
        return request.build_absolute_uri(url) if request else url


class PublicYoutubeVideoSerializer(serializers.ModelSerializer):
    class Meta:
        model = YoutubeVideo
        fields = ["id", "video_id", "thumbnail_url", "title", "description", "is_featured"]


class PublicInstagramPostSerializer(serializers.ModelSerializer):
    thumbnail = serializers.SerializerMethodField()

    class Meta:
        model = InstagramPost
        fields = ["id", "instagram_url", "caption", "thumbnail", "is_featured"]

    def get_thumbnail(self, obj):
        if not obj.thumbnail:
            return None
        request = self.context.get("request")
        url = obj.thumbnail.url
        return request.build_absolute_uri(url) if request else url


class PublicFacebookPostSerializer(serializers.ModelSerializer):
    thumbnail = serializers.SerializerMethodField()

    class Meta:
        model = FacebookPost
        fields = ["id", "facebook_url", "caption", "thumbnail", "is_featured"]

    def get_thumbnail(self, obj):
        if not obj.thumbnail:
            return None
        request = self.context.get("request")
        url = obj.thumbnail.url
        return request.build_absolute_uri(url) if request else url


class PublicMediaEventSerializer(serializers.ModelSerializer):
    """Compact card shape for the public Media & Events listing."""
    cover_image = serializers.SerializerMethodField()

    class Meta:
        model = MediaEvent
        fields = ["id", "title", "slug", "excerpt", "cover_image", "event_date", "created_at"]

    def get_cover_image(self, obj):
        if not obj.cover_image:
            return None
        request = self.context.get("request")
        url = obj.cover_image.url
        return request.build_absolute_uri(url) if request else url


class PublicMediaEventDetailSerializer(PublicMediaEventSerializer):
    """Full shape for a single Media & Events item's public detail page."""
    class Meta(PublicMediaEventSerializer.Meta):
        fields = PublicMediaEventSerializer.Meta.fields + ["body"]


class PublicGalleryImageSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()

    class Meta:
        model = GalleryImage
        fields = ["id", "image", "caption"]

    def get_image(self, obj):
        request = self.context.get("request")
        url = obj.image.url
        return request.build_absolute_uri(url) if request else url


class ManagerBranchWebsiteSerializer(serializers.ModelSerializer):
    """Manager-facing READ shape for one branch's Locations-page content
    -- Website Management's "Locations & Contact" tab. Keyed by Branch
    (not BranchWebsiteProfile) so every branch the caller manages shows
    up here even before a profile row exists yet, with `has_profile:
    false` telling the frontend to render a "not set up" state instead
    of a 404. name/code/address/phone are the operational Branch fields
    -- read-only here; edited from Admin > Branches, not from Website
    Management. See BranchWebsiteProfileContentSerializer for the write
    side (WebsiteBranchDetailView.patch upserts the profile row)."""
    branch_id = serializers.IntegerField(source="pk", read_only=True)
    has_profile = serializers.SerializerMethodField()
    profile_id = serializers.SerializerMethodField()
    is_published = serializers.SerializerMethodField()
    branch_type = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    highlights = serializers.SerializerMethodField()
    email = serializers.SerializerMethodField()
    hours_text = serializers.SerializerMethodField()
    map_url = serializers.SerializerMethodField()
    photo = serializers.SerializerMethodField()
    display_order = serializers.SerializerMethodField()

    class Meta:
        model = Branch
        fields = [
            "branch_id", "name", "code", "address", "phone", "is_active",
            "has_profile", "profile_id", "is_published", "branch_type", "description",
            "highlights", "email", "hours_text", "map_url", "photo", "display_order",
        ]

    def _profile(self, obj):
        # getattr(..., None) is safe here: Django's reverse-OneToOne
        # descriptor raises a RelatedObjectDoesNotExist that subclasses
        # AttributeError specifically so getattr/hasattr work like this.
        return getattr(obj, "website_profile", None)

    def get_has_profile(self, obj):
        return self._profile(obj) is not None

    def get_profile_id(self, obj):
        profile = self._profile(obj)
        return profile.pk if profile else None

    def get_is_published(self, obj):
        profile = self._profile(obj)
        return profile.is_published if profile else False

    def get_branch_type(self, obj):
        profile = self._profile(obj)
        return profile.branch_type if profile else "HOSPITAL"

    def get_description(self, obj):
        profile = self._profile(obj)
        return profile.description if profile else ""

    def get_highlights(self, obj):
        profile = self._profile(obj)
        return profile.highlights if profile else []

    def get_email(self, obj):
        profile = self._profile(obj)
        return profile.email if profile else ""

    def get_hours_text(self, obj):
        profile = self._profile(obj)
        return profile.hours_text if profile else ""

    def get_map_url(self, obj):
        profile = self._profile(obj)
        return profile.map_url if profile else ""

    def get_photo(self, obj):
        profile = self._profile(obj)
        if not profile or not profile.photo:
            return None
        request = self.context.get("request")
        url = profile.photo.url
        return request.build_absolute_uri(url) if request else url

    def get_display_order(self, obj):
        profile = self._profile(obj)
        return profile.display_order if profile else 0


class BranchWebsiteProfileContentSerializer(serializers.ModelSerializer):
    """Manager-facing WRITE shape for BranchWebsiteProfile itself. Used
    only from WebsiteBranchDetailView.patch, which get_or_create's the
    profile row first (a branch has none until a manager fills something
    in) then partially updates it with this. Same multipart-safe list
    field and explicit remove_photo flag as DoctorWebsiteProfileSerializer,
    for the same reason (photo upload forces multipart/form-data, and
    plain form semantics can't express "clear the existing photo").
    Deliberately excludes `branch` -- which branch this profile belongs
    to is fixed by the URL, never by the request body."""
    highlights = FlexibleJSONField(required=False)
    remove_photo = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = BranchWebsiteProfile
        fields = [
            "is_published", "branch_type", "description", "highlights", "email",
            "hours_text", "map_url", "photo", "remove_photo",
            "display_order", "updated_at",
        ]
        read_only_fields = ["updated_at"]

    def update(self, instance, validated_data):
        if validated_data.pop("remove_photo", False) and instance.photo:
            instance.photo.delete(save=False)
            instance.photo = None
        return super().update(instance, validated_data)


class PublicBranchChoiceSerializer(serializers.ModelSerializer):
    """Bare {branch_id, name} shape for the Contact form's "Hospitals"
    selector. Deliberately NOT PublicBranchSerializer -- that one is
    gated on a *published* BranchWebsiteProfile (marketing content ready
    to show), but a visitor should be able to route an enquiry to any
    operationally active branch whether or not its Locations page copy
    is finished yet. See PublicBranchChoicesListView."""
    class Meta:
        model = Branch
        fields = ["branch_id", "name"]


class PublicBranchSerializer(serializers.ModelSerializer):
    """Card shape for the public /branches/ ("Our Locations") listing.

    Backed by administration.Branch (name/code/address/phone -- the
    operational fields that already exist and are true regardless of
    website publishing) plus its BranchWebsiteProfile (description/
    highlights/email/hours/map link/photo -- the public-content layer a
    manager fills in and publishes independently). PublicBranchListView
    only ever serializes branches that have a *published* profile, so
    `website_profile` is guaranteed present here even though it's
    optional at the model level.
    """
    email = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    highlights = serializers.SerializerMethodField()
    hours_text = serializers.SerializerMethodField()
    map_url = serializers.SerializerMethodField()
    photo = serializers.SerializerMethodField()
    branch_type = serializers.SerializerMethodField()

    class Meta:
        model = Branch
        fields = [
            "branch_id", "name", "code", "address", "phone",
            "email", "description", "highlights", "hours_text", "map_url", "photo",
            "branch_type",
        ]

    def get_email(self, obj):
        return obj.website_profile.email

    def get_description(self, obj):
        return obj.website_profile.description

    def get_highlights(self, obj):
        return obj.website_profile.highlights

    def get_hours_text(self, obj):
        return obj.website_profile.hours_text

    def get_map_url(self, obj):
        return obj.website_profile.map_url

    def get_branch_type(self, obj):
        return obj.website_profile.branch_type

    def get_photo(self, obj):
        photo = obj.website_profile.photo
        if not photo:
            return None
        request = self.context.get("request")
        url = photo.url
        return request.build_absolute_uri(url) if request else url


class PublicTreatmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Treatment
        fields = ["id", "name", "slug", "kind", "summary"]


class PublicTreatmentDetailSerializer(PublicTreatmentSerializer):
    """Full detail-page payload for a single disease/procedure: itself,
    its hero image, its structured body (`sections`, same normalized
    shape as SpecialtySection.subsections_list() — a list of
    {heading, description, items[], alphabet_list[]} blocks), plus the
    parent specialty's name/slug so the frontend can build the
    breadcrumb and link back to the department page."""
    hero_image = serializers.SerializerMethodField()
    sections = serializers.SerializerMethodField()
    specialty_name = serializers.SerializerMethodField()
    specialty_slug = serializers.SerializerMethodField()
    related = serializers.SerializerMethodField()

    class Meta(PublicTreatmentSerializer.Meta):
        fields = PublicTreatmentSerializer.Meta.fields + [
            "hero_image", "sections", "specialty_name", "specialty_slug", "related",
        ]

    def get_hero_image(self, obj):
        if not obj.hero_image:
            return None
        request = self.context.get("request")
        url = obj.hero_image.url
        return request.build_absolute_uri(url) if request else url

    def get_sections(self, obj):
        return obj.sections_list()

    def get_specialty_name(self, obj):
        return obj.specialty.name if obj.specialty_id else None

    def get_specialty_slug(self, obj):
        return obj.specialty.slug if obj.specialty_id else None

    def get_related(self, obj):
        # Other active diseases/procedures under the same specialty, for
        # the "Diseases and Treatments" list at the bottom of the detail
        # page (reference site shows a two-column list of sibling
        # entries plus a "View All" link back to the specialty page).
        if not obj.specialty_id:
            return []
        qs = (
            Treatment.objects
            .filter(specialty_id=obj.specialty_id, is_active=True)
            .exclude(pk=obj.pk)
            .order_by("display_order", "name")[:6]
        )
        return PublicTreatmentSerializer(qs, many=True).data


class PublicSpecialtySectionSerializer(serializers.ModelSerializer):
    """Public shape for a 'Why Choose Us?' / 'Diseases and Treatments'
    / 'Available Facilities and Equipment' style content block — `items`
    is split into a list here so the frontend doesn't have to parse
    newlines itself."""
    items = serializers.SerializerMethodField()
    subsections = serializers.SerializerMethodField()

    class Meta:
        model = SpecialtySection
        fields = ["id", "heading", "intro", "items", "subsections"]

    def get_items(self, obj):
        return obj.items_list()

    def get_subsections(self, obj):
        return obj.subsections_list()


class PublicSpecialtySubSerializer(serializers.ModelSerializer):
    """Compact card shape for a sub-specialty nested under a top-level
    one in the public /specialities/ listing."""
    hero_image = serializers.SerializerMethodField()

    class Meta:
        model = Specialty
        fields = ["id", "name", "slug", "short_description", "hero_image", "display_order"]

    def get_hero_image(self, obj):
        if not obj.hero_image:
            return None
        request = self.context.get("request")
        url = obj.hero_image.url
        return request.build_absolute_uri(url) if request else url


class PublicSpecialtySerializer(serializers.ModelSerializer):
    """Card shape for the public /specialities/ listing: top-level
    specialties with their published sub_specialties nested."""
    hero_image = serializers.SerializerMethodField()
    sub_specialties = serializers.SerializerMethodField()

    class Meta:
        model = Specialty
        fields = ["id", "name", "slug", "short_description", "hero_image", "display_order", "sub_specialties"]

    def get_hero_image(self, obj):
        if not obj.hero_image:
            return None
        request = self.context.get("request")
        url = obj.hero_image.url
        return request.build_absolute_uri(url) if request else url

    def get_sub_specialties(self, obj):
        published_subs = [s for s in obj.sub_specialties.all() if s.is_published]
        return PublicSpecialtySubSerializer(published_subs, many=True, context=self.context).data


class PublicSpecialtyDetailSerializer(PublicSpecialtySerializer):
    """Full detail-page payload for one specialty: itself, its published
    sub-specialties, its procedures/diseases, the doctors/testimonials/
    youtube-videos/blogs scoped to it (or any of its sub-specialties) —
    everything a specialty detail page needs in one request."""
    long_description = serializers.CharField()
    content_sections = serializers.SerializerMethodField()
    procedures = serializers.SerializerMethodField()
    doctors = serializers.SerializerMethodField()
    testimonials = serializers.SerializerMethodField()
    youtube_videos = serializers.SerializerMethodField()
    blogs = serializers.SerializerMethodField()

    class Meta(PublicSpecialtySerializer.Meta):
        fields = PublicSpecialtySerializer.Meta.fields + [
            "long_description", "content_sections", "procedures", "doctors",
            "testimonials", "youtube_videos", "blogs",
        ]

    def _specialty_and_sub_ids(self, obj):
        return [obj.pk] + [s.pk for s in obj.sub_specialties.all() if s.is_published]

    def get_content_sections(self, obj):
        # Deliberately scoped to this specialty only (not its sub-specialties)
        # — unlike procedures/doctors/etc., these blocks are page-specific
        # prose, so a sub-specialty's own sections shouldn't leak onto its
        # parent's page.
        qs = obj.content_sections.filter(is_active=True).order_by("display_order", "id")
        return PublicSpecialtySectionSerializer(qs, many=True).data

    def get_procedures(self, obj):
        ids = self._specialty_and_sub_ids(obj)
        qs = Treatment.objects.filter(specialty_id__in=ids, is_active=True).order_by("display_order", "name")
        return PublicTreatmentSerializer(qs, many=True).data

    def get_doctors(self, obj):
        ids = self._specialty_and_sub_ids(obj)
        # ✅ FIX: was `doctor__specialty_id__in=ids` / select_related("doctor", ...)
        # -- both referenced the old singular `doctor` OneToOne field,
        # which no longer exists now that DoctorWebsiteProfile.doctors is
        # an M2M (see that model's docstring). Filtering/joining through
        # doctors__specialty_id instead; .distinct() because the M2M join
        # can otherwise return the same profile once per matching branch.
        qs = (
            DoctorWebsiteProfile.objects
            .filter(is_published=True, doctors__specialty_id__in=ids)
            .prefetch_related("doctors", "doctors__staff", "doctors__staff__user")
            .distinct()
            .order_by("display_order")
        )
        return PublicDoctorSerializer(qs, many=True, context=self.context).data

    def get_testimonials(self, obj):
        ids = self._specialty_and_sub_ids(obj)
        qs = Testimonial.objects.filter(is_active=True, specialty_id__in=ids).order_by("display_order")
        return PublicTestimonialSerializer(qs, many=True, context=self.context).data

    def get_youtube_videos(self, obj):
        ids = self._specialty_and_sub_ids(obj)
        # ✅ FIX: was `doctor__specialty_id__in=ids` -- valid when
        # YoutubeVideo.doctor pointed straight at DoctorProfile (which
        # has specialty_id), but it now points at DoctorWebsiteProfile
        # (see that field's docstring), which has no specialty of its
        # own -- specialty lives one hop further in, on each attached
        # DoctorProfile. `doctor__doctors__specialty_id` reaches through
        # doctor (FK to DoctorWebsiteProfile) -> doctors (M2M to
        # DoctorProfile) -> specialty_id.
        qs = (
            YoutubeVideo.objects
            .filter(is_active=True)
            .filter(Q(specialty_id__in=ids) | Q(doctor__doctors__specialty_id__in=ids))
            .distinct()
            .order_by("display_order")
        )
        return PublicYoutubeVideoSerializer(qs, many=True).data

    def get_blogs(self, obj):
        ids = self._specialty_and_sub_ids(obj)
        qs = Blog.objects.filter(is_published=True, specialty_id__in=ids).order_by("display_order", "-created_at")
        return PublicBlogSerializer(qs, many=True, context=self.context).data


class PublicBlogSerializer(serializers.ModelSerializer):
    """Compact card shape for the public blog listing."""
    cover_image = serializers.SerializerMethodField()
    specialty_name = serializers.SerializerMethodField()
    author_doctor_name = serializers.SerializerMethodField()

    class Meta:
        model = Blog
        fields = [
            "id", "title", "slug", "excerpt", "cover_image",
            "specialty_name", "author_doctor_name", "created_at",
        ]

    def get_cover_image(self, obj):
        if not obj.cover_image:
            return None
        request = self.context.get("request")
        url = obj.cover_image.url
        return request.build_absolute_uri(url) if request else url

    def get_specialty_name(self, obj):
        return obj.specialty.name if obj.specialty_id else None

    def get_author_doctor_name(self, obj):
        if not obj.author_doctor_id:
            return None
        try:
            return obj.author_doctor.get_name()
        except Exception:
            return None


class PublicBlogDetailSerializer(PublicBlogSerializer):
    """Full shape for a single blog's public detail page."""
    class Meta(PublicBlogSerializer.Meta):
        fields = PublicBlogSerializer.Meta.fields + ["body"]


def _normalize_person_name(value):
    return " ".join((value or "").split()).lower()


def _match_existing_patient(existing_patient, name, phone, gender):
    """Returns a dict of field -> error message for any mismatch between
    the given name/phone/(optional) gender and what's on file for
    existing_patient. Shared by PublicPreBookingSerializer and
    PublicMrdCheckSerializer so the two can never drift out of sync."""
    errors = {}

    on_file_first = (
        _normalize_person_name(existing_patient.first_name).split(" ")[0]
        if existing_patient.first_name else ""
    )
    entered_first = _normalize_person_name(name).split(" ")[0]
    if not entered_first or entered_first != on_file_first:
        errors["name"] = "This name doesn't match the name on file for this MRD number."

    if existing_patient.phone != phone:
        errors["phone"] = "This phone number doesn't match the phone number on file for this MRD number."

    entered_gender = gender or None
    if entered_gender and entered_gender != existing_patient.gender:
        errors["gender"] = "This gender doesn't match the gender on file for this MRD number."

    return errors


class PublicPreBookingSerializer(serializers.Serializer):
    """Validates the public BookAppointment.jsx payload before handing off
    to reception.serializers.ConsultationPreBookingWriteSerializer, which
    does the actual creation.

    mrd_number is optional:
      • left blank -> the caller is a brand-new patient. The view creates
        a new reception.models.Patient record (which auto-generates a
        fresh MRD number) and books against it.
      • provided -> must match an existing, registered patient. If it
        doesn't exist at all, that's rejected under the mrd_number field.
        If it does exist, the first name, phone, and (when given) gender
        entered here are each checked independently against what's on
        file for that patient, and any mismatch is reported under its
        own field (name / phone / gender) rather than one bundled
        message -- so the form can point at exactly what's wrong. This
        also closes the gap where an MRD number alone (sequential and
        therefore guessable) isn't enough to book an appointment under
        someone else's real patient record.
        Gender is only checked if the caller actually picked one; leaving
        it as "prefer not to say" never triggers a mismatch.
        We never silently fall back to creating a brand-new patient/MRD
        on a bad MRD entry -- the caller must either fix the mismatched
        field(s) or clear the MRD field entirely to be registered as a
        new patient.

    is_revisit is optional and only meaningful alongside a valid
    mrd_number:
      • defaults to False -> booked (and charged) as a normal NEW
        consultation, regardless of whether the patient happens to be
        inside a free-revisit window. This is the safe default: the
        website never silently downgrades a booking to free without the
        visitor explicitly asking for it.
      • True -> the view re-checks eligibility itself server-side (never
        trusts this flag alone) via patient_is_revisit_eligible(); if the
        patient genuinely qualifies, the booking is created as a ₹0
        REVISIT. If they don't actually qualify (e.g. the window closed
        between the frontend's live eligibility check and this submit),
        this flag is silently ignored and it's booked as a normal paid
        NEW consultation instead -- never an error, since "not eligible"
        just means falling back to the default behavior.
    """
    name = serializers.CharField(max_length=300)
    phone = serializers.CharField(max_length=10)
    doctor_id = serializers.IntegerField()
    date = serializers.DateField()
    time = serializers.TimeField()
    gender = serializers.ChoiceField(choices=["Male", "Female", "Other"], required=False, allow_null=True)
    age = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=150)
    mrd_number = serializers.CharField(max_length=20, required=False, allow_blank=True)
    is_revisit = serializers.BooleanField(required=False, default=False)

    def validate_phone(self, value):
        value = value.strip()
        if not value.isdigit() or len(value) != 10:
            raise serializers.ValidationError("Phone must be exactly 10 digits.")
        return value

    def validate_name(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Name is required.")
        return value.strip()

    def validate_mrd_number(self, value):
        value = value.strip()
        if not value:
            return value
        from reception.models import Patient
        try:
            self._existing_patient = Patient.objects.get(mrd_number__iexact=value)
        except Patient.DoesNotExist:
            raise serializers.ValidationError(
                "This MRD number doesn't exist. Please double-check it, or clear the field to "
                "be registered as a new patient."
            )
        return self._existing_patient.mrd_number

    def validate(self, attrs):
        existing_patient = getattr(self, "_existing_patient", None)
        if existing_patient:
            errors = _match_existing_patient(
                existing_patient, attrs.get("name"), attrs.get("phone"), attrs.get("gender")
            )
            if errors:
                raise serializers.ValidationError(errors)
        return attrs


class PublicMrdCheckSerializer(serializers.Serializer):
    """Backs GET-style live validation as the visitor fills in the MRD
    Number / Name / Phone / Gender fields, BEFORE they submit the full
    booking -- so the "you're eligible for a free revisit" message (or a
    name/phone/gender mismatch) can show up while they're still on the
    form. Deliberately narrower than PublicPreBookingSerializer: no
    doctor_id/date/time, since availability isn't relevant to this check.
    mrd_number is required here (unlike the booking serializer) since
    there's nothing to check for a brand-new patient.
    """
    name = serializers.CharField(max_length=300)
    phone = serializers.CharField(max_length=10)
    gender = serializers.ChoiceField(choices=["Male", "Female", "Other"], required=False, allow_null=True)
    mrd_number = serializers.CharField(max_length=20)

    def validate_phone(self, value):
        value = value.strip()
        if not value.isdigit() or len(value) != 10:
            raise serializers.ValidationError("Phone must be exactly 10 digits.")
        return value

    def validate_mrd_number(self, value):
        value = value.strip()
        from reception.models import Patient
        try:
            self._existing_patient = Patient.objects.get(mrd_number__iexact=value)
        except Patient.DoesNotExist:
            raise serializers.ValidationError(
                "This MRD number doesn't exist. Please double-check it, or clear the field to "
                "be registered as a new patient."
            )
        return self._existing_patient.mrd_number

    def validate(self, attrs):
        existing_patient = getattr(self, "_existing_patient", None)
        errors = _match_existing_patient(
            existing_patient, attrs.get("name"), attrs.get("phone"), attrs.get("gender")
        )
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class PublicContactInquirySerializer(serializers.ModelSerializer):
    # ✅ FIX: preferred_branch existed on the model (nullable, for exactly
    # this purpose per its own docstring) but was never exposed here, so
    # the Contact form's "Hospitals" selector had nothing to submit to.
    # Not required -- "All Hospital" / no selection is a legitimate
    # general enquiry (see model docstring) and maps to null, same as
    # before this field existed.
    preferred_branch = serializers.PrimaryKeyRelatedField(
        queryset=Branch.objects.filter(is_active=True),
        required=False, allow_null=True,
    )

    class Meta:
        model = PatientQuery
        fields = ["query_type", "name", "phone", "email", "message", "preferred_branch"]

    def validate_phone(self, value):
        value = value.strip()
        if not value.isdigit() or len(value) != 10:
            raise serializers.ValidationError("Phone must be exactly 10 digits.")
        return value