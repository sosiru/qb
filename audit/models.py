from django.db import models

from base.common import TimestampedModel
from base.utils import generate_uuid


class AuditLog(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    actor = models.ForeignKey("eusers.User", on_delete=models.SET_NULL, null=True, related_name="audit_logs")
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=64)
    target_id = models.UUIDField()
    metadata = models.JSONField(default=dict, blank=True)
