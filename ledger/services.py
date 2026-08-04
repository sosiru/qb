import json
import logging
import secrets
import time
from decimal import Decimal
from urllib import error, request

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    Account,
    AccountFieldType,
    BalanceEntryType,
    BalanceLog,
    BalanceLogEntry,
    EntryType,
    ExecutionProfile,
    PaymentMethod,
    PaymentRequest,
    RuleProfile,
    RuleProfileCommand,
    State,
    Transaction,
    TransactionType,
)

logger = logging.getLogger(__name__)


class LedgerError(Exception):
    pass


class DuplicateLedgerOperation(LedgerError):
    def __init__(self, transaction_record):
        super().__init__("Ledger operation already exists for this idempotency key.")
        self.transaction = transaction_record


class IdempotencyConflict(LedgerError):
    pass


class InsufficientLedgerFunds(LedgerError):
    def __init__(self, account, amount_minor, available_balance_minor):
        super().__init__("Insufficient available balance.")
        self.account = account
        self.amount_minor = amount_minor
        self.available_balance_minor = available_balance_minor


ACTIVE = "Active"
COMPLETED = "Completed"
FAILED = "Failed"

FIELD_TO_ACCOUNT_ATTR = {
    "Available": "available_balance_minor",
    "Current": "current_balance_minor",
    "Reserved": "reserved_balance_minor",
    "Uncleared": "uncleared_balance_minor",
    "Charge": "charge_balance_minor",
}

DEFAULT_PROFILES = {
    "InitiatePayIn": [["credit_account_uncleared", "credit_account_current"]],
    "ApprovePayIn": [["debit_account_uncleared", "credit_account_available"]],
    "InitiatePayout": [["debit_account_available", "credit_account_reserved"]],
    "ApprovePayout": [["debit_account_current", "debit_account_reserved"]],
    "FailPayout": [["debit_account_reserved", "credit_account_available"]],
    "WalletAdjustmentCredit": [["credit_account_current", "credit_account_available"]],
    "WalletAdjustmentDebit": [["debit_account_available", "debit_account_current"]],
}


def get_state(name=ACTIVE):
    state, _ = State.objects.get_or_create(name=name, defaults={"description": f"{name} state"})
    return state


def get_named(model, name, **defaults):
    defaults.setdefault("state", get_state())
    obj, _ = model.objects.get_or_create(name=name, defaults=defaults)
    return obj


def ensure_ledger_defaults():
    for state_name in [ACTIVE, COMPLETED, FAILED, "Disabled"]:
        get_state(state_name)
    for field_name in FIELD_TO_ACCOUNT_ATTR:
        get_named(AccountFieldType, field_name)
    for entry_name in ["Dr", "Cr"]:
        get_named(EntryType, entry_name)
    for method_name in ["Wallet", "STK Push", "Pay In", "Payout", "Sandbox"]:
        get_named(PaymentMethod, method_name)
    for name, simple_name in [
        ("WalletTopup", "Topup"),
        ("WalletWithdrawal", "Withdrawal"),
        ("BatchDisbursement", "Disbursement"),
        ("WalletTransfer", "Transfer"),
        ("Adjustment", "Adjustment"),
    ]:
        get_named(TransactionType, name, simple_name=simple_name)
    for entry_name in DEFAULT_PROFILES:
        get_named(BalanceEntryType, entry_name)
    for profile_name, rules in DEFAULT_PROFILES.items():
        profile = get_named(ExecutionProfile, profile_name)
        for rule_index, commands in enumerate(rules, start=1):
            rule, _ = RuleProfile.objects.get_or_create(
                execution_profile=profile,
                order=rule_index,
                defaults={"name": profile_name, "state": get_state()},
            )
            for command_index, command_name in enumerate(commands, start=1):
                RuleProfileCommand.objects.get_or_create(
                    rule_profile=rule,
                    order=command_index,
                    defaults={"name": command_name, "state": get_state()},
                )


def _account_number():
    return f"10{secrets.randbelow(10**8):08d}"


def _unique_account_number():
    for _ in range(20):
        account_number = _account_number()
        if not Account.objects.filter(account_number=account_number).exists():
            return account_number
    raise LedgerError("Could not generate a unique account number.")


def _transaction_reference(prefix="TRX"):
    return f"{prefix}-{secrets.token_hex(6).upper()}"


def unique_transaction_reference(prefix="TRX"):
    for _ in range(20):
        reference = _transaction_reference(prefix)
        if not Transaction.objects.filter(internal_reference=reference).exists():
            return reference
    raise LedgerError("Could not generate a unique transaction reference.")


def get_or_create_user_account(user, *, account_kind=Account.AccountKind.PRIMARY, currency="KES", name=""):
    ensure_ledger_defaults()
    account, _ = Account.objects.get_or_create(
        user=user,
        owner_type=Account.OwnerType.USER,
        account_kind=account_kind,
        currency=currency,
        defaults={
            "account_number": _unique_account_number(),
            "name": name or user.full_name,
            "state": get_state(),
        },
    )
    return account


def get_or_create_organization_account(organization, *, account_kind=Account.AccountKind.PRIMARY, currency="KES", name=""):
    ensure_ledger_defaults()
    account, _ = Account.objects.get_or_create(
        organization=organization,
        owner_type=Account.OwnerType.ORGANIZATION,
        account_kind=account_kind,
        currency=currency,
        defaults={
            "account_number": _unique_account_number(),
            "name": name or organization.name,
            "state": get_state(),
        },
    )
    return account


def get_or_create_system_account(name, *, currency="KES"):
    ensure_ledger_defaults()
    account, _ = Account.objects.get_or_create(
        owner_type=Account.OwnerType.SYSTEM,
        account_kind=Account.AccountKind.PRIMARY,
        currency=currency,
        name=name,
        defaults={"account_number": _unique_account_number(), "state": get_state()},
    )
    return account


class ProcessorBase:
    def process(self, *, balance_log, account, amount_minor, **kwargs):
        rule_profile = RuleProfile.objects.filter(
            execution_profile__name=self.__class__.__name__,
            state__name=ACTIVE,
        ).order_by("order").first()
        if not rule_profile:
            raise LedgerError(f"Rule profile {self.__class__.__name__} is not configured.")
        result = None
        for command in rule_profile.commands.filter(state__name=ACTIVE).order_by("order"):
            result = getattr(self, command.name)(account=account, balance_log=balance_log, amount_minor=amount_minor, **kwargs)
            if not result:
                raise LedgerError(f"Rule command {command.name} failed.")
        return result

    def _move(self, *, account, balance_log, balance_type, amount_minor, entry_type_name):
        field_type = get_named(AccountFieldType, balance_type)
        entry_type = get_named(EntryType, entry_type_name)
        model_field = FIELD_TO_ACCOUNT_ATTR[balance_type]
        balance_before = getattr(account, model_field)
        signed_amount = int(amount_minor) if entry_type_name == "Cr" else -int(amount_minor)
        balance_after = balance_before + signed_amount
        if balance_after < 0 and balance_type in {"Available", "Reserved", "Uncleared"}:
            raise InsufficientLedgerFunds(account, int(amount_minor), balance_before)
        setattr(account, model_field, balance_after)
        BalanceLogEntry.objects.create(
            balance_log=balance_log,
            entry_type=entry_type,
            account_field_type=field_type,
            amount_transacted_minor=int(amount_minor),
            balance_before_minor=balance_before,
            balance_after_minor=balance_after,
            state=get_state(COMPLETED),
        )
        return account

    def credit(self, account, balance_log, balance_type, amount_minor):
        return self._move(account=account, balance_log=balance_log, balance_type=balance_type, amount_minor=amount_minor, entry_type_name="Cr")

    def debit(self, account, balance_log, balance_type, amount_minor):
        return self._move(account=account, balance_log=balance_log, balance_type=balance_type, amount_minor=amount_minor, entry_type_name="Dr")

    def credit_account_available(self, account, balance_log, amount_minor, **kwargs):
        return self.credit(account, balance_log, "Available", amount_minor)

    def debit_account_available(self, account, balance_log, amount_minor, **kwargs):
        return self.debit(account, balance_log, "Available", amount_minor)

    def credit_account_current(self, account, balance_log, amount_minor, **kwargs):
        return self.credit(account, balance_log, "Current", amount_minor)

    def debit_account_current(self, account, balance_log, amount_minor, **kwargs):
        return self.debit(account, balance_log, "Current", amount_minor)

    def credit_account_reserved(self, account, balance_log, amount_minor, **kwargs):
        return self.credit(account, balance_log, "Reserved", amount_minor)

    def debit_account_reserved(self, account, balance_log, amount_minor, **kwargs):
        return self.debit(account, balance_log, "Reserved", amount_minor)

    def credit_account_uncleared(self, account, balance_log, amount_minor, **kwargs):
        return self.credit(account, balance_log, "Uncleared", amount_minor)

    def debit_account_uncleared(self, account, balance_log, amount_minor, **kwargs):
        return self.debit(account, balance_log, "Uncleared", amount_minor)


PROCESSORS = {
    name: type(name, (ProcessorBase,), {})
    for name in DEFAULT_PROFILES
}


def execute_profile(transaction_record, account, amount_minor, balance_entry_type_name, *, reference="", receipt="", description="", metadata=None):
    ensure_ledger_defaults()
    profile = ExecutionProfile.objects.filter(name=balance_entry_type_name, state__name=ACTIVE).first()
    if not profile:
        ensure_ledger_defaults()
        profile = ExecutionProfile.objects.filter(name=balance_entry_type_name, state__name=ACTIVE).first()
    if not profile:
        raise LedgerError(f"Execution profile {balance_entry_type_name} is not configured.")
    balance_entry_type = get_named(BalanceEntryType, balance_entry_type_name)
    processor_class = PROCESSORS.get(profile.name)
    if not processor_class:
        raise LedgerError(f"No processor registered for {profile.name}.")
    balance_log = BalanceLog.objects.create(
        transaction=transaction_record,
        balance_entry_type=balance_entry_type,
        reference=reference,
        receipt=receipt,
        amount_transacted_minor=int(amount_minor),
        description=description,
        state=get_state(ACTIVE),
        metadata=metadata or {},
    )
    try:
        for rule in profile.rules.filter(state__name=ACTIVE).order_by("order"):
            processor = processor_class()
            for command in rule.commands.filter(state__name=ACTIVE).order_by("order"):
                getattr(processor, command.name)(account=account, balance_log=balance_log, amount_minor=amount_minor)
            if rule.sleep_seconds:
                time.sleep(rule.sleep_seconds)
        balance_log.status = BalanceLog.Status.COMPLETED
        balance_log.state = get_state(COMPLETED)
        balance_log.total_balance_minor = account.available_balance_minor
        balance_log.save(update_fields=["status", "state", "total_balance_minor", "updated_at"])
        return balance_log
    except Exception:
        balance_log.status = BalanceLog.Status.FAILED
        balance_log.state = get_state(FAILED)
        balance_log.save(update_fields=["status", "state", "updated_at"])
        raise


def create_processing_transaction(
    account,
    *,
    transaction_type_name,
    direction,
    amount_minor,
    payment_method_name="Wallet",
    internal_reference="",
    idempotency_key="",
    description="",
    request_payload=None,
    metadata=None,
):
    if int(amount_minor) <= 0:
        raise LedgerError("Transaction amount must be greater than zero.")
    ensure_ledger_defaults()
    transaction_type = get_named(TransactionType, transaction_type_name, simple_name=transaction_type_name)
    payment_method = get_named(PaymentMethod, payment_method_name)
    if idempotency_key:
        existing = Transaction.objects.filter(account=account, idempotency_key=idempotency_key).first()
        if existing:
            if existing.amount_minor != int(amount_minor) or existing.transaction_type_id != transaction_type.id:
                raise IdempotencyConflict("Idempotency key was already used for a different transaction.")
            raise DuplicateLedgerOperation(existing)
    return Transaction.objects.create(
        account=account,
        transaction_type=transaction_type,
        payment_method=payment_method,
        direction=direction,
        internal_reference=internal_reference or unique_transaction_reference(),
        idempotency_key=idempotency_key or "",
        amount_minor=int(amount_minor),
        currency=account.currency,
        status=Transaction.Status.PROCESSING,
        request_payload=request_payload or {},
        description=description,
        state=get_state(ACTIVE),
        metadata=metadata or {},
    )


def initiate_pay_in(account, *, amount_minor, reference="", idempotency_key="", description="Pay in", metadata=None):
    with transaction.atomic():
        locked = Account.objects.select_for_update().get(pk=account.pk)
        tx = create_processing_transaction(
            locked,
            transaction_type_name="WalletTopup",
            direction=Transaction.Direction.PAY_IN,
            amount_minor=amount_minor,
            payment_method_name="Pay In",
            internal_reference=reference,
            idempotency_key=idempotency_key,
            description=description,
            metadata=metadata,
        )
        execute_profile(tx, locked, amount_minor, "InitiatePayIn", reference=tx.internal_reference, description=description)
        locked.save(update_fields=["current_balance_minor", "uncleared_balance_minor", "available_balance_minor", "reserved_balance_minor", "updated_at"])
        return tx


def complete_pay_in(transaction_record, *, response_payload=None, receipt="", confirmation_key=""):
    with transaction.atomic():
        tx = Transaction.objects.select_for_update().select_related("account").get(pk=transaction_record.pk)
        if tx.status == Transaction.Status.COMPLETED:
            return tx
        account = Account.objects.select_for_update().get(pk=tx.account_id)
        execute_profile(tx, account, tx.amount_minor, "ApprovePayIn", reference=tx.internal_reference, receipt=receipt, description=tx.description)
        account.save(update_fields=["current_balance_minor", "uncleared_balance_minor", "available_balance_minor", "reserved_balance_minor", "updated_at"])
        tx.status = Transaction.Status.COMPLETED
        tx.transaction_receipt = receipt or tx.transaction_receipt
        tx.confirmation_key = confirmation_key or tx.confirmation_key
        tx.response_payload = response_payload or tx.response_payload
        tx.processed_at = timezone.now()
        tx.state = get_state(COMPLETED)
        tx.save(update_fields=["status", "transaction_receipt", "confirmation_key", "response_payload", "processed_at", "state", "updated_at"])
        return tx


def initiate_payout(account, *, amount_minor, reference="", idempotency_key="", description="Payout", metadata=None):
    with transaction.atomic():
        locked = Account.objects.select_for_update().get(pk=account.pk)
        tx = create_processing_transaction(
            locked,
            transaction_type_name="WalletWithdrawal",
            direction=Transaction.Direction.PAY_OUT,
            amount_minor=amount_minor,
            payment_method_name="Payout",
            internal_reference=reference,
            idempotency_key=idempotency_key,
            description=description,
            metadata=metadata,
        )
        execute_profile(tx, locked, amount_minor, "InitiatePayout", reference=tx.internal_reference, description=description)
        locked.save(update_fields=["current_balance_minor", "uncleared_balance_minor", "available_balance_minor", "reserved_balance_minor", "updated_at"])
        return tx


def complete_payout(transaction_record, *, response_payload=None, receipt="", confirmation_key=""):
    with transaction.atomic():
        tx = Transaction.objects.select_for_update().select_related("account").get(pk=transaction_record.pk)
        if tx.status == Transaction.Status.COMPLETED:
            return tx
        account = Account.objects.select_for_update().get(pk=tx.account_id)
        execute_profile(tx, account, tx.amount_minor, "ApprovePayout", reference=tx.internal_reference, receipt=receipt, description=tx.description)
        account.save(update_fields=["current_balance_minor", "uncleared_balance_minor", "available_balance_minor", "reserved_balance_minor", "updated_at"])
        tx.status = Transaction.Status.COMPLETED
        tx.transaction_receipt = receipt or tx.transaction_receipt
        tx.confirmation_key = confirmation_key or tx.confirmation_key
        tx.response_payload = response_payload or tx.response_payload
        tx.processed_at = timezone.now()
        tx.state = get_state(COMPLETED)
        tx.save(update_fields=["status", "transaction_receipt", "confirmation_key", "response_payload", "processed_at", "state", "updated_at"])
        return tx


def fail_transaction(transaction_record, *, reason="", response_payload=None):
    with transaction.atomic():
        tx = Transaction.objects.select_for_update().select_related("account").get(pk=transaction_record.pk)
        if tx.status in {Transaction.Status.COMPLETED, Transaction.Status.FAILED}:
            return tx
        account = Account.objects.select_for_update().get(pk=tx.account_id)
        if tx.direction == Transaction.Direction.PAY_OUT and account.reserved_balance_minor >= tx.amount_minor:
            execute_profile(tx, account, tx.amount_minor, "FailPayout", reference=tx.internal_reference, description=reason or tx.description)
            account.save(update_fields=["current_balance_minor", "uncleared_balance_minor", "available_balance_minor", "reserved_balance_minor", "updated_at"])
        tx.status = Transaction.Status.FAILED
        tx.failure_reason = reason[:255]
        tx.response_payload = response_payload or tx.response_payload
        tx.processed_at = timezone.now()
        tx.state = get_state(FAILED)
        tx.save(update_fields=["status", "failure_reason", "response_payload", "processed_at", "state", "updated_at"])
        return tx


def transfer_between_accounts(debit_account, credit_account, *, amount_minor, reference="", description="Wallet transfer", metadata=None):
    with transaction.atomic():
        debit_tx = initiate_payout(
            debit_account,
            amount_minor=amount_minor,
            reference=f"{reference or unique_transaction_reference('TRF')}-DR",
            description=description,
            metadata={**(metadata or {}), "transfer_side": "debit"},
        )
        complete_payout(debit_tx)
        credit_tx = initiate_pay_in(
            credit_account,
            amount_minor=amount_minor,
            reference=f"{reference or unique_transaction_reference('TRF')}-CR",
            description=description,
            metadata={**(metadata or {}), "transfer_side": "credit"},
        )
        complete_pay_in(credit_tx)
        return debit_tx, credit_tx


class PaymentService:
    PROCESSING_TIMEOUT_SECONDS = 180
    LIPASYNC_SUCCESS_STATUSES = {"CAPTURED", "SETTLED", "SUCCESS"}
    LIPASYNC_FAILURE_STATUSES = {"FAILED", "CANCELLED", "EXPIRED", "DISPUTED", "REFUNDED", "MANUAL_REVIEW"}
    LIPASYNC_PENDING_STATUSES = {
        "INITIATED",
        "QUEUED",
        "REQUIRES_ACTION",
        "PROCESSING",
        "PENDING",
        "AUTHORIZED",
        "PARTIALLY_CAPTURED",
        "PARTIALLY_REFUNDED",
    }

    def __init__(self, *, sandbox=None, base_url=None, api_key=None, timeout=None):
        self.sandbox = getattr(settings, "PAYMENT_MICROSERVICE_SANDBOX", True) if sandbox is None else sandbox
        self.base_url = (base_url or getattr(settings, "PAYMENT_MICROSERVICE_URL", "")).rstrip("/")
        self.api_key = api_key or getattr(settings, "PAYMENT_MICROSERVICE_API_KEY", "")
        self.timeout = timeout or getattr(settings, "PAYMENT_MICROSERVICE_TIMEOUT_SECONDS", 30)

    def initiate_stk_push(self, account, *, amount_minor, phone_number, idempotency_key="", metadata=None):
        tx = initiate_pay_in(
            account,
            amount_minor=amount_minor,
            reference=unique_transaction_reference("STK"),
            idempotency_key=idempotency_key,
            description="STK push wallet top-up",
            metadata={**(metadata or {}), "phone_number": phone_number},
        )
        return self._create_or_submit_request(
            tx,
            PaymentRequest.Operation.STK_PUSH,
            {"phone_number": phone_number},
            metadata=metadata,
        )

    def initiate_pay_in(self, account, *, amount_minor, idempotency_key="", metadata=None):
        if not self.sandbox:
            raise LedgerError("Lipasync live payments require STK push for pay-ins.")
        tx = initiate_pay_in(
            account,
            amount_minor=amount_minor,
            reference=unique_transaction_reference("PIN"),
            idempotency_key=idempotency_key,
            description="Pay in",
            metadata=metadata,
        )
        return self._create_or_submit_request(tx, PaymentRequest.Operation.PAY_IN, {}, metadata=metadata)

    def initiate_payout(self, account, *, amount_minor, destination=None, idempotency_key="", metadata=None):
        if not self.sandbox:
            self._validate_lipasync_payout_destination(destination or {})
        tx = initiate_payout(
            account,
            amount_minor=amount_minor,
            reference=unique_transaction_reference("POT"),
            idempotency_key=idempotency_key,
            description="Payout",
            metadata={**(metadata or {}), "destination": destination or {}},
        )
        return self._create_or_submit_request(
            tx,
            PaymentRequest.Operation.PAYOUT,
            {"destination": destination or {}},
            metadata=metadata,
        )

    def initiate_instruction_payout(self, instruction, *, transaction_record=None, idempotency_key="", metadata=None):
        destination = instruction.destination or {}
        if not self.sandbox:
            self._validate_lipasync_payout_destination(destination)
        if transaction_record is None:
            account = instruction.batch.organization.billing_accounts.first() if instruction.batch.organization_id else instruction.batch.user.billing_accounts.first()
            tx = initiate_payout(
                account,
                amount_minor=instruction.amount_minor,
                reference=unique_transaction_reference("POT"),
                idempotency_key=idempotency_key,
                description=f"Payout to {instruction.recipient_name}",
                metadata={**(metadata or {}), "instruction_id": str(instruction.id), "batch_id": str(instruction.batch_id)},
            )
        else:
            tx = transaction_record
        return self._create_or_submit_request(
            tx,
            PaymentRequest.Operation.PAYOUT,
            {
                "instruction_id": str(instruction.id),
                "batch_id": str(instruction.batch_id),
                "amount_minor": instruction.amount_minor,
                "recipient_name": instruction.recipient_name,
                "recipient_type": instruction.recipient_type,
                "destination": destination,
            },
            originator_ref=unique_transaction_reference("REQ"),
            metadata={**(metadata or {}), "instruction_id": str(instruction.id), "batch_id": str(instruction.batch_id)},
        )

    def _create_or_submit_request(self, tx, operation, extra_payload, *, originator_ref=None, metadata=None):
        originator_ref = originator_ref or tx.internal_reference
        metadata = metadata or tx.metadata or {}
        if self.sandbox:
            provider_path = ""
            provider_payload = {
                "amount": self._amount_major(tx.amount_minor),
                "currency": tx.currency,
                "external_reference": originator_ref,
                "idempotency_key": originator_ref,
                "payment_payload": extra_payload,
            }
        else:
            provider_path, provider_payload = self._lipasync_initiate_request(
                tx,
                operation,
                extra_payload,
                originator_ref=originator_ref,
            )
            callback_url = getattr(settings, "PAYMENT_CALLBACK_URL", "").strip()
            if callback_url and not self._is_pesaway_core():
                provider_payload["callback_url"] = callback_url
        stored_payload = {
            "originator_ref": originator_ref,
            "external_reference": originator_ref,
            "idempotency_key": originator_ref,
            "amount_minor": tx.amount_minor,
            "amount": provider_payload["amount"],
            "currency": tx.currency,
            "operation": operation,
            "payment_payload": provider_payload.get("payment_payload") or provider_payload.get("provider_payload") or {},
            "provider_path": provider_path,
            "metadata": metadata,
            **extra_payload,
        }
        payment_request = PaymentRequest.objects.create(
            transaction=tx,
            operation=operation,
            originator_ref=originator_ref,
            sandbox=self.sandbox,
            request_payload=stored_payload,
        )
        if self.sandbox:
            response_payload = {
                "success": True,
                "originator_ref": originator_ref,
                "request_id": f"SIM-{secrets.token_hex(5).upper()}",
                "transaction_receipt": f"SIM-{secrets.token_hex(5).upper()}",
                "confirmation_key": secrets.token_hex(12),
            }
            payment_request.request_id = response_payload["request_id"]
            payment_request.response_payload = response_payload
            payment_request.save(update_fields=["request_id", "response_payload", "updated_at"])
            self.handle_webhook(response_payload)
            payment_request.refresh_from_db()
            return payment_request
        response_payload = self._post(provider_path, provider_payload)
        normalized_payload = self._normalize_microservice_payload(response_payload, payment_request=payment_request)
        payment_request.response_payload = response_payload
        payment_request.request_id = normalized_payload.get("request_id") or ""
        payment_request.save(update_fields=["response_payload", "request_id", "updated_at"])
        if "success" in normalized_payload:
            self.handle_webhook({**response_payload, **normalized_payload})
            payment_request.refresh_from_db()
        return payment_request

    def query_status(self, payment_request):
        if self._is_pesaway_core() and payment_request.operation == PaymentRequest.Operation.PAYOUT and payment_request.request_id:
            response_payload = self._get(f"/outbound-transfers/{payment_request.request_id}/status/")
            payment_request.last_query_at = timezone.now()
            payment_request.response_payload = response_payload
            payment_request.save(update_fields=["last_query_at", "response_payload", "updated_at"])
            normalized_payload = self._normalize_microservice_payload(response_payload, payment_request=payment_request)
            if "success" in normalized_payload:
                self.handle_webhook({**response_payload, **normalized_payload})
            return response_payload
        payload = {"originator_ref": payment_request.originator_ref, "request_id": payment_request.request_id}
        response_payload = self._post("/transactions/status/", payload)
        payment_request.last_query_at = timezone.now()
        payment_request.response_payload = response_payload
        payment_request.save(update_fields=["last_query_at", "response_payload", "updated_at"])
        normalized_payload = self._normalize_microservice_payload(response_payload, payment_request=payment_request)
        if "success" in normalized_payload:
            self.handle_webhook({**response_payload, **normalized_payload})
        return response_payload

    @transaction.atomic
    def handle_webhook(self, payload):
        normalized_payload = self._normalize_microservice_payload(payload)
        originator_ref = normalized_payload.get("originator_ref") or ""
        request_id = normalized_payload.get("request_id") or ""
        if not originator_ref and not request_id:
            raise LedgerError("originator_ref or request_id is required.")
        lookup = {"originator_ref": originator_ref} if originator_ref else {"request_id": request_id}
        payment_request = PaymentRequest.objects.select_for_update().select_related("transaction").get(**lookup)
        tx = payment_request.transaction
        payload = {**payload, **self._normalize_microservice_payload(payload, payment_request=payment_request)}
        if payment_request.request_id != request_id and request_id:
            payment_request.request_id = request_id
        if payment_request.status in {
            PaymentRequest.Status.COMPLETED,
            PaymentRequest.Status.FAILED,
        }:
            return payment_request
        request_payload = payment_request.request_payload or {}
        request_metadata = request_payload.get("metadata") or {}
        instruction_id = request_payload.get("instruction_id") or request_metadata.get("instruction_id")
        batch_collection_id = (
            request_payload.get("batch_id") or request_metadata.get("batch_id")
            if request_payload.get("purpose") == "batch_collection" or request_metadata.get("purpose") == "batch_collection"
            else ""
        )
        if "success" not in payload:
            payment_request.response_payload = payload
            payment_request.last_error = ""
            payment_request.save(update_fields=["request_id", "last_error", "response_payload", "updated_at"])
            return payment_request
        success = bool(payload.get("success"))
        if success:
            if instruction_id:
                from base.models import PaymentInstruction
                from base.services import record_instruction_success

                instruction = PaymentInstruction.objects.get(id=instruction_id)
                record_instruction_success(
                    instruction,
                    {"callback": payload, "payment_request_id": str(payment_request.id)},
                    microservice_request_id=payload.get("transaction_receipt") or payment_request.request_id or payment_request.originator_ref,
                )
            elif batch_collection_id:
                from base.models import PaymentBatch
                from base.services import mark_batch_collection_complete

                complete_pay_in(
                    tx,
                    response_payload=payload,
                    receipt=payload.get("transaction_receipt") or "",
                    confirmation_key=payload.get("confirmation_key") or "",
                )
                batch = PaymentBatch.objects.get(id=batch_collection_id)
                mark_batch_collection_complete(batch, payload)
            elif tx.direction == Transaction.Direction.PAY_OUT:
                complete_payout(
                    tx,
                    response_payload=payload,
                    receipt=payload.get("transaction_receipt") or "",
                    confirmation_key=payload.get("confirmation_key") or "",
                )
            else:
                complete_pay_in(
                    tx,
                    response_payload=payload,
                    receipt=payload.get("transaction_receipt") or "",
                    confirmation_key=payload.get("confirmation_key") or "",
                )
                if tx.transaction_type.name == "WalletTopup":
                    from notifications.services import queue_wallet_topup_completed_notification

                    queue_wallet_topup_completed_notification(tx)
            payment_request.status = PaymentRequest.Status.COMPLETED
            payment_request.last_error = ""
        else:
            reason = str(payload.get("failure_reason") or payload.get("message") or payload.get("error") or "Payment failed")
            if instruction_id:
                from base.models import PaymentInstruction
                from base.services import record_instruction_failure

                instruction = PaymentInstruction.objects.get(id=instruction_id)
                record_instruction_failure(
                    instruction,
                    reason,
                    microservice_response={"callback": payload, "payment_request_id": str(payment_request.id)},
                )
            elif batch_collection_id:
                from base.models import PaymentBatch
                from base.services import record_batch_failure

                fail_transaction(tx, reason=reason, response_payload=payload)
                batch = PaymentBatch.objects.get(id=batch_collection_id)
                record_batch_failure(batch, reason)
            else:
                fail_transaction(tx, reason=reason, response_payload=payload)
            payment_request.status = PaymentRequest.Status.FAILED
            payment_request.last_error = reason[:255]
        payment_request.response_payload = payload
        payment_request.save(update_fields=["request_id", "status", "last_error", "response_payload", "updated_at"])
        return payment_request

    def fail_processing_request(self, payment_request, reason):
        payload = {
            **(payment_request.response_payload or {}),
            "success": False,
            "originator_ref": payment_request.originator_ref,
            "request_id": payment_request.request_id,
            "status": PaymentRequest.Status.FAILED,
            "failure_reason": reason,
            "error": reason,
        }
        return self.handle_webhook(payload)

    def retry_stale_processing(self, *, older_than_seconds=PROCESSING_TIMEOUT_SECONDS, limit=50, query_status=False):
        cutoff = timezone.now() - timezone.timedelta(seconds=older_than_seconds)
        requests = PaymentRequest.objects.filter(status=PaymentRequest.Status.PROCESSING, created_at__lt=cutoff).order_by("created_at")[:limit]
        processed = 0
        for payment_request in requests:
            timeout_reason = f"Payment request timed out after {older_than_seconds} seconds without a final microservice response."
            pesaway_inbound = self._is_pesaway_core() and payment_request.operation != PaymentRequest.Operation.PAYOUT
            if self.sandbox or not query_status or not self._supports_status_query() or pesaway_inbound:
                self.fail_processing_request(payment_request, timeout_reason)
                processed += 1
                continue
            try:
                self.query_status(payment_request)
            except LedgerError as exc:
                self.fail_processing_request(
                    payment_request,
                    f"Payment status check failed after {older_than_seconds} seconds: {exc}",
                )
                processed += 1
                continue
            payment_request.refresh_from_db()
            if payment_request.status == PaymentRequest.Status.PROCESSING:
                self.fail_processing_request(payment_request, timeout_reason)
            processed += 1
        return processed

    def _post(self, path, payload):
        if not self.base_url:
            raise LedgerError("PAYMENT_MICROSERVICE_URL is not configured.")
        request_body = json.dumps(payload)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
            if not self._is_pesaway_core():
                headers["Authorization"] = f"Bearer {self.api_key}"
        req = request.Request(
            f"{self.base_url}{path}",
            data=request_body.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        correlation_id = payload.get("external_reference") or payload.get("originator_ref") or payload.get("request_id") or ""
        started_at = time.monotonic()
        logger.info(
            "payment.interface.request.start method=POST url=%s request_payload=%s correlation_id=%s sandbox=%s "
            "timeout_seconds=%s",
            req.full_url,
            request_body,
            correlation_id,
            self.sandbox,
            self.timeout,
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                result = json.loads(raw) if raw else {}
                logger.info(
                    "payment.interface.request.success method=POST path=%s correlation_id=%s http_status=%s provider_status=%s duration_ms=%s",
                    path,
                    correlation_id,
                    getattr(response, "status", None) or getattr(response, "code", None),
                    result.get("status", "") if isinstance(result, dict) else "",
                    round((time.monotonic() - started_at) * 1000),
                )
                return result
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {"raw": raw}
            logger.warning(
                "payment.interface.request.http_error method=POST path=%s correlation_id=%s http_status=%s duration_ms=%s",
                path,
                correlation_id,
                exc.code,
                round((time.monotonic() - started_at) * 1000),
            )
            raise LedgerError(f"Payment microservice returned HTTP {exc.code}: {parsed}") from exc
        except error.URLError as exc:
            logger.warning(
                "payment.interface.request.network_error method=POST path=%s correlation_id=%s reason=%s duration_ms=%s",
                path,
                correlation_id,
                exc.reason,
                round((time.monotonic() - started_at) * 1000),
            )
            raise LedgerError(str(exc.reason)) from exc
        except Exception:
            logger.exception(
                "payment.interface.request.unexpected_error method=POST path=%s correlation_id=%s duration_ms=%s",
                path,
                correlation_id,
                round((time.monotonic() - started_at) * 1000),
            )
            raise

    def _get(self, path):
        if not self.base_url:
            raise LedgerError("PAYMENT_MICROSERVICE_URL is not configured.")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
            if not self._is_pesaway_core():
                headers["Authorization"] = f"Bearer {self.api_key}"
        req = request.Request(f"{self.base_url}{path}", headers=headers, method="GET")
        started_at = time.monotonic()
        logger.info(
            "payment.interface.request.start method=GET url=%s sandbox=%s timeout_seconds=%s",
            req.full_url,
            self.sandbox,
            self.timeout,
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                result = json.loads(raw) if raw else {}
                logger.info(
                    "payment.interface.request.success method=GET path=%s http_status=%s provider_status=%s duration_ms=%s",
                    path,
                    getattr(response, "status", None) or getattr(response, "code", None),
                    result.get("status", "") if isinstance(result, dict) else "",
                    round((time.monotonic() - started_at) * 1000),
                )
                return result
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {"raw": raw}
            logger.warning(
                "payment.interface.request.http_error method=GET path=%s http_status=%s duration_ms=%s",
                path,
                exc.code,
                round((time.monotonic() - started_at) * 1000),
            )
            raise LedgerError(f"Payment microservice returned HTTP {exc.code}: {parsed}") from exc
        except error.URLError as exc:
            logger.warning(
                "payment.interface.request.network_error method=GET path=%s reason=%s duration_ms=%s",
                path,
                exc.reason,
                round((time.monotonic() - started_at) * 1000),
            )
            raise LedgerError(str(exc.reason)) from exc
        except Exception:
            logger.exception(
                "payment.interface.request.unexpected_error method=GET path=%s duration_ms=%s",
                path,
                round((time.monotonic() - started_at) * 1000),
            )
            raise

    def _supports_status_query(self):
        return self._is_pesaway_core() or ("lipasync.com" not in self.base_url and "/core/payments/qb" not in self.base_url)

    def _lipasync_initiate_request(self, tx, operation, extra_payload, *, originator_ref):
        if self._is_pesaway_core():
            return self._pesaway_initiate_request(tx, operation, extra_payload, originator_ref=originator_ref)
        if operation == PaymentRequest.Operation.STK_PUSH:
            return (
                "/stk-push/initiate/",
                {
                    "amount": self._amount_major(tx.amount_minor),
                    "currency": tx.currency,
                    "external_reference": originator_ref,
                    "idempotency_key": originator_ref,
                    "payment_payload": {
                        "daraja_flow": "stk_push",
                        "phone_number": extra_payload.get("phone_number") or "",
                        "account_reference": extra_payload.get("account_reference") or "",
                    },
                },
            )
        if operation == PaymentRequest.Operation.PAYOUT:
            destination = extra_payload.get("destination") or {}
            return (
                "/b2b_paybill/initiate/",
                {
                    "amount": self._amount_major(tx.amount_minor),
                    "currency": tx.currency,
                    "external_reference": originator_ref,
                    "idempotency_key": originator_ref,
                    "payment_payload": {
                        "daraja_flow": "b2b_paybill",
                        "destination_shortcode": self._paybill_shortcode(destination),
                        "account_reference": destination.get("account_reference") or destination.get("account_number") or "",
                    },
                },
            )
        raise LedgerError(f"Lipasync does not support payment operation {operation}.")

    def _pesaway_initiate_request(self, tx, operation, extra_payload, *, originator_ref):
        system_slug = getattr(settings, "PESAWAY_SYSTEM_SLUG", "")
        if not system_slug:
            raise LedgerError("PESAWAY_SYSTEM_SLUG is not configured.")
        if operation == PaymentRequest.Operation.STK_PUSH:
            event_slug = getattr(settings, "PESAWAY_COLLECTION_EVENT_SLUG", "")
            if not event_slug:
                raise LedgerError("PESAWAY_COLLECTION_EVENT_SLUG is not configured.")
            phone_number = str(extra_payload.get("phone_number") or "").strip()
            if not phone_number:
                raise LedgerError("PesaWay collection requires provider_payload.phone_number.")
            return (
                f"/inbound-payments/{system_slug}/{event_slug}/initiate/",
                {
                    "amount": self._amount_major_string(tx.amount_minor),
                    "idempotency_key": originator_ref,
                    "external_reference": originator_ref,
                    "provider_payload": {
                        "phone_number": phone_number,
                        "channel": self._pesaway_mobile_channel(extra_payload.get("channel")),
                        "reason": extra_payload.get("reason") or "Wallet top-up",
                    },
                },
            )
        if operation == PaymentRequest.Operation.PAYOUT:
            event_slug, provider_payload = self._pesaway_outbound_payload(extra_payload.get("destination") or {})
            return (
                f"/outbound-transfers/{system_slug}/{event_slug}/initiate/",
                {
                    "amount": self._amount_major_string(tx.amount_minor),
                    "idempotency_key": originator_ref,
                    "external_reference": originator_ref,
                    "provider_payload": provider_payload,
                },
            )
        raise LedgerError(f"PesaWay does not support payment operation {operation}.")

    def _pesaway_outbound_payload(self, destination):
        destination_type = str(destination.get("type") or "").upper()
        reason = destination.get("reason") or "Ratiba payout"
        if destination.get("phone_number") or destination_type in {"MPESA", "MOBILE", "B2C"}:
            event_slug = getattr(settings, "PESAWAY_B2C_EVENT_SLUG", "")
            if not event_slug:
                raise LedgerError("PESAWAY_B2C_EVENT_SLUG is not configured.")
            phone_number = str(destination.get("phone_number") or "").strip()
            if not phone_number:
                raise LedgerError("PesaWay B2C payout requires provider_payload.phone_number.")
            return event_slug, {
                "phone_number": phone_number,
                "channel": self._pesaway_mobile_channel(destination.get("channel")),
                "reason": reason,
            }
        if destination.get("paybill_number"):
            event_slug = getattr(settings, "PESAWAY_B2B_EVENT_SLUG", "")
            if not event_slug:
                raise LedgerError("PESAWAY_B2B_EVENT_SLUG is not configured.")
            return event_slug, {
                "account_number": str(destination.get("paybill_number") or "").strip(),
                "channel": "MPESA Paybill",
                "reason": reason,
            }
        if destination.get("till_number"):
            event_slug = getattr(settings, "PESAWAY_B2B_EVENT_SLUG", "")
            if not event_slug:
                raise LedgerError("PESAWAY_B2B_EVENT_SLUG is not configured.")
            return event_slug, {
                "account_number": str(destination.get("till_number") or "").strip(),
                "channel": "MPESA Till",
                "reason": reason,
            }
        if destination.get("account_number") and destination.get("bank_name"):
            event_slug = getattr(settings, "PESAWAY_BANK_EVENT_SLUG", "")
            if not event_slug:
                raise LedgerError("PESAWAY_BANK_EVENT_SLUG is not configured.")
            return event_slug, {
                "account_number": str(destination.get("account_number") or "").strip(),
                "bank_name": str(destination.get("bank_name") or "").strip(),
                "reason": reason,
            }
        raise LedgerError("PesaWay payout requires a mobile, paybill, till, or bank destination.")

    def _pesaway_mobile_channel(self, channel):
        channel = str(channel or "MPESA").strip()
        if channel not in {"MPESA", "Airtel"}:
            raise LedgerError("PesaWay mobile channel must be exactly MPESA or Airtel.")
        return channel

    def _normalize_microservice_payload(self, payload, *, payment_request=None):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        status = str(payload.get("status") or data.get("status") or "").upper()
        event = str(payload.get("event") or "").lower()
        normalized = {
            "originator_ref": str(
                payload.get("originator_ref")
                or payload.get("external_reference")
                or data.get("external_reference")
                or data.get("idempotency_key")
                or (payment_request.originator_ref if payment_request else "")
                or ""
            ),
            "request_id": str(
                payload.get("request_id")
                or payload.get("payment_intent_id")
                or payload.get("inbound_payment_id")
                or payload.get("outbound_transfer_id")
                or data.get("payment_intent_id")
                or data.get("inbound_payment_id")
                or data.get("outbound_transfer_id")
                or data.get("request_id")
                or data.get("id")
                or (payment_request.request_id if payment_request else "")
                or ""
            ),
            "transaction_receipt": str(
                payload.get("transaction_receipt")
                or payload.get("transaction_id")
                or payload.get("provider_transaction_id")
                or data.get("transaction_id")
                or data.get("provider_transaction_id")
                or data.get("payment_intent_id")
                or ""
            ),
            "confirmation_key": str(payload.get("confirmation_key") or ""),
            "status": status,
        }
        if status in self.LIPASYNC_SUCCESS_STATUSES or event in {"payment.captured", "payment.settled", "inbound_payment.captured", "inbound_payment.settled", "outbound_transfer.success"}:
            normalized["success"] = True
        elif status in self.LIPASYNC_FAILURE_STATUSES or event in {
            "payment.failed",
            "payment.cancelled",
            "payment.refunded",
            "inbound_payment.failed",
            "outbound_transfer.failed",
        }:
            normalized["success"] = False
            reason = payload.get("failure_reason") or payload.get("message") or payload.get("error")
            normalized["failure_reason"] = str(reason or f"Payment {status.lower() or event.split('.')[-1]}")[:255]
            normalized["error"] = normalized["failure_reason"]
        elif "success" in payload and status not in self.LIPASYNC_PENDING_STATUSES:
            normalized["success"] = bool(payload.get("success"))
        return normalized

    def _amount_major(self, amount_minor):
        return float((Decimal(int(amount_minor)) / Decimal("100")).quantize(Decimal("0.01")))

    def _amount_major_string(self, amount_minor):
        return str((Decimal(int(amount_minor)) / Decimal("100")).quantize(Decimal("0.01")))

    def _is_pesaway_core(self):
        return self.base_url.rstrip("/").endswith("/api/v1/core")

    def _validate_lipasync_payout_destination(self, destination):
        if self._is_pesaway_core():
            self._pesaway_outbound_payload(destination)
            return
        if not self._paybill_shortcode(destination):
            raise LedgerError("Lipasync B2B payout requires destination.paybill_number or destination.destination_shortcode.")

    def _paybill_shortcode(self, destination):
        return str(destination.get("destination_shortcode") or destination.get("paybill_number") or "").strip()


# Kept for integrations that imported the original class name.
PaymentInterface = PaymentService
