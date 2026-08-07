# authentication/cookie_auth.py
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError


class CookieJWTAuthentication(JWTAuthentication):
    """
    Reads the access token from the HttpOnly 'access_token' cookie
    instead of the Authorization header.

    JavaScript can never read or steal the token — it is sent
    automatically by the browser on every same-origin request.

    FIX 4: Wrap get_validated_token() in a try/except so that an
    expired or tampered cookie returns None (unauthenticated) instead
    of raising an unhandled exception that bypasses DRF's 401 handler.
    Without this, a stale cookie after a server restart (new SECRET_KEY)
    raised a 500 instead of a clean 401, breaking the refresh flow.
    """

    def authenticate(self, request):
        raw_token = request.COOKIES.get("access_token")
        if raw_token is None:
            return None  # no cookie → unauthenticated (not an error)

        try:
            validated_token = self.get_validated_token(raw_token)
        except (InvalidToken, TokenError):
            # Token is present but invalid/expired — return None so DRF
            # tries the next authenticator (header-based JWT), then 401s.
            return None

        return self.get_user(validated_token), validated_token