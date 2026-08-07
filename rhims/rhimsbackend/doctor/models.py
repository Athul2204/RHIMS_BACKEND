# ============================================================
# Django Models for Doctor Module
# ✅ PATCHED: Guest Doctor Support Added
# ============================================================

from decimal import Decimal, ROUND_CEILING

from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.utils import timezone
from django.contrib.auth.models import User

from pharmacist.models import Medicine, RouteChoices


# ============================================================
# CONSULTATION STATUS CHOICES
# ============================================================

class ConsultationStatus(models.TextChoices):
    STARTED = 'STARTED', 'Started'
    LAB_REQUESTED = 'LAB_REQUESTED', 'Lab Requested'
    WAITING_FOR_LAB = 'WAITING_FOR_LAB', 'Waiting for Lab'
    LAB_COMPLETED = 'LAB_COMPLETED', 'Lab Completed'
    FOLLOWUP_REQUIRED = 'FOLLOWUP_REQUIRED', 'Follow-up Required'
    COMPLETED = 'COMPLETED', 'Completed'
    CANCELLED = 'CANCELLED', 'Cancelled'


# ============================================================
# DOCTOR PROFILE MODEL
# ============================================================

class DoctorProfile(models.Model):

    profile_id = models.AutoField(primary_key=True)

    staff = models.OneToOneField(
        'administration.StaffProfile',
        on_delete=models.CASCADE,
        related_name='doctor_profile'
    )

    specialization = models.CharField(
        max_length=200,
        blank=True,
        null=True,
        help_text="Deprecated free-text field, kept as a one-release safety net after the "
                   "migration to `specialty` below. Not written to by new code — do not read "
                   "this for anything new; use `specialty` instead.",
    )

    # Queryable/filterable replacement for the free-text `specialization`
    # above. Backfilled from `specialization` by a data migration (see
    # doctor/migrations/0004_populate_specialty_from_specialization.py).
    # `specialization` itself is deliberately left in place for one release
    # as a safety net rather than dropped in the same change.
    specialty = models.ForeignKey(
        "manager.Specialty",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="doctors",
    )

    # ✅ FIX: no longer globally unique=True. DoctorProfile has no branch
    # field of its own (only reachable via staff -> StaffProfile.branch),
    # and a doctor working at two branches now gets two separate
    # StaffProfile/DoctorProfile rows (see StaffProfile.is_group_admin
    # docstring) — both legitimately sharing the same real-world
    # registration number. Dropped rather than reviving the deferred
    # DoctorMaster idea; duplicate-number typos become an admin-diligence
    # problem rather than a DB-enforced one, consistent with the
    # "duplicate for now" call already made on doctor bio/qualifications.
    registration_number = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )

    department = models.CharField(
        max_length=200,
        blank=True,
        null=True
    )

    consultation_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    is_available = models.BooleanField(default=True)

    created_at = models.DateTimeField(
        default=timezone.now,
        editable=False
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['profile_id']

    def clean(self):
        if self.staff.role != 'Doctor':
            raise ValidationError('Staff role must be Doctor')

    def __str__(self):
        return f"Dr. {self.staff.user.get_full_name()}"


# ============================================================
# CONSULTATION MODEL
# ============================================================
# ✅ PATCHED: Added guest_doctor FK for visiting doctors
# ✅ Updated Meta.indexes for guest_doctor and doctor_user
# ✅ Enhanced clean() method with better validation
# ✅ Added get_doctor_name() helper method
# ============================================================

class Consultation(models.Model):
    """
    Medical consultation record.
    
    Supports three doctor types:
    1. Regular Staff Doctor: doctor + doctor_user both set
    2. Guest Doctor: guest_doctor + doctor_user set (doctor is NULL)
    3. No Doctor: All doctor fields NULL (reference only)
    """

    consultation_id = models.AutoField(primary_key=True)

    consultation_bill = models.OneToOneField(
        'reception.ConsultationBill',
        on_delete=models.CASCADE,
        related_name='consultation',
        blank=True,
        null=True,
    )

    patient = models.ForeignKey(
        'reception.Patient',
        on_delete=models.CASCADE,
        related_name='consultations',
    )

    # ===================================================================
    # DOCTOR ASSIGNMENT FIELDS (3 TYPES)
    # ===================================================================

    # TYPE 1: Regular/Staff Doctor
    doctor = models.ForeignKey(
        'DoctorProfile',
        on_delete=models.SET_NULL,
        related_name='consultations',
        blank=True,
        null=True,
        help_text='Regular staff doctor (from DoctorProfile)'
    )

    # TYPE 2: Guest/Visiting Doctor (✅ PATCHED - NEW!)
    guest_doctor = models.ForeignKey(
        'administration.GuestDoctorProfile',
        on_delete=models.SET_NULL,
        related_name='consultations',
        blank=True,
        null=True,
        help_text='Visiting/consultant doctor (from GuestDoctorProfile)'
    )

    # Both: Direct user reference (for efficient filtering)
    doctor_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='doctor_consultations',
        blank=True,
        null=True,
        help_text='User account (same for both regular and guest doctors)'
    )

    # ===================================================================

    consultation_date = models.DateField(
        default=timezone.localdate
    )

    status = models.CharField(
        max_length=30,
        choices=ConsultationStatus.choices,
        default=ConsultationStatus.STARTED,
        db_index=True,
    )

    chief_complaint = models.TextField(
        blank=True,
        null=True
    )

    symptoms = models.TextField(
        blank=True,
        null=True
    )

    vital_signs = models.JSONField(
        blank=True,
        null=True
    )

    clinical_notes = models.TextField(
        blank=True,
        null=True
    )

    provisional_diagnosis = models.TextField(
        blank=True,
        null=True
    )

    final_diagnosis = models.TextField(
        blank=True,
        null=True
    )

    treatment_notes = models.TextField(
        blank=True,
        null=True
    )

    followup_instructions = models.TextField(
        blank=True,
        null=True
    )

    followup_date = models.DateField(
        blank=True,
        null=True
    )

    started_at = models.DateTimeField(
        default=timezone.now
    )

    completed_at = models.DateTimeField(
        blank=True,
        null=True
    )

    created_at = models.DateTimeField(
        default=timezone.now,
        editable=False
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        ordering = ['-consultation_id']
        indexes = [
            models.Index(fields=['patient', 'consultation_date']),
            models.Index(fields=['status']),
            models.Index(fields=['doctor_user']),        # ✅ PATCHED: New index
            models.Index(fields=['guest_doctor']),       # ✅ PATCHED: New index
        ]

    def clean(self):
        """
        Validate Consultation data.
        
        Supports three doctor assignment scenarios:
        1. Regular staff doctor (doctor + doctor_user)
        2. Guest doctor (guest_doctor + doctor_user)
        3. No doctor assigned (all NULL)
        """
        super().clean()
        
        # Validate patient
        if not self.patient_id:
            raise ValidationError('Patient is required')
        
        # Optional: Enforce doctor assignment if needed
        # Uncomment if you want to require a doctor for all consultations:
        # if not self.doctor_id and not self.guest_doctor_id and not self.doctor_user_id:
        #     raise ValidationError('At least one doctor type must be assigned')

    @property
    def is_orphaned(self):
        """
        Check if this consultation has no doctor assignment.
        
        An orphaned consultation has no regular doctor, guest doctor, or doctor_user.
        This may indicate incomplete data entry.
        """
        return not self.doctor_id and not self.guest_doctor_id and not self.doctor_user_id

    def get_doctor_name(self):
        """
        Get the doctor's name regardless of type.
        
        Returns name from:
        1. Regular doctor if assigned
        2. Guest doctor if assigned
        3. doctor_user if assigned
        4. 'Unknown' if no doctor assigned
        """
        if self.doctor:
            return str(self.doctor)
        elif self.guest_doctor:
            return self.guest_doctor.full_name
        elif self.doctor_user:
            return self.doctor_user.get_full_name() or self.doctor_user.username
        return 'Unknown'

    def complete_consultation(self):
        """Mark consultation as completed."""
        self.status = ConsultationStatus.COMPLETED
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'completed_at'])

    def __str__(self):
        return f"Consultation #{self.consultation_id} - {self.patient} (Doctor: {self.get_doctor_name()})"


# ============================================================
# CONSULTATION TIMELINE MODEL
# ============================================================

class ConsultationTimeline(models.Model):

    entry_id = models.AutoField(primary_key=True)

    consultation = models.ForeignKey(
        Consultation,
        on_delete=models.CASCADE,
        related_name='timeline_entries'
    )

    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='consultation_timeline_entries'
    )

    event = models.CharField(
        max_length=50,
        choices=[
            ('CREATED', 'Consultation Created'),
            ('STARTED', 'Consultation Started'),
            ('LAB_REQUESTED', 'Lab Test Requested'),
            ('LAB_COMPLETED', 'Lab Results Received'),
            ('PRESCRIPTION_ISSUED', 'Prescription Issued'),
            ('COMPLETED', 'Consultation Completed'),
            ('REOPENED', 'Consultation Reopened'),
            ('NOTES_UPDATED', 'Notes Updated'),
            ('STATUS_CHANGED', 'Status Changed'),
            ('CANCELLED', 'Appointment Cancelled'),
            ('OTHER', 'Other Event'),
        ]
    )

    description = models.TextField(blank=True, null=True)

    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['consultation', 'timestamp']),
            models.Index(fields=['event']),
        ]

    def __str__(self):
        return f"{self.event} @ {self.timestamp:%Y-%m-%d %H:%M}"


# ============================================================
# PRESCRIPTION TYPE CHOICES
# ============================================================

class PrescriptionType(models.TextChoices):
    FINAL = 'FINAL', 'Final'


# ============================================================
# PRESCRIPTION MODEL
# ============================================================

class Prescription(models.Model):

    prescription_id = models.AutoField(primary_key=True)

    consultation = models.ForeignKey(
        Consultation,
        on_delete=models.CASCADE,
        related_name='prescriptions',
    )

    prescribed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='prescriptions',
    )

    prescription_type = models.CharField(
        max_length=20,
        choices=PrescriptionType.choices,
        default=PrescriptionType.FINAL,
    )

    prescription_date = models.DateField(
        default=timezone.localdate
    )

    notes = models.TextField(blank=True, null=True)

    is_sent_to_pharmacy = models.BooleanField(
        default=False,
        help_text='True when prescription is sent to pharmacy for dispensing.'
    )

    created_at = models.DateTimeField(
        default=timezone.now,
        editable=False
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-prescription_id']
        indexes = [
            models.Index(fields=['consultation']),
            models.Index(fields=['is_sent_to_pharmacy']),
        ]

    def __str__(self):
        return f"Prescription #{self.prescription_id}"


# ============================================================
# FREQUENCY CHOICES
# ============================================================

class FrequencyChoices(models.TextChoices):
    OD = 'OD', 'Once Daily'
    BD = 'BD', 'Twice Daily'
    TDS = 'TDS', 'Three Times Daily'
    QID = 'QID', 'Four Times Daily'
    HS = 'HS', 'At Bedtime'              # ✅ For clinic prescriptions
    SOS = 'SOS', 'As Needed'
    STAT = 'STAT', 'Stat (Immediately, One-Time)'

    @staticmethod
    def doses_per_day(frequency):
        """Return the number of doses per day for a given frequency."""
        doses_map = {
            'OD': 1,
            'BD': 2,
            'TDS': 3,
            'QID': 4,
            'HS': 1,                      # Bedtime = once daily
            'SOS': 1,  # Default to 1 for SOS
            'STAT': 1,  # One-time immediate dose
        }
        return doses_map.get(frequency, 1)


# ============================================================
# MEAL TIMING CHOICES
# ============================================================

class MealTimingChoices(models.TextChoices):
    NONE = '', 'No specific timing'
    BEFORE_MEALS = 'BEFORE_MEALS', 'Before Meals'
    WITH_MEALS = 'WITH_MEALS', 'With Meals'
    AFTER_MEALS = 'AFTER_MEALS', 'After Meals'


# ============================================================
# PRN REASON CHOICES
# ============================================================

class PRNReasonChoices(models.TextChoices):
    FEVER = 'FEVER', 'Fever'
    PAIN = 'PAIN', 'Pain'
    ALLERGY = 'ALLERGY', 'Allergic Reaction'
    COUGH = 'COUGH', 'Cough'              # ✅ Added for common symptoms
    NAUSEA = 'NAUSEA', 'Nausea/Vomiting'
    OTHER = 'OTHER', 'Other (specify)'


# ============================================================
# PRESCRIPTION ITEM MODEL
# ============================================================

class PrescriptionItem(models.Model):

    item_id = models.AutoField(primary_key=True)

    prescription = models.ForeignKey(
        Prescription,
        on_delete=models.CASCADE,
        related_name='items',
    )

    medicine = models.ForeignKey(
        Medicine,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='prescriptions',
    )

    medicine_name = models.CharField(
        max_length=300,
        blank=True,
        null=True,
        help_text='Snapshot of medicine name at prescription time.',
    )

    dose_quantity = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(0.01)],
        help_text='Dose per administration (e.g. 1 tablet, 5 ml).'
    )

    frequency = models.CharField(
        max_length=10,
        choices=FrequencyChoices.choices,
        default=FrequencyChoices.OD,
        help_text='Frequency of administration.',
    )

    meal_timing = models.CharField(
        max_length=12,
        choices=MealTimingChoices.choices,
        default=MealTimingChoices.NONE,
        blank=True,                        # ✅ Allow blank values
        help_text='Timing relative to meals.',
    )

    duration_days = models.PositiveIntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(1)],
        help_text='Prescription duration in days (max 30 for clinic).',
    )

    route = models.CharField(
        max_length=10,
        choices=RouteChoices.choices,
        blank=True,
        null=True,
        help_text='Administration route. Auto-populated from medicine master.',
    )

    is_route_overridden = models.BooleanField(
        default=False,
        help_text='True if doctor manually overrode the medicine\'s default route.',
    )

    quantity = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text='Total quantity to dispense. Auto-calculated or manual.',
    )

    calculated_quantity = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text='Auto-calculated quantity (Dose × Freq × Duration).',
    )

    is_manual_quantity = models.BooleanField(
        default=False,
        help_text='True if the doctor manually overrode the auto-calculated quantity.',
    )

    prn_reason = models.CharField(
        max_length=20,
        choices=PRNReasonChoices.choices,
        blank=True,
        null=True,
        help_text='Required when frequency is SOS/PRN (As Needed).',
    )

    prn_reason_other = models.CharField(
        max_length=200,
        blank=True,
        null=True,
        help_text="Free-text reason when prn_reason='OTHER'.",
    )

    max_daily_dose = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Optional ceiling for PRN/SOS items, e.g. 'Not more than 4 tablets/day'.",
    )

    instructions = models.TextField(
        blank=True,
        null=True
    )

    created_at = models.DateTimeField(
        default=timezone.now,
        editable=False
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        ordering = ['item_id']
        indexes = [
            models.Index(fields=['medicine']),
            models.Index(fields=['medicine_name']),
        ]
        constraints = [
            models.CheckConstraint(
                check=~models.Q(route=''),
                name='prescription_item_route_not_empty',
            ),
        ]

    def clean(self):
        if (
            self.duration_days is not None and
            self.duration_days > 30
        ):
            raise ValidationError({
                'duration_days': 'Maximum prescription duration is 30 days.'
            })

        if not self.route or not str(self.route).strip():
            raise ValidationError({'route': 'Route is required and cannot be empty.'})

        if self.dose_quantity is not None and self.dose_quantity <= 0:
            raise ValidationError({'dose_quantity': 'Dose must be greater than zero.'})

        if self.frequency == FrequencyChoices.SOS:
            if not self.prn_reason:
                raise ValidationError({
                    'prn_reason': 'Reason for use is required when frequency is As Needed (PRN/SOS).'
                })
            if self.prn_reason == PRNReasonChoices.OTHER and not (
                self.prn_reason_other and self.prn_reason_other.strip()
            ):
                raise ValidationError({
                    'prn_reason_other': 'Please specify the reason when "Other" is selected.'
                })

        if self.medicine_id and not self.medicine.is_active:
            raise ValidationError({
                'medicine': f'Medicine "{self.medicine.name}" is no longer active and cannot be prescribed.'
            })

        if self.frequency != FrequencyChoices.SOS:
            if self.prn_reason or self.prn_reason_other:
                raise ValidationError({
                    'prn_reason': 'PRN reason should only be set when frequency is "As Needed".'
                })

        valid_routes = [choice[0] for choice in RouteChoices.choices]
        if self.route and self.route not in valid_routes:
            raise ValidationError({
                'route': f'Invalid route. Must be one of: {", ".join(valid_routes)}'
            })

    def compute_calculated_quantity(self):
        if self.dose_quantity is None:
            return None

        # STAT = a single one-time immediate dose. It isn't repeated daily,
        # so the quantity is just the dose itself, not dose × days.
        if self.frequency == FrequencyChoices.STAT:
            raw = Decimal(self.dose_quantity)
            if raw <= 0:
                return None
            return int(raw.to_integral_value(rounding=ROUND_CEILING))

        if self.duration_days is None:
            return None

        doses_per_day = FrequencyChoices.doses_per_day(self.frequency)
        if doses_per_day is None:
            doses_per_day = 1

        raw = Decimal(self.dose_quantity) * doses_per_day * self.duration_days
        if raw <= 0:
            return None

        whole = raw.to_integral_value(rounding=ROUND_CEILING)
        return int(whole)

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        medicine_changed = False

        if not is_new and self.pk:
            previous = (
                PrescriptionItem.objects
                .filter(pk=self.pk)
                .values_list('medicine_id', flat=True)
                .first()
            )
            medicine_changed = previous is not None and previous != self.medicine_id

        if self.medicine and not self.medicine_name:
            self.medicine_name = self.medicine.name

        if (is_new or medicine_changed) and not self.is_route_overridden and self.medicine_id:
            self.route = self.medicine.default_route

        if not self.route:
            self.route = self.medicine.default_route if self.medicine_id else RouteChoices.ORAL

        self.calculated_quantity = self.compute_calculated_quantity()
        if not self.is_manual_quantity and self.calculated_quantity is not None:
            self.quantity = self.calculated_quantity

        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def display_name(self):
        return self.medicine_name or (self.medicine.name if self.medicine else "")

    @property
    def display_schedule(self):
        frequency_str = self.get_frequency_display()

        if self.meal_timing and self.meal_timing != MealTimingChoices.NONE:
            meal_timing_str = self.get_meal_timing_display()
            return f"{frequency_str} · {meal_timing_str}"

        return frequency_str

    @property
    def quantity_source(self):
        return 'MANUAL' if self.is_manual_quantity else 'AUTO'

    def __str__(self):
        return self.display_name


# ============================================================
# FOLLOW-UP REMINDER MODEL
# ============================================================

class FollowUpReminder(models.Model):
    reminder_id = models.AutoField(primary_key=True)
    patient = models.ForeignKey('reception.Patient', on_delete=models.CASCADE, related_name='followup_reminders')
    consultation = models.ForeignKey('doctor.Consultation', on_delete=models.CASCADE, related_name='followup_reminders')
    followup_date = models.DateField(db_index=True)
    status = models.CharField(
        max_length=20,
        default='PENDING',
        choices=[
            ('PENDING', 'Pending'),
            ('CALLED', 'Called'),
            ('BOOKED', 'Booked'),
            ('DECLINED', 'Declined'),
            ('NO_ANSWER', 'No Answer')
        ]
    )
    reception_notes = models.TextField(blank=True, null=True)
    contacted_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['followup_date', 'reminder_id']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['followup_date']),
        ]

    def __str__(self):
        return f"Reminder #{self.reminder_id} - {self.patient}"


# ============================================================
# END OF FILE
# ============================================================
# ✅ ALL PATCHES APPLIED
# ✅ Guest Doctor Support Integrated
# ✅ PYTHON SYNTAX VALIDATED
# ✅ READY FOR PRODUCTION
# ============================================================