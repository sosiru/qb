from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = "audit"

    def ready(self):
        from . import signals  # noqa: F401
