# administration/audit.py
"""
Central audit-logging utilities, shared across every app.

Two logging paths feed the same AuditLog table:

1. EXPLICIT — log_audit() / log_admin_action() called directly from a view,
   where the affected object and a human-friendly label are known
   precisely. This is how administration/views.py has always logged its
   own actions, and how authentication/views.py logs LOGIN/LOGOUT (neither
   is a plain model save, so no signal could ever catch them).

2. GENERIC — register_audit_signals(), called from an app's
   AppConfig.ready(), auto-attaches a post_save/post_delete listener to
   every model in that app that isn't already covered by (1) or explicitly
   excluded. This is the safety net that makes sure nothing falls through
   the cracks just because nobody remembered to add an explicit call at
   some view — every remaining model change in every app becomes an audit
   entry automatically, with no per-model code required.

Both paths resolve "who did this" the same way: from the CURRENT REQUEST,
never from the object being changed. A signal receiver has no direct
access to the request, so CurrentRequestMiddleware (see
rhimsbackend/middleware.py) stashes the request in thread-local storage
for the duration of the request/response cycle; get_current_user() reads
it back. This is exactly what a naive per-model post_save signal used to
get wrong here (see the old administration/signals.py, which attributed
e.g. a brand-new staff member's own account as the actor of "this account
was created", instead of the admin who actually created it) — resolving
the actor from the instance itself is only ever a guess, resolving it from
the live request is always correct.
"""
import threading

from django.db.models.signals import post_save, post_delete

_thread_locals = threading.local()


def set_current_request(request):
    _thread_locals.request = request


def clear_current_request():
    _thread_locals.request = None


def get_current_user():
    """
    The authenticated user for the request currently being processed on
    this thread, or None — for an anonymous request, no request in flight
    (management command, shell, migration, background job), or a request
    CurrentRequestMiddleware never wrapped.
    """
    request = getattr(_thread_locals, "request", None)
    if request is None:
        return None
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return user


def get_current_ip():
    """
    Best-effort client IP for the request currently being processed on this
    thread — the same thread-local request get_current_user() reads back.
    Returns None outside a request (management command, shell, migration,
    background job) or a request CurrentRequestMiddleware never wrapped.

    Delegates the actual header/REMOTE_ADDR resolution to
    authentication.utils.get_client_ip so there is exactly one place that
    decides whether X-Forwarded-For is trusted (see TRUST_PROXY_HEADERS
    there) — this just supplies the in-flight request to it.
    """
    request = getattr(_thread_locals, "request", None)
    if request is None:
        return None
    from authentication.utils import get_client_ip
    return get_client_ip(request)


def get_current_user_agent():
    """
    Raw User-Agent header for the in-flight request on this thread, or ""
    outside a request. Not security-sensitive the way IP is — it's only
    ever used for display/identification, never for access decisions — so
    unlike get_current_ip() there's no trust question here: it's read
    directly off the request.
    """
    request = getattr(_thread_locals, "request", None)
    if request is None:
        return ""
    return request.META.get("HTTP_USER_AGENT", "")


def get_current_device_id():
    """
    Client-supplied device identifier for the in-flight request, from the
    X-Device-Id header the frontend attaches to every API call (see
    frontend's device-id util — a random UUID persisted in localStorage
    per browser). Returns "" outside a request or when the header is
    absent (older frontend build, or a raw/non-browser client).

    Like User-Agent, this is for identification/attribution only, never
    for access control — a client can send any value it wants here, so
    never gate a permission check on it. It's what lets two staff on the
    same clinic network/IP show up as distinct devices in the audit log,
    and what UserDevice (administration/models.py) uses to recognize a
    "new device" at login.
    """
    request = getattr(_thread_locals, "request", None)
    if request is None:
        return ""
    return request.META.get("HTTP_X_DEVICE_ID", "")[:64]


def resolve_log_branch(obj, actor):
    """
    Best-effort resolution of which Branch an audit-log entry belongs to.

    Tried in order:
      1. A direct `branch` FK on the affected object itself.
      2. One hop via a `staff` relation (profiles that only carry their
         branch indirectly through a StaffProfile).
      3. The acting user's own StaffProfile.branch, as a fallback for
         actions on branch-less objects (or no object at all — LOGIN).

    Returns None (never raises) when nothing above yields a branch — e.g.
    a group-admin-only action with no single owning branch. AuditLogListView
    treats a null branch as visible only to group admins.
    """
    from authentication.utils import get_user_branch

    branch = getattr(obj, "branch", None)
    if branch is not None:
        return branch

    staff = getattr(obj, "staff", None)
    if staff is not None:
        branch = getattr(staff, "branch", None)
        if branch is not None:
            return branch

    if actor is not None:
        return get_user_branch(actor)

    return None


_ACTION_VERBS = {
    "CREATE":     "created",
    "UPDATE":     "updated",
    "DELETE":     "deleted",
    "DEACTIVATE": "deactivated",
    "REACTIVATE": "reactivated",
    "LOGIN":      "logged in",
    "LOGOUT":     "logged out",
}


def log_audit(user, module, action, obj=None, label=None, extra="", branch=None,
              ip_address=None, user_agent=None, device_id=None):
    """
    Core AuditLog writer. `user` must already be the resolved actor (a
    view's request.user, or get_current_user() on the generic signal
    path) — never inferred from `obj`. Safe to call with user=None
    (system/anonymous actions are still recorded, just unattributed).

    `ip_address`, `user_agent`, and `device_id` all default to the
    thread-local in-flight request (get_current_ip/_user_agent/_device_id)
    so every call site (explicit or generic-signal) gets full request
    attribution for free without having to thread a request through. Pass
    any of them explicitly only when a caller already has the request in
    hand and wants to bypass the thread-local lookup (see
    log_admin_action below).
    """
    from .models import AuditLog

    if ip_address is None:
        ip_address = get_current_ip()
    if user_agent is None:
        user_agent = get_current_user_agent()
    if device_id is None:
        device_id = get_current_device_id()

    actor_name = user.username if user else "system"
    verb = _ACTION_VERBS.get(action, action.lower())
    pk = getattr(obj, "pk", None)

    if obj is None and label is None:
        # No affected object (LOGIN/LOGOUT, or any other action that isn't
        # "something happened to a record") — just state what the actor did.
        description = f"{actor_name} {verb}."
    else:
        display = label or type(obj).__name__
        detail = ""
        if obj is not None and not label:
            # No explicit label given — fall back to the model's own
            # __str__() for something more useful than a bare class name.
            try:
                text = str(obj)
            except Exception:
                text = ""
            if text and text != display:
                detail = f' "{text}"'
        description = f"{display}{detail}" + (f" (id={pk})" if obj is not None else "") + f" {verb} by {actor_name}."

    if extra:
        description += f" {extra}"

    AuditLog.objects.create(
        user=user,
        branch=branch if branch is not None else resolve_log_branch(obj, user),
        module=module,
        action=action,
        object_id=pk,
        description=description,
        ip_address=ip_address,
        user_agent=user_agent,
        device_id=device_id,
    )


def log_admin_action(request, action, module, obj, label=None, extra=""):
    """
    Back-compat wrapper — same signature administration/views.py has
    always called. Resolves the actor from `request.user`, and the IP/
    user-agent/device-id directly from that same `request` (rather than
    falling through to the thread-local lookups) since it's already in
    hand.
    """
    from authentication.utils import get_client_ip

    actor = request.user if request.user and request.user.is_authenticated else None
    log_audit(
        actor, module, action, obj, label=label, extra=extra,
        ip_address=get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        device_id=request.META.get("HTTP_X_DEVICE_ID", "")[:64],
    )


# ── Generic per-model auto-logging ─────────────────────────────────
_MODULE_LABELS = {
    "doctor":         "Doctor",
    "lab":            "Lab",
    "manager":        "Manager",
    "pharmacist":     "Pharmacy",
    "reception":      "Reception",
    "administration": "Administration",
    "authentication": "Authentication",
}


def _module_name_for(model_cls):
    return _MODULE_LABELS.get(model_cls._meta.app_label, model_cls._meta.app_label.title())


def _generic_post_save(sender, instance, created, **kwargs):
    from .models import AuditLog
    if sender is AuditLog:
        return  # hard safety net — never let AuditLog log itself
    if kwargs.get("raw"):
        return  # loaddata fixture loading — not a real user action
    log_audit(get_current_user(), _module_name_for(sender), "CREATE" if created else "UPDATE", instance)


def _generic_post_delete(sender, instance, **kwargs):
    from .models import AuditLog
    if sender is AuditLog:
        return
    log_audit(get_current_user(), _module_name_for(sender), "DELETE", instance)


def register_audit_signals(app_config, exclude=()):
    """
    Call from an AppConfig.ready(). Wires up automatic CREATE/UPDATE/DELETE
    audit logging for every concrete model owned by this app, except the
    ones in `exclude` — pass model classes for anything that:
      - already gets a richer, explicitly-labelled log_audit()/
        log_admin_action() call from its own view (avoids double-logging
        the same action once generically and once explicitly), or
      - is itself a log/alert/history table (e.g. a *StockLog,
        *StockAlert, ConsultationTimeline) where an automatic audit entry
        would just be noise logging a log, or
      - is a pure line-item / linking table with no meaning of its own
        outside its parent (e.g. a bill's line items) — the parent's own
        CREATE/UPDATE entry already covers the user-visible action.
    """
    exclude_set = set(exclude)
    for model in app_config.get_models():
        if model in exclude_set:
            continue
        label = model._meta.label
        post_save.connect(_generic_post_save, sender=model, weak=False, dispatch_uid=f"audit_save_{label}")
        post_delete.connect(_generic_post_delete, sender=model, weak=False, dispatch_uid=f"audit_delete_{label}")