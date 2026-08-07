from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import (
    TokenError,
    InvalidToken,
    AuthenticationFailed,
)
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from django.conf import settings

from .models import LoginActivity
from .serializers import (
    CustomTokenObtainPairSerializer,
    LoginActivitySerializer,
)
from .utils import get_client_ip, normalize_role
from administration.audit import log_audit
import logging

logger = logging.getLogger(__name__)


# Cookie configuration
_ACCESS_COOKIE = "access_token"
_REFRESH_COOKIE = "refresh_token"

# SECURITY FIX: this used to hardcode "secure": False with a "True in
# production" comment nobody ever came back to flip. settings.py already
# derives SESSION_COOKIE_SECURE correctly (on by default whenever
# DEBUG=False, overridable via SECURE_COOKIES env var) — reuse that same
# computed flag so the cookies actually carrying the JWTs get the Secure
# attribute in production instead of silently going out over plain HTTP
# regardless of environment.
_COOKIE_DEFAULTS = {
    "httponly": True,
    "secure": settings.SESSION_COOKIE_SECURE,
    "samesite": "Lax",
    "path": "/",
}


class LoginView(APIView):
    permission_classes = []
    # SECURITY: rate-limit login attempts to blunt brute-force/credential
    # stuffing. Rate is keyed by IP (anonymous request) — see
    # settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["login"].
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        serializer = CustomTokenObtainPairSerializer(
            data=request.data,
            context={"request": request},
        )

        try:
            serializer.is_valid(raise_exception=True)

        except DRFPermissionDenied as exc:
            # SECURITY: raised by authentication/signals.py when django-axes
            # reports this (username, IP) combination as locked out. Kept
            # separate from the generic branch below so the message is
            # accurate — this is not "wrong password", it's "stop trying
            # for a while" — while still not confirming whether the
            # username itself exists.
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            return Response(
                {"error": detail},
                status=status.HTTP_403_FORBIDDEN,
            )

        except (
            AuthenticationFailed,
            InvalidToken,
            TokenError,
            DRFValidationError,
        ):
            return Response(
                {"error": "Invalid username or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        data = serializer.validated_data

        access = data["access"]
        refresh = data["refresh"]
        user_info = data["user"]

        client_ip = get_client_ip(request)
        user_agent_str = request.META.get("HTTP_USER_AGENT", "")
        # Client-generated UUID persisted in the frontend's localStorage
        # per browser (see frontend deviceId util) and sent on every
        # request via this header. Empty for an older frontend build or a
        # non-browser client — everything below degrades gracefully to
        # "no device info" rather than failing.
        device_id = request.META.get("HTTP_X_DEVICE_ID", "")[:64]

        # Optional login activity logging. SECURITY: this is an audit trail —
        # if it silently fails we'd have no record that it broke. Log a
        # warning (but still don't block the login itself on a logging
        # failure).
        try:
            LoginActivity.objects.create(
                user_id=user_info["id"],
                ip_address=client_ip,
                user_agent=user_agent_str,
                device_id=device_id,
            )
        except Exception:
            logger.warning("Failed to record LoginActivity for user_id=%s", user_info.get("id"), exc_info=True)

        # "Known devices" check — is this (user, device_id) pair one we've
        # seen log in before? Only meaningful when the frontend actually
        # sent a device_id; an empty one is never treated as "known" or
        # "new" since it carries no information. This is a login-time-only
        # registry (see UserDevice docstring) — it does NOT get touched on
        # every request, just here.
        is_new_device = False
        if device_id:
            try:
                from django.utils import timezone
                from administration.models import UserDevice

                device, is_new_device = UserDevice.objects.get_or_create(
                    user_id=user_info["id"],
                    device_id=device_id,
                    defaults={
                        "user_agent": user_agent_str,
                        "first_ip": client_ip,
                        "last_ip": client_ip,
                    },
                )
                if not is_new_device:
                    device.last_ip = client_ip
                    device.last_seen = timezone.now()
                    device.user_agent = user_agent_str
                    device.save(update_fields=["last_ip", "last_seen", "user_agent"])
            except Exception:
                logger.warning("Failed to record UserDevice for user_id=%s", user_info.get("id"), exc_info=True)

        # Audit trail entry, separate from LoginActivity above (that table
        # is per-user login history for the user themselves; AuditLog is
        # the admin-facing, branch-scoped, cross-module trail). Same
        # "don't let logging break the actual action" guard as above.
        #
        # ip_address/user_agent/device_id are passed explicitly (we already
        # have `request` right here) rather than relying on log_audit's
        # thread-local fallback — equivalent either way for a real request,
        # but explicit is more robust and keeps this call self-contained.
        # A first-time device is called out in `extra` so it's visible
        # directly in the audit description, not just as a raw device_id
        # column — that's the actual "flag this for review" signal.
        try:
            log_audit(
                serializer.user, "Authentication", "LOGIN",
                extra="New device." if is_new_device else "",
                ip_address=client_ip,
                user_agent=user_agent_str,
                device_id=device_id,
            )
        except Exception:
            logger.warning("Failed to record audit log for login user_id=%s", user_info.get("id"), exc_info=True)

        response = Response(
            {
                "message": "Login successful.",
                "user": user_info,
            },
            status=status.HTTP_200_OK,
        )

        response.set_cookie(
            key=_ACCESS_COOKIE,
            value=access,
            max_age=60 * 60,
            **_COOKIE_DEFAULTS,
        )

        response.set_cookie(
            key=_REFRESH_COOKIE,
            value=refresh,
            max_age=60 * 60 * 24,
            **_COOKIE_DEFAULTS,
        )

        return response


class CookieTokenRefreshView(APIView):
    permission_classes = []

    def post(self, request):
        refresh_token = request.COOKIES.get(_REFRESH_COOKIE)

        if not refresh_token:
            return Response(
                {"error": "Refresh token missing."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            # RefreshToken.blacklist() is called automatically by simplejwt
            # when BLACKLIST_AFTER_ROTATION=True and we call token.access_token
            # via the rotate path.  We call rotate() explicitly here so we
            # always get a new refresh token regardless of simplejwt version.
            from django.contrib.auth import get_user_model
            User = get_user_model()

            old_token = RefreshToken(refresh_token)
            old_token.blacklist()          # invalidate the incoming token now
            user = User.objects.get(id=old_token["user_id"])
            new_token  = RefreshToken.for_user(user)   # tracked in OutstandingToken
            new_access  = str(new_token.access_token)
            new_refresh = str(new_token)

        except (TokenError, InvalidToken):
            return Response(
                {"error": "Invalid or expired refresh token."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        response = Response(
            {"message": "Token refreshed."},
            status=status.HTTP_200_OK,
        )

        response.set_cookie(
            key=_ACCESS_COOKIE,
            value=new_access,
            max_age=60 * 60,
            **_COOKIE_DEFAULTS,
        )

        # Rotate: send the brand-new refresh token back in the cookie.
        # The old one is now blacklisted and will be rejected if replayed.
        response.set_cookie(
            key=_REFRESH_COOKIE,
            value=new_refresh,
            max_age=60 * 60 * 24,
            **_COOKIE_DEFAULTS,
        )

        return response


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        staff_profile               = getattr(user, "staff_profile", None)
        guest_doctor_profile        = getattr(user, "guest_doctor_profile", None)
        common_receptionist_profile = getattr(user, "common_receptionist_profile", None)
        common_pharmacist_profile   = getattr(user, "common_pharmacist_profile", None)

        if guest_doctor_profile:
            # Guest doctor — always returns "doctor" role
            return Response({
                "id":              user.id,
                "username":        user.username,
                "first_name":      user.first_name,
                "last_name":       user.last_name,
                "email":           user.email,
                "is_staff":        user.is_staff,
                "role":            "doctor",
                "staff_code":      None,
                "is_guest_doctor": True,
                "is_common_staff": False,
                "guest_code":      guest_doctor_profile.guest_code,
                "common_code":     None,
                "full_name":       guest_doctor_profile.full_name,
            })

        if common_receptionist_profile or common_pharmacist_profile:
            # Common Receptionist / Common Pharmacist — admin-created account
            # fixed to a single role, same shape as the login response.
            profile = common_receptionist_profile or common_pharmacist_profile
            role = "receptionist" if common_receptionist_profile else "pharmacist"
            return Response({
                "id":              user.id,
                "username":        user.username,
                "first_name":      user.first_name,
                "last_name":       user.last_name,
                "email":           user.email,
                "is_staff":        user.is_staff,
                "role":            role,
                "staff_code":      None,
                "is_guest_doctor": False,
                "is_common_staff": True,
                "guest_code":      None,
                "common_code":     profile.common_code,
                "full_name":       profile.full_name,
            })

        # FIX 5: Use the shared normalize_role() helper so this endpoint
        # returns the same role string as the login response and permissions.py.
        # SECURITY: fail closed — only report "admin" for actual staff/
        # superuser accounts, not any account lacking a recognized profile
        # (see matching fix in serializers.CustomTokenObtainPairSerializer).
        if staff_profile and staff_profile.role:
            role = normalize_role(staff_profile.role)
        elif user.is_superuser or user.is_staff:
            role = "admin"
        else:
            role = None

        # FIX: mirrors the same fix in CustomTokenObtainPairSerializer —
        # this branch (regular staff/admin/superuser accounts) previously
        # omitted is_group_admin and branch_* entirely, even though the
        # guest-doctor branch above already returns them. Since /auth/me/
        # is re-hit on every page load to validate the session, that gap
        # meant the frontend could never reliably know an admin's
        # group-admin status and had to guess from GET /branches/ instead.
        from .utils import is_group_admin_user, get_user_branch_any, serialize_branch
        branch_id, branch_code, branch_name = serialize_branch(get_user_branch_any(user))

        return Response({
            "id":              user.id,
            "username":        user.username,
            "first_name":      user.first_name,
            "last_name":       user.last_name,
            "email":           user.email,
            "is_staff":        user.is_staff,
            "role":            role,
            "staff_code":      staff_profile.staff_code if staff_profile else None,
            "is_guest_doctor": False,
            "is_common_staff": False,
            "guest_code":      None,
            "common_code":     None,
            "full_name":       None,
            "is_group_admin":  is_group_admin_user(user),
            "branch_id":       branch_id,
            "branch_code":     branch_code,
            "branch_name":     branch_name,
        })


class LogoutView(APIView):
    permission_classes = []

    def post(self, request):
        # request.user is still resolved by CookieJWTAuthentication here
        # even though permission_classes is empty (that only skips the
        # permission *check*, not authentication itself) — so this is
        # still the real logging-out user whenever the access-token cookie
        # was valid.
        if request.user and request.user.is_authenticated:
            try:
                log_audit(request.user, "Authentication", "LOGOUT", ip_address=get_client_ip(request))
            except Exception:
                logger.warning("Failed to record audit log for logout user_id=%s", request.user.id, exc_info=True)

        raw_refresh = request.COOKIES.get(_REFRESH_COOKIE)

        if raw_refresh:
            try:
                # Blacklisting the refresh token server-side ensures it cannot
                # be replayed even if an attacker captured the cookie before
                # the user logged out.
                token = RefreshToken(raw_refresh)
                token.blacklist()
            except (TokenError, InvalidToken):
                # Token already expired or invalid — nothing to blacklist.
                pass

        response = Response(
            {"message": "Logged out successfully."},
            status=status.HTTP_200_OK,
        )

        # Standard practice: Use same attributes (path, samesite) 
        # when deleting as were used when setting. 
        # Secure can be omitted in dev but should ideally match.
        response.delete_cookie(_ACCESS_COOKIE,  path="/", samesite="Lax")
        response.delete_cookie(_REFRESH_COOKIE, path="/", samesite="Lax")

        return response


class LoginActivityView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        logs = LoginActivity.objects.filter(
            user=request.user
        ).order_by("-login_time")[:50]

        serializer = LoginActivitySerializer(logs, many=True)

        return Response(serializer.data)