import re
from rest_framework import serializers
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError

from .models import StaffProfile, ReceptionistProfile, PharmacistProfile, GuestDoctorProfile, CommonReceptionistProfile, CommonPharmacistProfile, Procedure, AuditLog, Branch, ManagerBranchAccess

ROLE_MIN_AGE = {
    "Receptionist": 21,
    "Pharmacist":   23,
    "Admin":        21,
    "Doctor":       25,
    "Lab Technician": 21,
    "Manager":      24,
}

ROLE_QUALIFICATION_RULES = {
    "Pharmacist": (
        r"\bB\.?Pharm\b",
        "Pharmacist must hold a B.Pharm qualification.",
    ),
    "Receptionist": (
        r"\b(BA|B\.A|BBA|B\.B\.A|BCom|B\.Com|BCOM|BCA|B\.C\.A|BSc|B\.Sc|BHM|B\.H\.M|Graduation|Degree|Diploma)\b",
        "Receptionist must hold a minimum degree or equivalent qualification.",
    ),
    "Doctor": (
        r"\b(MBBS|MD|MS|BDS|MDS|DNB|M\.?Ch|DM|D\.M|Diploma|PhD)\b",
        "Doctor must hold an MBBS, MD, MS or equivalent medical degree.",
    ),
}


def validate_qualification_for_role(role, qualification):
    rule = ROLE_QUALIFICATION_RULES.get(role)
    if not rule:
        return
    pattern, message = rule
    if not re.search(pattern, qualification or "", re.IGNORECASE):
        raise serializers.ValidationError({"qualification": message})


def calculate_age(dob):
    if not dob:
        return 0
    today = timezone.localdate()
    return (today - dob).days // 365


# ─── User Serializer ──────────────────────────────────────────────
class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True, required=False, validators=[validate_password]
    )

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "password"]
        read_only_fields = ["id"]
        extra_kwargs = {
            "username": {"required": False, "validators": []},
            "email": {"required": True},
        }

    def validate_username(self, value):
        # SECURITY: case-insensitive, matching login's username__iexact lookup
        # (authentication/serializers.py). Excludes the current instance so a
        # case-only rename (e.g. "jdoe" -> "JDoe") isn't rejected as a
        # collision with itself.
        qs = User.objects.filter(username__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A user with that username already exists.")
        return value

    def create(self, validated_data):
        pwd = validated_data.pop("password", None)

        if not validated_data.get("username"):
            base = validated_data.get("email", "").split("@")[0] or "user"
            username = base
            counter = 1
            # SECURITY: case-insensitive, so we don't generate e.g. "JDoe"
            # when "jdoe" already exists.
            while User.objects.filter(username__iexact=username).exists():
                username = f"{base}{counter}"
                counter += 1
            validated_data["username"] = username

        user = User(**validated_data)
        user.is_staff = True

        if pwd:
            user.set_password(pwd)
        else:
            user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        pwd = validated_data.pop("password", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if pwd:
            instance.set_password(pwd)
        instance.save()
        return instance


# ─── Staff Profile Serializer ────────────────────────────────────
class StaffProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and hasattr(self.instance, 'user') and 'user' in self.fields:
            self.fields['user'].instance = self.instance.user

    class Meta:
        model = StaffProfile
        fields = "__all__"
        # SECURITY: is_group_admin must stay read-only here. StaffProfile.save()
        # mirrors it straight onto User.is_superuser, and this serializer is used
        # by StaffListView/StaffDetailView behind plain IsAdminUser — which any
        # branch Admin satisfies, not just the group admin. Without this, a
        # branch Admin could POST/PATCH {"is_group_admin": true} on themselves
        # or a colleague and silently self-promote to full superuser + cross-
        # branch access, bypassing StaffPromoteGroupAdminView entirely (the only
        # endpoint meant to grant it, gated to existing group admins). See
        # StaffProfile.is_group_admin's docstring in models.py.
        read_only_fields = ["staff_code", "created_at", "updated_at", "is_group_admin"]

    receptionist_profile_id = serializers.PrimaryKeyRelatedField(read_only=True, source="receptionist_profile")
    pharmacist_profile_id   = serializers.PrimaryKeyRelatedField(read_only=True, source="pharmacist_profile")
    doctor_profile_id       = serializers.PrimaryKeyRelatedField(read_only=True, source="doctor_profile")

    @transaction.atomic
    def create(self, validated_data):
        user_data = validated_data.pop("user")

        if not user_data.get("password"):
            raise serializers.ValidationError({
                "user": {"password": "A password is required when creating a new staff member."}
            })

        user_serializer = UserSerializer(data=user_data)
        user_serializer.is_valid(raise_exception=True)
        user = user_serializer.save()

        role = validated_data.get("role")
        dob  = validated_data.get("date_of_birth")

        if dob and calculate_age(dob) < ROLE_MIN_AGE.get(role, 21):
            raise serializers.ValidationError({
                "date_of_birth": f"{role} must be at least {ROLE_MIN_AGE[role]} years old."
            })

        validate_qualification_for_role(role, validated_data.get("qualification", ""))

        return StaffProfile.objects.create(user=user, **validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", None)

        if user_data:
            user_serializer = UserSerializer(instance.user, data=user_data, partial=True)
            user_serializer.is_valid(raise_exception=True)
            user_serializer.save()

        role = validated_data.get("role", instance.role)
        dob  = validated_data.get("date_of_birth", instance.date_of_birth)

        if dob and calculate_age(dob) < ROLE_MIN_AGE.get(role, 21):
            raise serializers.ValidationError({
                "date_of_birth": f"{role} must be at least {ROLE_MIN_AGE[role]} years old."
            })

        qual = validated_data.get("qualification")
        if qual:
            validate_qualification_for_role(role, qual)
        elif not instance.qualification:
            validate_qualification_for_role(role, "")

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance


def _branch_scoped_staff_qs(role, request):
    """
    SECURITY: staff_id choices for a Receptionist/Pharmacist/Doctor profile
    must be limited to the caller's own branch — otherwise a branch admin
    could link a profile to a staff member belonging to a different
    branch entirely. Group admin (or no request in context, e.g. schema
    generation) sees every branch's staff of that role.
    """
    qs = StaffProfile.objects.filter(role=role, is_active=True)
    if request is None:
        return qs
    from authentication.utils import is_group_admin_user, get_user_branch
    if is_group_admin_user(request.user):
        return qs
    branch = get_user_branch(request.user)
    return qs.filter(branch=branch) if branch else qs.none()


# ─── Receptionist Profile Serializer ────────────────────────────
class ReceptionistProfileSerializer(serializers.ModelSerializer):
    staff = StaffProfileSerializer(read_only=True)
    staff_id = serializers.PrimaryKeyRelatedField(
        queryset=StaffProfile.objects.filter(role="Receptionist", is_active=True),
        source="staff",
        write_only=True,
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["staff_id"].queryset = _branch_scoped_staff_qs(
            "Receptionist", self.context.get("request")
        )

    class Meta:
        model = ReceptionistProfile
        fields = "__all__"
        read_only_fields = ["profile_id"]


# ─── Pharmacist Profile Serializer ───────────────────────────────
class PharmacistProfileSerializer(serializers.ModelSerializer):
    staff = StaffProfileSerializer(read_only=True)
    staff_id = serializers.PrimaryKeyRelatedField(
        queryset=StaffProfile.objects.filter(role="Pharmacist", is_active=True),
        source="staff",
        write_only=True,
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["staff_id"].queryset = _branch_scoped_staff_qs(
            "Pharmacist", self.context.get("request")
        )

    class Meta:
        model = PharmacistProfile
        fields = "__all__"
        read_only_fields = ["profile_id"]

# ─────────────────────────────────────────────────────────────────────────────
# PATCH: Replace GuestDoctorProfileSerializer in administration/serializers.py
# with this version. All other serializers stay unchanged.
# ─────────────────────────────────────────────────────────────────────────────

from django.contrib.auth.models import User
from rest_framework import serializers
from .models import GuestDoctorProfile


class GuestDoctorUserSerializer(serializers.Serializer):
    """Nested writable user block for guest doctor login credentials."""
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    email    = serializers.EmailField(required=False, allow_blank=True)


class GuestDoctorProfileSerializer(serializers.ModelSerializer):

    # Read: expose flat username/email for the table view
    username = serializers.CharField(source="user.username", read_only=True)
    email    = serializers.CharField(source="user.email",    read_only=True, allow_null=True)

    # Write: accept nested credentials block
    user_credentials = GuestDoctorUserSerializer(write_only=True, required=False)

    class Meta:
        model  = GuestDoctorProfile
        fields = [
            "guest_doctor_id", "guest_code", "branch",
            "full_name", "specialization", "department",
            "registration_number", "phone", "consultation_fee",
            "is_active", "notes", "created_at", "updated_at",
            # read-only flat fields
            "username", "email",
            # write-only nested block
            "user_credentials",
        ]
        read_only_fields = ["guest_doctor_id", "guest_code", "created_at", "updated_at"]

    # ── CREATE ───────────────────────────────────────────────────────────────
    def create(self, validated_data):
        creds = validated_data.pop("user_credentials", None)
        user  = None

        if creds:
            username = creds.get("username", "").strip()
            password = creds.get("password", "").strip()
            email    = creds.get("email", "").strip()

            if not username:
                raise serializers.ValidationError(
                    {"user_credentials": {"username": "Username is required."}}
                )
            if not password:
                raise serializers.ValidationError(
                    {"user_credentials": {"password": "Password is required when creating credentials."}}
                )
            # SECURITY: case-insensitive, matching login's username__iexact
            # lookup (authentication/serializers.py).
            if User.objects.filter(username__iexact=username).exists():
                raise serializers.ValidationError(
                    {"user_credentials": {"username": "This username is already taken."}}
                )

            user = User.objects.create_user(
                username=username,
                password=password,
                email=email,
                is_staff=False,
                is_superuser=False,
            )

            # If admin didn't supply a full_name (credentials-only flow),
            # default it to the username — reception will update it later.
            if not validated_data.get("full_name", "").strip():
                validated_data["full_name"] = username

        return GuestDoctorProfile.objects.create(user=user, **validated_data)

    # ── UPDATE ───────────────────────────────────────────────────────────────
    def update(self, instance, validated_data):
        creds = validated_data.pop("user_credentials", None)

        # Update profile fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if creds:
            username = creds.get("username", "").strip()
            password = creds.get("password", "").strip()
            email    = creds.get("email", "").strip()

            if instance.user:
                # Update existing user
                user = instance.user
                if username:
                    # SECURITY: case-insensitive, matching login's
                    # username__iexact lookup (authentication/serializers.py).
                    if (
                        User.objects
                        .filter(username__iexact=username)
                        .exclude(pk=user.pk)
                        .exists()
                    ):
                        raise serializers.ValidationError(
                            {"user_credentials": {"username": "This username is already taken."}}
                        )
                    user.username = username
                if email:
                    user.email = email
                if password:
                    user.set_password(password)
                user.save()
            else:
                # Create a new user and link it
                if not username:
                    raise serializers.ValidationError(
                        {"user_credentials": {"username": "Username is required."}}
                    )
                if not password:
                    raise serializers.ValidationError(
                        {"user_credentials": {"password": "Password is required when creating credentials."}}
                    )
                # SECURITY: case-insensitive, matching login's
                # username__iexact lookup (authentication/serializers.py).
                if User.objects.filter(username__iexact=username).exists():
                    raise serializers.ValidationError(
                        {"user_credentials": {"username": "This username is already taken."}}
                    )
                user = User.objects.create_user(
                    username=username,
                    password=password,
                    email=email,
                    is_staff=False,
                    is_superuser=False,
                )
                instance.user = user
                instance.save(update_fields=["user"])

        return instance

# ─────────────────────────────────────────────────────────────────────────────
# COMMON STAFF PROFILES — Common Receptionist / Common Pharmacist.
# Two separate single-role accounts, each admin-managed like a Guest Doctor.
# CommonStaffUserSerializer + CommonStaffProfileSerializerBase hold the
# shared credentials-handling logic; the two concrete serializers below
# just point at their own model.
# full_name/phone/qualification are ordinary editable fields (not
# credentials-only like guest doctor), since the admin sets these upfront.
# ─────────────────────────────────────────────────────────────────────────────
class CommonStaffUserSerializer(serializers.Serializer):
    """Nested writable user block for common-staff login credentials."""
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    email    = serializers.EmailField(required=False, allow_blank=True)


class CommonStaffProfileSerializerBase(serializers.ModelSerializer):
    """
    Shared serializer logic for the two common-staff account types.
    Concrete subclasses set `Meta.model` to CommonReceptionistProfile or
    CommonPharmacistProfile — everything else (fields, credential handling)
    is identical, so it lives here once.
    """

    username = serializers.CharField(source="user.username", read_only=True)
    email    = serializers.CharField(source="user.email",    read_only=True, allow_null=True)

    # Not required at request time — the admin "Add" flow is credentials-only
    # (username + password), same as Guest Doctor. If omitted/blank, create()
    # defaults it to the username below; reception/pharmacy can fill in the
    # real name later via PATCH.
    full_name = serializers.CharField(max_length=200, required=False, allow_blank=True)

    user_credentials = CommonStaffUserSerializer(write_only=True, required=False)

    class Meta:
        fields = [
            "branch", "full_name", "phone", "qualification",
            "is_active", "notes", "created_at", "updated_at",
            "username", "email",
            "user_credentials",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # branch is a required FK on the model, but it's resolved and
        # injected server-side by the view (resolve_branch_for_write) —
        # never trust a client-supplied value for it directly, and don't
        # make DRF demand it on every partial-update PATCH either.
        if "branch" in self.fields:
            self.fields["branch"].required = False

    def _extract_credentials(self, validated_data, required):
        creds = validated_data.pop("user_credentials", None)
        if not creds:
            if required:
                raise serializers.ValidationError(
                    {"user_credentials": {"username": "Login credentials are required."}}
                )
            return None, None, None

        username = creds.get("username", "").strip()
        password = creds.get("password", "").strip()
        email    = creds.get("email", "").strip()

        if not username:
            raise serializers.ValidationError(
                {"user_credentials": {"username": "Username is required."}}
            )
        if required and not password:
            raise serializers.ValidationError(
                {"user_credentials": {"password": "Password is required when creating credentials."}}
            )
        return username, password, email

    # ── CREATE ───────────────────────────────────────────────────────────────
    @transaction.atomic
    def create(self, validated_data):
        username, password, email = self._extract_credentials(validated_data, required=True)

        # SECURITY: case-insensitive, matching login's username__iexact
        # lookup (authentication/serializers.py).
        if User.objects.filter(username__iexact=username).exists():
            raise serializers.ValidationError(
                {"user_credentials": {"username": "This username is already taken."}}
            )

        user = User.objects.create_user(
            username=username,
            password=password,
            email=email,
            is_staff=True,
            is_superuser=False,
        )

        # Credentials-only create flow (mirrors Guest Doctor): default
        # full_name to the username if the admin didn't supply one.
        if not validated_data.get("full_name", "").strip():
            validated_data["full_name"] = username

        return self.Meta.model.objects.create(user=user, **validated_data)

    # ── UPDATE ───────────────────────────────────────────────────────────────
    @transaction.atomic
    def update(self, instance, validated_data):
        username, password, email = self._extract_credentials(validated_data, required=False)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if username or password or email:
            user = instance.user
            if username and username != user.username:
                # SECURITY: case-insensitive, matching login's
                # username__iexact lookup (authentication/serializers.py).
                if User.objects.filter(username__iexact=username).exclude(pk=user.pk).exists():
                    raise serializers.ValidationError(
                        {"user_credentials": {"username": "This username is already taken."}}
                    )
                user.username = username
            if email:
                user.email = email
            if password:
                user.set_password(password)
            user.save()

        return instance


class CommonReceptionistProfileSerializer(CommonStaffProfileSerializerBase):
    class Meta(CommonStaffProfileSerializerBase.Meta):
        model = CommonReceptionistProfile
        fields = ["common_receptionist_id", "common_code"] + CommonStaffProfileSerializerBase.Meta.fields
        read_only_fields = ["common_receptionist_id", "common_code"] + CommonStaffProfileSerializerBase.Meta.read_only_fields


class CommonPharmacistProfileSerializer(CommonStaffProfileSerializerBase):
    class Meta(CommonStaffProfileSerializerBase.Meta):
        model = CommonPharmacistProfile
        fields = ["common_pharmacist_id", "common_code"] + CommonStaffProfileSerializerBase.Meta.fields
        read_only_fields = ["common_pharmacist_id", "common_code"] + CommonStaffProfileSerializerBase.Meta.read_only_fields


# ─────────────────────────────────────────────────────────────────────────────
# DOCTOR PROFILE (admin view) — DoctorProfile actually lives in the `doctor`
# app, but admin staff management for doctors is exposed here so it follows
# the same /api/administration/... convention as receptionist/pharmacist.
# Imported lazily to avoid loading the `doctor` app before Django's app
# registry is ready (same pattern used in administration/signals.py).
# ─────────────────────────────────────────────────────────────────────────────
from doctor.models import DoctorProfile


class DoctorProfileSerializer(serializers.ModelSerializer):
    staff = StaffProfileSerializer(read_only=True)
    staff_id = serializers.PrimaryKeyRelatedField(
        queryset=StaffProfile.objects.filter(role="Doctor", is_active=True),
        source="staff",
        write_only=True,
        required=False,
    )
    specialty_name = serializers.SerializerMethodField(read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["staff_id"].queryset = _branch_scoped_staff_qs(
            "Doctor", self.context.get("request")
        )

    class Meta:
        model = DoctorProfile
        fields = [
            "profile_id", "staff", "staff_id",
            "specialty", "specialty_name",
            # `specialization` is deprecated (see doctor.DoctorProfile) and kept
            # here only as a read fallback for legacy rows that predate the
            # `specialty` FK backfill — nothing should write to it going forward.
            "specialization", "registration_number", "department",
            "consultation_fee", "is_available",
            "created_at", "updated_at",
        ]
        read_only_fields = ["profile_id", "created_at", "updated_at"]

    def get_specialty_name(self, obj):
        return obj.specialty.name if obj.specialty_id else None


# ─── Procedure Serializer ────────────────────────────────────────
class ProcedureSerializer(serializers.ModelSerializer):
    class Meta:
        model = Procedure
        fields = "__all__"
        read_only_fields = ["procedure_id", "created_at", "updated_at", "created_by_pharmacist"]


# ─── Audit Log Serializer ────────────────────────────────────────
class BranchSerializer(serializers.ModelSerializer):
    """
    Branch itself is not covered by the usual scope_queryset_to_branch
    pattern (a Branch doesn't have a branch FK to itself) — the view layer
    (BranchListView/BranchDetailView) is responsible for restricting which
    rows a non-group-admin can see/touch. `code` is normalized to uppercase
    by Branch.clean(), same as the model.
    """

    class Meta:
        model = Branch
        fields = [
            "branch_id", "name", "code", "address", "phone",
            "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["branch_id", "created_at", "updated_at"]


def _staff_display_name(staff_profile):
    """
    Safely resolve a display name for a StaffProfile's linked user.

    Mirrors manager/serializers.py:_staff_profile_display_name (kept as a
    separate copy rather than a cross-app import, same as the rest of this
    module) — staff_profile.user is a required OneToOne in normal
    operation, but a data-integrity gap (a User row removed without its
    StaffProfile being cleaned up) leaves the FK pointing nowhere.
    """
    try:
        user = staff_profile.user
    except User.DoesNotExist:
        return f"{staff_profile.staff_code} (user removed)"
    return user.get_full_name() or user.username


# ─── Manager Branch Access Serializers ────────────────────────────
class ManagerBranchAccessSerializer(serializers.ModelSerializer):
    """
    Read serializer for a manager's extra-branch grant. Used by the
    group-admin-only list/grant/revoke endpoints (see
    administration/views.py:ManagerBranchAccessListView /
    ManagerBranchAccessRevokeView) — powers the "Branch Access" control on
    the frontend's Staff/Manager management page.
    """

    manager_staff_code = serializers.CharField(source="manager.staff_code", read_only=True)
    manager_name = serializers.SerializerMethodField()
    manager_home_branch_id = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    branch_code = serializers.CharField(source="branch.code", read_only=True)
    granted_by_username = serializers.SerializerMethodField()

    class Meta:
        model = ManagerBranchAccess
        fields = [
            "id",
            "manager", "manager_staff_code", "manager_name", "manager_home_branch_id",
            "branch", "branch_name", "branch_code",
            "granted_by", "granted_by_username",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_manager_name(self, obj):
        return _staff_display_name(obj.manager)

    def get_manager_home_branch_id(self, obj):
        return obj.manager.branch_id

    def get_granted_by_username(self, obj):
        return obj.granted_by.username if obj.granted_by else None


class ManagerBranchAccessGrantSerializer(serializers.ModelSerializer):
    """
    Write serializer for POST /api/administration/manager-branch-access/ —
    a group admin grants a Manager access to an additional branch.

    `manager` and `branch` are accepted as ids; `granted_by` is set by the
    view from request.user (never client-supplied — same reasoning as
    StaffProfile.is_group_admin being view-set only, see
    StaffPromoteGroupAdminView). ManagerBranchAccess.save() re-runs the
    same clean()/unique-together checks as the model directly, so this
    serializer just needs to translate those into DRF-shaped errors
    instead of duplicating the rules.
    """

    class Meta:
        model = ManagerBranchAccess
        fields = ["id", "manager", "branch", "granted_by", "created_at"]
        read_only_fields = ["id", "granted_by", "created_at"]

    def validate_manager(self, value):
        if value.role != "Manager":
            raise serializers.ValidationError("Branch access can only be granted to a Manager account.")
        return value

    def create(self, validated_data):
        try:
            return ManagerBranchAccess.objects.create(**validated_data)
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages}
            raise serializers.ValidationError(detail)


class AuditLogSerializer(serializers.ModelSerializer):
    username = serializers.SerializerMethodField()
    branch_code = serializers.SerializerMethodField()
    branch_name = serializers.SerializerMethodField()
    # Populated only when the queryset was annotated with the ROW_NUMBER()
    # window function (AuditLogListView) — a plain AuditLog.objects.get()
    # elsewhere won't have it, hence required=False/allow_null.
    serial_number = serializers.IntegerField(read_only=True, required=False, allow_null=True)

    def get_username(self, obj):
        return obj.user.username if obj.user else "system"

    def get_branch_code(self, obj):
        return obj.branch.code if obj.branch else None

    def get_branch_name(self, obj):
        return obj.branch.name if obj.branch else None

    class Meta:
        model = AuditLog
        fields = "__all__"

# =========================================================
# HOSPITAL SETTINGS  (singleton — admin only)
# =========================================================
from .models import HospitalSettings


class HospitalSettingsSerializer(serializers.ModelSerializer):

    class Meta:
        model  = HospitalSettings
        fields = [
            'mrd_registration_fee',
            'default_home_visit_fee',
            'default_home_visit_travel_charge',
            'updated_at',
        ]
        read_only_fields = ['updated_at']

    def validate_mrd_registration_fee(self, value):
        if value < 0:
            raise serializers.ValidationError('Registration fee cannot be negative.')
        return value

    def validate_default_home_visit_fee(self, value):
        if value < 0:
            raise serializers.ValidationError('Home visit fee cannot be negative.')
        return value

    def validate_default_home_visit_travel_charge(self, value):
        if value < 0:
            raise serializers.ValidationError('Travel charge cannot be negative.')
        return value