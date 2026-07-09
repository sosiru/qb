from django.db import IntegrityError, transaction

from .models import LedgerEntry, LedgerEntryLog, WalletAccount


class LedgerError(Exception):
    pass


class DuplicateLedgerOperation(LedgerError):
    def __init__(self, entry):
        super().__init__("Ledger operation already exists for this idempotency key.")
        self.entry = entry


class IdempotencyConflict(LedgerError):
    pass


class InsufficientLedgerFunds(LedgerError):
    def __init__(self, account, amount_minor, available_balance_minor):
        super().__init__("Insufficient available ledger balance.")
        self.account = account
        self.amount_minor = amount_minor
        self.available_balance_minor = available_balance_minor


BALANCE_FIELD_TO_MODEL_FIELD = {
    LedgerEntryLog.BalanceField.AVAILABLE: "available_balance_minor",
    LedgerEntryLog.BalanceField.CURRENT: "current_balance_minor",
    LedgerEntryLog.BalanceField.RESERVED: "reserved_balance_minor",
    LedgerEntryLog.BalanceField.UNCLEARED: "uncleared_balance_minor",
}


def create_ledger_account(*, account_type, user=None, organization=None, name="", currency="KES", metadata=None):
    return WalletAccount.objects.create(
        owner_type=account_type,
        user=user,
        organization=organization,
        name=name,
        currency=currency,
        metadata=metadata or {},
    )


def get_or_create_user_ledger_account(user, *, currency="KES", name=""):
    account, _ = WalletAccount.objects.get_or_create(
        user=user,
        owner_type=WalletAccount.OwnerType.USER,
        wallet_type=WalletAccount.WalletType.PRIMARY,
        currency=currency,
        defaults={"name": name or user.full_name},
    )
    return account


def get_or_create_organization_ledger_account(organization, *, currency="KES", name=""):
    account, _ = WalletAccount.objects.get_or_create(
        organization=organization,
        owner_type=WalletAccount.OwnerType.ORGANIZATION,
        wallet_type=WalletAccount.WalletType.PRIMARY,
        currency=currency,
        defaults={"name": name or organization.name},
    )
    return account


def get_or_create_user_wallet_account(user, *, wallet_type=WalletAccount.WalletType.PRIMARY, currency="KES", name=""):
    account, _ = WalletAccount.objects.get_or_create(
        user=user,
        owner_type=WalletAccount.OwnerType.USER,
        wallet_type=wallet_type,
        currency=currency,
        defaults={"name": name or user.full_name},
    )
    return account


def get_or_create_organization_wallet_account(organization, *, currency="KES", name=""):
    account, _ = WalletAccount.objects.get_or_create(
        organization=organization,
        owner_type=WalletAccount.OwnerType.ORGANIZATION,
        wallet_type=WalletAccount.WalletType.PRIMARY,
        currency=currency,
        defaults={"name": name or organization.name},
    )
    return account


def post_top_up(account, *, amount_minor, reference, idempotency_key, description="", metadata=None):
    steps = [
        (1, LedgerEntryLog.BalanceField.UNCLEARED, amount_minor, "top_up_received_uncleared"),
        (1, LedgerEntryLog.BalanceField.CURRENT, amount_minor, "top_up_received_current"),
        (2, LedgerEntryLog.BalanceField.UNCLEARED, -amount_minor, "top_up_cleared_uncleared"),
        (2, LedgerEntryLog.BalanceField.AVAILABLE, amount_minor, "top_up_cleared_available"),
    ]
    return _post_ledger_entry(
        account,
        transaction_type=LedgerEntry.TransactionType.TOP_UP,
        amount_minor=amount_minor,
        reference=reference,
        idempotency_key=idempotency_key,
        steps=steps,
        description=description,
        metadata=metadata,
    )


def post_uncleared_top_up(account, *, amount_minor, reference, idempotency_key, description="", metadata=None):
    steps = [
        (1, LedgerEntryLog.BalanceField.UNCLEARED, amount_minor, "top_up_received_uncleared"),
        (1, LedgerEntryLog.BalanceField.CURRENT, amount_minor, "top_up_received_current"),
    ]
    return _post_ledger_entry(
        account,
        transaction_type=LedgerEntry.TransactionType.TOP_UP,
        amount_minor=amount_minor,
        reference=reference,
        idempotency_key=idempotency_key,
        steps=steps,
        description=description,
        metadata={**(metadata or {}), "clearing_status": "UNCLEARED"},
    )


def clear_uncleared_top_up(entry):
    with transaction.atomic():
        locked_entry = LedgerEntry.objects.select_for_update().select_related("account").get(pk=entry.pk)
        if locked_entry.metadata.get("clearing_status") == "CLEARED":
            return locked_entry
        account = WalletAccount.objects.select_for_update().get(pk=locked_entry.account_id)
        next_sequence = (locked_entry.logs.order_by("-sequence").first().sequence if locked_entry.logs.exists() else 1) + 1
        _apply_balance_delta(
            account,
            locked_entry,
            next_sequence,
            LedgerEntryLog.BalanceField.UNCLEARED,
            -locked_entry.amount_minor,
            "top_up_cleared_uncleared",
        )
        _apply_balance_delta(
            account,
            locked_entry,
            next_sequence,
            LedgerEntryLog.BalanceField.AVAILABLE,
            locked_entry.amount_minor,
            "top_up_cleared_available",
        )
        locked_entry.metadata = {**locked_entry.metadata, "clearing_status": "CLEARED"}
        locked_entry.balance_after_minor = account.available_balance_minor
        locked_entry.save(update_fields=["metadata", "balance_after_minor", "updated_at"])
        account.save(
            update_fields=[
                "available_balance_minor",
                "current_balance_minor",
                "reserved_balance_minor",
                "uncleared_balance_minor",
                "updated_at",
            ]
        )
        return locked_entry


def post_withdrawal(account, *, amount_minor, reference, idempotency_key, description="", metadata=None):
    steps = [
        (1, LedgerEntryLog.BalanceField.AVAILABLE, -amount_minor, "withdrawal_reserved_available"),
        (1, LedgerEntryLog.BalanceField.RESERVED, amount_minor, "withdrawal_reserved_reserved"),
        (2, LedgerEntryLog.BalanceField.CURRENT, -amount_minor, "withdrawal_settled_current"),
        (2, LedgerEntryLog.BalanceField.RESERVED, -amount_minor, "withdrawal_settled_reserved"),
    ]
    return _post_ledger_entry(
        account,
        transaction_type=LedgerEntry.TransactionType.WITHDRAWAL,
        amount_minor=amount_minor,
        reference=reference,
        idempotency_key=idempotency_key,
        steps=steps,
        description=description,
        metadata=metadata,
    )


def post_wallet_adjustment(account, *, amount_minor, reference, idempotency_key, description="", metadata=None):
    if amount_minor >= 0:
        steps = [
            (1, LedgerEntryLog.BalanceField.CURRENT, amount_minor, "wallet_adjustment_current"),
            (1, LedgerEntryLog.BalanceField.AVAILABLE, amount_minor, "wallet_adjustment_available"),
        ]
    else:
        steps = [
            (1, LedgerEntryLog.BalanceField.AVAILABLE, amount_minor, "wallet_adjustment_available"),
            (1, LedgerEntryLog.BalanceField.CURRENT, amount_minor, "wallet_adjustment_current"),
        ]
    return _post_ledger_entry(
        account,
        transaction_type=LedgerEntry.TransactionType.ADJUSTMENT,
        amount_minor=abs(amount_minor),
        reference=reference,
        idempotency_key=idempotency_key,
        steps=steps,
        description=description,
        metadata=metadata,
    )


def reserve_wallet_funds(account, *, amount_minor, reference, idempotency_key, description="", metadata=None):
    steps = [
        (1, LedgerEntryLog.BalanceField.AVAILABLE, -amount_minor, "funds_reserved_available"),
        (1, LedgerEntryLog.BalanceField.RESERVED, amount_minor, "funds_reserved_reserved"),
    ]
    return _post_ledger_entry(
        account,
        transaction_type=LedgerEntry.TransactionType.ADJUSTMENT,
        amount_minor=amount_minor,
        reference=reference,
        idempotency_key=idempotency_key,
        steps=steps,
        description=description,
        metadata=metadata,
    )


def release_wallet_reserve(account, *, amount_minor, reference, idempotency_key, description="", metadata=None):
    steps = [
        (1, LedgerEntryLog.BalanceField.RESERVED, -amount_minor, "funds_released_reserved"),
        (1, LedgerEntryLog.BalanceField.AVAILABLE, amount_minor, "funds_released_available"),
    ]
    return _post_ledger_entry(
        account,
        transaction_type=LedgerEntry.TransactionType.ADJUSTMENT,
        amount_minor=amount_minor,
        reference=reference,
        idempotency_key=idempotency_key,
        steps=steps,
        description=description,
        metadata=metadata,
    )


def _post_ledger_entry(
    account,
    *,
    transaction_type,
    amount_minor,
    reference,
    idempotency_key,
    steps,
    description="",
    metadata=None,
):
    if amount_minor <= 0:
        raise LedgerError("Ledger amount must be greater than zero.")

    metadata = metadata or {}
    with transaction.atomic():
        locked_account = WalletAccount.objects.select_for_update().get(pk=account.pk)
        existing = LedgerEntry.objects.filter(account=locked_account, idempotency_key=idempotency_key).first()
        if existing:
            _ensure_idempotent_retry(
                existing,
                transaction_type=transaction_type,
                amount_minor=amount_minor,
                reference=reference,
            )
            return existing
        if locked_account.status != WalletAccount.Status.ACTIVE:
            raise LedgerError("Ledger account is not active.")

        balance_before = locked_account.available_balance_minor
        if transaction_type == LedgerEntry.TransactionType.WITHDRAWAL and balance_before < amount_minor:
            raise InsufficientLedgerFunds(locked_account, amount_minor, balance_before)

        try:
            entry = LedgerEntry.objects.create(
                account=locked_account,
                transaction_type=transaction_type,
                reference=reference,
                idempotency_key=idempotency_key,
                amount_minor=amount_minor,
                currency=locked_account.currency,
                balance_before_minor=balance_before,
                balance_after_minor=balance_before,
                description=description,
                metadata=metadata,
            )
        except IntegrityError:
            existing = LedgerEntry.objects.filter(account=locked_account, idempotency_key=idempotency_key).first()
            if existing:
                _ensure_idempotent_retry(
                    existing,
                    transaction_type=transaction_type,
                    amount_minor=amount_minor,
                    reference=reference,
                )
                return existing
            raise LedgerError("Ledger reference already exists.")

        for sequence, balance_field, delta_minor, reason in steps:
            _apply_balance_delta(locked_account, entry, sequence, balance_field, delta_minor, reason)

        entry.balance_after_minor = locked_account.available_balance_minor
        entry.save(update_fields=["balance_after_minor", "updated_at"])
        locked_account.save(
            update_fields=[
                "available_balance_minor",
                "current_balance_minor",
                "reserved_balance_minor",
                "uncleared_balance_minor",
                "updated_at",
            ]
        )
        return entry


def _ensure_idempotent_retry(existing, *, transaction_type, amount_minor, reference):
    if (
        existing.transaction_type != transaction_type
        or existing.amount_minor != amount_minor
    ):
        raise IdempotencyConflict("Idempotency key was already used for a different ledger operation.")


def _apply_balance_delta(account, entry, sequence, balance_field, delta_minor, reason):
    model_field = BALANCE_FIELD_TO_MODEL_FIELD[balance_field]
    balance_before = getattr(account, model_field)
    balance_after = balance_before + delta_minor
    if balance_after < 0 and balance_field in {
        LedgerEntryLog.BalanceField.AVAILABLE,
        LedgerEntryLog.BalanceField.RESERVED,
        LedgerEntryLog.BalanceField.UNCLEARED,
    }:
        raise LedgerError(f"{balance_field} balance cannot become negative.")

    setattr(account, model_field, balance_after)
    LedgerEntryLog.objects.create(
        entry=entry,
        account=account,
        sequence=sequence,
        balance_field=balance_field,
        delta_minor=delta_minor,
        balance_before_minor=balance_before,
        balance_after_minor=balance_after,
        reason=reason,
    )
