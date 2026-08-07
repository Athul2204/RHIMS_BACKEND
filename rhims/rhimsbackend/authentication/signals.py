# authentication/signals.py
"""
SECURITY: django-axes integration with Django REST Framework.

Axes hooks into django.contrib.auth's authenticate() call via
AxesStandaloneBackend (see AUTHENTICATION_BACKENDS in settings.py), which is
how it sees every login attempt made through CustomTokenObtainPairSerializer
(LoginView) as well as the Django admin login form.

But when an account/IP combination is already locked out, Axes' backend
doesn't raise anything by itself for DRF-based flows — per django-axes' own
docs (Integration with Django REST Framework), you're expected to raise the
lockout yourself off the `user_locked_out` signal. Without this file, a
locked-out login attempt would silently fall through to "invalid username or
password" (technically fine for the user, but we lose the more accurate
"you're locked out, try again in N minutes" messaging), or in some code
paths surface as an unhandled 500.

This receiver raises rest_framework.exceptions.PermissionDenied, which:
  - propagates up through TokenObtainPairSerializer.validate() and
    serializer.is_valid(raise_exception=True) in LoginView.post()
  - is NOT one of the exception types LoginView.post() currently catches
    and rewrites to "Invalid username or password" — see the dedicated
    except clause added there for PermissionDenied specifically, which
    reports the lockout distinctly (still without leaking whether the
    *username* itself exists).
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models.signals import pre_save
from django.dispatch import receiver

from rest_framework.exceptions import PermissionDenied

from axes.signals import user_locked_out


@receiver(user_locked_out)
def raise_permission_denied_on_lockout(*args, **kwargs):
    cooloff = getattr(settings, "AXES_COOLOFF_TIME", None)
    if isinstance(cooloff, timedelta):
        minutes = max(1, int(cooloff.total_seconds() // 60))
        detail = (
            f"Too many failed login attempts. This account is temporarily "
            f"locked. Try again in about {minutes} minutes."
        )
    else:
        detail = (
            "Too many failed login attempts. This account is temporarily "
            "locked. Please try again later or contact an administrator."
        )
    raise PermissionDenied(detail)

# ---------------------------------------------------------------------------
# MODEL-LEVEL VALIDATION: block usernames that differ only by case.
#
# CaseInsensitiveModelBackend (authentication/backends.py) makes LOGIN
# treat "Admin"/"admin"/"ADMIN" as the same account by looking them up with
# username__iexact. But that only helps if just ONE such account exists.
# Nothing before this stopped someone from actually CREATING a second
# account ("admin") while "Admin" already existed — whichever account
# username__iexact happened to return first would "win" unpredictably, and
# creating a third would raise MultipleObjectsReturned and hard-fail every
# login for all of them.
#
# This pre_save receiver closes that gap: it runs on every User save
# (createsuperuser, the Django admin "Add user" form, and any programmatic
# User.objects.create_user() call elsewhere in the codebase) and rejects
# the save outright if a DIFFERENT existing user already has the same
# username under a case-insensitive comparison.
# ---------------------------------------------------------------------------
@receiver(pre_save, sender=get_user_model())
def prevent_case_variant_duplicate_usernames(sender, instance, **kwargs):
    if not instance.username:
        return

    conflict = sender.objects.filter(
        username__iexact=instance.username
    ).exclude(pk=instance.pk).exists()

    if conflict:
        raise ValidationError(
            {"username": "A user with this username already exists "
                          "(usernames are case-insensitive)."}
        )