"""
Custom authentication backend(s) for RHIMS HEALTH.

Save this file as: authentication/backends.py
(alongside authentication/views.py, authentication/signals.py, etc.)
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class CaseInsensitiveModelBackend(ModelBackend):
    """
    Same as Django's default ModelBackend, except the username lookup is
    case-insensitive. "Admin", "admin", and "ADMIN" all match the same
    account at login time.

    NOT currently wired into AUTHENTICATION_BACKENDS (see settings.py) -
    kept here in case case-insensitive login is wanted again later.

    NOTE: this only affects the *lookup* used during login. It does not
    enforce case-insensitive uniqueness at the database level. If two
    accounts could ever be created whose usernames differ only by case
    (e.g. "Admin" and "admin" as separate rows), get() below would raise
    MultipleObjectsReturned. That's not a concern for a small, admin-created
    staff user base, but if usernames become self-service/public-facing,
    add a case-insensitive uniqueness constraint on the username field.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None

        UserModel = get_user_model()
        try:
            user = UserModel.objects.get(username__iexact=username)
        except UserModel.DoesNotExist:
            # Run the default password hasher anyway to keep timing
            # consistent whether or not the username exists, mirroring
            # ModelBackend's own behavior against user enumeration.
            UserModel().set_password(password)
            return None
        except UserModel.MultipleObjectsReturned:
            # Two accounts differ only by case - fail closed rather than
            # guessing which one was meant.
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None


class StrictCaseModelBackend(ModelBackend):
    """
    Behaves like Django's default ModelBackend, EXCEPT it fixes a case-
    sensitivity leak caused by MySQL's default collation.

    MySQL's default collation (utf8mb4_general_ci / utf8mb4_0900_ai_ci) is
    case-INSENSITIVE, so a plain `username=<value>` WHERE clause matches a
    differently-cased row regardless of what Django's Python code does.
    ModelBackend's own authenticate() does exactly this plain lookup, so on
    this database, logging in as "athul" would silently succeed against an
    account actually created as "Athul" - not because Django chose to allow
    it, but because MySQL folded the case before Django ever saw the row.

    This backend closes that gap the same way authentication/serializers.py
    (CustomTokenObtainPairSerializer) already does for the JWT/API login:
    let the (case-insensitive-at-the-DB-level) lookup happen, then
    explicitly re-check the returned user's actual stored username against
    what was typed, in Python, and reject on any case mismatch.

    Currently wired into AUTHENTICATION_BACKENDS in place of plain
    'django.contrib.auth.backends.ModelBackend' - see settings.py.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None

        UserModel = get_user_model()
        username_field = UserModel.USERNAME_FIELD  # normally "username"

        try:
            user = UserModel._default_manager.get_by_natural_key(username)
        except UserModel.DoesNotExist:
            # Run the default password hasher anyway to keep timing
            # consistent whether or not the username exists.
            UserModel().set_password(password)
            return None

        # The DB lookup above may have matched case-insensitively (MySQL's
        # doing, not Django's) - reject if the stored username's casing
        # doesn't exactly match what was typed.
        if getattr(user, username_field) != username:
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None