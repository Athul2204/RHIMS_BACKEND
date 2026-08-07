# reception/models.py
# ============================================================
# COMPLETE UPDATED FILE - WITH DOCTOR ASSIGNMENT
# ============================================================
# Changes:
# ✅ Added assigned_doctor FK to Patient model
# ✅ Added index for performance
# ✅ All existing code preserved
#
# ✅ BUG FIXES APPLIED:
#
# Bug 1 (PRIMARY — caused HTTP 500 on every bill creation):
#   ConsultationTimeline was created with event='CONSULTATION_STARTED'
#   which is NOT a valid choice on the event field.
#   Valid choices are: CREATED, STARTED, LAB_REQUESTED, LAB_COMPLETED,
#   PRESCRIPTION_ISSUED, COMPLETED, REOPENED, NOTES_UPDATED,
#   STATUS_CHANGED, OTHER.
#   Fixed: changed to event='CREATED'.
#
# Bug 2 (silent data loss for guest-doctor bills):
#   Consultation.objects.create() was missing guest_doctor=self.guest_doctor,
#   so Scenario B (guest doctor) consultations were stored with no doctor
#   reference at all — the doctor's queue never saw them.
#   Fixed: added guest_doctor=self.guest_doctor to the create() call.
# ============================================================

from django.db import models, transaction
from django.utils import timezone
from django.core.validators import RegexValidator
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from datetime import date, timedelta


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────
def _date_str(d):
    """DD-MM-YYYY string for bill/OP number formatting."""
    return d.strftime("%d-%m-%Y")


def _generate_mrd(branch):
    """
    Sequential, branch-prefixed patient number: TVM-0001, KOL-0001, ...

    ✅ CHANGED: dropped the literal "MRD" segment (was TVM-MRD-0001) per
    request — now just {branch code}-{number}. Existing patients keep
    their old-format mrd_number untouched (nothing renames historical
    rows); only patients registered from now on get the new format.
    The sequence itself isn't a separately stored counter — it's derived
    from the most recently created patient's mrd_number — so this picks
    up exactly where the old TVM-MRD-#### sequence left off:
    mrd_number.split("-")[-1] takes the *last* hyphen-separated segment
    either way, so "TVM-MRD-0007".split("-")[-1] and "TVM-0007".split("-")[-1]
    both correctly yield "0007".

    Uses select_for_update to prevent race conditions. Only ever called for
    patients that actually get a real MRD (has_mrd=True) — home-visit
    entries without one skip this entirely.
    """
    prefix = f"{branch.code}-"
    last = (
        Patient.objects
        .select_for_update()
        .filter(branch_id=branch.branch_id, mrd_number__startswith=prefix)
        .order_by("-patient_id")
        .first()
    )
    new_number = 1
    if last and last.mrd_number:
        try:
            new_number = int(last.mrd_number.split("-")[-1]) + 1
        except (ValueError, IndexError):
            pass
    return f"{prefix}{str(new_number).zfill(4)}"


def _generate_op_number(bill_date, branch):
    """
    Daily, branch-prefixed OP number: TVM-OP-0001, KOL-OP-0001, ...
    Resets to 0001 each new day *per branch* — the daily reset is enforced
    by scoping the "last number" lookup to this branch's bills whose
    consultation_date matches bill_date, and by a DB-level unique_together
    constraint on (branch, op_number, consultation_date) — see
    ConsultationBill.Meta. This means TVM-OP-0001 can legitimately appear
    again on a later day, and KOL-OP-0001 can exist the same day.
    """
    prefix = f"{branch.code}-OP-"
    last = (
        ConsultationBill.objects
        .select_for_update()
        .filter(branch_id=branch.branch_id, op_number__startswith=prefix, consultation_date=bill_date)
        .order_by("-bill_id")
        .first()
    )
    new_number = 1
    if last and last.op_number:
        try:
            new_number = int(last.op_number.split("-")[-1]) + 1
        except (ValueError, IndexError):
            pass
    return f"{prefix}{str(new_number).zfill(4)}"


def _generate_cons_bill_number(bill_date, branch):
    """
    Continuous, branch-prefixed CONS number: TVM-CONS-0001, KOL-CONS-0001, ...
    Never resets — keeps incrementing per branch across all days.
    """
    prefix = f"{branch.code}-CONS-"
    last = (
        ConsultationBill.objects
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


def _generate_prebooking_reference(branch):
    """
    Continuous, branch-prefixed reference for ConsultationPreBooking:
    TVM-PB-000001, KOL-PB-000001, ... Never resets — mirrors
    _generate_cons_bill_number's continuous-numbering approach (as opposed
    to _generate_op_number's daily reset), since a booking reference has
    to stay unique and lookup-able indefinitely (patient may call back
    days later quoting it). Populated for every booking mode
    (CALL/WALKIN/WEBSITE), but it's the public website flow
    (public/views.py) that actually surfaces it to the caller — internal
    reception flows already show prebooking_id.
    """
    prefix = f"{branch.code}-PB-"
    last = (
        ConsultationPreBooking.objects
        .select_for_update()
        .filter(branch_id=branch.branch_id, reference_number__startswith=prefix)
        .order_by("-prebooking_id")
        .first()
    )
    new_number = 1
    if last and last.reference_number:
        try:
            new_number = int(last.reference_number.split("-")[-1]) + 1
        except (ValueError, IndexError):
            pass
    return f"{prefix}{str(new_number).zfill(6)}"


# ─────────────────────────────────────────────────────────
# Patient
# ─────────────────────────────────────────────────────────
# ✅ UPDATED: Added assigned_doctor field for strict access control
class Patient(models.Model):

    GENDER_CHOICES = [
        ('Male', 'Male'),
        ('Female', 'Female'),
        ('Other', 'Other'),
    ]

    BLOOD_GROUP_CHOICES = [
        ('A+', 'A+'), ('A-', 'A-'),
        ('B+', 'B+'), ('B-', 'B-'),
        ('AB+', 'AB+'), ('AB-', 'AB-'),
        ('O+', 'O+'), ('O-', 'O-'),
    ]

    patient_id = models.AutoField(primary_key=True)

    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="patients",
        help_text="Branch this patient record belongs to. Re-registration at a "
                   "different branch creates a new Patient row with its own MRD, "
                   "not a cross-branch link.",
    )

    # False for a home-visit entry taken before a real MRD has been issued —
    # e.g. a reception call-in that just needs a name/phone/address on file
    # to schedule the visit. mrd_number stays NULL while this is False.
    # Flip to True (via assign_mrd()) once the patient is actually
    # registered with a real medical record — at that point prescriptions,
    # lab history, and anything else that needs a real MRD become available.
    has_mrd = models.BooleanField(
        default=True,
        help_text="False for a home-visit entry that hasn't been issued a real "
                   "MRD yet. Prescriptions and lab history stay blocked until "
                   "this is True and mrd_number is assigned.",
    )

    mrd_number = models.CharField(
        max_length=20, null=True, blank=True, editable=False, db_index=True,
        help_text="NULL until has_mrd is True — a home-visit-only patient may "
                   "not have one yet.",
    )

    first_name = models.CharField(max_length=100)
    # blank=True: many patients (esp. quick-registered via the prebooking
    # "new patient" quick-entry, which only collects a single free-text
    # name field) legitimately have a single-word name with no surname.
    # Previously this was required, so Patient.full_clean() rejected any
    # single-word name with "This field cannot be blank." — surfacing as a
    # 400 on ConsultationPreBookingConvertView the moment reception tried
    # to convert a booking for a patient like "Shyam".
    last_name = models.CharField(max_length=100, blank=True, default='')

    phone = models.CharField(
        max_length=10,
        validators=[RegexValidator(r'^\d{10}$', 'Phone must be exactly 10 digits.')],
        db_index=True
    )

    place = models.CharField(max_length=200, blank=True, null=True, db_index=True)
    address = models.TextField(blank=True, null=True)

    date_of_birth = models.DateField(null=True, blank=True)
    age = models.PositiveIntegerField(blank=True, null=True)

    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, null=True, blank=True)
    blood_group = models.CharField(
        max_length=3, choices=BLOOD_GROUP_CHOICES, blank=True, null=True
    )

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    # True once the one-time MRD registration fee has been collected.
    # Set permanently on the patient record the moment the first bill
    # carrying a registration_fee is marked PAID.
    # Checked by ConsultationBill._compute_registration_fee() so the fee
    # is never charged twice, regardless of billing history.
    registration_fee_paid = models.BooleanField(
        default=False,
        help_text='True once the one-time MRD registration fee has been collected.',
    )

    # ============================================================
    # ✅ NEW FIELD: DOCTOR ASSIGNMENT FOR STRICT ACCESS CONTROL
    # ============================================================
    # The primary doctor assigned to manage this patient.
    # Only this doctor (and admins) can view this patient's records.
    # This enforces strict access control - no fallback to consultation history.
    assigned_doctor = models.ForeignKey(
        'doctor.DoctorProfile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_patients',
        help_text='The primary doctor assigned to this patient. Only this doctor (and admins) can view the patient.',
        db_index=True,
    )

    def clean(self):
        if self.date_of_birth and self.date_of_birth > timezone.localdate():
            raise ValidationError({"date_of_birth": "Date of birth cannot be in the future."})
        # has_mrd=False means "no real MRD yet" — mrd_number must stay NULL
        # so it can never look like a real record until assign_mrd() runs.
        if not self.has_mrd and self.mrd_number:
            raise ValidationError({
                "mrd_number": "A patient without has_mrd cannot have an mrd_number. "
                               "Use assign_mrd() to promote this record instead."
            })

    def save(self, *args, **kwargs):
        self.full_clean()

        if self.date_of_birth:
            today = timezone.localdate()
            self.age = (
                today.year - self.date_of_birth.year
                - ((today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day))
            )

        if self.has_mrd and not self.mrd_number:
            with transaction.atomic():
                self.mrd_number = _generate_mrd(self.branch)

        super().save(*args, **kwargs)

    def assign_mrd(self):
        """
        Promote a no-MRD home-visit entry to a fully registered patient:
        issues a real mrd_number and flips has_mrd to True. No-op if the
        patient already has one. Callers (views/serializers) are
        responsible for gating this behind whatever business step actually
        counts as "registration" (e.g. first walk-in visit, fee collected).
        """
        if self.has_mrd and self.mrd_number:
            return self
        self.has_mrd = True
        self.save()
        return self

    def __str__(self):
        ident = self.mrd_number or "No-MRD"
        return f"{ident} - {self.first_name} {self.last_name}"

    class Meta:
        indexes = [
            models.Index(fields=["mrd_number"]),
            models.Index(fields=["phone"]),
            models.Index(fields=["place"]),
            models.Index(fields=["assigned_doctor"]),  # ✅ NEW INDEX for assignment queries
        ]
        # Only enforced when mrd_number is not null — NULLs are never
        # considered equal to each other by unique_together, so any number
        # of has_mrd=False rows can coexist per branch.
        unique_together = [("branch", "mrd_number")]


# ─────────────────────────────────────────────────────────
# Consultation Bill
# ─────────────────────────────────────────────────────────
class ConsultationBill(models.Model):

    CONSULTATION_TYPE_CHOICES = [
        ('NEW', 'New'),
        ('REVISIT', 'Revisit'),
        ('HOME_VISIT', 'Home Visit'),
    ]

    PAYMENT_METHOD_CHOICES = [
        ('CASH', 'Cash'),
        ('UPI', 'UPI'),
    ]

    PAYMENT_STATUS_CHOICES = [
        ('PAID', 'Paid'),
        ('PENDING', 'Pending'),
    ]

    bill_id = models.AutoField(primary_key=True)

    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="consultation_bills",
        null=True, blank=True,
        help_text="Auto-set from patient.branch on save if not provided.",
    )

    # Auto-generated identifiers
    bill_number = models.CharField(max_length=30, editable=False, db_index=True)
    op_number = models.CharField(max_length=30, editable=False, db_index=True)

    patient = models.ForeignKey(
        Patient, on_delete=models.CASCADE, related_name="consultation_bills"
    )

    # ── Doctor assignment — three mutually exclusive scenarios ────────────
    #
    # Scenario A – Registered DoctorProfile:
    #   doctor FK is set, guest_doctor is NULL.
    #   doctor_name is auto-populated from the DoctorProfile.
    #
    # Scenario B – Guest / Visiting Doctor (GuestDoctorProfile):
    #   doctor is NULL, guest_doctor FK is set.
    #   doctor_name is auto-populated from GuestDoctorProfile.full_name.
    #
    # Scenario C – Anonymous / Manual Entry (legacy fallback):
    #   Both doctor and guest_doctor are NULL.
    #   Reception types doctor_name manually.
    #
    # doctor_name is always stored so it survives future deactivation/deletion.

    doctor = models.ForeignKey(
        'doctor.DoctorProfile',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='consultation_bills',
    )

    guest_doctor = models.ForeignKey(
        'administration.GuestDoctorProfile',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='consultation_bills',
    )

    # Permanent name copy — preserved even if doctor/guest_doctor is deactivated.
    doctor_name = models.CharField(max_length=200)

    consultation_date = models.DateField(default=timezone.localdate)  # FIX: localdate returns a date, not a datetime
    consultation_type = models.CharField(
        max_length=10, choices=CONSULTATION_TYPE_CHOICES, default='NEW'
    )
    consultation_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )

    # Only meaningful when consultation_type == 'HOME_VISIT'. Optional,
    # defaults to ₹0. The Home Visit Fee itself is stored in
    # consultation_fee (reusing the existing field, relabeled on the
    # frontend for this type) — travel_charge is the one genuinely new
    # billing field this consultation type needs.
    travel_charge = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text='Optional travel charge for Home Visit bills. ₹0 for NEW/REVISIT.',
    )

    # revisit_valid_until is only meaningful for NEW consultation bills.
    # A REVISIT bill sets this to NULL so it cannot chain another free visit.
    revisit_valid_until = models.DateField(null=True, blank=True, editable=False)

    # ── Registration & totals ──────────────────────────────────────────────
    #
    # registration_fee: One-time MRD fee for brand-new patients.
    #   Auto-set from HospitalSettings.mrd_registration_fee when:
    #     - consultation_type == 'NEW'
    #     - this is the patient's very first ConsultationBill ever
    #   Always 0 for returning patients and FOLLOW_UP bills.
    #
    # total_amount: registration_fee + consultation_fee.
    #   Recomputed on every save so it is always consistent.
    #   This is the amount reception collects from the patient.

    registration_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text=(
            'One-time MRD registration charge. '
            'Auto-populated for first-visit bills; 0 for returning patients.'
        ),
    )

    # Flat-amount discount applied to this bill. Must be >= 0 and cannot
    # exceed the pre-discount subtotal (registration_fee + consultation_fee).
    # Re-validated server-side in clean() regardless of what the frontend
    # sends. total_amount is computed as subtotal - discount_amount, never
    # below 0.
    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text='Flat discount amount applied to this bill (not a percentage).',
    )

    total_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text=(
            'subtotal (registration_fee + consultation_fee) minus '
            'discount_amount. Computed on save, never below 0.'
        ),
    )

    payment_method = models.CharField(
        max_length=10, choices=PAYMENT_METHOD_CHOICES, default='CASH'
    )
    upi_reference = models.CharField(max_length=100, blank=True, null=True)

    payment_status = models.CharField(
        max_length=10, choices=PAYMENT_STATUS_CHOICES, default='PENDING'
    )

    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    # True once the one-time MRD registration fee has been collected.
    # Set permanently on the patient record the moment the first bill
    # carrying a registration_fee is marked PAID.
    # Checked by ConsultationBill._compute_registration_fee() so the fee
    # is never charged twice, regardless of billing history.
    registration_fee_paid = models.BooleanField(
        default=False,
        help_text='True once the one-time MRD registration fee has been collected.',
    )

    def clean(self):
        if self.payment_method == 'UPI' and not self.upi_reference:
            raise ValidationError({"upi_reference": "UPI reference number is required for UPI payments."})
        if self.consultation_type == 'REVISIT' and self.consultation_fee != 0:
            raise ValidationError({"consultation_fee": "Revisit consultations must have ₹0 fee."})
        if self.consultation_type == 'HOME_VISIT' and not self.consultation_fee:
            raise ValidationError({"consultation_fee": "Home Visit Fee is required and must be greater than ₹0."})
        if self.consultation_fee is not None and self.consultation_fee < 0:
            raise ValidationError({"consultation_fee": "Consultation fee cannot be negative."})
        if self.travel_charge is not None and self.travel_charge < 0:
            raise ValidationError({"travel_charge": "Travel charge cannot be negative."})
        if self.registration_fee is not None and self.registration_fee < 0:
            raise ValidationError({"registration_fee": "Registration fee cannot be negative."})
        # Ensure doctor and guest_doctor are not both set
        if self.doctor_id and self.guest_doctor_id:
            raise ValidationError({
                "doctor": "A bill cannot have both a registered doctor and a guest doctor."
            })
        # Discount must be a non-negative flat amount and cannot exceed the
        # pre-discount subtotal. Registration fee is server-computed (see
        # _compute_registration_fee), so this checks against consultation_fee
        # + registration_fee as currently set on the instance; save()
        # recomputes registration_fee before this total is used to build
        # total_amount, keeping the two in sync.
        if self.discount_amount is not None and self.discount_amount < 0:
            raise ValidationError({"discount_amount": "Discount amount cannot be negative."})
        if self.discount_amount:
            from decimal import Decimal
            subtotal = (
                (self.consultation_fee or Decimal('0'))
                + (self.registration_fee or Decimal('0'))
                + (self.travel_charge or Decimal('0'))
            )
            if self.discount_amount > subtotal:
                raise ValidationError({
                    "discount_amount": "Discount amount cannot exceed the bill subtotal."
                })

    def _compute_registration_fee(self):
        """
        One-time MRD registration fee logic — single rule:

          Charge once per MRD number, never again.

          - Patient.registration_fee_paid = True  →  ₹0  (already collected)
          - REVISIT bill                         →  ₹0  (not a new registration)
          - Everything else                        →  HospitalSettings.mrd_registration_fee

        The flag is stored on the Patient record (not on billing history),
        so it is permanent and survives any bill deletions or re-creations.
        """
        from decimal import Decimal

        # Already paid for this MRD — never charge again.
        if self.patient.registration_fee_paid:
            return Decimal('0')

        # Revisit visits don't carry a registration component.
        if self.consultation_type == 'REVISIT':
            return Decimal('0')

        # New MRD, first-ever visit — charge the registration fee.
        from administration.models import HospitalSettings
        return HospitalSettings.get(self.branch).mrd_registration_fee or Decimal('0')

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if not self.branch_id and self.patient_id:
            self.branch = self.patient.branch
        self.full_clean()

        # revisit_valid_until is set in MarkBillPaidView when payment is confirmed.
        # At creation time it is always NULL — the window only starts after payment.
        if not self.pk:
            self.revisit_valid_until = None

        # Registration fee is always computed from the MRD flag — never
        # taken from the request payload.  On new bills set it unconditionally;
        # on updates recompute so total_amount stays consistent.
        from decimal import Decimal
        self.registration_fee = self._compute_registration_fee()
        subtotal = (
            (self.consultation_fee or Decimal('0'))
            + (self.registration_fee or Decimal('0'))
            + (self.travel_charge or Decimal('0'))
        )
        discount = self.discount_amount or Decimal('0')

        # Re-validate against the *final* subtotal now that registration_fee
        # has been authoritatively recomputed above — clean() already ran
        # this check, but registration_fee may have changed since (e.g. it
        # is always forced to 0 for REVISIT bills), so re-check here rather
        # than trusting the pre-recompute value.
        if discount > subtotal:
            raise ValidationError({
                "discount_amount": "Discount amount cannot exceed the bill subtotal."
            })

        self.total_amount = max(subtotal - discount, Decimal('0'))

        # REVISIT consultations are always ₹0 (enforced in clean()) — there is
        # nothing for reception to actually collect, so there's no reason to
        # make them click "Mark Paid" for a ₹0 bill. Auto-settle it as PAID
        # the moment the bill is created. This only applies at creation time
        # so it can never silently flip a bill that's being edited later.
        if is_new and self.consultation_type == 'REVISIT':
            self.payment_status = 'PAID'

        if not self.bill_number:
            with transaction.atomic():
                bill_date = self.consultation_date or timezone.localdate()
                self.op_number = _generate_op_number(bill_date, self.branch)
                self.bill_number = _generate_cons_bill_number(bill_date, self.branch)

        super().save(*args, **kwargs)

        if is_new:
            from doctor.models import Consultation, ConsultationTimeline

            # Resolve which User account to link as doctor_user so the
            # consultation appears in the right doctor's queue.
            #
            # Scenario A – Registered doctor:
            #   doctor FK is set → use doctor.staff.user (always present;
            #   DoctorProfile.staff is a required OneToOneField).
            #
            # Scenario B – Guest doctor with a portal login:
            #   guest_doctor FK is set AND guest_doctor.user is not None →
            #   use that user account.  Guest doctors without a login account
            #   (guest_doctor.user_id is None) legitimately have no queue.
            #
            # Scenario C – Manual / anonymous entry:
            #   Both FKs are None → doctor_user stays None.
            _doctor_user = None
            if self.doctor_id:
                try:
                    _doctor_user = self.doctor.staff.user
                except Exception:
                    pass
            elif self.guest_doctor_id:
                try:
                    # .user_id is a direct column read — no extra DB query.
                    # Only fetch the full User object when a login exists.
                    if self.guest_doctor.user_id:
                        _doctor_user = self.guest_doctor.user
                except Exception:
                    pass

            consultation = Consultation.objects.create(
                patient=self.patient,
                consultation_bill=self,
                doctor=self.doctor,
                guest_doctor=self.guest_doctor,   # ✅ FIX Bug 2: was missing — guest-doctor bills had no doctor reference in Consultation
                doctor_user=_doctor_user,
                consultation_date=self.consultation_date or timezone.localdate()
            )
            ConsultationTimeline.objects.create(
                consultation=consultation,
                event='CREATED',                  # ✅ FIX Bug 1: was 'CONSULTATION_STARTED' — not a valid choice, caused HTTP 500
                description='Consultation created automatically from reception billing.'
            )

    @property
    def subtotal(self):
        """Pre-discount total: registration_fee + consultation_fee."""
        from decimal import Decimal
        return (self.consultation_fee or Decimal('0')) + (self.registration_fee or Decimal('0'))

    def __str__(self):
        return f"{self.bill_number} - {self.patient.mrd_number}"

    class Meta:
        ordering = ["-bill_id"]
        indexes = [
            models.Index(fields=["bill_number"]),
            models.Index(fields=["op_number"]),
            models.Index(fields=["patient", "consultation_date"]),
        ]
        unique_together = [
            ("branch", "op_number", "consultation_date"),
            ("branch", "bill_number"),
        ]

def patient_is_revisit_eligible(patient):
    """
    Shared eligibility check used by RevisitCheckView, the prebooking
    write serializer, and the prebooking convert flow, so the three stay
    in sync instead of re-implementing this logic separately.

    True while `today` is within the patient's free-revisit window: the
    day of their last NEW consultation plus the following 2 days —
    i.e. today, tomorrow, and the day after tomorrow relative to that
    visit. False (and the caller should default back to NEW) once that
    3-day window has passed.

    A cancelled appointment never actually happened clinically, so it
    must not be able to seed a free-revisit window for the patient's
    next real visit. Bills whose linked Consultation was cancelled are
    excluded from the lookup entirely (in addition to CancelBillView
    clearing revisit_valid_until on the cancelled bill itself — this
    filter is the belt to that braces, e.g. for older cancelled bills
    from before that field was cleared).
    """
    latest_new_bill = (
        ConsultationBill.objects
        .filter(patient=patient, consultation_type='NEW')
        .exclude(consultation__status='CANCELLED')
        .order_by('-consultation_date')
        .first()
    )
    if not latest_new_bill or latest_new_bill.revisit_valid_until is None:
        return False
    return timezone.localdate() <= latest_new_bill.revisit_valid_until


# ─────────────────────────────────────────────────────────
# Consultation Pre-Booking
# ─────────────────────────────────────────────────────────
# A slot reserved ahead of the patient's actual visit — either a phone
# call-in or a walk-in who wants to book for a later time/date. On the
# day of the visit reception "converts" the booking into a real
# ConsultationBill (see reception/views.py convert action).
#
# Mirrors ConsultationBill's mutually-exclusive doctor/guest_doctor
# pattern, and Patient/LabRequest's "quick fields for a not-yet-registered
# person" pattern for brand-new patients who haven't been through MRD
# registration yet.
class ConsultationPreBooking(models.Model):

    BOOKING_MODE_CHOICES = [
        ('CALL', 'Call-in'),
        ('WALKIN', 'Walk-in'),
        ('WEBSITE', 'Public Website'),
    ]

    PAYMENT_STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('PAID', 'Paid'),
    ]

    PAYMENT_METHOD_CHOICES = [
        ('CASH', 'Cash'),
        ('UPI', 'UPI'),
    ]

    STATUS_CHOICES = [
        ('BOOKED', 'Booked'),
        ('CONFIRMED', 'Confirmed'),
        ('CONVERTED', 'Converted'),
        ('CANCELLED', 'Cancelled'),
        ('NO_SHOW', 'No Show'),
    ]

    GENDER_CHOICES = [
        ('Male', 'Male'),
        ('Female', 'Female'),
        ('Other', 'Other'),
    ]

    CONSULTATION_TYPE_CHOICES = [
        ('NEW', 'New'),
        ('REVISIT', 'Revisit'),
        ('HOME_VISIT', 'Home Visit'),
    ]

    prebooking_id = models.AutoField(primary_key=True)

    branch = models.ForeignKey(
        "administration.Branch",
        on_delete=models.PROTECT,
        related_name="prebookings",
        null=True, blank=True,
        help_text="Which branch this booking is for. Auto-set from patient.branch "
                   "when an existing patient is linked; must be provided explicitly "
                   "for a brand-new (not-yet-registered) patient, since there's no "
                   "Patient row yet to derive it from.",
    )

    # Public, guessable-but-unique reference a caller/website user can quote
    # back to reception ("PB-000123"). Auto-generated in save() the first
    # time a row is created — see _generate_prebooking_reference() above.
    reference_number = models.CharField(max_length=30, editable=False, db_index=True, blank=True)

    # ── Patient — existing (FK) OR brand-new (quick fields) ──────────────
    # Exactly one of {patient} or {new_patient_name} must be set; enforced
    # in clean().
    patient = models.ForeignKey(
        Patient,
        on_delete=models.CASCADE,
        related_name='prebookings',
        null=True, blank=True,
        help_text='Existing registered patient. NULL if this is a new, not-yet-registered patient.',
    )
    new_patient_name = models.CharField(
        max_length=300, blank=True, null=True,
        help_text='Quick name entry for a new patient not yet MRD-registered.',
    )
    new_patient_phone = models.CharField(
        max_length=10, blank=True, null=True,
        validators=[RegexValidator(r'^\d{10}$', 'Phone must be exactly 10 digits.')],
    )
    new_patient_gender = models.CharField(
        max_length=10, choices=GENDER_CHOICES, blank=True, null=True,
    )
    new_patient_age = models.PositiveIntegerField(blank=True, null=True)

    # ── Doctor assignment — same mutually-exclusive pattern as ConsultationBill ──
    doctor = models.ForeignKey(
        'doctor.DoctorProfile',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='prebookings',
    )
    guest_doctor = models.ForeignKey(
        'administration.GuestDoctorProfile',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='prebookings',
    )

    booking_mode = models.CharField(max_length=10, choices=BOOKING_MODE_CHOICES, default='CALL')

    # Whether this booking is for a fresh consultation or a free revisit
    # within 3 days (today/tomorrow/day-after) of the patient's last NEW
    # consultation. Locks consultation_fee to ₹0 when set to REVISIT — see
    # clean() below. Re-validated against the patient's *current*
    # eligibility both here at booking time and again at convert time
    # (see ConsultationPreBookingConvertView), since the window can close
    # between when a call-in booking is taken and the day of the visit.
    consultation_type = models.CharField(
        max_length=10, choices=CONSULTATION_TYPE_CHOICES, default='NEW'
    )

    requested_date = models.DateField()
    requested_time = models.TimeField()

    # Copied from the doctor/guest doctor's consultation_fee at booking
    # time, so a later fee change doesn't retroactively alter this booking.
    consultation_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Only meaningful when consultation_type == 'HOME_VISIT'. Mirrors
    # ConsultationBill.travel_charge — optional, defaults to ₹0.
    travel_charge = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text='Optional travel charge for Home Visit bookings. ₹0 for NEW/REVISIT.',
    )

    # One-time MRD registration fee, mirroring ConsultationBill.registration_fee.
    # 0 until payment is collected; ConsultationPreBookingPayView computes and
    # locks it in at collection time (via _compute_prebooking_registration_fee
    # below) so the amount reception actually charged is preserved even if
    # HospitalSettings.mrd_registration_fee changes later. Left at 0 for
    # bookings that never get a "Pay" call before being converted directly —
    # in that case ConsultationBillSerializer/ConsultationBill computes it
    # fresh at convert time instead.
    registration_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text='One-time MRD registration charge, locked in when payment is collected.',
    )

    payment_status = models.CharField(max_length=10, choices=PAYMENT_STATUS_CHOICES, default='PENDING')
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHOD_CHOICES, blank=True, null=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='BOOKED', db_index=True)

    converted_bill = models.ForeignKey(
        ConsultationBill,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='prebooking_source',
        help_text='Set once this booking has been converted into a real ConsultationBill.',
    )

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='prebookings_created',
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-requested_date', '-requested_time']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['requested_date']),
            models.Index(fields=['payment_status']),
        ]
        unique_together = [("branch", "reference_number")]

    def clean(self):
        super().clean()

        # Exactly one patient source
        has_existing_patient = self.patient_id is not None
        has_new_patient_name = bool(self.new_patient_name and self.new_patient_name.strip())
        if has_existing_patient and has_new_patient_name:
            raise ValidationError({
                'patient': 'Provide either an existing patient or new-patient details, not both.'
            })
        if not has_existing_patient and not has_new_patient_name:
            raise ValidationError({
                'patient': 'Either an existing patient or new_patient_name is required.'
            })

        # branch is auto-derived from patient.branch (see save()) when an
        # existing patient is linked, but a brand-new patient has no Patient
        # row yet to derive it from — the caller must supply it directly.
        if not self.branch_id and not has_existing_patient:
            raise ValidationError({
                'branch': 'Branch is required when booking for a new, not-yet-registered patient.'
            })

        # Doctor and guest_doctor are mutually exclusive
        if self.doctor_id and self.guest_doctor_id:
            raise ValidationError({
                'doctor': 'A booking cannot have both a registered doctor and a guest doctor.'
            })

        if self.consultation_fee is not None and self.consultation_fee < 0:
            raise ValidationError({'consultation_fee': 'Consultation fee cannot be negative.'})

        if self.travel_charge is not None and self.travel_charge < 0:
            raise ValidationError({'travel_charge': 'Travel charge cannot be negative.'})

        if self.consultation_type == 'REVISIT' and self.consultation_fee not in (None, 0):
            raise ValidationError({'consultation_fee': 'Revisit consultation fee must be ₹0.'})

        if self.consultation_type == 'HOME_VISIT' and not self.consultation_fee:
            raise ValidationError({'consultation_fee': 'Home Visit Fee is required and must be greater than ₹0.'})

        if self.payment_status == 'PAID' and not self.payment_method:
            raise ValidationError({'payment_method': 'Payment method is required once payment_status is PAID.'})

    def save(self, *args, **kwargs):
        if not self.branch_id and self.patient_id:
            self.branch = self.patient.branch
        self.full_clean()

        # Only generate once — reference_number is meant to be stable for
        # the lifetime of the booking, not reissued on later PATCH saves
        # (e.g. status transitions, payment updates).
        if not self.reference_number:
            with transaction.atomic():
                self.reference_number = _generate_prebooking_reference(self.branch)
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)

    def get_patient_name(self):
        if self.patient_id:
            return f"{self.patient.first_name} {self.patient.last_name}".strip()
        return self.new_patient_name or 'Unknown'

    def get_patient_phone(self):
        if self.patient_id:
            return getattr(self.patient, 'phone', None)
        return self.new_patient_phone

    def get_doctor_name(self):
        if self.doctor_id:
            staff = getattr(self.doctor, 'staff', None)
            user = getattr(staff, 'user', None) if staff else None
            if user:
                return user.get_full_name() or user.username
            return None
        if self.guest_doctor_id:
            return self.guest_doctor.full_name
        return None

    def compute_registration_fee(self):
        """
        One-time MRD registration fee for THIS booking, mirroring
        ConsultationBill._compute_registration_fee() so a brand-new patient
        is charged the fee at whichever point money is first actually
        collected — either here (ConsultationPreBookingPayView, when
        reception collects payment on the booking itself) or at Convert
        time if no "Pay" call ever happened first. Whichever happens
        first flips Patient.registration_fee_paid, so the other path
        naturally computes ₹0 and never double-charges.

          - No linked Patient yet (new_patient_* quick fields, not yet
            registered) -> charge it; there's no record to have paid
            before.
          - Patient.registration_fee_paid = True -> ₹0 (already collected).
          - REVISIT booking                      -> ₹0 (not a new registration).
          - Everything else                        -> HospitalSettings.mrd_registration_fee.
        """
        from decimal import Decimal
        if self.patient_id and self.patient.registration_fee_paid:
            return Decimal('0')
        if self.consultation_type == 'REVISIT':
            return Decimal('0')
        from administration.models import HospitalSettings
        return HospitalSettings.get(self.branch).mrd_registration_fee or Decimal('0')

    @property
    def total_amount(self):
        """consultation_fee + travel_charge + registration_fee (whichever of
        these is currently set/locked-in on the row -- see
        compute_registration_fee() for when registration_fee gets populated)."""
        from decimal import Decimal
        return (
            (self.consultation_fee or Decimal('0'))
            + (self.travel_charge or Decimal('0'))
            + (self.registration_fee or Decimal('0'))
        )

    def __str__(self):
        return f"PreBooking #{self.prebooking_id} — {self.get_patient_name()} [{self.status}]"