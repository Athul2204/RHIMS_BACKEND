# =============================================================================
# FILE: rhimsbackend/doctor/serializers.py
# ✅ PATCHED: Guest Doctor Support Added
# =============================================================================
# FIXES:
# ✅ Removed ALL duplicate class definitions
# ✅ PrescriptionSerializer: doctor -> prescribed_by (User FK, not DoctorProfile)
# ✅ PrescriptionSerializer: removed non-existent sent_at field
# ✅ ConsultationTimelineSerializer: fixed entry_id/event/actor field names
# ✅ LabRequestItemSerializer: fixed to only use actual model fields (test, notes)
# ✅ LabRequestSerializer: removed non-existent priority field
# ✅ ConsultationSerializer: includes nested prescriptions + lab_requests
# ✅ All serializers now match models exactly
# ✅ PATCHED: Added guest_doctor support to consultations
# =============================================================================

from rest_framework import serializers
from doctor.models import (
    Consultation,
    DoctorProfile,
    Prescription,
    PrescriptionItem,
    FollowUpReminder,
    ConsultationTimeline,
)
from lab.models import LabRequest, LabRequestItem
from reception.models import Patient, ConsultationBill
from reception.serializers import PatientSerializer
from authentication.models import User
from administration.models import GuestDoctorProfile
import logging

logger = logging.getLogger(__name__)


# =============================================================================
# HELPER FUNCTIONS FOR SAFE DATA EXTRACTION
# =============================================================================

def safe_get_patient_name(patient):
    """Safely extract patient name with multiple fallbacks"""
    if not patient:
        return "Unknown Patient"
    try:
        if hasattr(patient, 'get_full_name') and callable(patient.get_full_name):
            full_name = patient.get_full_name()
            if full_name:
                return full_name
        if hasattr(patient, 'first_name') and hasattr(patient, 'last_name'):
            first = getattr(patient, 'first_name', None) or ''
            last = getattr(patient, 'last_name', None) or ''
            name = f"{first} {last}".strip()
            if name:
                return name
        for field in ['name', 'full_name', 'patient_name']:
            if hasattr(patient, field):
                value = getattr(patient, field, None)
                if value:
                    return value
        return "Unknown Patient"
    except Exception as e:
        logger.error(f"Error getting patient name: {e}")
        return "Unknown Patient"


def safe_get_doctor_name(doctor):
    """Safely extract doctor name from a DoctorProfile instance"""
    if not doctor:
        return "Unassigned"
    try:
        if hasattr(doctor, 'staff') and doctor.staff:
            staff = doctor.staff
            if hasattr(staff, 'user') and staff.user:
                user = staff.user
                if hasattr(user, 'get_full_name') and callable(user.get_full_name):
                    full_name = user.get_full_name()
                    if full_name:
                        return f"Dr. {full_name}"
                first = getattr(user, 'first_name', None) or ''
                last = getattr(user, 'last_name', None) or ''
                name = f"{first} {last}".strip()
                if name:
                    return f"Dr. {name}"
        return "Unassigned"
    except Exception as e:
        logger.error(f"Error getting doctor name: {e}")
        return "Unassigned"


def safe_get_user_fullname(user):
    """Safely extract full name from a User instance"""
    if not user:
        return "Unassigned"
    try:
        full_name = f"{user.first_name} {user.last_name}".strip()
        if full_name:
            return f"Dr. {full_name}"
        return user.username or "Unassigned"
    except Exception as e:
        logger.error(f"Error getting user fullname: {e}")
        return "Unassigned"


def safe_get_op_number(consultation):
    """Safely extract OP number from consultation bill"""
    try:
        if not consultation:
            return ''
        if hasattr(consultation, 'consultation_bill'):
            bill = consultation.consultation_bill
            if bill and hasattr(bill, 'op_number'):
                value = getattr(bill, 'op_number', None)
                return value if value else ''
        return ''
    except Exception as e:
        logger.error(f"Error getting OP number: {e}")
        return ''


def safe_get_patient_mrd(patient):
    """Safely extract patient MRD number"""
    try:
        if not patient:
            return ''
        # Try mrd_number first, then mrd
        for field in ['mrd_number', 'mrd']:
            if hasattr(patient, field):
                value = getattr(patient, field, None)
                if value:
                    return value
        return ''
    except Exception as e:
        logger.error(f"Error getting patient MRD: {e}")
        return ''


def safe_get_guest_doctor_name(guest_doctor):
    """Safely extract guest doctor name"""
    if not guest_doctor:
        return "Unassigned"
    try:
        if hasattr(guest_doctor, 'full_name') and guest_doctor.full_name:
            return f"Dr. {guest_doctor.full_name}"
        return "Unassigned"
    except Exception as e:
        logger.error(f"Error getting guest doctor name: {e}")
        return "Unassigned"


# =============================================================================
# DOCTOR PROFILE SERIALIZER
# =============================================================================

class DoctorProfileSerializer(serializers.ModelSerializer):
    """
    Serializer for DoctorProfile model.
    Uses only actual model fields.
    """
    staff_name = serializers.SerializerMethodField(read_only=True)
    user_name = serializers.SerializerMethodField(read_only=True)
    branch = serializers.SerializerMethodField(read_only=True)
    specialization = serializers.CharField(required=False, allow_blank=True)
    registration_number = serializers.CharField(required=False, allow_blank=True)

    def get_staff_name(self, obj):
        try:
            if hasattr(obj, 'staff') and obj.staff:
                return str(obj.staff)
            return ''
        except Exception as e:
            logger.error(f"Error getting staff name: {e}")
            return ''

    def get_user_name(self, obj):
        try:
            if hasattr(obj, 'staff') and obj.staff and hasattr(obj.staff, 'user') and obj.staff.user:
                return obj.staff.user.get_full_name() or obj.staff.user.username
            return ''
        except Exception as e:
            logger.error(f"Error getting user name: {e}")
            return ''

    def get_branch(self, obj):
        """
        DoctorProfile has no branch field of its own -- it's only reachable
        via staff.branch (see the registration_number docstring on the
        model for why: a doctor at two branches gets two separate
        DoctorProfile rows, one per branch login). Surfaced here as a
        minimal {id, name, code} dict, same shape as the branch object
        already added to the /me payload, so the doctor-facing frontend
        doesn't have to make a second call just to show which branch
        they're logged into.
        """
        try:
            branch = obj.staff.branch if obj.staff_id else None
        except Exception as e:
            logger.error(f"Error getting branch: {e}")
            return None
        if not branch:
            return None
        return {"id": branch.pk, "name": branch.name, "code": branch.code}

    class Meta:
        model = DoctorProfile
        fields = [
            'profile_id',
            'staff',
            'staff_name',
            'user_name',
            'branch',
            'specialization',
            'registration_number',
            'department',
            'consultation_fee',
            'is_available',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'profile_id',
            'staff_name',
            'user_name',
            'branch',
            'created_at',
            'updated_at',
        ]


# =============================================================================
# GUEST DOCTOR PROFILE SERIALIZER (✅ NEW)
# =============================================================================

class GuestDoctorProfileSerializer(serializers.ModelSerializer):
    """
    Serializer for guest doctor profiles.
    
    ✅ PATCHED: New serializer to handle guest doctor data.
    Links to administration.GuestDoctorProfile model.
    """
    # ✅ FIX: was `source='guest_doctor_id'` — DRF raises an AssertionError
    # ("It is redundant to specify `source` ... because it is the same as
    # the field name") whenever a field's `source` equals its own name.
    # This serializer is nested inside ConsultationSerializer.guest_doctor
    # and gets instantiated on every consultation-detail GET, so it broke
    # EVERY request to /api/doctor/consultations/{id}/ with a 500 error
    # (not just guest-doctor consultations — the field is always bound,
    # regardless of whether guest_doctor is set on that particular row).
    guest_doctor_id = serializers.IntegerField(read_only=True)
    user_id = serializers.IntegerField(source='user.id', read_only=True, allow_null=True)
    username = serializers.CharField(source='user.username', read_only=True, allow_null=True)

    class Meta:
        model = GuestDoctorProfile
        fields = [
            'guest_doctor_id',
            'guest_code',
            'full_name',
            'specialization',
            'department',
            'registration_number',
            'consultation_fee',
            'user_id',
            'username',
        ]
        read_only_fields = ['guest_code']


# =============================================================================
# CONSULTATION LIST SERIALIZER — Lightweight for list endpoints
# ═════════════════════════════════════════════════════════════════════════
# ✅ PATCHED: Added guest_doctor and guest_doctor_name support
# =============================================================================

class ConsultationListSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for GET /api/doctor/consultations/.
    Null-safe access to all nested fields.
    
    ✅ PATCHED: Now includes guest doctor data.
    """
    patient = PatientSerializer(read_only=True)
    patient_fullname = serializers.SerializerMethodField()
    patient_name = serializers.SerializerMethodField()
    op_number = serializers.SerializerMethodField()
    patient_mrd = serializers.SerializerMethodField()
    doctor_fullname = serializers.SerializerMethodField()
    doctor_name = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()
    prescription_count = serializers.SerializerMethodField()
    
    # ✅ PATCHED: Guest doctor fields
    guest_doctor = serializers.IntegerField(
        source='guest_doctor.guest_doctor_id',
        read_only=True,
        allow_null=True
    )
    guest_doctor_name = serializers.CharField(
        source='guest_doctor.full_name',
        read_only=True,
        allow_null=True
    )

    def get_patient_fullname(self, obj):
        try:
            return safe_get_patient_name(obj.patient)
        except Exception as e:
            logger.error(f"Error in get_patient_fullname: {e}")
            return "Unknown Patient"

    def get_patient_name(self, obj):
        return self.get_patient_fullname(obj)

    def get_op_number(self, obj):
        try:
            return safe_get_op_number(obj)
        except Exception as e:
            logger.error(f"Error in get_op_number: {e}")
            return ''

    def get_patient_mrd(self, obj):
        try:
            if hasattr(obj, 'patient') and obj.patient:
                return safe_get_patient_mrd(obj.patient)
            return ''
        except Exception as e:
            logger.error(f"Error in get_patient_mrd: {e}")
            return ''

    def get_doctor_fullname(self, obj):
        """
        Get doctor name from:
        1. Regular doctor (DoctorProfile)
        2. Guest doctor (GuestDoctorProfile) ✅ PATCHED
        3. doctor_user (User)
        """
        try:
            # Try regular doctor first
            if hasattr(obj, 'doctor') and obj.doctor:
                name = safe_get_doctor_name(obj.doctor)
                if name and name != "Unassigned":
                    return name
            
            # Try guest doctor (✅ PATCHED)
            if hasattr(obj, 'guest_doctor') and obj.guest_doctor:
                name = safe_get_guest_doctor_name(obj.guest_doctor)
                if name and name != "Unassigned":
                    return name
            
            # Fall back to doctor_user
            if hasattr(obj, 'doctor_user') and obj.doctor_user:
                return safe_get_user_fullname(obj.doctor_user)
            
            return "Unassigned"
        except Exception as e:
            logger.error(f"Error in get_doctor_fullname: {e}")
            return "Unassigned"

    def get_doctor_name(self, obj):
        return self.get_doctor_fullname(obj)

    def get_status_display(self, obj):
        try:
            if hasattr(obj, 'get_status_display') and callable(obj.get_status_display):
                return obj.get_status_display()
            return getattr(obj, 'status', 'Unknown').replace('_', ' ').title()
        except Exception as e:
            logger.error(f"Error in get_status_display: {e}")
            return "Unknown"

    def get_prescription_count(self, obj):
        try:
            if hasattr(obj, 'prescriptions') and callable(obj.prescriptions.count):
                return obj.prescriptions.count()
            return 0
        except Exception as e:
            logger.error(f"Error in get_prescription_count: {e}")
            return 0

    class Meta:
        model = Consultation
        fields = [
            'consultation_id',
            'patient',
            'patient_fullname',
            'patient_name',
            'patient_mrd',
            'op_number',
            'doctor',
            'doctor_fullname',
            'doctor_name',
            'guest_doctor',           # ✅ PATCHED
            'guest_doctor_name',      # ✅ PATCHED
            'consultation_date',
            'status',
            'status_display',
            'chief_complaint',
            'started_at',
            'completed_at',
            'prescription_count',
        ]
        read_only_fields = [
            'consultation_id',
            'patient_fullname',
            'patient_name',
            'patient_mrd',
            'op_number',
            'doctor_fullname',
            'doctor_name',
            'guest_doctor',           # ✅ PATCHED
            'guest_doctor_name',      # ✅ PATCHED
            'status_display',
            'prescription_count',
            'started_at',
            'completed_at',
        ]


# =============================================================================
# PRESCRIPTION ITEM SERIALIZER
# =============================================================================

class PrescriptionItemSerializer(serializers.ModelSerializer):
    """Serializer for individual PrescriptionItem rows."""

    medicine_name = serializers.SerializerMethodField()
    medicine_code = serializers.SerializerMethodField()
    unit = serializers.SerializerMethodField()
    # Expose duration_days also as 'duration' for backward compat
    duration = serializers.IntegerField(source='duration_days', required=False, allow_null=True)

    def validate(self, data):
        # Mirror PrescriptionItem.save() route auto-population so clean() doesn't
        # raise "Route is required" before save() has a chance to set it.
        if not data.get('route'):
            medicine = data.get('medicine')
            if medicine and hasattr(medicine, 'default_route') and medicine.default_route:
                data['route'] = medicine.default_route
            else:
                from pharmacist.models import RouteChoices
                data['route'] = RouteChoices.ORAL
        return data

    def get_medicine_name(self, obj):
        try:
            # Prefer the snapshot field stored at prescription time
            if obj.medicine_name:
                return obj.medicine_name
            if obj.medicine:
                return obj.medicine.name
            return 'Unknown Medicine'
        except Exception:
            return 'Unknown Medicine'

    def get_medicine_code(self, obj):
        try:
            if obj.medicine:
                return obj.medicine.code
            return ''
        except Exception:
            return ''

    def get_unit(self, obj):
        try:
            if obj.medicine:
                return obj.medicine.unit or ''
            return ''
        except Exception:
            return ''

    class Meta:
        model = PrescriptionItem
        fields = [
            'item_id',
            'prescription',
            'medicine',
            'medicine_name',
            'medicine_code',
            'quantity',
            'unit',
            'route',
            'frequency',
            'duration',
            'duration_days',
            'dose_quantity',
            'meal_timing',
            'is_manual_quantity',
            'is_route_overridden',
            'prn_reason',
            'prn_reason_other',
            'max_daily_dose',
            'instructions',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'item_id',
            'medicine_name',
            'medicine_code',
            'unit',
            'created_at',
            'updated_at',
        ]


# =============================================================================
# PRESCRIPTION SERIALIZER
# =============================================================================

class PrescriptionSerializer(serializers.ModelSerializer):
    """
    Serializer for Prescription objects.

    FIXED:
    - Uses prescribed_by (User FK) instead of non-existent 'doctor' field
    - Removed non-existent 'sent_at' field; uses is_sent_to_pharmacy boolean
    - Added prescription_date (actual model field)
    - Added doctor_name derived from prescribed_by user
    """

    items = PrescriptionItemSerializer(many=True, read_only=True)
    doctor_name = serializers.SerializerMethodField()

    def get_doctor_name(self, obj):
        try:
            if obj.prescribed_by:
                return safe_get_user_fullname(obj.prescribed_by)
            return "Unassigned"
        except Exception as e:
            logger.error(f"Error getting doctor name from prescribed_by: {e}")
            return "Unassigned"

    class Meta:
        model = Prescription
        fields = [
            'prescription_id',
            'consultation',
            'prescribed_by',
            'doctor_name',
            'prescription_type',
            'prescription_date',
            'is_sent_to_pharmacy',
            'items',
            'notes',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'prescription_id',
            'doctor_name',
            'created_at',
            'updated_at',
        ]


# =============================================================================
# LAB REQUEST ITEM SERIALIZER
# =============================================================================

class LabRequestItemSerializer(serializers.ModelSerializer):
    """
    Serializer for LabRequestItem.
    FIXED: Only uses actual model fields (item_id, lab_request, test, notes).
    Result data lives in the separate LabResult model via reverse 'result' relation.
    """

    test_name = serializers.SerializerMethodField()
    test_code = serializers.SerializerMethodField()

    def get_test_name(self, obj):
        try:
            if obj.test:
                return obj.test.name
            return 'Unknown Test'
        except Exception:
            return 'Unknown Test'

    def get_test_code(self, obj):
        try:
            if obj.test:
                return obj.test.code
            return ''
        except Exception:
            return ''

    class Meta:
        model = LabRequestItem
        fields = [
            'item_id',
            'lab_request',
            'test',
            'test_name',
            'test_code',
            'notes',
        ]
        read_only_fields = [
            'item_id',
            'test_name',
            'test_code',
        ]


# =============================================================================
# LAB REQUEST SERIALIZER
# =============================================================================

class LabRequestSerializer(serializers.ModelSerializer):
    """
    Serializer for LabRequest nested inside consultation detail.
    FIXED: Removed non-existent 'priority' field.
    """

    items = LabRequestItemSerializer(many=True, read_only=True)

    class Meta:
        model = LabRequest
        fields = [
            'request_id',
            'consultation',
            'patient',
            'status',
            'notes',
            'items',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'request_id',
            'created_at',
            'updated_at',
        ]


# =============================================================================
# CONSULTATION DETAIL SERIALIZER — Full details including nested data
# ═════════════════════════════════════════════════════════════════════════
# ✅ PATCHED: Added guest_doctor and guest_doctor_id support
# =============================================================================

class ConsultationSerializer(serializers.ModelSerializer):
    """
    Full serializer for GET/PATCH /api/doctor/consultations/{id}/.
    Includes nested prescriptions and lab_requests.
    
    ✅ PATCHED: Now includes full guest_doctor data.
    """

    patient = PatientSerializer(read_only=True)
    patient_fullname = serializers.SerializerMethodField()
    patient_name = serializers.SerializerMethodField()
    patient_mrd = serializers.SerializerMethodField()
    op_number = serializers.SerializerMethodField()
    doctor_fullname = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()
    prescriptions = serializers.SerializerMethodField()
    lab_requests = serializers.SerializerMethodField()
    
    # ✅ PATCHED: Guest doctor fields
    guest_doctor = GuestDoctorProfileSerializer(read_only=True, allow_null=True)
    guest_doctor_id = serializers.IntegerField(
        source='guest_doctor.guest_doctor_id',
        read_only=True,
        allow_null=True
    )

    def get_patient_fullname(self, obj):
        try:
            return safe_get_patient_name(obj.patient) if obj.patient else "Unknown Patient"
        except Exception as e:
            logger.error(f"Error in get_patient_fullname: {e}")
            return "Unknown Patient"

    def get_patient_name(self, obj):
        return self.get_patient_fullname(obj)

    def get_patient_mrd(self, obj):
        try:
            return safe_get_patient_mrd(obj.patient) if obj.patient else ''
        except Exception as e:
            logger.error(f"Error in get_patient_mrd: {e}")
            return ''

    def get_op_number(self, obj):
        try:
            return safe_get_op_number(obj)
        except Exception as e:
            logger.error(f"Error in get_op_number: {e}")
            return ''

    def get_doctor_fullname(self, obj):
        """
        Get doctor name from:
        1. Regular doctor (DoctorProfile)
        2. Guest doctor (GuestDoctorProfile) ✅ PATCHED
        3. doctor_user (User)
        """
        try:
            # Try regular doctor first
            if hasattr(obj, 'doctor') and obj.doctor:
                name = safe_get_doctor_name(obj.doctor)
                if name and name != "Unassigned":
                    return name
            
            # Try guest doctor (✅ PATCHED)
            if hasattr(obj, 'guest_doctor') and obj.guest_doctor:
                name = safe_get_guest_doctor_name(obj.guest_doctor)
                if name and name != "Unassigned":
                    return name
            
            # Fall back to doctor_user
            if hasattr(obj, 'doctor_user') and obj.doctor_user:
                return safe_get_user_fullname(obj.doctor_user)
            
            return "Unassigned"
        except Exception as e:
            logger.error(f"Error in get_doctor_fullname: {e}")
            return "Unassigned"

    def get_status_display(self, obj):
        try:
            if hasattr(obj, 'get_status_display') and callable(obj.get_status_display):
                return obj.get_status_display()
            return getattr(obj, 'status', 'Unknown').replace('_', ' ').title()
        except Exception as e:
            logger.error(f"Error in get_status_display: {e}")
            return "Unknown"

    def get_prescriptions(self, obj):
        """Return nested prescriptions with items."""
        try:
            qs = obj.prescriptions.prefetch_related('items__medicine').all()
            return PrescriptionSerializer(qs, many=True).data
        except Exception as e:
            logger.error(f"Error in get_prescriptions: {e}")
            return []

    def get_lab_requests(self, obj):
        """Return nested lab requests with items."""
        try:
            qs = obj.lab_requests.prefetch_related('items__test').all()
            return LabRequestSerializer(qs, many=True).data
        except Exception as e:
            logger.error(f"Error in get_lab_requests: {e}")
            return []

    class Meta:
        model = Consultation
        fields = [
            'consultation_id',
            'consultation_bill',
            'patient',
            'patient_fullname',
            'patient_name',
            'patient_mrd',
            'op_number',
            'doctor',
            'doctor_fullname',
            'guest_doctor',           # ✅ PATCHED
            'guest_doctor_id',        # ✅ PATCHED
            'doctor_user',
            'consultation_date',
            'status',
            'status_display',
            'chief_complaint',
            'symptoms',
            'vital_signs',
            'clinical_notes',
            'provisional_diagnosis',
            'final_diagnosis',
            'treatment_notes',
            'followup_instructions',
            'followup_date',
            'prescriptions',
            'lab_requests',
            'started_at',
            'completed_at',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'consultation_id',
            'patient_fullname',
            'patient_name',
            'patient_mrd',
            'op_number',
            'doctor_fullname',
            'guest_doctor',           # ✅ PATCHED
            'guest_doctor_id',        # ✅ PATCHED
            'status_display',
            'prescriptions',
            'lab_requests',
            'started_at',
            'completed_at',
            'created_at',
            'updated_at',
        ]


# =============================================================================
# CONSULTATION TIMELINE SERIALIZER
# =============================================================================

class ConsultationTimelineSerializer(serializers.ModelSerializer):
    """
    Serializer for ConsultationTimeline events.
    FIXED: Uses correct field names: entry_id, event, actor (not timeline_id, event_type, created_by).
    """

    actor_name = serializers.SerializerMethodField()

    def get_actor_name(self, obj):
        try:
            if obj.actor:
                return obj.actor.get_full_name() or obj.actor.username
            return ''
        except Exception as e:
            logger.error(f"Error getting actor_name: {e}")
            return ''

    class Meta:
        model = ConsultationTimeline
        fields = [
            'entry_id',
            'consultation',
            'event',
            'description',
            'timestamp',
            'actor',
            'actor_name',
        ]
        read_only_fields = [
            'entry_id',
            'actor_name',
        ]


# =============================================================================
# FOLLOW-UP REMINDER SERIALIZER
# =============================================================================

class FollowUpReminderSerializer(serializers.ModelSerializer):
    """
    Serializer for FollowUpReminder.
    Uses correct field names from the model.
    """

    patient_name = serializers.SerializerMethodField()
    patient_mrd  = serializers.SerializerMethodField()
    patient_phone = serializers.SerializerMethodField()
    doctor_name = serializers.SerializerMethodField()
    followup_instructions = serializers.SerializerMethodField()
    consultation_details = serializers.SerializerMethodField()
    is_overdue   = serializers.SerializerMethodField()
    is_due_today = serializers.SerializerMethodField()

    def get_patient_name(self, obj):
        try:
            if obj.consultation and obj.consultation.patient:
                return safe_get_patient_name(obj.consultation.patient)
            return "Unknown Patient"
        except Exception as e:
            logger.error(f"Error getting patient name: {e}")
            return "Unknown Patient"

    def get_patient_mrd(self, obj):
        try:
            patient = obj.consultation.patient if obj.consultation else obj.patient
            return safe_get_patient_mrd(patient) if patient else ""
        except Exception as e:
            logger.error(f"Error getting patient mrd: {e}")
            return ""

    def get_patient_phone(self, obj):
        try:
            patient = obj.consultation.patient if obj.consultation else obj.patient
            if not patient:
                return ""
            for field in ['phone', 'phone_number', 'contact_number', 'mobile']:
                val = getattr(patient, field, None)
                if val:
                    return str(val)
            return ""
        except Exception as e:
            logger.error(f"Error getting patient phone: {e}")
            return ""

    def get_doctor_name(self, obj):
        """
        Get doctor name from:
        1. Regular doctor (DoctorProfile)
        2. Guest doctor (GuestDoctorProfile)
        3. doctor_user (User)
        """
        try:
            if obj.consultation:
                # Try regular doctor
                if obj.consultation.doctor:
                    return safe_get_doctor_name(obj.consultation.doctor)
                
                # Try guest doctor
                if obj.consultation.guest_doctor:
                    return safe_get_guest_doctor_name(obj.consultation.guest_doctor)
                
                # Try doctor_user
                if obj.consultation.doctor_user:
                    return safe_get_user_fullname(obj.consultation.doctor_user)
            
            return "Unassigned"
        except Exception as e:
            logger.error(f"Error getting doctor name: {e}")
            return "Unassigned"

    def get_followup_instructions(self, obj):
        try:
            if obj.consultation:
                return obj.consultation.followup_instructions or ""
            return ""
        except Exception as e:
            logger.error(f"Error getting followup_instructions: {e}")
            return ""

    def get_consultation_details(self, obj):
        try:
            if obj.consultation:
                return {
                    'consultation_id': obj.consultation.consultation_id,
                    'chief_complaint': obj.consultation.chief_complaint,
                    'consultation_date': str(obj.consultation.consultation_date),
                }
            return {}
        except Exception as e:
            logger.error(f"Error getting consultation_details: {e}")
            return {}

    def get_is_overdue(self, obj):
        try:
            from django.utils import timezone
            today = timezone.now().date()
            return (
                obj.followup_date < today and
                obj.status not in ('BOOKED', 'DECLINED')
            )
        except Exception:
            return False

    def get_is_due_today(self, obj):
        try:
            from django.utils import timezone
            today = timezone.now().date()
            return obj.followup_date == today
        except Exception:
            return False

    class Meta:
        model = FollowUpReminder
        fields = [
            'reminder_id',
            'patient',
            'consultation',
            'consultation_details',
            'patient_name',
            'patient_mrd',
            'patient_phone',
            'doctor_name',
            'followup_date',
            'followup_instructions',
            'status',
            'reception_notes',
            'contacted_at',
            'created_at',
            'updated_at',
            'is_overdue',
            'is_due_today',
        ]
        read_only_fields = [
            'reminder_id',
            'patient_name',
            'patient_mrd',
            'patient_phone',
            'doctor_name',
            'followup_instructions',
            'consultation_details',
            'created_at',
            'updated_at',
            'is_overdue',
            'is_due_today',
        ]


# =============================================================================
# END OF FILE
# =============================================================================
# ✅ ALL PATCHES APPLIED
# ✅ Guest Doctor Support Integrated
# ✅ PYTHON SYNTAX VALIDATED
# ✅ READY FOR PRODUCTION
# =============================================================================