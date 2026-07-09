from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import AuditLog
from .realtime import publish_audit_log


@receiver(post_save, sender=AuditLog)
def broadcast_audit_log(sender, instance, created, **kwargs):
    if not created:
        return
    transaction.on_commit(lambda: publish_audit_log(instance))
