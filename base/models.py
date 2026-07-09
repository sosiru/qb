from django.db import models
from django.utils import timezone

from base.common import TimestampedModel
from base.utils import generate_uuid


class PayeeType(models.TextChoices):
    PAYBILL = "PAYBILL", "M-Pesa Paybill"
    TILL = "TILL", "M-Pesa Till"
    MOBILE = "MOBILE", "Mobile Send Money"
    BANK = "BANK", "Bank Account"


class Organization(TimestampedModel):
    class KycStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        VERIFIED = "VERIFIED", "Verified"
        REJECTED = "REJECTED", "Rejected"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    registration_number = models.CharField(max_length=64, blank=True)
    kyc_status = models.CharField(max_length=16, choices=KycStatus.choices, default=KycStatus.PENDING)
    default_currency = models.CharField(max_length=3, default="KES")
    push_notifications_enabled = models.BooleanField(default=True)
    sms_notifications_enabled = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class OrganizationMembership(TimestampedModel):
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Admin"
        MAKER = "MAKER", "Maker"
        CHECKER = "CHECKER", "Checker"
        VIEWER = "VIEWER", "Viewer"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    user = models.ForeignKey("eusers.User", on_delete=models.CASCADE, related_name="organization_memberships")
    organization = models.ForeignKey("base.Organization", on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=16, choices=Role.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "organization"], name="uniq_org_membership"),
        ]


class OrganizationInvite(TimestampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACCEPTED = "ACCEPTED", "Accepted"
        REVOKED = "REVOKED", "Revoked"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    organization = models.ForeignKey("base.Organization", on_delete=models.CASCADE, related_name="invites")
    invited_by = models.ForeignKey("eusers.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="sent_organization_invites")
    email = models.EmailField()
    role = models.CharField(max_length=16, choices=OrganizationMembership.Role.choices)
    token = models.CharField(max_length=96, unique=True, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "email"],
                condition=models.Q(status="PENDING"),
                name="uniq_pending_org_invite_email",
            ),
        ]


class Wallet(TimestampedModel):
    class OwnerType(models.TextChoices):
        USER = "USER", "User"
        ORGANIZATION = "ORGANIZATION", "Organization"

    class WalletType(models.TextChoices):
        PRIMARY = "PRIMARY", "Primary"
        VAULT = "VAULT", "Vault"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    owner_type = models.CharField(max_length=20, choices=OwnerType.choices)
    wallet_type = models.CharField(max_length=20, choices=WalletType.choices, default=WalletType.PRIMARY)
    user = models.ForeignKey("eusers.User", on_delete=models.CASCADE, null=True, blank=True, related_name="wallets")
    organization = models.ForeignKey(
        "base.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="wallets",
    )
    currency = models.CharField(max_length=3, default="KES")
    available_balance_minor = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "wallet_type"],
                condition=models.Q(user__isnull=False),
                name="uniq_user_wallet_type",
            ),
            models.UniqueConstraint(
                fields=["organization", "wallet_type"],
                condition=models.Q(organization__isnull=False),
                name="uniq_org_wallet_type",
            ),
        ]

    def __str__(self):
        owner = self.user.full_name if self.user_id else self.organization.name
        return f"{owner} {self.wallet_type}"


class WalletBalance(TimestampedModel):
    wallet = models.OneToOneField("base.Wallet", on_delete=models.PROTECT, related_name="balance")
    current_balance_minor = models.BigIntegerField(default=0)
    uncleared_balance_minor = models.BigIntegerField(default=0)
    reserved_balance_minor = models.BigIntegerField(default=0)
    available_balance_minor = models.BigIntegerField(default=0)

    # class Meta:
    #     constraints = [
    #         models.CheckConstraint(check=models.Q(available_balance_minor__gte=0), name="wallet_balance_available_not_negative"),
    #     ]


class WalletLedgerEntry(TimestampedModel):
    class EntryType(models.TextChoices):
        TOP_UP = "TOP_UP", "Top Up"
        TRANSFER_TO_VAULT = "TRANSFER_TO_VAULT", "Transfer To Vault"
        TRANSFER_FROM_VAULT = "TRANSFER_FROM_VAULT", "Transfer From Vault"
        DISBURSEMENT = "DISBURSEMENT", "Disbursement"
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"

    class ClearingStatus(models.TextChoices):
        CLEARED = "CLEARED", "Cleared"
        UNCLEARED = "UNCLEARED", "Uncleared"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    wallet = models.ForeignKey("base.Wallet", on_delete=models.CASCADE, related_name="ledger_entries")
    entry_type = models.CharField(max_length=32, choices=EntryType.choices)
    amount_minor = models.BigIntegerField()
    balance_after_minor = models.BigIntegerField()
    reference = models.CharField(max_length=255)
    clearing_status = models.CharField(max_length=16, choices=ClearingStatus.choices, default=ClearingStatus.CLEARED)
    cleared_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["wallet", "created_at"]),
            models.Index(fields=["wallet", "clearing_status"]),
        ]


class WalletHold(TimestampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        RELEASED = "RELEASED", "Released"
        CAPTURED = "CAPTURED", "Captured"
        EXPIRED = "EXPIRED", "Expired"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    wallet = models.ForeignKey("base.Wallet", on_delete=models.PROTECT, related_name="holds")
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="KES")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    reason = models.CharField(max_length=64)
    reference = models.CharField(max_length=64, unique=True)
    related_transaction = models.ForeignKey("base.LedgerTransaction", null=True, blank=True, on_delete=models.SET_NULL, related_name="capturing_holds")
    metadata = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        # constraints = [
        #     models.CheckConstraint(check=models.Q(amount_minor__gt=0), name="wallet_hold_amount_positive"),
        # ]
        indexes = [
            models.Index(fields=["wallet", "status"]),
            models.Index(fields=["status", "expires_at"]),
        ]


class LedgerTransaction(TimestampedModel):
    class Status(models.TextChoices):
        POSTED = "POSTED", "Posted"
        REVERSED = "REVERSED", "Reversed"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    reference = models.CharField(max_length=255, unique=True)
    transaction_type = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.POSTED)
    actor = models.ForeignKey("eusers.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="ledger_transactions")
    source = models.CharField(max_length=64, blank=True)
    description = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)


class LedgerMovement(TimestampedModel):
    class Direction(models.TextChoices):
        DEBIT = "DR", "Debit"
        CREDIT = "CR", "Credit"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    transaction = models.ForeignKey("base.LedgerTransaction", on_delete=models.CASCADE, related_name="movements")
    wallet = models.ForeignKey("base.Wallet", on_delete=models.SET_NULL, null=True, blank=True, related_name="ledger_movements")
    account_code = models.CharField(max_length=64)
    direction = models.CharField(max_length=2, choices=Direction.choices)
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="KES")
    balance_before_minor = models.BigIntegerField()
    balance_after_minor = models.BigIntegerField()
    description = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["wallet", "created_at"], name="ledger_movement_wallet_created"),
            models.Index(fields=["account_code", "created_at"], name="ledger_move_acct_created"),
        ]


class IdempotencyRecord(TimestampedModel):
    class Status(models.TextChoices):
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    user = models.ForeignKey("eusers.User", on_delete=models.CASCADE, null=True, blank=True, related_name="idempotency_records")
    key = models.CharField(max_length=128)
    method = models.CharField(max_length=12)
    path = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROCESSING)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(default=dict, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "key", "method", "path"], name="uniq_user_idempotency_scope"),
        ]


class TransactionEvent(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    aggregate_type = models.CharField(max_length=64, db_index=True)
    aggregate_id = models.UUIDField(db_index=True)
    event_type = models.CharField(max_length=64)
    from_status = models.CharField(max_length=32, blank=True)
    to_status = models.CharField(max_length=32, blank=True)
    actor = models.ForeignKey("eusers.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="transaction_events")
    provider_reference = models.CharField(max_length=120, blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["aggregate_type", "aggregate_id", "created_at"], name="tx_event_aggregate_created"),
        ]


class ReconciliationException(TimestampedModel):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        INVESTIGATING = "INVESTIGATING", "Investigating"
        RESOLVED = "RESOLVED", "Resolved"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    source = models.CharField(max_length=64)
    reference = models.CharField(max_length=120)
    internal_reference = models.CharField(max_length=120, blank=True)
    expected_amount_minor = models.BigIntegerField(null=True, blank=True)
    actual_amount_minor = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=3, default="KES")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "reference"], name="uniq_reconciliation_source_reference"),
        ]


class PayeePreset(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    label = models.CharField(max_length=255, unique=True)
    payee_type = models.CharField(max_length=16, choices=PayeeType.choices)
    paybill_number = models.CharField(max_length=20, blank=True)
    till_number = models.CharField(max_length=20, blank=True)
    expense_category = models.CharField(max_length=64, default="general")
    active = models.BooleanField(default=True)


class ExpenseCategory(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    name = models.CharField(max_length=64, unique=True)
    description = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name", "created_at"]

    def __str__(self):
        return self.name


class BankDirectory(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=20, unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name", "code"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class Payee(TimestampedModel):
    PayeeType = PayeeType

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    user = models.ForeignKey("eusers.User", on_delete=models.CASCADE, null=True, blank=True, related_name="payees")
    organization = models.ForeignKey(
        "base.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="payees",
    )
    preset = models.ForeignKey(
        "base.PayeePreset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payees",
    )
    payee_type = models.CharField(max_length=16, choices=PayeeType.choices)
    label = models.CharField(max_length=255)
    account_reference = models.CharField(max_length=120, blank=True)
    phone_number = models.CharField(max_length=20, blank=True)
    paybill_number = models.CharField(max_length=20, blank=True)
    till_number = models.CharField(max_length=20, blank=True)
    bank_name = models.CharField(max_length=120, blank=True)
    bank_code = models.CharField(max_length=20, blank=True)
    account_number = models.CharField(max_length=40, blank=True)
    expense_category = models.CharField(max_length=64, default="general")
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.label


class PaymentSchedule(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    payee = models.ForeignKey("base.Payee", on_delete=models.CASCADE, related_name="schedules")
    amount_minor = models.BigIntegerField()
    day_of_month = models.PositiveSmallIntegerField()
    interval_months = models.PositiveSmallIntegerField(default=1)
    next_due_date = models.DateField(default=timezone.localdate)
    requires_approval = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(day_of_month__gte=1) & models.Q(day_of_month__lte=31),
                name="schedule_day_between_1_31",
            ),
            models.CheckConstraint(
                condition=models.Q(interval_months__gte=1),
                name="schedule_interval_months_gte_1",
            ),
        ]


class PaymentBatch(TimestampedModel):
    class BatchKind(models.TextChoices):
        INDIVIDUAL_MONTHLY = "INDIVIDUAL_MONTHLY", "Individual Monthly"
        INDIVIDUAL_ADHOC = "INDIVIDUAL_ADHOC", "Individual Ad Hoc"
        CORPORATE_UPLOAD = "CORPORATE_UPLOAD", "Corporate Upload"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING_APPROVAL = "PENDING_APPROVAL", "Pending Approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        PROCESSING = "PROCESSING", "Processing"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        PARTIAL = "PARTIAL", "Partial"
        FAILED = "FAILED", "Failed"

    class PaymentMode(models.TextChoices):
        WALLET = "WALLET", "Wallet"
        STK = "STK", "STK Push"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    batch_kind = models.CharField(max_length=32, choices=BatchKind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    payment_mode = models.CharField(max_length=10, choices=PaymentMode.choices)
    user = models.ForeignKey("eusers.User", on_delete=models.CASCADE, null=True, blank=True, related_name="payment_batches")
    organization = models.ForeignKey(
        "base.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="payment_batches",
    )
    scheduled_for = models.DateField()
    description = models.CharField(max_length=255, blank=True)
    source_file_name = models.CharField(max_length=255, blank=True)
    total_amount_minor = models.BigIntegerField(default=0)
    fee_amount_minor = models.BigIntegerField(default=0)
    submitted_by = models.ForeignKey(
        "eusers.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_batches",
    )
    approved_by = models.ForeignKey(
        "eusers.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_batches",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    def recalculate_totals(self):
        total = self.instructions.aggregate(total=models.Sum("amount_minor"))["total"] or 0
        self.total_amount_minor = total
        self.save(update_fields=["total_amount_minor", "updated_at"])


class PaymentInstruction(TimestampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    batch = models.ForeignKey("base.PaymentBatch", on_delete=models.CASCADE, related_name="instructions")
    payee = models.ForeignKey("base.Payee", on_delete=models.SET_NULL, null=True, blank=True, related_name="instructions")
    recipient_name = models.CharField(max_length=255)
    recipient_type = models.CharField(max_length=16, choices=Payee.PayeeType.choices)
    destination = models.JSONField(default=dict)
    amount_minor = models.BigIntegerField()
    fee_amount_minor = models.BigIntegerField(default=0)
    category = models.CharField(max_length=64, default="general")
    external_reference = models.CharField(max_length=120, blank=True)
    provider_reference = models.CharField(max_length=120, blank=True)
    provider_response = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    failure_reason = models.CharField(max_length=255, blank=True)


class OutboxEvent(TimestampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        DONE = "DONE", "Done"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    topic = models.CharField(max_length=64, db_index=True)
    aggregate_type = models.CharField(max_length=64)
    aggregate_id = models.UUIDField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_error = models.CharField(max_length=255, blank=True)
    payload = models.JSONField(default=dict, blank=True)


class CircuitBreakerState(TimestampedModel):
    class Status(models.TextChoices):
        CLOSED = "CLOSED", "Closed"
        OPEN = "OPEN", "Open"
        HALF_OPEN = "HALF_OPEN", "Half Open"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    name = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CLOSED)
    failure_count = models.PositiveIntegerField(default=0)
    opened_until = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
