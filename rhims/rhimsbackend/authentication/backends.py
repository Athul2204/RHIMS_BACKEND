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