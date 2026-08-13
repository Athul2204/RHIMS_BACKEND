from django.db import models, transaction
from django.core.validators import RegexValidator, MinValueValidator
from django.utils import timezone
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from decimal import Decimal


# ─────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────
def calculate_age(dob):
    today = timezone.localdate()
    return (today - dob).days // 365 if dob else 0


# ─────────────────────────────────────────────
# BRANCH
# ─────────────────────────────────────────────
class Branch(models.Model):
    """
    A physical hospital location. Everything transactional/operational
    (staff, patients, billing, stock, salary) hangs off a Branch via FK.
    Public-website/CMS content (manager app) stays branch-less/shared.
    """

    branch_id = models.AutoField(primary_key=True)

    name = models.CharField(max_length=200, unique=True)

    code = models.CharField(
        max_length=10,
        unique=True,
        db_index=True,
        help_text="Short prefix used in staff codes / bill numbers, e.g. TVM, KOL.",
    )

    address = models.TextField(blank=True, null=True)

    phone = models.CharField(
        max_length=15,
        blank=True,
        null=True,
        validators=[RegexValidator(r'^\+?\d{9,15}$')],
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.code:
            self.code = self.code.strip().upper()
        if not self.name or not self.name.strip():
            raise ValidationError({"name": "Branch name cannot be blank."})
        if not self.code:
            raise ValidationError({"code": "Branch code cannot be blank."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} — {self.name}"

    class Meta:
        ordering = ["name"]


# ─────────────────────────────────────────────
# STAFF PROFILE (CORE MODEL)
# ─────────────────────────────────────────────
class StaffProfile(models.Model):

    ROLE_CHOICES = [
        ("Receptionist", "Receptionist"),
        ("Pharmacist", "Pharmacist"),
        ("Admin", "Admin"),
        ("Doctor", "Doctor"),
        ("Lab Technician", "Lab Technician"),
        ("Manager", "Manager"),
    ]

    STAFF_CODE_PREFIX = {
        "Receptionist": "REC",
        "Pharmacist":   "PHARM",
        "Admin":        "ADM",
        "Doctor":       "DOC",
        "Lab Technician": "LAB",
        "Manager":      "MGR",
    }

    id = models.AutoField(primary_key=True)

    branch = models.ForeignKey(
        "Branch",
        on_delete=models.PROTECT,
        related_name="staff_profiles",
        null=True,
        blank=True,
        help_text=(
            "Required for every ordinary staff account. Left null only for the "
            "original group-admin superuser account, which is not scoped to "
            "any single branch."
        ),
    )

    # True only for the original superuser and anyone they explicitly
    # promote. This — not role=='Admin' — is what actually grants
    # is_superuser and the branch-filter bypass. A branch Admin created by
    # the superuser has role='Admin' but is_group_admin=False, and stays
    # scoped to their own branch like everyone else. Settable only through
    # an admin action gated to existing group admins (enforced in the
    # view/serializer layer, not here).
    is_group_admin = models.BooleanField(default=False)

    staff_code = models.CharField(
        max_length=20, editable=False, db_index=True
    )

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="staff_profile",
    )

    role = models.CharField(max_length=20, choices=ROLE_CHOICES)

    phone = models.CharField(
        max_length=15,
        validators=[RegexValidator(r'^\+?\d{9,15}$')],
        null=True,
        blank=True,
    )

    date_of_birth = models.DateField(null=True, blank=True)

    address = models.TextField(blank=True, null=True)

    qualification = models.CharField(max_length=255, default="Not Specified")

    SALARY_TYPE_CHOICES = [
        ("Monthly", "Monthly"),
        ("Weekly",  "Weekly"),
        ("Daily",   "Daily"),
    ]

    # Pay basis — mirrors SupportStaff.salary_type. Determines which rate
    # field below is the "primary" one for this staff member; the others
    # are purely reference figures shown to the manager on the Salary
    # sheet and are never auto-computed into net_salary.
    salary_type = models.CharField(max_length=10, choices=SALARY_TYPE_CHOICES, default="Monthly")

    salary = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        default=10000,
        help_text="Monthly salary amount. Used as-is when salary_type is Monthly.",
    )

    # For daily-rate staff — if 0, derived from salary / mandatory_working_days
    # for display purposes only (see reference_rate()).
    daily_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Reference daily pay rate. If left as 0, derived from salary for display purposes.",
    )

    # For weekly-rate staff — if 0, derived from daily_rate × 7 for display.
    weekly_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Reference weekly pay rate. If left as 0, derived from daily_rate × 7 for display purposes.",
    )

    # Minimum number of duty days required this month — mirrors
    # SupportStaff.mandatory_working_days. Used both as the monthly↔daily
    # rate bridge in reference_rate() and shown on the Salary sheet next
    # to Present/Absent for reference.
    mandatory_working_days = models.PositiveSmallIntegerField(default=26)

    joining_date = models.DateField(default=timezone.localdate)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    updated_at = models.DateTimeField(auto_now=True)

    # ─────────────────────────────
    # VALIDATION
    # ─────────────────────────────
    def clean(self):
        if not self.is_group_admin and not self.branch_id:
            raise ValidationError({"branch": "Branch is required for every staff account except the group admin."})

        if self.date_of_birth:
            if self.date_of_birth > timezone.localdate():
                raise ValidationError({"date_of_birth": "Date of birth cannot be in the future."})

            min_age = {
                "Receptionist": 21,
                "Pharmacist": 23,
                "Admin": 21,
                "Doctor": 25,
                "Lab Technician": 21,
                "Manager": 24,
            }.get(self.role, 21)

            if calculate_age(self.date_of_birth) < min_age:
                raise ValidationError({
                    "date_of_birth": f"{self.role} must be at least {min_age} years old."
                })

        if self.salary and not (1 <= self.salary <= 1_000_000):
            raise ValidationError({"salary": "Salary must be between ₹1 and ₹10,00,000."})

        if self.joining_date and self.joining_date > timezone.localdate():
            raise ValidationError({"joining_date": "Joining date cannot be in the future."})

    # ─────────────────────────────
    # SAVE
    # ─────────────────────────────
    def save(self, *args, **kwargs):
        self.full_clean()

        if not self.staff_code:
            with transaction.atomic():
                role_prefix = self.STAFF_CODE_PREFIX.get(self.role, "STF")
                branch_code = self.branch.code if self.branch_id else "GRP"
                full_prefix = f"{branch_code}-{role_prefix}"

                last = (
                    StaffProfile.objects
                    .select_for_update()
                    .filter(branch_id=self.branch_id, staff_code__startswith=full_prefix + "-")
                    .order_by("-id")
                    .first()
                )

                new_number = 1
                if last and last.staff_code:
                    try:
                        new_number = int(last.staff_code.split("-")[-1]) + 1
                    except Exception:
                        pass

                self.staff_code = f"{full_prefix}-{str(new_number).zfill(3)}"

        super().save(*args, **kwargs)

        if self.user:
            self.user.is_active = self.is_active
            self.user.is_staff = True
            self.user.is_superuser = bool(self.is_group_admin)
            self.user.save(update_fields=["is_active", "is_staff", "is_superuser"])

    def __str__(self):
        return f"{self.staff_code} — {self.user.get_full_name() or self.user.username} ({self.role})"

    def reference_rate(self, period_type):
        """
        Best-effort reference figure for the given period_type, purely for
        display on the Salary sheet — never used to auto-fill net_salary.
        Falls back to deriving from whichever rate IS set when the specific
        one requested is 0/unset, using mandatory_working_days as the
        monthly↔daily bridge (mirrors SupportStaff.reference_rate).
        """
        working_days = self.mandatory_working_days or 26
        daily = self.daily_rate
        if not daily:
            if self.salary:
                daily = (Decimal(self.salary) / working_days)
            elif self.weekly_rate:
                daily = (self.weekly_rate / 7)

        if period_type == "Daily":
            return daily
        if period_type == "Weekly":
            return self.weekly_rate if self.weekly_rate else (daily * 7 if daily else Decimal("0.00"))
        # Monthly
        return Decimal(self.salary) if self.salary else (daily * working_days if daily else Decimal("0.00"))

    class Meta:
        ordering = ["-id"]
        unique_together = [
            ("branch", "staff_code"),
            ("branch", "phone"),
        ]


# ─────────────────────────────────────────────
# MANAGER BRANCH ACCESS
# ─────────────────────────────────────────────
class ManagerBranchAccess(models.Model):
    """
    Grants a Manager account read/write access to an *additional* branch,
    beyond their own home branch (StaffProfile.branch) — one login, no
    separate credentials per branch. Only a group admin can create/remove
    these grants (see administration/views.py:ManagerBranchAccessListView /
    ManagerBranchAccessRevokeView).

    Purely additive: removing a grant never touches the manager's home
    branch, and a manager's home branch never needs a row here — it's
    already implied by StaffProfile.branch. See
    authentication/utils.py:get_manager_accessible_branches /
    resolve_manager_active_branch for how this feeds the branch-switching
    request flow, and BranchSwitcher.jsx (frontend) for the manager
    equivalent of the group-admin branch switcher (no "All Branches"
    option — a manager only ever sees the specific branches they've
    actually been granted).
    """

    id = models.AutoField(primary_key=True)

    manager = models.ForeignKey(
        "StaffProfile",
        on_delete=models.CASCADE,
        related_name="extra_branch_access",
        limit_choices_to={"role": "Manager"},
        help_text="Must be a StaffProfile with role='Manager'. Enforced in clean() too.",
    )

    branch = models.ForeignKey(
        "Branch",
        on_delete=models.CASCADE,
        related_name="manager_access_grants",
    )

    granted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manager_branch_grants_made",
        help_text="The group admin who granted this access, for audit purposes.",
    )

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    def clean(self):
        if self.manager_id and self.manager.role != "Manager":
            raise ValidationError({"manager": "Branch access can only be granted to a Manager account."})
        if self.manager_id and self.branch_id and self.manager.branch_id == self.branch_id:
            raise ValidationError({"branch": "This is already the manager's own home branch."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.manager.staff_code} → {self.branch.code}"

    class Meta:
        ordering = ["branch__name"]
        unique_together = [("manager", "branch")]
        verbose_name = "Manager branch access grant"
        verbose_name_plural = "Manager branch access grants"


# ─────────────────────────────────────────────
# RECEPTIONIST PROFILE
# ─────────────────────────────────────────────
class ReceptionistProfile(models.Model):
    """
    Extended profile for Receptionist-role staff.
    Auto-created by a signal whenever a Receptionist StaffProfile is saved.
    """

    profile_id = models.AutoField(primary_key=True)

    staff = models.OneToOneField(
        StaffProfile,
        on_delete=models.CASCADE,
        related_name="receptionist_profile",
    )

    def clean(self):
        if self.staff and self.staff.role != "Receptionist":
            raise ValidationError({"staff": "Linked staff member must have the Receptionist role."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.staff.staff_code} — Receptionist"

    class Meta:
        ordering = ["-profile_id"]


# ─────────────────────────────────────────────
# PHARMACIST PROFILE
# ─────────────────────────────────────────────
class PharmacistProfile(models.Model):
    """
    Extended profile for Pharmacist-role staff.
    Auto-created by a signal whenever a Pharmacist StaffProfile is saved.
    """

    profile_id = models.AutoField(primary_key=True)

    staff = models.OneToOneField(
        StaffProfile,
        on_delete=models.CASCADE,
        related_name="pharmacist_profile",
    )

    license_number = models.CharField(max_length=100, blank=True, null=True)

    def clean(self):
        if self.staff and self.staff.role != "Pharmacist":
            raise ValidationError({"staff": "Linked staff member must have the Pharmacist role."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.staff.staff_code} — Pharmacist"

    class Meta:
        ordering = ["-profile_id"]





# ─────────────────────────────────────────────
# PROCEDURE
# ─────────────────────────────────────────────
class Procedure(models.Model):

    procedure_id = models.AutoField(primary_key=True)

    branch = models.ForeignKey(
        "Branch",
        on_delete=models.PROTECT,
        related_name="procedures",
        help_text="Each branch manager prices/activates their own procedure catalog independently.",
    )

    name = models.CharField(max_length=200)

    description = models.TextField(blank=True, null=True)

    charge = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0)],
    )

    is_active = models.BooleanField(default=True)

    created_by_pharmacist = models.BooleanField(
        default=False,
        help_text=(
            "True only for one-off procedures auto-created by the pharmacist's "
            "'instant procedure' billing flow (amount typed per-patient at "
            "billing time). These are never shown in the admin-managed "
            "procedure catalog/picker, since their charge is not a real "
            "standard price - it was whatever that pharmacist typed for that "
            "one patient. Kept separate from is_active so admins can still "
            "freely activate/deactivate real catalog procedures without "
            "accidentally surfacing these one-offs."
        ),
    )

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not self.name or not self.name.strip():
            raise ValidationError({"name": "Procedure name cannot be blank."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} — ₹{self.charge}"

    class Meta:
        ordering = ["name"]
        unique_together = [("branch", "name")]


# ─────────────────────────────────────────────
# BILLING DEPARTMENT
# ─────────────────────────────────────────────
class BillingDepartment(models.Model):
    """
    Manager-curated master list of departments a consultation can be
    *billed against* (e.g. 'General Medicine', 'Paediatrics', 'Emergency').

    This is deliberately separate from DoctorProfile.department /
    GuestDoctorProfile.department (the doctor's own home department) and
    from manager.Specialty (the public-website specialty pages). A
    paediatrician, for example, is still a paediatrician on their profile,
    but reception may need to bill that same consultation under Emergency
    or General Medicine depending on which department actually saw the
    patient. billed_department on ConsultationBill records that choice.

    Hospital-wide (not per-branch) — the same set of departments applies
    across every branch, unlike Procedure's per-branch catalog.
    """

    department_id = models.AutoField(primary_key=True)

    name = models.CharField(max_length=200, unique=True)

    is_active = models.BooleanField(
        default=True,
        help_text="Inactive departments are hidden from the reception billing "
                   "dropdown but are kept for historical bills that reference them.",
    )

    display_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not self.name or not self.name.strip():
            raise ValidationError({"name": "Department name cannot be blank."})
        self.name = self.name.strip()

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["display_order", "name"]


# ─────────────────────────────────────────────
# AUDIT LOG
# ─────────────────────────────────────────────
class AuditLog(models.Model):

    ACTION_CHOICES = [
        ("CREATE",     "Create"),
        ("UPDATE",     "Update"),
        ("DELETE",     "Delete"),
        ("LOGIN",      "Login"),
        ("LOGOUT",     "Logout"),
        ("REACTIVATE", "Reactivate"),
        ("DEACTIVATE", "Deactivate"),
    ]

    log_id = models.AutoField(primary_key=True)

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text=(
            "Client IP the action was performed from. Resolved at log-creation "
            "time from the in-flight request (administration.audit.get_current_ip, "
            "backed by authentication.utils.get_client_ip) — never trusted from "
            "client-supplied data. Null only for actions with no request in "
            "flight at all, e.g. a management command or migration."
        ),
    )

    branch = models.ForeignKey(
        "Branch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
        help_text=(
            "Branch this action is scoped to. Resolved at log-creation time from "
            "the affected object's own branch, falling back to the acting user's "
            "StaffProfile.branch. Null for group-admin/system actions with no "
            "single branch (e.g. promoting a group admin)."
        ),
    )

    module = models.CharField(max_length=50, default="General")

    action = models.CharField(max_length=20, choices=ACTION_CHOICES)

    object_id = models.PositiveIntegerField(null=True, blank=True)

    description = models.TextField(blank=True, null=True)

    user_agent = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text=(
            "Raw User-Agent header of the request this action came from — "
            "identifies browser/OS (e.g. 'Chrome 128 on Windows'). Resolved "
            "the same way as ip_address, from the in-flight request."
        ),
    )

    device_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text=(
            "Client-generated device identifier sent via the X-Device-Id "
            "header (see authentication frontend). Lets two staff sharing "
            "one IP/network be told apart, and lets a single account's "
            "actions be traced to a specific browser/device rather than "
            "just a network. Empty when the client didn't send one (older "
            "frontend build, or a request with no request in flight)."
        ),
    )

    timestamp = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        username = self.user.username if self.user else "system"
        return f"[{self.timestamp:%Y-%m-%d %H:%M}] {username} — {self.action} — {self.module}"


class UserDevice(models.Model):
    """
    Registry of (user, device_id) pairs this user has actually logged in
    from before — the "known devices" list. Distinct from AuditLog: this
    table exists to answer "have we seen this device for this user
    before?" cheaply at login time (one indexed lookup), not to be a full
    history — AuditLog already is that.

    A row here is created/touched only from LoginView, not from every
    request, since "new device" is meaningfully a login-time event.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="known_devices")
    device_id = models.CharField(max_length=64)
    user_agent = models.CharField(max_length=512, blank=True, default="")
    first_ip = models.GenericIPAddressField(null=True, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("user", "device_id")
        ordering = ["-last_seen"]

    def __str__(self):
        return f"{self.user.username} — {self.device_id[:8]}…"
    


# ─────────────────────────────────────────────────────────────────────────────
# PATCH: Replace the GuestDoctorProfile class in administration/models.py
# with this version. Only GuestDoctorProfile changes — all other models stay.
# ─────────────────────────────────────────────────────────────────────────────

class GuestDoctorProfile(models.Model):
    """
    Visiting / consultant doctors.
    Now supports an optional Django User account for portal login.
    - If user is set, the guest doctor can log in with username + password.
    - If user is None, they're a reference-only record (no login).
    """

    guest_doctor_id = models.AutoField(primary_key=True)

    branch = models.ForeignKey(
        "Branch",
        on_delete=models.PROTECT,
        related_name="guest_doctor_profiles",
        null=True,
        blank=True,
        help_text="Leave blank for a guest doctor who consults across multiple branches (reference-only).",
    )

    guest_code = models.CharField(
        max_length=20, editable=False, db_index=True
    )

    # ── NEW: optional login account ──────────────────────────────────────
    user = models.OneToOneField(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="guest_doctor_profile",
    )
    # ─────────────────────────────────────────────────────────────────────

    full_name = models.CharField(max_length=200)

    specialization = models.CharField(max_length=200, blank=True, null=True)

    department = models.CharField(max_length=200, blank=True, null=True)

    registration_number = models.CharField(
        max_length=100, unique=True, blank=True, null=True
    )

    phone = models.CharField(
        max_length=15,
        blank=True,
        null=True,
        validators=[RegexValidator(r'^\+?\d{9,15}$', 'Enter a valid phone number.')],
    )

    consultation_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )

    is_active = models.BooleanField(default=True)

    notes = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not self.full_name or not self.full_name.strip():
            raise ValidationError({"full_name": "Full name cannot be blank."})

    def save(self, *args, **kwargs):
        self.full_clean()

        if not self.guest_code:
            with transaction.atomic():
                prefix = f"{self.branch.code}-GDR" if self.branch_id else "GDR"
                last = (
                    GuestDoctorProfile.objects
                    .select_for_update()
                    .filter(branch_id=self.branch_id, guest_code__startswith=prefix + "-")
                    .order_by("-guest_doctor_id")
                    .first()
                )
                new_number = 1
                if last and last.guest_code:
                    try:
                        new_number = int(last.guest_code.split("-")[-1]) + 1
                    except Exception:
                        pass
                self.guest_code = f"{prefix}-{str(new_number).zfill(3)}"

        super().save(*args, **kwargs)

        # Sync is_active to the linked User account (if any)
        if self.user_id:
            User.objects.filter(pk=self.user_id).update(is_active=self.is_active)

    def __str__(self):
        return f"{self.guest_code} — {self.full_name}"

    class Meta:
        ordering = ["-guest_doctor_id"]
        unique_together = [("branch", "guest_code")]

# ─────────────────────────────────────────────────────────────────────────────
# COMMON STAFF PROFILES
# Two separate admin-created account types — "Common Receptionist" and
# "Common Pharmacist" — each a single-role account that mirrors the
# GuestDoctorProfile pattern (independent of the StaffProfile hierarchy),
# with a mandatory login since these staff need to actually log in and work
# their module day to day, not just exist as a reference record.
#
# AbstractCommonStaffProfile holds everything the two share (code
# generation, login sync, validation) so the two concrete models — and
# later their serializers/views — stay thin, matching what a shared
# component on the frontend does for the same pair.
# ─────────────────────────────────────────────────────────────────────────────
class AbstractCommonStaffProfile(models.Model):
    """
    Shared shape for the "common" account types below. Not a role field on
    one model — each concrete subclass IS a fixed role, same as
    ReceptionistProfile / PharmacistProfile are fixed to their role, so
    permission checks stay simple string equality.
    """

    CODE_PREFIX = "CST"  # overridden per subclass

    branch = models.ForeignKey(
        "Branch",
        on_delete=models.PROTECT,
    )

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
    )

    full_name = models.CharField(max_length=200)

    phone = models.CharField(
        max_length=15,
        blank=True,
        null=True,
        validators=[RegexValidator(r'^\+?\d{9,15}$', 'Enter a valid phone number.')],
    )

    qualification = models.CharField(max_length=255, default="Not Specified")

    is_active = models.BooleanField(default=True)

    notes = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def clean(self):
        if not self.full_name or not self.full_name.strip():
            raise ValidationError({"full_name": "Full name cannot be blank."})

    def save(self, *args, **kwargs):
        self.full_clean()

        if not self.common_code:
            with transaction.atomic():
                model = type(self)
                prefix = f"{self.branch.code}-{self.CODE_PREFIX}"
                last = (
                    model.objects
                    .select_for_update()
                    .filter(branch_id=self.branch_id, common_code__startswith=prefix + "-")
                    .order_by(f"-{model._meta.pk.name}")
                    .first()
                )
                new_number = 1
                if last and last.common_code:
                    try:
                        new_number = int(last.common_code.split("-")[-1]) + 1
                    except Exception:
                        pass
                self.common_code = f"{prefix}-{str(new_number).zfill(3)}"

        super().save(*args, **kwargs)

        # Sync is_active + is_staff to the linked User account
        if self.user_id:
            User.objects.filter(pk=self.user_id).update(
                is_active=self.is_active, is_staff=True
            )

    def __str__(self):
        return f"{self.common_code} — {self.full_name}"


class CommonReceptionistProfile(AbstractCommonStaffProfile):
    """
    Admin-created Receptionist account — same workflow/permissions as a
    regular StaffProfile Receptionist, but managed like a Guest Doctor
    (its own admin page, independent of the staff-hiring flow).
    """

    CODE_PREFIX = "CREC"

    common_receptionist_id = models.AutoField(primary_key=True)

    common_code = models.CharField(
        max_length=20, editable=False, db_index=True
    )

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="common_receptionist_profile",
    )

    class Meta:
        ordering = ["-common_receptionist_id"]
        unique_together = [("branch", "common_code")]


class CommonPharmacistProfile(AbstractCommonStaffProfile):
    """
    Admin-created Pharmacist account — same workflow/permissions as a
    regular StaffProfile Pharmacist, but managed like a Guest Doctor
    (its own admin page, independent of the staff-hiring flow).
    """

    CODE_PREFIX = "CPHARM"

    common_pharmacist_id = models.AutoField(primary_key=True)

    common_code = models.CharField(
        max_length=20, editable=False, db_index=True
    )

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="common_pharmacist_profile",
    )

    class Meta:
        ordering = ["-common_pharmacist_id"]
        unique_together = [("branch", "common_code")]


# ======================================
# HOSPITAL SETTINGS  (singleton)
# ======================================
class HospitalSettings(models.Model):
    """
    Config record — one row per Branch (was a single pk=1 singleton).
    Admin updates it via the settings endpoint; code reads it via
    HospitalSettings.get(branch).
    """

    branch = models.OneToOneField(
        "Branch",
        on_delete=models.CASCADE,
        related_name="hospital_settings",
    )

    mrd_registration_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
        help_text='One-time fee charged when a new MRD/patient record is created.',
    )

    default_home_visit_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
        help_text='Manager-set default Home Visit Fee, pre-filled on new Home Visit bills.',
    )

    default_home_visit_travel_charge = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
        help_text='Manager-set default Travel Charge, pre-filled on new Home Visit bills.',
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = 'Hospital Settings'
        verbose_name_plural = 'Hospital Settings'

    @classmethod
    def get(cls, branch):
        """Return this branch's settings row, creating it with defaults if absent."""
        obj, _ = cls.objects.get_or_create(branch=branch)
        return obj

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        branch_code = self.branch.code if self.branch_id else "?"
        return f'Hospital Settings [{branch_code}] (MRD fee: ₹{self.mrd_registration_fee})'