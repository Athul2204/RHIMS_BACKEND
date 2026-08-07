"""
Small, dependency-free security-header middleware.

CSP mitigates XSS by telling the browser which sources of scripts/styles/
etc. it's allowed to execute or load, so injected <script> tags or
attacker-hosted resources are blocked even if they slip past input
sanitisation somewhere else in the app.

Kept deliberately simple (one header, no nonces/reporting) rather than
pulling in django-csp, since this project only needs a static policy:
- the React SPA is served from a separate origin and talks to this API
  purely over JSON (no inline scripts to allow for it),
- the only server-rendered HTML this backend produces is the Django
  admin, which needs 'unsafe-inline' for style only (it uses inline
  style="" attributes) but no inline/eval scripts.
"""

CSP_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
    "form-action 'self'",
])


class ContentSecurityPolicyMiddleware:
    """Attaches a Content-Security-Policy header to every response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # Don't overwrite a header a view has deliberately set.
        response.setdefault("Content-Security-Policy", CSP_POLICY)
        return response


class CurrentRequestMiddleware:
    """
    Stashes the in-flight request in thread-local storage so code with no
    direct access to it — model post_save/post_delete signal receivers, in
    particular administration.audit's generic audit-log auto-logger — can
    still find out who is making the current request.

    The request object is stored, not request.user directly, because at
    the point this middleware runs (before self.get_response(request)),
    DRF hasn't authenticated the request yet — that happens later, inside
    APIView.dispatch(), which happens *during* the get_response() call
    below. Reading request.user lazily off the stored request — whenever
    a signal handler actually asks for it, deep inside that same call —
    is what makes it come back correctly authenticated. Everything runs on
    one thread per request, so plain threading.local (rather than
    anything request-ID-keyed) is sufficient here.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from administration.audit import set_current_request, clear_current_request

        set_current_request(request)
        try:
            response = self.get_response(request)
        finally:
            clear_current_request()
        return response