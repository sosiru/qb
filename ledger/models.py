from django.db import models

from base.common import TimestampedModel
from base.utils import generate_uuid


class WalletAccount(TimestampedModel):
    class OwnerType(models.TextChoices):
        USER = "USER", "User"
        ORGANIZATION = "ORGANIZATION", "Organization"
        SYSTEM = "SYSTEM", "System"

    class WalletType(models.TextChoices):
        PRIMARY = "PRIMARY", "Primary"
        VAULT = "VAULT", "Vault"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        CLOSED = "CLOSED", "Closed"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    owner_type = models.CharField(max_length=24, choices=OwnerType.choices)
    wallet_type = models.CharField(max_length=20, choices=WalletType.choices, default=WalletType.PRIMARY)
    user = models.ForeignKey("eusers.User", on_delete=models.PROTECT, null=True, blank=True, related_name="ledger_accounts")
    organization = models.ForeignKey(
        "base.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_accounts",
    )
    name = models.CharField(max_length=255, blank=True)
    currency = models.CharField(max_length=3, default="KES")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    available_balance_minor = models.BigIntegerField(default=0)
    current_balance_minor = models.BigIntegerField(default=0)
    reserved_balance_minor = models.BigIntegerField(default=0)
    uncleared_balance_minor = models.BigIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "wallet_accounts"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "wallet_type", "currency"],
                condition=models.Q(user__isnull=False),
                name="uniq_wallet_account_user_type_currency",
            ),
            models.UniqueConstraint(
                fields=["organization", "wallet_type", "currency"],
                condition=models.Q(organization__isnull=False),
                name="uniq_wallet_account_org_type_currency",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(owner_type="USER", user__isnull=False, organization__isnull=True)
                    | models.Q(owner_type="ORGANIZATION", user__isnull=True, organization__isnull=False)
                    | models.Q(owner_type="SYSTEM", user__isnull=True, organization__isnull=True)
                ),
                name="wallet_account_single_owner",
            ),
            models.CheckConstraint(condition=models.Q(available_balance_minor__gte=0), name="wallet_account_available_not_negative"),
            models.CheckConstraint(condition=models.Q(reserved_balance_minor__gte=0), name="wallet_account_reserved_not_negative"),
            models.CheckConstraint(condition=models.Q(uncleared_balance_minor__gte=0), name="wallet_account_uncleared_not_negative"),
        ]
        indexes = [
            models.Index(fields=["owner_type", "wallet_type", "currency"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        owner = self.name or self.user_id or self.organization_id or "System"
        return f"{owner} {self.wallet_type} {self.currency}"


class LedgerEntry(TimestampedModel):
    class TransactionType(models.TextChoices):
        TOP_UP = "TOP_UP", "Top Up"
        WITHDRAWAL = "WITHDRAWAL", "Withdrawal"
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"

    class Status(models.TextChoices):
        POSTED = "POSTED", "Posted"
        REVERSED = "REVERSED", "Reversed"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    account = models.ForeignKey("ledger.WalletAccount", on_delete=models.PROTECT, related_name="entries")
    transaction_type = models.CharField(max_length=32, choices=TransactionType.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.POSTED)
    reference = models.CharField(max_length=120, unique=True)
    idempotency_key = models.CharField(max_length=128)
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="KES")
    balance_before_minor = models.BigIntegerField()
    balance_after_minor = models.BigIntegerField()
    description = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "ledger_entries"
        constraints = [
            models.UniqueConstraint(fields=["account", "idempotency_key"], name="uniq_wallet_account_idempotency_key"),
            models.CheckConstraint(condition=models.Q(amount_minor__gt=0), name="ledger_entry_amount_positive"),
        ]
        indexes = [
            models.Index(fields=["account", "created_at"]),
            models.Index(fields=["transaction_type", "created_at"]),
        ]

    def __str__(self):
        return f"{self.reference} {self.transaction_type}"

    @property
    def wallet(self):
        return self.account

    @property
    def wallet_id(self):
        return self.account_id

    @property
    def entry_type(self):
        mapping = {
            self.TransactionType.TOP_UP: "TOP_UP",
            self.TransactionType.WITHDRAWAL: "DISBURSEMENT",
            self.TransactionType.ADJUSTMENT: "ADJUSTMENT",
        }
        return mapping.get(self.transaction_type, self.transaction_type)


class LedgerEntryLog(TimestampedModel):
    class BalanceField(models.TextChoices):
        AVAILABLE = "available", "Available"
        CURRENT = "current", "Current"
        RESERVED = "reserved", "Reserved"
        UNCLEARED = "uncleared", "Uncleared"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    entry = models.ForeignKey("ledger.LedgerEntry", on_delete=models.CASCADE, related_name="logs")
    account = models.ForeignKey("ledger.WalletAccount", on_delete=models.PROTECT, related_name="entry_logs")
    sequence = models.PositiveSmallIntegerField()
    balance_field = models.CharField(max_length=16, choices=BalanceField.choices)
    delta_minor = models.BigIntegerField()
    balance_before_minor = models.BigIntegerField()
    balance_after_minor = models.BigIntegerField()
    reason = models.CharField(max_length=120)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "ledger_entry_logs"
        constraints = [
            models.UniqueConstraint(fields=["entry", "sequence", "balance_field"], name="uniq_ledger_log_step_field"),
        ]
        indexes = [
            models.Index(fields=["account", "created_at"]),
            models.Index(fields=["entry", "sequence"]),
        ]
        ordering = ["entry", "sequence", "created_at"]

    def __str__(self):
        return f"{self.entry.reference} #{self.sequence} {self.balance_field} {self.delta_minor}"
