from decimal import Decimal
from django.db import models, transaction
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.utils import timezone


# ======================================
# LAB REQUEST STATUS
# ======================================
class LabRequestStatus(models.TextChoices):
    REQUESTED        = 'REQUESTED',        'Requested'
    SAMPLE_COLLECTED = 'SAMPLE_COLLECTED', 'Sample Collected'
    PROCESSING       = 'PROCESSING',       'Processing'
    COMPLETED        = 'COMPLETED',        'Completed'
    VERIFIED         = 'VERIFIED',         'Verified'
    DELIVERED        = 'DELIVERED',        'Delivered'


# ======================================
# TEST GROUP  (panel of tests billed as one unit)
# ======================================
class TestGroup(models.Model):
    """
    A group/panel of lab tests sold and billed together as a single unit
    (e.g. 'Liver Function Test' = SGOT + SGPT + Bilirubin + ALP).

    Each sub-test (see LabTest.group) still records its own result value
    and normal range individually — grouping only affects billing: the
    patient is charged `price` once for the whole panel instead of once
    per sub-test. See calculate_lab_subtotal() below.
    """

    group_id     = models.AutoField(primary_key=True)

    # ✅ FIX: no branch field — TestGroup had global unique=True on name and
    # code, same gap as the pharmacy catalogue models. branch FK +
    # branch-scoped unique_together on each, matching the catalog treatment
    # decided for Medicine/SupplyItem/GeneralItem.
    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="test_groups",
    )

    name         = models.CharField(max_length=200)
    code         = models.CharField(
        max_length=50,
        help_text="Short code e.g. LFT, KFT, LIPID"
    )
    description  = models.TextField(blank=True, null=True)
    price        = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="The single price charged for the whole panel."
    )
    is_active    = models.BooleanField(default=True)
    created_at   = models.DateTimeField(default=timezone.now, editable=False)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        unique_together = [('branch', 'name'), ('branch', 'code')]

    def clean(self):
        if not self.name or not self.name.strip():
            raise ValidationError({'name': 'Group name cannot be blank.'})
        if not self.code or not self.code.strip():
            raise ValidationError({'code': 'Group code cannot be blank.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} — {self.name}"


# ======================================
# LAB TEST  (master catalog)
# ======================================
class LabTest(models.Model):
    """Master catalog of all available lab tests."""

    test_id      = models.AutoField(primary_key=True)

    # ✅ FIX: no branch field — same gap as TestGroup. branch FK +
    # branch-scoped unique_together on name and code.
    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="lab_tests",
    )

    name         = models.CharField(max_length=200)
    code         = models.CharField(
        max_length=50,
        help_text="Short code e.g. CBC, LFT, RBS"
    )
    description  = models.TextField(blank=True, null=True)
    normal_range = models.TextField(blank=True, null=True)
    unit         = models.CharField(max_length=50, blank=True, null=True)
    price        = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active    = models.BooleanField(default=True)

    # Optional membership in a billing panel. Most tests stay standalone
    # (group=None). When set, this test is one of the sub-tests that make
    # up the group's panel — it still keeps its own normal_range/unit and
    # gets its own LabResult, but is billed as part of the group's price
    # only when ordered via that group (see LabRequestItem.ordered_as_group).
    # The test's own `price` remains meaningful for when it's ordered
    # standalone, outside of its group.
    group = models.ForeignKey(
        TestGroup,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='sub_tests',
        help_text='Optional panel this test belongs to (e.g. LFT). Leave blank for a standalone test.',
    )

    created_at   = models.DateTimeField(default=timezone.now, editable=False)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        unique_together = [('branch', 'name'), ('branch', 'code')]

    def clean(self):
        if not self.name or not self.name.strip():
            raise ValidationError({'name': 'Test name cannot be blank.'})
        if not self.code or not self.code.strip():
            raise ValidationError({'code': 'Test code cannot be blank.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} — {self.name}"


# ======================================
# LAB REQUEST  (per consultation)
# ======================================
class LabRequest(models.Model):
    """
    A lab investigation request, either:
      - created by a doctor for a consultation (registered patient,
        consultation is set, is_walkin=False), or
      - created directly by the lab technician for a walk-in patient who
        comes straight to the lab without a doctor consultation / MRD
        registration (consultation and patient are NULL, is_walkin=True).

    One consultation can have multiple LabRequests.

    This is the canonical owner of the lab_requests relation on
    doctor.Consultation — doctor/models.py does NOT define its own
    LabRequest model.
    """

    request_id = models.AutoField(primary_key=True)

    # ✅ FIX: no branch field — LabRequest had no way to scope a walk-in
    # request to a branch, same gap as PharmacyBill's walk-in case. Auto-
    # derived from patient.branch on save when a registered patient is
    # set; required explicitly for walk-in requests (is_walkin=True,
    # patient=None).
    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="lab_requests",
        null=True, blank=True,
        help_text="Auto-set from patient.branch on save if not provided. Required for walk-in requests.",
    )

    consultation = models.ForeignKey(
        'doctor.Consultation',
        on_delete=models.CASCADE,
        related_name='lab_requests',   # Consultation.lab_requests.all()
        null=True, blank=True,
        help_text='NULL for walk-in requests (is_walkin=True).',
    )

    patient = models.ForeignKey(
        'reception.Patient',
        on_delete=models.CASCADE,
        related_name='lab_requests',
        null=True, blank=True,
        help_text='NULL for walk-in requests (is_walkin=True).',
    )

    requested_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='lab_requests_made',
    )

    # ── Claim workflow ──────────────────────────────────────────────
    # Any lab staff member can claim an unclaimed request so only they can
    # act on it (enter results / change status). Everyone else still sees
    # it in the list, marked "Claimed by {name}", but read-only.
    claimed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='lab_requests_claimed',
        help_text='Lab staff member who has claimed this request. NULL if unclaimed.',
    )
    claimed_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When this request was claimed.',
    )

    status = models.CharField(
        max_length=20,
        choices=LabRequestStatus.choices,
        default=LabRequestStatus.REQUESTED,   # First step in the workflow
        db_index=True,
    )

    request_date = models.DateField(default=timezone.localdate)  # FIX: localdate returns a date, not a datetime
    notes        = models.TextField(blank=True, null=True)

    # ── Walk-in patient fields ─────────────────────────────────────
    # Mirrors pharmacist.PharmacyBill's walk-in pattern: a patient who
    # walks directly into the lab for a test, with no doctor consultation
    # and no MRD registration.
    is_walkin = models.BooleanField(
        default=False,
        help_text='True if this is a walk-in lab request (no consultation / MRD registration)',
    )
    walkin_name = models.CharField(
        max_length=300, blank=True, null=True,
        help_text='Walk-in patient name (used only if is_walkin=True)',
    )
    walkin_phone = models.CharField(
        max_length=10, blank=True, null=True,
        validators=[RegexValidator(r'^\d{10}$', 'Phone must be exactly 10 digits.')],
        help_text='Walk-in patient phone (optional, used only if is_walkin=True)',
    )
    GENDER_CHOICES = [('Male', 'Male'), ('Female', 'Female'), ('Other', 'Other')]
    walkin_gender = models.CharField(
        max_length=10, choices=GENDER_CHOICES, blank=True, null=True,
        help_text='Walk-in patient gender (optional)',
    )
    walkin_age = models.PositiveIntegerField(
        blank=True, null=True,
        help_text='Walk-in patient age (optional)',
    )

    created_at   = models.DateTimeField(default=timezone.now, editable=False)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-request_id']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['patient', 'request_date']),
        ]

    def clean(self):
        super().clean()

        if self.is_walkin:
            if not self.walkin_name or not self.walkin_name.strip():
                raise ValidationError({'walkin_name': 'Walk-in patient name is required when is_walkin=True'})
            if self.patient is not None:
                raise ValidationError({'patient': 'Patient must be NULL for walk-in requests (is_walkin=True).'})
            if self.consultation is not None:
                raise ValidationError({'consultation': 'Consultation must be NULL for walk-in requests (is_walkin=True).'})
            if not self.branch_id:
                raise ValidationError({'branch': 'Branch is required for walk-in requests (no patient to derive it from).'})
        else:
            if self.patient is None:
                raise ValidationError({'patient': 'Patient is required for non-walk-in lab requests.'})
            if (self.walkin_name or self.walkin_phone or
                    self.walkin_gender or self.walkin_age is not None):
                raise ValidationError({
                    'walkin_fields': 'Walk-in patient fields must be empty for registered patient requests.'
                })

    def save(self, *args, **kwargs):
        if not self.branch_id and self.patient_id:
            self.branch = self.patient.branch
        self.full_clean()
        super().save(*args, **kwargs)

    def get_patient_name(self):
        if self.is_walkin:
            return self.walkin_name or 'Unknown'
        elif self.patient:
            return f"{self.patient.first_name} {self.patient.last_name}".strip()
        return 'Unknown'

    def get_patient_phone(self):
        if self.is_walkin:
            return self.walkin_phone
        elif self.patient:
            return getattr(self.patient, 'phone', None)
        return None

    def get_patient_gender(self):
        if self.is_walkin:
            return self.walkin_gender
        elif self.patient:
            return getattr(self.patient, 'gender', None)
        return None

    def get_patient_age(self):
        if self.is_walkin:
            return self.walkin_age
        elif self.patient:
            return getattr(self.patient, 'age', None)
        return None

    def get_patient_info_dict(self):
        if self.is_walkin:
            return {
                'type': 'walk-in',
                'name': self.walkin_name,
                'phone': self.walkin_phone,
                'gender': self.walkin_gender,
                'age': self.walkin_age,
            }
        elif self.patient:
            return {
                'type': 'registered',
                'mrd': getattr(self.patient, 'mrd_number', None),
                'patient_id': self.patient.patient_id,
                'name': f"{self.patient.first_name} {self.patient.last_name}".strip(),
                'phone': getattr(self.patient, 'phone', None),
            }
        return {'type': 'unknown', 'name': 'Unknown'}

    def __str__(self):
        if self.is_walkin:
            return f"LabRequest #{self.request_id} — Walk-in: {self.walkin_name} [{self.status}]"
        return (
            f"LabRequest #{self.request_id} — "
            f"Consultation #{self.consultation_id} [{self.status}]"
        )


# ======================================
# LAB REQUEST ITEM  (individual test within a request)
# ======================================
class LabRequestItem(models.Model):
    """Each row represents one test within a LabRequest."""

    item_id = models.AutoField(primary_key=True)

    lab_request = models.ForeignKey(
        LabRequest,
        on_delete=models.CASCADE,
        related_name='items',
    )

    # FK named 'test' — matches lab serializers, admin, and views.
    # doctor serializers must reference 'test' (not 'lab_test').
    test = models.ForeignKey(
        LabTest,
        on_delete=models.PROTECT,
        related_name='request_items',
    )

    # NULL = this row was ordered as a standalone test, billed at
    # test.price. Set = this row was added as part of a group order — all
    # sibling rows sharing the same (lab_request, ordered_as_group) pair
    # count as ONE billable unit at group.price (see
    # calculate_lab_subtotal() and add_test_group() below).
    ordered_as_group = models.ForeignKey(
        TestGroup,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='request_items',
    )

    notes = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['item_id']
        unique_together = [['lab_request', 'test']]

    def __str__(self):
        return f"Request #{self.lab_request_id} — {self.test.code}"


# ======================================
# LAB RESULT  (per LabRequestItem)
# ======================================
class LabResult(models.Model):
    """Result value entered by the lab technician for one test item."""

    result_id = models.AutoField(primary_key=True)

    lab_request_item = models.OneToOneField(
        LabRequestItem,
        on_delete=models.CASCADE,
        related_name='result',
    )

    result_value = models.TextField()
    normal_range = models.TextField(blank=True, null=True)
    is_abnormal  = models.BooleanField(default=False)
    remarks      = models.TextField(blank=True, null=True)

    performed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='lab_results_performed',
    )
    performed_at = models.DateTimeField(default=timezone.now)
    created_at   = models.DateTimeField(default=timezone.now, editable=False)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-result_id']

    def __str__(self):
        return (
            f"Result for {self.lab_request_item.test.code} "
            f"(Item #{self.lab_request_item_id})"
        )


# ======================================
# LAB REPORT  (finalised report for the whole LabRequest)
# ======================================
class LabReport(models.Model):
    """
    Compiled, verified report covering all items in a LabRequest.
    Auto-created when status reaches COMPLETED; marked VERIFIED/DELIVERED
    afterwards by LabRequestStatusUpdateView.
    """

    report_id = models.AutoField(primary_key=True)

    lab_request = models.OneToOneField(
        LabRequest,
        on_delete=models.CASCADE,
        related_name='report',
    )

    report_notes = models.TextField(blank=True, null=True)

    verified_by  = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='lab_reports_verified',
    )
    verified_at  = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-report_id']

    def __str__(self):
        return f"LabReport #{self.report_id} — Request #{self.lab_request_id}"


# ======================================
# LAB BILL
# ======================================
class BillPaymentStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    PAID    = 'PAID',    'Paid'


class BillPaymentMethod(models.TextChoices):
    CASH      = 'CASH',      'Cash'
    CARD      = 'CARD',      'Card'
    UPI       = 'UPI',       'UPI'
    INSURANCE = 'INSURANCE', 'Insurance'
    NONE      = 'NONE',      'None'


def calculate_lab_subtotal(items):
    """
    Shared subtotal helper -- the single source of truth for how a
    LabRequest's items translate into a LabBill.subtotal.

    items: queryset/list of LabRequestItem, ideally pre-fetched with
    .select_related('test', 'ordered_as_group') to avoid N+1 queries.

    Groups are billed once at group.price no matter how many sub-test
    rows share that (lab_request, ordered_as_group) pair; standalone
    tests (ordered_as_group is NULL) are billed individually at
    test.price.
    """
    subtotal = Decimal('0.00')
    counted_groups = set()
    for item in items:
        if item.ordered_as_group_id:
            if item.ordered_as_group_id not in counted_groups:
                subtotal += item.ordered_as_group.price
                counted_groups.add(item.ordered_as_group_id)
        else:
            subtotal += item.test.price
    return subtotal


def add_test_group(lab_request, test_group):
    """
    Expand a group order into one LabRequestItem per active sub-test,
    all tagged with ordered_as_group=test_group so they're billed once
    as a unit (see calculate_lab_subtotal above). Uses ignore_conflicts
    so an already-present sub-test (unique_together on lab_request+test)
    is silently skipped rather than raising.
    """
    sub_tests = LabTest.objects.filter(group=test_group, is_active=True)
    items = [
        LabRequestItem(lab_request=lab_request, test=t, ordered_as_group=test_group)
        for t in sub_tests
    ]
    LabRequestItem.objects.bulk_create(items, ignore_conflicts=True)


def _generate_lab_bill_number(bill_date, branch):
    """
    Continuous, branch-prefixed lab bill number: TVM-LAB-0001, KOL-LAB-0001,
    ... Never resets — keeps incrementing per branch across all days,
    matching the branch-prefixed convention used for pharmacy bills
    (pharmacist._generate_pharmacy_bill_number) and consultation bills
    (reception._generate_cons_bill_number) elsewhere in the system.

    ✅ FIX: this used to generate a flat "LAB-0001" sequence shared across
    every branch, with no branch parameter at all — even though
    pharmacist._generate_pharmacy_bill_number's own docstring already
    claimed lab bills followed this same branch-prefixed convention. That
    claim was wrong; this brings the code in line with it.

    Uses select_for_update() on the last matching row to avoid two
    concurrent bill creations landing on the same number; callers must
    invoke this inside a transaction.atomic() block (see LabBill.save()
    below).
    """
    prefix = f"{branch.code}-LAB-"
    last = (
        LabBill.objects
        .select_for_update()
        .filter(branch_id=branch.branch_id, bill_number__startswith=prefix)
        .order_by("-bill_id")
        .first()
    )
    new_number = 1
    if last and last.bill_number:
        try:
            new_number = int(last.bill_number.split("-")[-1]) + 1
        except (ValueError, IndexError):
            pass
    return f"{prefix}{str(new_number).zfill(4)}"


class LabBill(models.Model):
    """Billing record for a completed lab request. One bill per request."""

    bill_id = models.AutoField(primary_key=True)
    # ✅ FIX: no longer globally unique=True — see _generate_lab_bill_number's
    # docstring. Uniqueness is now enforced per-branch via Meta.unique_together
    # below, matching PharmacyBill/ConsultationBill.
    bill_number = models.CharField(max_length=50, blank=True)

    # ✅ FIX: no branch field — same gap as TestGroup/LabTest/LabRequest had.
    # Auto-derived from lab_request.branch on save (a LabRequest always has
    # its branch resolved by the time a bill is created off it, whether via
    # a registered patient or a walk-in request).
    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="lab_bills",
        null=True, blank=True,
        help_text="Auto-set from lab_request.branch on save if not provided.",
    )

    lab_request = models.OneToOneField(
        LabRequest,
        on_delete=models.CASCADE,
        related_name='bill',
    )

    patient = models.ForeignKey(
        'reception.Patient',
        on_delete=models.CASCADE,
        related_name='lab_bills',
        null=True, blank=True,
        help_text='NULL for walk-in lab bills — see lab_request.walkin_* fields instead.',
    )

    subtotal     = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount     = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    paid_amount  = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    payment_status = models.CharField(
        max_length=10,
        choices=BillPaymentStatus.choices,
        default=BillPaymentStatus.PENDING,
        db_index=True,
    )
    payment_method = models.CharField(
        max_length=10,
        choices=BillPaymentMethod.choices,
        default=BillPaymentMethod.NONE,
    )

    billed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='lab_bills_created',
    )

    notes      = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-bill_id']
        unique_together = [('branch', 'bill_number')]

    def clean(self):
        super().clean()
        # discount is a flat amount (not a %), same as ConsultationBill and
        # PharmacyBill — must be non-negative and cannot exceed the
        # pre-discount subtotal. Re-validated here server-side regardless
        # of what the frontend sends.
        if self.discount is not None and self.discount < 0:
            raise ValidationError({'discount': 'Discount cannot be negative.'})
        if self.discount and self.subtotal is not None and self.discount > self.subtotal:
            raise ValidationError({'discount': 'Discount cannot exceed the bill subtotal.'})

    def save(self, *args, **kwargs):
        if not self.branch_id and self.lab_request_id:
            self.branch = self.lab_request.branch
        # Only run the discount/subtotal validation added above — this
        # model never called full_clean() on save before, and introducing
        # it now would newly enforce unrelated field validators (e.g.
        # uniqueness checks on bill_number before it's generated) that the
        # rest of this method isn't written to expect.
        self.clean()
        self.total_amount = max(self.subtotal - self.discount, 0)
        # Lab bills only ever go PENDING -> PAID (no PARTIAL/WAIVED — see
        # BillPaymentStatus). Full payment is always required before a bill
        # is considered settled, matching LabBillPayView's validation.
        if self.paid_amount >= self.total_amount and self.total_amount > 0:
            self.payment_status = BillPaymentStatus.PAID
        if not self.bill_number:
            with transaction.atomic():
                self.bill_number = _generate_lab_bill_number(timezone.localdate(), self.branch)
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)

    def __str__(self):
        return f"LabBill {self.bill_number or self.bill_id} — Request #{self.lab_request_id} [{self.payment_status}]"


# ======================================
# LAB EQUIPMENT
# ======================================
class EquipmentStatus(models.TextChoices):
    ACTIVE            = 'ACTIVE',            'Active'
    UNDER_MAINTENANCE = 'UNDER_MAINTENANCE', 'Under Maintenance'
    RETIRED           = 'RETIRED',           'Retired'


class LabEquipment(models.Model):
    """Lab equipment inventory."""

    equipment_id    = models.AutoField(primary_key=True)

    # ✅ FIX: no branch field — a physical piece of equipment sits at one
    # branch's lab. serial_number stays globally unique=True as-is (a
    # physical asset's serial number is genuinely global; only its
    # location is branch-scoped), unlike the catalog models (TestGroup,
    # LabTest) where name/code needed branch-scoped uniqueness instead.
    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="lab_equipment",
    )

    name            = models.CharField(max_length=200)
    model_number    = models.CharField(max_length=100, blank=True, null=True)
    serial_number   = models.CharField(max_length=100, blank=True, null=True, unique=True)
    manufacturer    = models.CharField(max_length=200, blank=True, null=True)
    purchase_date   = models.DateField(null=True, blank=True)
    warranty_expiry = models.DateField(null=True, blank=True)
    status          = models.CharField(
        max_length=20,
        choices=EquipmentStatus.choices,
        default=EquipmentStatus.ACTIVE,
        db_index=True,
    )
    location   = models.CharField(max_length=200, blank=True, null=True)
    notes      = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.status})"


# ======================================
# LAB MAINTENANCE LOG
# ======================================
class MaintenanceType(models.TextChoices):
    ROUTINE      = 'ROUTINE',      'Routine'
    REPAIR       = 'REPAIR',       'Repair'
    CALIBRATION  = 'CALIBRATION',  'Calibration'
    INSTALLATION = 'INSTALLATION', 'Installation'


class MaintenanceStatus(models.TextChoices):
    SCHEDULED   = 'SCHEDULED',   'Scheduled'
    IN_PROGRESS = 'IN_PROGRESS', 'In Progress'
    COMPLETED   = 'COMPLETED',   'Completed'
    CANCELLED   = 'CANCELLED',   'Cancelled'


class LabMaintenance(models.Model):
    """Maintenance log entry for a piece of lab equipment."""

    maintenance_id   = models.AutoField(primary_key=True)
    equipment        = models.ForeignKey(
        LabEquipment,
        on_delete=models.CASCADE,
        related_name='maintenance_logs',
    )
    maintenance_type = models.CharField(max_length=20, choices=MaintenanceType.choices)
    description      = models.TextField()
    scheduled_date   = models.DateField()
    completed_date   = models.DateField(null=True, blank=True)
    status           = models.CharField(
        max_length=15,
        choices=MaintenanceStatus.choices,
        default=MaintenanceStatus.SCHEDULED,
        db_index=True,
    )
    performed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='maintenance_performed',
    )
    cost       = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    notes      = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-scheduled_date']

    def __str__(self):
        return f"{self.maintenance_type} — {self.equipment.name} ({self.status})"


# ======================================
# LAB SUPPLY ORDER
# ======================================
class OrderCategory(models.TextChoices):
    REAGENT    = 'REAGENT',    'Reagent'
    CONSUMABLE = 'CONSUMABLE', 'Consumable'
    EQUIPMENT  = 'EQUIPMENT',  'Equipment'
    OTHER      = 'OTHER',      'Other'


class OrderStatus(models.TextChoices):
    PENDING             = 'PENDING',             'Pending'
    ORDERED             = 'ORDERED',             'Ordered'
    PARTIALLY_DELIVERED = 'PARTIALLY_DELIVERED', 'Partially Delivered'
    DELIVERED           = 'DELIVERED',           'Delivered'
    CANCELLED           = 'CANCELLED',           'Cancelled'


class LabOrder(models.Model):
    """Supply/procurement order for lab consumables and reagents."""

    order_id  = models.AutoField(primary_key=True)

    # ✅ FIX: no branch field — LabOrder had no way to scope a supply order
    # to a branch at all, so it was structurally shared/leaked across every
    # branch. Each branch's lab places its own consumable/reagent/equipment
    # orders, so this follows the same required, non-null pattern as
    # LabEquipment.branch rather than LabRequest/LabBill's auto-derive
    # (there's no related record to derive it from here).
    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="lab_orders",
        default=1,  # TEMP: backfills existing rows to Branch pk=1. Change this
                    # number if branch_id 1 isn't a real branch in your DB, and
                    # remove this default= entirely once migrated (see notes).
    )

    item_name = models.CharField(max_length=200)
    category  = models.CharField(
        max_length=15,
        choices=OrderCategory.choices,
        default=OrderCategory.CONSUMABLE,
        db_index=True,
    )
    quantity  = models.PositiveIntegerField(default=1)
    unit      = models.CharField(
        max_length=50, blank=True, null=True,
        help_text="e.g. boxes, pieces, liters"
    )
    supplier   = models.CharField(max_length=200, blank=True, null=True)
    unit_cost  = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    total_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    status     = models.CharField(
        max_length=25,
        choices=OrderStatus.choices,
        default=OrderStatus.PENDING,
        db_index=True,
    )
    ordered_by        = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='lab_orders_placed',
    )
    order_date        = models.DateField(default=timezone.localdate)  # FIX: localdate returns a date, not a datetime
    expected_delivery = models.DateField(null=True, blank=True)
    delivered_date    = models.DateField(null=True, blank=True)
    notes      = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-order_id']

    def save(self, *args, **kwargs):
        if self.unit_cost and self.quantity:
            self.total_cost = self.unit_cost * self.quantity
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Order #{self.order_id} — {self.item_name} [{self.status}]"