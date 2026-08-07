from django.contrib import admin
from .models import ReceptionistProfile, PharmacistProfile, Procedure, AuditLog, Branch, ManagerBranchAccess, UserDevice

# StaffProfile is registered in authentication/admin.py alongside the
# custom User admin so password handling is centralised. Registering it
# a second time here would raise AlreadyRegistered.


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    # NOTE: this was the only way to create/edit a Branch at all before the
    # /api/administration/branches/ endpoints were added — kept as a backup
    # path (and for is_superuser-only bulk fixes) even now that the API
    # exists.
    list_display  = ("branch_id", "code", "name", "phone", "is_active", "updated_at")
    search_fields = ("name", "code")
    list_filter   = ("is_active",)
    readonly_fields = ("branch_id", "created_at", "updated_at")
    ordering = ("name",)


@admin.register(ReceptionistProfile)
class ReceptionistProfileAdmin(admin.ModelAdmin):
    list_display  = ("profile_id", "get_staff_code", "get_full_name", "get_is_active")
    search_fields = ("staff__staff_code", "staff__user__first_name", "staff__user__last_name")
    list_filter   = ("staff__is_active",)
    readonly_fields = ("profile_id",)

    def get_staff_code(self, obj): return obj.staff.staff_code
    get_staff_code.short_description = "Staff Code"

    def get_full_name(self, obj):
        return obj.staff.user.get_full_name() or obj.staff.user.username
    get_full_name.short_description = "Full Name"

    def get_is_active(self, obj): return obj.staff.is_active
    get_is_active.short_description = "Active"
    get_is_active.boolean = True


@admin.register(PharmacistProfile)
class PharmacistProfileAdmin(admin.ModelAdmin):
    list_display  = ("profile_id", "get_staff_code", "get_full_name", "license_number", "get_is_active")
    search_fields = ("staff__staff_code", "staff__user__first_name", "license_number")
    list_filter   = ("staff__is_active",)
    readonly_fields = ("profile_id",)

    def get_staff_code(self, obj): return obj.staff.staff_code
    get_staff_code.short_description = "Staff Code"

    def get_full_name(self, obj):
        return obj.staff.user.get_full_name() or obj.staff.user.username
    get_full_name.short_description = "Full Name"

    def get_is_active(self, obj): return obj.staff.is_active
    get_is_active.short_description = "Active"
    get_is_active.boolean = True


@admin.register(Procedure)
class ProcedureAdmin(admin.ModelAdmin):
    list_display  = ("procedure_id", "name", "charge", "is_active", "updated_at")
    search_fields = ("name",)
    list_filter   = ("is_active",)
    readonly_fields = ("procedure_id", "created_at", "updated_at")
    ordering = ("name",)


@admin.register(ManagerBranchAccess)
class ManagerBranchAccessAdmin(admin.ModelAdmin):
    list_display  = ("id", "get_manager_code", "get_manager_name", "branch", "granted_by", "created_at")
    search_fields = ("manager__staff_code", "manager__user__first_name", "manager__user__last_name", "branch__name", "branch__code")
    list_filter   = ("branch",)
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)

    def get_manager_code(self, obj): return obj.manager.staff_code
    get_manager_code.short_description = "Manager Code"

    def get_manager_name(self, obj):
        return obj.manager.user.get_full_name() or obj.manager.user.username
    get_manager_name.short_description = "Manager Name"


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display  = ("log_id", "timestamp", "get_username", "action", "module", "branch", "ip_address", "device_id", "object_id")
    search_fields = ("user__username", "description", "ip_address", "device_id", "user_agent")
    list_filter   = ("action", "module", "branch")
    readonly_fields = ("log_id", "timestamp")
    ordering = ("-timestamp",)

    def get_username(self, obj): return obj.user.username if obj.user else "system"
    get_username.short_description = "User"

    def has_add_permission(self, request):
        return False   # audit logs should never be hand-created via admin

    def has_change_permission(self, request, obj=None):
        return False   # audit logs are immutable


@admin.register(UserDevice)
class UserDeviceAdmin(admin.ModelAdmin):
    list_display  = ("user", "device_id", "user_agent", "last_ip", "first_seen", "last_seen")
    search_fields = ("user__username", "device_id", "user_agent", "last_ip")
    list_filter   = ("user",)
    readonly_fields = ("first_seen",)
    ordering = ("-last_seen",)