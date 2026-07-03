import hashlib
import secrets
from datetime import timedelta

from django.db import models
from django.utils import timezone

from base.common import TimestampedModel
from base.utils import generate_uuid


class IntegrationApiKey(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    user = models.ForeignKey("eusers.User", on_delete=models.CASCADE, related_name="integration_api_keys")
    organization = models.ForeignKey(
        "base.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="integration_api_keys",
    )
    created_by = models.ForeignKey(
        "eusers.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_integration_api_keys",
    )
    name = models.CharField(max_length=120)
    key_prefix = models.CharField(max_length=12, db_index=True)
    key_hash = models.CharField(max_length=64, unique=True)
    scopes = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    @classmethod
    def issue(cls, user, name, organization=None, created_by=None, scopes=None, ttl_days=365):
        raw_key = f"rtk_{secrets.token_urlsafe(32)}"
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        key = cls.objects.create(
            user=user,
            organization=organization,
            created_by=created_by or user,
            name=name,
            key_prefix=raw_key[:12],
            key_hash=digest,
            scopes=scopes or ["read", "write"],
            expires_at=timezone.now() + timedelta(days=ttl_days),
        )
        return key, raw_key

    def is_currently_active(self):
        now = timezone.now()
        if not self.is_active or self.revoked_at:
            return False
        if self.expires_at and self.expires_at <= now:
            return False
        return True

    def __str__(self):
        return f"{self.name} ({self.key_prefix})"
