from rest_framework import serializers
from django.utils import timezone
from decimal import Decimal
from django.db import transaction
from django.db.models import F
from authentication.utils import scope_queryset_to_branch

from .models import (
    Medicine,
    MedicineBatch,
    MedicineStockLog,
    StockAlert,
    PharmacyBill,
    PharmacyBillMedicineItem,
    PharmacyBillProcedureItem,
    MedicineReturnToProvider,
    RouteChoices,
    MedicineTypeChoices,
    SupplyItem,
    SupplyBatch,
    SupplyUsageLog,
    SupplyStockAlert,
    SupplyReturn,
    SupplyReturnReasonChoices,
    SupplyCategoryChoices,
    SupplyUnitChoices,
    GeneralItem,
    GeneralItemBatch,
    GeneralItemStockLog,
    GeneralItemStockAlert,
    GeneralItemCategoryChoices,
    PharmacyBillGeneralItem,
    GeneralItemReturn,
    GeneralItemReturnReasonChoices,
)
from reception.models import Patient


# ============================================================
# MEDICINE SERIALIZERS
# ============================================================

class MedicineSerializer(serializers.ModelSerializer):
    default_route_display = serializers.CharField(source='get_default_route_display', read_only=True)
    medicine_type_display = serializers.CharField(source='get_medicine_type_display', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, default=None)
    branch_code = serializers.CharField(source='branch.code', read_only=True, default=None)

    class Meta:
        model = Medicine
        fields = [
            'medicine_id',
            'branch',
            'branch_name',
            'branch_code',
            'name',
            'generic_name',
            'category',
            'unit',
            'medicine_type',
            'medicine_type_display',
            'strength',
            'default_route',
            'default_route_display',
            'description',
            'is_active',
        ]
        read_only_fields = ['medicine_id', 'branch']


class MedicineWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    generic_name = serializers.CharField(required=False, allow_blank=True, max_length=300)
    category = serializers.CharField(required=False, allow_blank=True, max_length=100)
    unit = serializers.CharField(required=False, allow_blank=True, max_length=50)
    medicine_type = serializers.ChoiceField(
        choices=MedicineTypeChoices.choices,
        required=False,
        default=MedicineTypeChoices.TABLET,
        help_text='Dosage form, e.g. Tablet, Capsule, Syrup, Cream, Nasal Spray.',
    )
    strength = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=100,
        help_text="Standard strength, e.g. '500mg', '250mg/5ml'.",
    )
    default_route = serializers.ChoiceField(
        choices=RouteChoices.choices,
        required=True,
        allow_blank=False,
        help_text='Default administration route for this medicine (required).',
    )
    description = serializers.CharField(required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False, default=True)

    def validate_default_route(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError('Default route is required and cannot be empty.')
        return value

    def validate_name(self, value):
        # Uniqueness is branch-scoped (Medicine.Meta.unique_together =
        # [('branch', 'name')]), so the check must be too — otherwise a
        # duplicate name in another branch either wrongly blocks this one
        # (if checked globally) or slips through to an unhandled
        # IntegrityError (if not checked at all). The branch is supplied
        # by the view via serializer context, never by the client.
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Name cannot be empty.')
        branch = self.context.get('branch')
        if branch is not None:
            qs = Medicine.objects.filter(branch=branch, name__iexact=value)
            instance = self.context.get('instance')
            if instance:
                qs = qs.exclude(pk=instance.pk)
            if qs.exists():
                raise serializers.ValidationError('A medicine with this name already exists at this branch.')
        return value


class MedicineWithBatchesSerializer(MedicineSerializer):
    batches = serializers.SerializerMethodField()
    total_stock = serializers.SerializerMethodField()

    def get_batches(self, obj):
        batches = obj.batches.all()
        return MedicineBatchDetailSerializer(batches, many=True).data
    
    def get_total_stock(self, obj):
        """Calculate total available stock across all ACTIVE batches"""
        total = 0
        for batch in obj.batches.filter(status='ACTIVE'):
            # Calculate: quantity - allocated_quantity = available
            available = (batch.quantity or 0) - (batch.allocated_quantity or 0)
            if available > 0:
                total += available
        return total

    class Meta(MedicineSerializer.Meta):
        fields = MedicineSerializer.Meta.fields + ['batches', 'total_stock']


# ============================================================
# MEDICINE BATCH SERIALIZERS
# ============================================================

# FIXED BATCH SERIALIZERS
# File: /rhimsbackend/pharmacist/serializers.py
# Lines: 105-180 (CORRECTED VERSION)

"""
CHANGES MADE:
1. MedicineBatchSerializer: Added 'allocated_quantity' and 'cost_price' to fields
2. MedicineBatchWriteSerializer: Added 'cost_price' field with validation
"""

class MedicineBatchSerializer(serializers.ModelSerializer):
    medicine_name = serializers.CharField(source='medicine.name', read_only=True)
    medicine_strength = serializers.CharField(source='medicine.strength', read_only=True)
    # ✅ FIX: expose the medicine's knowledge fields so client-side search
    # (e.g. AddMedicineForm on the Bills page) can actually match against
    # generic name, category, and description — previously these were
    # missing here, so those fields were always undefined on the frontend
    # and only name/batch_number search worked.
    # Plain CharField(source=...) would serialize a NULL db value as the
    # literal string "None" (DRF only applies `default` when the attribute
    # is missing, not when it's None) — these fields are null=True on the
    # model, so a method field is used to safely coerce None -> ''.
    medicine_generic_name = serializers.SerializerMethodField()
    medicine_category = serializers.SerializerMethodField()
    medicine_description = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    dealer_name = serializers.CharField(source='dealer.name', read_only=True, default=None)

    class Meta:
        model = MedicineBatch
        fields = [
            'batch_id',
            'medicine',
            'medicine_name',
            'medicine_strength',
            'medicine_generic_name',
            'medicine_category',
            'medicine_description',
            'batch_number',
            'quantity',
            'allocated_quantity',        # ✅ NEW: Required for showing reserved stock
            'cost_price',                # ✅ NEW: Required for showing purchase cost
            'mrp',
            'gst_percentage',
            'expiry_date',
            'status',
            'status_display',
            'low_stock_threshold',
            'dealer',                    # OPTIONAL: dealer this stock was bought from
            'dealer_name',
            'settlement_method',         # OPTIONAL: CREDIT / PAID
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'batch_id',
            'medicine_name',
            'medicine_strength',
            'status_display',
            'dealer_name',
            'created_at',
            'updated_at',
            'allocated_quantity',        # ✅ Make read-only (auto-managed by bills)
            # Note: medicine_generic_name/category/description are
            # SerializerMethodFields, which are read-only by definition —
            # listing them here would raise a DRF assertion error.
        ]

    def get_medicine_generic_name(self, obj):
        return obj.medicine.generic_name or ''

    def get_medicine_category(self, obj):
        return obj.medicine.category or ''

    def get_medicine_description(self, obj):
        return obj.medicine.description or ''

    def validate_quantity(self, value):
        if value < 0:
            raise serializers.ValidationError("Quantity cannot be negative.")
        return value


class MedicineBatchDetailSerializer(MedicineBatchSerializer):
    """Same as MedicineBatchSerializer - no changes needed"""
    pass


class MedicineBatchWriteSerializer(serializers.Serializer):
    medicine_id = serializers.IntegerField(required=True)
    batch_number = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=100,
        help_text='Batch number must be entered manually (required)'
    )
    quantity = serializers.IntegerField(required=True, min_value=0)
    cost_price = serializers.DecimalField(                           # ✅ NEW: Allow cost_price updates
        required=True,
        max_digits=10,
        decimal_places=2,
        help_text='Cost price per unit (wholesale price)'
    )
    mrp = serializers.DecimalField(required=True, max_digits=10, decimal_places=2)
    gst_percentage = serializers.DecimalField(
        required=False,
        max_digits=5,
        decimal_places=2,
        default=0
    )
    expiry_date = serializers.DateField(
        required=True,
        allow_null=False,
        help_text='Expiry date is required for all batches'
    )
    low_stock_threshold = serializers.IntegerField(required=False, min_value=0, default=10)
    status = serializers.ChoiceField(
        required=False,
        choices=['ACTIVE', 'EXPIRED', 'DEPLETED'],
        default='ACTIVE',
    )
    # OPTIONAL — see MedicineBatch.dealer/settlement_method in models.py.
    # Leaving these unset behaves exactly as before this feature existed.
    dealer_id = serializers.IntegerField(required=False, allow_null=True)
    settlement_method = serializers.ChoiceField(
        required=False, allow_null=True,
        choices=['CREDIT', 'PAID'],
    )

    def validate_batch_number(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError('Batch number cannot be empty.')
        return value.strip()

    def validate_cost_price(self, value):                           # ✅ NEW: Validate cost_price
        if value < 0:
            raise serializers.ValidationError('Cost price cannot be negative.')
        return value

    def validate(self, data):
        """Cross-field validation: pricing + duplicate batch number check."""
        cost_price = data.get('cost_price')
        mrp = data.get('mrp')

        if cost_price and mrp and cost_price > mrp:
            raise serializers.ValidationError(
                {
                    'cost_price': f'Cost price (₹{cost_price}) cannot exceed MRP (₹{mrp})'
                }
            )

        # Pre-check batch_number uniqueness per medicine so the user gets a
        # clear 400 with a field-level message instead of a DB-level 409.
        # Skip during partial updates (PATCH) — the batch already exists.
        #
        # Scoped to the caller's own branch (via serializer context, set
        # by the view) rather than a raw global query — otherwise this
        # existence check would silently query/leak whether a matching
        # batch number exists at a different branch's medicine record.
        medicine_id = data.get('medicine_id')
        batch_number = data.get('batch_number')
        if medicine_id and batch_number:
            dup_qs = scope_queryset_to_branch(
                MedicineBatch.objects.filter(medicine_id=medicine_id, batch_number=batch_number),
                self.context.get('user'), branch_field='medicine__branch',
            )
            if dup_qs.exists():
                raise serializers.ValidationError(
                    {
                        'batch_number': (
                            f'A batch with number "{batch_number}" already exists '
                            f'for this medicine. Use a different batch number, '
                            f'or edit the existing batch instead.'
                        )
                    }
                )

        return data
# ============================================================
# MEDICINE STOCK LOG SERIALIZERS
# ============================================================

class MedicineStockLogSerializer(serializers.ModelSerializer):
    batch_medicine = serializers.CharField(source='batch.medicine.name', read_only=True)
    batch_number = serializers.CharField(source='batch.batch_number', read_only=True)
    change_type_display = serializers.CharField(source='get_change_type_display', read_only=True)

    class Meta:
        model = MedicineStockLog
        fields = [
            'log_id',
            'batch',
            'batch_medicine',
            'batch_number',
            'change_type',
            'change_type_display',
            'quantity_changed',
            'remarks',
            'created_at',
        ]
        read_only_fields = [
            'log_id',
            'batch_medicine',
            'batch_number',
            'change_type_display',
            'created_at',
        ]


# ============================================================
# STOCK ALERT SERIALIZERS
# ============================================================

class StockAlertSerializer(serializers.ModelSerializer):
    batch_medicine = serializers.CharField(source='batch.medicine.name', read_only=True)
    batch_number = serializers.CharField(source='batch.batch_number', read_only=True)
    alert_type_display = serializers.CharField(source='get_alert_type_display', read_only=True)

    class Meta:
        model = StockAlert
        fields = [
            'alert_id',
            'batch',
            'batch_medicine',
            'batch_number',
            'alert_type',
            'alert_type_display',
            'is_resolved',
            'created_at',
        ]
        read_only_fields = [
            'alert_id',
            'batch_medicine',
            'batch_number',
            'alert_type_display',
            'created_at',
        ]


# ============================================================
# PHARMACY BILL MEDICINE ITEM SERIALIZERS
# ============================================================

class PharmacyBillMedicineItemSerializer(serializers.ModelSerializer):
    medicine_name = serializers.CharField(source='batch.medicine.name', read_only=True)
    medicine_strength = serializers.CharField(source='batch.medicine.strength', read_only=True, allow_null=True)
    medicine_type = serializers.CharField(source='batch.medicine.medicine_type', read_only=True)
    medicine_type_display = serializers.CharField(source='batch.medicine.get_medicine_type_display', read_only=True)
    batch_number = serializers.CharField(source='batch.batch_number', read_only=True)
    available_stock = serializers.SerializerMethodField()
    is_dispensed = serializers.BooleanField(read_only=True)

    # Prescription context fields
    route = serializers.CharField(source='prescription_item.route', read_only=True, allow_null=True)
    route_display = serializers.CharField(source='prescription_item.get_route_display', read_only=True, allow_null=True)
    frequency = serializers.CharField(source='prescription_item.frequency', read_only=True, allow_null=True)
    frequency_display = serializers.CharField(source='prescription_item.get_frequency_display', read_only=True, allow_null=True)
    duration_days = serializers.IntegerField(source='prescription_item.duration_days', read_only=True, allow_null=True)
    dose_quantity = serializers.DecimalField(source='prescription_item.dose_quantity', max_digits=5, decimal_places=2, read_only=True, allow_null=True)
    meal_timing = serializers.CharField(source='prescription_item.meal_timing', read_only=True, allow_null=True)
    meal_timing_display = serializers.CharField(source='prescription_item.get_meal_timing_display', read_only=True, allow_null=True)
    instructions = serializers.CharField(source='prescription_item.instructions', read_only=True, allow_null=True)

    def get_available_stock(self, obj):
        return obj.batch.quantity

    class Meta:
        model = PharmacyBillMedicineItem
        fields = [
            'item_id',
            'bill',
            'batch',
            'prescription_item',
            'medicine_name',
            'medicine_strength',
            'medicine_type',
            'medicine_type_display',
            'batch_number',
            'quantity',
            'unit_mrp',
            'gst_percentage',
            'item_total',
            'available_stock',
            'is_dispensed',
            'route',
            'route_display',
            'frequency',
            'frequency_display',
            'duration_days',
            'dose_quantity',
            'meal_timing',
            'meal_timing_display',
            'instructions',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'item_id',
            'medicine_name',
            'medicine_strength',
            'medicine_type',
            'medicine_type_display',
            'batch_number',
            'unit_mrp',
            'gst_percentage',
            'item_total',
            'available_stock',
            'is_dispensed',
            'route',
            'route_display',
            'frequency',
            'frequency_display',
            'duration_days',
            'dose_quantity',
            'meal_timing',
            'meal_timing_display',
            'instructions',
            'created_at',
            'updated_at',
        ]


class PharmacyBillProcedureItemSerializer(serializers.ModelSerializer):
    procedure_name = serializers.CharField(source='display_name', read_only=True)
    is_manual = serializers.SerializerMethodField()

    class Meta:
        model = PharmacyBillProcedureItem
        fields = [
            'item_id',
            'bill',
            'procedure',
            'procedure_name',
            'manual_description',
            'is_manual',
            'quantity',
            'unit_charge',
            'item_total',
        ]
        read_only_fields = ['item_id', 'procedure_name', 'is_manual', 'unit_charge', 'item_total']

    def get_is_manual(self, obj):
        return obj.procedure_id is None


# ============================================================
# PHARMACY BILL SERIALIZERS
# ============================================================

class PharmacyBillSerializer(serializers.ModelSerializer):
    medicine_items = PharmacyBillMedicineItemSerializer(many=True, read_only=True)
    procedure_items = PharmacyBillProcedureItemSerializer(many=True, read_only=True)
    general_items = serializers.SerializerMethodField()
    is_dispensed = serializers.BooleanField(read_only=True)
    can_add_items = serializers.SerializerMethodField()
    dispense_status = serializers.SerializerMethodField()
    patient_info = serializers.SerializerMethodField()

    def get_general_items(self, obj):
        # Local import avoids a circular reference at module load time
        # (PharmacyBillGeneralItemSerializer is defined further down this file).
        return PharmacyBillGeneralItemSerializer(obj.general_items.all(), many=True).data
    
    # ✓ NEW: Dispensing confirmation fields
    dispensing_confirmed = serializers.BooleanField(read_only=True)
    dispensing_confirmed_at = serializers.DateTimeField(read_only=True)
    dispensing_confirmed_by = serializers.StringRelatedField(read_only=True)

    # ← NEW: send-to-reception
    sent_to_reception = serializers.BooleanField(read_only=True)
    sent_to_reception_at = serializers.DateTimeField(read_only=True)
    sent_to_reception_by = serializers.StringRelatedField(read_only=True)

    def get_can_add_items(self, obj):
        return obj.can_add_items()

    def get_dispense_status(self, obj):
        medicine_items = obj.medicine_items.all()
        general_items = obj.general_items.all()
        return {
            'is_dispensed': obj.is_dispensed,
            'dispensed_date': obj.updated_at if obj.is_dispensed else None,
            'bill_status': obj.bill_status,
            'payment_status': obj.payment_status,
            'total_medicine_items': medicine_items.count(),
            'dispensed_items_count': medicine_items.filter(is_dispensed=True).count() + general_items.filter(is_dispensed=True).count(),
            'pending_items_count': medicine_items.filter(is_dispensed=False).count() + general_items.filter(is_dispensed=False).count(),
            'note': (
                'Medicines will be physically dispensed and stock deducted when bill is marked PAID.'
                if not obj.is_dispensed
                else 'Medicines have been physically dispensed and stock deducted.'
            ),
        }

    def get_patient_info(self, obj):
        return obj.get_patient_info_dict()

    class Meta:
        model = PharmacyBill
        fields = [
            'bill_id',
            'bill_number',
            'patient',
            'patient_name',
            'consultation_bill',
            'prescription',
            'is_walkin',
            'walkin_name',
            'walkin_phone',
            'walkin_gender',
            'walkin_age',
            'patient_info',
            'bill_date',
            'bill_status',
            'payment_status',
            'payment_method',
            'upi_reference',
            'bill_type',
            'subtotal',
            'gst_amount',
            'margin_adjustment',
            'discount_amount',
            'total_amount',
            'is_dispensed',
            'can_add_items',
            'dispense_status',
            'medicine_items',
            'procedure_items',
            'general_items',
            'notes',
            'dispensing_confirmed',          # ← NEW
            'dispensing_confirmed_at',       # ← NEW
            'dispensing_confirmed_by',       # ← NEW
            'sent_to_reception',              # ← NEW
            'sent_to_reception_at',           # ← NEW
            'sent_to_reception_by',           # ← NEW
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'bill_id',
            'bill_number',
            'bill_date',
            'subtotal',
            'gst_amount',
            'discount_amount',
            'total_amount',
            'is_dispensed',
            'can_add_items',
            'dispense_status',
            'patient_info',
        ]


# ============================================================
# MEDICINE RETURN SERIALIZERS
# ============================================================



# ============================================================
# BILL OPERATION SERIALIZERS
# ============================================================

class CreateBillSerializer(serializers.Serializer):
    patient_type = serializers.ChoiceField(
        choices=['registered', 'walkin'],
        required=True,
        help_text="'registered' for MRD patient, 'walkin' for walk-in customer",
    )
    patient_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        help_text='Patient primary key for registered patient lookup',
    )
    mrd_number = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=20,
        help_text="MRD number for registered patient lookup (e.g. 'MRD-0001')",
    )
    prescription_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        help_text='Prescription ID to create bill from a doctor prescription',
    )
    auto_add_medicines = serializers.BooleanField(
        required=False,
        default=False,
        help_text='Auto-add prescribed medicines when creating bill from a prescription',
    )
    walkin_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=300,
        help_text="Walk-in patient's full name (required for walkin type)",
    )
    walkin_phone = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=10,
        help_text="Walk-in patient's phone number (optional, must be exactly 10 digits)",
    )
    walkin_gender = serializers.ChoiceField(
        choices=['Male', 'Female', 'Other'],
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Walk-in patient's gender (optional)",
    )
    walkin_age = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=0,
        max_value=150,
        help_text="Walk-in patient's age (optional)",
    )
    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
        help_text='Optional notes for the bill',
    )

    def validate_walkin_phone(self, value):
        if value:
            value = value.strip()
            if len(value) != 10 or not value.isdigit():
                raise serializers.ValidationError('Phone must be exactly 10 digits.')
        return value

    def validate_mrd_number(self, value):
        if value:
            return value.strip()
        return value

    def validate(self, data):
        patient_type = data.get('patient_type')

        if patient_type == 'registered':
            patient_id = data.get('patient_id')
            mrd_number = data.get('mrd_number') or ''
            prescription_id = data.get('prescription_id')

            if not any([patient_id, mrd_number.strip(), prescription_id]):
                raise serializers.ValidationError(
                    'For registered patient, provide at least one of: '
                    'patient_id, mrd_number, or prescription_id.'
                )

            if any([
                data.get('walkin_name'),
                data.get('walkin_phone'),
                data.get('walkin_gender'),
                data.get('walkin_age') is not None,
            ]):
                raise serializers.ValidationError(
                    'Walk-in fields (walkin_name, walkin_phone, walkin_gender, walkin_age) '
                    'must not be provided for registered patients.'
                )

        elif patient_type == 'walkin':
            walkin_name = (data.get('walkin_name') or '').strip()
            if not walkin_name:
                raise serializers.ValidationError({
                    'walkin_name': 'walkin_name is required for walk-in patient type.'
                })

            if any([
                data.get('patient_id'),
                data.get('mrd_number'),
                data.get('prescription_id'),
            ]):
                raise serializers.ValidationError(
                    'Registered patient fields (patient_id, mrd_number, prescription_id) '
                    'must not be provided for walk-in patients.'
                )

            if data.get('auto_add_medicines'):
                raise serializers.ValidationError(
                    'auto_add_medicines cannot be used with walk-in patients '
                    '(no prescription to auto-add from).'
                )

        return data


class AddMedicineItemSerializer(serializers.Serializer):
    batch_id = serializers.IntegerField(required=True)
    quantity = serializers.IntegerField(required=True, min_value=1)
    # Optional: when this item is being added from a doctor's prescription
    # (e.g. the "Prescribed Medicines" quick-add list), pass the source
    # PrescriptionItem id here so the bill line carries its dose/frequency/
    # meal-timing/duration for display on the bill and printed receipt.
    prescription_item_id = serializers.IntegerField(required=False, allow_null=True)


class AddProcedureItemSerializer(serializers.Serializer):
    # ✅ FIX: procedure_id is now optional — see AddProcedureItemView for why.
    procedure_id = serializers.IntegerField(required=False, allow_null=True)
    quantity     = serializers.IntegerField(required=False, min_value=1, default=1)

    # ✅ FIX: "instant procedure" support. The frontend's "create new
    # procedure" form (WalkInBillPage.jsx handleCreateAndAddProcedure)
    # sends `description` + `amount` instead of an existing procedure_id
    # — there was no backend support for that at all, so it always failed
    # with "procedure_id: This field is required." Now: if procedure_id is
    # omitted, description + amount create (or reuse) a catalog Procedure
    # on the fly.
    description = serializers.CharField(required=False, allow_blank=True, max_length=200)
    amount = serializers.DecimalField(required=False, max_digits=10, decimal_places=2, min_value=0)

    def validate(self, data):
        if not data.get('procedure_id'):
            description = (data.get('description') or '').strip()
            amount = data.get('amount')
            if not description or amount is None:
                raise serializers.ValidationError(
                    'Provide either procedure_id, or both description and amount to create a new procedure.'
                )
        return data


class PayBillSerializer(serializers.Serializer):
    PAYMENT_METHOD_CHOICES = [
        ('CASH', 'Cash'),
        ('CARD', 'Card'),
        ('UPI', 'UPI'),
        ('CHEQUE', 'Cheque'),
    ]

    payment_method = serializers.ChoiceField(choices=PAYMENT_METHOD_CHOICES, required=False)
    upi_reference = serializers.CharField(required=False, allow_blank=True)

    def validate(self, data):
        if data.get('payment_method') == 'UPI' and not data.get('upi_reference'):
            raise serializers.ValidationError(
                'upi_reference is required when payment_method is UPI.'
            )
        return data


class MarkBillPaidSerializer(PayBillSerializer):
    """Alias for PayBillSerializer with required payment_method for marking bills as paid."""
    pass


class CancelBillSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)


class ReopenBillSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)


class SetBillDiscountSerializer(serializers.Serializer):
    """
    Input serializer for POST /api/pharmacist/bills/<id>/discount/.

    Flat-amount discount only (not a percentage). Cross-checked against
    the bill's current subtotal in the view, since subtotal isn't known
    until the bill (and its items) are loaded.
    """
    discount_amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=0,
        help_text='Flat discount amount to apply to this bill (not a percentage).',
    )


# ============================================================
# PATIENT SEARCH SERIALIZER
# ============================================================

class PatientSearchResultSerializer(serializers.Serializer):
    patient_id = serializers.IntegerField(read_only=True)
    mrd_number = serializers.CharField(read_only=True)
    first_name = serializers.CharField(read_only=True)
    last_name = serializers.CharField(read_only=True)
    phone = serializers.CharField(read_only=True)
    place = serializers.CharField(read_only=True)

    # pharmacist/serializers.py - FIXED RETURN TO PROVIDER SECTION
# ═════════════════════════════════════════════════════════════════════════════
# ✅ FIXED: Proper serializer for creating returns with medicine/batch/qty selection
# ═════════════════════════════════════════════════════════════════════════════



# ═════════════════════════════════════════════════════════════════════════════
# RETURN TO PROVIDER SERIALIZERS
# ═════════════════════════════════════════════════════════════════════════════

class MedicineReturnToProviderSerializer(serializers.ModelSerializer):
    """
    ✅ FIXED: Full serializer for displaying return to provider records
    
    Shows:
    - Medicine details (via batch.medicine)
    - Batch details
    - Quantity being returned
    - Reason for return (both code and display)
    - Current status
    - User who requested/approved
    - Timestamps
    """
    
    # ✅ Medicine information (derived from batch)
    batch_medicine = serializers.CharField(
        source='batch.medicine.name',
        read_only=True,
        help_text="Name of the medicine"
    )
    batch_medicine_id = serializers.IntegerField(
        source='batch.medicine.medicine_id',
        read_only=True,
        help_text="ID of the medicine"
    )
    batch_generic_name = serializers.CharField(
        source='batch.medicine.generic_name',
        read_only=True,
        allow_null=True,
        help_text="Generic name of the medicine"
    )
    
    # ✅ Batch information
    batch_number = serializers.CharField(
        source='batch.batch_number',
        read_only=True,
        help_text="Batch number"
    )
    batch_status = serializers.CharField(
        source='batch.status',
        read_only=True,
        help_text="Current status of the batch"
    )
    expiry_date = serializers.DateField(
        source='batch.expiry_date',
        read_only=True,
        allow_null=True,
        help_text="Batch expiry date"
    )
    
    # ✅ Quantity and pricing info
    available_quantity = serializers.SerializerMethodField(
        help_text="Quantity available for return (not allocated)"
    )
    mrp = serializers.DecimalField(
        source='batch.mrp',
        read_only=True,
        max_digits=10,
        decimal_places=2,
        help_text="MRP of the medicine (selling price — not what the refund is based on)"
    )
    cost_price = serializers.DecimalField(
        source='batch.cost_price',
        read_only=True,
        max_digits=10,
        decimal_places=2,
        help_text="Purchase (cost) price per unit — what the refund is actually based on"
    )
    
    # ✅ Reason display
    reason_display = serializers.CharField(
        source='get_reason_display',
        read_only=True,
        help_text="Human-readable reason"
    )
    
    # ✅ Status display
    status_display = serializers.CharField(
        source='get_status_display',
        read_only=True,
        help_text="Human-readable status"
    )
    
    # ✅ User information
    requested_by_name = serializers.CharField(
        source='requested_by.username',
        read_only=True,
        allow_null=True,
        help_text="Username of pharmacist who requested return"
    )
    requested_by_full_name = serializers.SerializerMethodField(
        help_text="Full name of pharmacist who requested return"
    )
    
    approved_by_name = serializers.CharField(
        source='approved_by.username',
        read_only=True,
        allow_null=True,
        help_text="Username of manager who approved/rejected"
    )
    approved_by_full_name = serializers.SerializerMethodField(
        help_text="Full name of manager who approved/rejected"
    )

    # OPTIONAL dealer link — see MedicineReturnToProvider.dealer in models.py
    dealer_name = serializers.CharField(source='dealer.name', read_only=True, default=None)

    class Meta:
        model = MedicineReturnToProvider
        fields = [
            'return_id',
            'batch',
            'batch_medicine',
            'batch_medicine_id',
            'batch_generic_name',
            'batch_number',
            'batch_status',
            'expiry_date',
            'quantity',
            'available_quantity',
            'mrp',
            'cost_price',
            'refund_amount',
            'reason',
            'reason_display',
            'reason_details',
            'status',
            'status_display',
            'dealer',
            'dealer_name',
            'settlement_method',
            'requested_by',
            'requested_by_name',
            'requested_by_full_name',
            'approved_by',
            'approved_by_name',
            'approved_by_full_name',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'return_id',
            'batch_medicine',
            'batch_medicine_id',
            'batch_generic_name',
            'batch_number',
            'batch_status',
            'expiry_date',
            'available_quantity',
            'mrp',
            'cost_price',
            'refund_amount',
            'reason_display',
            'status_display',
            'dealer_name',
            'requested_by_name',
            'requested_by_full_name',
            'approved_by_name',
            'approved_by_full_name',
            'created_at',
            'updated_at',
        ]
    
    def get_available_quantity(self, obj):
        """Calculate available quantity for return"""
        if obj.batch:
            return obj.batch.get_available_quantity()
        return 0
    
    def get_requested_by_full_name(self, obj):
        """Get full name of requesting user"""
        if obj.requested_by:
            return f"{obj.requested_by.first_name} {obj.requested_by.last_name}".strip()
        return None
    
    def get_approved_by_full_name(self, obj):
        """Get full name of approving user"""
        if obj.approved_by:
            return f"{obj.approved_by.first_name} {obj.approved_by.last_name}".strip()
        return None
    
    def validate(self, data):
        """Validate the return data"""
        if data.get('quantity', 0) <= 0:
            raise serializers.ValidationError({
                'quantity': 'Quantity must be greater than zero.'
            })
        return data
 
 
class MedicineReturnToProviderCreateSerializer(serializers.Serializer):
    """
    ✅ FIXED: Input serializer for creating return to provider records
    
    User provides:
    - batch_id: ID of the batch to return
    - quantity: Number of units to return
    - reason: Reason code (EXPIRED, DAMAGED, DEFECTIVE, RECALL, OTHER)
    - reason_details: Optional additional details
    
    This serializer:
    1. Validates all inputs
    2. Checks batch exists
    3. Checks quantity is available
    4. Creates the return record with proper status and user info
    """
    
    batch_id = serializers.IntegerField(
        min_value=1,
        help_text="ID of the medicine batch to return"
    )
    quantity = serializers.IntegerField(
        min_value=1,
        help_text="Number of units to return"
    )
    reason = serializers.ChoiceField(
        choices=['EXPIRED', 'DAMAGED', 'DEFECTIVE', 'RECALL', 'OTHER'],
        help_text="Reason for return: EXPIRED, DAMAGED, DEFECTIVE, RECALL, or OTHER"
    )
    reason_details = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional detailed explanation"
    )
    # OPTIONAL — see MedicineReturnToProvider.dealer/settlement_method in
    # models.py. Leaving these unset behaves exactly as before this
    # feature existed (a plain return with no ledger entry).
    dealer_id = serializers.IntegerField(required=False, allow_null=True)
    settlement_method = serializers.ChoiceField(
        required=False, allow_null=True,
        choices=['CREDIT', 'REFUND', 'EXCHANGE'],
    )
    
    def validate_batch_id(self, value):
        """Validate batch exists and belongs to the requester's branch."""
        qs = self._scoped_batches()
        try:
            batch = qs.get(pk=value)
            if batch.status == 'DEPLETED':
                raise serializers.ValidationError("Cannot return from depleted batch")
            return value
        except MedicineBatch.DoesNotExist:
            raise serializers.ValidationError("Batch not found")

    def _scoped_batches(self):
        # Branch comes from serializer context (set by the view via
        # scope_queryset_to_branch), so a non-group-admin pharmacist can
        # never return stock from another branch's batch just by
        # guessing/incrementing a batch_id.
        qs = MedicineBatch.objects.all()
        branch = self.context.get('branch')
        if branch is not None:
            qs = qs.filter(medicine__branch=branch)
        return qs

    def validate(self, data):
        """
        ✅ FIXED: Validate quantity is available
        Checks that requested quantity doesn't exceed available (non-allocated) stock
        """
        batch_id = data.get('batch_id')
        quantity = data.get('quantity')
        
        try:
            batch = self._scoped_batches().get(pk=batch_id)
        except MedicineBatch.DoesNotExist:
            raise serializers.ValidationError({
                'batch_id': 'Batch not found'
            })
        
        available = batch.get_available_quantity()
        if quantity > available:
            raise serializers.ValidationError({
                'quantity': f'Only {available} units available for return. Requested: {quantity}'
            })

        dealer_id = data.get('dealer_id')
        if dealer_id:
            # Scoped to this batch's branch — same rule as every other
            # dealer lookup in this module (BatchCreateView, AddStockView,
            # ReturnToProviderView, GeneralItemBatchCreateView, and the
            # general-item return view all require dealer.branch to match).
            from manager.models import Dealer
            if not Dealer.objects.filter(pk=dealer_id, branch=batch.medicine.branch).exists():
                raise serializers.ValidationError({'dealer_id': 'Dealer not found'})

        return data
    
    @transaction.atomic
    def save(self, requested_by=None):
        """
        Create the return-to-provider record AND deduct stock immediately.

        The frontend flow is a single-step action (pharmacist hands the
        stock back to the provider right away) - there is no separate
        approve/complete step anywhere in the UI. Deduction is therefore
        done here, atomically, using an F()-based .update() so
        MedicineBatch.save()'s full_clean() is never invoked (that was
        the original 500 trigger).
        """
        # Re-fetch through the same branch-scoped queryset used in
        # validate() rather than a raw MedicineBatch.objects.get() — this
        # is the actual stock-deduction step, so it must not depend on
        # validate() having already gated it; it re-verifies branch
        # ownership itself.
        batch = self._scoped_batches().select_for_update().get(
            pk=self.validated_data['batch_id']
        )
        return_quantity = self.validated_data['quantity']

        # Re-check availability under the row lock (race-condition guard)
        available_qty = batch.get_available_quantity()
        if return_quantity > available_qty:
            raise serializers.ValidationError(
                f"Only {available_qty} units available. Requested: {return_quantity}"
            )

        # Deduct stock immediately via an atomic update - bypasses
        # MedicineBatch.save()/full_clean() entirely.
        MedicineBatch.objects.filter(pk=batch.pk).update(
            quantity=F('quantity') - return_quantity
        )
        batch.refresh_from_db()
        if batch.quantity <= 0:
            MedicineBatch.objects.filter(pk=batch.pk).update(status='DEPLETED')
            batch.status = 'DEPLETED'

        medicine_return = MedicineReturnToProvider.objects.create(
            batch=batch,
            quantity=return_quantity,
            refund_amount=return_quantity * batch.cost_price,
            reason=self.validated_data['reason'],
            reason_details=self.validated_data.get('reason_details', ''),
            # Stock is already deducted above and this is a single-step
            # action (no separate approve/complete step in the UI — see
            # note on save() above), so the record should be created as
            # COMPLETED. Leaving this as REQUESTED made the status badge
            # say "pending" even though the refund_amount was already
            # being counted as recovered money on the finance dashboard.
            status='COMPLETED',
            requested_by=requested_by,
            dealer_id=self.validated_data.get('dealer_id') or None,
            settlement_method=self.validated_data.get('settlement_method') or None,
        )

        MedicineStockLog.objects.create(
            batch=batch,
            change_type='RETURN',
            quantity_changed=-return_quantity,
            remarks=f"Returned to provider - {medicine_return.get_reason_display()} (Return ID: {medicine_return.return_id})",
        )

        # OPTIONAL dealer link → auto-log a PENDING credit-note transaction
        # for the manager to review/finalize on the Dealers page.
        if medicine_return.dealer_id:
            from manager.models import create_dealer_transaction
            create_dealer_transaction(
                dealer=medicine_return.dealer,
                transaction_type='CREDIT_NOTE',
                amount=medicine_return.refund_amount,
                source_model='MEDICINE_RETURN',
                source_id=medicine_return.return_id,
                created_by=requested_by,
                settlement_method=medicine_return.settlement_method,
                notes=f"Return: {batch.medicine.name} × {return_quantity} "
                      f"({medicine_return.get_reason_display()}), batch {batch.batch_number}.",
            )

        return medicine_return
 
 
# ═════════════════════════════════════════════════════════════════════════════
# DELETE THESE (DUPLICATE DEFINITIONS - LINES 496-549)
# ═════════════════════════════════════════════════════════════════════════════
 
# ❌ DELETE THESE LINES: 496-549
# - First MedicineReturnToProviderSerializer (line 496)
# - First MedicineReturnToProviderCreateSerializer (line 544)
 
# The NEWER implementations have:
# - More complete field definitions
# - Better error messages
# - Proper validation logic
# - Stock log creation
# - User tracking
 
# ═════════════════════════════════════════════════════════════════════════════
# CLEANUP INSTRUCTIONS
# ═════════════════════════════════════════════════════════════════════════════
 
# 1. Open pharmacist/serializers.py
# 2. Find line 496: "class MedicineReturnToProviderSerializer(serializers.ModelSerializer):"
# 3. Delete from line 496 to line 549 (where the first MedicineReturnToProviderCreateSerializer ends)
# 4. Run: python manage.py check
# 5. Verify no import errors
 
# VERIFICATION:
# grep "^class MedicineReturnToProviderSerializer" pharmacist/serializers.py
# Should output: 1 occurrence
# grep "^class MedicineReturnToProviderCreateSerializer" pharmacist/serializers.py
# Should output: 1 occurrence
 
# ═════════════════════════════════════════════════════════════════════════════
# KEY DIFFERENCES - WHY KEEP THE SECOND IMPLEMENTATION
# ═════════════════════════════════════════════════════════════════════════════
 
# FIRST VERSION (DELETE):
# - Minimal field definitions
# - No available_quantity calculation
# - No full name fields
# - Limited error messages
# - Simple validation
 
# SECOND VERSION (KEEP):
# - All medicine and batch details
# - Calculates available_quantity
# - Shows full names of users
# - Comprehensive error messages
# - Stock log creation in save()
# - Proper status values (REQUESTED not PENDING)
# - Better help text for all fields

# ═════════════════════════════════════════════════════════════════════════
# SUPPLIES & CONSUMABLES SERIALIZERS
# ═════════════════════════════════════════════════════════════════════════

class SupplyItemSerializer(serializers.ModelSerializer):
    """Read serializer for the supply catalogue."""
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    unit_display = serializers.CharField(source='get_unit_display', read_only=True)
    total_stock = serializers.SerializerMethodField()
    total_stock_value = serializers.SerializerMethodField()
    is_low_stock = serializers.SerializerMethodField()
    batch_count = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source='branch.name', read_only=True, default=None)
    branch_code = serializers.CharField(source='branch.code', read_only=True, default=None)

    class Meta:
        model = SupplyItem
        fields = [
            'item_id', 'branch', 'branch_name', 'branch_code',
            'name', 'category', 'category_display', 'unit', 'unit_display',
            'description', 'low_stock_threshold', 'is_active',
            'total_stock', 'total_stock_value', 'is_low_stock', 'batch_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['item_id', 'branch', 'created_at', 'updated_at']

    def get_total_stock(self, obj):
        return obj.total_stock

    def get_total_stock_value(self, obj):
        return obj.total_stock_value

    def get_is_low_stock(self, obj):
        return obj.is_low_stock

    def get_batch_count(self, obj):
        return obj.batches.filter(quantity__gt=0).count()


class SupplyItemWriteSerializer(serializers.Serializer):
    """Create/update serializer for a supply catalogue entry."""
    name = serializers.CharField(required=True, max_length=200)
    category = serializers.ChoiceField(required=True, choices=SupplyCategoryChoices.choices)
    unit = serializers.ChoiceField(required=True, choices=SupplyUnitChoices.choices)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    low_stock_threshold = serializers.IntegerField(required=False, min_value=0, default=10)
    is_active = serializers.BooleanField(required=False, default=True)

    def validate_name(self, value):
        # Branch-scoped, matching SupplyItem.Meta.unique_together =
        # [('branch', 'name')] — was previously checked globally, which
        # both blocked legitimate duplicate names across branches and
        # missed same-branch duplicates for a group admin passing an
        # explicit branch. Branch comes from serializer context (set by
        # the view via resolve_branch_for_write), never from the client.
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Name cannot be empty.')
        branch = self.context.get('branch')
        if branch is not None:
            qs = SupplyItem.objects.filter(branch=branch, name__iexact=value)
            instance = self.context.get('instance')
            if instance:
                qs = qs.exclude(pk=instance.pk)
            if qs.exists():
                raise serializers.ValidationError('A supply item with this name already exists at this branch.')
        return value


class SupplyBatchSerializer(serializers.ModelSerializer):
    """Read serializer for a supply batch."""
    supply_item_name = serializers.CharField(source='supply_item.name', read_only=True)
    received_by_name = serializers.SerializerMethodField()
    remaining_percentage = serializers.SerializerMethodField()
    is_expiring_soon = serializers.BooleanField(read_only=True)
    dealer_name = serializers.CharField(source='dealer.name', read_only=True, default=None)

    class Meta:
        model = SupplyBatch
        fields = [
            'batch_id', 'batch_number', 'supply_item', 'supply_item_name',
            'quantity', 'original_quantity', 'unit_cost', 'total_cost',
            'supplier_name', 'supplier_contact', 'invoice_number',
            'dealer', 'dealer_name', 'settlement_method',
            'purchase_date', 'expiry_date', 'notes',
            'received_by', 'received_by_name',
            'remaining_percentage', 'is_expiring_soon',
            'created_at',
        ]
        read_only_fields = ['batch_id', 'original_quantity', 'total_cost', 'dealer_name', 'created_at']

    def get_received_by_name(self, obj):
        if not obj.received_by:
            return None
        return obj.received_by.get_full_name() or obj.received_by.username

    def get_remaining_percentage(self, obj):
        return obj.remaining_percentage


class SupplyBatchWriteSerializer(serializers.Serializer):
    """Create serializer for adding stock (a new batch/purchase).

    All receipt details are mandatory so every batch carries a full,
    auditable paper trail (who supplied it, what it cost, when it expires).
    """
    batch_number = serializers.CharField(required=True, max_length=100)
    quantity = serializers.IntegerField(required=True, min_value=1)
    unit_cost = serializers.DecimalField(required=True, max_digits=10, decimal_places=2, min_value=0)
    supplier_name = serializers.CharField(required=True, max_length=200)
    supplier_contact = serializers.CharField(required=True, max_length=100)
    invoice_number = serializers.CharField(required=True, max_length=100)
    purchase_date = serializers.DateField(required=True)
    expiry_date = serializers.DateField(required=True)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    # OPTIONAL — see SupplyBatch.dealer/settlement_method in models.py.
    # Leaving these unset behaves exactly as before this feature existed.
    dealer_id = serializers.IntegerField(required=False, allow_null=True)
    settlement_method = serializers.ChoiceField(
        required=False, allow_null=True,
        choices=['CREDIT', 'PAID'],
    )

    def validate_batch_number(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Batch/lot number is required.')
        return value

    def validate_supplier_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Supplier name is required.')
        return value

    def validate_supplier_contact(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Supplier contact is required.')
        return value

    def validate_invoice_number(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Invoice number is required.')
        return value

    def validate(self, data):
        """Pre-check batch_number uniqueness per supply item, so the user
        gets a clear 400 with a field-level message instead of a DB-level
        409 (SupplyBatch.Meta has a unique_together on
        (supply_item, batch_number)). Mirrors the identical check on
        MedicineBatchWriteSerializer and GeneralItemBatchWriteSerializer
        above — this one was missing it, so AddStockView's IntegrityError
        catch was the only thing standing between a duplicate and a 500.

        supply_item comes from serializer context as the concrete object
        (set by AddStockView, already resolved through
        scope_queryset_to_branch + get_object_or_404), so no separate
        branch filter is needed here — unlike the medicine/general-item
        versions, which only have an id and must re-scope by branch.
        """
        supply_item = self.context.get('supply_item')
        batch_number = data.get('batch_number')
        if supply_item is not None and batch_number:
            if SupplyBatch.objects.filter(supply_item=supply_item, batch_number=batch_number).exists():
                raise serializers.ValidationError(
                    {
                        'batch_number': (
                            f'A batch with number "{batch_number}" already exists '
                            f'for this item. Use a different batch number, '
                            f'or edit the existing batch instead.'
                        )
                    }
                )
        return data


class SupplyUsageLogSerializer(serializers.ModelSerializer):
    """Read serializer for a usage log entry."""
    supply_item_name = serializers.CharField(source='supply_item.name', read_only=True)
    supply_item_category = serializers.CharField(source='supply_item.category', read_only=True)
    used_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SupplyUsageLog
        fields = [
            'log_id', 'supply_item', 'supply_item_name', 'supply_item_category',
            'batch', 'quantity_used', 'department',
            'used_by', 'used_by_name', 'notes', 'used_at', 'balance_after',
        ]
        read_only_fields = ['log_id', 'used_at']

    def get_used_by_name(self, obj):
        if not obj.used_by:
            return None
        return obj.used_by.get_full_name() or obj.used_by.username


class UseStockSerializer(serializers.Serializer):
    """Write serializer for deducting supply stock (issuing to a department)."""
    quantity_used = serializers.IntegerField(required=True, min_value=1)
    department = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=100)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=300)


class SupplyReturnSerializer(serializers.ModelSerializer):
    """Read serializer for a 'return to provider' record."""
    supply_item_name = serializers.CharField(source='supply_item.name', read_only=True)
    reason_display = serializers.CharField(source='get_reason_display', read_only=True)
    batch_number = serializers.CharField(source='batch.batch_number', read_only=True)
    supplier_name = serializers.CharField(source='batch.supplier_name', read_only=True)
    returned_by_name = serializers.SerializerMethodField()
    dealer_name = serializers.CharField(source='dealer.name', read_only=True, default=None)

    class Meta:
        model = SupplyReturn
        fields = [
            'return_id', 'supply_item', 'supply_item_name',
            'batch', 'batch_number', 'supplier_name',
            'quantity_returned', 'refund_amount', 'reason', 'reason_display', 'reason_details', 'reference_number',
            'dealer', 'dealer_name', 'settlement_method',
            'returned_by', 'returned_by_name', 'returned_at',
        ]
        read_only_fields = ['return_id', 'dealer_name', 'returned_at']

    def get_returned_by_name(self, obj):
        if not obj.returned_by:
            return None
        return obj.returned_by.get_full_name() or obj.returned_by.username


class ReturnToProviderSerializer(serializers.Serializer):
    """Write serializer for returning (part of) a batch to the supplier."""
    quantity_returned = serializers.IntegerField(required=True, min_value=1)
    reason = serializers.ChoiceField(required=True, choices=SupplyReturnReasonChoices.choices)
    reason_details = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=300)
    reference_number = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=100)
    # OPTIONAL — see SupplyReturn.dealer/settlement_method in models.py.
    dealer_id = serializers.IntegerField(required=False, allow_null=True)
    settlement_method = serializers.ChoiceField(
        required=False, allow_null=True,
        choices=['CREDIT', 'REFUND', 'EXCHANGE'],
    )


class SupplyStockAlertSerializer(serializers.ModelSerializer):
    """Read serializer for a supply low-stock alert."""
    supply_item_name = serializers.CharField(source='supply_item.name', read_only=True)
    supply_item_category = serializers.CharField(source='supply_item.category', read_only=True)

    class Meta:
        model = SupplyStockAlert
        fields = [
            'alert_id', 'supply_item', 'supply_item_name', 'supply_item_category',
            'current_stock', 'threshold', 'is_resolved', 'created_at',
        ]
        read_only_fields = ['alert_id', 'created_at']

# ============================================================
# GENERAL ITEM SERIALIZERS (non-medicine retail products)
# ============================================================

class GeneralItemSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, default=None)
    branch_code = serializers.CharField(source='branch.code', read_only=True, default=None)

    class Meta:
        model = GeneralItem
        fields = [
            'item_id',
            'branch',
            'branch_name',
            'branch_code',
            'name',
            'brand',
            'category',
            'category_display',
            'unit',
            'description',
            'is_active',
        ]
        read_only_fields = ['item_id', 'branch']


class GeneralItemWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    brand = serializers.CharField(required=False, allow_blank=True, max_length=150)
    category = serializers.ChoiceField(
        choices=GeneralItemCategoryChoices.choices,
        required=False,
        default=GeneralItemCategoryChoices.OTHER,
    )
    unit = serializers.CharField(required=False, allow_blank=True, max_length=50)
    description = serializers.CharField(required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False, default=True)

    def validate_name(self, value):
        # Branch-scoped, matching GeneralItem.Meta.unique_together =
        # [('branch', 'name')]. Branch comes from serializer context (set
        # by the view), never from the client.
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Name cannot be empty.')
        branch = self.context.get('branch')
        if branch is not None:
            qs = GeneralItem.objects.filter(branch=branch, name__iexact=value)
            instance = self.context.get('instance')
            if instance:
                qs = qs.exclude(pk=instance.pk)
            if qs.exists():
                raise serializers.ValidationError('An item with this name already exists at this branch.')
        return value


class GeneralItemBatchSerializer(serializers.ModelSerializer):
    general_item_name = serializers.CharField(source='general_item.name', read_only=True)
    category = serializers.CharField(source='general_item.category', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    dealer_name = serializers.CharField(source='dealer.name', read_only=True, default=None)

    class Meta:
        model = GeneralItemBatch
        fields = [
            'batch_id',
            'general_item',
            'general_item_name',
            'category',
            'batch_number',
            'quantity',
            'allocated_quantity',
            'cost_price',
            'mrp',
            'gst_percentage',
            'expiry_date',
            'status',
            'status_display',
            'low_stock_threshold',
            'dealer',
            'dealer_name',
            'settlement_method',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'batch_id', 'general_item_name', 'category', 'status_display',
            'dealer_name', 'created_at', 'updated_at', 'allocated_quantity',
        ]

    def validate_quantity(self, value):
        if value < 0:
            raise serializers.ValidationError("Quantity cannot be negative.")
        return value


class GeneralItemBatchDetailSerializer(GeneralItemBatchSerializer):
    pass


class GeneralItemWithBatchesSerializer(GeneralItemSerializer):
    batches = serializers.SerializerMethodField()
    total_stock = serializers.SerializerMethodField()

    def get_batches(self, obj):
        return GeneralItemBatchDetailSerializer(obj.batches.all(), many=True).data

    def get_total_stock(self, obj):
        total = 0
        for batch in obj.batches.filter(status='ACTIVE'):
            available = (batch.quantity or 0) - (batch.allocated_quantity or 0)
            if available > 0:
                total += available
        return total

    class Meta(GeneralItemSerializer.Meta):
        fields = GeneralItemSerializer.Meta.fields + ['batches', 'total_stock']


class GeneralItemBatchWriteSerializer(serializers.Serializer):
    general_item_id = serializers.IntegerField(required=True)
    batch_number = serializers.CharField(required=True, allow_blank=False, max_length=100)
    quantity = serializers.IntegerField(required=True, min_value=0)
    cost_price = serializers.DecimalField(required=True, max_digits=10, decimal_places=2)
    mrp = serializers.DecimalField(required=True, max_digits=10, decimal_places=2)
    gst_percentage = serializers.DecimalField(required=False, max_digits=5, decimal_places=2, default=0)
    # Optional, unlike Medicine — many general items never expire.
    expiry_date = serializers.DateField(required=False, allow_null=True)
    low_stock_threshold = serializers.IntegerField(required=False, min_value=0, default=10)
    status = serializers.ChoiceField(required=False, choices=['ACTIVE', 'EXPIRED', 'DEPLETED'], default='ACTIVE')
    dealer_id = serializers.IntegerField(required=False, allow_null=True)
    settlement_method = serializers.ChoiceField(required=False, allow_null=True, choices=['CREDIT', 'PAID'])

    def validate_batch_number(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError('Batch number cannot be empty.')
        return value.strip()

    def validate_cost_price(self, value):
        if value < 0:
            raise serializers.ValidationError('Cost price cannot be negative.')
        return value

    def validate(self, data):
        cost_price = data.get('cost_price')
        mrp = data.get('mrp')
        if cost_price and mrp and cost_price > mrp:
            raise serializers.ValidationError(
                {'cost_price': f'Cost price (₹{cost_price}) cannot exceed MRP (₹{mrp})'}
            )

        # Scoped to the caller's own branch — see the identical fix on
        # MedicineBatchWriteSerializer.validate() above for rationale.
        general_item_id = data.get('general_item_id')
        batch_number = data.get('batch_number')
        if general_item_id and batch_number:
            dup_qs = scope_queryset_to_branch(
                GeneralItemBatch.objects.filter(general_item_id=general_item_id, batch_number=batch_number),
                self.context.get('user'), branch_field='general_item__branch',
            )
            if dup_qs.exists():
                raise serializers.ValidationError(
                    {
                        'batch_number': (
                            f'A batch with number "{batch_number}" already exists '
                            f'for this item. Use a different batch number, '
                            f'or edit the existing batch instead.'
                        )
                    }
                )
        return data


class GeneralItemStockAlertSerializer(serializers.ModelSerializer):
    general_item_name = serializers.CharField(source='batch.general_item.name', read_only=True)
    batch_number = serializers.CharField(source='batch.batch_number', read_only=True)
    alert_type_display = serializers.CharField(source='get_alert_type_display', read_only=True)

    class Meta:
        model = GeneralItemStockAlert
        fields = [
            'alert_id', 'batch', 'general_item_name', 'batch_number',
            'alert_type', 'alert_type_display', 'is_resolved', 'created_at',
        ]
        read_only_fields = ['alert_id', 'created_at']


class PharmacyBillGeneralItemSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source='batch.general_item.name', read_only=True)
    brand = serializers.CharField(source='batch.general_item.brand', read_only=True, allow_null=True)
    category = serializers.CharField(source='batch.general_item.category', read_only=True)
    batch_number = serializers.CharField(source='batch.batch_number', read_only=True)
    available_stock = serializers.SerializerMethodField()
    is_dispensed = serializers.BooleanField(read_only=True)

    def get_available_stock(self, obj):
        return obj.batch.quantity

    class Meta:
        model = PharmacyBillGeneralItem
        fields = [
            'item_id',
            'bill',
            'batch',
            'item_name',
            'brand',
            'category',
            'batch_number',
            'quantity',
            'unit_mrp',
            'gst_percentage',
            'item_total',
            'available_stock',
            'is_dispensed',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'item_id', 'item_name', 'brand', 'category', 'batch_number',
            'unit_mrp', 'gst_percentage', 'item_total', 'available_stock',
            'is_dispensed', 'created_at', 'updated_at',
        ]


class GeneralItemReturnSerializer(serializers.ModelSerializer):
    """Read serializer for a general-item 'return to provider' record."""
    general_item_name = serializers.CharField(source='general_item.name', read_only=True)
    reason_display     = serializers.CharField(source='get_reason_display', read_only=True)
    batch_number       = serializers.CharField(source='batch.batch_number', read_only=True)
    dealer_name        = serializers.CharField(source='dealer.name', read_only=True, default=None)
    returned_by_name   = serializers.SerializerMethodField()

    class Meta:
        model = GeneralItemReturn
        fields = [
            'return_id', 'general_item', 'general_item_name',
            'batch', 'batch_number',
            'quantity_returned', 'refund_amount', 'reason', 'reason_display', 'reason_details', 'reference_number',
            'dealer', 'dealer_name', 'settlement_method',
            'returned_by', 'returned_by_name', 'returned_at',
        ]
        read_only_fields = ['return_id', 'dealer_name', 'returned_at']

    def get_returned_by_name(self, obj):
        if not obj.returned_by:
            return None
        return obj.returned_by.get_full_name() or obj.returned_by.username


class GeneralItemReturnToProviderSerializer(serializers.Serializer):
    """Write serializer for returning (part of) a general-item batch to the dealer."""
    quantity_returned = serializers.IntegerField(required=True, min_value=1)
    reason = serializers.ChoiceField(required=True, choices=GeneralItemReturnReasonChoices.choices)
    reason_details = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=300)
    reference_number = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=100)
    dealer_id = serializers.IntegerField(required=False, allow_null=True)
    settlement_method = serializers.ChoiceField(
        required=False, allow_null=True,
        choices=['CREDIT', 'REFUND', 'EXCHANGE'],
    )


class AddGeneralItemSerializer(serializers.Serializer):
    batch_id = serializers.IntegerField(required=True)
    quantity = serializers.IntegerField(required=True, min_value=1)