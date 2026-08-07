from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import StaffProfile, ReceptionistProfile, PharmacistProfile

# ─────────────────────────────────────────────────────────────────────────
# NOTE: this file used to also write AuditLog entries directly (a
# `_create_log()` helper wired to StaffProfile/ReceptionistProfile/
# PharmacistProfile post_save, and StaffProfile post_delete). That has been
# removed — it resolved "who did this" from the *instance itself*
# (instance.user / instance.staff.user), which for a CREATE is always the
# brand-new account being created, not the admin actually creating it, so
# e.g. a new receptionist's own account showed up as the actor of "this
# account was created". It also duplicated the correctly-attributed entry
# StaffListView/StaffDetailView/ReceptionistListView/PharmacistListView
# etc. already write via administration.audit.log_admin_action(request, ...)
# — every StaffProfile/ReceptionistProfile/PharmacistProfile change was
# logged twice, once right and once wrong.
#
# All of that is now handled by administration.audit's request-attributed
# logging: explicitly via log_admin_action() in administration/views.py for
# these three models specifically, and generically (for every other model
# in every app that doesn't already log itself explicitly) via
# register_audit_signals() — see each app's apps.py ready(). This file now
# only keeps the business logic that has nothing to do with audit logging:
# auto-creating/cleaning-up the role-specific sub-profile whenever a
# StaffProfile is created or its role changes.
# ─────────────────────────────────────────────────────────────────────────


# ── Role → profile model mapping ──────────────────────────────────
# Used for auto-creating / cleaning up role-specific profiles.
# Import DoctorProfile lazily inside the receiver to avoid a circular
# import at module load time (doctor app imports administration models).
def _get_role_profile_map():
    from doctor.models import DoctorProfile
    return {
        "Receptionist": ReceptionistProfile,
        "Pharmacist":   PharmacistProfile,
        "Doctor":       DoctorProfile,
    }


@receiver(post_save, sender=StaffProfile)
def handle_staff_post_save(sender, instance, created, **kwargs):
    role_profile_map = _get_role_profile_map()

    if created:
        # Auto-create the role-specific sub-profile for new staff.
        profile_model = role_profile_map.get(instance.role)
        if profile_model:
            profile_model.objects.get_or_create(staff=instance)

    else:
        # On role CHANGE: delete stale profiles from the old role, then
        # ensure the new role's profile exists.
        #
        # Example: a Receptionist is promoted to Doctor. The old
        # ReceptionistProfile is deleted so only DoctorProfile remains.
        for role, profile_model in role_profile_map.items():
            if role != instance.role:
                profile_model.objects.filter(staff=instance).delete()

        current_profile_model = role_profile_map.get(instance.role)
        if current_profile_model:
            current_profile_model.objects.get_or_create(staff=instance)
