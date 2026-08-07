# authentication/serializers.py

from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .models import LoginActivity
from .utils import normalize_role


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Extends the default JWT serializer to include user info in validated_data
    so the LoginView can build a proper response without an extra DB query.

    FIX 3 & 5: Role normalization now uses the shared normalize_role() helper
    (authentication/utils.py) instead of an inline .replace(" ", "_") call.
    This makes the role string emitted in the login response consistent with
    what permissions.py checks and what /auth/me/ returns.
    """

    def validate(self, attrs):
        # Login is whitespace-insensitive but CASE-SENSITIVE by design —
        # "Admin" and "admin" are treated as different usernames.
        #
        # NOTE: MySQL's default column collation (utf8mb4_general_ci /
        # utf8mb4_0900_ai_ci) is case-INSENSITIVE, so a plain
        # `username=<value>` lookup at the DB layer will still match a
        # differently-cased row. That means simply removing the old
        # __iexact lookup below is NOT enough by itself — authenticate()
        # would still resolve to the right user regardless of casing,
        # because MySQL, not Django, is the one doing the case-folding.
        # So after Django resolves self.user, we explicitly re-check the
        # casing in Python and reject on mismatch, which is correct
        # regardless of DB collation/engine.
        username = attrs.get(self.username_field)
        if username:
            username = username.strip()
            attrs[self.username_field] = username

        data = super().validate(attrs)  # sets self.user, adds 'access' + 'refresh'

        if username and getattr(self.user, self.username_field) != username:
            # DB collation matched case-insensitively, but the casing the
            # client typed doesn't exactly match what's stored — treat
            # this the same as a bad credential (don't reveal that the
            # username exists under different casing).
            from rest_framework_simplejwt.exceptions import AuthenticationFailed
            raise AuthenticationFailed(
                "No active account found with the given credentials",
                "no_active_account",
            )

        staff_profile              = getattr(self.user, "staff_profile", None)
        guest_doctor_profile       = getattr(self.user, "guest_doctor_profile", None)
        common_receptionist_profile = getattr(self.user, "common_receptionist_profile", None)
        common_pharmacist_profile   = getattr(self.user, "common_pharmacist_profile", None)

        if guest_doctor_profile:
            # Guest doctor login — use doctor role, no staff_code
            role = "doctor"
            from .utils import is_group_admin_user, get_user_branch_any, serialize_branch
            branch_id, branch_code, branch_name = serialize_branch(get_user_branch_any(self.user))
            data["user"] = {
                "id":               self.user.id,
                "username":         self.user.username,
                "first_name":       self.user.first_name,
                "last_name":        self.user.last_name,
                "email":            self.user.email,
                "is_staff":         self.user.is_staff,
                "role":             role,
                "staff_code":       None,
                "is_guest_doctor":  True,
                "is_common_staff":  False,
                "guest_code":       guest_doctor_profile.guest_code,
                "common_code":      None,
                "full_name":        guest_doctor_profile.full_name,
                "is_group_admin":   is_group_admin_user(self.user),
                "branch_id":        branch_id,
                "branch_code":      branch_code,
                "branch_name":      branch_name,
            }
        elif common_receptionist_profile or common_pharmacist_profile:
            # Common Receptionist / Common Pharmacist login — admin-created
            # account fixed to a single role, same shape as a regular staff
            # login except staff_code is None and is_common_staff is True.
            profile = common_receptionist_profile or common_pharmacist_profile
            role = "receptionist" if common_receptionist_profile else "pharmacist"
            data["user"] = {
                "id":               self.user.id,
                "username":         self.user.username,
                "first_name":       self.user.first_name,
                "last_name":        self.user.last_name,
                "email":            self.user.email,
                "is_staff":         self.user.is_staff,
                "role":             role,
                "staff_code":       None,
                "is_guest_doctor":  False,
                "is_common_staff":  True,
                "guest_code":       None,
                "common_code":      profile.common_code,
                "full_name":        profile.full_name,
            }
        else:
            # SECURITY: previously defaulted to "admin" for ANY authenticated
            # account without a StaffProfile/guest/common profile — not just
            # superusers/staff. That's a fail-open default: an incomplete or
            # misconfigured account (or any future account type this code
            # doesn't yet know about) would be told it's an admin. Real
            # authorization is enforced by authentication.permissions._get_role()
            # regardless of this display value, but the value returned here
            # is what the frontend uses to decide what UI to show, so it
            # must fail closed too.
            if staff_profile and staff_profile.role:
                role = normalize_role(staff_profile.role)
            elif self.user.is_superuser or self.user.is_staff:
                role = "admin"
            else:
                role = None

            # FIX: mirrors the guest-doctor branch above and MeView's
            # already-fixed shape — this branch (regular staff/admin/
            # superuser accounts) previously omitted is_group_admin and
            # branch_*, forcing the frontend to guess group-admin status
            # from GET /branches/ result count instead of being told
            # directly. That guess is wrong whenever only one branch
            # exists yet, even for a genuine group admin.
            from .utils import is_group_admin_user, get_user_branch_any, serialize_branch
            branch_id, branch_code, branch_name = serialize_branch(get_user_branch_any(self.user))

            data["user"] = {
                "id":               self.user.id,
                "username":         self.user.username,
                "first_name":       self.user.first_name,
                "last_name":        self.user.last_name,
                "email":            self.user.email,
                "is_staff":         self.user.is_staff,
                "role":             role,
                "staff_code":       staff_profile.staff_code if staff_profile else None,
                "is_guest_doctor":  False,
                "is_common_staff":  False,
                "guest_code":       None,
                "common_code":      None,
                "full_name":        None,
                "is_group_admin":   is_group_admin_user(self.user),
                "branch_id":        branch_id,
                "branch_code":      branch_code,
                "branch_name":      branch_name,
            }

        return data


class LoginActivitySerializer(serializers.ModelSerializer):

    class Meta:
        model = LoginActivity
        fields = '__all__'
        read_only_fields = ['login_time']