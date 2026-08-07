# authentication/throttling.py
"""
SECURITY: shared throttle classes for endpoints that need stricter limits
than the general DEFAULT_THROTTLE_RATES["user"] (300/minute) — specifically
sensitive admin actions like staff/account creation and hospital settings
changes, where a compromised or malicious admin session (or a script
hammering the endpoint) could otherwise create/modify a large number of
records very quickly with no extra friction.

We deliberately only throttle mutating methods (POST/PUT/PATCH/DELETE) and
let GET/HEAD/OPTIONS pass through under the normal per-user rate — these
views are also used to list/view records (e.g. the staff list page), and
those reads shouldn't be squeezed down to the same tight limit as writes.
"""
from rest_framework.throttling import ScopedRateThrottle

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


class SensitiveWriteThrottle(ScopedRateThrottle):
    """
    ScopedRateThrottle that only applies to write requests. Attach via
    `throttle_classes = [SensitiveWriteThrottle]` and set `throttle_scope`
    on the view (see DEFAULT_THROTTLE_RATES in settings.py for the actual
    rate, e.g. "admin_write").
    """

    def allow_request(self, request, view):
        if request.method in _SAFE_METHODS:
            return True
        return super().allow_request(request, view)


class PublicWriteThrottle(ScopedRateThrottle):
    """
    ScopedRateThrottle for the public, unauthenticated (AllowAny) booking
    and contact-form endpoints (public/views.py). These have no user to
    key on, so DRF falls back to keying by IP — set `throttle_scope =
    "public_write"` on the view (see DEFAULT_THROTTLE_RATES in
    settings.py). Unlike SensitiveWriteThrottle, this applies to every
    request on the view (GET isn't offered on these views at all — they're
    POST-only), so no method filtering is needed here.
    """

    scope = "public_write"