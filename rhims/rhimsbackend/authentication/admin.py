from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import ReadOnlyPasswordHashField
from django import forms
from administration.models import StaffProfile


# ── Form used when CREATING a new staff member via admin ──────────
class StaffUserCreationForm(forms.ModelForm):
    """Creates a User with a properly hashed password."""
    password1 = forms.CharField(label='Password', widget=forms.PasswordInput)
    password2 = forms.CharField(label='Confirm Password', widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = ('username', 'first_name', 'last_name', 'email')

    def clean_password2(self):
        p1 = self.cleaned_data.get('password1')
        p2 = self.cleaned_data.get('password2')
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("Passwords don't match.")
        return p2

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data['password1'])
        if commit:
            user.save()
        return user


# ── Form used when CHANGING an existing staff member via admin ────
class StaffUserChangeForm(forms.ModelForm):
    """Shows hashed password display; allows setting a new password."""
    password = ReadOnlyPasswordHashField(
        label='Current password',
        help_text=(
            'Raw passwords are not stored. '
            'Use the fields below to set a new password.'
        )
    )
    new_password1 = forms.CharField(
        label='New password', widget=forms.PasswordInput, required=False,
        help_text='Leave blank to keep the current password.'
    )
    new_password2 = forms.CharField(
        label='Confirm new password', widget=forms.PasswordInput, required=False
    )

    class Meta:
        model = User
        fields = ('username', 'first_name', 'last_name', 'email', 'is_active', 'password')

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get('new_password1')
        p2 = cleaned.get('new_password2')
        if p1 or p2:
            if p1 != p2:
                raise forms.ValidationError("New passwords don't match.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        new_pw = self.cleaned_data.get('new_password1')
        if new_pw:
            user.set_password(new_pw)   # ✅ properly hashes the password
        if commit:
            user.save()
        return user


# ── StaffProfile admin ────────────────────────────────────────────
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ('staff_code', 'get_full_name', 'role', 'phone', 'is_active')
    search_fields = ('staff_code', 'user__username', 'user__first_name', 'user__last_name')
    list_filter = ('role', 'is_active')
    readonly_fields = ('staff_code', 'created_at', 'updated_at')

    def get_full_name(self, obj):
        name = f"{obj.user.first_name} {obj.user.last_name}".strip()
        return name or obj.user.username
    get_full_name.short_description = 'Full Name'


admin.site.register(StaffProfile, StaffProfileAdmin)


# ── Override Django's default User admin ──────────────────────────
class CustomUserAdmin(BaseUserAdmin):
    """
    Replaces the default User admin so that passwords entered via the
    Django admin panel are always properly hashed with set_password().
    """
    add_form = StaffUserCreationForm
    form = StaffUserChangeForm

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'first_name', 'last_name', 'email', 'password1', 'password2'),
        }),
    )

    fieldsets = (
        (None,          {'fields': ('username', 'password', 'new_password1', 'new_password2')}),
        ('Personal',    {'fields': ('first_name', 'last_name', 'email')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Dates',       {'fields': ('last_login', 'date_joined')}),
    )

    list_display = ('username', 'first_name', 'last_name', 'email', 'is_staff', 'is_active')
    search_fields = ('username', 'first_name', 'last_name', 'email')
    list_filter = ('is_staff', 'is_superuser', 'is_active')

    def save_model(self, request, obj, form, change):
        """
        ✅ FIX: BaseUserAdmin.save_model() does NOT call form.save() — it calls
        obj.save() directly. This means the new_password1/new_password2 fields
        in our custom form are never processed unless we handle them here.
        """
        new_pw = form.cleaned_data.get('new_password1')
        if new_pw:
            obj.set_password(new_pw)
        super().save_model(request, obj, form, change)


# Unregister the default and register our fixed version
admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)