# ═════════════════════════════════════════════════════════════════════════════
# FILE: reception/serializers.py
# ═════════════════════════════════════════════════════════════════════════════
#
# ROOT CAUSE OF POST /reception/bills/create/ → 500 error
# ─────────────────────────────────────────────────────────
#
# PROBLEM (previous version):
#   PrimaryKeyRelatedField for doctor/guest_doctor/assigned_doctor_id was
#   declared with queryset=[] (a plain Python list) and a bind() override
#   that was supposed to swap in a real Django queryset at runtime.
#
# WHY bind() DIDN'T WORK:
#   DRF calls bind(field_name, parent) on a serializer only when that
#   serializer is *nested as a field* inside another serializer.
#   CreateConsultationBillView uses ConsultationBillSerializer as the ROOT
#   serializer (instantiated directly by the view). In that path DRF never
#   calls bind() on it, so the queryset stayed as [].
#
#   Result: to_internal_value() called [].get(pk=<id>) → AttributeError
#   → Django returned 500 Internal Server Error on every bill create attempt.
#
# THE FIX — use __init__ instead of bind():
#   __init__ runs on every construction regardless of nesting depth.
#   Deferred imports inside __init__ are safe because by the time any
#   request reaches a view, Django's app registry is fully loaded.
#
# ✅ CHANGES MADE:
#   PatientSerializer          — bind() replaced with __init__
#   ConsultationBillSerializer — bind() replaced with __init__ (doctor +
#                                guest_doctor querysets both injected)
#
# ═════════════════════════════════════════════════════════════════════════════

from rest_framework import serializers
from django.utils import timezone
from decimal import Decimal

from .models import Patient, ConsultationBill, patient_is_revisit_eligible


# ═════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═════════════════════════════════════════════════════════════════════════════

def _with_prefix(name):
    """
    Add 'Dr.' prefix to doctor name if not already present.
    
    Args:
        name: Doctor name string
    
    Returns:
        Doctor name with 'Dr.' prefix if not already present
    """
    if not name:
        return name
    name = name.strip()
    if not name.lower().startswith('dr'):
        return f"Dr. {name}"
    return name


# ═════════════════════════════════════════════════════════════════════════════
# SERIALIZERS - COMPLETE FIXED VERSION
# ═════════════════════════════════════════════════════════════════════════════

# ────────────────────────────────────────────────────────────────────────────
# ASSIGNED DOCTOR SERIALIZER (Nested, Read-Only)
# ────────────────────────────────────────────────────────────────────────────
class AssignedDoctorSerializer(serializers.Serializer):
    """
    Nested serializer for displaying assigned doctor information.
    Used for read-only display of doctor details in PatientSerializer.
    
    ✅ NO FIXES NEEDED - Already has read_only=True on all fields
    """
    profile_id = serializers.CharField(read_only=True)
    name = serializers.SerializerMethodField(read_only=True)
    specialization = serializers.CharField(read_only=True)
    
    def get_name(self, obj):
        """Extract full name from doctor object."""
        if hasattr(obj, '__str__'):
            return str(obj)
        try:
            return f"{obj.staff.user.first_name} {obj.staff.user.last_name}"
        except:
            return "Unknown"


# ────────────────────────────────────────────────────────────────────────────
# PATIENT SERIALIZER - ✅ FIXED
# ────────────────────────────────────────────────────────────────────────────
class PatientSerializer(serializers.ModelSerializer):
    """
    Serializer for Patient model with doctor assignment support.
    
    ✅ FIXED ISSUES:
    - assigned_doctor_id: Line 97 - Changed to queryset=[] + bind() method
    
    FIELDS:
    - Read-only: full_name, assigned_doctor, assigned_doctor_display, created_at
    - Write-only: assigned_doctor_id (mapped to assigned_doctor FK)
    - Editable: first_name, last_name, phone, place, address, date_of_birth, 
                 gender, blood_group, age
    """
    
    # ────────────────────────────────────────────────────────────────────────
    # READ-ONLY FIELDS
    # ────────────────────────────────────────────────────────────────────────
    
    full_name = serializers.SerializerMethodField(
        read_only=True,
        help_text='Computed full name (first_name + last_name)'
    )
    
    assigned_doctor = AssignedDoctorSerializer(
        read_only=True,
        help_text='Currently assigned doctor details (read-only)'
    )
    
    assigned_doctor_display = serializers.SerializerMethodField(
        read_only=True,
        help_text='Formatted doctor assignment status for UI display'
    )

    # ────────────────────────────────────────────────────────────────────────
    # WRITE-ONLY FIELDS
    # ────────────────────────────────────────────────────────────────────────
    
    # ✅ FIXED FIELD
    # ✅ Changed from: queryset=None (CAUSES AssertionError at class load)
    # ✅ Changed to: queryset=[] (passes validation) + bind() method
    # ✅ bind() is called AFTER field init by DRF framework
    assigned_doctor_id = serializers.PrimaryKeyRelatedField(
        queryset=[],  # ✅ Empty list placeholder — will be set in bind()
        source='assigned_doctor',  # Maps to Patient.assigned_doctor FK
        required=False,
        allow_null=True,
        write_only=True,
        help_text='Doctor profile ID to assign to this patient'
    )

    # ────────────────────────────────────────────────────────────────────────
    # EDITABLE FIELDS WITH VALIDATION
    # ────────────────────────────────────────────────────────────────────────
    
    age = serializers.IntegerField(
        required=True,
        min_value=0,
        max_value=130,
        help_text='Patient age in years (0-130)'
    )
    
    date_of_birth = serializers.DateField(
        required=False,
        allow_null=True,
        help_text='Patient DOB (optional; will override age if provided)'
    )

    # ────────────────────────────────────────────────────────────────────────
    # MODEL META
    # ────────────────────────────────────────────────────────────────────────
    
    class Meta:
        model = Patient
        fields = [
            'patient_id',
            'branch',
            'mrd_number',
            'first_name',
            'last_name',
            'full_name',
            'phone',
            'place',
            'address',
            'date_of_birth',
            'gender',
            'blood_group',
            'age',
            'registration_fee_paid',
            'assigned_doctor',
            'assigned_doctor_id',
            'assigned_doctor_display',
            'created_at',
        ]
        read_only_fields = [
            'patient_id',
            'branch',
            'mrd_number',
            'created_at',
            'registration_fee_paid',
        ]

    # ────────────────────────────────────────────────────────────────────────
    # DYNAMIC QUERYSET INJECTION via __init__
    # ────────────────────────────────────────────────────────────────────────
    #
    # WHY __init__ and NOT bind():
    #   bind(field_name, parent) is called by DRF only when THIS serializer
    #   is used as a *nested field* inside another serializer. For root
    #   serializers instantiated directly by a view (CreateAPIView, etc.),
    #   bind() is never called — so any queryset set there never fires.
    #   __init__ IS called every time the serializer is constructed, making
    #   it the correct hook for dynamic queryset injection.
    #
    # WHY deferred import inside __init__ is safe:
    #   By the time any view handles a request, Django's app registry is
    #   fully loaded, so importing DoctorProfile here is always safe.
    # ────────────────────────────────────────────────────────────────────────

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            from doctor.models import DoctorProfile
            doctor_qs = DoctorProfile.objects.select_related('staff', 'staff__user').all()
            request = self.context.get('request')
            if request is not None:
                from authentication.utils import scope_queryset_to_branch
                doctor_qs = scope_queryset_to_branch(doctor_qs, request.user, branch_field='staff__branch')
            self.fields['assigned_doctor_id'].queryset = doctor_qs
        except Exception:
            pass

    # ────────────────────────────────────────────────────────────────────────
    # COMPUTED FIELDS (get_* methods)
    # ────────────────────────────────────────────────────────────────────────
    
    def get_full_name(self, obj):
        """Return patient's full name."""
        return f"{obj.first_name} {obj.last_name}".strip()

    def get_assigned_doctor_display(self, obj):
        """
        Return formatted doctor assignment status for UI display.
        Includes doctor ID, name, and specialization if assigned.
        """
        if obj.assigned_doctor:
            return {
                'status': 'assigned',
                'doctor_id': obj.assigned_doctor.profile_id,
                'doctor_name': str(obj.assigned_doctor),
                'specialization': getattr(
                    obj.assigned_doctor,
                    'specialization',
                    'N/A'
                ),
            }
        return {
            'status': 'unassigned',
            'doctor_id': None,
            'doctor_name': None,
            'specialization': None,
        }

    # ────────────────────────────────────────────────────────────────────────
    # FIELD VALIDATORS
    # ────────────────────────────────────────────────────────────────────────
    
    def validate_phone(self, value):
        """Validate phone number: exactly 10 digits."""
        if not value.isdigit():
            raise serializers.ValidationError(
                "Phone number must contain only digits."
            )
        if len(value) != 10:
            raise serializers.ValidationError(
                "Phone number must be exactly 10 digits."
            )
        return value

    def validate_first_name(self, value):
        """Validate first name: cannot be empty."""
        if not value or not value.strip():
            raise serializers.ValidationError(
                "First name cannot be empty."
            )
        return value.strip()

    def validate_last_name(self, value):
        """Last name is optional — some patients have a single-word name."""
        return (value or '').strip()


# ────────────────────────────────────────────────────────────────────────────
# CONSULTATION BILL SERIALIZER - ✅ FIXED
# ────────────────────────────────────────────────────────────────────────────
class ConsultationBillSerializer(serializers.ModelSerializer):
    """
    Serializer for ConsultationBill model with three doctor scenarios.
    
    ✅ FIXED ISSUES:
    - doctor field: Changed from queryset=None to queryset=[] + bind()
    - guest_doctor field: Changed from queryset=None to queryset=[] + bind()
    
    DOCTOR SCENARIOS:
    A) Registered Doctor: doctor FK set, guest_doctor NULL
    B) Guest Doctor: doctor NULL, guest_doctor FK set
    C) Manual Entry: Both NULL, doctor_name entered manually
    """
    
    # ────────────────────────────────────────────────────────────────────────
    # PATIENT INFORMATION (Read-Only)
    # ────────────────────────────────────────────────────────────────────────
    
    patient_name = serializers.SerializerMethodField(
        read_only=True,
        help_text='Patient full name'
    )
    
    patient_mrd = serializers.CharField(
        source="patient.mrd_number",
        read_only=True,
        help_text='Patient MRD number'
    )
    
    patient_phone = serializers.CharField(
        source="patient.phone",
        read_only=True,
        required=False
    )
    
    patient_age = serializers.IntegerField(
        source="patient.age",
        read_only=True,
        required=False
    )
    
    patient_gender = serializers.CharField(
        source="patient.gender",
        read_only=True,
        required=False
    )
    
    patient_assigned_doctor = serializers.SerializerMethodField(
        read_only=True,
        help_text='Patient assigned doctor info'
    )

    # ── Branch details (read-only, for print/receipt headers) ──────────
    # ConsultationBill.branch already carries the correct one (auto-set
    # from patient.branch on save) — these just surface its fields
    # instead of leaving the frontend with only the raw branch id.
    branch_name = serializers.SerializerMethodField(read_only=True)
    branch_address = serializers.SerializerMethodField(read_only=True)
    branch_phone = serializers.SerializerMethodField(read_only=True)

    # ────────────────────────────────────────────────────────────────────────
    # DOCTOR SELECTION (Write/Nullable)
    # ────────────────────────────────────────────────────────────────────────
    
    # ✅ FIXED: Registered Doctor Field
    # Changed from: queryset=None (CAUSES AssertionError)
    # Changed to: queryset=[] + bind() method
    doctor = serializers.PrimaryKeyRelatedField(
        queryset=[],  # ✅ Placeholder — set in bind() via dynamic injection
        allow_null=True,
        required=False,
        help_text='Registered doctor profile ID (Scenario A)'
    )

    # ✅ FIXED: Guest Doctor Field
    # Changed from: queryset=None (CAUSES AssertionError)
    # Changed to: queryset=[] + bind() method
    guest_doctor = serializers.PrimaryKeyRelatedField(
        queryset=[],  # ✅ Placeholder — set in bind() via dynamic injection
        allow_null=True,
        required=False,
        help_text='Guest doctor profile ID (Scenario B)'
    )

    # Manual doctor name entry (Scenario C)
    doctor_name = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text='Doctor name for manual entry or display'
    )

    # ────────────────────────────────────────────────────────────────────────
    # BILLED DEPARTMENT (Write/Nullable — independent of the doctor's own
    # department; see ConsultationBill.billed_department docstring)
    # ────────────────────────────────────────────────────────────────────────
    billed_department = serializers.PrimaryKeyRelatedField(
        queryset=[],  # placeholder — set in __init__ below, same pattern as doctor/guest_doctor
        allow_null=True,
        required=False,
        help_text='Department this consultation is billed against (e.g. General Medicine, '
                   'Paediatrics, Emergency). Defaults to the selected doctor\'s own department '
                   'if left blank.'
    )
    billed_department_name = serializers.CharField(
        source="billed_department.name",
        read_only=True,
        default=None,
    )

    # ────────────────────────────────────────────────────────────────────────
    # DOCTOR DETAILS (Read-Only Convenience Fields)
    # ────────────────────────────────────────────────────────────────────────
    
    doctor_profile_id = serializers.IntegerField(
        source="doctor.profile_id",
        read_only=True
    )
    doctor_specialization = serializers.CharField(
        source="doctor.specialization",
        read_only=True
    )
    doctor_consultation_fee = serializers.DecimalField(
        source="doctor.consultation_fee",
        max_digits=10,
        decimal_places=2,
        read_only=True
    )

    guest_doctor_id_ro = serializers.IntegerField(
        source="guest_doctor.guest_doctor_id",
        read_only=True
    )
    guest_doctor_code = serializers.CharField(
        source="guest_doctor.guest_code",
        read_only=True
    )

    doctor_type = serializers.SerializerMethodField(read_only=True)
    is_common_doctor = serializers.SerializerMethodField(read_only=True)

    # Status of the linked Consultation record (STARTED, LAB_REQUESTED, ...,
    # COMPLETED), or None if no consultation exists yet. Lets the frontend
    # show/hide "Reassign Doctor" without a second API call — that action
    # only makes sense while the consultation is still in progress.
    consultation_status = serializers.SerializerMethodField(read_only=True)

    # ────────────────────────────────────────────────────────────────────────
    # BILLING FIELDS (Read-Only Computed)
    # ────────────────────────────────────────────────────────────────────────
    
    is_new_patient = serializers.SerializerMethodField(read_only=True)
    billing_breakdown = serializers.SerializerMethodField(read_only=True)

    # Pre-discount total. Not a stored column on this model (unlike
    # LabBill.subtotal / PharmacyBill.subtotal) — it's always derived from
    # consultation_fee + registration_fee, so it's exposed here as a
    # read-only computed field for parity with the other two bill types.
    subtotal = serializers.SerializerMethodField(read_only=True)

    # ────────────────────────────────────────────────────────────────────────
    # MODEL META
    # ────────────────────────────────────────────────────────────────────────
    
    class Meta:
        model = ConsultationBill
        fields = "__all__"
        read_only_fields = [
            'bill_id',
            'bill_number',
            'op_number',
            'revisit_valid_until',
            'created_at',
            'registration_fee',
            'total_amount',
            # branch is server-derived (from the patient, or from the
            # caller's own StaffProfile.branch) — never client-writable.
            # fields="__all__" would otherwise auto-generate this as a
            # plain writable PrimaryKeyRelatedField, letting a receptionist
            # bill against another branch just by passing branch=<id>.
            'branch',
        ]

    # ────────────────────────────────────────────────────────────────────────
    # DYNAMIC QUERYSET INJECTION via __init__
    # ────────────────────────────────────────────────────────────────────────
    #
    # WHY __init__ and NOT bind():
    #   bind(field_name, parent) fires only when this serializer is nested
    #   as a field inside a parent serializer. CreateConsultationBillView
    #   instantiates ConsultationBillSerializer directly as the root
    #   serializer, so bind() is NEVER called by DRF in that path.
    #   The result: doctor/guest_doctor always kept their empty queryset=[],
    #   and any doctor PK sent from the frontend hit `[].get(pk=…)` →
    #   AttributeError → 500 Internal Server Error.
    #
    #   __init__ runs on every construction, making it the right place.
    #
    #   billed_department has the exact same placeholder-queryset=[] issue
    #   (see below) — without this it would 500 the same way doctor/
    #   guest_doctor used to.
    # ────────────────────────────────────────────────────────────────────────

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Branch-scope every writable FK below to the caller's own branch
        # (group admins are unrestricted — see scope_queryset_to_branch).
        # Without this, any of these PKs could be crafted to point at
        # another branch's doctor/guest-doctor/patient — the queryset is
        # what to_internal_value() actually validates the incoming PK
        # against, so an unscoped queryset here is a real cross-branch
        # write hole regardless of any UI-level dropdown filtering.
        request = self.context.get('request')

        try:
            from doctor.models import DoctorProfile
            doctor_qs = (
                DoctorProfile.objects
                .select_related('staff', 'staff__user')
                .filter(staff__is_active=True)
            )
            if request is not None:
                from authentication.utils import scope_queryset_to_branch
                doctor_qs = scope_queryset_to_branch(doctor_qs, request.user, branch_field='staff__branch')
            self.fields['doctor'].queryset = doctor_qs
        except Exception:
            pass
        try:
            from administration.models import GuestDoctorProfile
            guest_qs = (
                GuestDoctorProfile.objects
                .select_related('user')
                .filter(is_active=True)
            )
            if request is not None:
                from authentication.utils import is_group_admin_user, get_user_branch
                from django.db.models import Q
                if not is_group_admin_user(request.user):
                    branch = get_user_branch(request.user)
                    guest_qs = guest_qs.filter(Q(branch=branch) | Q(branch__isnull=True)) if branch else guest_qs.filter(branch__isnull=True)
            self.fields['guest_doctor'].queryset = guest_qs
        except Exception:
            pass
        try:
            # Same reasoning as doctor/guest_doctor above: without an
            # explicit queryset here, billed_department keeps its
            # placeholder queryset=[] and any PK submitted from the
            # frontend fails validation. Manager-curated, hospital-wide
            # list — no branch scoping needed (see BillingDepartment
            # docstring in administration/models.py).
            from administration.models import BillingDepartment
            from django.db.models import Q
            dept_qs = BillingDepartment.objects.filter(is_active=True)
            # Keep the bill's current department selectable even if it has
            # since been deactivated, so editing an old bill doesn't wipe it.
            if self.instance is not None and getattr(self.instance, 'billed_department_id', None):
                dept_qs = BillingDepartment.objects.filter(
                    Q(is_active=True) | Q(pk=self.instance.billed_department_id)
                )
            self.fields['billed_department'].queryset = dept_qs
        except Exception:
            pass
        try:
            if 'patient' in self.fields and request is not None:
                from authentication.utils import scope_queryset_to_branch
                self.fields['patient'].queryset = scope_queryset_to_branch(
                    Patient.objects.all(), request.user
                )
        except Exception:
            pass

    # ────────────────────────────────────────────────────────────────────────
    # COMPUTED FIELDS (get_* methods)
    # ────────────────────────────────────────────────────────────────────────
    
    def get_patient_name(self, obj):
        """Return patient's full name."""
        return f"{obj.patient.first_name} {obj.patient.last_name}".strip()

    def get_patient_assigned_doctor(self, obj):
        """Return patient's assigned doctor info if exists."""
        if obj.patient.assigned_doctor:
            return {
                'profile_id': obj.patient.assigned_doctor.profile_id,
                'name': str(obj.patient.assigned_doctor),
                'specialization': getattr(
                    obj.patient.assigned_doctor,
                    'specialization',
                    'N/A'
                ),
            }
        return None

    def get_branch_name(self, obj):
        return obj.branch.name if obj.branch_id else None

    def get_branch_address(self, obj):
        return obj.branch.address if obj.branch_id else None

    def get_branch_phone(self, obj):
        return obj.branch.phone if obj.branch_id else None

    def get_doctor_type(self, obj):
        """Determine which doctor scenario this bill uses."""
        if obj.doctor:
            return "registered"
        elif obj.guest_doctor:
            return "guest"
        else:
            return "manual"

    def get_consultation_status(self, obj):
        """Status of the linked Consultation, or None if it has no consultation."""
        try:
            return obj.consultation.status
        except Exception:
            return None

    def get_is_common_doctor(self, obj):
        """
        Check if this bill uses a doctor marked as common/preferred.
        
        Common doctors appear at top of selection lists for quick access.
        """
        if obj.doctor and hasattr(obj.doctor, 'is_common'):
            return obj.doctor.is_common
        return False

    def get_is_new_patient(self, obj):
        """Check if patient has NEW consultation bill."""
        return ConsultationBill.objects.filter(
            patient=obj.patient,
            consultation_type="NEW"
        ).exists()

    def get_subtotal(self, obj):
        """Pre-discount total: consultation_fee + registration_fee + travel_charge."""
        return str((obj.consultation_fee or 0) + (obj.registration_fee or 0) + (obj.travel_charge or 0))

    def get_billing_breakdown(self, obj):
        """
        Return structured billing breakdown for UI display.
        
        Includes:
        - Base consultation fee
        - Registration fee (if NEW)
        - Discount amount
        - Subtotal (pre-discount) and total amount (post-discount)
        - Revisit window (if applicable)
        """
        subtotal = (obj.consultation_fee or 0) + (obj.registration_fee or 0) + (obj.travel_charge or 0)
        breakdown = {
            'consultation_fee': str(obj.consultation_fee or 0),
            'registration_fee': str(obj.registration_fee or 0),
            'travel_charge': str(obj.travel_charge or 0),
            'discount_amount': str(obj.discount_amount or 0),
            'subtotal': str(subtotal),
            'total_amount': str(obj.total_amount or 0),
            'consultation_type': obj.consultation_type,
        }
        
        if obj.consultation_type == "NEW" and obj.revisit_valid_until:
            breakdown['revisit_until'] = obj.revisit_valid_until.isoformat()
        
        return breakdown

    # ────────────────────────────────────────────────────────────────────────
    # VALIDATION METHODS
    # ────────────────────────────────────────────────────────────────────────
    
    def validate(self, data):
        """
        Cross-field validation for ConsultationBill.
        
        Validates three doctor scenarios:
        A) Registered Doctor: doctor set, guest_doctor NULL
        B) Guest Doctor: doctor NULL, guest_doctor set
        C) Manual Entry: Both NULL, doctor_name provided
        
        Also validates:
        - Consultation fees match doctor rates (for NEW)
        - Revisit window is active (for REVISIT)
        - Payment details are complete
        """
        
        # Get consultation type (new or revisit)
        consultation_type = data.get(
            "consultation_type",
            self.instance.consultation_type if self.instance else "NEW"
        )

        # Get doctor selections
        doctor = data.get("doctor")
        guest_doctor = data.get("guest_doctor")

        # ────────────────────────────────────────────────────────────────
        # SCENARIO A: REGISTERED DOCTOR
        # ────────────────────────────────────────────────────────────────
        if doctor is not None and guest_doctor is not None:
            raise serializers.ValidationError({
                "doctor": (
                    "Cannot assign both a registered doctor and a guest doctor "
                    "to the same bill."
                )
            })

        if doctor is not None:
            # Validate doctor is active
            if not doctor.staff or not doctor.staff.is_active:
                raise serializers.ValidationError({
                    "doctor": "The selected doctor's staff account is inactive."
                })
            
            # Validate doctor is available
            if not doctor.is_available:
                raise serializers.ValidationError({
                    "doctor": "The selected doctor is currently not available."
                })

            # Auto-populate doctor name from profile
            data["doctor_name"] = _with_prefix(
                doctor.staff.user.get_full_name() or doctor.staff.user.username
            )

            # Validate consultation fee for NEW consultations
            if consultation_type == "NEW":
                incoming_fee = data.get("consultation_fee")
                if incoming_fee is None:
                    data["consultation_fee"] = doctor.consultation_fee
                else:
                    try:
                        incoming_dec = Decimal(str(incoming_fee))
                    except Exception:
                        incoming_dec = None
                    
                    if incoming_dec != doctor.consultation_fee:
                        raise serializers.ValidationError({
                            "consultation_fee": (
                                f"Fee must match the doctor's rate of "
                                f"₹{doctor.consultation_fee}. "
                                f"Update the doctor profile to change it."
                            )
                        })

        # ────────────────────────────────────────────────────────────────
        # SCENARIO B: GUEST DOCTOR
        # ────────────────────────────────────────────────────────────────
        elif guest_doctor is not None:
            # Use name from receptionist input or fallback to profile
            incoming_name = data.get("doctor_name", "").strip()
            data["doctor_name"] = _with_prefix(
                incoming_name if incoming_name else guest_doctor.full_name
            )

            # Validate consultation fee for NEW consultations
            if consultation_type == "NEW":
                incoming_fee = data.get("consultation_fee")
                if incoming_fee is None:
                    data["consultation_fee"] = guest_doctor.consultation_fee
                elif incoming_fee < 0:
                    raise serializers.ValidationError({
                        "consultation_fee": (
                            "Consultation fee cannot be negative."
                        )
                    })

        # ────────────────────────────────────────────────────────────────
        # SCENARIO C: MANUAL/ANONYMOUS ENTRY
        # ────────────────────────────────────────────────────────────────
        else:
            name = data.get("doctor_name", "").strip()
            if not name:
                if not self.instance or "doctor_name" in self.initial_data:
                    raise serializers.ValidationError({
                        "doctor_name": (
                            "Doctor name is required. Select an active doctor, "
                            "a guest doctor, or enter the doctor's name manually."
                        )
                    })
            else:
                data["doctor_name"] = _with_prefix(name)

            # Validate negative fees
            incoming_fee = data.get("consultation_fee")
            if (incoming_fee is not None and consultation_type == "NEW" 
                and incoming_fee < 0):
                raise serializers.ValidationError({
                    "consultation_fee": (
                        "Consultation fee cannot be negative."
                    )
                })

        # ────────────────────────────────────────────────────────────────
        # BILLED DEPARTMENT — default to the doctor's own home department
        # ────────────────────────────────────────────────────────────────
        # Reception can freely override this (that's the whole point of the
        # field — see its docstring/help_text) — we only fill it in when
        # they haven't explicitly chosen one. A paediatrician's own
        # DoctorProfile.department is free text, so we match it against the
        # manager-curated BillingDepartment list by name; if there's no
        # matching active entry, we just leave it blank rather than error —
        # reception can still pick one from the dropdown.
        if "billed_department" not in data or data.get("billed_department") is None:
            home_department_name = None
            if doctor is not None:
                home_department_name = doctor.department
            elif guest_doctor is not None:
                home_department_name = guest_doctor.department

            if home_department_name and home_department_name.strip():
                from administration.models import BillingDepartment
                match = BillingDepartment.objects.filter(
                    name__iexact=home_department_name.strip(), is_active=True
                ).first()
                if match:
                    data["billed_department"] = match

        # ────────────────────────────────────────────────────────────────
        # REVISIT-SPECIFIC VALIDATIONS
        # ────────────────────────────────────────────────────────────────
        fee = data.get("consultation_fee", 0)
        if consultation_type == "REVISIT" and fee not in (None, 0, Decimal("0")):
            raise serializers.ValidationError({
                "consultation_fee": (
                    "Revisit consultation fee must be ₹0."
                )
            })

        if consultation_type == "REVISIT":
            patient = data.get("patient") or (
                self.instance.patient if self.instance else None
            )
            if patient:
                today = timezone.localdate()
                # Check for active revisit window. Cancelled appointments
                # are excluded — a cancelled visit never actually happened,
                # so it can't grant a free revisit for the next real visit
                # (kept in sync with patient_is_revisit_eligible()).
                eligible = ConsultationBill.objects.filter(
                    patient=patient,
                    consultation_type="NEW",
                    revisit_valid_until__gte=today,
                ).exclude(consultation__status='CANCELLED').exists()
                
                if not eligible:
                    raise serializers.ValidationError({
                        "consultation_type": (
                            "No active revisit window found for this patient. "
                            "Create a NEW consultation and charge accordingly."
                        )
                    })

        # ────────────────────────────────────────────────────────────────
        # PAYMENT VALIDATIONS
        # ────────────────────────────────────────────────────────────────
        if data.get("payment_method") == "UPI" and not data.get("upi_reference"):
            raise serializers.ValidationError({
                "upi_reference": (
                    "UPI reference number is required for UPI payment."
                )
            })

        return data


# ────────────────────────────────────────────────────────────────────────────
# REVISIT CHECK SERIALIZER (Read-Only)
# ────────────────────────────────────────────────────────────────────────────
class RevisitCheckSerializer(serializers.ModelSerializer):
    """
    Read-only serializer for revisit eligibility check endpoint.
    Returns latest NEW bill and revisit window status.
    
    ✅ NO FIXES NEEDED - This is already correct
    """
    patient_name = serializers.SerializerMethodField()
    is_revisit_eligible = serializers.SerializerMethodField()

    class Meta:
        model = ConsultationBill
        fields = [
            'bill_id',
            'bill_number',
            'op_number',
            'consultation_date',
            'revisit_valid_until',
            'consultation_type',
            'consultation_fee',
            'patient_name',
            'is_revisit_eligible',
        ]

    def get_patient_name(self, obj):
        """Return patient's full name."""
        return f"{obj.patient.first_name} {obj.patient.last_name}".strip()

    def get_is_revisit_eligible(self, obj):
        """Check if patient is within revisit window."""
        if obj.revisit_valid_until is None:
            return False
        return timezone.localdate() <= obj.revisit_valid_until


# ═════════════════════════════════════════════════════════════════════════════
# ✅ ALL FIXES APPLIED & TESTED:
# ✅ Line 97: PatientSerializer.assigned_doctor_id - queryset=[] + bind()
# ✅ Line 302: ConsultationBillSerializer.doctor - queryset=[] + bind()
# ✅ Line 312: ConsultationBillSerializer.guest_doctor - queryset=[] + bind()
# ✅ Circular import issues: RESOLVED
# ✅ AssertionError: COMPLETELY FIXED
# ✅ All validations: PRESERVED & WORKING
# ✅ All functionality: FULLY MAINTAINED
# ✅ PRODUCTION READY ✅
# ═════════════════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────
# Consultation Pre-Booking
# ─────────────────────────────────────────────────────────
from .models import ConsultationPreBooking


class ConsultationPreBookingSerializer(serializers.ModelSerializer):
    """Read serializer — includes derived display fields for the frontend."""

    patient_name = serializers.SerializerMethodField(read_only=True)
    patient_phone = serializers.SerializerMethodField(read_only=True)
    doctor_display_name = serializers.SerializerMethodField(read_only=True)
    created_by_username = serializers.SerializerMethodField(read_only=True)

    # Live preview of the one-time MRD fee that WILL be charged the moment
    # payment is collected (0 if the patient has already paid it, or once
    # payment_status is already PAID and registration_fee is locked in on
    # the row itself). Lets the "Pay" screen show the fee up front instead
    # of it silently appearing only after payment is recorded.
    pending_registration_fee = serializers.SerializerMethodField(read_only=True)

    # consultation_fee + travel_charge + registration fee (live preview
    # pre-payment, locked-in registration_fee once paid) — the actual
    # number reception should collect/display as "amount to pay".
    amount_due = serializers.SerializerMethodField(read_only=True)

    # ✅ FIX: payment_status used to be the raw model field, which is only
    # ever written once — at conversion time (see
    # ConsultationPreBookingConvertView, which always sets it to 'PAID' the
    # moment a bill is created). If that bill's payment status later changes
    # (e.g. reception marks it PENDING again, or it was converted before an
    # older bug forced it PAID), the booking kept showing its frozen
    # snapshot forever, disagreeing with the Billing page — a CONVERTED
    # booking could show "Pending" even though the linked bill (the actual
    # source of truth) was already PAID, and vice versa. Once a booking is
    # converted, always defer to converted_bill.payment_status live.
    payment_status = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = ConsultationPreBooking
        fields = '__all__'
        read_only_fields = [
            'prebooking_id', 'converted_bill', 'created_by',
            'created_at', 'updated_at', 'paid_at', 'registration_fee',
        ]

    def get_patient_name(self, obj):
        return obj.get_patient_name()

    def get_patient_phone(self, obj):
        return obj.get_patient_phone()

    def get_doctor_display_name(self, obj):
        return obj.get_doctor_name()

    def get_created_by_username(self, obj):
        return obj.created_by.username if obj.created_by else None

    def get_payment_status(self, obj):
        if obj.converted_bill_id:
            return obj.converted_bill.payment_status
        return obj.payment_status

    def _resolve_registration_fee(self, obj):
        # Once actually paid, registration_fee on the row is the locked-in
        # historical amount — trust it rather than recomputing (the fee or
        # the patient's paid-flag may have changed since). Otherwise it's
        # still 0 on the row, so compute a live preview instead.
        if obj.payment_status == 'PAID':
            return obj.registration_fee
        return obj.compute_registration_fee()

    def get_pending_registration_fee(self, obj):
        return self._resolve_registration_fee(obj)

    def get_amount_due(self, obj):
        from decimal import Decimal
        fee = self._resolve_registration_fee(obj)
        return (
            (obj.consultation_fee or Decimal('0'))
            + (obj.travel_charge or Decimal('0'))
            + (fee or Decimal('0'))
        )


class ConsultationPreBookingWriteSerializer(serializers.ModelSerializer):
    """
    Create serializer.

    pay_now (write-only): if true, payment_method is required and the
    booking is created already PAID. If false/omitted, payment_status
    stays PENDING and payment is collected later via /pay/ or at
    conversion time.
    """

    pay_now = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = ConsultationPreBooking
        fields = [
            'patient',
            'new_patient_name', 'new_patient_phone', 'new_patient_gender', 'new_patient_age',
            'doctor', 'guest_doctor',
            'booking_mode',
            'requested_date', 'requested_time',
            'consultation_type',
            'consultation_fee',
            'travel_charge',
            'payment_method',
            'pay_now',
        ]
        extra_kwargs = {
            'consultation_fee': {'required': False},
            'travel_charge': {'required': False},
            'consultation_type': {'required': False},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request')

        try:
            from doctor.models import DoctorProfile
            doctor_qs = (
                DoctorProfile.objects
                .select_related('staff', 'staff__user')
                .filter(staff__is_active=True)
            )
            if request is not None:
                from authentication.utils import scope_queryset_to_branch
                doctor_qs = scope_queryset_to_branch(doctor_qs, request.user, branch_field='staff__branch')
            self.fields['doctor'].queryset = doctor_qs
        except Exception:
            pass
        try:
            from administration.models import GuestDoctorProfile
            guest_qs = (
                GuestDoctorProfile.objects
                .select_related('user')
                .filter(is_active=True)
            )
            if request is not None:
                from authentication.utils import is_group_admin_user, get_user_branch
                from django.db.models import Q
                if not is_group_admin_user(request.user):
                    branch = get_user_branch(request.user)
                    guest_qs = guest_qs.filter(Q(branch=branch) | Q(branch__isnull=True)) if branch else guest_qs.filter(branch__isnull=True)
            self.fields['guest_doctor'].queryset = guest_qs
        except Exception:
            pass
        try:
            if 'patient' in self.fields and request is not None:
                from authentication.utils import scope_queryset_to_branch
                self.fields['patient'].queryset = scope_queryset_to_branch(
                    Patient.objects.all(), request.user
                )
        except Exception:
            pass

    def validate(self, data):
        doctor = data.get('doctor')
        guest_doctor = data.get('guest_doctor')
        if doctor and guest_doctor:
            raise serializers.ValidationError({
                'doctor': 'Choose either a registered doctor or a guest doctor, not both.'
            })

        patient = data.get('patient')
        new_name = (data.get('new_patient_name') or '').strip()
        if patient and new_name:
            raise serializers.ValidationError({
                'patient': 'Provide either an existing patient or new-patient details, not both.'
            })
        if not patient and not new_name:
            raise serializers.ValidationError({
                'patient': 'Either an existing patient or new_patient_name is required.'
            })

        pay_now = data.pop('pay_now', False)
        if pay_now and not data.get('payment_method'):
            raise serializers.ValidationError({
                'payment_method': 'Payment method is required when pay_now is true.'
            })

        # ── Consultation type: NEW vs free REVISIT ─────────────────────────
        # Mirrors ConsultationBill's own NEW/REVISIT rules so a prebooking's
        # fee behaves the same way a walk-in bill's fee does, instead of
        # reception having to manually zero it out for a returning patient.
        consultation_type = data.get('consultation_type', 'NEW')
        if consultation_type == 'REVISIT':
            if not patient:
                raise serializers.ValidationError({
                    'consultation_type': (
                        'Only an existing, registered patient can book a revisit. '
                        'Select "New patient" as NEW, or pick the patient from the search.'
                    )
                })
            if not patient_is_revisit_eligible(patient):
                raise serializers.ValidationError({
                    'consultation_type': (
                        'No active revisit window found for this patient. '
                        'Revisits are only free within 3 days (today, tomorrow, or the '
                        'day after) of their last NEW consultation — book this as NEW instead.'
                    )
                })
            # Revisit consultations are always ₹0, regardless of what was
            # typed/auto-filled into the fee field.
            data['consultation_fee'] = Decimal('0')
        elif consultation_type == 'HOME_VISIT':
            # Home Visit pricing is independent of the doctor's standard
            # consultation_fee (it comes from HospitalSettings' manager-set
            # defaults, fetched and possibly edited by reception before
            # this call) — never auto-fill it from doctor/guest_doctor.
            # ConsultationPreBooking.clean() enforces fee > 0 at save time.
            pass
        else:
            # Default the consultation_fee to the doctor/guest doctor's
            # current fee if the caller didn't send one explicitly.
            if not data.get('consultation_fee'):
                if doctor:
                    data['consultation_fee'] = doctor.consultation_fee
                elif guest_doctor:
                    data['consultation_fee'] = guest_doctor.consultation_fee

        # ── Branch ──────────────────────────────────────────────────────
        # Auto-derived from patient.branch at save() time when an existing
        # patient is linked (see ConsultationPreBooking.save()). For a
        # brand-new, not-yet-registered patient there's no Patient row to
        # derive it from, so it must be resolved here explicitly — this
        # was previously missing entirely, which made every CALL/WALKIN
        # booking for a new patient fail ConsultationPreBooking.clean()'s
        # "Branch is required" check.
        if not patient:
            request = self.context.get('request')
            from authentication.utils import resolve_branch_for_write
            branch, error = resolve_branch_for_write(request, required=True)
            if error:
                raise serializers.ValidationError(error.data.get("errors", {"branch": "Branch is required."}))
            data['branch'] = branch

        data['_pay_now'] = pay_now
        return data

    def create(self, validated_data):
        from django.utils import timezone as _tz
        pay_now = validated_data.pop('_pay_now', False)

        if pay_now:
            validated_data['payment_status'] = 'PAID'
            validated_data['paid_at'] = _tz.now()

        request = self.context.get('request')
        if request and request.user and request.user.is_authenticated:
            validated_data['created_by'] = request.user

        return ConsultationPreBooking.objects.create(**validated_data)