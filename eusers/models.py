import hashlib
import base64
import hmac
import json
import secrets
from datetime import timedelta

from django.conf import settings
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
        SERVICE_PROVIDER = "SERVICE_PROVIDER", "Service Provider"
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
    payouts_require_owner_approval = models.BooleanField(default=False)
    mpesa_withdrawal_phone = models.CharField(max_length=20, blank=True)
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
    IDLE_TIMEOUT_SECONDS = 5 * 60
    ABSOLUTE_TIMEOUT_SECONDS = 30 * 24 * 60 * 60

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    user = models.ForeignKey("eusers.User", on_delete=models.CASCADE, related_name="access_tokens")
    prefix = models.CharField(max_length=12, db_index=True)
    token_hash = models.CharField(max_length=64, unique=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def issue(cls, user, ttl_days=30):
        now = timezone.now()
        placeholder = secrets.token_urlsafe(32)
        token = cls.objects.create(
            user=user,
            prefix=placeholder[:12],
            token_hash=hashlib.sha256(placeholder.encode("utf-8")).hexdigest(),
            last_used_at=now,
            expires_at=now + cls.idle_timeout(),
        )
        raw_token = token.encode_jwt(ttl_days=ttl_days)
        token.prefix = raw_token[:12]
        token.token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        token.save(update_fields=["prefix", "token_hash", "updated_at"])
        return token, raw_token

    @classmethod
    def idle_timeout(cls):
        return timedelta(seconds=int(getattr(settings, "ACCESS_TOKEN_IDLE_TIMEOUT_SECONDS", cls.IDLE_TIMEOUT_SECONDS)))

    @classmethod
    def absolute_timeout(cls):
        default_seconds = cls.ABSOLUTE_TIMEOUT_SECONDS
        return timedelta(seconds=int(getattr(settings, "ACCESS_TOKEN_ABSOLUTE_TIMEOUT_SECONDS", default_seconds)))

    @classmethod
    def _jwt_secret(cls):
        configured = getattr(settings, "JWT_SESSION_SECRET", "")
        return (configured or settings.SECRET_KEY).encode("utf-8")

    @staticmethod
    def _base64url_encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _base64url_decode(value):
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))

    @staticmethod
    def hash_token(raw_token):
        return hashlib.sha256(str(raw_token or "").encode("utf-8")).hexdigest()

    def encode_jwt(self, ttl_days=30):
        now = timezone.now()
        absolute_timeout = min(timedelta(days=ttl_days), self.absolute_timeout())
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "typ": "access",
            "sub": str(self.user_id),
            "sid": str(self.id),
            "iat": int(now.timestamp()),
            "exp": int((now + absolute_timeout).timestamp()),
            "idle_timeout_seconds": int(self.idle_timeout().total_seconds()),
        }
        signing_input = ".".join(
            [
                self._base64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
                self._base64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            ]
        )
        signature = hmac.new(self._jwt_secret(), signing_input.encode("ascii"), hashlib.sha256).digest()
        return f"{signing_input}.{self._base64url_encode(signature)}"

    @classmethod
    def decode_jwt(cls, raw_token):
        parts = str(raw_token or "").split(".")
        if len(parts) != 3:
            return None
        signing_input = ".".join(parts[:2])
        expected = hmac.new(cls._jwt_secret(), signing_input.encode("ascii"), hashlib.sha256).digest()
        supplied = cls._base64url_decode(parts[2])
        if not hmac.compare_digest(supplied, expected):
            return None
        payload = json.loads(cls._base64url_decode(parts[1]).decode("utf-8"))
        if payload.get("typ") != "access":
            return None
        if int(payload.get("exp") or 0) <= int(timezone.now().timestamp()):
            return None
        return payload

    def is_active(self):
        now = timezone.now()
        if self.revoked_at:
            return False
        if self.expires_at and self.expires_at <= now:
            return False
        return True

    def extend_session(self):
        now = timezone.now()
        self.last_used_at = now
        self.expires_at = now + self.idle_timeout()
        self.save(update_fields=["last_used_at", "expires_at", "updated_at"])


class LoginOtp(TimestampedModel):
    class Purpose(models.TextChoices):
        LOGIN = "LOGIN", "Login"
        PASSWORD_RESET = "PASSWORD_RESET", "Password Reset"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    user = models.ForeignKey("eusers.User", on_delete=models.CASCADE, related_name="login_otps")
    purpose = models.CharField(max_length=20, choices=Purpose.choices, default=Purpose.LOGIN)
    code_hash = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "purpose", "consumed_at", "expires_at"]),
        ]

    @classmethod
    def hash_code(cls, code):
        return hashlib.sha256(str(code).encode("utf-8")).hexdigest()

    def is_active(self):
        if self.consumed_at:
            return False
        if self.expires_at <= timezone.now():
            return False
        if self.attempts >= self.max_attempts:
            return False
        return True
