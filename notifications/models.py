from django.db import models

from base.common import NOTIFICATION_CHANNEL_CHOICES, NOTIFICATION_EVENT_TYPE_CHOICES, TimestampedModel
from base.utils import generate_uuid


class NotificationTemplate(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    code = models.CharField(max_length=64, unique=True)
    event_type = models.CharField(max_length=32, choices=NOTIFICATION_EVENT_TYPE_CHOICES)
    channel = models.CharField(max_length=8, choices=NOTIFICATION_CHANNEL_CHOICES)
    system = models.CharField(max_length=64, default="route")
    provider_template = models.CharField(max_length=64)
    subject_template = models.CharField(max_length=255, blank=True)
    description = models.CharField(max_length=255, blank=True)
    default_context = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event_type", "channel"], name="uniq_notification_template_event_channel"),
        ]


class NotificationEvent(TimestampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    user = models.ForeignKey("eusers.User", on_delete=models.CASCADE, related_name="notifications", null=True, blank=True)
    template = models.ForeignKey("notifications.NotificationTemplate", on_delete=models.PROTECT, related_name="events", null=True, blank=True)
    channel = models.CharField(max_length=8, choices=NOTIFICATION_CHANNEL_CHOICES)
    event_type = models.CharField(max_length=32, choices=NOTIFICATION_EVENT_TYPE_CHOICES)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    scheduled_for = models.DateTimeField()
    sent_at = models.DateTimeField(null=True, blank=True)
    unique_identifier = models.CharField(max_length=120, db_index=True, blank=True, default="")
    recipients = models.JSONField(default=list, blank=True)
    context = models.JSONField(default=dict, blank=True)
    provider_response = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=255, blank=True)
