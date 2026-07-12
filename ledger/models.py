from django.db import models

from base.common import TimestampedModel
from base.utils import generate_uuid


class State(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    name = models.CharField(max_length=64, unique=True)
    description = models.TextField(max_length=255, blank=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class AccountFieldType(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    name = models.CharField(max_length=64, unique=True)
    description = models.TextField(max_length=255, blank=True)
    state = models.ForeignKey("ledger.State", on_delete=models.PROTECT, related_name="account_field_types")

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class PaymentMethod(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    name = models.CharField(max_length=64, unique=True)
    description = models.TextField(max_length=255, blank=True)
    state = models.ForeignKey("ledger.State", on_delete=models.PROTECT, related_name="payment_methods")

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class TransactionType(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    name = models.CharField(max_length=100, unique=True)
    simple_name = models.CharField(max_length=64)
    description = models.TextField(max_length=255, blank=True)
    is_viewable = models.BooleanField(default=True)
    state = models.ForeignKey("ledger.State", on_delete=models.PROTECT, related_name="transaction_types")

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.name} {self.simple_name}"


class BalanceEntryType(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(max_length=255, blank=True)
    state = models.ForeignKey("ledger.State", on_delete=models.PROTECT, related_name="balance_entry_types")

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class ExecutionProfile(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(max_length=255, blank=True)
    state = models.ForeignKey("ledger.State", on_delete=models.PROTECT, related_name="execution_profiles")

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class RuleProfile(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    execution_profile = models.ForeignKey("ledger.ExecutionProfile", on_delete=models.CASCADE, related_name="rules")
    name = models.CharField(max_length=100)
    description = models.TextField(max_length=255, blank=True)
    order = models.PositiveSmallIntegerField()
    sleep_seconds = models.PositiveSmallIntegerField(default=0)
    state = models.ForeignKey("ledger.State", on_delete=models.PROTECT, related_name="rule_profiles")

    class Meta:
        ordering = ("execution_profile__name", "order")
        constraints = [
            models.UniqueConstraint(fields=["execution_profile", "name"], name="uniq_rule_profile_execution_name"),
            models.UniqueConstraint(fields=["execution_profile", "order"], name="uniq_rule_profile_execution_order"),
        ]

    def __str__(self):
        return f"{self.execution_profile} {self.name}"


class RuleProfileCommand(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    rule_profile = models.ForeignKey("ledger.RuleProfile", on_delete=models.CASCADE, related_name="commands")
    name = models.CharField(max_length=100)
    description = models.TextField(max_length=255, blank=True)
    order = models.PositiveSmallIntegerField()
    state = models.ForeignKey("ledger.State", on_delete=models.PROTECT, related_name="rule_profile_commands")

    class Meta:
        ordering = ("rule_profile__execution_profile__name", "rule_profile__order", "order")
        constraints = [
            models.UniqueConstraint(fields=["rule_profile", "order"], name="uniq_rule_profile_command_order"),
        ]

    def __str__(self):
        return f"{self.rule_profile} {self.name}"


class EntryType(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    name = models.CharField(max_length=20, unique=True)
    description = models.TextField(max_length=255, blank=True)
    state = models.ForeignKey("ledger.State", on_delete=models.PROTECT, related_name="entry_types")

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class Account(TimestampedModel):
    class OwnerType(models.TextChoices):
        USER = "USER", "User"
        ORGANIZATION = "ORGANIZATION", "Organization"
        SYSTEM = "SYSTEM", "System"

    class AccountKind(models.TextChoices):
        PRIMARY = "PRIMARY", "Primary"
        VAULT = "VAULT", "Vault"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        CLOSED = "CLOSED", "Closed"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    owner_type = models.CharField(max_length=24, choices=OwnerType.choices)
    account_kind = models.CharField(max_length=20, choices=AccountKind.choices, default=AccountKind.PRIMARY)
    account_number = models.CharField(max_length=50, unique=True)
    user = models.ForeignKey("eusers.User", on_delete=models.PROTECT, null=True, blank=True, related_name="billing_accounts")
    organization = models.ForeignKey(
        "base.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="billing_accounts",
    )
    name = models.CharField(max_length=255, blank=True)
    currency = models.CharField(max_length=3, default="KES")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    current_balance_minor = models.BigIntegerField(default=0)
    reserved_balance_minor = models.BigIntegerField(default=0)
    available_balance_minor = models.BigIntegerField(default=0)
    uncleared_balance_minor = models.BigIntegerField(default=0)
    charge_balance_minor = models.BigIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    state = models.ForeignKey("ledger.State", on_delete=models.PROTECT, related_name="accounts")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "account_kind", "currency"],
                condition=models.Q(user__isnull=False),
                name="uniq_account_user_kind_currency",
            ),
            models.UniqueConstraint(
                fields=["organization", "account_kind", "currency"],
                condition=models.Q(organization__isnull=False),
                name="uniq_account_org_kind_currency",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(owner_type="USER", user__isnull=False, organization__isnull=True)
                    | models.Q(owner_type="ORGANIZATION", user__isnull=True, organization__isnull=False)
                    | models.Q(owner_type="SYSTEM", user__isnull=True, organization__isnull=True)
                ),
                name="account_single_owner",
            ),
            models.CheckConstraint(condition=models.Q(available_balance_minor__gte=0), name="account_available_not_negative"),
            models.CheckConstraint(condition=models.Q(reserved_balance_minor__gte=0), name="account_reserved_not_negative"),
            models.CheckConstraint(condition=models.Q(uncleared_balance_minor__gte=0), name="account_uncleared_not_negative"),
        ]
        indexes = [
            models.Index(fields=["owner_type", "account_kind", "currency"]),
            models.Index(fields=["status", "created_at"]),
        ]

    @property
    def wallet_type(self):
        return self.account_kind

    def __str__(self):
        return f"{self.account_number} {self.account_kind} {self.currency}"


class Transaction(TimestampedModel):
    class Status(models.TextChoices):
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    class Direction(models.TextChoices):
        PAY_IN = "PAY_IN", "Pay In"
        PAY_OUT = "PAY_OUT", "Pay Out"
        INTERNAL = "INTERNAL", "Internal"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="transactions")
    transaction_type = models.ForeignKey("ledger.TransactionType", on_delete=models.PROTECT, related_name="transactions")
    payment_method = models.ForeignKey("ledger.PaymentMethod", on_delete=models.PROTECT, null=True, blank=True, related_name="transactions")
    direction = models.CharField(max_length=16, choices=Direction.choices)
    internal_reference = models.CharField(max_length=100, unique=True)
    request_id = models.CharField(max_length=120, blank=True)
    transaction_receipt = models.CharField(max_length=120, blank=True)
    confirmation_key = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=128, blank=True)
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="KES")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROCESSING)
    source_ip = models.CharField(max_length=45, blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    description = models.TextField(max_length=300, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    state = models.ForeignKey("ledger.State", on_delete=models.PROTECT, related_name="transactions")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["account", "created_at"]),
            models.Index(fields=["transaction_type", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uniq_transaction_account_idempotency_key",
            ),
            models.CheckConstraint(condition=models.Q(amount_minor__gt=0), name="transaction_amount_positive"),
        ]

    @property
    def reference(self):
        return self.internal_reference

    @property
    def balance_after_minor(self):
        latest = self.balance_logs.order_by("-created_at").first()
        return latest.total_balance_minor if latest else self.account.available_balance_minor

    def __str__(self):
        return f"{self.internal_reference} {self.transaction_type}"


class BalanceLog(TimestampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    transaction = models.ForeignKey("ledger.Transaction", on_delete=models.CASCADE, related_name="balance_logs")
    balance_entry_type = models.ForeignKey("ledger.BalanceEntryType", on_delete=models.PROTECT, related_name="balance_logs")
    reference = models.CharField(max_length=100, blank=True)
    receipt = models.CharField(max_length=120, blank=True)
    amount_transacted_minor = models.BigIntegerField()
    total_balance_minor = models.BigIntegerField(default=0)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    state = models.ForeignKey("ledger.State", on_delete=models.PROTECT, related_name="balance_logs")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.transaction} {self.balance_entry_type} {self.amount_transacted_minor}"


class BalanceLogEntry(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    balance_log = models.ForeignKey("ledger.BalanceLog", on_delete=models.CASCADE, related_name="entries")
    entry_type = models.ForeignKey("ledger.EntryType", on_delete=models.PROTECT, related_name="balance_log_entries")
    account_field_type = models.ForeignKey("ledger.AccountFieldType", on_delete=models.PROTECT, related_name="balance_log_entries")
    amount_transacted_minor = models.BigIntegerField()
    balance_before_minor = models.BigIntegerField()
    balance_after_minor = models.BigIntegerField()
    state = models.ForeignKey("ledger.State", on_delete=models.PROTECT, related_name="balance_log_entries")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("balance_log", "created_at")

    def __str__(self):
        return f"{self.balance_log} {self.account_field_type} {self.amount_transacted_minor}"


class PaymentRequest(TimestampedModel):
    class Operation(models.TextChoices):
        STK_PUSH = "STK_PUSH", "STK Push"
        PAY_IN = "PAY_IN", "Pay In"
        PAYOUT = "PAYOUT", "Payout"

    class Status(models.TextChoices):
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    transaction = models.ForeignKey("ledger.Transaction", on_delete=models.CASCADE, related_name="payment_requests")
    operation = models.CharField(max_length=20, choices=Operation.choices)
    originator_ref = models.CharField(max_length=100, unique=True)
    request_id = models.CharField(max_length=120, blank=True, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROCESSING)
    sandbox = models.BooleanField(default=True)
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    last_query_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["operation", "created_at"]),
        ]

    def __str__(self):
        return f"{self.operation} {self.originator_ref}"
