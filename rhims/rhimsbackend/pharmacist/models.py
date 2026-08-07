# pharmacist/models.py - COMPLETE FIXED VERSION

from django.db import models, transaction
from django.db.models import Sum, F, DecimalField
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.contrib.auth.models import User
from django.core.validators import RegexValidator
from datetime import timedelta


# ─────────────────────────────────────────────────────────
# Administration Route
# ─────────────────────────────────────────────────────────
class RouteChoices(models.TextChoices):
    ORAL    = 'ORAL',    'Oral'
    IV      = 'IV',      'Intravenous'
    IM      = 'IM',      'Intramuscular'
    SC      = 'SC',      'Subcutaneous'
    TOPICAL = 'TOPICAL', 'Topical'
    NASAL   = 'NASAL',   'Nasal'
    RECTAL  = 'RECTAL',  'Rectal'
    OTHER   = 'OTHER',   'Other'


# ─────────────────────────────────────────────────────────
# Medicine Type
# ─────────────────────────────────────────────────────────
class MedicineTypeChoices(models.TextChoices):
    TABLET      = 'TABLET',      'Tablet'
    CAPSULE     = 'CAPSULE',     'Capsule'
    SYRUP       = 'SYRUP',       'Syrup'
    SUSPENSION  = 'SUSPENSION',  'Suspension'
    CREAM       = 'CREAM',       'Cream'
    OINTMENT    = 'OINTMENT',    'Ointment'
    GEL         = 'GEL',         'Gel'
    LOTION      = 'LOTION',      'Lotion'
    DROPS       = 'DROPS',       'Drops'
    NASAL_SPRAY = 'NASAL_SPRAY', 'Nasal Spray'
    INHALER     = 'INHALER',     'Inhaler'
    SUPPOSITORY = 'SUPPOSITORY', 'Suppository'
    POWDER      = 'POWDER',      'Powder'
    INJECTION   = 'INJECTION',   'Injection'
    OTHER       = 'OTHER',       'Other'


# ─────────────────────────────────────────────────────────
# Medicine
# ─────────────────────────────────────────────────────────
class Medicine(models.Model):
    medicine_id  = models.AutoField(primary_key=True)

    # ✅ FIX: no branch field at all — Medicine had a global unique=True on
    # name, meaning two branches couldn't each stock a medicine of the same
    # name independently. Matches the Dealer/SupplyItem/GeneralItem
    # treatment: branch FK + branch-scoped unique_together on name.
    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="medicines",
        help_text="Each branch pharmacist manages their own medicine catalogue independently.",
    )

    name         = models.CharField(max_length=255)
    generic_name = models.CharField(max_length=300, blank=True, null=True)
    category     = models.CharField(max_length=100, blank=True, null=True)
    unit         = models.CharField(max_length=50,  blank=True, null=True)

    medicine_type = models.CharField(
        max_length=20,
        choices=MedicineTypeChoices.choices,
        default=MedicineTypeChoices.TABLET,
        help_text='Dosage form, e.g. Tablet, Capsule, Syrup, Cream, Nasal Spray.',
    )

    strength = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Standard strength, e.g. '500mg', '250mg/5ml'.",
    )

    default_route = models.CharField(
        max_length=10,
        choices=RouteChoices.choices,
        default=RouteChoices.ORAL,
        help_text='Default administration route. Auto-copied onto prescription items.',
    )

    description = models.TextField(blank=True, null=True)
    is_active   = models.BooleanField(default=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not self.default_route or not str(self.default_route).strip():
            raise ValidationError({'default_route': 'Default route is required and cannot be empty.'})

    def save(self, *args, **kwargs):
        if not self.default_route:
            self.default_route = RouteChoices.ORAL
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']
        unique_together = [('branch', 'name')]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(default_route=''),
                name='medicine_default_route_not_empty',
            ),
        ]


# ─────────────────────────────────────────────────────────
# Medicine Batch - ENHANCED VERSION WITH STOCK MANAGEMENT
# ─────────────────────────────────────────────────────────
class MedicineBatch(models.Model):
    # ✅ NEW: PENDING_APPROVAL / REJECTED — a batch linked to a dealer no
    # longer goes straight to ACTIVE (sellable). It starts PENDING_APPROVAL
    # and stays that way — excluded from every existing `status='ACTIVE'`
    # stock/availability/billing query below, so it's neither dispensable
    # nor counted in stock totals — until the manager reviews the
    # auto-created PURCHASE DealerTransaction on the Dealers page:
    #   • CONFIRM → batch flips to ACTIVE (or DEPLETED if already at 0),
    #     i.e. the manager has "allowed" this stock to be sold.
    #   • REJECT  → batch flips to REJECTED and its remaining
    #     (undispensed) quantity is zeroed out — effectively returned to
    #     the dealer. See manager.views._sync_batch_approval_status.
    # Batches with no dealer link are unaffected and still default straight
    # to ACTIVE, exactly as before this existed.
    BATCH_STATUS_CHOICES = [
        ('ACTIVE',            'Active'),
        ('EXPIRED',           'Expired'),
        ('DEPLETED',          'Depleted'),
        ('PENDING_APPROVAL',  'Pending manager approval'),
        ('REJECTED',          'Rejected — returned to dealer'),
    ]

    batch_id            = models.AutoField(primary_key=True)
    medicine            = models.ForeignKey(Medicine, on_delete=models.CASCADE, related_name='batches')
    batch_number        = models.CharField(max_length=100)

    # OPTIONAL — which dealer this stock was purchased from. Entirely
    # optional: batches with no dealer behave exactly as before this field
    # existed. When set, a manager.DealerTransaction (PURCHASE, PENDING) is
    # auto-created for the manager to review/finalize on the Dealers page.
    dealer = models.ForeignKey(
        'manager.Dealer', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='medicine_batches',
        help_text='Dealer this stock was purchased from (optional).',
    )
    PURCHASE_SETTLEMENT_CHOICES = [
        ('CREDIT', 'Credit — to be paid later'),
        ('PAID',   'Paid in full on receipt'),
    ]
    settlement_method = models.CharField(
        max_length=10, choices=PURCHASE_SETTLEMENT_CHOICES, blank=True, null=True,
        help_text='How this purchase was/will be settled with the dealer (optional).',
    )

    # STOCK MANAGEMENT
    quantity            = models.PositiveIntegerField(
        default=0,
        help_text='Current physical quantity in stock'
    )
    allocated_quantity  = models.PositiveIntegerField(
        default=0,
        help_text='Quantity reserved/allocated to pending bills (not yet dispensed)'
    )
    
    # PRICING INFORMATION
    cost_price          = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text='Cost price per unit (wholesale price)'
    )
    mrp                 = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text='Maximum Retail Price (selling price)'
    )
    
    # STOCK ALERTS
    gst_percentage      = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    low_stock_threshold = models.PositiveIntegerField(default=10)
    
    # EXPIRY
    expiry_date         = models.DateField(null=True, blank=True)
    status              = models.CharField(max_length=17, choices=BATCH_STATUS_CHOICES, default='ACTIVE')

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def get_available_quantity(self):
        """
        Returns the quantity available for new bill allocations.
        Available = Physical quantity - Already allocated
        """
        available = self.quantity - self.allocated_quantity
        return max(0, available)

    def clean(self):
        """Validate pricing and stock levels."""
        errors = {}
        
        # Validate pricing
        if self.cost_price and self.mrp and self.cost_price > self.mrp:
            errors['cost_price'] = (
                f'Cost price (₹{self.cost_price}) cannot be greater than MRP (₹{self.mrp})'
            )
        
        # Validate stock allocation
        if self.allocated_quantity > self.quantity:
            errors['allocated_quantity'] = (
                f'Allocated quantity ({self.allocated_quantity}) cannot exceed available quantity ({self.quantity})'
            )
        
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # ✅ FIX: normalize before the blank-check and uniqueness validation
        # run, so 'B1' and ' B1 ' aren't treated as different batch numbers
        # and silently bypass unique_batch_number_per_medicine.
        if self.batch_number:
            self.batch_number = self.batch_number.strip()
        if not self.batch_number:
            raise ValidationError("Batch number is required and must be entered manually.")
        if self.quantity == 0 and self.status == 'ACTIVE':
            self.status = 'DEPLETED'
        self.full_clean()
        super().save(*args, **kwargs)
        _check_stock_alerts(self)

    @property
    def margin_per_unit(self):
        """Calculate profit margin per unit in rupees."""
        if self.cost_price:
            return self.mrp - self.cost_price
        return None

    @property
    def margin_percentage(self):
        """Calculate profit margin percentage."""
        if self.cost_price and self.cost_price > 0:
            return ((self.mrp - self.cost_price) / self.cost_price) * 100
        return None

    @property
    def is_low_stock(self):
        """Check if stock is below low_stock_threshold."""
        return self.get_available_quantity() <= self.low_stock_threshold

    @property
    def is_expiring_soon(self):
        """Check if batch expires within 30 days."""
        if not self.expiry_date:
            return False
        from datetime import timedelta
        return self.expiry_date <= timezone.localdate() + timedelta(days=30)

    def __str__(self):
        available = self.get_available_quantity()
        return f"{self.medicine.name} — Batch {self.batch_number} (Available: {available}/{self.quantity})"

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['medicine']),
            models.Index(fields=['status']),
            models.Index(fields=['expiry_date']),
            models.Index(fields=['batch_number']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['medicine', 'batch_number'],
                name='unique_batch_number_per_medicine',
            ),
        ]


# ─────────────────────────────────────────────────────────
# Medicine Stock Log
# ─────────────────────────────────────────────────────────
class MedicineStockLog(models.Model):
    CHANGE_TYPE_CHOICES = [
        ('IN',     'Stock In'),
        ('OUT',    'Dispensed'),
        ('RETURN', 'Return'),
        ('ADJUST', 'Adjustment'),
    ]

    log_id          = models.AutoField(primary_key=True)
    batch           = models.ForeignKey(MedicineBatch, on_delete=models.CASCADE, related_name='stock_logs')
    change_type     = models.CharField(max_length=10, choices=CHANGE_TYPE_CHOICES)
    quantity_changed = models.IntegerField()
    remarks         = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    def __str__(self):
        return f"{self.batch} | {self.change_type} {self.quantity_changed}"

    class Meta:
        ordering = ['-created_at']


# ─────────────────────────────────────────────────────────
# Stock Alert
# ─────────────────────────────────────────────────────────
class StockAlert(models.Model):
    ALERT_TYPE_CHOICES = [
        ('LOW_STOCK', 'Low Stock'),
        ('EXPIRY',    'Near Expiry / Expired'),
    ]

    alert_id    = models.AutoField(primary_key=True)
    batch       = models.ForeignKey(MedicineBatch, on_delete=models.CASCADE, related_name='alerts')
    alert_type  = models.CharField(max_length=15, choices=ALERT_TYPE_CHOICES)
    is_resolved = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    def __str__(self):
        return f"{self.alert_type} — {self.batch}"

    class Meta:
        ordering = ['-created_at']
        # ✅ FIX: uniqueness is on (batch, alert_type) ONLY — one alert row is
        # reused across its whole open→resolved→reopened lifecycle instead of
        # a new row per state. The old ('batch', 'alert_type', 'is_resolved')
        # tuple let a resolved row and an open row coexist for one cycle, but
        # the moment that item went low a 2nd time and was resolved again,
        # the UPDATE tried to set is_resolved=True on a row that collided
        # with the *first* resolved row already sitting at
        # (batch, alert_type, True) — an uncaught IntegrityError (surfaced to
        # the client as a bare 500, or a 409 via the global exception
        # handler) on what should have been an ordinary restock.
        unique_together = [('batch', 'alert_type')]


# ─────────────────────────────────────────────────────────
# Helper: check and create stock alerts
# ─────────────────────────────────────────────────────────
def _check_stock_alerts(batch):
    """Create/reopen or resolve stock/expiry alerts for a given batch.

    Exactly one StockAlert row exists per (batch, alert_type) — its
    is_resolved flag is flipped in place across cycles rather than a new
    row being created per state. This matches the Meta.unique_together
    above and means the resolve path can never hit an IntegrityError, so
    the try/except that used to guard only the create path is no longer
    needed.
    """
    today = timezone.localdate()

    # Low stock alert
    if batch.quantity <= batch.low_stock_threshold and batch.status == 'ACTIVE':
        StockAlert.objects.update_or_create(
            batch=batch, alert_type='LOW_STOCK',
            defaults={'is_resolved': False},
        )
    else:
        StockAlert.objects.filter(
            batch=batch, alert_type='LOW_STOCK', is_resolved=False
        ).update(is_resolved=True)

    # Expiry alert
    # BUG FIX: previously fired purely off the date, with no check on the
    # batch's status/quantity — so a batch fully returned (quantity=0,
    # status auto-flips to DEPLETED in save() above) with a near/past
    # expiry date kept showing "expiring soon" forever, even though
    # there's no physical stock left to act on. DEPLETED is excluded here
    # the same way the LOW_STOCK check above already excludes it. EXPIRED
    # status batches (unsold stock still on the shelf past its date) are
    # deliberately still included — that's a real, actionable alert.
    if batch.expiry_date and batch.status != 'DEPLETED' and batch.expiry_date <= today + __import__('datetime').timedelta(days=30):
        StockAlert.objects.update_or_create(
            batch=batch, alert_type='EXPIRY',
            defaults={'is_resolved': False},
        )
    else:
        StockAlert.objects.filter(
            batch=batch, alert_type='EXPIRY', is_resolved=False
        ).update(is_resolved=True)


# ─────────────────────────────────────────────────────────
# Pharmacy Bill - ENHANCED VERSION
# ─────────────────────────────────────────────────────────
def _generate_pharmacy_bill_number(bill_date, branch):
    """
    Continuous, branch-prefixed pharmacy bill number: TVM-PHARM-0001,
    KOL-PHARM-0001, ... Never resets — keeps incrementing per branch across
    all days, matching the branch-prefixed convention used for consultation
    bills (reception._generate_cons_bill_number) and lab bills
    (lab._generate_lab_bill_number) elsewhere in the system.

    Uses select_for_update() on the last matching row to avoid two
    concurrent bill creations landing on the same number; callers must
    invoke this inside a transaction.atomic() block (see
    PharmacyBill.save() below).
    """
    prefix = f"{branch.code}-PHARM-"
    last = (
        PharmacyBill.objects
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


class PharmacyBill(models.Model):
    BILL_STATUS_CHOICES = [
        ('DRAFT',     'Draft - Awaiting Review'),
        ('OPEN',      'Open - Items Can Be Added'),
        ('READY',     'Ready - Awaiting Dispensing'),
        ('COMPLETED', 'Completed - Items Dispensed'),
        ('PAID',      'Paid - Invoice Generated'),
        ('CANCELLED', 'Cancelled'),
    ]
    PAYMENT_STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('PAID',    'Paid'),
    ]
    PAYMENT_METHOD_CHOICES = [
        ('CASH',  'Cash'),
        ('CARD',  'Card'),
        ('UPI',   'UPI'),
        ('OTHER', 'Other'),
    ]
    BILL_TYPE_CHOICES = [
        ('MEDICINE',  'Medicine Only'),
        ('GENERAL',   'General Items Only'),
        ('PROCEDURE', 'Procedure Only'),
        ('MIXED',     'Mixed (2 or more of: medicine / general / procedure)'),
    ]

    bill_id     = models.AutoField(primary_key=True)
    bill_number = models.CharField(max_length=50, blank=True)

    # ✅ FIX: no branch field — PharmacyBill (like ConsultationBill/LabBill)
    # needs to know which branch it belongs to, for both stock scoping and
    # the branch-prefixed bill number below. Auto-derived from
    # patient.branch on save when a registered patient is set; required
    # explicitly for walk-in bills (is_walkin=True, patient=None), same
    # pattern as ConsultationPreBooking.
    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="pharmacy_bills",
        null=True, blank=True,
        help_text="Auto-set from patient.branch on save if not provided. Required for walk-in bills.",
    )

    patient = models.ForeignKey(
        'reception.Patient',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='pharmacy_bills',
    )
    patient_name = models.CharField(max_length=300, blank=True, null=True)

    consultation_bill = models.ForeignKey(
        'reception.ConsultationBill',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='pharmacy_bills',
    )

    prescription = models.ForeignKey(
        'doctor.Prescription',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='pharmacy_bills',
        help_text='The prescription this bill was generated from. Null for walk-in bills.',
    )

    bill_date      = models.DateField(default=timezone.localdate)
    bill_status    = models.CharField(max_length=20, choices=BILL_STATUS_CHOICES, default='DRAFT')
    payment_status = models.CharField(max_length=10, choices=PAYMENT_STATUS_CHOICES, default='PENDING')
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHOD_CHOICES, default='CASH')
    upi_reference  = models.CharField(max_length=100, blank=True, null=True)
    bill_type      = models.CharField(max_length=10, choices=BILL_TYPE_CHOICES, default='MEDICINE')

    subtotal          = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    gst_amount        = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    margin_adjustment = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Flat-amount discount (not a percentage) applied to this bill. Must be
    # >= 0 and cannot exceed subtotal. Applied in _recalculate_bill_totals()
    # below, which is the single place total_amount is (re)computed whenever
    # bill items change.
    discount_amount   = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text='Flat discount amount applied to this bill (not a percentage).',
    )

    total_amount      = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    notes = models.TextField(blank=True, null=True)

    # ── Walk-in patient fields ─────────────────────────────────────
    is_walkin = models.BooleanField(
        default=False,
        help_text='True if this is a walk-in patient bill (no MRD registration)',
    )
    walkin_name = models.CharField(max_length=300, blank=True, null=True,
                                   help_text='Walk-in patient name (used only if is_walkin=True)')
    walkin_phone = models.CharField(
        max_length=10, blank=True, null=True,
        validators=[RegexValidator(r'^\d{10}$', 'Phone must be exactly 10 digits.')],
        help_text='Walk-in patient phone (optional, used only if is_walkin=True)',
    )
    GENDER_CHOICES = [('Male', 'Male'), ('Female', 'Female'), ('Other', 'Other')]
    walkin_gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True, null=True,
                                     help_text='Walk-in patient gender (optional)')
    walkin_age = models.PositiveIntegerField(blank=True, null=True,
                                             help_text='Walk-in patient age (optional)')

    # ── NEW FIELDS FOR WORKFLOW ────────────────────────────────────
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='pharmacy_bills_reviewed',
        help_text='Pharmacist who reviewed and allocated stock for this bill'
    )
    
    reviewed_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the bill was reviewed and stock was allocated'
    )

    # ── DISPENSING CONFIRMATION ────────────────────────────────────
    dispensing_confirmed = models.BooleanField(
        default=False,
        help_text='Set to True when pharmacist physically confirms dispensing of items'
    )
    
    dispensing_confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Timestamp when dispensing was physically confirmed'
    )
    
    dispensing_confirmed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pharmacy_bills_dispensing_confirmed',
        help_text='Pharmacist who physically confirmed dispensing'
    )

    # ── SEND TO RECEPTION ──────────────────────────────────────────
    # Once a bill is PAID (paid + dispensed — see bill_status choices
    # above), a pharmacist can forward it to the reception module so
    # reception staff can review and print it from their own bills
    # list (filterable by today / this week / this month there).
    sent_to_reception = models.BooleanField(
        default=False,
        help_text='True once a pharmacist has forwarded this (PAID) bill to the reception module for review/printing.',
    )
    sent_to_reception_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When this bill was sent to reception.',
    )
    sent_to_reception_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='pharmacy_bills_sent_to_reception',
        help_text='Pharmacist who sent this bill to reception.',
    )

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()

        if self.is_walkin:
            if not self.walkin_name or not self.walkin_name.strip():
                raise ValidationError({'walkin_name': 'Walk-in patient name is required when is_walkin=True'})
            if self.patient is not None:
                raise ValidationError({'patient': 'Patient must be NULL for walk-in bills (is_walkin=True).'})
            if not self.branch_id:
                raise ValidationError({'branch': 'Branch is required for walk-in bills (no patient to derive it from).'})
        else:
            if self.patient is None and (not self.patient_name or not self.patient_name.strip()):
                raise ValidationError({'patient': 'Either patient (FK) or patient_name must be provided.'})
            if (self.walkin_name or self.walkin_phone or
                    self.walkin_gender or self.walkin_age is not None):
                raise ValidationError({
                    'walkin_fields': 'Walk-in patient fields must be empty for registered patient bills.'
                })

        # discount_amount is a flat amount (not a %), same as
        # ConsultationBill and LabBill — must be non-negative and cannot
        # exceed the pre-discount subtotal. Re-validated here server-side
        # regardless of what the frontend sends. subtotal is only
        # authoritative once bill items exist (see _recalculate_bill_totals),
        # so this only rejects a discount that's already provably too large
        # against the subtotal value currently on the instance.
        if self.discount_amount is not None and self.discount_amount < 0:
            raise ValidationError({'discount_amount': 'Discount amount cannot be negative.'})
        if self.discount_amount and self.subtotal is not None and self.discount_amount > self.subtotal:
            raise ValidationError({'discount_amount': 'Discount amount cannot exceed the bill subtotal.'})

    def save(self, *args, **kwargs):
        if not self.branch_id and self.patient_id:
            self.branch = self.patient.branch

        if not self.bill_number:
            with transaction.atomic():
                self.bill_number = _generate_pharmacy_bill_number(
                    self.bill_date or timezone.localdate(), self.branch
                )

        # FIX: Skip full_clean() on partial saves (update_fields=...) — these are
        # already-validated records being updated in place (status transitions, etc.).
        # Running full_clean() on a partial save can raise spurious validation errors.
        if not kwargs.get('update_fields'):
            self.full_clean()

        if not self.is_walkin and self.patient and not self.patient_name:
            self.patient_name = f"{self.patient.first_name} {self.patient.last_name}".strip()

        super().save(*args, **kwargs)

    def get_patient_name(self):
        if self.is_walkin:
            return self.walkin_name or 'Unknown'
        elif self.patient:
            return f"{self.patient.first_name} {self.patient.last_name}".strip()
        return self.patient_name or 'Unknown'

    def get_patient_phone(self):
        if self.is_walkin:
            return self.walkin_phone
        elif self.patient:
            return self.patient.phone
        return None

    def get_patient_gender(self):
        if self.is_walkin:
            return self.walkin_gender
        elif self.patient:
            return self.patient.gender
        return None

    def get_patient_age(self):
        if self.is_walkin:
            return self.walkin_age
        elif self.patient:
            return self.patient.age
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
                'mrd': self.patient.mrd_number,
                'patient_id': self.patient.patient_id,
                'name': f"{self.patient.first_name} {self.patient.last_name}",
                'phone': self.patient.phone,
                'gender': self.patient.gender,
                'age': self.patient.age,
                'place': self.patient.place,
            }
        return {'type': 'unknown', 'name': self.patient_name or 'Unknown'}

    @property
    def is_dispensed(self):
        """True when the bill has been fully paid and medicines dispensed."""
        return self.bill_status == 'PAID'

    def can_add_items(self):
        """Returns True when the bill is DRAFT or OPEN."""
        return self.bill_status in ['DRAFT', 'OPEN']

    def review_and_allocate_stock(self, reviewer, batch_assignments_data):
        """
        Pharmacist reviews bill, assigns batches, and allocates stock.
        
        Args:
            reviewer: User object (the pharmacist reviewing)
            batch_assignments_data: List of dicts with structure:
                [
                    {
                        'medicine_item_id': 123,
                        'batch_id': 456,
                        'selected_quantity': 10
                    },
                    ...
                ]
        
        Raises:
            ValidationError: If validation fails
        """
        if self.bill_status != 'DRAFT':
            raise ValidationError(
                f"Only DRAFT bills can be reviewed. Current status: {self.bill_status}"
            )
        
        if not batch_assignments_data:
            raise ValidationError("batch_assignments_data cannot be empty")
        
        with transaction.atomic():
            # Get all medicine items in this bill that need dispensing
            medicine_items = {
                item.item_id: item 
                for item in self.medicine_items.filter(is_dispensed=False)
            }
            
            if not medicine_items:
                raise ValidationError("No items to dispense in this bill")
            
            # Process each assignment
            for assignment_data in batch_assignments_data:
                medicine_item_id = assignment_data.get('medicine_item_id')
                batch_id = assignment_data.get('batch_id')
                selected_qty = assignment_data.get('selected_quantity')
                
                # Validate required fields
                if not all([medicine_item_id, batch_id, selected_qty]):
                    raise ValidationError(
                        "Each assignment must have medicine_item_id, batch_id, and selected_quantity"
                    )
                
                # Validate item exists in this bill
                if medicine_item_id not in medicine_items:
                    raise ValidationError(f"Medicine item {medicine_item_id} not found in this bill")
                
                medicine_item = medicine_items[medicine_item_id]
                
                # Validate batch exists AND belongs to this bill's own
                # branch — a bill can only ever be dispensed from its own
                # branch's stock, never another branch's.
                try:
                    batch = MedicineBatch.objects.select_related('medicine').get(batch_id=batch_id)
                except MedicineBatch.DoesNotExist:
                    raise ValidationError(f"Batch {batch_id} not found")

                if batch.medicine.branch_id != self.branch_id:
                    raise ValidationError(
                        f"Batch {batch.batch_number} belongs to a different branch's stock "
                        f"and cannot be used for this bill."
                    )
                
                # Validate batch is active
                if batch.status != 'ACTIVE':
                    raise ValidationError(
                        f"Batch {batch.batch_number} is {batch.status}, cannot use for dispensing"
                    )
                
                # Check stock availability (physical - allocated)
                available = batch.get_available_quantity()
                if available < selected_qty:
                    raise ValidationError(
                        f"Insufficient stock in batch '{batch.batch_number}' for '{batch.medicine.name}'. "
                        f"Available: {available} (Physical: {batch.quantity}, "
                        f"Allocated: {batch.allocated_quantity}), Requested: {selected_qty}"
                    )
                
                # Check that selected quantity matches item requirement
                if selected_qty != medicine_item.quantity:
                    raise ValidationError(
                        f"Selected quantity ({selected_qty}) must match bill item quantity ({medicine_item.quantity})"
                    )
                
                # Create or update batch assignment
                PharmacyBillBatchAssignment.objects.update_or_create(
                    bill=self,
                    medicine_item=medicine_item,
                    defaults={
                        'batch': batch,
                        'selected_quantity': selected_qty
                    }
                )
                
                # ALLOCATE STOCK: Reserve this quantity for this bill
                MedicineBatch.objects.filter(batch_id=batch_id).update(
                    allocated_quantity=models.F('allocated_quantity') + selected_qty
                )
            
            # If we got here, all validations passed
            # Transition bill to READY status
            self.bill_status = 'READY'
            self.reviewed_by = reviewer
            self.reviewed_at = timezone.now()
            self.save(update_fields=['bill_status', 'reviewed_by', 'reviewed_at'])

    def deallocate_all_stock(self):
        """
        Deallocate stock for all batch assignments.
        Used when cancelling a READY bill.
        """
        with transaction.atomic():
            for assignment in self.batch_assignments.all():
                batch = assignment.batch
                # Reduce allocated quantity
                MedicineBatch.objects.filter(batch_id=batch.batch_id).update(
                    allocated_quantity=models.F('allocated_quantity') - assignment.selected_quantity
                )

    @transaction.atomic
    def finalize_dispense(self):
        """
        Deduct stock for every pending medicine item and mark them dispensed.
        Called ONLY from MarkBillPaidView after bill_status has been set to 'PAID'.
        """
        pending_items = self.medicine_items.filter(is_dispensed=False).select_related('batch__medicine')

        for item in pending_items:
            batch = item.batch

            current_qty = MedicineBatch.objects.filter(pk=batch.pk).values_list('quantity', flat=True).first()
            if current_qty is None or current_qty < item.quantity:
                raise ValidationError(
                    f"Insufficient stock for '{batch.medicine.name}' "
                    f"(batch {batch.batch_number}). "
                    f"Available: {current_qty or 0}, required: {item.quantity}. "
                    "Payment rolled back — please review the bill items."
                )

            MedicineBatch.objects.filter(pk=batch.pk).update(
                quantity=models.F('quantity') - item.quantity
            )
            batch.refresh_from_db()

            if batch.quantity == 0 and batch.status == 'ACTIVE':
                MedicineBatch.objects.filter(pk=batch.pk).update(status='DEPLETED')
                batch.status = 'DEPLETED'

            MedicineStockLog.objects.create(
                batch=batch,
                change_type='OUT',
                quantity_changed=-item.quantity,
                remarks=f"Dispensed — bill {self.bill_number}",
            )

            PharmacyBillMedicineItem.objects.filter(pk=item.pk).update(is_dispensed=True)
            _check_stock_alerts(batch)

        # General/FMCG items — no allocation step exists for these (stock is
        # only checked, not reserved, when added to the bill), so deduction
        # happens the same moment as medicines: when the bill is marked PAID.
        pending_general_items = self.general_items.filter(is_dispensed=False).select_related('batch__general_item')

        for gi in pending_general_items:
            batch = gi.batch

            current_qty = GeneralItemBatch.objects.filter(pk=batch.pk).values_list('quantity', flat=True).first()
            if current_qty is None or current_qty < gi.quantity:
                raise ValidationError(
                    f"Insufficient stock for '{batch.general_item.name}' "
                    f"(batch {batch.batch_number}). "
                    f"Available: {current_qty or 0}, required: {gi.quantity}. "
                    "Payment rolled back — please review the bill items."
                )

            GeneralItemBatch.objects.filter(pk=batch.pk).update(
                quantity=models.F('quantity') - gi.quantity
            )
            batch.refresh_from_db()

            if batch.quantity == 0 and batch.status == 'ACTIVE':
                GeneralItemBatch.objects.filter(pk=batch.pk).update(status='DEPLETED')
                batch.status = 'DEPLETED'

            GeneralItemStockLog.objects.create(
                batch=batch,
                change_type='OUT',
                quantity_changed=-gi.quantity,
                remarks=f"Dispensed — bill {self.bill_number}",
            )

            PharmacyBillGeneralItem.objects.filter(pk=gi.pk).update(is_dispensed=True)
            _check_general_item_alerts(batch)

    def cancel_bill(self):
        """Cancel a DRAFT, OPEN, READY, or COMPLETED bill."""
        if self.is_dispensed:
            raise ValidationError(
                "Cannot cancel a PAID bill — medicines have already been physically dispensed."
            )
        if self.bill_status not in ('DRAFT', 'OPEN', 'READY', 'COMPLETED'):
            raise ValidationError(
                f"Only DRAFT/OPEN/READY/COMPLETED bills can be cancelled. "
                f"Current status: '{self.bill_status}'."
            )
        
        # Deallocate stock if in READY status
        if self.bill_status == 'READY':
            self.deallocate_all_stock()
        
        self.bill_status = 'CANCELLED'
        self.save(update_fields=['bill_status'])

    def __str__(self):
        return f"PharmacyBill {self.bill_number} [{self.bill_status}]"

    class Meta:
        ordering = ['-bill_id']
        unique_together = [('branch', 'bill_number')]


# ─────────────────────────────────────────────────────────
# Pharmacy Bill Batch Assignment - NEW MODEL
# ─────────────────────────────────────────────────────────
class PharmacyBillBatchAssignment(models.Model):
    """
    Tracks which batch a medicine will be dispensed from for a bill item.
    Created when pharmacist reviews a bill and assigns batches.
    This bridges the gap between a prescription and actual stock batches.
    """
    assignment_id = models.AutoField(primary_key=True)
    
    bill = models.ForeignKey(
        PharmacyBill,
        on_delete=models.CASCADE,
        related_name='batch_assignments',
        help_text='The PharmacyBill this assignment belongs to'
    )
    
    medicine_item = models.OneToOneField(
        'PharmacyBillMedicineItem',
        on_delete=models.CASCADE,
        related_name='batch_assignment',
        help_text='The medicine item in the bill'
    )
    
    batch = models.ForeignKey(
        MedicineBatch,
        on_delete=models.PROTECT,
        related_name='bill_assignments',
        help_text='The batch from which this item will be dispensed'
    )
    
    selected_quantity = models.PositiveIntegerField(
        help_text='Quantity from this specific batch to use for this bill item'
    )
    
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('bill', 'medicine_item')]
        ordering = ['assignment_id']
        indexes = [
            models.Index(fields=['bill']),
            models.Index(fields=['batch']),
        ]

    def __str__(self):
        return (
            f"Bill {self.bill.bill_id} - "
            f"{self.batch.medicine.name} "
            f"({self.selected_quantity} from batch {self.batch.batch_number})"
        )


# ─────────────────────────────────────────────────────────
# Pharmacy Bill — Medicine Item
# ─────────────────────────────────────────────────────────
class PharmacyBillMedicineItem(models.Model):
    item_id = models.AutoField(primary_key=True)
    bill    = models.ForeignKey(PharmacyBill,   on_delete=models.CASCADE,  related_name='medicine_items')
    batch   = models.ForeignKey(MedicineBatch,  on_delete=models.PROTECT,  related_name='bill_items')

    prescription_item = models.ForeignKey(
        'doctor.PrescriptionItem',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='pharmacy_bill_items',
        help_text='Original prescription item this was dispensed from',
    )

    quantity       = models.PositiveIntegerField()
    unit_mrp       = models.DecimalField(max_digits=10,  decimal_places=2)
    gst_percentage = models.DecimalField(max_digits=5,   decimal_places=2, default=0)
    item_total     = models.DecimalField(max_digits=12,  decimal_places=2, default=0)
    is_dispensed   = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.unit_mrp       = self.batch.mrp
        self.gst_percentage = self.batch.gst_percentage
        self.item_total     = self.unit_mrp * self.quantity
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.batch.medicine.name} x{self.quantity}"

    class Meta:
        ordering = ['item_id']


# ─────────────────────────────────────────────────────────
# Pharmacy Bill — Procedure Item
# ─────────────────────────────────────────────────────────
class PharmacyBillProcedureItem(models.Model):
    item_id   = models.AutoField(primary_key=True)
    bill      = models.ForeignKey(PharmacyBill,            on_delete=models.CASCADE,  related_name='procedure_items')
    # Nullable: a bill line can either reference a real admin-managed catalog
    # Procedure, or be a one-off manual entry (see manual_description below).
    # Manual entries are typed by the pharmacist at billing time and are NOT
    # persisted as a new catalog Procedure — they're just a line on this
    # specific bill, so they never "leak" into the admin procedure picker
    # and never randomly reappear/stay fixed across other bills.
    procedure = models.ForeignKey(
        'administration.Procedure', on_delete=models.PROTECT,
        related_name='pharmacy_bill_items', null=True, blank=True,
    )
    manual_description = models.CharField(max_length=200, blank=True, null=True)
    quantity   = models.PositiveIntegerField(default=1)
    unit_charge = models.DecimalField(max_digits=10, decimal_places=2)
    item_total  = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    @property
    def display_name(self):
        return self.procedure.name if self.procedure_id else (self.manual_description or "Procedure")

    def save(self, *args, **kwargs):
        # unit_charge normally mirrors the catalog Procedure's charge, but
        # callers (e.g. the manual add-procedure flow) may set unit_charge
        # explicitly beforehand — required for manual entries since there's
        # no catalog Procedure to fall back to.
        if self.unit_charge is None:
            if not self.procedure_id:
                raise ValueError("unit_charge is required for manual procedure entries.")
            self.unit_charge = self.procedure.charge
        self.item_total = self.unit_charge * self.quantity
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.display_name} x{self.quantity}"

    class Meta:
        ordering = ['item_id']


# ─────────────────────────────────────────────────────────
# Helper: recalculate bill totals
# ─────────────────────────────────────────────────────────
def _recalculate_bill_totals(bill):
    """Recompute subtotal, gst_amount, total_amount, and bill_type for a PharmacyBill."""
    from django.db.models import Sum

    med_items  = PharmacyBillMedicineItem.objects.filter(bill=bill)
    proc_items = PharmacyBillProcedureItem.objects.filter(bill=bill)
    gen_items  = PharmacyBillGeneralItem.objects.filter(bill=bill)

    def _gst_sum(qs):
        return qs.aggregate(
            g=Sum(models.ExpressionWrapper(
                models.F('item_total') * models.F('gst_percentage') / 100,
                output_field=models.DecimalField(),
            ))
        )['g'] or 0

    med_subtotal  = med_items.aggregate(s=Sum('item_total'))['s'] or 0
    gen_subtotal  = gen_items.aggregate(s=Sum('item_total'))['s'] or 0
    gst           = _gst_sum(med_items) + _gst_sum(gen_items)
    proc_subtotal = proc_items.aggregate(s=Sum('item_total'))['s'] or 0

    subtotal = med_subtotal + gen_subtotal + proc_subtotal

    # Items on the bill can change after a discount was applied, so the
    # discount is re-clamped against the freshly computed subtotal here
    # rather than trusted as-is — never below 0, never more than subtotal.
    discount = bill.discount_amount or 0
    if discount < 0:
        discount = 0
    if discount > subtotal:
        discount = subtotal

    total = max(subtotal + gst + bill.margin_adjustment - discount, 0)

    has_med  = med_items.exists()
    has_gen  = gen_items.exists()
    has_proc = proc_items.exists()
    category_count = sum([has_med, has_gen, has_proc])

    if category_count >= 2:
        bill_type = 'MIXED'
    elif has_proc:
        bill_type = 'PROCEDURE'
    elif has_gen:
        bill_type = 'GENERAL'
    else:
        bill_type = 'MEDICINE'

    PharmacyBill.objects.filter(pk=bill.pk).update(
        subtotal=subtotal,
        gst_amount=gst,
        discount_amount=discount,
        total_amount=total,
        bill_type=bill_type,
    )

# pharmacist/models.py - FIXED VERSION (Return to Provider section)
# ═════════════════════════════════════════════════════════════════════════════
# This file shows the FIXED MedicineReturnToProvider model with proper fields
# ═════════════════════════════════════════════════════════════════════════════

from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.contrib.auth.models import User


# ─────────────────────────────────────────────────────────
# Medicine Return to Provider (expired / damaged / recall)
# ─────────────────────────────────────────────────────────
class MedicineReturnToProvider(models.Model):
    """
    ✅ FIXED MODEL - For returning medicines to provider
    
    Fields for selection:
    - medicine (via batch.medicine) - READ-ONLY (computed from batch)
    - batch - ForeignKey to MedicineBatch (USER SELECTS THIS)
    - quantity - PositiveIntegerField (USER ENTERS THIS)
    - reason - Choices field (USER SELECTS THIS)
    - reason_details - Additional notes (USER CAN ENTER)
    
    Workflow status:
    - REQUESTED: Initial state when pharmacist requests return
    - APPROVED: Manager approves the return request
    - REJECTED: Manager rejects the return request
    - COMPLETED: Return has been physically completed
    """
    
    RETURN_STATUS_CHOICES = [
        ('REQUESTED', 'Return Requested'),
        ('APPROVED',  'Approved'),
        ('REJECTED',  'Rejected'),
        ('COMPLETED', 'Completed'),
    ]
    
    REASON_CHOICES = [
        ('EXPIRED',   'Expired'),
        ('DAMAGED',   'Damaged'),
        ('DEFECTIVE', 'Defective'),
        ('RECALL',    'Recall'),
        ('OTHER',     'Other'),
    ]

    # Primary Key
    return_id = models.AutoField(primary_key=True)
    
    # ✅ MEDICINE SELECTION - User selects batch, medicine is derived
    batch = models.ForeignKey(
        'MedicineBatch',
        on_delete=models.PROTECT,
        related_name='provider_returns',
        help_text="Medicine batch to return to provider"
    )
    
    # ✅ QUANTITY SELECTION - User enters quantity to return
    quantity = models.PositiveIntegerField(
        help_text="Quantity to return to provider"
    )
    
    # ✅ REASON SELECTION - User selects reason for return
    reason = models.CharField(
        max_length=50,
        choices=REASON_CHOICES,
        default='EXPIRED',
        help_text="Reason for returning to provider"
    )
    
    # Additional reason details
    reason_details = models.TextField(
        blank=True,
        null=True,
        help_text="Additional details about the return reason"
    )

    # OPTIONAL — dealer this return is going back to, and how the
    # pharmacist expects it to be settled. Both optional: leaving dealer
    # blank keeps this exactly like a plain return with no ledger impact.
    # When dealer is set, a manager.DealerTransaction (CREDIT_NOTE,
    # PENDING) is auto-created for the manager to review/finalize.
    dealer = models.ForeignKey(
        'manager.Dealer', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='medicine_returns',
        help_text='Dealer this return is going back to (optional).',
    )
    SETTLEMENT_CHOICES = [
        ('CREDIT',   'Credit — carried forward against future orders'),
        ('REFUND',   'Cash refund'),
        ('EXCHANGE', 'Exchanged for other product(s)'),
    ]
    settlement_method = models.CharField(
        max_length=10, choices=SETTLEMENT_CHOICES, blank=True, null=True,
        help_text='How the pharmacist expects this return to be settled by the dealer (optional).',
    )

    # Refund owed by the supplier — quantity × the batch's purchase (cost)
    # price, NOT the MRP/selling price. Snapshotted at return time so later
    # edits to the medicine's price never change historical figures.
    # NOTE: matches migration 0014_supplyreturn_refund_amount_and_more,
    # which already added this column to the database — this field was
    # missing from the model source, which made every return-to-provider
    # request crash with "refund_amount is an invalid keyword argument".
    refund_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Refund due from the supplier — quantity × purchase (cost) price, not MRP."
    )

    # Status tracking
    status = models.CharField(
        max_length=15,
        choices=RETURN_STATUS_CHOICES,
        default='REQUESTED',
        help_text="Current status of the return request"
    )
    
    # User tracking
    requested_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='medicine_provider_returns_requested',
        help_text="Pharmacist who requested the return"
    )
    
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='medicine_provider_returns_approved',
        help_text="Manager who approved/rejected the return"
    )
    
    # Timestamps
    created_at = models.DateTimeField(
        default=timezone.now,
        editable=False,
        help_text="When the return request was created"
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="When the return request was last updated"
    )
    
    def clean(self):
        """
        Validate the return request.

        Note: stock availability is checked and deducted BEFORE this
        record is created (see MedicineReturnToProviderCreateSerializer
        .save()). By the time this runs, the batch's quantity already
        reflects the deduction for *this* return, so we must not
        re-check quantity against the batch here - doing so would
        compare this return's own quantity against already-reduced
        stock and raise a false "insufficient quantity" error.
        """
        errors = {}
        
        if self.quantity <= 0:
            errors['quantity'] = 'Quantity must be greater than zero.'
        
        if self.batch_id and not MedicineBatch.objects.filter(pk=self.batch_id).exists():
            errors['batch'] = 'Batch not found.'
        
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """
        Save the return request.

        Stock deduction happens once, at creation time, inside
        MedicineReturnToProviderCreateSerializer.save() (via an atomic
        F() .update() on MedicineBatch). It is intentionally NOT
        repeated here on a status change to 'COMPLETED' - the frontend
        has no separate approve/complete step, and doing it in both
        places would double-deduct stock.
        """
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"Provider Return {self.return_id} — "
            f"{self.batch.medicine.name} x{self.quantity} [{self.status}]"
        )

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = "Medicine Returns to Provider"
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['batch', 'status']),
        ]


# ═════════════════════════════════════════════════════════════════════════
# SUPPLIES & CONSUMABLES
# ═════════════════════════════════════════════════════════════════════════
# Hospital consumable supplies that are NOT dispensable medicines — items
# purchased in bulk, tracked by stock level, consumed internally by wards/
# departments, and restocked from suppliers (syringes, gloves, IV sets,
# dressings, PPE, etc). Lives inside the pharmacist app (same module the
# pharmacist team already works in) rather than as a separate Django app,
# and is intentionally NOT wired into Medicine / MedicineBatch / PharmacyBill
# — it is a completely separate stock system.
# ═════════════════════════════════════════════════════════════════════════

class SupplyCategoryChoices(models.TextChoices):
    INJECTION    = 'INJECTION',    'Injection & Infusion'
    WOUND_CARE   = 'WOUND_CARE',   'Wound Care & Dressings'
    RESPIRATORY  = 'RESPIRATORY',  'Respiratory & Oxygen'
    PPE          = 'PPE',          'Personal Protective Equipment'
    URINARY      = 'URINARY',      'Urinary & Drainage'
    COLLECTION   = 'COLLECTION',   'Collection & Diagnostic'
    SURGICAL     = 'SURGICAL',     'Surgical & Procedure'
    HOUSEKEEPING = 'HOUSEKEEPING', 'Housekeeping & Safety'
    OTHER        = 'OTHER',        'Other'


class SupplyUnitChoices(models.TextChoices):
    PIECES  = 'PIECES',  'Pieces'
    BOXES   = 'BOXES',   'Boxes'
    PACKETS = 'PACKETS', 'Packets'
    ROLLS   = 'ROLLS',   'Rolls'
    PAIRS   = 'PAIRS',   'Pairs'
    LITRES  = 'LITRES',  'Litres'
    ML      = 'ML',      'Millilitres'
    CYLINDERS = 'CYLINDERS', 'Cylinders'
    VIALS   = 'VIALS',   'Vials'
    SETS    = 'SETS',    'Sets'
    STRIPS  = 'STRIPS',  'Strips'
    METRES  = 'METRES',  'Metres'
    KG      = 'KG',      'Kilograms'
    GRAMS   = 'GRAMS',   'Grams'


class SupplyItem(models.Model):
    """Catalogue entry — one row per product type (e.g. 'Syringe 5ml')."""
    item_id  = models.AutoField(primary_key=True)

    # ✅ FIX: no branch field — same gap as Medicine. branch FK +
    # branch-scoped unique_together on name.
    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="supply_items",
    )

    name     = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=SupplyCategoryChoices.choices, default=SupplyCategoryChoices.OTHER)
    unit     = models.CharField(max_length=10, choices=SupplyUnitChoices.choices, default=SupplyUnitChoices.PIECES)

    description = models.TextField(blank=True, null=True, help_text='Spec, size, brand notes')

    low_stock_threshold = models.PositiveIntegerField(default=10)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def total_stock(self):
        # ✅ FIX: excludes PENDING_APPROVAL/REJECTED batches — a dealer-linked
        # batch awaiting manager sign-off (or one the manager rejected) must
        # not count as available stock. See SupplyBatch.BATCH_STATUS_CHOICES.
        agg = self.batches.filter(status='ACTIVE').aggregate(total=Sum('quantity'))
        return agg['total'] or 0

    @property
    def total_stock_value(self):
        """Current value of remaining stock across all ACTIVE batches (qty * unit_cost)."""
        agg = self.batches.filter(status='ACTIVE').aggregate(
            value=Sum(F('quantity') * F('unit_cost'), output_field=DecimalField())
        )
        return agg['value'] or 0

    @property
    def is_low_stock(self):
        return self.total_stock < self.low_stock_threshold

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['category', 'name']
        unique_together = [('branch', 'name')]
        indexes = [
            models.Index(fields=['category']),
            models.Index(fields=['is_active']),
        ]


class SupplyBatch(models.Model):
    """A single purchase/receipt of a SupplyItem."""
    # ✅ NEW: same approval-gate concept as MedicineBatch/GeneralItemBatch
    # (see MedicineBatch.BATCH_STATUS_CHOICES for the full explanation).
    # SupplyBatch had no status field at all before — availability was
    # purely `quantity`-based — so ACTIVE is the new default and behaves
    # exactly like "no status field existed" for every batch with no
    # dealer link. Dealer-linked batches start PENDING_APPROVAL instead.
    BATCH_STATUS_CHOICES = [
        ('ACTIVE',           'Active'),
        ('DEPLETED',         'Depleted'),
        ('PENDING_APPROVAL', 'Pending manager approval'),
        ('REJECTED',         'Rejected — returned to dealer'),
    ]

    batch_id     = models.AutoField(primary_key=True)
    supply_item  = models.ForeignKey(SupplyItem, on_delete=models.CASCADE, related_name='batches')
    batch_number = models.CharField(max_length=100, blank=True, null=True, help_text='Supplier/invoice batch or lot number (required)')

    quantity          = models.PositiveIntegerField(help_text='Current remaining stock')
    original_quantity = models.PositiveIntegerField(help_text='Quantity at time of receipt, never changes')
    status = models.CharField(max_length=17, choices=BATCH_STATUS_CHOICES, default='ACTIVE')

    unit_cost  = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    supplier_name    = models.CharField(max_length=200, blank=True, null=True)
    supplier_contact = models.CharField(max_length=100, blank=True, null=True)
    invoice_number   = models.CharField(max_length=100, blank=True, null=True)

    # OPTIONAL — link to a manager.Dealer master record, distinct from the
    # free-text supplier_name/supplier_contact above (kept for backward
    # compatibility with existing batches). When set, a
    # manager.DealerTransaction (PURCHASE, PENDING) is auto-created.
    dealer = models.ForeignKey(
        'manager.Dealer', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='supply_batches',
        help_text='Dealer this stock was purchased from (optional).',
    )
    PURCHASE_SETTLEMENT_CHOICES = [
        ('CREDIT', 'Credit — to be paid later'),
        ('PAID',   'Paid in full on receipt'),
    ]
    settlement_method = models.CharField(
        max_length=10, choices=PURCHASE_SETTLEMENT_CHOICES, blank=True, null=True,
        help_text='How this purchase was/will be settled with the dealer (optional).',
    )

    purchase_date = models.DateField(default=timezone.localdate)
    expiry_date   = models.DateField(null=True, blank=True)

    notes = models.TextField(blank=True, null=True)

    received_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='supply_batches_received')

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        # Only enforce "batch number required" when this batch is being
        # created, or when the caller is explicitly writing batch_number
        # itself. A plain quantity/notes update on an existing batch — e.g.
        # deducting stock on use, or a supplier return — must never be
        # blocked just because the batch pre-dates this field being made
        # mandatory (see the "legacy #<id>" batches shown in the UI).
        needs_check = self.pk is None or update_fields is None or 'batch_number' in update_fields
        if needs_check:
            if self.batch_number:
                self.batch_number = str(self.batch_number).strip()
            if not self.batch_number:
                raise ValidationError("Batch number is required and must be entered manually.")

        # ✅ FIX: original_quantity/unit_cost can arrive here as plain strings
        # (e.g. any call site that builds/updates this model without going
        # through a DRF DecimalField, or a Python attribute that was never
        # cast). `int * str` does NOT raise — it silently repeats the string
        # (e.g. 100 * "2.50" -> "2.502.502.50..."), which then fails deep in
        # the DB layer with a cryptic "not a valid decimal" error, or worse,
        # gets truncated/corrupted on write. Coerce explicitly and fail loudly
        # with a clean validation error instead.
        from decimal import Decimal, InvalidOperation
        try:
            qty = Decimal(str(self.original_quantity)) if self.original_quantity not in (None, '') else Decimal('0')
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError("original_quantity must be a valid whole number.")
        try:
            cost = Decimal(str(self.unit_cost)) if self.unit_cost not in (None, '') else Decimal('0')
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError("unit_cost must be a valid decimal number.")

        self.total_cost = qty * cost
        super().save(*args, **kwargs)

    @property
    def remaining_percentage(self):
        if not self.original_quantity:
            return 0
        return round((self.quantity / self.original_quantity) * 100, 1)

    @property
    def is_expiring_soon(self):
        if not self.expiry_date:
            return False
        return self.expiry_date <= timezone.localdate() + timedelta(days=30)

    def __str__(self):
        return f"{self.supply_item.name} — {self.purchase_date}"

    class Meta:
        ordering = ['-purchase_date', '-batch_id']
        indexes = [
            models.Index(fields=['supply_item']),
            models.Index(fields=['expiry_date']),
            models.Index(fields=['batch_number'], name='pharmacist__batch_n_a1f3c2_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['supply_item', 'batch_number'],
                name='unique_batch_number_per_supply_item',
            ),
        ]


class SupplyUsageLog(models.Model):
    """Every deduction from supply stock (issued to a ward/department)."""
    log_id       = models.AutoField(primary_key=True)
    supply_item  = models.ForeignKey(SupplyItem, on_delete=models.CASCADE, related_name='usage_logs')
    batch        = models.ForeignKey(SupplyBatch, on_delete=models.CASCADE, null=True, blank=True, related_name='usage_logs')

    quantity_used = models.PositiveIntegerField()
    department    = models.CharField(max_length=100, blank=True, null=True)
    used_by       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='supply_usage_logs')
    notes         = models.CharField(max_length=300, blank=True, null=True)
    balance_after = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Item's total remaining stock (across all batches) immediately after this issue"
    )

    used_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.supply_item.name} x{self.quantity_used} — {self.department or 'N/A'}"

    class Meta:
        ordering = ['-used_at']
        indexes = [
            models.Index(fields=['supply_item']),
            models.Index(fields=['department']),
            models.Index(fields=['-used_at']),
        ]


class SupplyReturnReasonChoices(models.TextChoices):
    EXPIRED   = 'EXPIRED', 'Expired'
    DAMAGED   = 'DAMAGED', 'Damaged'
    DEFECTIVE = 'DEFECTIVE', 'Defective'
    RECALL    = 'RECALL', 'Recall'
    EXCESS    = 'EXCESS', 'Excess / Over-ordered'
    OTHER     = 'OTHER', 'Other'


class SupplyReturn(models.Model):
    """A record of stock returned from a SupplyBatch back to the supplier."""
    return_id   = models.AutoField(primary_key=True)
    supply_item = models.ForeignKey(SupplyItem, on_delete=models.CASCADE, related_name='returns')
    batch       = models.ForeignKey(SupplyBatch, on_delete=models.CASCADE, related_name='returns')

    quantity_returned = models.PositiveIntegerField()
    reason = models.CharField(
        max_length=20,
        choices=SupplyReturnReasonChoices.choices,
        default=SupplyReturnReasonChoices.OTHER,
        help_text='Why this stock is being returned',
    )
    reason_details = models.CharField(
        max_length=300, blank=True, null=True,
        help_text='Extra detail about the reason',
    )
    reference_number = models.CharField(
        max_length=100, blank=True, null=True,
        help_text='Supplier credit note / RMA number',
    )

    # OPTIONAL — see MedicineReturnToProvider.dealer/settlement_method above
    # for the full explanation; identical pattern here for supplies.
    dealer = models.ForeignKey(
        'manager.Dealer', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='supply_returns',
        help_text='Dealer this return is going back to (optional).',
    )
    SETTLEMENT_CHOICES = [
        ('CREDIT',   'Credit — carried forward against future orders'),
        ('REFUND',   'Cash refund'),
        ('EXCHANGE', 'Exchanged for other product(s)'),
    ]
    settlement_method = models.CharField(
        max_length=10, choices=SETTLEMENT_CHOICES, blank=True, null=True,
        help_text='How the pharmacist expects this return to be settled by the dealer (optional).',
    )

    # Refund owed by the supplier — quantity returned × the batch's purchase
    # (unit) cost, NOT an MRP/selling price. Snapshotted at return time.
    # NOTE: matches migration 0014_supplyreturn_refund_amount_and_more,
    # which already added this column to the database — this field was
    # missing from the model source, which made every return-to-provider
    # request crash with "refund_amount is an invalid keyword argument".
    refund_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text=(
            "Refund owed by the supplier — quantity returned × the batch's purchase "
            "(unit) cost, NOT the MRP/selling price. Snapshotted at return time."
        ),
    )

    returned_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='supply_returns')
    returned_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.supply_item.name} x{self.quantity_returned} returned — {self.get_reason_display()}"

    class Meta:
        ordering = ['-returned_at']
        indexes = [
            models.Index(fields=['supply_item'], name='pharm_ret_item_idx'),
            models.Index(fields=['batch'], name='pharm_ret_batch_idx'),
        ]


class SupplyStockAlert(models.Model):
    """Auto-generated low-stock flag for a SupplyItem — one row per item,
    reused across its whole open→resolved→reopened lifecycle (see
    ``supply_item`` uniqueness below)."""
    alert_id    = models.AutoField(primary_key=True)
    # ✅ FIX: unique=True (was a non-unique FK backed only by the flawed
    # ('supply_item', 'is_resolved') unique_together below). One alert row
    # per item is created once and updated in place forever after; there is
    # nothing left to collide with, so resolving it a second/third/Nth time
    # can never raise an IntegrityError the way the old design could.
    supply_item = models.OneToOneField(SupplyItem, on_delete=models.CASCADE, related_name='alerts')

    current_stock = models.PositiveIntegerField()
    threshold     = models.PositiveIntegerField()
    is_resolved   = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    resolved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Low stock — {self.supply_item.name}"

    class Meta:
        ordering = ['-created_at']


# ─────────────────────────────────────────────────────────
# General / Retail (FMCG) Items — diapers, tissues, soap,
# shampoo, toothpaste, chocolates, etc.
#
# These are NOT prescription medicines (no generic_name/route/strength)
# and NOT internal ward consumables (no "issue to department" concept —
# see SupplyItem above). They are ordinary retail products sold straight
# to a walk-in/registered patient through a PharmacyBill, exactly like a
# medicine line, which is why this mirrors Medicine/MedicineBatch/
# PharmacyBillMedicineItem field-for-field.
# ─────────────────────────────────────────────────────────
class GeneralItemCategoryChoices(models.TextChoices):
    BABY_CARE     = 'BABY_CARE',     'Baby Care'
    PERSONAL_CARE = 'PERSONAL_CARE', 'Personal Care & Toiletries'
    FOOD_BEVERAGE = 'FOOD_BEVERAGE', 'Food & Beverages'
    HOUSEHOLD     = 'HOUSEHOLD',     'Household & Cleaning'
    STATIONERY    = 'STATIONERY',    'Stationery & Sundries'
    OTHER         = 'OTHER',         'Other'


class GeneralItem(models.Model):
    """Catalogue entry for a sellable non-medicine retail product."""
    item_id     = models.AutoField(primary_key=True)

    # ✅ FIX: no branch field — same gap as Medicine/SupplyItem. branch FK +
    # branch-scoped unique_together on name.
    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="general_items",
    )

    name        = models.CharField(max_length=255)
    brand       = models.CharField(max_length=150, blank=True, null=True)
    category    = models.CharField(
        max_length=20,
        choices=GeneralItemCategoryChoices.choices,
        default=GeneralItemCategoryChoices.OTHER,
    )
    unit        = models.CharField(max_length=50, blank=True, null=True, help_text="e.g. 'pack', 'piece', 'bottle'")
    description = models.TextField(blank=True, null=True)
    is_active   = models.BooleanField(default=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def total_stock(self):
        total = 0
        for batch in self.batches.filter(status='ACTIVE'):
            total += max(0, batch.quantity - batch.allocated_quantity)
        return total

    def __str__(self):
        return f"{self.name} ({self.brand})" if self.brand else self.name

    class Meta:
        ordering = ['category', 'name']
        unique_together = [('branch', 'name')]
        indexes = [
            models.Index(fields=['category']),
            models.Index(fields=['is_active']),
        ]


class GeneralItemBatch(models.Model):
    """Stock batch for a GeneralItem — same purchase/pricing/reorder shape as MedicineBatch."""
    # ✅ NEW: PENDING_APPROVAL / REJECTED — same approval gate as
    # MedicineBatch.BATCH_STATUS_CHOICES above; see that comment for the
    # full explanation.
    BATCH_STATUS_CHOICES = [
        ('ACTIVE',            'Active'),
        ('EXPIRED',           'Expired'),
        ('DEPLETED',          'Depleted'),
        ('PENDING_APPROVAL',  'Pending manager approval'),
        ('REJECTED',          'Rejected — returned to dealer'),
    ]

    batch_id     = models.AutoField(primary_key=True)
    general_item = models.ForeignKey(GeneralItem, on_delete=models.CASCADE, related_name='batches')
    batch_number = models.CharField(max_length=100)

    dealer = models.ForeignKey(
        'manager.Dealer', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='general_item_batches',
        help_text='Dealer this stock was purchased from (optional).',
    )
    settlement_method = models.CharField(
        max_length=10,
        choices=[('CREDIT', 'Credit — to be paid later'), ('PAID', 'Paid in full on receipt')],
        blank=True, null=True,
    )

    quantity           = models.PositiveIntegerField(default=0)
    allocated_quantity = models.PositiveIntegerField(default=0)

    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    mrp        = models.DecimalField(max_digits=10, decimal_places=2)

    gst_percentage      = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    low_stock_threshold = models.PositiveIntegerField(default=10)

    # Optional — unlike medicines, many FMCG items (soap, tissues) never expire.
    expiry_date = models.DateField(null=True, blank=True)
    status      = models.CharField(max_length=17, choices=BATCH_STATUS_CHOICES, default='ACTIVE')

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def get_available_quantity(self):
        return max(0, self.quantity - self.allocated_quantity)

    @property
    def is_expiring_soon(self):
        if not self.expiry_date:
            return False
        return self.expiry_date <= timezone.localdate() + timedelta(days=30)

    def clean(self):
        if self.cost_price and self.mrp and self.cost_price > self.mrp:
            raise ValidationError({'cost_price': f'Cost price (₹{self.cost_price}) cannot be greater than MRP (₹{self.mrp})'})

    def __str__(self):
        return f"{self.general_item.name} — batch {self.batch_number}"

    class Meta:
        ordering = ['general_item__name', 'expiry_date']
        indexes = [
            models.Index(fields=['general_item']),
            models.Index(fields=['status']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['general_item', 'batch_number'], name='unique_batch_number_per_general_item'),
        ]


class GeneralItemStockLog(models.Model):
    CHANGE_TYPE_CHOICES = [
        ('IN',     'Stock In'),
        ('OUT',    'Dispensed'),
        ('RETURN', 'Return'),
        ('ADJUST', 'Adjustment'),
    ]
    log_id           = models.AutoField(primary_key=True)
    batch            = models.ForeignKey(GeneralItemBatch, on_delete=models.CASCADE, related_name='stock_logs')
    change_type      = models.CharField(max_length=10, choices=CHANGE_TYPE_CHOICES)
    quantity_changed = models.IntegerField()
    remarks          = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    def __str__(self):
        return f"{self.batch} | {self.change_type} {self.quantity_changed}"

    class Meta:
        ordering = ['-created_at']


class GeneralItemStockAlert(models.Model):
    ALERT_TYPE_CHOICES = [
        ('LOW_STOCK', 'Low Stock'),
        ('EXPIRY',    'Near Expiry / Expired'),
    ]
    alert_id    = models.AutoField(primary_key=True)
    batch       = models.ForeignKey(GeneralItemBatch, on_delete=models.CASCADE, related_name='alerts')
    alert_type  = models.CharField(max_length=15, choices=ALERT_TYPE_CHOICES)
    is_resolved = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    def __str__(self):
        return f"{self.alert_type} — {self.batch}"

    class Meta:
        ordering = ['-created_at']
        unique_together = [('batch', 'alert_type')]


def _check_general_item_alerts(batch):
    """Mirrors _check_stock_alerts() but for GeneralItemBatch."""
    today = timezone.localdate()

    if batch.quantity <= batch.low_stock_threshold and batch.status == 'ACTIVE':
        GeneralItemStockAlert.objects.update_or_create(
            batch=batch, alert_type='LOW_STOCK', defaults={'is_resolved': False},
        )
    else:
        GeneralItemStockAlert.objects.filter(
            batch=batch, alert_type='LOW_STOCK', is_resolved=False
        ).update(is_resolved=True)

    # Expiry alert — see the matching comment in _check_stock_alerts()
    # above: excludes DEPLETED batches (e.g. fully returned stock) so a
    # batch with zero remaining quantity stops being flagged as
    # "expiring soon" just because its date happens to be near/past.
    if batch.expiry_date and batch.status != 'DEPLETED' and batch.expiry_date <= today + timedelta(days=30):
        GeneralItemStockAlert.objects.update_or_create(
            batch=batch, alert_type='EXPIRY', defaults={'is_resolved': False},
        )
    else:
        GeneralItemStockAlert.objects.filter(
            batch=batch, alert_type='EXPIRY', is_resolved=False
        ).update(is_resolved=True)


class PharmacyBillGeneralItem(models.Model):
    """A general/FMCG retail line item on a PharmacyBill — same shape as PharmacyBillMedicineItem."""
    item_id = models.AutoField(primary_key=True)
    bill    = models.ForeignKey(PharmacyBill,       on_delete=models.CASCADE, related_name='general_items')
    batch   = models.ForeignKey(GeneralItemBatch,   on_delete=models.PROTECT, related_name='bill_items')

    quantity       = models.PositiveIntegerField()
    unit_mrp       = models.DecimalField(max_digits=10, decimal_places=2)
    gst_percentage = models.DecimalField(max_digits=5,  decimal_places=2, default=0)
    item_total     = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    is_dispensed   = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.unit_mrp       = self.batch.mrp
        self.gst_percentage = self.batch.gst_percentage
        self.item_total     = self.unit_mrp * self.quantity
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.batch.general_item.name} x{self.quantity}"

    class Meta:
        ordering = ['item_id']


class GeneralItemReturnReasonChoices(models.TextChoices):
    EXPIRED   = 'EXPIRED', 'Expired'
    DAMAGED   = 'DAMAGED', 'Damaged'
    DEFECTIVE = 'DEFECTIVE', 'Defective'
    RECALL    = 'RECALL', 'Recall'
    EXCESS    = 'EXCESS', 'Excess / Over-ordered'
    OTHER     = 'OTHER', 'Other'


class GeneralItemReturn(models.Model):
    """A record of general/FMCG stock returned from a GeneralItemBatch back
    to the dealer/provider it was purchased from. Mirrors SupplyReturn."""
    return_id    = models.AutoField(primary_key=True)
    general_item = models.ForeignKey(GeneralItem, on_delete=models.CASCADE, related_name='returns')
    batch        = models.ForeignKey(GeneralItemBatch, on_delete=models.CASCADE, related_name='returns')

    quantity_returned = models.PositiveIntegerField()
    reason = models.CharField(
        max_length=20,
        choices=GeneralItemReturnReasonChoices.choices,
        default=GeneralItemReturnReasonChoices.OTHER,
        help_text='Why this stock is being returned',
    )
    reason_details = models.CharField(max_length=300, blank=True, null=True)
    reference_number = models.CharField(
        max_length=100, blank=True, null=True,
        help_text='Supplier credit note / RMA number',
    )

    dealer = models.ForeignKey(
        'manager.Dealer', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='general_item_returns',
        help_text='Dealer this return is going back to (optional).',
    )
    SETTLEMENT_CHOICES = [
        ('CREDIT',   'Credit — carried forward against future orders'),
        ('REFUND',   'Cash refund'),
        ('EXCHANGE', 'Exchanged for other product(s)'),
    ]
    settlement_method = models.CharField(
        max_length=10, choices=SETTLEMENT_CHOICES, blank=True, null=True,
        help_text='How the pharmacist expects this return to be settled by the dealer (optional).',
    )

    # Refund owed by the dealer — quantity returned × the batch's purchase
    # (cost) price, NOT the MRP/selling price. Snapshotted at return time.
    refund_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Refund owed by the dealer — quantity returned × the batch's cost price.",
    )

    returned_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='general_item_returns')
    returned_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.general_item.name} x{self.quantity_returned} returned — {self.get_reason_display()}"

    class Meta:
        ordering = ['-returned_at']
        indexes = [
            models.Index(fields=['general_item'], name='pharm_gi_ret_item_idx'),
            models.Index(fields=['batch'], name='pharm_gi_ret_batch_idx'),
        ]


def _check_supply_alerts(supply_item):
    """
    Open/reopen or auto-resolve the single low-stock alert row for a
    SupplyItem, called after any stock deduction/addition/adjustment.
    """
    total_stock = supply_item.total_stock
    threshold = supply_item.low_stock_threshold

    if total_stock < threshold:
        SupplyStockAlert.objects.update_or_create(
            supply_item=supply_item,
            defaults={
                'current_stock': total_stock,
                'threshold': threshold,
                'is_resolved': False,
                'resolved_at': None,
            },
        )
    else:
        SupplyStockAlert.objects.filter(supply_item=supply_item, is_resolved=False).update(
            is_resolved=True, resolved_at=timezone.now(),
        )