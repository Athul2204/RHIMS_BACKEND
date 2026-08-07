"""
Django settings for RHIMS HEALTH backend.
Roles supported: Admin, Receptionist, Pharmacist, Doctor, Lab Technician
"""

from pathlib import Path
import os
from datetime import timedelta

try:
    from dotenv import load_dotenv # type: ignore
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY: DEBUG now defaults to False. Dev environments must opt in
# explicitly via .env (DEBUG=True) instead of the app silently running in
# debug mode (full tracebacks, settings dump on 500s) whenever the env var
# is missing.
DEBUG = os.getenv("DEBUG", "False") == "True"

# SECURITY: no more hardcoded fallback secret. In production this MUST come
# from the environment / secrets manager. We only allow the insecure dev
# fallback while DEBUG=True, so a misconfigured production deployment fails
# at startup instead of silently signing JWTs with a key anyone can read
# in this file's git history.
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "unsafe-dev-key-change-in-production"
    else:
        raise RuntimeError(
            "SECRET_KEY environment variable is not set. Refusing to start "
            "with DEBUG=False and no SECRET_KEY — generate one (e.g. "
            "`python -c \"from django.core.management.utils import "
            "get_random_secret_key; print(get_random_secret_key())\"`) and "
            "set it in the environment/.env, never in source control."
        )

# SECURITY: 0.0.0.0 / wildcard-style hosts should only be present in dev.
# ALLOWED_HOSTS is now driven by env so each deployment (dev/staging/prod)
# declares only the hostnames it actually serves.
_default_hosts = "127.0.0.1,localhost,0.0.0.0" if DEBUG else ""
ALLOWED_HOSTS = [
    h.strip() for h in os.getenv("ALLOWED_HOSTS", _default_hosts).split(",") if h.strip()
]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'django_filters',
    # SECURITY: account lockout / brute-force protection. See AUTHENTICATION_BACKENDS
    # and AXES_* settings below, plus authentication/signals.py for the DRF hookup.
    'axes',

    # Custom apps
    'authentication',
    'administration',
    'reception',
    'doctor',
    'lab',
    'pharmacist',
    'manager',
]

MIDDLEWARE = [
    # CorsMiddleware MUST be first so it can handle pre-flight OPTIONS before
    # any other middleware touches the request.
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # SECURITY: Content-Security-Policy header (see middleware.py for why
    # this is a small in-house middleware rather than django-csp).
    'rhimsbackend.middleware.ContentSecurityPolicyMiddleware',
    # SECURITY: must come after AuthenticationMiddleware. Tracks failed
    # logins server-side and enforces lockouts for both the DRF login
    # endpoint (via AxesStandaloneBackend + the signal in
    # authentication/signals.py) and the Django admin login form.
    'axes.middleware.AxesMiddleware',
    # Makes the current request available to model signal handlers so the
    # audit-log auto-logger (administration/audit.py) can attribute every
    # generically-logged change to the actual person making the request.
    'rhimsbackend.middleware.CurrentRequestMiddleware',
]

ROOT_URLCONF = 'rhimsbackend.urls'
WSGI_APPLICATION = 'rhimsbackend.wsgi.application'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.getenv('DB_NAME', 'rhims_temp'),
        'USER': os.getenv('DB_USER', 'root'),
        'PASSWORD': os.getenv('DB_PASSWORD', '1234'),
        'HOST': os.getenv('DB_HOST', 'localhost'),
        'PORT': os.getenv('DB_PORT', '3306'),
        # BUG FIX: charset must match how the database/tables were created.
        # Without this, Django may fail to connect if the MySQL server defaults
        # to utf8mb4 and the connection charset mismatches, causing seemingly
        # random OperationalError on first query — which surfaces as a 500 on
        # the login endpoint (obscured as "Invalid username or password").
        'OPTIONS': {
            'charset': 'utf8mb4',
            # SECURITY/DATA INTEGRITY: MySQL Strict Mode escalates data-
            # truncation and similar silent-corruption warnings into hard
            # errors on INSERT/UPDATE, instead of quietly truncating or
            # coercing bad values. Off by default on many MySQL installs
            # (including PythonAnywhere's), which is what Django's W002
            # system check warns about.
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }
}

# SECURITY: AxesStandaloneBackend must be first — it intercepts every
# authenticate() call (JWT login via CustomTokenObtainPairSerializer, AND
# the Django admin login form) to check for an existing lockout *before*
# ModelBackend ever touches the password. It never itself confirms a
# password; ModelBackend still owns that.
AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# ── django-axes: brute-force / credential-stuffing lockout ─────────────────
# This is a second, independent layer from the DRF "login" throttle above
# (REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["login"] = 5/minute):
#   - The DRF throttle is fast and cheap (in-process cache, keyed by IP),
#     resets every minute, and blunts a rapid-fire script.
#   - Axes is slower and persisted to the database, keyed by the
#     (username, IP) combination, and survives across throttle resets — it
#     also protects the separate /admin/ login form, which the DRF throttle
#     never touches.
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=15)

# Lock on the combination of username + IP rather than IP alone: several
# staff terminals in the same clinic can share one IP, and locking by IP
# alone would let one receptionist mistyping a password lock out doctors
# and pharmacists on the same network. Locking by username alone would let
# an attacker rotate IPs to keep hammering one account — combining both
# means an attacker still gets locked out per (account, IP) pair.
# (AXES_LOCKOUT_PARAMETERS is the modern replacement for the deprecated
# AXES_LOCK_OUT_BY_COMBINATION_USER_AND_IP flag — a list of lists means
# "combine these fields together".)
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]

# A successful login clears that account's failure count immediately,
# rather than making a legitimate user wait out the cooloff after they
# eventually get the password right.
AXES_RESET_ON_SUCCESS = True

# NOTE: the trusted-proxy IP setting (AXES_IPWARE_META_PRECEDENCE_ORDER) is
# set further down, right after TRUST_PROXY_HEADERS is defined, so it can
# reuse that same decision instead of duplicating the env-parsing logic here.

# Persist attempts to the database (default AxesDatabaseHandler) rather than
# a local-memory cache — this is a single-server deployment for now, but a
# DB-backed lockout also means `python manage.py axes_reset` and the Django
# admin's "Access attempts" list stay authoritative even if the process
# restarts, unlike an in-memory cache.

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

# BUG FIX: Without DEFAULT_AUTO_FIELD Django 3.2+ warns and some third-party
# packages behave inconsistently when migrations are created by different
# Django versions.
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'static'
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

REST_FRAMEWORK = {
    # FIX: Django's core ValidationError (raised by model.full_clean() inside
    # almost every save() override in this project) and DB IntegrityError
    # were previously uncaught by DRF, surfacing as bare 500s on POST/PATCH
    # for batches, supplies, and dealers. See rhimsbackend/exceptions.py.
    "EXCEPTION_HANDLER": "rhimsbackend.exceptions.custom_exception_handler",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "authentication.cookie_auth.CookieJWTAuthentication",  # cookie first
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend"
    ],

    # SECURITY: DRF's HTML "Browsable API" is convenient in dev but has no
    # place in production — it's an extra navigable surface that exposes
    # form fields, allowed methods, and endpoint structure to anyone poking
    # around in a browser. JSON-only outside DEBUG.
    #
    # NOTE: deliberately NOT setting DEFAULT_PAGINATION_CLASS here.
    # reception/views.py has PatientListView, ConsultationBillListView, and
    # PatientConsultationHistoryView as ListAPIView with no pagination_class
    # override — they currently return bare JSON arrays, which the frontend
    # consumes directly. A global pagination default would silently wrap
    # those responses in {count, next, previous, results: [...]} and break
    # every frontend call site expecting an array. If you want pagination
    # (recommended for these specific endpoints as patient/consultation
    # tables grow), add `pagination_class = PageNumberPagination` to each
    # view individually AND update the matching frontend code in the same
    # change — don't flip this as a blanket setting.
    "DEFAULT_RENDERER_CLASSES": (
        ["rest_framework.renderers.JSONRenderer", "rest_framework.renderers.BrowsableAPIRenderer"]
        if DEBUG else
        ["rest_framework.renderers.JSONRenderer"]
    ),

    # SECURITY: rate limiting. "login" is a custom scope applied explicitly
    # to LoginView (see authentication/views.py) to blunt password-guessing/
    # credential-stuffing attempts. General anon/user throttles are a
    # sane baseline for the rest of the API.
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/minute",
        "user": "300/minute",
        "login": "5/minute",
        # Used by SensitiveWriteThrottle (authentication/throttling.py) on
        # admin write endpoints, e.g. staff creation in
        # administration/views.py. Tighter than the general "user" rate
        # since these are sensitive, low-frequency admin actions.
        "admin_write": "20/minute",
        # Used by PublicWriteThrottle (authentication/throttling.py) on the
        # public, unauthenticated booking/contact endpoints. Tighter than
        # the general "anon" rate since these create DB records (a
        # ConsultationPreBooking / PatientQuery per request) rather than
        # just reading data.
        "public_write": "10/hour",
    },
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME':  timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    'AUTH_HEADER_TYPES': ('Bearer',),
    'SIGNING_KEY': SECRET_KEY,

    # Issue a fresh refresh token on every /api/auth/refresh/ call and
    # immediately blacklist the old one.  This gives each refresh token a
    # single-use lifetime: a stolen token is invalidated the moment the
    # legitimate client next refreshes, and a proactively stolen-then-used
    # token is caught on the next legitimate refresh attempt.
    'ROTATE_REFRESH_TOKENS':    True,
    'BLACKLIST_AFTER_ROTATION': True,
}

# ── CORS ─────────────────────────────────────────────────────────────────────
# SECURITY: driven by env so prod lists only the real frontend origin(s)
# instead of inheriting the dev Vite ports.
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",") if o.strip()
]
CORS_ALLOW_CREDENTIALS = True

# BUG FIX: Explicitly allow the headers that Axios sends.
# Without this, django-cors-headers strips Content-Type from pre-flight
# responses, which causes browsers to block the actual POST — making
# login look like a network error rather than a CORS error.
CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
    # Client-generated per-browser device identifier (see
    # administration.audit.get_current_device_id / frontend deviceId
    # util) — sent on every API call so AuditLog/UserDevice can tell
    # devices apart even when they share one IP.
    "x-device-id",
]

# BUG FIX: Django 4.0+ requires CSRF_TRUSTED_ORIGINS for any request whose
# Origin/Referer doesn't match the Host header (common in dev with Vite proxy
# and in production behind a reverse proxy). DRF API views are already
# csrf_exempt, but the Django admin panel (/admin/) and any Django-rendered
# views need this. Without it, admin POSTs (password change, login) get 403.
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.getenv(
        "CSRF_TRUSTED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000"
    ).split(",") if o.strip()
]

# SECURITY: only set TRUST_PROXY_HEADERS=True once you've confirmed the
# reverse proxy/load balancer in front of this app strips any client-sent
# X-Forwarded-For and sets its own — see authentication/utils.get_client_ip().
#
# Deployed on PythonAnywhere: their load balancer only guarantees the
# LAST entry of X-Forwarded-For and the X-Real-IP header, never the
# whole X-Forwarded-For list — see get_client_ip()'s docstring.
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "False") == "True"

# SECURITY: same trusted-proxy decision as above, reused for django-axes'
# IP resolution (see AXES_* block earlier in this file) so lockouts are
# keyed by the real client IP behind a verified reverse proxy, and by
# REMOTE_ADDR (which the client cannot spoof) everywhere else.
# X-Real-IP first (PythonAnywhere sets this directly, never client-
# settable), X-Forwarded-For as fallback — django-ipware (which axes
# uses under the hood) reads the LAST entry of X-Forwarded-For by
# default, matching PythonAnywhere's own guarantee, so no extra config
# is needed there the way get_client_ip() needs the explicit [-1].
if TRUST_PROXY_HEADERS:
    AXES_IPWARE_META_PRECEDENCE_ORDER = ['HTTP_X_REAL_IP', 'HTTP_X_FORWARDED_FOR', 'REMOTE_ADDR']
    # SECURITY: without this, SECURE_SSL_REDIRECT (below) causes an infinite
    # redirect loop on PythonAnywhere. Their proxy terminates HTTPS and
    # forwards the request to this app over plain HTTP, so Django has no
    # way to know the original request was secure unless told which header
    # carries that signal — it then redirects to HTTPS, the proxy strips
    # SSL again on the way in, and the loop repeats. Only enable this once
    # you've confirmed PythonAnywhere sets X-Forwarded-Proto unconditionally
    # (never passes through a client-supplied value) — same trust
    # requirement as TRUST_PROXY_HEADERS above, which is why this is gated
    # behind that same flag rather than always on.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY    = False   # Must be readable by JS for the admin panel

# SECURITY: Secure flag now defaults to "on whenever we're not in DEBUG".
# Previously this was hardcoded False, meaning auth cookies (which carry the
# JWTs) would be sent over plain HTTP even in production. Override with
# SECURE_COOKIES=False in .env only for a non-HTTPS staging box.
_secure_cookies = os.getenv("SECURE_COOKIES", "" if DEBUG else "True") == "True"
SESSION_COOKIE_SECURE = _secure_cookies
CSRF_COOKIE_SECURE    = _secure_cookies

if not DEBUG:
    # Standard hardening once this is actually deployed behind HTTPS.
    SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "True") == "True"
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True