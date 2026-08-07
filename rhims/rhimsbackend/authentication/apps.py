from django.apps import AppConfig


class AuthenticationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'authentication'

    def ready(self):
        # SECURITY: registers the django-axes lockout -> DRF PermissionDenied
        # bridge. Must be imported here (not at module load time) so the
        # signal is connected exactly once, after the app registry is ready.
        from . import signals  # noqa: F401