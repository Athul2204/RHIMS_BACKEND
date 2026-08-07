# manager/models.py
import re
from urllib.parse import urlparse, parse_qs
from django.db import models, transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.core.validators import RegexValidator, MinValueValidator
from django.utils import timezone
from django.utils.text import slugify
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from decimal import Decimal, ROUND_HALF_UP


DEALS_IN_ATOMIC_CODES = ("MEDICINE", "SUPPLY", "GENERAL")

# Hardening: ImageField already verifies (via Pillow) that an upload is a
# genuine image, and every uploader below is authenticated Manager-role
# staff, so this isn't closing a real vulnerability — it just stops one
# staff account from filling up disk with oversized uploads (doctor
# photos, blog covers, gallery images, etc.), accidentally or otherwise.
MAX_IMAGE_UPLOAD_SIZE_MB = 5
MAX_IMAGE_UPLOAD_SIZE_BYTES = MAX_IMAGE_UPLOAD_SIZE_MB * 1024 * 1024


def validate_image_file_size(file):
    """Reject an uploaded image over MAX_IMAGE_UPLOAD_SIZE_MB. Attach to
    every ImageField below via validators=[validate_image_file_size]."""
    if file.size > MAX_IMAGE_UPLOAD_SIZE_BYTES:
        raise ValidationError(
            f"Image file too large ({file.size / (1024 * 1024):.1f} MB). "
            f"Maximum allowed size is {MAX_IMAGE_UPLOAD_SIZE_MB} MB."
        )


def validate_deals_in(value):
    """
    Field validator for Dealer.deals_in. Accepts a single code
    ("MEDICINE"), the legacy "BOTH" (= all three categories), or a
    comma-separated combination of two or more codes (e.g.
    "MEDICINE,SUPPLY"). Defined at module level (not as a lambda/method)
    so Django can serialize it into migrations.
    """
    if value == "BOTH":
        return
    codes = [c.strip() for c in value.split(",") if c.strip()]
    if not codes or any(c not in DEALS_IN_ATOMIC_CODES for c in codes):
        raise ValidationError(
            "deals_in must be 'BOTH' or a comma-separated combination of "
            f"{DEALS_IN_ATOMIC_CODES}."
        )
    if len(set(codes)) != len(codes):
        raise ValidationError("deals_in must not repeat the same category.")


# ─────────────────────────────────────────────
# SUPPORT STAFF (Non-EMR: Nurse, Cleaning, etc.)
# ─────────────────────────────────────────────
class SupportStaff(models.Model):

    ROLE_CHOICES = [
        ("Nurse",          "Nurse"),
        ("Cleaning Staff", "Cleaning Staff"),
        ("Security",       "Security"),
        ("Cook",           "Cook"),
        ("Driver",         "Driver"),
        ("Ward Boy",       "Ward Boy"),
        ("Other",          "Other"),
    ]

    SALARY_TYPE_CHOICES = [
        ("Monthly", "Monthly"),
        ("Weekly",  "Weekly"),
        ("Daily",   "Daily"),
    ]

    STAFF_CODE_PREFIX = {
        "Nurse":          "NRS",
        "Cleaning Staff": "CLN",
        "Security":       "SEC",
        "Cook":           "COOK",
        "Driver":         "DRV",
        "Ward Boy":       "WDB",
        "Other":          "STF",
    }

    staff_id   = models.AutoField(primary_key=True)

    # ✅ FIX: SupportStaff had no branch field at all. Attendance/
    # LeaveRequest/SalaryRecord all key off this FK, so they inherit
    # branch scope transparently once this is set — no extra field
    # needed on those three. Required + PROTECT, matching
    # administration.StaffProfile.branch.
    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="support_staff",
    )

    staff_code = models.CharField(max_length=20, editable=False, db_index=True)

    full_name    = models.CharField(max_length=200)
    role         = models.CharField(max_length=20, choices=ROLE_CHOICES)
    department   = models.CharField(max_length=100, blank=True, null=True)

    phone = models.CharField(
        max_length=15,
        blank=True, null=True,
        validators=[RegexValidator(r'^\+?\d{9,15}$', 'Enter a valid phone number.')],
    )

    date_of_birth = models.DateField(null=True, blank=True)
    address       = models.TextField(blank=True, null=True)
    joining_date  = models.DateField(default=timezone.localdate)

    salary_type    = models.CharField(max_length=10, choices=SALARY_TYPE_CHOICES, default="Monthly")
    monthly_salary = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    # For daily-rate staff — if 0 will be derived from monthly_salary / mandatory_working_days
    daily_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    # For weekly-rate staff — if 0 will be derived from daily_rate × 7
    weekly_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Reference weekly pay rate. If left as 0, derived from daily_rate × 7 for display purposes.",
    )
    # Number of working days used to compute per-day salary for this staff member
    mandatory_working_days = models.PositiveSmallIntegerField(default=26)

    is_active  = models.BooleanField(default=True)
    notes      = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    # ── validation ──────────────────────────────────────────
    def clean(self):
        if not self.full_name or not self.full_name.strip():
            raise ValidationError({"full_name": "Full name cannot be blank."})
        if self.date_of_birth and self.date_of_birth > timezone.localdate():
            raise ValidationError({"date_of_birth": "Date of birth cannot be in the future."})
        if self.joining_date and self.joining_date > timezone.localdate():
            raise ValidationError({"joining_date": "Joining date cannot be in the future."})

    # ── auto staff_code ──────────────────────────────────────
    def save(self, *args, **kwargs):
        self.full_clean()
        if not self.staff_code:
            with transaction.atomic():
                role_prefix = self.STAFF_CODE_PREFIX.get(self.role, "STF")
                branch_code = self.branch.code if self.branch_id else "GRP"
                prefix = f"{branch_code}-{role_prefix}"
                last = (
                    SupportStaff.objects
                    .select_for_update()
                    .filter(branch_id=self.branch_id, staff_code__startswith=prefix + "-")
                    .order_by("-staff_id")
                    .first()
                )
                new_number = 1
                if last and last.staff_code:
                    try:
                        new_number = int(last.staff_code.split("-")[-1]) + 1
                    except Exception:
                        pass
                self.staff_code = f"{prefix}-{str(new_number).zfill(3)}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.staff_code} — {self.full_name} ({self.role})"

    def reference_rate(self, period_type):
        """
        Best-effort reference figure for the given period_type, purely for
        display on the Salary sheet — never used to auto-fill net_salary.
        Falls back to deriving from whichever rate IS set when the specific
        one requested is 0/unset, using mandatory_working_days as the
        monthly↔daily bridge.
        """
        working_days = self.mandatory_working_days or 26
        daily = self.daily_rate
        if not daily:
            if self.monthly_salary:
                daily = (self.monthly_salary / working_days)
            elif self.weekly_rate:
                daily = (self.weekly_rate / 7)

        if period_type == "Daily":
            return daily
        if period_type == "Weekly":
            return self.weekly_rate if self.weekly_rate else (daily * 7 if daily else Decimal("0.00"))
        # Monthly
        return self.monthly_salary if self.monthly_salary else (daily * working_days if daily else Decimal("0.00"))

    class Meta:
        ordering = ["-staff_id"]
        unique_together = [("branch", "staff_code")]


# ─────────────────────────────────────────────
# ATTENDANCE
# ─────────────────────────────────────────────
class Attendance(models.Model):

    STATUS_CHOICES = [
        ("Present",      "Present"),
        ("Absent",       "Absent"),
        ("Paid Leave",   "Paid Leave"),
        ("Unpaid Leave", "Unpaid Leave"),
        ("Holiday",      "Holiday"),
        ("Half Day",     "Half Day"),
    ]

    attendance_id = models.AutoField(primary_key=True)
    date          = models.DateField()

    # One of these two is set, never both
    staff_profile = models.ForeignKey(
        "administration.StaffProfile",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="attendances",
    )
    support_staff = models.ForeignKey(
        SupportStaff,
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="attendances",
    )

    status    = models.CharField(max_length=15, choices=STATUS_CHOICES, default="Present")
    notes     = models.TextField(blank=True, null=True)
    marked_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="marked_attendances",
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.staff_profile_id and self.support_staff_id:
            raise ValidationError("An attendance record must link to either a staff profile or support staff, not both.")
        if not self.staff_profile_id and not self.support_staff_id:
            raise ValidationError("An attendance record must link to a staff member.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        staff = self.staff_profile or self.support_staff
        return f"{self.date} — {staff} — {self.status}"

    class Meta:
        ordering = ["-date"]
        constraints = [
            # No condition= here: MySQL doesn't support partial indexes.
            # MySQL treats each NULL as distinct in unique indexes, so this
            # correctly allows multiple records with staff_profile=NULL or
            # support_staff=NULL while still preventing duplicate entries
            # for the same staff on the same date.
            models.UniqueConstraint(
                fields=["date", "staff_profile"],
                name="unique_attendance_staff_profile_date",
            ),
            models.UniqueConstraint(
                fields=["date", "support_staff"],
                name="unique_attendance_support_staff_date",
            ),
        ]


# ─────────────────────────────────────────────
# LEAVE REQUEST
# ─────────────────────────────────────────────
class LeaveRequest(models.Model):

    LEAVE_TYPE_CHOICES = [
        ("Paid",   "Paid Leave"),
        ("Unpaid", "Unpaid Leave"),
    ]

    STATUS_CHOICES = [
        ("Pending",  "Pending"),
        ("Approved", "Approved"),
        ("Rejected", "Rejected"),
    ]

    leave_id = models.AutoField(primary_key=True)

    staff_profile = models.ForeignKey(
        "administration.StaffProfile",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="leave_requests",
    )
    support_staff = models.ForeignKey(
        SupportStaff,
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="leave_requests",
    )

    leave_type = models.CharField(max_length=10, choices=LEAVE_TYPE_CHOICES)
    start_date = models.DateField()
    end_date   = models.DateField()
    reason     = models.TextField(blank=True, null=True)

    status            = models.CharField(max_length=10, choices=STATUS_CHOICES, default="Pending")
    approved_by       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_leaves")
    approved_at       = models.DateTimeField(null=True, blank=True)
    rejection_reason  = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.staff_profile_id and self.support_staff_id:
            raise ValidationError("A leave request must link to either a staff profile or support staff, not both.")
        if not self.staff_profile_id and not self.support_staff_id:
            raise ValidationError("A leave request must link to a staff member.")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError({"end_date": "End date must be on or after start date."})

    @property
    def total_days(self):
        if self.start_date and self.end_date:
            return (self.end_date - self.start_date).days + 1
        return 0

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        staff = self.staff_profile or self.support_staff
        return f"{staff} — {self.leave_type} — {self.start_date} to {self.end_date} [{self.status}]"

    class Meta:
        ordering = ["-created_at"]


# ─────────────────────────────────────────────
# SALARY RECORD (monthly pay slip)
#
# MANUAL ENTRY MODEL — no auto-calculation. A manager types in the amount
# to pay each staff member (doctor, support staff, or manager) for the
# month. This replaces the earlier attendance-formula design (Base ÷
# Working Days × Present Days) and, for EMR staff, the auto-pull of
# base_salary from administration.StaffProfile.salary (what Admin set
# when creating the account). Neither happens anymore — the amount paid
# is decided and entered here only.
#
# present_days/paid_leave_days/etc. remain as READ-ONLY informational
# context pulled from Attendance for the month (so a manager can see
# attendance next to the pay they're entering) — they no longer drive
# net_salary.
# ─────────────────────────────────────────────
class SalaryRecord(models.Model):

    record_id = models.AutoField(primary_key=True)

    staff_profile = models.ForeignKey(
        "administration.StaffProfile",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="salary_records",
    )
    support_staff = models.ForeignKey(
        SupportStaff,
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="salary_records",
    )

    # Salary is always tracked per calendar month now — no more
    # Weekly/Daily sheets. A staff member can have at most one
    # SalaryRecord per (month, year), enforced below.
    month = models.PositiveSmallIntegerField()   # 1-12
    year  = models.PositiveIntegerField()

    # Informational attendance snapshot only — NOT used to compute salary.
    present_days     = models.PositiveSmallIntegerField(default=0)
    paid_leave_days   = models.PositiveSmallIntegerField(default=0)
    unpaid_leave_days = models.PositiveSmallIntegerField(default=0)
    half_days         = models.PositiveSmallIntegerField(default=0)
    absent_days       = models.PositiveSmallIntegerField(default=0)

    # ── Typed in manually by the manager each month (not auto-derived from
    # attendance). Locked once is_paid is set — see SalaryRecordSerializer.
    # max_digits=12 to safely hold values up to ₹9,999,999,999.99.
    net_salary = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Final take-home salary for this month, entered manually by the manager.",
    )
    bonus = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Bonus amount for this month, entered manually by the manager.",
    )
    deductions = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Deduction amount for this month, entered manually by the manager.",
    )

    is_paid  = models.BooleanField(default=False)
    paid_at  = models.DateTimeField(null=True, blank=True)
    notes    = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.staff_profile_id and self.support_staff_id:
            raise ValidationError("A salary record must link to either a staff profile or support staff, not both.")
        if not self.staff_profile_id and not self.support_staff_id:
            raise ValidationError("A salary record must link to a staff member.")
        if not (1 <= self.month <= 12):
            raise ValidationError({"month": "Month must be between 1 and 12."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        staff = self.staff_profile or self.support_staff
        return f"{staff} — {self.month}/{self.year} — ₹{self.net_salary}"

    class Meta:
        ordering = ["-year", "-month"]
        constraints = [
            # No condition= here: MySQL doesn't support partial indexes.
            # MySQL treats each NULL as distinct in unique indexes, so this
            # correctly prevents duplicate salary records for the same
            # staff member in the same calendar month while allowing NULL
            # in each FK column.
            models.UniqueConstraint(
                fields=["month", "year", "staff_profile"],
                name="unique_salary_staff_profile_month",
            ),
            models.UniqueConstraint(
                fields=["month", "year", "support_staff"],
                name="unique_salary_support_staff_month",
            ),
        ]


# ─────────────────────────────────────────────
# SALARY ENTRY
# One row per pay item a manager adds to a SalaryRecord — tagged Salary,
# Bonus, or Deduction, each with an amount and an optional note. The
# record's net_salary/bonus/deductions totals are kept in sync with these
# entries by the views (SalaryEntryListCreateView / ...DeleteView), not
# here, so the manager never has to type a final total manually.
# ─────────────────────────────────────────────
class SalaryEntry(models.Model):

    ENTRY_TYPE_CHOICES = [
        ("Salary",     "Salary"),
        ("Bonus",      "Bonus"),
        ("Deduction",  "Deduction"),
    ]

    salary_record = models.ForeignKey(
        SalaryRecord,
        on_delete=models.CASCADE,
        related_name="entries",
    )

    entry_type = models.CharField(max_length=10, choices=ENTRY_TYPE_CHOICES, default="Salary")
    amount = models.DecimalField(
        max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Amount for this pay item.",
    )
    note = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    def __str__(self):
        return f"{self.salary_record} — {self.entry_type} — ₹{self.amount}"

    class Meta:
        ordering = ["created_at"]
        verbose_name_plural = "Salary entries"


# ═════════════════════════════════════════════════════════════════════════
# DEALERS (SUPPLIERS/PROVIDERS) + CREDIT LEDGER
# ═════════════════════════════════════════════════════════════════════════
# A "Dealer" is who the pharmacist buys medicines/supplies from, and who
# medicines/supplies get returned to. Dealer master data lives here, in the
# manager module, per requirements. Pharmacist links a batch or a return to
# a Dealer (both links are OPTIONAL — plenty of stock is entered without
# ever tracking a dealer, exactly as before this feature existed).
#
# Whenever a batch/return IS linked to a dealer, a DealerTransaction row is
# auto-created with status=PENDING. The manager reviews it on the Dealers
# page and finalizes it (confirms it, choosing how it was actually settled:
# left as credit, paid immediately, refunded in cash, or settled via an
# exchange of goods) or rejects it. Nothing about existing purchase/return
# flows changes when no dealer is selected.
#
# Balance model: Dealer.balance is computed live from CONFIRMED ledger rows
# only — never stored/duplicated — so it can't drift out of sync:
#   + PURCHASE      → increases what the hospital owes the dealer
#   - PAYMENT       → hospital paid the dealer cash
#   - CREDIT_NOTE   → dealer owes hospital for returned goods (credit note)
#   + CASH_REFUND   → dealer paid the hospital cash back for a return —
#                     this SETTLES a CREDIT_NOTE, so it must move the
#                     balance in the opposite direction from CREDIT_NOTE,
#                     back toward zero, not the same direction. (Bug fix:
#                     this was previously also subtracted, which made a
#                     cash refund double the apparent debt instead of
#                     paying it off — e.g. a ₹50 CREDIT_NOTE followed by
#                     its ₹50 CASH_REFUND wrongly totalled -₹100 instead
#                     of ₹0/settled.)
#   +/- ADJUSTMENT  → manual correction by the manager (signed amount)
#
# A dealer bringing a fresh order that costs more than their outstanding
# credit is handled automatically by this running balance — no separate
# "apply credit" step is required: the new PURCHASE and the old CREDIT_NOTE
# simply net out, and whatever remains is what's still owed either way.
#
# `linked_transaction` is a purely OPTIONAL pointer used for traceability
# only (e.g. "this new purchase is the exchange goods for that return") —
# it has no effect on the balance math above.
# ═════════════════════════════════════════════════════════════════════════
class Dealer(models.Model):

    # NOTE: "BOTH" is a legacy value kept for backward compatibility —
    # dealers saved before the combination picker existed used it to mean
    # "all three". It's still accepted as a synonym for all three categories.
    DEALS_IN_CHOICES = [
        ("MEDICINE", "Medicine"),
        ("SUPPLY",   "Supplies"),
        ("GENERAL",  "General Items"),
        ("BOTH",     "All"),
    ]


    dealer_id = models.AutoField(primary_key=True)

    # ✅ FIX: Dealer had no branch field — confirmed branch-wise (each
    # branch manages its own vendor list independently), matching
    # Medicine's branch scoping. Required + PROTECT, same as SupportStaff.
    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="dealers",
    )

    name           = models.CharField(max_length=200, help_text="Must be unique per branch (case-insensitive).")
    contact_person = models.CharField(max_length=150, blank=True, null=True)
    phone = models.CharField(
        max_length=15,
        blank=True, null=True,
        validators=[RegexValidator(r'^\+?\d{9,15}$', 'Enter a valid phone number.')],
    )
    email      = models.EmailField(blank=True, null=True)
    address    = models.TextField(blank=True, null=True)
    gst_number = models.CharField(max_length=20, blank=True, null=True)
    deals_in   = models.CharField(
        max_length=30, default="BOTH",
        validators=[validate_deals_in],
        help_text="'BOTH' (all three), a single code, or a comma-separated "
                   "combination, e.g. 'MEDICINE,SUPPLY'.",
    )

    # Optional starting balance if migrating an existing running account
    # onto this ledger (positive = hospital already owed the dealer this
    # much before the ledger started tracking anything).
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    is_active = models.BooleanField(default=True)
    notes     = models.TextField(blank=True, null=True)

    added_by   = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="dealers_added")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not self.name or not self.name.strip():
            raise ValidationError({"name": "Dealer name cannot be blank."})
        self.name = self.name.strip()
        # ✅ FIX: no duplicate-name protection existed at all before — two
        # dealers named "Acme Pharma" and "acme pharma" (or the same name
        # twice by accident) could both be created, silently splitting one
        # supplier's purchase/return/payment history across two unrelated
        # ledgers. Checked here for a friendly 400; enforced for real by the
        # case-insensitive DB constraint below. Scoped per branch — two
        # different branches are allowed to each carry their own "Acme
        # Pharma" vendor record.
        qs = Dealer.objects.filter(branch_id=self.branch_id, name__iexact=self.name)
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        if qs.exists():
            raise ValidationError({"name": "A dealer with this name already exists at this branch."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def balance(self):
        """Positive = we owe the dealer. Negative = the dealer owes us (credit)."""
        agg = self.transactions.filter(status="CONFIRMED").aggregate(
            purchases   = models.Sum("amount", filter=Q(transaction_type="PURCHASE")),
            payments    = models.Sum("amount", filter=Q(transaction_type="PAYMENT")),
            credit_notes= models.Sum("amount", filter=Q(transaction_type="CREDIT_NOTE")),
            cash_refunds= models.Sum("amount", filter=Q(transaction_type="CASH_REFUND")),
            adjustments = models.Sum("amount", filter=Q(transaction_type="ADJUSTMENT")),
        )
        zero = Decimal("0.00")
        total = (
            self.opening_balance
            + (agg["purchases"] or zero)
            - (agg["payments"] or zero)
            - (agg["credit_notes"] or zero)
            + (agg["cash_refunds"] or zero)
            + (agg["adjustments"] or zero)
        )
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def pending_count(self):
        return self.transactions.filter(status="PENDING").count()

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["is_active"]),
            models.Index(fields=["name"]),
        ]
        constraints = [
            # ✅ FIX: DB-level backstop for the clean() check above — closes
            # the race window between two concurrent "add dealer" requests
            # for the same name that clean()'s SELECT alone can't prevent.
            # Scoped per branch, matching the branch field added above.
            models.UniqueConstraint(Lower("name"), "branch", name="unique_dealer_name_ci_per_branch"),
        ]


class DealerTransaction(models.Model):
    """One ledger row per dealer-linked purchase, return, payment, refund
    or manual adjustment. See the module docstring above for the full
    balance model."""

    TRANSACTION_TYPE_CHOICES = [
        ("PURCHASE",    "Purchase (stock received)"),
        ("PAYMENT",     "Payment made to dealer"),
        ("CREDIT_NOTE", "Credit note (dealer owes us, from a return)"),
        ("CASH_REFUND", "Cash refund received from dealer"),
        ("ADJUSTMENT",  "Manual adjustment"),
    ]

    # How the pharmacist/manager intends (or ends up) settling this entry.
    # Purely descriptive/optional — does NOT drive the balance math, which
    # is entirely determined by transaction_type + amount above. Present
    # because the pharmacist needs to say "credit / paid / refund /
    # exchange" up front, and it's shown back on the ledger for context.
    SETTLEMENT_CHOICES = [
        ("CREDIT",   "Credit — carried forward against future orders"),
        ("PAID",     "Paid in full"),
        ("REFUND",   "Cash refund"),
        ("EXCHANGE", "Exchanged for other product(s)"),
    ]

    STATUS_CHOICES = [
        ("PENDING",   "Pending manager review"),
        ("CONFIRMED", "Confirmed"),
        ("REJECTED",  "Rejected"),
    ]

    # What originally generated this row — loose reference (no FK/GFK)
    # so the manager app never has to import pharmacist models.
    SOURCE_MODEL_CHOICES = [
        ("MEDICINE_BATCH",     "Medicine batch"),
        ("SUPPLY_BATCH",       "Supply batch"),
        ("GENERAL_ITEM_BATCH", "General item batch"),
        ("MEDICINE_RETURN",    "Medicine return to provider"),
        ("SUPPLY_RETURN",      "Supply return"),
        ("GENERAL_ITEM_RETURN", "General item return"),
        ("MANUAL",             "Manually entered"),
    ]

    transaction_id = models.AutoField(primary_key=True)

    dealer = models.ForeignKey(Dealer, on_delete=models.PROTECT, related_name="transactions")

    transaction_type = models.CharField(max_length=15, choices=TRANSACTION_TYPE_CHOICES)
    settlement_method = models.CharField(max_length=10, choices=SETTLEMENT_CHOICES, blank=True, null=True)
    # ✅ FIX: no longer a flat MinValueValidator(0.01). That silently made
    # every ADJUSTMENT positive-only, contradicting the "+/- ADJUSTMENT →
    # manual correction by the manager (signed amount)" balance model
    # documented above — a manager had no way to record a downward
    # correction. PURCHASE/PAYMENT/CREDIT_NOTE/CASH_REFUND must still be
    # strictly positive (their sign is implied by transaction_type);
    # ADJUSTMENT may be positive or negative, just never zero. See clean().
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="PENDING")

    source_model = models.CharField(max_length=20, choices=SOURCE_MODEL_CHOICES, default="MANUAL")
    source_id    = models.PositiveIntegerField(blank=True, null=True)

    # Optional pointer to another transaction for this dealer — e.g. the
    # replacement-goods purchase that settles an earlier exchange, or the
    # later purchase a credit note is meant to offset. Never required.
    linked_transaction = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="linked_from",
    )

    reference_number = models.CharField(max_length=100, blank=True, null=True, help_text="Invoice / credit-note / RMA number")
    notes = models.TextField(blank=True, null=True)

    # ✅ NEW: when a manager settles an entry as "Pending — Pay Later"
    # (settlement_method=CREDIT) instead of paying/refunding immediately,
    # this optionally records when it's expected to be cleared. Purely
    # informational — like settlement_method, it never drives the balance
    # math, it just lets the ledger surface "due" / "overdue" items instead
    # of leaving every credit entry looking identical and open-ended.
    due_date = models.DateField(
        null=True, blank=True,
        help_text="When this entry (left as credit) is expected to be settled.",
    )

    # Snapshot of the dealer's balance immediately after this row was
    # confirmed — informational only, for a stable historical ledger view.
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    created_by   = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="dealer_transactions_created")
    confirmed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="dealer_transactions_confirmed")

    created_at   = models.DateTimeField(default=timezone.now, editable=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    updated_at   = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.linked_transaction_id and self.linked_transaction_id == self.transaction_id:
            raise ValidationError({"linked_transaction": "A transaction cannot link to itself."})
        if self.amount is not None:
            if self.transaction_type == "ADJUSTMENT":
                if self.amount == 0:
                    raise ValidationError({"amount": "Adjustment amount cannot be zero."})
            elif self.amount <= 0:
                raise ValidationError({"amount": "Amount must be greater than zero."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.dealer.name} — {self.get_transaction_type_display()} — ₹{self.amount} [{self.status}]"

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["dealer", "status"]),
            models.Index(fields=["source_model", "source_id"]),
            models.Index(fields=["status", "-created_at"]),
        ]


def create_dealer_transaction(
    *, dealer, transaction_type, amount, source_model, source_id,
    created_by=None, settlement_method=None, reference_number=None, notes=None,
):
    """
    Convenience for other apps (pharmacist) to log a PENDING dealer
    transaction the moment a batch/return is saved with a dealer attached.
    Intended to be called as:

        from manager.models import create_dealer_transaction
        create_dealer_transaction(...)

    imported at call-time inside a view/serializer method (not at module
    import time) so there is no risk of a circular import between the
    pharmacist and manager apps. Returns None (and creates nothing) if
    dealer is falsy, or amount isn't positive for the non-ADJUSTMENT types
    that this helper is normally used for — callers can call this
    unconditionally without an extra `if dealer:` guard. ADJUSTMENT amounts
    are signed (see DealerTransaction.clean()), so only zero is rejected
    for that type.
    """
    if not dealer or amount is None:
        return None
    if transaction_type == "ADJUSTMENT":
        if amount == 0:
            return None
    elif amount <= 0:
        return None
    return DealerTransaction.objects.create(
        dealer=dealer,
        transaction_type=transaction_type,
        settlement_method=settlement_method,
        amount=amount,
        status="PENDING",
        source_model=source_model,
        source_id=source_id,
        created_by=created_by,
        reference_number=reference_number,
        notes=notes,
    )


# ─────────────────────────────────────────────
# HOSPITAL EXPENSE
# ─────────────────────────────────────────────
class HospitalExpense(models.Model):

    CATEGORY_CHOICES = [
        ("Salary",      "Staff Salary"),
        ("Utilities",   "Utilities"),
        ("Equipment",   "Equipment"),
        ("Supplies",    "Supplies"),
        ("Maintenance", "Maintenance"),
        ("Rent",        "Rent"),
        ("Other",       "Other"),
    ]

    expense_id     = models.AutoField(primary_key=True)

    # ✅ FIX: no branch field at all — every expense row needs to know
    # which branch's Finance Dashboard it belongs to. Required + PROTECT,
    # matching the rest of the branch-scoped operational models.
    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="expenses",
    )

    title          = models.CharField(max_length=200)
    category       = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    amount         = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0"))])
    date           = models.DateField(default=timezone.localdate)
    description    = models.TextField(blank=True, null=True)
    receipt_number = models.CharField(max_length=100, blank=True, null=True)
    added_by       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="added_expenses")
    created_at     = models.DateTimeField(default=timezone.now, editable=False)
    updated_at     = models.DateTimeField(auto_now=True)

    def clean(self):
        if not self.title or not self.title.strip():
            raise ValidationError({"title": "Expense title cannot be blank."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.date} — {self.category} — {self.title} — ₹{self.amount}"

    class Meta:
        ordering = ["-date"]


class OtherIncome(models.Model):
    """
    Non-billing income the hospital receives outside the normal
    reception/pharmacy/lab billing flow — e.g. commission from the
    laboratory for referrals, or a donation. Mirrors HospitalExpense's
    shape so the Finance Dashboard can treat it the same way.
    """

    CATEGORY_CHOICES = [
        ("LAB_COMMISSION", "Lab Commission"),
        ("DONATION",        "Donation"),
        ("OTHER",           "Other"),
    ]

    income_id      = models.AutoField(primary_key=True)

    # ✅ FIX: same gap as HospitalExpense — no branch field, just scoping,
    # so every income row can be attributed to the right branch's Finance
    # Dashboard.
    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="other_income",
    )

    title          = models.CharField(max_length=200)
    category       = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    amount         = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0"))])
    date           = models.DateField(default=timezone.localdate)
    description    = models.TextField(blank=True, null=True)
    receipt_number = models.CharField(max_length=100, blank=True, null=True)
    added_by       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="added_income")
    created_at     = models.DateTimeField(default=timezone.now, editable=False)
    updated_at     = models.DateTimeField(auto_now=True)

    def clean(self):
        if not self.title or not self.title.strip():
            raise ValidationError({"title": "Income title cannot be blank."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.date} — {self.category} — {self.title} — ₹{self.amount}"

    class Meta:
        ordering = ["-date"]


# ═══════════════════════════════════════════════════════
# PUBLIC WEBSITE — content curated by the manager, served
# read-only (AllowAny) to the unauthenticated marketing site
# by the separate `public` app. See public/views.py.
# ═══════════════════════════════════════════════════════

def _website_doctor_photo_path(instance, filename):
    return f"website/doctors/{instance.pk or 'new'}/{filename}"


class DoctorWebsiteProfile(models.Model):
    """
    Public-facing profile for a doctor. Deliberately separate from
    doctor.DoctorProfile (the EMR record) -- not every staff doctor should
    appear on the public site, and the public site needs fields (bio,
    photo, display_order) that have no place in the EMR model.

    ✅ FIX: `doctor` changed from OneToOneField to ManyToManyField. A
    doctor working at two branches gets a separate DoctorProfile row per
    branch (see doctor.models.DoctorProfile's registration_number
    docstring) -- under the old OneToOne, that meant a separate
    DoctorWebsiteProfile per branch too, forcing two managers to
    independently retype the same name/photo/regno/specialty/bio/
    education/awards, with no guarantee they'd stay in sync. The public
    identity (this model) is shared/common exactly like the rest of the
    Web Management CMS; only availability stays genuinely per-branch (see
    DoctorWeeklyAvailability/DoctorAvailabilityException, still keyed to
    a single DoctorProfile each). A manager adding a doctor's second
    branch now attaches the *existing* DoctorWebsiteProfile to the new
    DoctorProfile instead of creating a new one -- see
    WebsiteDoctorAttachBranchView.
    """

    doctors = models.ManyToManyField(
        "doctor.DoctorProfile",
        related_name="website_profiles",
        help_text="Every branch-specific DoctorProfile this public profile represents "
                   "-- one per branch the doctor practices at.",
    )
    is_published = models.BooleanField(
        default=False,
        help_text="Only published profiles appear on the public website.",
    )
    bio = models.TextField(blank=True, default="")
    photo = models.ImageField(upload_to=_website_doctor_photo_path, blank=True, null=True, validators=[validate_image_file_size])
    display_order = models.PositiveIntegerField(default=0)

    # ── Public "View Profile" detail-page content ──────────────────────
    # Separate from the card-list fields above (name/specialty/photo come
    # from the linked EMR doctor + bio/photo here). These back the public
    # detail page's Work Experience / Area of Expertise / Education /
    # Awards / Languages Known sections. Timing is deliberately NOT
    # duplicated here -- it's derived at read time from this doctor's
    # DoctorWeeklyAvailability rows so there's one source of truth.
    designation = models.CharField(
        max_length=200, blank=True, default="",
        help_text='Public-facing title, e.g. "Senior Consultant & Director - Interventional Cardiology". '
                   "Falls back to specialization/department if left blank.",
    )
    experience_summary = models.CharField(
        max_length=100, blank=True, default="",
        help_text='Short headline for the "Work Experience" section, e.g. "49 Years".',
    )
    area_of_expertise = models.JSONField(
        default=list, blank=True,
        help_text="List of bullet-point strings.",
    )
    education = models.JSONField(
        default=list, blank=True,
        help_text="List of degree/qualification strings, e.g. [\"MBBS\", \"MD\", \"DM\"].",
    )
    awards = models.JSONField(
        default=list, blank=True,
        help_text="List of award/membership bullet strings.",
    )
    languages_known = models.JSONField(
        default=list, blank=True,
        help_text="List of language name strings.",
    )

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    @staticmethod
    def _validate_string_list(value, field_name):
        if value in (None, ""):
            return
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValidationError({field_name: "Must be a list of strings."})

    def clean(self):
        # Membership validation (each attached doctor must be Doctor-role)
        # happens in the M2M-aware validators below rather than here --
        # self.doctors isn't queryable until this instance has a pk (M2M
        # needs the row to exist first), so it can't run inside clean().
        # See validate_doctor_membership() / validate_doctors(), called
        # from the serializer/view instead.
        for field_name in ("area_of_expertise", "education", "awards", "languages_known"):
            self._validate_string_list(getattr(self, field_name), field_name)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def primary_doctor(self):
        """First attached DoctorProfile, ordered by pk -- used as the
        stable source for name/specialty/registration_number display.
        These fields don't vary by branch, so any attached doctor works;
        pk ordering just keeps it deterministic across calls."""
        return self.doctors.select_related("staff", "staff__user", "specialty").order_by("pk").first()

    @staticmethod
    def validate_doctor_membership(doctor):
        """Raise if `doctor` (a doctor.DoctorProfile) isn't Doctor-role.
        Called from the serializer on create/attach, since a fresh
        instance has no pk yet for self.doctors to be queryable inside
        clean()."""
        if getattr(doctor, "staff", None) and doctor.staff.role != "Doctor":
            raise ValidationError({"doctor": "Website profile must point to a Doctor-role staff member."})

    def get_name(self):
        doctor = self.primary_doctor()
        if not doctor:
            return f"Website Profile #{self.pk}"
        try:
            return doctor.staff.user.get_full_name() or doctor.staff.user.username
        except Exception:
            return f"Doctor #{doctor.pk}"

    def branch_summaries(self):
        """[{branch_id, branch_name, branch_code, doctor_id}, ...] for
        every attached DoctorProfile -- one entry per branch the doctor
        practices at. Used by the public detail page to label each
        availability block, and by the manager CMS to show which
        branches a profile already covers."""
        return [
            {
                "doctor_id": d.pk,
                "branch_id": d.staff.branch_id if getattr(d, "staff", None) else None,
                "branch_name": d.staff.branch.name if getattr(d, "staff", None) and d.staff.branch_id else "",
                "branch_code": d.staff.branch.code if getattr(d, "staff", None) and d.staff.branch_id else "",
            }
            for d in self.doctors.select_related("staff", "staff__branch").order_by("staff__branch__name")
        ]

    def __str__(self):
        return f"{self.get_name()} - {'Published' if self.is_published else 'Hidden'}"

    class Meta:
        ordering = ["display_order", "pk"]
        verbose_name = "Doctor Website Profile"


def _website_branch_photo_path(instance, filename):
    return f"website/branches/{instance.pk or 'new'}/{filename}"


class BranchWebsiteProfile(models.Model):
    """
    Public-facing content for one hospital location, shown on the
    website's "Our Locations" page. Deliberately separate from
    administration.Branch (the operational record every staff/patient/
    billing row hangs off) for the same reason DoctorWebsiteProfile is
    separate from doctor.DoctorProfile: the public site needs fields
    (description, highlights, photo, map link) that have no place on the
    operational model, and not every branch necessarily has to be public
    yet (is_published gates that independently of whether the branch is
    operationally is_active).

    OneToOne rather than DoctorWebsiteProfile's ManyToMany -- a Branch
    *is* one physical location, so there's no analogous "one profile
    shared across several branch rows" case here; each Branch gets at
    most one BranchWebsiteProfile.
    """

    branch = models.OneToOneField(
        "administration.Branch",
        on_delete=models.CASCADE,
        related_name="website_profile",
        help_text="The operational branch this public content describes.",
    )
    BRANCH_TYPE_CHOICES = [
        ("HOSPITAL", "Hospital"),
        ("MEDICAL_CENTRE", "Medical Centre"),
    ]
    branch_type = models.CharField(
        max_length=20, choices=BRANCH_TYPE_CHOICES, default="HOSPITAL",
        help_text="Which section of the public Locations page this branch is grouped "
                   "under -- full-service Hospitals get the larger banner-card listing, "
                   "smaller Medical Centres get the compact grid below it.",
    )
    is_published = models.BooleanField(
        default=False,
        help_text="Only published branches appear on the public website's Locations page.",
    )
    description = models.TextField(
        blank=True, default="",
        help_text="\"About this branch\" copy shown on the Locations page.",
    )
    highlights = models.JSONField(
        default=list, blank=True,
        help_text="List of short bullet strings, e.g. [\"24/7 Emergency\", \"Free Parking\", \"200 Beds\"].",
    )
    email = models.EmailField(blank=True, default="")
    hours_text = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Free-text display hours, e.g. \"Mon\u2013Sat: 9:00 AM \u2013 6:00 PM\". "
                   "Not used for booking logic -- purely informational copy for this page.",
    )
    map_url = models.URLField(
        blank=True, default="",
        help_text="Google Maps (or similar) link used for the page's \"Get Directions\" button.",
    )
    photo = models.ImageField(upload_to=_website_branch_photo_path, blank=True, null=True, validators=[validate_image_file_size])
    display_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.highlights in (None, ""):
            return
        if not isinstance(self.highlights, list) or not all(isinstance(item, str) for item in self.highlights):
            raise ValidationError({"highlights": "Must be a list of strings."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.branch.name} - {'Published' if self.is_published else 'Hidden'}"

    class Meta:
        ordering = ["display_order", "pk"]
        verbose_name = "Branch Website Profile"


class DoctorWeeklyAvailability(models.Model):
    """A recurring weekly slot-generating rule for one doctor, e.g. 'every
    Monday 10:00-13:00 in 15-minute slots'. Combined with
    DoctorAvailabilityException and already-booked ConsultationPreBookings
    at query time to compute a specific day's open slots."""

    DAY_CHOICES = [
        (0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"),
        (4, "Friday"), (5, "Saturday"), (6, "Sunday"),
    ]

    doctor = models.ForeignKey(
        "doctor.DoctorProfile",
        on_delete=models.CASCADE,
        related_name="weekly_availabilities",
    )
    day_of_week = models.PositiveSmallIntegerField(choices=DAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()
    slot_duration_minutes = models.PositiveIntegerField(default=15)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    MIN_SLOT_DURATION_MINUTES = 4

    def clean(self):
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValidationError({"end_time": "End time must be after start time."})
        if not self.slot_duration_minutes or self.slot_duration_minutes < self.MIN_SLOT_DURATION_MINUTES:
            raise ValidationError({
                "slot_duration_minutes": f"Slot duration must be at least {self.MIN_SLOT_DURATION_MINUTES} minutes."
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.doctor_id} - {self.get_day_of_week_display()} {self.start_time}-{self.end_time}"

    class Meta:
        ordering = ["day_of_week", "start_time"]
        verbose_name = "Doctor Weekly Availability"
        verbose_name_plural = "Doctor Weekly Availabilities"


class DoctorAvailabilityException(models.Model):
    """A one-off override for a single date -- either the doctor is fully
    unavailable that day, or their hours for that day differ from the
    normal weekly rule (e.g. shortened hours before a holiday). Also
    doubles as a standalone "duty day" — a manager can add a specific
    date + time-window + slot duration for a doctor who has NO weekly
    availability rule at all; the public availability computation reads
    exceptions before weekly rules, so this works entirely on its own."""

    doctor = models.ForeignKey(
        "doctor.DoctorProfile",
        on_delete=models.CASCADE,
        related_name="availability_exceptions",
    )
    date = models.DateField()
    is_unavailable = models.BooleanField(default=False)
    start_time = models.TimeField(blank=True, null=True)
    end_time = models.TimeField(blank=True, null=True)
    slot_duration_minutes = models.PositiveIntegerField(
        blank=True, null=True,
        help_text=(
            "Only used when is_unavailable=False. Slot length for this specific "
            "date. If left blank, falls back to the doctor's weekly rule for "
            "that weekday if one exists, or 15 minutes otherwise — set this "
            "explicitly so a duty day works correctly even with no weekly "
            "rule configured at all."
        ),
    )

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.is_unavailable:
            if self.start_time or self.end_time or self.slot_duration_minutes:
                raise ValidationError({
                    "is_unavailable": "Don't set start_time/end_time/slot_duration_minutes on a full-day-off exception."
                })
        else:
            if not self.start_time or not self.end_time:
                raise ValidationError({
                    "start_time": "Provide start_time and end_time, or set is_unavailable instead."
                })
            if self.start_time >= self.end_time:
                raise ValidationError({"end_time": "End time must be after start time."})
            if (
                self.slot_duration_minutes is not None
                and self.slot_duration_minutes < DoctorWeeklyAvailability.MIN_SLOT_DURATION_MINUTES
            ):
                raise ValidationError({
                    "slot_duration_minutes": (
                        f"Slot duration must be at least "
                        f"{DoctorWeeklyAvailability.MIN_SLOT_DURATION_MINUTES} minutes."
                    )
                })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        if self.is_unavailable:
            return f"{self.doctor_id} - {self.date} (Unavailable)"
        return f"{self.doctor_id} - {self.date} ({self.start_time}-{self.end_time})"

    class Meta:
        ordering = ["-date"]
        verbose_name = "Doctor Availability Exception"
        unique_together = [("doctor", "date")]


class PatientQuery(models.Model):
    """Manager-reviewed inbox for the public site's Contact form."""

    STATUS_CHOICES = [
        ("NEW", "New"),
        ("IN_PROGRESS", "In Progress"),
        ("RESOLVED", "Resolved"),
    ]

    # ✅ FIX: the Contact page has two visually distinct tabs ("Appointment
    # Queries" vs "Feedback/Complaints") but nothing on the backend
    # recorded which one a visitor meant -- every enquiry landed in the
    # same undifferentiated inbox. A manager triaging appointment
    # requests (time-sensitive) couldn't separate them from feedback
    # (not time-sensitive) without reading every message.
    QUERY_TYPE_CHOICES = [
        ("APPOINTMENT", "Appointment Query"),
        ("FEEDBACK", "Feedback / Complaint"),
    ]

    query_type = models.CharField(max_length=15, choices=QUERY_TYPE_CHOICES, default="APPOINTMENT", db_index=True)
    name = models.CharField(max_length=200)
    phone = models.CharField(
        max_length=10,
        validators=[RegexValidator(r"^\d{10}$", "Phone must be exactly 10 digits.")],
    )
    email = models.EmailField(blank=True, null=True)
    message = models.TextField()
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="NEW", db_index=True)

    # ✅ FIX: the actual gap in an otherwise branch-less CMS app — a
    # visitor filling the public contact form has no way to say which
    # branch they mean, and a manager reviewing the inbox has no way to
    # route it. Nullable: general enquiries not tied to a specific branch
    # stay visible group-wide; branch-tagged ones route to that branch's
    # queue.
    preferred_branch = models.ForeignKey(
        "administration.Branch",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="patient_queries",
        help_text="Branch the enquiry should be routed to, if known.",
    )

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not self.name or not self.name.strip():
            raise ValidationError({"name": "Name cannot be blank."})
        if not self.message or not self.message.strip():
            raise ValidationError({"message": "Message cannot be blank."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} - {self.status} - {self.created_at:%Y-%m-%d}"

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Patient Query"
        verbose_name_plural = "Patient Queries"


# ─────────────────────────────────────────────
# SPECIALTY / DEPARTMENT CMS
# ─────────────────────────────────────────────
# Single-hospital assumption for now (confirmed with the task owner —
# more branches may come later, but that's a separate future change: a
# Hospital FK could be bolted onto Specialty/Procedure/Blog at that point
# without disturbing anything below).

def _specialty_hero_image_path(instance, filename):
    return f"website/specialities/{filename}"


def _blog_cover_image_path(instance, filename):
    return f"website/blogs/{filename}"


def _procedure_hero_image_path(instance, filename):
    return f"website/procedures/{filename}"


def _unique_slug_for(model_cls, value, instance_pk=None):
    """Shared slug-uniqueness helper: slugify `value`, then append -2, -3,
    etc. if that slug is already taken by a *different* row."""
    base_slug = slugify(value) or "item"
    slug = base_slug
    counter = 2
    qs = model_cls.objects.filter(slug=slug)
    if instance_pk:
        qs = qs.exclude(pk=instance_pk)
    while qs.exists():
        slug = f"{base_slug}-{counter}"
        counter += 1
        qs = model_cls.objects.filter(slug=slug)
        if instance_pk:
            qs = qs.exclude(pk=instance_pk)
    return slug


class Specialty(models.Model):
    """A specialty/department page on the public site (e.g. 'Cardiac
    Sciences'), optionally nesting sub-specialties (e.g. 'Cardiology',
    'Cardiothoracic and Vascular Surgery') under a top-level one via
    `parent`."""

    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(
        max_length=220, unique=True, blank=True,
        help_text="Auto-generated from name if left blank. Used in the public URL, e.g. /speciality/cardiac-sciences.",
    )
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="sub_specialties",
        help_text="Set this to nest under a top-level specialty, e.g. 'Cardiology' under 'Cardiac Sciences'.",
    )
    short_description = models.CharField(
        max_length=300, blank=True, default="",
        help_text="Used in cards/listings.",
    )
    long_description = models.TextField(
        blank=True, default="",
        help_text="The hero paragraph shown on the specialty detail page.",
    )
    hero_image = models.ImageField(upload_to=_specialty_hero_image_path, blank=True, null=True, validators=[validate_image_file_size])
    is_published = models.BooleanField(
        default=False, help_text="Only published specialties appear on the public website.",
    )
    display_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not self.name or not self.name.strip():
            raise ValidationError({"name": "Name cannot be blank."})
        if self.parent_id and self.pk and self.parent_id == self.pk:
            raise ValidationError({"parent": "A specialty cannot be its own parent."})
        if not self.slug:
            self.slug = _unique_slug_for(Specialty, self.name, instance_pk=self.pk)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["display_order", "name"]
        verbose_name_plural = "Specialties"


class SpecialtySection(models.Model):
    """A free-form bulleted content block on a specialty detail page —
    e.g. 'Why Choose Us?', 'Treatments and Procedures at RHIMS HEALTH',
    'Available Facilities and Equipment'. Each is a heading + optional
    intro paragraph + a bullet list (one item per line in `items`),
    ordered per-specialty via `display_order` so editors can add as many
    of these blocks as a given specialty page needs."""

    specialty = models.ForeignKey(Specialty, on_delete=models.CASCADE, related_name="content_sections")
    heading = models.CharField(
        max_length=200,
        help_text="e.g. 'Why Choose Us?', 'Treatments and Procedures at RHIMS HEALTH'.",
    )
    intro = models.TextField(
        blank=True, default="",
        help_text="Optional paragraph shown above the bullet list.",
    )
    items = models.TextField(
        blank=True, default="",
        help_text="One bullet point per line, shown directly under the main heading/intro.",
    )
    # ✅ NEW: optional sub-headings within this section. Each sub-heading
    # is shown plain (no "a./b./c." prefix — that lettering now only
    # applies one level deeper, see `alphabet_list` below) with an
    # optional short description paragraph, followed by EITHER a plain
    # bullet list OR a nested "alphabet list": a set of auto-lettered
    # entries (a., b., c., … computed at render time from position,
    # never stored), where each lettered entry itself has its own
    # optional description paragraph AND its own optional bullet list
    # (both usable together on the same entry). `items` and
    # `alphabet_list` are independent alternatives — a sub-heading uses
    # one or the other, not both.
    # Shape: [{
    #   "heading": str, "description": str, "items": [str, ...],
    #   "alphabet_list": [{"description": str, "items": [str, ...]}, ...],
    # }, ...]
    subsections = models.JSONField(
        default=list, blank=True,
        help_text='Optional list of {"heading": str, "description": str, "items": [str, ...], "alphabet_list": [{"description": str, "items": [str, ...]}, ...]} sub-groups. Each renders as a plain sub-heading with an optional description, followed by either its bullet list or its auto-lettered (a., b., c., …) alphabet list.',
    )
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def items_list(self):
        return [line.strip() for line in self.items.splitlines() if line.strip()]

    def subsections_list(self):
        """Normalized [{"heading": str, "description": str, "items": [str, ...],
        "alphabet_list": [{"description": str, "items": [str, ...]}, ...]}, ...],
        dropping any sub-heading (or alphabet-list entry) left with
        nothing filled in at all."""
        cleaned = []
        for entry in (self.subsections or []):
            if not isinstance(entry, dict):
                continue
            heading = str(entry.get("heading", "")).strip()
            description = str(entry.get("description", "")).strip()
            items = [str(i).strip() for i in entry.get("items", []) if str(i).strip()]
            alphabet_list = []
            for a_entry in entry.get("alphabet_list", []) or []:
                if not isinstance(a_entry, dict):
                    continue
                a_description = str(a_entry.get("description", "")).strip()
                a_items = [str(i).strip() for i in a_entry.get("items", []) if str(i).strip()]
                if not a_description and not a_items:
                    continue
                alphabet_list.append({"description": a_description, "items": a_items})
            if not heading and not description and not items and not alphabet_list:
                continue
            cleaned.append({
                "heading": heading, "description": description,
                "items": items, "alphabet_list": alphabet_list,
            })
        return cleaned

    def clean(self):
        if self.subsections is None:
            self.subsections = []
        if not isinstance(self.subsections, list):
            raise ValidationError({"subsections": "Must be a list of {heading, description, items} sub-groups."})
        for entry in self.subsections:
            if not isinstance(entry, dict):
                raise ValidationError({"subsections": "Each sub-group must be an object with 'heading', 'description' and 'items'."})
            if "heading" not in entry:
                raise ValidationError({"subsections": "Each sub-group needs a 'heading'."})
            description = entry.get("description", "")
            if description is not None and not isinstance(description, str):
                raise ValidationError({"subsections": "Each sub-group's 'description' must be a string."})
            items = entry.get("items", [])
            if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
                raise ValidationError({"subsections": "Each sub-group's 'items' must be a list of strings."})
            alphabet_list = entry.get("alphabet_list", [])
            if not isinstance(alphabet_list, list):
                raise ValidationError({"subsections": "Each sub-group's 'alphabet_list' must be a list."})
            for a_entry in alphabet_list:
                if not isinstance(a_entry, dict):
                    raise ValidationError({"subsections": "Each 'alphabet_list' entry must be an object with 'description' and 'items'."})
                a_description = a_entry.get("description", "")
                if a_description is not None and not isinstance(a_description, str):
                    raise ValidationError({"subsections": "Each 'alphabet_list' entry's 'description' must be a string."})
                a_items = a_entry.get("items", [])
                if not isinstance(a_items, list) or not all(isinstance(i, str) for i in a_items):
                    raise ValidationError({"subsections": "Each 'alphabet_list' entry's 'items' must be a list of strings."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.specialty.name} — {self.heading}"

    class Meta:
        ordering = ["display_order", "id"]


class Treatment(models.Model):
    """A disease or procedure listed under a specialty's 'Diseases &
    Treatments' section. Diseases and procedures are treated as one
    tagged list (matching the reference site's tag-chip UI) rather than
    two separate models — `kind` distinguishes them.

    ✅ FIX: renamed from Procedure -> Treatment (model + table rename via
    migration). This was a genuine naming collision with
    administration.Procedure, the branch-priced service catalog used for
    real billing — a completely different concept (public CMS content vs
    a branch's payable procedure list) that happened to share a class
    name. Public API paths / view / serializer names under "procedures"
    are left as-is (cosmetic only, no functional collision there); only
    the model itself and its Python-level references change.

    Beyond the chip/tag itself, each one can now also have a full public
    detail page (hero image + structured body content), matching the
    reference site's disease/procedure pages — e.g. /diseases/<slug> or
    /procedures/<slug>, breadcrumb label driven by `kind`. A Procedure
    with no `sections` filled in still works fine as just a chip with no
    detail page content beyond its `summary`."""

    KIND_CHOICES = [
        ("Disease", "Disease"),
        ("Procedure", "Procedure"),
    ]

    specialty = models.ForeignKey(Specialty, on_delete=models.CASCADE, related_name="procedures")
    name = models.CharField(max_length=250)
    slug = models.SlugField(
        max_length=255, unique=True, blank=True,
        help_text="Auto-generated from name if left blank. Used in the public URL, e.g. /diseases/ankylosing-spondylitis.",
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    summary = models.TextField(
        blank=True, default="",
        help_text="Short blurb — used as the chip tooltip in the 'Diseases & Procedures' list, and as the lead paragraph at the top of the detail page.",
    )
    hero_image = models.ImageField(
        upload_to=_procedure_hero_image_path, blank=True, null=True,
        validators=[validate_image_file_size],
        help_text="Shown in the detail page hero, beside the 'Book An Appointment' form.",
    )
    # Same shape/semantics as SpecialtySection.subsections — a list of
    # {heading, description, items, alphabet_list} blocks, rendered in
    # order down the page. Reused here rather than modeling a separate
    # "ProcedureSection" table since a disease/procedure page's body is
    # the same kind of content (e.g. "What symptoms does X Cause?" +
    # paragraph/bullets, "Who is More at Risk?" + bullets, ...).
    # Shape: [{
    #   "heading": str, "description": str, "items": [str, ...],
    #   "alphabet_list": [{"description": str, "items": [str, ...]}, ...],
    # }, ...]
    sections = models.JSONField(
        default=list, blank=True,
        help_text='Optional list of {"heading": str, "description": str, "items": [str, ...], "alphabet_list": [{"description": str, "items": [str, ...]}, ...]} content blocks making up the detail page body, e.g. "What symptoms does X Cause?" + a paragraph/bullets, "Who is More at Risk?" + bullets.',
    )
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def sections_list(self):
        """Normalized [{"heading": str, "description": str, "items": [str, ...],
        "alphabet_list": [{"description": str, "items": [str, ...]}, ...]}, ...],
        dropping any block (or alphabet-list entry) left with nothing
        filled in at all. Same normalization as
        SpecialtySection.subsections_list()."""
        cleaned = []
        for entry in (self.sections or []):
            if not isinstance(entry, dict):
                continue
            heading = str(entry.get("heading", "")).strip()
            description = str(entry.get("description", "")).strip()
            items = [str(i).strip() for i in entry.get("items", []) if str(i).strip()]
            alphabet_list = []
            for a_entry in entry.get("alphabet_list", []) or []:
                if not isinstance(a_entry, dict):
                    continue
                a_description = str(a_entry.get("description", "")).strip()
                a_items = [str(i).strip() for i in a_entry.get("items", []) if str(i).strip()]
                if not a_description and not a_items:
                    continue
                alphabet_list.append({"description": a_description, "items": a_items})
            if not heading and not description and not items and not alphabet_list:
                continue
            cleaned.append({
                "heading": heading, "description": description,
                "items": items, "alphabet_list": alphabet_list,
            })
        return cleaned

    def clean(self):
        if not self.name or not self.name.strip():
            raise ValidationError({"name": "Name cannot be blank."})
        if not self.slug:
            self.slug = _unique_slug_for(Treatment, self.name, instance_pk=self.pk)

        if self.sections is None:
            self.sections = []
        if not isinstance(self.sections, list):
            raise ValidationError({"sections": "Must be a list of {heading, description, items} content blocks."})
        for entry in self.sections:
            if not isinstance(entry, dict):
                raise ValidationError({"sections": "Each block must be an object with 'heading', 'description' and 'items'."})
            if "heading" not in entry:
                raise ValidationError({"sections": "Each block needs a 'heading'."})
            description = entry.get("description", "")
            if description is not None and not isinstance(description, str):
                raise ValidationError({"sections": "Each block's 'description' must be a string."})
            items = entry.get("items", [])
            if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
                raise ValidationError({"sections": "Each block's 'items' must be a list of strings."})
            alphabet_list = entry.get("alphabet_list", [])
            if not isinstance(alphabet_list, list):
                raise ValidationError({"sections": "Each block's 'alphabet_list' must be a list."})
            for a_entry in alphabet_list:
                if not isinstance(a_entry, dict):
                    raise ValidationError({"sections": "Each 'alphabet_list' entry must be an object with 'description' and 'items'."})
                a_description = a_entry.get("description", "")
                if a_description is not None and not isinstance(a_description, str):
                    raise ValidationError({"sections": "Each 'alphabet_list' entry's 'description' must be a string."})
                a_items = a_entry.get("items", [])
                if not isinstance(a_items, list) or not all(isinstance(i, str) for i in a_items):
                    raise ValidationError({"sections": "Each 'alphabet_list' entry's 'items' must be a list of strings."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.kind})"

    class Meta:
        ordering = ["display_order", "name"]


class Blog(models.Model):
    """A blog/article post for the public site's blog listing, optionally
    scoped to a specialty."""

    specialty = models.ForeignKey(
        Specialty, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="blogs",
        help_text="Optional — a blog post may not always map cleanly to one specialty.",
    )
    # ✅ FIX: was a direct FK to doctor.DoctorProfile (one specific
    # branch's row). Blog is shared CMS content, same as the rest of Web
    # Management -- crediting a post to one branch-specific login meant a
    # different branch's manager couldn't pick that doctor as author, and
    # the byline vanished (on_delete=SET_NULL) if that particular branch
    # row was ever removed even though the doctor still practices
    # elsewhere in the group. Points at the shared public identity now,
    # same fix as DoctorWebsiteProfile.doctors -- see that model's
    # docstring.
    author_doctor = models.ForeignKey(
        "manager.DoctorWebsiteProfile", on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="blogs",
    )
    title = models.CharField(max_length=250)
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    excerpt = models.CharField(max_length=300, blank=True, default="", help_text="Short card summary.")
    body = models.TextField()
    cover_image = models.ImageField(upload_to=_blog_cover_image_path, blank=True, null=True, validators=[validate_image_file_size])
    is_published = models.BooleanField(default=False, help_text="Only published blogs appear on the public site.")
    display_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not self.title or not self.title.strip():
            raise ValidationError({"title": "Title cannot be blank."})
        if not self.body or not self.body.strip():
            raise ValidationError({"body": "Body cannot be blank."})
        if not self.slug:
            self.slug = _unique_slug_for(Blog, self.title, instance_pk=self.pk)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title

    class Meta:
        ordering = ["display_order", "-created_at"]


def _testimonial_photo_path(instance, filename):
    return f"website/testimonials/{filename}"


class Testimonial(models.Model):
    patient_name = models.CharField(max_length=200)
    designation = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Optional, e.g. 'Cardiac patient', 'Attendant of patient'.",
    )
    review = models.TextField(help_text="The testimonial text itself.")
    photo = models.ImageField(upload_to=_testimonial_photo_path, blank=True, null=True, validators=[validate_image_file_size])
    specialty = models.ForeignKey(
        Specialty, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="testimonials",
    )
    rating = models.PositiveSmallIntegerField(
        blank=True, null=True,
        validators=[MinValueValidator(1)],
        help_text="1-5 stars, optional.",
    )
    is_active = models.BooleanField(default=False, help_text="Only active testimonials appear on the public site.")
    is_featured = models.BooleanField(default=False, help_text="Featured testimonials can be highlighted separately, e.g. on the homepage hero.")
    display_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not self.patient_name or not self.patient_name.strip():
            raise ValidationError({"patient_name": "Patient name cannot be blank."})
        if not self.review or not self.review.strip():
            raise ValidationError({"review": "Review cannot be blank."})
        if self.rating is not None and self.rating > 5:
            raise ValidationError({"rating": "Rating must be between 1 and 5."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.patient_name} - {'Active' if self.is_active else 'Inactive'}"

    class Meta:
        ordering = ["display_order", "-created_at"]


# ── YouTube video ID extraction ──────────────────────────────────────
# Supports the shapes managers are likely to paste in:
#   https://www.youtube.com/watch?v=VIDEOID
#   https://youtu.be/VIDEOID
#   https://www.youtube.com/embed/VIDEOID
#   https://www.youtube.com/shorts/VIDEOID
# and a bare 11-character video ID typed directly.
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_youtube_video_id(url_or_id):
    """Return the 11-char YouTube video ID from a URL, or None if the
    input doesn't look like a valid YouTube URL/ID at all."""
    if not url_or_id:
        return None
    candidate = url_or_id.strip()

    if _YOUTUBE_ID_RE.match(candidate):
        return candidate

    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower().replace("www.", "").replace("m.", "")

    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0]
        return video_id if _YOUTUBE_ID_RE.match(video_id) else None

    if host in ("youtube.com", "youtube-nocookie.com"):
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
            return video_id if video_id and _YOUTUBE_ID_RE.match(video_id) else None
        for prefix in ("/embed/", "/shorts/", "/live/"):
            if parsed.path.startswith(prefix):
                video_id = parsed.path[len(prefix):].split("/")[0]
                return video_id if _YOUTUBE_ID_RE.match(video_id) else None

    return None


class YoutubeVideo(models.Model):
    """A YouTube video featured on the public site (replaces the earlier,
    more generic ExpertTalk model — split into YoutubeVideo/InstagramPost
    per the dedicated CMS requirements)."""

    youtube_url = models.URLField(help_text="Full YouTube URL, e.g. https://www.youtube.com/watch?v=XXXXXXXXXXX")
    video_id = models.CharField(max_length=20, editable=False, db_index=True)
    thumbnail_url = models.URLField(editable=False, blank=True)
    title = models.CharField(max_length=250, blank=True, default="")
    description = models.TextField(blank=True, default="")
    # ✅ FIX: was a direct FK to doctor.DoctorProfile (one specific
    # branch's row) -- same issue and same fix as Blog.author_doctor
    # above: this is shared CMS content, so the featured doctor should be
    # the shared public identity, not one branch-specific login.
    doctor = models.ForeignKey(
        "manager.DoctorWebsiteProfile",
        on_delete=models.SET_NULL,
        blank=True, null=True,
        related_name="youtube_videos",
        help_text="Optional featured doctor for this video.",
    )
    specialty = models.ForeignKey(
        Specialty, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="youtube_videos",
        help_text="Optional — for videos not tied to one specific doctor, or so specialty-filtering doesn't depend on joining through the doctor.",
    )
    display_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=False, help_text="Only active videos appear on the public site.")
    is_featured = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        video_id = extract_youtube_video_id(self.youtube_url)
        if not video_id:
            raise ValidationError({"youtube_url": "That doesn't look like a valid YouTube URL."})
        self.video_id = video_id
        self.thumbnail_url = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title or self.video_id

    class Meta:
        ordering = ["display_order", "-created_at"]
        verbose_name = "YouTube Video"


# ── Instagram URL validation (no scraping — we only ever store the URL) ──
_INSTAGRAM_URL_RE = re.compile(
    r"^https?://(www\.)?instagram\.com/(p|reel|tv)/[A-Za-z0-9_-]+/?(\?.*)?$"
)


def is_valid_instagram_url(url):
    return bool(url and _INSTAGRAM_URL_RE.match(url.strip()))


def _instagram_thumbnail_path(instance, filename):
    return f"website/instagram/{filename}"


class InstagramPost(models.Model):
    """An Instagram post embed for the public site — URL only, no
    scraping; the frontend renders Instagram's own embed for the URL.

    thumbnail: optional, manually-uploaded preview image. We never scrape
    Instagram for the post's photo, so there's no thumbnail unless a staff
    member uploads one here; the frontend falls back to a text-only card
    when it's blank."""

    instagram_url = models.URLField(help_text="Full Instagram post/reel URL, e.g. https://www.instagram.com/p/XXXXXXXXX/")
    caption = models.TextField(blank=True, default="")
    thumbnail = models.ImageField(
        upload_to=_instagram_thumbnail_path, blank=True, null=True,
        validators=[validate_image_file_size],
        help_text="Optional preview image, uploaded manually — we don't scrape Instagram for this.",
    )
    display_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=False, help_text="Only active posts appear on the public site.")
    is_featured = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not is_valid_instagram_url(self.instagram_url):
            raise ValidationError({"instagram_url": "That doesn't look like a valid Instagram post/reel URL."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.instagram_url

    class Meta:
        ordering = ["display_order", "-created_at"]
        verbose_name = "Instagram Post"


# ── Facebook URL validation (no scraping — we only ever store the URL) ──
# Facebook post/video/reel URLs come in several shapes depending on how
# they were shared (a page's /posts/, /videos/, /reel/, watch?v=, or the
# share.php redirect link) — this is deliberately permissive about the
# path and just anchors on it being a facebook.com/fb.watch link, same
# trade-off as the Instagram regex above.
_FACEBOOK_URL_RE = re.compile(
    r"^https?://(www\.|m\.|web\.)?(facebook\.com|fb\.watch)/.+"
)


def is_valid_facebook_url(url):
    return bool(url and _FACEBOOK_URL_RE.match(url.strip()))


def _facebook_thumbnail_path(instance, filename):
    return f"website/facebook/{filename}"


class FacebookPost(models.Model):
    """A Facebook post/video embed for the public site — URL only, no
    scraping; the frontend links out to the URL rather than embedding
    Facebook's own widget.

    thumbnail: optional, manually-uploaded preview image, same trade-off
    as InstagramPost.thumbnail — we never scrape Facebook for the post's
    photo, so there's no thumbnail unless a staff member uploads one."""

    facebook_url = models.URLField(help_text="Full Facebook post/video/reel URL, e.g. https://www.facebook.com/PageName/posts/XXXXXXXXX")
    caption = models.TextField(blank=True, default="")
    thumbnail = models.ImageField(
        upload_to=_facebook_thumbnail_path, blank=True, null=True,
        validators=[validate_image_file_size],
        help_text="Optional preview image, uploaded manually — we don't scrape Facebook for this.",
    )
    display_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=False, help_text="Only active posts appear on the public site.")
    is_featured = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not is_valid_facebook_url(self.facebook_url):
            raise ValidationError({"facebook_url": "That doesn't look like a valid Facebook URL."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.facebook_url

    class Meta:
        ordering = ["display_order", "-created_at"]
        verbose_name = "Facebook Post"


def _media_event_cover_image_path(instance, filename):
    return f"website/media-events/{filename}"


class MediaEvent(models.Model):
    """A "Media & Events" article for the public site — hospital news,
    press coverage, camps, awards, conferences, etc. Same shape as Blog
    (title/slug/excerpt/body/cover_image) since it's the same kind of
    content — a dated article with a "Read More" detail page — just a
    separate listing so the two aren't mixed together on the site."""

    title = models.CharField(max_length=250)
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    excerpt = models.CharField(max_length=300, blank=True, default="", help_text="Short card summary.")
    body = models.TextField()
    cover_image = models.ImageField(upload_to=_media_event_cover_image_path, blank=True, null=True, validators=[validate_image_file_size])
    event_date = models.DateField(
        null=True, blank=True,
        help_text="Optional — when the event/news item actually happened, for display on the card.",
    )
    is_active = models.BooleanField(default=False, help_text="Only active items appear on the public site.")
    is_featured = models.BooleanField(default=False)
    display_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not self.title or not self.title.strip():
            raise ValidationError({"title": "Title cannot be blank."})
        if not self.body or not self.body.strip():
            raise ValidationError({"body": "Body cannot be blank."})
        if not self.slug:
            self.slug = _unique_slug_for(MediaEvent, self.title, instance_pk=self.pk)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title

    class Meta:
        ordering = ["display_order", "-event_date", "-created_at"]
        verbose_name = "Media & Event"
        verbose_name_plural = "Media & Events"


def _gallery_image_path(instance, filename):
    return f"website/gallery/{filename}"


class GalleryImage(models.Model):
    """A single photo in the public site's "Our Gallery" grid — no
    detail page, no caption required; just an image, an optional
    caption, and ordering. Deliberately the simplest of the four
    "section + View All" content types added around this one — the
    others (Testimonials, Blog, Media & Events) all have a title/body,
    this one is image-first."""

    image = models.ImageField(upload_to=_gallery_image_path, validators=[validate_image_file_size])
    caption = models.CharField(max_length=200, blank=True, default="")
    is_active = models.BooleanField(default=False, help_text="Only active images appear on the public site.")
    display_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.caption or f"Gallery image #{self.pk}"

    class Meta:
        ordering = ["display_order", "-created_at"]
        verbose_name = "Gallery Image"