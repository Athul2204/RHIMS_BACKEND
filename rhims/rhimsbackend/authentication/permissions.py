# ═════════════════════════════════════════════════════════════════════════════
# FILE: rhimsbackend/authentication/permissions.py
# COMPLETE FIXED VERSION - Guest Doctor Support
# ═════════════════════════════════════════════════════════════════════════════
# FIXES:
# ✅ Guest doctors (visiting consultants) can now access API
# ✅ Guest doctors properly recognized via guest_doctor_profile relation
# ✅ Role detection handles both regular & guest doctors
# ✅ IsDoctor permission class updated for guest doctors
# ═════════════════════════════════════════════════════════════════════════════

from rest_framework.permissions import BasePermission


def _get_role(user):
    """
    Returns the normalized lowercase role string.
    
    Hierarchy:
    1. Superusers / is_staff with StaffProfile → role from StaffProfile
    2. Guest doctors (users linked via GuestDoctorProfile) → 'doctor'
    3. Common Receptionist (users linked via CommonReceptionistProfile) → 'receptionist'
    4. Common Pharmacist (users linked via CommonPharmacistProfile) → 'pharmacist'
    5. Superusers / is_staff without StaffProfile → 'admin'
    6. Others → None
    
    Normalization: lowercase + strip spaces AND underscores, so all of
    "Lab Technician", "lab technician", "lab_technician" resolve to "labtechnician".
    """
    if not user or not user.is_authenticated:
        return None
    
    # Check for StaffProfile first (regular staff members)
    staff_profile = getattr(user, "staff_profile", None)
    if staff_profile:
        if staff_profile.role:
            return staff_profile.role.lower().replace(" ", "").replace("_", "")
        return None  # profile exists but role unset → deny access cleanly
    
    # ✅ NEW: Check for guest_doctor_profile (visiting/consultant doctors)
    # This must come BEFORE is_staff check so guest doctors return 'doctor'
    guest_doctor_profile = getattr(user, "guest_doctor_profile", None)
    if guest_doctor_profile:
        return "doctor"

    # Common Receptionist / Common Pharmacist — admin-created accounts,
    # each fixed to a single role, independent of StaffProfile. Same
    # equality-based permission checks below work unchanged for them.
    if getattr(user, "common_receptionist_profile", None):
        return "receptionist"

    if getattr(user, "common_pharmacist_profile", None):
        return "pharmacist"
    
    # Fallback for staff users without StaffProfile
    if user.is_staff or user.is_superuser:
        return "admin"
    
    return None


class IsAdminUser(BasePermission):
    message = "Access denied. Admin role required."

    def has_permission(self, request, view):
        return _get_role(request.user) == "admin"


class IsReceptionist(BasePermission):
    message = "Access denied. Receptionist role required."

    def has_permission(self, request, view):
        return _get_role(request.user) == "receptionist"


class IsPharmacist(BasePermission):
    message = "Access denied. Pharmacist role required."

    def has_permission(self, request, view):
        return _get_role(request.user) == "pharmacist"


class IsAdminOrReceptionist(BasePermission):
    message = "Access denied. Admin or Receptionist role required."

    def has_permission(self, request, view):
        return _get_role(request.user) in ("admin", "receptionist")


class IsAdminOrPharmacist(BasePermission):
    message = "Access denied. Admin or Pharmacist role required."

    def has_permission(self, request, view):
        return _get_role(request.user) in ("admin", "pharmacist")


class IsDoctor(BasePermission):
    """
    Allows access to:
    - Regular doctors (DoctorProfile with staff.user relation)
    - Guest doctors (GuestDoctorProfile with user relation)
    
    ✅ FIXED: Now properly recognizes both doctor types
    """
    message = "Access denied. Doctor role required."

    def has_permission(self, request, view):
        return _get_role(request.user) == "doctor"


class IsAdminOrDoctor(BasePermission):
    message = "Access denied. Admin or Doctor role required."

    def has_permission(self, request, view):
        return _get_role(request.user) in ("admin", "doctor")


class IsAdminOrDoctorOrReceptionist(BasePermission):
    """
    Follow-up reminders are created by doctors but managed day-to-day by
    reception (calling patients, booking their revisit, updating status).
    The reminder views' own internal logic already branches on role to
    scope/allow receptionist access — this permission class just needs to
    let that request through the gate instead of blocking it with a 403
    before the view ever runs.
    """
    message = "Access denied. Admin, Doctor, or Receptionist role required."

    def has_permission(self, request, view):
        return _get_role(request.user) in ("admin", "doctor", "receptionist")


class IsAdminOrDoctorOrPharmacistRead(BasePermission):
    """
    Permission class for prescription endpoints.
    
    Allows:
    - Admins: Full access (GET, POST, PATCH, DELETE)
    - Doctors: Full access (GET, POST, PATCH, DELETE) 
    - Pharmacists: READ-ONLY (GET only) for prescription details
    
    Pharmacists need READ access to prescriptions for the billing workflow,
    but should not be able to create, modify, or delete prescriptions.
    """
    message = "Access denied. Insufficient permissions for this operation."

    def has_permission(self, request, view):
        role = _get_role(request.user)
        
        # All authenticated users can READ (GET, HEAD, OPTIONS)
        if request.method in ['GET', 'HEAD', 'OPTIONS']:
            return role in ('admin', 'doctor', 'pharmacist')
        
        # Only admins and doctors can WRITE (POST, PATCH, DELETE, etc)
        return role in ('admin', 'doctor')


class IsAdminDoctorOrPharmacist(BasePermission):
    """
    Permission class that allows access to users with any of these roles:
    - Admin
    - Doctor (regular or guest)
    - Pharmacist
    
    ✅ FIXED: Now supports guest doctors through _get_role()
    """
    message = "Access denied. Admin, Doctor, or Pharmacist role required."

    def has_permission(self, request, view):
        return _get_role(request.user) in ("admin", "doctor", "pharmacist")


class IsLabTechnician(BasePermission):
    message = "Access denied. Lab Technician role required."

    def has_permission(self, request, view):
        return _get_role(request.user) == "labtechnician"


class IsAdminOrLabTechnician(BasePermission):
    message = "Access denied. Admin or Lab Technician role required."

    def has_permission(self, request, view):
        return _get_role(request.user) in ("admin", "labtechnician")


class IsAdminOrDoctorOrLabTechnician(BasePermission):
    """
    For clinical lab data: requests, results, reports, patient lab history.
    Admin/lab techs see everything; doctors are further scoped to their own
    requests by the view's own queryset filtering (see
    lab/views.py:_get_lab_request_for_user). Deliberately excludes
    pharmacist/receptionist/manager — they have no clinical need to read
    patient lab results/reports.
    """
    message = "Access denied. Admin, Doctor, or Lab Technician role required."

    def has_permission(self, request, view):
        return _get_role(request.user) in ("admin", "doctor", "labtechnician")


def _resolve_manager_branch_if_manager(request, role):
    """
    Side effect: for a Manager account with access to more than one branch
    (own home branch + any granted via ManagerBranchAccess), resolve which
    one this request targets and stash it on request.user so
    get_user_branch()/scope_queryset_to_branch() downstream see it without
    every manager view needing to opt in individually. No-op for anything
    else. See authentication/utils.py:resolve_manager_active_branch.
    """
    if role == "manager":
        from authentication.utils import resolve_manager_active_branch
        resolve_manager_active_branch(request)


class IsManager(BasePermission):
    message = "Access denied. Manager role required."

    def has_permission(self, request, view):
        role = _get_role(request.user)
        if role != "manager":
            return False
        _resolve_manager_branch_if_manager(request, role)
        return True


class IsAuthenticatedResolveManagerBranch(BasePermission):
    """
    Same gate as plain IsAuthenticated (any logged-in role may pass), but
    also runs the manager active-branch resolution side effect first.

    Needed for endpoints that are readable by every role (reception,
    pharmacy, admin, manager, ...) but where a multi-branch Manager's
    switched-to branch must still apply to reads, not just to the writes
    gated behind IsAdminOrManager. Without this, a manager's GET falls
    back to their home-branch StaffProfile.branch instead of the branch
    they've switched into, while their POST/PUT/DELETE (behind
    IsAdminOrManager, which does resolve the override) target the
    switched-to branch — reads and writes silently disagree on branch.
    """
    message = "Authentication required."

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        _resolve_manager_branch_if_manager(request, _get_role(request.user))
        return True


class IsAdminOrManager(BasePermission):
    message = "Access denied. Admin or Manager role required."

    def has_permission(self, request, view):
        role = _get_role(request.user)
        _resolve_manager_branch_if_manager(request, role)
        return role in ("admin", "manager")


class IsAdminOrManagerOrPharmacistRead(BasePermission):
    """
    Dealer master data: Admin/Manager get full access (the Dealers page
    lives in the manager module). Pharmacist gets READ-ONLY (GET) access
    so they can pick a dealer when logging stock or requesting a return —
    but cannot add/edit/deactivate dealers themselves.
    """
    message = "Access denied. Insufficient permissions for this operation."

    def has_permission(self, request, view):
        role = _get_role(request.user)
        _resolve_manager_branch_if_manager(request, role)
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return role in ("admin", "manager", "pharmacist")
        return role in ("admin", "manager")


class IsAdminOrManagerOrReceptionistRead(BasePermission):
    """
    Home Visit fee defaults: Admin/Manager get full access (they set the
    defaults). Receptionist gets READ-ONLY (GET) access so BillingPage can
    fetch the current defaults to pre-fill a new Home Visit bill — but
    cannot change the defaults themselves.
    """
    message = "Access denied. Insufficient permissions for this operation."

    def has_permission(self, request, view):
        role = _get_role(request.user)
        _resolve_manager_branch_if_manager(request, role)
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return role in ("admin", "manager", "receptionist")
        return role in ("admin", "manager")


# ═════════════════════════════════════════════════════════════════════════════
# BACKWARDS COMPATIBILITY ALIASES
# ═════════════════════════════════════════════════════════════════════════════
IsAdmin = IsAdminUser