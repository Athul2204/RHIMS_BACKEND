# pyrefly: ignore [missing-import]
from rest_framework import serializers
# pyrefly: ignore [missing-import]
from django.db import IntegrityError, transaction
# pyrefly: ignore [missing-import]
from authentication.permissions import _get_role
from authentication.utils import is_group_admin_user, get_user_branch

from .models import (
    TestGroup,
    LabTest,
    LabRequest,
    LabRequestItem,
    LabResult,
    LabReport,
    LabRequestStatus,
    LabBill,
    LabEquipment,
    LabMaintenance,
    LabOrder,
    add_test_group,
)


# ─────────────────────────────────────────────────────────
# SAFE PATIENT HELPERS
# ─────────────────────────────────────────────────────────
def get_safe_patient_name(patient):
    if not patient:
        return None

    first = getattr(patient, 'first_name', '') or ''
    last = getattr(patient, 'last_name', '') or ''

    return f"{first} {last}".strip()


def get_safe_patient_identifier(patient):
    """
    Prevent 500 errors when MRD/UHID field differs
    across Patient model implementations.
    """
    if not patient:
        return None

    return (
        getattr(patient, 'mrd_number', None)
        or getattr(patient, 'patient_id', None)
        or getattr(patient, 'uhid', None)
        or getattr(patient, 'registration_number', None)
        or getattr(patient, 'id', None)
    )


# ─────────────────────────────────────────────────────────
# Test Group (billing panel, e.g. LFT, KFT, LIPID)
# ─────────────────────────────────────────────────────────
class TestGroupSubTestSerializer(serializers.ModelSerializer):
    """Minimal read-only view of a sub-test for display under its panel."""

    class Meta:
        model = LabTest
        fields = ['test_id', 'name', 'code', 'unit', 'normal_range']


class TestGroupSerializer(serializers.ModelSerializer):

    sub_tests = TestGroupSubTestSerializer(many=True, read_only=True)

    class Meta:
        model = TestGroup
        fields = '__all__'
        read_only_fields = ['group_id', 'created_at', 'updated_at']


# ─────────────────────────────────────────────────────────
# Lab Test
# ─────────────────────────────────────────────────────────
class LabTestSerializer(serializers.ModelSerializer):

    # Read-only convenience field so the test-picker UI can show which
    # panel (if any) a test belongs to without a second lookup.
    group_name = serializers.CharField(source='group.name', read_only=True, default=None)

    class Meta:
        model = LabTest
        fields = '__all__'
        read_only_fields = ['test_id', 'created_at', 'updated_at']


# ─────────────────────────────────────────────────────────
# Lab Result
# ─────────────────────────────────────────────────────────
class LabResultSerializer(serializers.ModelSerializer):

    performed_by_username = serializers.SerializerMethodField()

    test_code = serializers.CharField(
        source='lab_request_item.test.code',
        read_only=True
    )

    test_name = serializers.CharField(
        source='lab_request_item.test.name',
        read_only=True
    )

    class Meta:
        model = LabResult
        fields = '__all__'
        read_only_fields = ['result_id', 'created_at', 'updated_at']

    def get_performed_by_username(self, obj):
        if obj.performed_by:
            return obj.performed_by.username
        return None


class LabResultWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = LabResult
        fields = '__all__'
        read_only_fields = ['result_id', 'created_at', 'updated_at']

    def validate_lab_request_item(self, value):
        if self.instance is None:
            if LabResult.objects.filter(lab_request_item=value).exists():
                raise serializers.ValidationError(
                    'A result already exists for this test item.'
                )
        return value


# ─────────────────────────────────────────────────────────
# Lab Request Item
# ─────────────────────────────────────────────────────────
class LabRequestItemSerializer(serializers.ModelSerializer):

    test_code = serializers.CharField(
        source='test.code',
        read_only=True
    )

    test_name = serializers.CharField(
        source='test.name',
        read_only=True
    )

    test_unit = serializers.CharField(
        source='test.unit',
        read_only=True
    )

    test_normal_range = serializers.CharField(
        source='test.normal_range',
        read_only=True
    )

    # Per-test catalog price, exposed read-only so bill receipts can show a
    # per-line rate without a second lookup against LabTest.
    test_price = serializers.DecimalField(
        source='test.price',
        max_digits=10, decimal_places=2,
        read_only=True
    )

    # Read-only convenience field so the frontend can cluster sibling rows
    # under one heading/one price line without a second TestGroup lookup.
    ordered_as_group_name = serializers.CharField(
        source='ordered_as_group.name',
        read_only=True,
        default=None,
    )

    ordered_as_group_price = serializers.DecimalField(
        source='ordered_as_group.price',
        max_digits=10, decimal_places=2,
        read_only=True,
        default=None,
    )

    result = LabResultSerializer(read_only=True)

    class Meta:
        model = LabRequestItem
        fields = '__all__'
        read_only_fields = ['item_id']


# ─────────────────────────────────────────────────────────
# Lab Report
# ─────────────────────────────────────────────────────────
class LabReportSerializer(serializers.ModelSerializer):

    verified_by_username = serializers.SerializerMethodField()

    class Meta:
        model = LabReport
        fields = '__all__'
        read_only_fields = ['report_id', 'created_at', 'updated_at']

    def get_verified_by_username(self, obj):
        if obj.verified_by:
            return obj.verified_by.username
        return None


class LabReportWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = LabReport
        fields = '__all__'
        read_only_fields = ['report_id', 'created_at', 'updated_at']


# ─────────────────────────────────────────────────────────
# Lab Request (Read)
# ─────────────────────────────────────────────────────────
class LabRequestSerializer(serializers.ModelSerializer):

    items = LabRequestItemSerializer(many=True, read_only=True)

    report = LabReportSerializer(read_only=True)

    requested_by_username = serializers.SerializerMethodField()

    patient_name = serializers.SerializerMethodField()

    patient_mrd = serializers.SerializerMethodField()

    patient_phone = serializers.SerializerMethodField()

    patient_type = serializers.SerializerMethodField()

    claimed_by_name = serializers.SerializerMethodField()

    claimed_by_id = serializers.IntegerField(read_only=True)

    is_claimed = serializers.SerializerMethodField()

    can_claim = serializers.SerializerMethodField()

    can_act = serializers.SerializerMethodField()

    class Meta:
        model = LabRequest
        fields = '__all__'
        read_only_fields = ['request_id', 'created_at', 'updated_at', 'claimed_by', 'claimed_at']

    def get_requested_by_username(self, obj):
        if obj.requested_by:
            return obj.requested_by.username
        return None

    def get_patient_name(self, obj):
        if obj.is_walkin:
            return obj.walkin_name or 'Unknown'
        return get_safe_patient_name(obj.patient)

    def get_patient_mrd(self, obj):
        if obj.is_walkin:
            return None
        return get_safe_patient_identifier(obj.patient)

    def get_patient_phone(self, obj):
        if obj.is_walkin:
            return obj.walkin_phone
        return getattr(obj.patient, 'phone', None) if obj.patient else None

    def get_patient_type(self, obj):
        return 'walk-in' if obj.is_walkin else 'registered'

    def get_claimed_by_name(self, obj):
        if obj.claimed_by:
            return obj.claimed_by.get_full_name() or obj.claimed_by.username
        return None

    def get_is_claimed(self, obj):
        return obj.claimed_by_id is not None

    def get_can_claim(self, obj):
        return obj.claimed_by_id is None

    def get_can_act(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return False
        if obj.claimed_by_id == user.id:
            return True
        return _get_role(user) in ('admin', 'manager')


# ─────────────────────────────────────────────────────────
# Lab Request (Write)
# ─────────────────────────────────────────────────────────
class LabRequestWriteSerializer(serializers.ModelSerializer):

    # Standalone tests, ordered individually and billed at test.price each.
    test_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        default=list,
    )

    # Panels/groups, e.g. "Liver Function Test" — each expands server-side
    # to all its active sub-tests, tagged so they're billed once as a unit
    # (see add_test_group() / calculate_lab_subtotal() in lab/models.py).
    group_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        default=list,
    )

    class Meta:
        model = LabRequest
        fields = [
            'consultation',
            'patient',
            'requested_by',
            'notes',
            'test_ids',
            'group_ids',
        ]
        # patient is auto-derived from consultation in validate() — not required from caller
        extra_kwargs = {
            'patient': {'required': False},
        }

    def validate(self, data):
        """Auto-populate patient from consultation so the frontend does not need to send it."""
        consultation = data.get('consultation')
        if consultation and not data.get('patient'):
            data['patient'] = consultation.patient
        if not data.get('patient'):
            raise serializers.ValidationError(
                {'patient': 'Could not determine patient from consultation.'}
            )
        patient = data['patient']

        # SECURITY: a consultation id passed directly must actually belong
        # to the resolved patient — otherwise a doctor could pair a
        # consultation from one patient with a different patient's id.
        if consultation and consultation.patient_id != patient.pk:
            raise serializers.ValidationError(
                {'consultation': 'Consultation does not belong to the selected patient.'}
            )

        # SECURITY: without this, a doctor could pass any patient_id /
        # consultation_id (e.g. incrementing IDs) and create a lab request
        # for another branch's patient — the request would silently inherit
        # that branch via LabRequest.save()'s auto-derive-from-patient logic.
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is not None and not is_group_admin_user(user):
            user_branch = get_user_branch(user)
            if user_branch is None or patient.branch_id != user_branch.pk:
                raise serializers.ValidationError(
                    {'patient': 'Patient not found.'}
                )

        if not data.get('test_ids') and not data.get('group_ids'):
            raise serializers.ValidationError(
                {'non_field_errors': ['Provide at least one test_id or group_id.']}
            )

        # SECURITY: test_ids/group_ids are validated for existence/active
        # status in validate_test_ids/validate_group_ids, but not (until
        # now) for branch — a doctor could otherwise order tests from
        # another branch's catalog onto their own patient's request.
        test_ids = data.get('test_ids') or []
        if test_ids:
            foreign = list(
                LabTest.objects.filter(pk__in=test_ids)
                .exclude(branch_id=patient.branch_id)
                .values_list('pk', flat=True)
            )
            if foreign:
                raise serializers.ValidationError(
                    {'test_ids': f"Invalid test IDs: {sorted(foreign)}"}
                )

        group_ids = data.get('group_ids') or []
        if group_ids:
            foreign = list(
                TestGroup.objects.filter(pk__in=group_ids)
                .exclude(branch_id=patient.branch_id)
                .values_list('pk', flat=True)
            )
            if foreign:
                raise serializers.ValidationError(
                    {'group_ids': f"Invalid group IDs: {sorted(foreign)}"}
                )

        return data

    def validate_test_ids(self, value):

        existing = set(
            LabTest.objects.filter(
                pk__in=value,
                is_active=True
            ).values_list('pk', flat=True)
        )

        missing = set(value) - existing

        if missing:
            raise serializers.ValidationError(
                f"Invalid or inactive test IDs: {sorted(missing)}"
            )

        return value

    def validate_group_ids(self, value):

        existing = set(
            TestGroup.objects.filter(
                pk__in=value,
                is_active=True
            ).values_list('pk', flat=True)
        )

        missing = set(value) - existing

        if missing:
            raise serializers.ValidationError(
                f"Invalid or inactive group IDs: {sorted(missing)}"
            )

        return value

    def create(self, validated_data):
        test_ids = validated_data.pop('test_ids', [])
        group_ids = validated_data.pop('group_ids', [])
        # Ensure test_ids are unique to avoid duplicate LabRequestItem creation
        # which would violate the unique_together constraint.
        unique_test_ids = list(set(test_ids))
        unique_group_ids = list(set(group_ids))
        try:
            with transaction.atomic():
                lab_request = LabRequest.objects.create(**validated_data)
                for test_id in unique_test_ids:
                    LabRequestItem.objects.create(lab_request=lab_request, test_id=test_id)
                for group_id in unique_group_ids:
                    test_group = TestGroup.objects.get(pk=group_id)
                    add_test_group(lab_request, test_group)
        except IntegrityError as e:
            raise serializers.ValidationError({"non_field_errors": ["Duplicate test IDs provided or constraint violation."]})
        except Exception as e:
            raise serializers.ValidationError({"non_field_errors": [str(e)]})
        return lab_request


# ─────────────────────────────────────────────────────────
# Lab Request — Walk-in (Write)
# ─────────────────────────────────────────────────────────
class WalkInLabRequestSerializer(serializers.ModelSerializer):
    """
    Used by POST /lab/requests/walkin/create/ — creates a LabRequest for a
    patient who walks directly into the lab (no doctor consultation, no
    MRD registration). Mirrors pharmacist.CreateBillSerializer's walk-in path.
    """

    test_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        default=list,
    )

    # Panels/groups — see LabRequestWriteSerializer.group_ids for details.
    group_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        default=list,
    )

    class Meta:
        model = LabRequest
        fields = [
            'branch',
            'walkin_name',
            'walkin_phone',
            'walkin_gender',
            'walkin_age',
            'requested_by',
            'notes',
            'test_ids',
            'group_ids',
        ]

    def validate_walkin_name(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError('Walk-in patient name is required.')
        return value.strip()

    def validate(self, data):
        if not data.get('test_ids') and not data.get('group_ids'):
            raise serializers.ValidationError(
                {'non_field_errors': ['Provide at least one test_id or group_id.']}
            )

        # SECURITY: test_ids/group_ids must belong to the (server-resolved,
        # not client-supplied) branch this walk-in request is being created
        # for — otherwise a lab tech could pull another branch's catalog
        # items onto this branch's walk-in request.
        branch = data.get('branch')
        if branch is not None:
            test_ids = data.get('test_ids') or []
            if test_ids:
                foreign = list(
                    LabTest.objects.filter(pk__in=test_ids)
                    .exclude(branch_id=branch.pk if hasattr(branch, 'pk') else branch)
                    .values_list('pk', flat=True)
                )
                if foreign:
                    raise serializers.ValidationError(
                        {'test_ids': f"Invalid test IDs: {sorted(foreign)}"}
                    )

            group_ids = data.get('group_ids') or []
            if group_ids:
                foreign = list(
                    TestGroup.objects.filter(pk__in=group_ids)
                    .exclude(branch_id=branch.pk if hasattr(branch, 'pk') else branch)
                    .values_list('pk', flat=True)
                )
                if foreign:
                    raise serializers.ValidationError(
                        {'group_ids': f"Invalid group IDs: {sorted(foreign)}"}
                    )

        return data

    def validate_test_ids(self, value):

        existing = set(
            LabTest.objects.filter(
                pk__in=value,
                is_active=True
            ).values_list('pk', flat=True)
        )

        missing = set(value) - existing

        if missing:
            raise serializers.ValidationError(
                f"Invalid or inactive test IDs: {sorted(missing)}"
            )

        return value

    def validate_group_ids(self, value):

        existing = set(
            TestGroup.objects.filter(
                pk__in=value,
                is_active=True
            ).values_list('pk', flat=True)
        )

        missing = set(value) - existing

        if missing:
            raise serializers.ValidationError(
                f"Invalid or inactive group IDs: {sorted(missing)}"
            )

        return value

    def create(self, validated_data):
        test_ids = validated_data.pop('test_ids', [])
        group_ids = validated_data.pop('group_ids', [])
        unique_test_ids = list(set(test_ids))
        unique_group_ids = list(set(group_ids))
        validated_data['is_walkin'] = True
        validated_data['patient'] = None
        validated_data['consultation'] = None
        try:
            with transaction.atomic():
                lab_request = LabRequest.objects.create(**validated_data)
                for test_id in unique_test_ids:
                    LabRequestItem.objects.create(lab_request=lab_request, test_id=test_id)
                for group_id in unique_group_ids:
                    test_group = TestGroup.objects.get(pk=group_id)
                    add_test_group(lab_request, test_group)
        except IntegrityError:
            raise serializers.ValidationError(
                {"non_field_errors": ["Duplicate test IDs provided or constraint violation."]}
            )
        except Exception as e:
            raise serializers.ValidationError({"non_field_errors": [str(e)]})
        return lab_request


# ─────────────────────────────────────────────────────────
# Lab Request Status Update
# ─────────────────────────────────────────────────────────
class LabRequestStatusSerializer(serializers.Serializer):

    status = serializers.ChoiceField(
        choices=LabRequestStatus.choices
    )

    note = serializers.CharField(
        required=False,
        allow_blank=True
    )

    VALID_TRANSITIONS = {
        LabRequestStatus.REQUESTED: [
            LabRequestStatus.SAMPLE_COLLECTED
        ],

        LabRequestStatus.SAMPLE_COLLECTED: [
            LabRequestStatus.PROCESSING
        ],

        LabRequestStatus.PROCESSING: [
            LabRequestStatus.COMPLETED
        ],

        LabRequestStatus.COMPLETED: [
            LabRequestStatus.VERIFIED
        ],

        LabRequestStatus.VERIFIED: [
            LabRequestStatus.DELIVERED
        ],

        LabRequestStatus.DELIVERED: [],
    }

    def validate(self, data):

        current_status = self.context.get('current_status')

        new_status = data['status']

        allowed = self.VALID_TRANSITIONS.get(
            current_status,
            []
        )

        if new_status not in allowed:
            raise serializers.ValidationError({
                'status': (
                    f"Cannot move from '{current_status}' "
                    f"to '{new_status}'. "
                    f"Allowed next states: {allowed or ['none']}"
                )
            })

        return data


# ─────────────────────────────────────────────────────────
# Lab Bill
# ─────────────────────────────────────────────────────────
class LabBillSerializer(serializers.ModelSerializer):

    billed_by_username = serializers.SerializerMethodField()

    patient_name = serializers.SerializerMethodField()

    patient_mrd = serializers.SerializerMethodField()

    patient_type = serializers.SerializerMethodField()

    request_date = serializers.DateField(
        source='lab_request.request_date',
        read_only=True
    )

    class Meta:
        model = LabBill
        fields = '__all__'
        read_only_fields = [
            'bill_id',
            'bill_number',
            'total_amount',
            'created_at',
            'updated_at'
        ]

    def get_billed_by_username(self, obj):
        if obj.billed_by:
            return obj.billed_by.username
        return None

    def get_patient_name(self, obj):
        if obj.lab_request and obj.lab_request.is_walkin:
            return obj.lab_request.walkin_name or 'Unknown'
        return get_safe_patient_name(obj.patient)

    def get_patient_mrd(self, obj):
        if obj.lab_request and obj.lab_request.is_walkin:
            return None
        return get_safe_patient_identifier(obj.patient)

    def get_patient_type(self, obj):
        if obj.lab_request and obj.lab_request.is_walkin:
            return 'walk-in'
        return 'registered'


class LabBillWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = LabBill
        fields = '__all__'
        read_only_fields = [
            'bill_id',
            'bill_number',
            # subtotal is derived from the bill's lab test items — never
            # accept it from the client, same as PharmacyBillSerializer.
            # Without this, a caller could PATCH an arbitrary subtotal and
            # get an unvalidated discount/total_amount past the discount
            # <= subtotal check below.
            'subtotal',
            'total_amount',
            'created_at',
            'updated_at'
        ]

    def validate(self, data):

        subtotal = data.get(
            'subtotal',
            getattr(self.instance, 'subtotal', 0)
        )

        discount = data.get(
            'discount',
            getattr(self.instance, 'discount', 0)
        )

        paid = data.get(
            'paid_amount',
            getattr(self.instance, 'paid_amount', 0)
        )

        total = max(subtotal - discount, 0)

        if paid > total and total > 0:
            raise serializers.ValidationError({
                'paid_amount':
                    'Paid amount cannot exceed total amount.'
            })

        # Lab bills no longer support a PARTIAL status — a bill is either
        # unpaid or paid in full (see BillPaymentStatus / LabBillPayView).
        # Block any value that would leave paid_amount stuck strictly
        # between 0 and the total, since LabBill.save() would then have no
        # valid status to represent it.
        if 0 < paid < total:
            raise serializers.ValidationError({
                'paid_amount':
                    'Partial payments are not supported for lab bills. '
                    'Use the payment endpoint to pay the full amount.'
            })

        return data


# ─────────────────────────────────────────────────────────
# Lab Equipment
# ─────────────────────────────────────────────────────────
class LabEquipmentSerializer(serializers.ModelSerializer):

    maintenance_count = serializers.SerializerMethodField()

    class Meta:
        model = LabEquipment
        fields = '__all__'
        read_only_fields = [
            'equipment_id',
            'created_at',
            'updated_at'
        ]

    def get_maintenance_count(self, obj):
        try:
            return obj.maintenance_logs.count()
        except Exception:
            return 0


# ─────────────────────────────────────────────────────────
# Lab Maintenance
# ─────────────────────────────────────────────────────────
class LabMaintenanceSerializer(serializers.ModelSerializer):

    equipment_name = serializers.CharField(
        source='equipment.name',
        read_only=True
    )

    performed_by_username = serializers.SerializerMethodField()

    class Meta:
        model = LabMaintenance
        fields = '__all__'
        read_only_fields = [
            'maintenance_id',
            'created_at',
            'updated_at'
        ]

    def get_performed_by_username(self, obj):
        if obj.performed_by:
            return obj.performed_by.username
        return None


class LabMaintenanceWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = LabMaintenance
        fields = '__all__'
        read_only_fields = [
            'maintenance_id',
            'created_at',
            'updated_at'
        ]


# ─────────────────────────────────────────────────────────
# Lab Order
# ─────────────────────────────────────────────────────────
class LabOrderSerializer(serializers.ModelSerializer):

    ordered_by_username = serializers.SerializerMethodField()

    total_cost_display = serializers.SerializerMethodField()

    class Meta:
        model = LabOrder
        fields = '__all__'
        read_only_fields = [
            'order_id',
            'total_cost',
            'created_at',
            'updated_at'
        ]

    def get_ordered_by_username(self, obj):
        if obj.ordered_by:
            return obj.ordered_by.username
        return None

    def get_total_cost_display(self, obj):
        if obj.total_cost is not None:
            return float(obj.total_cost)
        return 0


class LabOrderWriteSerializer(serializers.ModelSerializer):

    class Meta:
        model = LabOrder
        fields = '__all__'
        read_only_fields = [
            'order_id',
            'total_cost',
            'created_at',
            'updated_at'
        ]

    def validate_quantity(self, value):

        if value <= 0:
            raise serializers.ValidationError(
                "Quantity must be greater than zero."
            )

        return value

    def validate(self, data):

        unit_cost = data.get(
            'unit_cost',
            getattr(self.instance, 'unit_cost', None)
        )

        quantity = data.get(
            'quantity',
            getattr(self.instance, 'quantity', 1)
        )

        if unit_cost is not None and unit_cost < 0:
            raise serializers.ValidationError({
                'unit_cost': 'Unit cost cannot be negative.'
            })

        if quantity <= 0:
            raise serializers.ValidationError({
                'quantity': 'Quantity must be greater than zero.'
            })

        return data

# ─────────────────────────────────────────────────────────
# Lab Bill Pay  (reception collects payment)
# ─────────────────────────────────────────────────────────
class LabBillPaySerializer(serializers.Serializer):
    """
    Used by PATCH /lab/bills/<pk>/pay/ to collect payment.
    Validates paid_amount and payment_method, then marks bill PAID.
    """
    payment_method = serializers.ChoiceField(
        choices=[
            ('CASH', 'Cash'),
            ('CARD', 'Card'),
            ('UPI', 'UPI'),
            ('INSURANCE', 'Insurance'),
        ]
    )
    paid_amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=0,
    )
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, data):
        bill = self.context.get('bill')
        if bill:
            total = bill.total_amount
            paid  = data.get('paid_amount', 0)
            if total > 0 and paid < total:
                raise serializers.ValidationError({
                    'paid_amount': (
                        f'Paid amount ({paid}) is less than total '
                        f'({total}). Full payment required before sample collection.'
                    )
                })
        return data