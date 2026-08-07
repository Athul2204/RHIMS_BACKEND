# authentication/utils.py
import secrets
from django.conf import settings


def get_client_ip(request):
    """
    Extract the client IP for audit logging.

    SECURITY: X-Forwarded-For and X-Real-IP are attacker-controlled unless a
    trusted proxy sits in front of this app and is configured to strip/
    overwrite any client-supplied value before setting its own. We only
    trust them when TRUST_PROXY_HEADERS=True is explicitly set in the
    environment (set this only if you've verified your reverse proxy/load
    balancer does that stripping) — otherwise we always use REMOTE_ADDR,
    which the client cannot spoof.

    Deployed on PythonAnywhere: their load balancer sets X-Real-IP directly
    (never client-settable) and only guarantees the LAST entry of
    X-Forwarded-For, not the first — a client can prepend arbitrary values
    of their own to X-Forwarded-For, so reading index [0] instead of [-1]
    is spoofable even with a trusted proxy in front. Precedence here matches
    AXES_IPWARE_META_PRECEDENCE_ORDER in settings.py: X-Real-IP first, then
    the last hop of X-Forwarded-For, then REMOTE_ADDR.
    """
    if getattr(settings, "TRUST_PROXY_HEADERS", False):
        x_real_ip = request.META.get('HTTP_X_REAL_IP')
        if x_real_ip:
            return x_real_ip.strip()
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[-1].strip()
    return request.META.get('REMOTE_ADDR')


def generate_otp():
    """
    Generate a 6-digit OTP using a cryptographically secure RNG.

    SECURITY: random.randint() is not suitable for anything
    security-sensitive (predictable given enough samples / seed exposure).
    secrets.randbelow() is CSPRNG-backed.
    """
    return 100000 + secrets.randbelow(900000)


# FIX 3 & 5: Centralized role normalization used by the serializer, MeView,
# and permissions.py.  Previously each site did its own string manipulation
# and they diverged: the serializer used .replace(" ", "_") while permissions
# used .replace(" ", "").replace("_", ""), so "Lab Technician" became
# "lab_technician" in JWT claims but "labtechnician" in permission checks —
# the permission check always failed for lab techs.
def normalize_role(role_str):
    """
    Return a canonical, lowercase, whitespace-and-underscore-free role string.

    Examples:
        "Lab Technician"  → "labtechnician"
        "lab_technician"  → "labtechnician"
        "Receptionist"    → "receptionist"
        "admin"           → "admin"
    """
    if not role_str:
        return None
    return role_str.lower().replace(" ", "").replace("_", "")


# ─────────────────────────────────────────────────────────────────────────────
# Branch scoping helpers
#
# Used throughout administration/ and reception/ (views.py, serializers.py)
# to enforce multi-branch data isolation. A user is either:
#   - a "group admin" (the original superuser, or anyone they've explicitly
#     promoted via StaffProfile.is_group_admin) — sees/writes across every
#     branch, optionally narrowed by an explicit ?branch=/data["branch"], or
#   - an ordinary branch-scoped user — always forced to their own
#     StaffProfile.branch, regardless of any branch param they pass.
# ─────────────────────────────────────────────────────────────────────────────

def is_group_admin_user(user):
    """
    True only for the original superuser, or a staff member explicitly
    promoted via StaffProfile.is_group_admin=True. This — not role=='Admin'
    — is what grants unrestricted cross-branch access. See StaffProfile's
    is_group_admin field docstring in administration/models.py.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    staff = getattr(user, "staff_profile", None)
    return bool(staff and staff.is_group_admin)


def get_user_branch(user):
    """
    Return the Branch instance this user's own StaffProfile is scoped to,
    or None if they have no staff_profile or are branch-less (group admins
    are the only accounts allowed a null branch).

    Checks `_active_branch_override` first — set by
    resolve_manager_active_branch() for a Manager account with 2+
    accessible branches (their own home branch plus any granted via
    ManagerBranchAccess) who has switched into one of the extra branches
    for this request. Every other account type never has this attribute
    set, so this is a no-op for them.
    """
    staff = getattr(user, "staff_profile", None)
    if not staff:
        return None
    override = getattr(user, "_active_branch_override", None)
    if override is not None:
        return override
    return staff.branch


def get_user_branch_any(user):
    """
    Like get_user_branch(), but also covers the two other login-capable
    profile types that carry their own `branch` FK instead of going through
    StaffProfile: GuestDoctorProfile (nullable branch — a guest doctor can
    be cross-branch) and CommonReceptionistProfile/CommonPharmacistProfile
    (required branch). Checked in the same precedence order the login
    serializer and MeView already use for role resolution.

    Used for both: (a) surfacing branch info on the login/me responses,
    and (b) resolve_branch_for_write's non-group-admin branch, so any
    branch-scoped account type can create/write records into its own
    branch — not just StaffProfile-backed accounts.
    """
    staff = getattr(user, "staff_profile", None)
    if staff:
        override = getattr(user, "_active_branch_override", None)
        if override is not None:
            return override
        return staff.branch

    guest_doctor = getattr(user, "guest_doctor_profile", None)
    if guest_doctor:
        return guest_doctor.branch

    common_receptionist = getattr(user, "common_receptionist_profile", None)
    if common_receptionist:
        return common_receptionist.branch

    common_pharmacist = getattr(user, "common_pharmacist_profile", None)
    if common_pharmacist:
        return common_pharmacist.branch

    return None


def serialize_branch(branch):
    """
    Return the (branch_id, branch_code, branch_name) triple the login/me
    responses embed, or all-None if the account isn't tied to one branch
    (a branch-less group admin, or a cross-branch guest doctor).
    """
    if branch is None:
        return None, None, None
    return branch.pk, branch.code, branch.name


def scope_queryset_to_branch(qs, user, branch_field="branch", branch_id=None):
    """
    Restrict `qs` to the caller's own branch, unless they're a group admin.

    branch_field: the path from `qs`'s model to its Branch FK, e.g. "branch"
        (direct) or "staff__branch" (via a related staff row). Supports the
        same double-underscore relation traversal Django filters use.
    branch_id: an optional explicit branch to narrow to. Only ever honored
        for a group admin (an optional drill-down, e.g. the superuser's
        "?branch=3" query param) — an ordinary branch-scoped user cannot use
        this to see outside their own branch, so it's ignored for them.

    A branch-scoped user with no branch on their own StaffProfile (shouldn't
    normally happen, but defensively) gets an empty queryset rather than an
    unfiltered one — fail closed, not open.
    """
    lookup_key = f"{branch_field}_id"

    if is_group_admin_user(user):
        if branch_id:
            return qs.filter(**{lookup_key: branch_id})
        return qs

    # ✅ FIX: previously used get_user_branch(), which ONLY checks
    # user.staff_profile. Accounts created via the "Common" admin flow
    # (CommonPharmacistProfile / CommonReceptionistProfile) or guest
    # doctors (GuestDoctorProfile) don't have a staff_profile, so this
    # always resolved to None for them and every list endpoint silently
    # returned qs.none() — e.g. a Common Pharmacist could successfully
    # create a medicine (once resolve_branch_for_write below was fixed
    # the same way) but the medicines list would still show 0 results,
    # because "fail closed" here meant "always empty" rather than "empty
    # only when truly branch-less". get_user_branch_any() checks all
    # four login-capable profile types, same as resolve_branch_for_write.
    branch = get_user_branch_any(user)
    if branch is None:
        return qs.none()
    return qs.filter(**{lookup_key: branch.pk})


def resolve_branch_for_write(request, required=True):
    """
    Resolve which Branch a POST/create should be scoped to, for models with
    a *direct* branch field (branch_lookup == "branch" callers).

    Returns (branch, error):
      - branch: a Branch instance to assign, or None.
      - error: None on success, or a ready-to-return DRF Response (400) with
        body {"errors": {...}} describing what went wrong.

    Behavior:
      - Ordinary (non-group-admin) staff: always resolved to their own
        StaffProfile.branch — they can never write into another branch by
        passing a different one in the request body. If required=True and
        they somehow have no branch of their own, that's an error.
      - Group admin: must explicitly supply request.data["branch"] (a
        branch id) themselves, since they aren't scoped to any one branch.
        If required=False and none is supplied, branch=None is a valid
        result (e.g. a cross-branch GuestDoctorProfile).
    """
    from rest_framework.response import Response
    from rest_framework import status as drf_status

    user = request.user

    if is_group_admin_user(user):
        branch_id = request.data.get("branch")
        if branch_id in (None, "", "null"):
            if required:
                return None, Response(
                    {"errors": {"branch": "Branch is required."}},
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )
            return None, None

        from administration.models import Branch
        try:
            branch = Branch.objects.get(pk=branch_id)
        except (Branch.DoesNotExist, ValueError, TypeError):
            return None, Response(
                {"errors": {"branch": "Invalid branch selected."}},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        return branch, None

    # ✅ FIX: previously used get_user_branch(), which ONLY checks
    # user.staff_profile. Accounts created via the "Common" admin flow
    # (CommonPharmacistProfile / CommonReceptionistProfile) or guest
    # doctors (GuestDoctorProfile) don't have a staff_profile at all, so
    # every create/write request from those accounts hit the "Your
    # account is not assigned to a branch" error below even though the
    # account clearly has one — e.g. a Common Pharmacist could browse
    # medicines but never create one. get_user_branch_any() checks all
    # four login-capable profile types in the same precedence order the
    # login/me endpoints already use.
    branch = get_user_branch_any(user)
    if branch is None and required:
        return None, Response(
            {"errors": {"branch": "Your account is not assigned to a branch. Contact a group admin."}},
            status=drf_status.HTTP_400_BAD_REQUEST,
        )
    return branch, None


# ─────────────────────────────────────────────────────────────────────────────
# Manager multi-branch access
#
# A Manager account is still exactly one StaffProfile/login — a group admin
# can additionally grant it access to other branches (ManagerBranchAccess),
# so the same manager can switch between their home branch and any granted
# ones without a separate account per branch. See administration/models.py
# for the grant model and BranchSwitcher-equivalent frontend component.
# ─────────────────────────────────────────────────────────────────────────────

def get_manager_accessible_branches(user):
    """
    Return the ordered list of Branch objects this Manager can work in:
    their own home branch first (if any), then every branch a group admin
    has explicitly granted via ManagerBranchAccess, alphabetically.

    Returns [] for any non-Manager account — callers use this to decide
    whether a branch switcher is even relevant, so an empty list is the
    correct "nothing to switch" answer for every other role too.
    """
    staff = getattr(user, "staff_profile", None)
    if not staff or staff.role != "Manager":
        return []

    branches = []
    if staff.branch_id:
        branches.append(staff.branch)

    from administration.models import ManagerBranchAccess
    extra = (
        ManagerBranchAccess.objects
        .filter(manager_id=staff.pk)
        .select_related("branch")
        .order_by("branch__name")
    )
    branches.extend(access.branch for access in extra)
    return branches


def resolve_manager_active_branch(request):
    """
    For a Manager account, resolve which branch this request is scoped to
    and stash it on request.user as `_active_branch_override` so every
    existing get_user_branch() / get_user_branch_any() /
    scope_queryset_to_branch() call downstream picks it up transparently —
    none of manager/views.py's many call sites need to change.

    Resolution:
      - Reads `branch` from the query string (GET/list) or the request
        body (POST/create), same param name the group-admin drill-down
        already uses elsewhere.
      - Must be one of this manager's own accessible branches (home branch
        + anything granted via ManagerBranchAccess) — anything else is
        ignored, never honored, so a manager can't reach a branch nobody
        granted them just by passing a different id.
      - Defaults to their home branch when nothing valid was supplied.

    No-op (leaves get_user_branch() falling through to staff.branch as
    before) for non-Manager accounts, or a Manager with only one
    accessible branch — there's nothing to switch between.
    """
    user = request.user
    staff = getattr(user, "staff_profile", None)
    if not staff or staff.role != "Manager":
        return

    accessible = get_manager_accessible_branches(user)
    if len(accessible) <= 1:
        return

    requested = None
    query_params = getattr(request, "query_params", None)
    if query_params is not None:
        requested = query_params.get("branch")
    if requested in (None, "", "null"):
        data = getattr(request, "data", None)
        if data is not None:
            requested = data.get("branch")

    chosen = None
    if requested not in (None, "", "null"):
        try:
            requested_id = int(requested)
        except (TypeError, ValueError):
            requested_id = None
        if requested_id is not None:
            chosen = next((b for b in accessible if b.pk == requested_id), None)

    if chosen is None:
        chosen = accessible[0]  # default: their own home branch

    user._active_branch_override = chosen


def strip_privileged_fields(data, fields, user, bypass_check=None):
    """
    Defense-in-depth: drop `fields` (e.g. ["is_group_admin"]) from an
    incoming request body unless `bypass_check(user)` says the caller is
    allowed to set them (default bypass_check is is_group_admin_user).

    This exists as a second layer behind the serializer's own
    read_only_fields (see StaffProfileSerializer.Meta) — it protects the
    view even if a future edit to the serializer accidentally makes one of
    these fields writable again. Only StaffPromoteGroupAdminView, which
    sets the field directly on the model (not through this serializer/view
    path), is meant to ever change is_group_admin.

    Returns a (possibly copied) dict with the fields removed; the input is
    left untouched if nothing needed stripping.
    """
    check = bypass_check or is_group_admin_user
    if check(user):
        return data
    if any(f in data for f in fields):
        data = data.copy()
        for f in fields:
            data.pop(f, None)
    return data