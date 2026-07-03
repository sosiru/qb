import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone

from base.common import TimestampedModel
from base.utils import generate_uuid

from .managers import UserManager


class User(TimestampedModel, AbstractBaseUser, PermissionsMixin):
    class AccountType(models.TextChoices):
        INDIVIDUAL = "INDIVIDUAL", "Individual"
        CORPORATE = "CORPORATE", "Corporate"
        SUPERADMIN = "SUPERADMIN", "Superadmin"

    class PaymentMode(models.TextChoices):
        WALLET = "WALLET", "Wallet"
        STK = "STK", "STK Push"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    phone_number = models.CharField(max_length=20, unique=True)
    email = models.EmailField(blank=True)
    full_name = models.CharField(max_length=255)
    account_type = models.CharField(max_length=20, choices=AccountType.choices)
    default_payment_mode = models.CharField(
        max_length=10,
        choices=PaymentMode.choices,
        default=PaymentMode.WALLET,
    )
    sms_notifications_enabled = models.BooleanField(default=True)
    email_notifications_enabled = models.BooleanField(default=True)
    push_notifications_enabled = models.BooleanField(default=True)
    mfa_enabled = models.BooleanField(default=False)
    is_phone_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    username = None

    USERNAME_FIELD = "phone_number"
    REQUIRED_FIELDS = ["full_name", "account_type"]

    objects = UserManager()

    def __str__(self):
        return f"{self.full_name} ({self.phone_number})"


class AccessToken(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    user = models.ForeignKey("eusers.User", on_delete=models.CASCADE, related_name="access_tokens")
    prefix = models.CharField(max_length=12, db_index=True)
    token_hash = models.CharField(max_length=64, unique=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def issue(cls, user, ttl_days=30):
        raw_token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        token = cls.objects.create(
            user=user,
            prefix=raw_token[:12],
            token_hash=digest,
            expires_at=timezone.now() + timedelta(days=ttl_days),
        )
        return token, raw_token

    def is_active(self):
        now = timezone.now()
        if self.revoked_at:
            return False
        if self.expires_at and self.expires_at <= now:
            return False
        return True
