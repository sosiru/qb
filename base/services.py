import calendar
import csv
import io
import logging
import random
import secrets
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import authenticate
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from django.template.defaultfilters import slugify
from django.utils import timezone

from api.models import IntegrationApiKey
from audit.models import AuditLog
from eusers.models import AccessToken, LoginOtp, User
from eusers.utils import normalize_phone_number
from notifications.services import queue_email_notification, queue_notifications_for_user
from ledger.models import Account, Transaction as LedgerTransactionRecord
from ledger.services import (
    PaymentInterface,
    complete_pay_in,
    complete_payout,
    get_or_create_organization_account,
    get_or_create_user_account,
    initiate_pay_in,
    initiate_payout,
    transfer_between_accounts,
    unique_transaction_reference,
)

from .models import (
    BankDirectory,
    Organization,
    OrganizationInvite,
    OrganizationMembership,
    ExpenseCategory,
    OutboxEvent,
    Payee,
    PayeePreset,
    PaymentBatch,
    PaymentInstruction,
    PaymentSchedule,
    ReconciliationException,
    TransactionEvent,
)
from .utils import TransactionRefGenerator

SERVICE_FEE_BPS = 200
logger = logging.getLogger(__name__)
DEFAULT_TEST_OTP_PHONE = "254710956633"
DEFAULT_TEST_OTP_CODE = "123456"
LOGIN_OTP_TTL_MINUTES = 10
LOGIN_OTP_RETRY_AFTER_SECONDS = 60
TRANSACTION_REF_GENERATOR = TransactionRefGenerator(prefix="QB")

PAYMENT_BATCH_TRANSITIONS = {
    PaymentBatch.Status.DRAFT: {PaymentBatch.Status.PENDING_APPROVAL},
    PaymentBatch.Status.PENDING_APPROVAL: {PaymentBatch.Status.APPROVED, PaymentBatch.Status.REJECTED, PaymentBatch.Status.FAILED},
    PaymentBatch.Status.APPROVED: {PaymentBatch.Status.PROCESSING, PaymentBatch.Status.SUCCEEDED, PaymentBatch.Status.FAILED},
    PaymentBatch.Status.PROCESSING: {PaymentBatch.Status.SUCCEEDED, PaymentBatch.Status.PARTIAL, PaymentBatch.Status.FAILED},
    PaymentBatch.Status.PARTIAL: set(),
    PaymentBatch.Status.SUCCEEDED: set(),
    PaymentBatch.Status.FAILED: set(),
    PaymentBatch.Status.REJECTED: set(),
}
PAYMENT_INSTRUCTION_TRANSITIONS = {
    PaymentInstruction.Status.PENDING: {PaymentInstruction.Status.SUCCEEDED, PaymentInstruction.Status.FAILED},
    PaymentInstruction.Status.SUCCEEDED: set(),
    PaymentInstruction.Status.FAILED: set(),
}


class DomainError(Exception):
    pass


class InsufficientFundsError(DomainError):
    def __init__(self, wallet, amount_minor, available_balance_minor):
        super().__init__(f"Insufficient funds for wallet {wallet.id}.")
        self.wallet = wallet
        self.amount_minor = amount_minor
        self.available_balance_minor = available_balance_minor


def generate_transaction_reference():
    return unique_transaction_reference("QB")


def record_transaction_event(aggregate_type, aggregate_id, event_type, *, actor=None, from_status="", to_status="", payload=None, microservice_request_id=""):
    return TransactionEvent.objects.create(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        from_status=from_status or "",
        to_status=to_status or "",
        actor=actor,
        microservice_request_id=microservice_request_id or "",
        payload=payload or {},
    )


def transition_payment_batch(batch, to_status, *, actor=None, event_type=None, payload=None, update_fields=None):
    from_status = batch.status
    if from_status == to_status:
        return batch
    if to_status not in PAYMENT_BATCH_TRANSITIONS.get(from_status, set()):
        AuditLog.objects.create(
            actor=actor,
            action="payment_batch.invalid_transition",
            target_type="payment_batch",
            target_id=batch.id,
            metadata={"from_status": from_status, "to_status": to_status, "payload": payload or {}},
        )
        raise ValidationError(f"Invalid payment batch transition {from_status} -> {to_status}.")
    batch.status = to_status
    fields = ["status", "updated_at"]
    if update_fields:
        fields.extend(field for field in update_fields if field not in fields)
    batch.save(update_fields=fields)
    record_transaction_event(
        "payment_batch",
        batch.id,
        event_type or f"payment_batch.{to_status.lower()}",
        actor=actor,
        from_status=from_status,
        to_status=to_status,
        payload=payload,
    )
    return batch


def transition_payment_instruction(instruction, to_status, *, actor=None, event_type=None, payload=None, microservice_request_id=""):
    from_status = instruction.status
    if from_status == to_status:
        return instruction
    if to_status not in PAYMENT_INSTRUCTION_TRANSITIONS.get(from_status, set()):
        AuditLog.objects.create(
            actor=actor,
            action="payment_instruction.invalid_transition",
            target_type="payment_instruction",
            target_id=instruction.id,
            metadata={"from_status": from_status, "to_status": to_status, "payload": payload or {}},
        )
        raise ValidationError(f"Invalid payment instruction transition {from_status} -> {to_status}.")
    instruction.status = to_status
    instruction.save(update_fields=["status", "microservice_request_id", "microservice_response", "failure_reason", "updated_at"])
    record_transaction_event(
        "payment_instruction",
        instruction.id,
        event_type or f"payment_instruction.{to_status.lower()}",
        actor=actor,
        from_status=from_status,
        to_status=to_status,
        payload=payload,
        microservice_request_id=microservice_request_id,
    )
    return instruction


def ledger_description(entry_type, metadata=None):
    metadata = metadata or {}
    if metadata.get("description"):
        return metadata["description"]
    descriptions = {
        "TOP_UP": "Top up of funds",
        "TRANSFER_TO_VAULT": "Funds moved to vault",
        "TRANSFER_FROM_VAULT": "Funds received from vault transfer",
        "DISBURSEMENT": "Withdrawal of funds",
        "WITHDRAWAL": "Withdrawal of funds",
        "ADJUSTMENT": "Wallet adjustment",
    }
    return descriptions.get(entry_type, "Wallet transaction")


def _wallet_account_code(wallet):
    if wallet.organization_id:
        owner = f"ORG:{wallet.organization_id}"
    else:
        owner = f"USER:{wallet.user_id}"
    return f"WALLET:{owner}:{wallet.wallet_type}"


def _wallet_movement_ref(wallet):
    return None


def _wallet_idempotency_key(prefix, reference):
    return f"{prefix}:{reference}"


class PermissionDeniedError(DomainError):
    pass


class ValidationError(DomainError):
    pass


class OtpRequired(DomainError):
    def __init__(self, message, phone_number=None, dev_otp=None, expires_in_seconds=None, retry_after_seconds=None):
        super().__init__(message)
        self.phone_number = phone_number
        self.dev_otp = dev_otp
        self.expires_in_seconds = expires_in_seconds
        self.retry_after_seconds = retry_after_seconds


def can_manage_all_organizations(user):
    return user.account_type in {User.AccountType.SUPERADMIN, User.AccountType.SERVICE_PROVIDER}


def can_access_individual_features(user):
    return user.account_type in {User.AccountType.INDIVIDUAL, User.AccountType.SUPERADMIN}


def can_access_corporate_features(user):
    return user.account_type in {User.AccountType.CORPORATE, User.AccountType.SUPERADMIN, User.AccountType.SERVICE_PROVIDER}


def _parse_date(value):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValidationError("Dates must use ISO format YYYY-MM-DD.") from exc


def _clamp_day_for_month(year, month, day):
    return min(day, calendar.monthrange(year, month)[1])


def _compute_initial_due_date(day_of_month, base_date=None):
    base_date = base_date or timezone.localdate()
    year = base_date.year
    month = base_date.month
    due_day = _clamp_day_for_month(year, month, day_of_month)
    due_date = base_date.replace(day=due_day)
    if due_date < base_date:
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        due_day = _clamp_day_for_month(year, month, day_of_month)
        due_date = due_date.replace(year=year, month=month, day=due_day)
    return due_date


def _add_months(source_date, months, day_of_month):
    month_index = (source_date.month - 1) + months
    year = source_date.year + month_index // 12
    month = month_index % 12 + 1
    day = _clamp_day_for_month(year, month, day_of_month)
    return source_date.replace(year=year, month=month, day=day)


def _start_of_month(target_date):
    return target_date.replace(day=1)


def _end_of_month(target_date):
    return target_date.replace(day=calendar.monthrange(target_date.year, target_date.month)[1])


def _format_schedule_cadence(interval_months):
    if interval_months == 1:
        return "Every month"
    return f"Every {interval_months} months"


def _instruction_queryset_for_user(user, organization_id=None):
    queryset = PaymentInstruction.objects.select_related("batch", "payee").order_by("-created_at")
    if organization_id:
        organization = get_organization_for_user(user, organization_id)
        return queryset.filter(batch__organization=organization)
    if can_manage_all_organizations(user):
        return queryset
    if user.account_type == User.AccountType.CORPORATE:
        memberships = OrganizationMembership.objects.filter(user=user, is_active=True).values_list("organization_id", flat=True)
        return queryset.filter(batch__organization_id__in=memberships)
    return queryset.filter(batch__user=user)


def _serialize_activity_instruction(instruction):
    destination = instruction.destination or {}
    sub_parts = []
    if instruction.recipient_type == Payee.PayeeType.PAYBILL and destination.get("paybill_number"):
        sub_parts.append(f"Paybill {destination['paybill_number']}")
    elif instruction.recipient_type == Payee.PayeeType.TILL and destination.get("till_number"):
        sub_parts.append(f"Till {destination['till_number']}")
    elif instruction.recipient_type == Payee.PayeeType.MOBILE and destination.get("phone_number"):
        sub_parts.append(destination["phone_number"])
    elif instruction.recipient_type == Payee.PayeeType.BANK and destination.get("bank_name"):
        sub_parts.append(destination["bank_name"])
    if instruction.external_reference:
        sub_parts.append(instruction.external_reference)
    return {
        "instruction_id": str(instruction.id),
        "batch_id": str(instruction.batch_id),
        "recipient_name": instruction.recipient_name,
        "recipient_type": instruction.recipient_type,
        "category": instruction.category,
        "base_amount_minor": instruction.amount_minor,
        "fee_amount_minor": instruction.fee_amount_minor,
        "gross_amount_minor": instruction.amount_minor + instruction.fee_amount_minor,
        "status": instruction.status,
        "reference": instruction.microservice_request_id or instruction.external_reference or str(instruction.id),
        "description": instruction.recipient_name,
        "subtext": " · ".join(part for part in sub_parts if part),
        "created_at": instruction.created_at.isoformat(),
    }


def payment_microservice_dispatch_enabled():
    return bool(getattr(settings, "PAYMENT_MICROSERVICE_URL", ""))


def should_simulate_payment_collection(payment_mode, payload=None):
    payload = payload or {}
    if "simulate_collection" in payload:
        return bool(payload.get("simulate_collection"))
    return payment_mode != PaymentBatch.PaymentMode.STK


def should_simulate_wallet_topup(payload=None):
    payload = payload or {}
    if "simulate_collection" in payload:
        return bool(payload.get("simulate_collection"))
    if "simulate" in payload:
        return bool(payload.get("simulate"))
    if not getattr(settings, "PAYMENT_MICROSERVICE_URL", ""):
        return True
    return not payment_microservice_dispatch_enabled()


def payment_stk_phone_number(phone_number):
    override = getattr(settings, "PAYMENT_MICROSERVICE_STK_PHONE_OVERRIDE", "")
    if override:
        normalized_override = normalize_phone_number(override)
        logger.warning(
            "payment_microservice.stk.phone_override.active original_phone=%s override_phone=%s",
            phone_number,
            normalized_override,
        )
        return normalized_override
    return normalize_phone_number(phone_number)


def dispatch_outbox_event_inline(event):
    if not getattr(settings, "PAYMENT_MICROSERVICE_INLINE_DISPATCH", False):
        logger.info(
            "outbox.inline_dispatch.skipped event_id=%s topic=%s aggregate_type=%s aggregate_id=%s",
            event.id,
            event.topic,
            event.aggregate_type,
            event.aggregate_id,
        )
        return False
    logger.info(
        "outbox.inline_dispatch.start event_id=%s topic=%s aggregate_type=%s aggregate_id=%s payload=%s",
        event.id,
        event.topic,
        event.aggregate_type,
        event.aggregate_id,
        event.payload,
    )
    try:
        from base.payment_microservice_executor import fail_instruction_event, process_outbox_event

        event.status = OutboxEvent.Status.PROCESSING
        event.attempts += 1
        event.save(update_fields=["status", "attempts", "updated_at"])
        process_outbox_event(event)
        event.status = OutboxEvent.Status.DONE
        event.last_error = ""
        event.save(update_fields=["status", "last_error", "updated_at"])
        logger.info("outbox.inline_dispatch.success event_id=%s topic=%s", event.id, event.topic)
        return True
    except Exception as exc:
        event.status = OutboxEvent.Status.FAILED
        event.last_error = str(exc)[:255]
        event.save(update_fields=["status", "last_error", "updated_at"])
        logger.exception("outbox.inline_dispatch.failed event_id=%s topic=%s error=%s", event.id, event.topic, exc)
        fail_instruction_event(event, exc)
        return False


def amount_minor_to_payment_amount(amount_minor):
    return str((Decimal(amount_minor) / Decimal("100")).quantize(Decimal("0.01")))


def build_microservice_request_id(prefix, entity_id):
    return f"{prefix}-{str(entity_id).split('-')[0]}-{uuid.uuid4().hex[:8]}"


def ensure_wallet_balance(wallet):
    return wallet


def _apply_wallet_balance_delta(wallet, *, current_delta=0, uncleared_delta=0, reserved_delta=0, available_delta=0):
    raise ValidationError("Direct wallet balance deltas are no longer supported. Use ledger execution profiles.")


def ensure_user_wallets(user):
    primary = get_or_create_user_account(
        user,
        account_kind=Account.AccountKind.PRIMARY,
    )
    vault = None
    if can_access_individual_features(user):
        vault = get_or_create_user_account(
            user,
            account_kind=Account.AccountKind.VAULT,
        )
    return primary, vault


def ensure_organization_wallet(organization):
    return get_or_create_organization_account(organization)


@transaction.atomic
def place_wallet_hold(wallet, amount_minor, *, reason, reference, expires_at=None, metadata=None):
    if amount_minor <= 0:
        raise ValidationError("amount_minor must be greater than 0.")
    if wallet.available_balance_minor < amount_minor:
        raise InsufficientFundsError(wallet, amount_minor, wallet.available_balance_minor)
    return initiate_payout(
        wallet,
        amount_minor=amount_minor,
        reference=reference,
        idempotency_key=_wallet_idempotency_key("reserve", reference),
        description=reason,
        metadata={**(metadata or {}), "reason": reason, "expires_at": expires_at.isoformat() if expires_at else None},
    )


@transaction.atomic
def release_wallet_hold(hold_id):
    hold = LedgerTransactionRecord.objects.select_related("account").get(id=hold_id)
    from ledger.services import fail_transaction

    return fail_transaction(hold, reason="Reserve released")


@transaction.atomic
def post_uncleared_wallet_entry(wallet, amount_minor, *, entry_type, reference, metadata=None):
    return initiate_pay_in(
        wallet,
        amount_minor=amount_minor,
        reference=reference,
        idempotency_key=_wallet_idempotency_key("uncleared", reference),
        description=ledger_description(entry_type, metadata),
        metadata={**(metadata or {}), "entry_type": entry_type},
    )


@transaction.atomic
def mark_wallet_entry_cleared(entry_id):
    entry = LedgerTransactionRecord.objects.select_related("account").get(id=entry_id)
    return complete_pay_in(entry)


def issue_token(user):
    logger.info("auth.token.issue.start user_id=%s phone=%s", user.id, user.phone_number)
    _, raw_token = AccessToken.issue(user)
    logger.info("auth.token.issue.success user_id=%s phone=%s", user.id, user.phone_number)
    return raw_token


def _generate_login_otp(phone_number):
    if phone_number == DEFAULT_TEST_OTP_PHONE:
        return DEFAULT_TEST_OTP_CODE
    return f"{random.SystemRandom().randint(0, 99999):05d}"


def _is_default_test_otp(phone_number, code):
    normalized_code = str(code or "").strip()
    return phone_number == DEFAULT_TEST_OTP_PHONE and normalized_code.isdigit() and len(normalized_code) == 6


def _create_login_otp(user):
    code = _generate_login_otp(user.phone_number)
    LoginOtp.objects.filter(
        user=user,
        purpose=LoginOtp.Purpose.LOGIN,
        consumed_at__isnull=True,
    ).update(consumed_at=timezone.now())
    LoginOtp.objects.create(
        user=user,
        purpose=LoginOtp.Purpose.LOGIN,
        code_hash=LoginOtp.hash_code(code),
        expires_at=timezone.now() + timedelta(minutes=LOGIN_OTP_TTL_MINUTES),
    )
    logger.info("auth.otp.created user_id=%s phone=%s purpose=LOGIN", user.id, user.phone_number)
    if settings.DEBUG:
        logger.info("auth.otp.dev_code user_id=%s phone=%s otp=%s", user.id, user.phone_number, code)
    return code


def _verify_login_otp(user, code):
    normalized_code = str(code or "").strip()
    if _is_default_test_otp(user.phone_number, normalized_code):
        logger.info("auth.otp.verify.default_test_override user_id=%s phone=%s", user.id, user.phone_number)
        return
    otp = (
        LoginOtp.objects.filter(
            user=user,
            purpose=LoginOtp.Purpose.LOGIN,
            consumed_at__isnull=True,
        )
        .order_by("-created_at")
        .first()
    )
    if not otp or not otp.is_active():
        logger.warning("auth.otp.verify.expired_or_missing user_id=%s phone=%s", user.id, user.phone_number)
        raise ValidationError("OTP expired or missing. Request a new OTP.")

    otp.attempts += 1
    if otp.code_hash != LoginOtp.hash_code(normalized_code):
        otp.save(update_fields=["attempts", "updated_at"])
        logger.warning(
            "auth.otp.verify.failed user_id=%s phone=%s attempts=%s",
            user.id,
            user.phone_number,
            otp.attempts,
        )
        raise ValidationError("Invalid OTP.")

    otp.consumed_at = timezone.now()
    otp.save(update_fields=["attempts", "consumed_at", "updated_at"])
    logger.info("auth.otp.verify.success user_id=%s phone=%s", user.id, user.phone_number)


def issue_integration_api_key(user, payload):
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValidationError("API key name is required.")

    organization = None
    if payload.get("organization_id"):
        organization = get_organization_for_user(
            user,
            payload["organization_id"],
            allowed_roles=[OrganizationMembership.Role.ADMIN],
        )

    target_user = user
    scopes = payload.get("scopes") or ["read", "write"]
    if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
        raise ValidationError("scopes must be a list of strings.")

    api_key, raw_key = IntegrationApiKey.issue(
        user=target_user,
        name=name,
        organization=organization,
        created_by=user,
        scopes=scopes,
        ttl_days=int(payload.get("ttl_days") or 365),
    )
    AuditLog.objects.create(
        actor=user,
        action="integration_api_key.created",
        target_type="integration_api_key",
        target_id=api_key.id,
        metadata={
            "name": api_key.name,
            "organization_id": str(organization.id) if organization else None,
            "scopes": scopes,
        },
    )
    return api_key, raw_key


def list_integration_api_keys(user, organization_id=None):
    queryset = IntegrationApiKey.objects.select_related("user", "organization", "created_by").order_by("-created_at")
    if can_manage_all_organizations(user) and not organization_id:
        return queryset
    if organization_id:
        organization = get_organization_for_user(user, organization_id, allowed_roles=[OrganizationMembership.Role.ADMIN])
        return queryset.filter(organization=organization)
    return queryset.filter(user=user)


def revoke_integration_api_key(user, api_key_id):
    api_key = get_object_or_404(IntegrationApiKey.objects.select_related("organization", "user"), id=api_key_id)
    if api_key.organization_id:
        get_organization_for_user(user, api_key.organization_id, allowed_roles=[OrganizationMembership.Role.ADMIN])
    elif api_key.user_id != user.id and not can_manage_all_organizations(user):
        raise PermissionDeniedError("You do not have permission to revoke this API key.")
    api_key.is_active = False
    api_key.revoked_at = timezone.now()
    api_key.save(update_fields=["is_active", "revoked_at", "updated_at"])
    AuditLog.objects.create(
        actor=user,
        action="integration_api_key.revoked",
        target_type="integration_api_key",
        target_id=api_key.id,
        metadata={"name": api_key.name, "organization_id": str(api_key.organization_id) if api_key.organization_id else None},
    )
    return api_key


@transaction.atomic
def register_user(payload):
    logger.info("auth.register.start account_type=%s", payload.get("account_type"))
    phone_number = normalize_phone_number(payload.get("phone_number"))
    password = payload.get("password") or ""
    full_name = (payload.get("full_name") or "").strip()
    account_type = payload.get("account_type")

    if not phone_number or not password or not full_name or not account_type:
        raise ValidationError("phone_number, password, full_name, and account_type are required.")
    if account_type not in User.AccountType.values:
        raise ValidationError("account_type must be INDIVIDUAL, CORPORATE, SERVICE_PROVIDER, or SUPERADMIN.")

    user = User.objects.create_user(
        phone_number=phone_number,
        password=password,
        full_name=full_name,
        email=(payload.get("email") or "").strip(),
        account_type=account_type,
        default_payment_mode=payload.get("default_payment_mode", User.PaymentMode.WALLET),
    )
    logger.info("auth.register.user_created user_id=%s phone=%s account_type=%s", user.id, user.phone_number, user.account_type)
    ensure_user_wallets(user)
    logger.info("auth.register.wallets_ready user_id=%s phone=%s", user.id, user.phone_number)

    if can_access_corporate_features(user) and payload.get("organization_name"):
        create_organization(
            user,
            {
                "name": payload["organization_name"],
                "registration_number": payload.get("registration_number", ""),
                "tax_identification_document": payload.get("tax_identification_document"),
                "business_registration_certificate": payload.get("business_registration_certificate"),
                "kyc_status": payload.get("kyc_status", Organization.KycStatus.PENDING),
                "role": payload.get("organization_role", OrganizationMembership.Role.ADMIN),
            },
        )

    token = issue_token(user)
    queue_notifications_for_user(
        user,
        "SELF_ONBOARDING",
        {
            "user_name": user.full_name,
            "phone_number": user.phone_number,
            "account_type": user.account_type,
            "cta_url": getattr(settings, "FRONTEND_BASE_URL", "http://localhost:4200"),
        },
        scheduled_for=timezone.now(),
    )
    logger.info("auth.register.success user_id=%s phone=%s", user.id, user.phone_number)
    return user, token


def login_user(payload):
    phone_number = normalize_phone_number(payload.get("phone_number"))
    password = payload.get("password")
    otp = payload.get("otp")
    logger.info("auth.login.start phone=%s otp_present=%s", phone_number, bool(otp))
    if not phone_number or not password:
        raise ValidationError("phone_number and password are required.")
    user = authenticate(phone_number=phone_number, password=password)
    if not user:
        logger.warning("auth.login.invalid_credentials phone=%s", phone_number)
        raise ValidationError("Invalid credentials.")
    logger.info("auth.login.password_verified user_id=%s phone=%s", user.id, user.phone_number)

    if not otp:
        dev_otp = _create_login_otp(user)
        queue_notifications_for_user(
            user,
            "LOGIN_OTP",
            {
                "user_name": user.full_name,
                "phone_number": user.phone_number,
                "otp": dev_otp,
                "expires_in": f"{LOGIN_OTP_TTL_MINUTES} minutes",
                "cta_url": getattr(settings, "FRONTEND_BASE_URL", "http://localhost:4200"),
            },
            scheduled_for=timezone.now(),
        )
        raise OtpRequired(
            "OTP required. Enter the code sent to your phone.",
            phone_number=user.phone_number,
            dev_otp=dev_otp if settings.DEBUG or user.phone_number == DEFAULT_TEST_OTP_PHONE else None,
            expires_in_seconds=LOGIN_OTP_TTL_MINUTES * 60,
            retry_after_seconds=LOGIN_OTP_RETRY_AFTER_SECONDS,
        )

    _verify_login_otp(user, otp)
    token = issue_token(user)
    ensure_user_wallets(user)
    queue_notifications_for_user(
        user,
        "LOGIN_SUCCESS",
        {
            "user_name": user.full_name,
            "phone_number": user.phone_number,
            "login_time": timezone.localtime(timezone.now()).strftime("%d %b %Y, %I:%M %p"),
            "cta_url": getattr(settings, "FRONTEND_BASE_URL", "http://localhost:4200"),
        },
        scheduled_for=timezone.now(),
    )
    logger.info("auth.login.success user_id=%s phone=%s", user.id, user.phone_number)
    return user, token


def update_user_profile(user, payload):
    update_fields = []

    if "full_name" in payload:
        full_name = (payload.get("full_name") or "").strip()
        if not full_name:
            raise ValidationError("full_name cannot be blank.")
        user.full_name = full_name
        update_fields.append("full_name")

    if "email" in payload:
        user.email = (payload.get("email") or "").strip()
        update_fields.append("email")

    if "phone_number" in payload:
        phone_number = normalize_phone_number(payload.get("phone_number"))
        if not phone_number:
            raise ValidationError("phone_number cannot be blank.")
        user.phone_number = phone_number
        update_fields.append("phone_number")

    if "default_payment_mode" in payload:
        payment_mode = payload.get("default_payment_mode")
        if payment_mode not in User.PaymentMode.values:
            raise ValidationError("Invalid default_payment_mode.")
        user.default_payment_mode = payment_mode
        update_fields.append("default_payment_mode")

    for field_name in [
        "sms_notifications_enabled",
        "email_notifications_enabled",
        "push_notifications_enabled",
        "mfa_enabled",
        "payouts_require_owner_approval",
    ]:
        if field_name in payload:
            setattr(user, field_name, bool(payload.get(field_name)))
            update_fields.append(field_name)

    if "mpesa_withdrawal_phone" in payload:
        phone_number = normalize_phone_number(payload.get("mpesa_withdrawal_phone"))
        user.mpesa_withdrawal_phone = phone_number or ""
        update_fields.append("mpesa_withdrawal_phone")

    if not update_fields:
        return user

    user.save(update_fields=update_fields + ["updated_at"])
    AuditLog.objects.create(
        actor=user,
        action="user.profile_updated",
        target_type="user",
        target_id=user.id,
        metadata={"fields": update_fields},
    )
    return user


def change_user_password(user, payload):
    current_password = payload.get("current_password") or ""
    new_password = payload.get("new_password") or ""

    if not current_password or not new_password:
        raise ValidationError("current_password and new_password are required.")
    if not user.check_password(current_password):
        raise ValidationError("Current password is invalid.")
    if len(new_password) < 8:
        raise ValidationError("new_password must be at least 8 characters.")

    user.set_password(new_password)
    user.save(update_fields=["password", "updated_at"])
    AuditLog.objects.create(actor=user, action="user.password_changed", target_type="user", target_id=user.id)
    return user


def list_organizations(user, filters=None):
    filters = filters or {}
    queryset = Organization.objects.all().order_by("name")
    if user.account_type == User.AccountType.CORPORATE:
        memberships = OrganizationMembership.objects.filter(user=user, is_active=True).values_list("organization_id", flat=True)
        queryset = queryset.filter(id__in=memberships)
    elif user.account_type not in {
        User.AccountType.CORPORATE,
        User.AccountType.SUPERADMIN,
        User.AccountType.SERVICE_PROVIDER,
    }:
        return Organization.objects.none()

    if filters.get("q"):
        queryset = queryset.filter(name__icontains=filters["q"].strip())
    if filters.get("kyc_status") in Organization.KycStatus.values:
        queryset = queryset.filter(kyc_status=filters["kyc_status"])
    return queryset


def create_organization(user, payload):
    if not can_access_corporate_features(user):
        raise PermissionDeniedError("Only corporate or superadmin users can create organizations.")

    name = (payload.get("name") or "").strip()
    if not name:
        raise ValidationError("Organization name is required.")

    base_slug = slugify(name)
    slug = base_slug
    counter = 1
    while Organization.objects.filter(slug=slug).exists():
        counter += 1
        slug = f"{base_slug}-{counter}"

    role = payload.get("role", OrganizationMembership.Role.ADMIN)
    if role not in OrganizationMembership.Role.values:
        raise ValidationError("Invalid organization role.")

    organization = Organization.objects.create(
        name=name,
        slug=slug,
        registration_number=(payload.get("registration_number") or "").strip(),
        tax_identification_document=payload.get("tax_identification_document"),
        business_registration_certificate=payload.get("business_registration_certificate"),
        kyc_status=payload.get("kyc_status", Organization.KycStatus.PENDING),
    )
    OrganizationMembership.objects.create(user=user, organization=organization, role=role)
    ensure_organization_wallet(organization)
    AuditLog.objects.create(
        actor=user,
        action="organization.created",
        target_type="organization",
        target_id=organization.id,
        metadata={"organization_name": organization.name, "role": role},
    )
    return organization


def update_organization(actor, organization_id, payload):
    organization = get_organization_for_user(actor, organization_id, allowed_roles=[OrganizationMembership.Role.ADMIN])
    update_fields = []

    if "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValidationError("Organization name cannot be blank.")
        if name != organization.name:
            base_slug = slugify(name)
            slug = base_slug
            counter = 1
            while Organization.objects.exclude(id=organization.id).filter(slug=slug).exists():
                counter += 1
                slug = f"{base_slug}-{counter}"
            organization.name = name
            organization.slug = slug
            update_fields.extend(["name", "slug"])

    if "default_currency" in payload:
        currency = (payload.get("default_currency") or "").strip().upper()
        if len(currency) != 3:
            raise ValidationError("default_currency must be a 3-letter ISO code.")
        organization.default_currency = currency
        update_fields.append("default_currency")

    if "registration_number" in payload:
        organization.registration_number = (payload.get("registration_number") or "").strip()
        update_fields.append("registration_number")

    for field_name in ["push_notifications_enabled", "sms_notifications_enabled"]:
        if field_name in payload:
            setattr(organization, field_name, bool(payload.get(field_name)))
            update_fields.append(field_name)

    if "kyc_status" in payload:
        if actor.account_type != User.AccountType.SUPERADMIN:
            raise PermissionDeniedError("Only superadmin users can update kyc_status.")
        kyc_status = payload.get("kyc_status")
        if kyc_status not in Organization.KycStatus.values:
            raise ValidationError("Invalid kyc_status.")
        organization.kyc_status = kyc_status
        update_fields.append("kyc_status")

    if not update_fields:
        return organization

    organization.save(update_fields=update_fields + ["updated_at"])
    AuditLog.objects.create(
        actor=actor,
        action="organization.updated",
        target_type="organization",
        target_id=organization.id,
        metadata={"fields": update_fields, "organization_name": organization.name},
    )
    return organization


def add_organization_member(actor, organization_id, payload):
    organization = get_organization_for_user(actor, organization_id, allowed_roles=[OrganizationMembership.Role.ADMIN])
    member_user_id = payload.get("user_id")
    role = payload.get("role")
    if not member_user_id or role not in OrganizationMembership.Role.values:
        raise ValidationError("user_id and a valid role are required.")
    member = get_object_or_404(User, id=member_user_id)
    membership, created = OrganizationMembership.objects.update_or_create(
        organization=organization,
        user=member,
        defaults={"role": role, "is_active": True},
    )
    AuditLog.objects.create(
        actor=actor,
        action="organization.member_upserted",
        target_type="membership",
        target_id=membership.id,
        metadata={
            "created": created,
            "role": role,
            "member_name": member.full_name,
            "user_id": str(member.id),
            "organization_id": str(organization.id),
            "organization_name": organization.name,
        },
    )
    return membership


def invite_organization_member(actor, organization_id, payload):
    organization = get_organization_for_user(actor, organization_id, allowed_roles=[OrganizationMembership.Role.ADMIN])
    email = (payload.get("email") or "").strip().lower()
    role = payload.get("role")
    if not email or "@" not in email or role not in OrganizationMembership.Role.values:
        raise ValidationError("email and a valid role are required.")

    token = secrets.token_urlsafe(32)
    invite, created = OrganizationInvite.objects.update_or_create(
        organization=organization,
        email=email,
        status=OrganizationInvite.Status.PENDING,
        defaults={
            "role": role,
            "token": token,
            "invited_by": actor,
            "expires_at": timezone.now() + timedelta(days=7),
        },
    )
    frontend_base_url = (payload.get("frontend_base_url") or getattr(settings, "FRONTEND_BASE_URL", "http://localhost:4200")).rstrip("/")
    invite_link = f"{frontend_base_url}/accept-invite?token={invite.token}"
    queue_email_notification(
        [invite.email],
        "ORGANIZATION_INVITE",
        {
            "email": invite.email,
            "role": invite.role,
            "organization_id": str(organization.id),
            "organization_name": organization.name,
            "invite_link": invite_link,
            "invited_by": actor.full_name,
        },
        scheduled_for=timezone.now(),
    )
    AuditLog.objects.create(
        actor=actor,
        action="organization.member_invited",
        target_type="organization_invite",
        target_id=invite.id,
        metadata={
            "created": created,
            "email": email,
            "role": role,
            "organization_id": str(organization.id),
            "organization_name": organization.name,
        },
    )
    return invite, invite_link


@transaction.atomic
def accept_organization_invite(payload):
    token = (payload.get("token") or "").strip()
    if not token:
        raise ValidationError("token is required.")
    invite = get_object_or_404(
        OrganizationInvite.objects.select_related("organization"),
        token=token,
        status=OrganizationInvite.Status.PENDING,
    )
    if invite.expires_at < timezone.now():
        invite.status = OrganizationInvite.Status.REVOKED
        invite.save(update_fields=["status", "updated_at"])
        raise ValidationError("Invite has expired.")

    phone_number = normalize_phone_number(payload.get("phone_number"))
    password = payload.get("password") or ""
    full_name = (payload.get("full_name") or "").strip()
    if not phone_number or not password or not full_name:
        raise ValidationError("phone_number, password, and full_name are required.")

    existing_user = User.objects.filter(phone_number=phone_number).first()
    if existing_user:
        user = existing_user
        user.email = invite.email
        user.full_name = full_name
        if not user.account_type == User.AccountType.CORPORATE:
            user.account_type = User.AccountType.CORPORATE
        user.set_password(password)
        user.save(update_fields=["email", "full_name", "account_type", "password", "updated_at"])
        token_value = issue_token(user)
    else:
        user, token_value = register_user(
            {
                "phone_number": phone_number,
                "password": password,
                "full_name": full_name,
                "email": invite.email,
                "account_type": User.AccountType.CORPORATE,
            }
        )

    membership, _ = OrganizationMembership.objects.update_or_create(
        organization=invite.organization,
        user=user,
        defaults={"role": invite.role, "is_active": True},
    )
    invite.status = OrganizationInvite.Status.ACCEPTED
    invite.accepted_at = timezone.now()
    invite.save(update_fields=["status", "accepted_at", "updated_at"])
    AuditLog.objects.create(
        actor=user,
        action="organization.invite_accepted",
        target_type="organization_invite",
        target_id=invite.id,
        metadata={
            "organization_id": str(invite.organization_id),
            "organization_name": invite.organization.name,
            "membership_id": str(membership.id),
            "role": membership.role,
        },
    )
    return user, token_value, membership


def list_organization_members(actor, organization_id, filters=None):
    organization = get_organization_for_user(actor, organization_id, allowed_roles=[OrganizationMembership.Role.ADMIN])
    filters = filters or {}
    queryset = (
        OrganizationMembership.objects.select_related("user", "organization")
        .filter(organization=organization)
        .order_by("user__full_name", "created_at")
    )
    if filters.get("role") in OrganizationMembership.Role.values:
        queryset = queryset.filter(role=filters["role"])
    if filters.get("is_active") is not None:
        queryset = queryset.filter(is_active=bool(filters["is_active"]))
    if filters.get("q"):
        term = filters["q"].strip()
        queryset = queryset.filter(Q(user__full_name__icontains=term) | Q(user__phone_number__icontains=term) | Q(user__email__icontains=term))
    return queryset


def update_organization_member(actor, organization_id, membership_id, payload):
    organization = get_organization_for_user(actor, organization_id, allowed_roles=[OrganizationMembership.Role.ADMIN])
    membership = get_object_or_404(
        OrganizationMembership.objects.select_related("user", "organization"),
        id=membership_id,
        organization=organization,
    )
    update_fields = []
    if "role" in payload:
        role = payload.get("role")
        if role not in OrganizationMembership.Role.values:
            raise ValidationError("Invalid membership role.")
        membership.role = role
        update_fields.append("role")
    if "is_active" in payload:
        membership.is_active = bool(payload.get("is_active"))
        update_fields.append("is_active")
    if not update_fields:
        return membership
    membership.save(update_fields=update_fields + ["updated_at"])
    AuditLog.objects.create(
        actor=actor,
        action="organization.member_updated",
        target_type="membership",
        target_id=membership.id,
        metadata={
            "fields": update_fields,
            "member_name": membership.user.full_name,
            "user_id": str(membership.user_id),
            "organization_id": str(organization.id),
            "organization_name": organization.name,
        },
    )
    return membership


def deactivate_organization_member(actor, organization_id, membership_id):
    membership = update_organization_member(actor, organization_id, membership_id, {"is_active": False})
    AuditLog.objects.create(
        actor=actor,
        action="organization.member_removed",
        target_type="membership",
        target_id=membership.id,
        metadata={"member_name": membership.user.full_name, "user_id": str(membership.user_id)},
    )
    return membership


def get_organization_for_user(user, organization_id, allowed_roles=None):
    if can_manage_all_organizations(user):
        organization = Organization.objects.filter(id=organization_id).first()
        if not organization:
            raise PermissionDeniedError("Organization not found.")
        return organization
    membership = (
        OrganizationMembership.objects.select_related("organization")
        .filter(user=user, organization_id=organization_id, is_active=True)
        .first()
    )
    if not membership:
        raise PermissionDeniedError("You do not belong to this organization.")
    if allowed_roles and membership.role not in allowed_roles:
        raise PermissionDeniedError("You do not have permission for this organization action.")
    return membership.organization


def _resolve_payee_preset(payload):
    preset_id = payload.get("preset_id")
    if not preset_id:
        return dict(payload), None

    preset = PayeePreset.objects.filter(id=preset_id, active=True).first()
    if not preset:
        raise ValidationError("Invalid preset_id.")

    resolved = dict(payload)
    if payload.get("payee_type") and payload["payee_type"] != preset.payee_type:
        raise ValidationError("payee_type must match the selected preset.")

    resolved["payee_type"] = preset.payee_type
    if not (resolved.get("label") or "").strip():
        resolved["label"] = preset.label
    if not (resolved.get("expense_category") or "").strip():
        resolved["expense_category"] = preset.expense_category

    if preset.payee_type == Payee.PayeeType.PAYBILL:
        provided_number = (payload.get("paybill_number") or "").strip()
        if provided_number and provided_number != preset.paybill_number:
            raise ValidationError("paybill_number must match the selected preset.")
        resolved["paybill_number"] = preset.paybill_number
        resolved["till_number"] = ""
    elif preset.payee_type == Payee.PayeeType.TILL:
        provided_number = (payload.get("till_number") or "").strip()
        if provided_number and provided_number != preset.till_number:
            raise ValidationError("till_number must match the selected preset.")
        resolved["till_number"] = preset.till_number
        resolved["paybill_number"] = ""

    return resolved, preset


def _validate_payee(payload):
    payee_type = payload.get("payee_type")
    if payee_type not in Payee.PayeeType.values:
        raise ValidationError("Invalid payee_type.")
    label = (payload.get("label") or "").strip()
    if not label:
        raise ValidationError("Payee label is required.")
    if payee_type == Payee.PayeeType.PAYBILL and not payload.get("paybill_number"):
        raise ValidationError("paybill_number is required for paybill payees.")
    if payee_type == Payee.PayeeType.TILL and not payload.get("till_number"):
        raise ValidationError("till_number is required for till payees.")
    if payee_type == Payee.PayeeType.MOBILE and not payload.get("phone_number"):
        raise ValidationError("phone_number is required for mobile payees.")
    if payee_type == Payee.PayeeType.BANK and (
        not payload.get("bank_name") or not payload.get("bank_code") or not payload.get("account_number")
    ):
        raise ValidationError("bank_name, bank_code, and account_number are required for bank payees.")


def ensure_expense_category(name):
    category_name = (name or "general").strip() or "general"
    category, _ = ExpenseCategory.objects.get_or_create(
        name=category_name,
        defaults={"active": True},
    )
    if not category.active:
        category.active = True
        category.save(update_fields=["active", "updated_at"])
    return category


def create_payee(user, payload):
    resolved_payload, preset = _resolve_payee_preset(payload)
    _validate_payee(resolved_payload)
    organization = None
    if resolved_payload.get("organization_id"):
        organization = get_organization_for_user(
            user,
            resolved_payload["organization_id"],
            allowed_roles=[
                OrganizationMembership.Role.ADMIN,
                OrganizationMembership.Role.MAKER,
                OrganizationMembership.Role.CHECKER,
            ],
        )

    phone_number = normalize_phone_number(resolved_payload.get("phone_number"))
    expense_category = (resolved_payload.get("expense_category") or "general").strip()
    ensure_expense_category(expense_category)
    payee = Payee.objects.create(
        user=None if organization else user,
        organization=organization,
        preset=preset,
        payee_type=resolved_payload["payee_type"],
        label=resolved_payload["label"].strip(),
        account_reference=(resolved_payload.get("account_reference") or "").strip(),
        phone_number=phone_number,
        paybill_number=(resolved_payload.get("paybill_number") or "").strip(),
        till_number=(resolved_payload.get("till_number") or "").strip(),
        bank_name=(resolved_payload.get("bank_name") or "").strip(),
        bank_code=(resolved_payload.get("bank_code") or "").strip(),
        account_number=(resolved_payload.get("account_number") or "").strip(),
        expense_category=expense_category,
        active=resolved_payload.get("active", True),
    )
    AuditLog.objects.create(
        actor=user,
        action="payee.created",
        target_type="payee",
        target_id=payee.id,
        metadata={
            "payee_label": payee.label,
            "payee_type": payee.payee_type,
            "organization_id": str(payee.organization_id) if payee.organization_id else None,
        },
    )
    return payee


def get_payee_for_user(user, payee_id):
    payee = get_object_or_404(Payee, id=payee_id)
    if payee.organization_id:
        get_organization_for_user(
            user,
            payee.organization_id,
            allowed_roles=[
                OrganizationMembership.Role.ADMIN,
                OrganizationMembership.Role.MAKER,
                OrganizationMembership.Role.CHECKER,
            ],
        )
    elif payee.user_id != user.id:
        raise PermissionDeniedError("You do not own this payee.")
    return payee


def update_payee(user, payee_id, payload):
    payee = get_payee_for_user(user, payee_id)
    candidate = {
        "payee_type": payload.get("payee_type", payee.payee_type),
        "label": payload.get("label", payee.label),
        "paybill_number": payload.get("paybill_number", payee.paybill_number),
        "till_number": payload.get("till_number", payee.till_number),
        "phone_number": normalize_phone_number(payload.get("phone_number", payee.phone_number)),
        "bank_name": payload.get("bank_name", payee.bank_name),
        "bank_code": payload.get("bank_code", payee.bank_code),
        "account_number": payload.get("account_number", payee.account_number),
    }
    _validate_payee(candidate)

    update_fields = []
    for field_name in [
        "payee_type",
        "label",
        "account_reference",
        "phone_number",
        "paybill_number",
        "till_number",
        "bank_name",
        "bank_code",
        "account_number",
        "expense_category",
        "active",
    ]:
        if field_name in payload:
            value = payload.get(field_name)
            if isinstance(getattr(payee, field_name), str):
                value = (value or "").strip()
            if field_name == "phone_number":
                value = normalize_phone_number(value)
            if field_name == "expense_category":
                ensure_expense_category(value)
            if field_name == "active":
                value = bool(value)
            setattr(payee, field_name, value)
            update_fields.append(field_name)

    if not update_fields:
        return payee

    payee.save(update_fields=update_fields + ["updated_at"])
    AuditLog.objects.create(
        actor=user,
        action="payee.updated",
        target_type="payee",
        target_id=payee.id,
        metadata={"fields": update_fields, "payee_label": payee.label, "payee_type": payee.payee_type},
    )
    return payee


def delete_payee(user, payee_id):
    payee = get_payee_for_user(user, payee_id)
    payee_identifier = payee.id
    payee_label = payee.label
    payee_type = payee.payee_type
    payee.delete()
    AuditLog.objects.create(
        actor=user,
        action="payee.deleted",
        target_type="payee",
        target_id=payee_identifier,
        metadata={"payee_label": payee_label, "payee_type": payee_type},
    )
    return payee_identifier


def create_schedule(user, payload):
    payee_id = payload.get("payee_id")
    amount_minor = payload.get("amount_minor")
    day_of_month = payload.get("day_of_month")
    if not payee_id or amount_minor is None or day_of_month is None:
        raise ValidationError("payee_id, amount_minor, and day_of_month are required.")
    if int(day_of_month) < 1 or int(day_of_month) > 31:
        raise ValidationError("day_of_month must be between 1 and 31.")
    payee = get_object_or_404(Payee, id=payee_id)
    if payee.user_id and payee.user_id != user.id:
        raise PermissionDeniedError("You do not own this payee.")
    if payee.organization_id:
        get_organization_for_user(
            user,
            payee.organization_id,
            allowed_roles=[
                OrganizationMembership.Role.ADMIN,
                OrganizationMembership.Role.MAKER,
            ],
        )

    interval_months = int(payload.get("interval_months") or 1)
    if interval_months < 1:
        raise ValidationError("interval_months must be at least 1.")
    next_due_date = _parse_date(payload.get("next_due_date")) if payload.get("next_due_date") else _compute_initial_due_date(int(day_of_month))

    schedule = PaymentSchedule.objects.create(
        payee=payee,
        amount_minor=int(amount_minor),
        day_of_month=int(day_of_month),
        interval_months=interval_months,
        next_due_date=next_due_date,
        requires_approval=bool(payload.get("requires_approval", False)),
        active=payload.get("active", True),
    )
    AuditLog.objects.create(
        actor=user,
        action="schedule.created",
        target_type="schedule",
        target_id=schedule.id,
        metadata={
            "payee_label": payee.label,
            "amount_minor": schedule.amount_minor,
            "day_of_month": schedule.day_of_month,
            "interval_months": schedule.interval_months,
            "next_due_date": schedule.next_due_date.isoformat(),
        },
    )
    return schedule


def get_schedule_for_user(user, schedule_id):
    schedule = get_object_or_404(PaymentSchedule.objects.select_related("payee"), id=schedule_id)
    if schedule.payee.organization_id:
        get_organization_for_user(
            user,
            schedule.payee.organization_id,
            allowed_roles=[
                OrganizationMembership.Role.ADMIN,
                OrganizationMembership.Role.MAKER,
                OrganizationMembership.Role.CHECKER,
            ],
        )
    elif schedule.payee.user_id != user.id:
        raise PermissionDeniedError("You do not own this schedule.")
    return schedule


def update_schedule(user, schedule_id, payload):
    schedule = get_schedule_for_user(user, schedule_id)
    update_fields = []

    if "payee_id" in payload and payload.get("payee_id"):
        payee = get_object_or_404(Payee, id=payload.get("payee_id"))
        if payee.user_id and payee.user_id != user.id:
            raise PermissionDeniedError("You do not own this payee.")
        if payee.organization_id:
            get_organization_for_user(
                user,
                payee.organization_id,
                allowed_roles=[
                    OrganizationMembership.Role.ADMIN,
                    OrganizationMembership.Role.MAKER,
                ],
            )
        if schedule.payee.organization_id != payee.organization_id:
            raise ValidationError("Schedule recipient must belong to the same workspace.")
        schedule.payee = payee
        update_fields.append("payee")

    if "amount_minor" in payload:
        amount_minor = int(payload.get("amount_minor") or 0)
        if amount_minor <= 0:
            raise ValidationError("amount_minor must be greater than 0.")
        schedule.amount_minor = amount_minor
        update_fields.append("amount_minor")

    if "day_of_month" in payload:
        day_of_month = int(payload.get("day_of_month") or 0)
        if day_of_month < 1 or day_of_month > 31:
            raise ValidationError("day_of_month must be between 1 and 31.")
        schedule.day_of_month = day_of_month
        update_fields.append("day_of_month")

    if "interval_months" in payload:
        interval_months = int(payload.get("interval_months") or 0)
        if interval_months < 1:
            raise ValidationError("interval_months must be at least 1.")
        schedule.interval_months = interval_months
        update_fields.append("interval_months")

    if "next_due_date" in payload:
        schedule.next_due_date = _parse_date(payload.get("next_due_date"))
        update_fields.append("next_due_date")

    if "requires_approval" in payload:
        schedule.requires_approval = bool(payload.get("requires_approval"))
        update_fields.append("requires_approval")

    if "active" in payload:
        schedule.active = bool(payload.get("active"))
        update_fields.append("active")

    if not update_fields:
        return schedule

    schedule.save(update_fields=update_fields + ["updated_at"])
    AuditLog.objects.create(
        actor=user,
        action="schedule.updated",
        target_type="schedule",
        target_id=schedule.id,
        metadata={"fields": update_fields, "payee_label": schedule.payee.label, "amount_minor": schedule.amount_minor},
    )
    return schedule


def delete_schedule(user, schedule_id):
    schedule = get_schedule_for_user(user, schedule_id)
    schedule_identifier = schedule.id
    payee_label = schedule.payee.label
    schedule.delete()
    AuditLog.objects.create(
        actor=user,
        action="schedule.deleted",
        target_type="schedule",
        target_id=schedule_identifier,
        metadata={"payee_label": payee_label},
    )
    return schedule_identifier


def list_payees(user, organization_id=None, filters=None):
    filters = filters or {}
    if organization_id:
        organization = get_organization_for_user(user, organization_id)
        queryset = Payee.objects.filter(organization=organization)
    else:
        queryset = Payee.objects.filter(user=user)

    if filters.get("payee_type") in Payee.PayeeType.values:
        queryset = queryset.filter(payee_type=filters["payee_type"])
    if filters.get("active") is not None:
        queryset = queryset.filter(active=bool(filters["active"]))
    if filters.get("q"):
        term = filters["q"].strip()
        queryset = queryset.filter(
            Q(label__icontains=term)
            | Q(account_reference__icontains=term)
            | Q(phone_number__icontains=term)
            | Q(paybill_number__icontains=term)
            | Q(till_number__icontains=term)
            | Q(account_number__icontains=term)
        )
    return queryset.order_by("label", "created_at")


def list_payee_presets(filters=None):
    filters = filters or {}
    queryset = PayeePreset.objects.all()

    if filters.get("payee_type") in Payee.PayeeType.values:
        queryset = queryset.filter(payee_type=filters["payee_type"])

    active = filters.get("active")
    if active is None:
        queryset = queryset.filter(active=True)
    else:
        queryset = queryset.filter(active=bool(active))

    if filters.get("q"):
        term = filters["q"].strip()
        queryset = queryset.filter(
            Q(label__icontains=term)
            | Q(paybill_number__icontains=term)
            | Q(till_number__icontains=term)
            | Q(expense_category__icontains=term)
        )
    return queryset.order_by("label", "created_at")


def list_expense_categories(filters=None):
    filters = filters or {}
    queryset = ExpenseCategory.objects.all()
    active = filters.get("active")
    if active is None:
        queryset = queryset.filter(active=True)
    else:
        queryset = queryset.filter(active=bool(active))
    if filters.get("q"):
        term = filters["q"].strip()
        queryset = queryset.filter(Q(name__icontains=term) | Q(description__icontains=term))
    return queryset.order_by("name", "created_at")


def list_banks(filters=None):
    filters = filters or {}
    queryset = BankDirectory.objects.all()
    active = filters.get("active")
    if active is None:
        queryset = queryset.filter(active=True)
    else:
        queryset = queryset.filter(active=bool(active))
    if filters.get("q"):
        term = filters["q"].strip()
        queryset = queryset.filter(Q(name__icontains=term) | Q(code__icontains=term))
    return queryset.order_by("name", "code")


def list_schedules(user, organization_id=None, filters=None):
    filters = filters or {}
    if organization_id:
        organization = get_organization_for_user(user, organization_id)
        queryset = PaymentSchedule.objects.filter(payee__organization=organization)
    else:
        queryset = PaymentSchedule.objects.filter(payee__user=user)

    queryset = queryset.select_related("payee")
    if filters.get("active") is not None:
        queryset = queryset.filter(active=bool(filters["active"]))
    if filters.get("category"):
        queryset = queryset.filter(payee__expense_category=filters["category"].strip())
    if filters.get("q"):
        term = filters["q"].strip()
        queryset = queryset.filter(Q(payee__label__icontains=term) | Q(payee__account_reference__icontains=term))
    return queryset.order_by("next_due_date", "day_of_month", "payee__label")


def top_up_wallet(user, payload):
    amount_minor = int(payload.get("amount_minor") or 0)
    if amount_minor <= 0:
        raise ValidationError("amount_minor must be greater than 0.")
    requested_wallet_type = payload.get("wallet_type", Account.AccountKind.PRIMARY)
    if requested_wallet_type not in Account.AccountKind.values:
        raise ValidationError("Invalid wallet_type.")

    if payload.get("organization_id"):
        if requested_wallet_type != Account.AccountKind.PRIMARY:
            raise ValidationError("Organization top-ups can only target the primary wallet.")
        organization = get_organization_for_user(
            user,
            payload["organization_id"],
            allowed_roles=[OrganizationMembership.Role.ADMIN, OrganizationMembership.Role.CHECKER],
        )
        wallet = ensure_organization_wallet(organization)
    else:
        primary_wallet, vault_wallet = ensure_user_wallets(user)
        if requested_wallet_type == Account.AccountKind.VAULT:
            if not vault_wallet:
                raise ValidationError("Vault top-ups are only available for individual accounts.")
            wallet = vault_wallet
        else:
            wallet = primary_wallet

    phone_number = payment_stk_phone_number(user.phone_number)
    if not phone_number:
        raise ValidationError("A valid phone number is required to initiate wallet top-up STK.")
    PaymentInterface(sandbox=should_simulate_wallet_topup(payload)).initiate_stk_push(
        wallet,
        amount_minor=amount_minor,
        phone_number=phone_number,
        idempotency_key=payload.get("idempotency_key") or "",
        metadata={
            "description": "Top up of funds",
            "payment_service": payload.get("payment_service", "payment_microservice"),
            "wallet_type": wallet.wallet_type,
            "base_amount_minor": amount_minor,
            "fee_amount_minor": 0,
            "gross_amount_minor": amount_minor,
            "status": "PROCESSING",
        },
    )
    wallet.refresh_from_db()
    return wallet


def list_wallet_ledger(user, organization_id=None, filters=None):
    filters = filters or {}
    if organization_id:
        organization = get_organization_for_user(user, organization_id)
        wallets = Account.objects.filter(organization=organization)
    else:
        wallets = Account.objects.filter(user=user)

    if filters.get("wallet_type") in Account.AccountKind.values:
        wallets = wallets.filter(account_kind=filters["wallet_type"])

    queryset = LedgerTransactionRecord.objects.select_related("account", "transaction_type").filter(account__in=wallets)
    if filters.get("entry_type"):
        queryset = queryset.filter(transaction_type__name=filters["entry_type"])
    return queryset.order_by("-created_at")


@transaction.atomic
def transfer_to_vault(user, payload):
    amount_minor = int(payload.get("amount_minor") or 0)
    direction = (payload.get("direction") or "TO_VAULT").upper()
    if amount_minor <= 0:
        raise ValidationError("amount_minor must be greater than 0.")
    if direction not in {"TO_VAULT", "TO_PRIMARY"}:
        raise ValidationError("direction must be TO_VAULT or TO_PRIMARY.")
    primary_wallet, vault_wallet = ensure_user_wallets(user)
    if not vault_wallet:
        raise ValidationError("Vaulting is only available for individual accounts.")

    if direction == "TO_VAULT":
        if primary_wallet.available_balance_minor < amount_minor:
            raise ValidationError("Insufficient primary wallet balance.")
        debit_wallet = primary_wallet
        credit_wallet = vault_wallet
        debit_description = "Funds moved to vault"
        credit_description = "Funds received in vault"
    else:
        if vault_wallet.available_balance_minor < amount_minor:
            raise ValidationError("Insufficient vault balance.")
        debit_wallet = vault_wallet
        credit_wallet = primary_wallet
        debit_description = "Funds moved from vault"
        credit_description = "Funds returned to primary wallet"

    transfer_between_accounts(
        debit_wallet,
        credit_wallet,
        amount_minor=amount_minor,
        reference=generate_transaction_reference(),
        description=debit_description,
        metadata={
            "direction": direction,
            "base_amount_minor": amount_minor,
            "fee_amount_minor": 0,
            "gross_amount_minor": amount_minor,
            "status": "SUCCEEDED",
        },
    )
    primary_wallet.refresh_from_db()
    vault_wallet.refresh_from_db()
    return primary_wallet, vault_wallet


@transaction.atomic
def withdraw_to_mpesa(user, payload):
    amount_minor = int(payload.get("amount_minor") or 0)
    if amount_minor <= 0:
        raise ValidationError("amount_minor must be greater than 0.")

    phone_number = normalize_phone_number(payload.get("phone_number") or user.mpesa_withdrawal_phone or user.phone_number)
    if not phone_number:
        raise ValidationError("A valid M-Pesa withdrawal phone number is required.")

    primary_wallet, _ = ensure_user_wallets(user)
    if primary_wallet.available_balance_minor < amount_minor:
        raise ValidationError("Insufficient wallet balance.")

    payment_request = PaymentInterface(sandbox=should_simulate_wallet_topup(payload)).initiate_payout(
        primary_wallet,
        amount_minor=amount_minor,
        destination={"type": "MPESA", "phone_number": phone_number},
        idempotency_key=payload.get("idempotency_key") or "",
        metadata={
            "description": "Withdrawal of funds",
            "destination_type": "MPESA",
            "phone_number": phone_number,
            "requires_owner_approval": user.payouts_require_owner_approval,
            "status": "PENDING_APPROVAL" if user.payouts_require_owner_approval else "SUBMITTED",
            "base_amount_minor": amount_minor,
            "fee_amount_minor": 0,
            "gross_amount_minor": amount_minor,
        },
    )
    primary_wallet.refresh_from_db()
    AuditLog.objects.create(
        actor=user,
        action="wallet.withdrawal_requested",
        target_type="wallet",
        target_id=primary_wallet.id,
        metadata={
            "amount_minor": amount_minor,
            "phone_number": phone_number,
            "transaction_id": str(payment_request.transaction_id),
            "requires_owner_approval": user.payouts_require_owner_approval,
        },
    )
    return primary_wallet, payment_request.transaction


def _build_destination_from_payee(payee):
    return {
        "account_reference": payee.account_reference,
        "phone_number": payee.phone_number,
        "paybill_number": payee.paybill_number,
        "till_number": payee.till_number,
        "bank_name": payee.bank_name,
        "bank_code": payee.bank_code,
        "account_number": payee.account_number,
    }


def _calculate_instruction_fee(amount_minor):
    return max(0, int(amount_minor) * SERVICE_FEE_BPS // 10000)


def _recalculate_batch_fee(batch):
    batch.fee_amount_minor = batch.instructions.aggregate(total=Sum("fee_amount_minor"))["total"] or 0
    batch.save(update_fields=["fee_amount_minor", "updated_at"])
    return batch.fee_amount_minor


def _queue_instruction_dispatches(batch):
    for instruction in batch.instructions.filter(status=PaymentInstruction.Status.PENDING):
        OutboxEvent.objects.create(
            topic="payment.instruction.dispatch",
            aggregate_type="payment_instruction",
            aggregate_id=instruction.id,
            payload={"batch_id": str(batch.id)},
        )


def _mark_batch_success(batch, actor=None):
    from_status = batch.status
    batch.status = PaymentBatch.Status.SUCCEEDED
    batch.processed_at = timezone.now()
    batch.save(update_fields=["status", "processed_at", "updated_at"])
    record_transaction_event(
        "payment_batch",
        batch.id,
        "payment_batch.succeeded",
        actor=actor,
        from_status=from_status,
        to_status=batch.status,
        payload={"total_amount_minor": batch.total_amount_minor, "fee_amount_minor": batch.fee_amount_minor},
    )
    schedule_ids = batch.metadata.get("schedule_ids") or []
    if schedule_ids:
        for schedule in PaymentSchedule.objects.filter(id__in=schedule_ids):
            schedule.next_due_date = _add_months(schedule.next_due_date, schedule.interval_months, schedule.day_of_month)
            schedule.save(update_fields=["next_due_date", "updated_at"])
    OutboxEvent.objects.create(
        topic="payment.batch.succeeded",
        aggregate_type="payment_batch",
        aggregate_id=batch.id,
        payload={"total_amount_minor": batch.total_amount_minor, "fee_amount_minor": batch.fee_amount_minor},
    )
    if actor:
        queue_notifications_for_user(
            actor,
            "PAYMENT_SUCCESS",
            _payment_success_notification_context(batch, actor),
            scheduled_for=timezone.now(),
        )


def _mark_batch_failure(batch, actor, reason, status=PaymentBatch.Status.FAILED):
    from_status = batch.status
    batch.status = status
    batch.processed_at = timezone.now()
    batch.save(update_fields=["status", "processed_at", "updated_at"])
    record_transaction_event(
        "payment_batch",
        batch.id,
        "payment_batch.failed",
        actor=actor,
        from_status=from_status,
        to_status=batch.status,
        payload={"reason": reason},
    )
    queue_notifications_for_user(
        actor,
        "PAYMENT_FAILURE",
        {"batch_id": str(batch.id), "reason": reason, "status": status},
        scheduled_for=timezone.now(),
    )


def record_batch_failure(batch, reason, status=PaymentBatch.Status.FAILED):
    actor = _batch_notification_user(batch)
    if actor:
        _mark_batch_failure(batch, actor, reason, status=status)
    else:
        from_status = batch.status
        batch.status = status
        batch.processed_at = timezone.now()
        batch.save(update_fields=["status", "processed_at", "updated_at"])
        record_transaction_event(
            "payment_batch",
            batch.id,
            "payment_batch.failed",
            from_status=from_status,
            to_status=batch.status,
            payload={"reason": reason},
        )


def _batch_notification_user(batch):
    return batch.user or batch.approved_by or batch.submitted_by


def _instruction_destination_phone(instruction):
    destination = instruction.destination or {}
    return destination.get("phone_number") or destination.get("account_number") or ""


def _payment_success_notification_context(batch, user):
    context = {
        "batch_id": str(batch.id),
        "total_amount_minor": batch.total_amount_minor,
        "sender_name": user.full_name if user else "",
        "sender_phone_number": user.phone_number if user else "",
    }
    instructions = list(batch.instructions.all()[:2])
    if len(instructions) == 1:
        instruction = instructions[0]
        context.update(
            {
                "amount_minor": instruction.amount_minor,
                "recipient_name": instruction.recipient_name,
                "recipient_phone_number": _instruction_destination_phone(instruction),
                "recipient_type": instruction.recipient_type,
            }
        )
    else:
        context["payout_count"] = batch.instructions.count()
    return context


@transaction.atomic
def pay_individual_due_items(user, payload):
    if not can_access_individual_features(user):
        raise PermissionDeniedError("Only individual or superadmin users can use pay-all.")

    schedule_ids = payload.get("schedule_ids")
    payment_mode = payload.get("payment_mode", user.default_payment_mode)
    if payment_mode not in PaymentBatch.PaymentMode.values:
        raise ValidationError("Invalid payment_mode.")

    schedules = PaymentSchedule.objects.filter(payee__user=user, active=True, payee__active=True).select_related("payee")
    if schedule_ids:
        schedules = schedules.filter(id__in=schedule_ids)
    else:
        schedules = schedules.filter(next_due_date__lte=timezone.localdate())
    schedules = list(schedules)
    if not schedules:
        raise ValidationError("No active schedules found for payment.")

    batch = PaymentBatch.objects.create(
        batch_kind=PaymentBatch.BatchKind.INDIVIDUAL_MONTHLY,
        status=PaymentBatch.Status.PENDING_APPROVAL if user.payouts_require_owner_approval else PaymentBatch.Status.PROCESSING,
        payment_mode=payment_mode,
        user=user,
        scheduled_for=timezone.localdate(),
        description="Individual pay-all execution",
        submitted_by=user,
        submitted_at=timezone.now() if user.payouts_require_owner_approval else None,
        metadata={
            "schedule_ids": [str(schedule.id) for schedule in schedules],
            "requires_owner_approval": user.payouts_require_owner_approval,
        },
    )
    for schedule in schedules:
        PaymentInstruction.objects.create(
            batch=batch,
            payee=schedule.payee,
            recipient_name=schedule.payee.label,
            recipient_type=schedule.payee.payee_type,
            destination=_build_destination_from_payee(schedule.payee),
            amount_minor=schedule.amount_minor,
            fee_amount_minor=_calculate_instruction_fee(schedule.amount_minor),
            category=schedule.payee.expense_category,
            external_reference=schedule.payee.account_reference,
        )
    batch.recalculate_totals()
    _recalculate_batch_fee(batch)
    if user.payouts_require_owner_approval:
        AuditLog.objects.create(
            actor=user,
            action="individual_batch.pending_owner_approval",
            target_type="batch",
            target_id=batch.id,
            metadata={
                "batch_description": batch.description,
                "instruction_count": batch.instructions.count(),
                "amount_minor": batch.total_amount_minor,
                "fee_amount_minor": batch.fee_amount_minor,
            },
        )
        return batch
    settle_batch(batch, actor=user, simulate_collection=should_simulate_payment_collection(payment_mode, payload))
    return batch


def run_due_wallet_autopayments(run_date=None):
    run_date = run_date or timezone.localdate()
    processed = 0
    users = User.objects.filter(
        default_payment_mode=User.PaymentMode.WALLET,
        payees__schedules__active=True,
        payees__active=True,
        payees__schedules__next_due_date__lte=run_date,
        payees__schedules__requires_approval=False,
    ).filter(account_type__in=[User.AccountType.INDIVIDUAL, User.AccountType.SUPERADMIN]).distinct()

    for user in users:
        already_processed = PaymentBatch.objects.filter(
            user=user,
            batch_kind=PaymentBatch.BatchKind.INDIVIDUAL_MONTHLY,
            scheduled_for=run_date,
            status__in=[PaymentBatch.Status.PENDING_APPROVAL, PaymentBatch.Status.PROCESSING, PaymentBatch.Status.SUCCEEDED],
        ).exists()
        if already_processed:
            continue
        schedules = PaymentSchedule.objects.filter(
            payee__user=user,
            active=True,
            payee__active=True,
            next_due_date__lte=run_date,
            requires_approval=False,
        ).values_list("id", flat=True)
        if schedules:
            pay_individual_due_items(
                user,
                {
                    "payment_mode": PaymentBatch.PaymentMode.WALLET,
                    "schedule_ids": list(schedules),
                },
            )
            processed += 1
    return processed


def _create_instruction_from_row(batch, row):
    recipient_type = row.get("recipient_type")
    if recipient_type not in Payee.PayeeType.values:
        raise ValidationError(f"Invalid recipient_type in CSV row: {recipient_type}")
    amount_minor = int(row.get("amount_minor") or 0)
    if amount_minor <= 0:
        raise ValidationError("CSV amount_minor must be greater than 0.")
    PaymentInstruction.objects.create(
        batch=batch,
        recipient_name=(row.get("recipient_name") or "").strip() or "Unnamed Recipient",
        recipient_type=recipient_type,
        destination={
            "phone_number": normalize_phone_number(row.get("phone_number")),
            "paybill_number": (row.get("paybill_number") or "").strip(),
            "till_number": (row.get("till_number") or "").strip(),
            "bank_name": (row.get("bank_name") or "").strip(),
            "bank_code": (row.get("bank_code") or "").strip(),
            "account_number": (row.get("account_number") or "").strip(),
            "account_reference": (row.get("account_reference") or "").strip(),
        },
        amount_minor=amount_minor,
        fee_amount_minor=_calculate_instruction_fee(amount_minor),
        category=(row.get("category") or "general").strip(),
        external_reference=(row.get("external_reference") or "").strip(),
    )


@transaction.atomic
def upload_corporate_batch(user, payload):
    organization_id = payload.get("organization_id")
    organization = get_organization_for_user(
        user,
        organization_id,
        allowed_roles=[OrganizationMembership.Role.ADMIN, OrganizationMembership.Role.MAKER],
    )
    csv_content = payload.get("csv_content")
    if not csv_content:
        raise ValidationError("csv_content is required.")
    scheduled_for = _parse_date(payload.get("scheduled_for") or timezone.localdate())
    payment_mode = payload.get("payment_mode", PaymentBatch.PaymentMode.WALLET)
    if payment_mode not in PaymentBatch.PaymentMode.values:
        raise ValidationError("Invalid payment_mode.")

    batch = PaymentBatch.objects.create(
        batch_kind=PaymentBatch.BatchKind.CORPORATE_UPLOAD,
        status=PaymentBatch.Status.DRAFT,
        payment_mode=payment_mode,
        organization=organization,
        scheduled_for=scheduled_for,
        description=(payload.get("description") or "Corporate upload batch").strip(),
        source_file_name=(payload.get("source_file_name") or "upload.csv").strip(),
        submitted_by=user,
    )

    reader = csv.DictReader(io.StringIO(csv_content))
    if not reader.fieldnames:
        raise ValidationError("CSV content must include a header row.")
    for row in reader:
        _create_instruction_from_row(batch, row)

    if not batch.instructions.exists():
        raise ValidationError("CSV did not contain any payment rows.")

    batch.recalculate_totals()
    _recalculate_batch_fee(batch)
    AuditLog.objects.create(
        actor=user,
        action="batch.uploaded",
        target_type="batch",
        target_id=batch.id,
        metadata={
            "batch_description": batch.description,
            "source_file_name": batch.source_file_name,
            "instruction_count": batch.instructions.count(),
            "amount_minor": batch.total_amount_minor,
            "fee_amount_minor": batch.fee_amount_minor,
            "organization_id": str(organization.id),
            "organization_name": organization.name,
        },
    )
    return batch


@transaction.atomic
def submit_batch_for_approval(user, batch_id):
    batch = get_object_or_404(
        PaymentBatch.objects.select_related("organization"),
        id=batch_id,
        batch_kind=PaymentBatch.BatchKind.CORPORATE_UPLOAD,
    )
    get_organization_for_user(
        user,
        batch.organization_id,
        allowed_roles=[OrganizationMembership.Role.ADMIN, OrganizationMembership.Role.MAKER],
    )
    if batch.status != PaymentBatch.Status.DRAFT:
        raise ValidationError("Only draft batches can be submitted.")
    from_status = batch.status
    batch.status = PaymentBatch.Status.PENDING_APPROVAL
    batch.submitted_by = user
    batch.submitted_at = timezone.now()
    approval_history = list(batch.metadata.get("approval_history") or [])
    approval_history.append(
        {
            "action": "SUBMITTED",
            "actor_user_id": str(user.id),
            "at": timezone.now().isoformat(),
        }
    )
    batch.metadata["approval_history"] = approval_history
    batch.save(update_fields=["status", "submitted_by", "submitted_at", "metadata", "updated_at"])
    record_transaction_event(
        "payment_batch",
        batch.id,
        "payment_batch.submitted",
        actor=user,
        from_status=from_status,
        to_status=batch.status,
        payload={"organization_id": str(batch.organization_id), "instruction_count": batch.instructions.count()},
    )

    checker_members = OrganizationMembership.objects.filter(
        organization=batch.organization,
        is_active=True,
        role__in=[OrganizationMembership.Role.CHECKER, OrganizationMembership.Role.ADMIN],
    ).select_related("user")
    for member in checker_members:
        queue_notifications_for_user(
            member.user,
            "APPROVAL_REQUEST",
            {"batch_id": str(batch.id), "total_amount_minor": batch.total_amount_minor},
            scheduled_for=timezone.now(),
        )
    AuditLog.objects.create(
        actor=user,
        action="batch.submitted",
        target_type="batch",
        target_id=batch.id,
        metadata={
            "batch_description": batch.description,
            "instruction_count": batch.instructions.count(),
            "amount_minor": batch.total_amount_minor,
            "fee_amount_minor": batch.fee_amount_minor,
            "organization_id": str(batch.organization_id),
            "organization_name": batch.organization.name if batch.organization_id else None,
        },
    )
    return batch


@transaction.atomic
def approve_batch(user, batch_id):
    batch = get_object_or_404(
        PaymentBatch.objects.select_related("organization", "user", "submitted_by"),
        id=batch_id,
    )
    if batch.organization_id:
        get_organization_for_user(
            user,
            batch.organization_id,
            allowed_roles=[OrganizationMembership.Role.ADMIN, OrganizationMembership.Role.CHECKER],
        )
    elif batch.user_id != user.id and not can_manage_all_organizations(user):
        raise PermissionDeniedError("You cannot approve this payout batch.")
    if batch.status != PaymentBatch.Status.PENDING_APPROVAL:
        raise ValidationError("Only pending approval batches can be approved.")

    from_status = batch.status
    batch.status = PaymentBatch.Status.APPROVED
    batch.approved_by = user
    batch.approved_at = timezone.now()
    approval_history = list(batch.metadata.get("approval_history") or [])
    approval_history.append(
        {
            "action": "APPROVED",
            "actor_user_id": str(user.id),
            "at": timezone.now().isoformat(),
        }
    )
    batch.metadata["approval_history"] = approval_history
    batch.save(update_fields=["status", "approved_by", "approved_at", "metadata", "updated_at"])
    record_transaction_event(
        "payment_batch",
        batch.id,
        "payment_batch.approved",
        actor=user,
        from_status=from_status,
        to_status=batch.status,
        payload={"organization_id": str(batch.organization_id) if batch.organization_id else None},
    )
    if batch.submitted_by_id and batch.submitted_by_id != user.id:
        queue_notifications_for_user(
            batch.submitted_by,
            "BATCH_APPROVED",
            {"batch_id": str(batch.id), "organization_id": str(batch.organization_id)},
            scheduled_for=timezone.now(),
        )
    settle_batch(batch, actor=user, simulate_collection=should_simulate_payment_collection(batch.payment_mode))
    AuditLog.objects.create(
        actor=user,
        action="batch.approved",
        target_type="batch",
        target_id=batch.id,
        metadata={
            "batch_description": batch.description,
            "instruction_count": batch.instructions.count(),
            "amount_minor": batch.total_amount_minor,
            "fee_amount_minor": batch.fee_amount_minor,
            "organization_id": str(batch.organization_id) if batch.organization_id else None,
            "organization_name": batch.organization.name if batch.organization_id else None,
        },
    )
    return batch


@transaction.atomic
def reject_batch(user, batch_id, payload):
    batch = get_object_or_404(
        PaymentBatch.objects.select_related("organization", "submitted_by", "user"),
        id=batch_id,
    )
    if batch.organization_id:
        get_organization_for_user(
            user,
            batch.organization_id,
            allowed_roles=[OrganizationMembership.Role.ADMIN, OrganizationMembership.Role.CHECKER],
        )
    elif batch.user_id != user.id and not can_manage_all_organizations(user):
        raise PermissionDeniedError("You cannot reject this payout batch.")
    if batch.status != PaymentBatch.Status.PENDING_APPROVAL:
        raise ValidationError("Only pending approval batches can be rejected.")

    rejection_reason = (payload.get("reason") or "").strip()
    if not rejection_reason:
        raise ValidationError("reason is required.")

    from_status = batch.status
    batch.status = PaymentBatch.Status.REJECTED
    approval_history = list(batch.metadata.get("approval_history") or [])
    approval_history.append(
        {
            "action": "REJECTED",
            "actor_user_id": str(user.id),
            "at": timezone.now().isoformat(),
            "reason": rejection_reason,
        }
    )
    batch.metadata["approval_history"] = approval_history
    batch.metadata["rejection_reason"] = rejection_reason
    batch.processed_at = timezone.now()
    batch.save(update_fields=["status", "metadata", "processed_at", "updated_at"])
    record_transaction_event(
        "payment_batch",
        batch.id,
        "payment_batch.rejected",
        actor=user,
        from_status=from_status,
        to_status=batch.status,
        payload={"reason": rejection_reason},
    )

    if batch.submitted_by_id and batch.submitted_by_id != user.id:
        queue_notifications_for_user(
            batch.submitted_by,
            "BATCH_REJECTED",
            {
                "batch_id": str(batch.id),
                "organization_id": str(batch.organization_id),
                "reason": rejection_reason,
            },
            scheduled_for=timezone.now(),
        )
    AuditLog.objects.create(
        actor=user,
        action="batch.rejected",
        target_type="batch",
        target_id=batch.id,
        metadata={
            "reason": rejection_reason,
            "batch_description": batch.description,
            "instruction_count": batch.instructions.count(),
            "organization_id": str(batch.organization_id) if batch.organization_id else None,
            "organization_name": batch.organization.name if batch.organization_id else None,
        },
    )
    return batch


@transaction.atomic
def settle_batch(batch, actor, simulate_collection=True):
    logger.info(
        "payment.batch.settle.start batch_id=%s actor_id=%s payment_mode=%s simulate_collection=%s status=%s",
        batch.id,
        actor.id if actor else None,
        batch.payment_mode,
        simulate_collection,
        batch.status,
    )
    if batch.payment_mode == PaymentBatch.PaymentMode.WALLET:
        if batch.organization_id:
            wallet = ensure_organization_wallet(batch.organization)
        else:
            wallet, _ = ensure_user_wallets(batch.user)
        required_total = batch.total_amount_minor + batch.fee_amount_minor
        if wallet.available_balance_minor < required_total:
            _mark_batch_failure(batch, actor, "insufficient_wallet_balance")
            raise ValidationError("Insufficient wallet balance.")

        reference = generate_transaction_reference()
        ledger_tx = initiate_payout(
            wallet,
            amount_minor=required_total,
            reference=reference,
            idempotency_key=_wallet_idempotency_key("batch-disbursement", reference),
            description="Batch disbursement",
            metadata={
                "description": "Batch disbursement",
                "batch_id": str(batch.id),
                "base_amount_minor": batch.total_amount_minor,
                "fee_amount_minor": batch.fee_amount_minor,
                "gross_amount_minor": required_total,
                "status": "PROCESSING" if payment_microservice_dispatch_enabled() and not simulate_collection else "SUCCEEDED",
            },
        )
        batch.metadata["ledger_transaction_id"] = str(ledger_tx.id)
        batch.save(update_fields=["metadata", "updated_at"])
        if not payment_microservice_dispatch_enabled() or simulate_collection:
            complete_payout(ledger_tx)
        wallet.refresh_from_db()
        if payment_microservice_dispatch_enabled() and not simulate_collection:
            from_status = batch.status
            batch.status = PaymentBatch.Status.PROCESSING
            batch.save(update_fields=["status", "updated_at"])
            record_transaction_event(
                "payment_batch",
                batch.id,
                "payment_batch.processing",
                actor=actor,
                from_status=from_status,
                to_status=batch.status,
                payload={"dispatch": "payment_microservice"},
            )
            _queue_instruction_dispatches(batch)
            logger.info("payment.batch.wallet.dispatch_queued batch_id=%s instruction_count=%s", batch.id, batch.instructions.count())
            return batch
    else:
        if not simulate_collection:
            phone_number = payment_stk_phone_number(batch.user.phone_number if batch.user_id else "")
            from_status = batch.status
            batch.status = PaymentBatch.Status.PROCESSING
            batch.save(update_fields=["status", "updated_at"])
            record_transaction_event(
                "payment_batch",
                batch.id,
                "payment_batch.processing",
                actor=actor,
                from_status=from_status,
                to_status=batch.status,
                payload={"dispatch": "collection.stk.requested"},
            )
            event = OutboxEvent.objects.create(
                topic="collection.stk.requested",
                aggregate_type="payment_batch",
                aggregate_id=batch.id,
                payload={
                    "amount_minor": batch.total_amount_minor + batch.fee_amount_minor,
                    "phone_number": phone_number,
                },
            )
            logger.info(
                "payment.batch.stk.collection_queued batch_id=%s event_id=%s phone_number=%s amount_minor=%s",
                batch.id,
                event.id,
                phone_number,
                batch.total_amount_minor + batch.fee_amount_minor,
            )
            dispatch_outbox_event_inline(event)
            logger.info(
                "payment.batch.stk.collection_after_dispatch batch_id=%s event_id=%s event_status=%s last_error=%s",
                batch.id,
                event.id,
                event.status,
                event.last_error,
            )
            return batch

    batch.instructions.update(status=PaymentInstruction.Status.SUCCEEDED, failure_reason="")
    _mark_batch_success(batch, actor=actor)
    return batch


def mark_batch_collection_complete(batch, microservice_response):
    batch.metadata["collection_response"] = microservice_response
    batch.save(update_fields=["metadata", "updated_at"])
    _queue_instruction_dispatches(batch)
    return batch


def finalize_batch_from_instructions(batch):
    summary = batch.instructions.aggregate(
        pending=Count("id", filter=Q(status=PaymentInstruction.Status.PENDING)),
        failed=Count("id", filter=Q(status=PaymentInstruction.Status.FAILED)),
        succeeded=Count("id", filter=Q(status=PaymentInstruction.Status.SUCCEEDED)),
    )
    if summary["pending"]:
        return batch.status
    from_status = batch.status
    if summary["failed"] and summary["succeeded"]:
        batch.status = PaymentBatch.Status.PARTIAL
    elif summary["failed"]:
        batch.status = PaymentBatch.Status.FAILED
    else:
        batch.status = PaymentBatch.Status.SUCCEEDED
    ledger_transaction_id = (batch.metadata or {}).get("ledger_transaction_id")
    if ledger_transaction_id:
        try:
            ledger_tx = LedgerTransactionRecord.objects.get(id=ledger_transaction_id)
            if batch.status == PaymentBatch.Status.SUCCEEDED:
                complete_payout(ledger_tx)
            elif batch.status in {PaymentBatch.Status.FAILED, PaymentBatch.Status.PARTIAL}:
                from ledger.services import fail_transaction

                fail_transaction(ledger_tx, reason=f"Batch ended as {batch.status}")
        except LedgerTransactionRecord.DoesNotExist:
            logger.warning("payment.batch.ledger_transaction_missing batch_id=%s transaction_id=%s", batch.id, ledger_transaction_id)
    batch.processed_at = timezone.now()
    batch.save(update_fields=["status", "processed_at", "updated_at"])
    record_transaction_event(
        "payment_batch",
        batch.id,
        f"payment_batch.{batch.status.lower()}",
        actor=_batch_notification_user(batch),
        from_status=from_status,
        to_status=batch.status,
        payload=summary,
    )
    OutboxEvent.objects.create(
        topic=f"payment.batch.{batch.status.lower()}",
        aggregate_type="payment_batch",
        aggregate_id=batch.id,
        payload={"status": batch.status},
    )
    user = _batch_notification_user(batch)
    if not user:
        return batch.status
    if batch.status == PaymentBatch.Status.SUCCEEDED:
        queue_notifications_for_user(
            user,
            "PAYMENT_SUCCESS",
            _payment_success_notification_context(batch, user),
            scheduled_for=timezone.now(),
        )
    elif batch.status in {PaymentBatch.Status.FAILED, PaymentBatch.Status.PARTIAL}:
        queue_notifications_for_user(
            user,
            "PAYMENT_FAILURE",
            {"batch_id": str(batch.id), "status": batch.status},
            scheduled_for=timezone.now(),
        )
    return batch.status


def record_instruction_success(instruction, microservice_response, microservice_request_id=""):
    from_status = instruction.status
    instruction.status = PaymentInstruction.Status.SUCCEEDED
    instruction.failure_reason = ""
    instruction.microservice_request_id = microservice_request_id or instruction.microservice_request_id
    instruction.microservice_response = microservice_response or {}
    instruction.save(
        update_fields=["status", "failure_reason", "microservice_request_id", "microservice_response", "updated_at"]
    )
    record_transaction_event(
        "payment_instruction",
        instruction.id,
        "payment_instruction.succeeded",
        from_status=from_status,
        to_status=instruction.status,
        microservice_request_id=instruction.microservice_request_id,
        payload={"microservice_response": microservice_response or {}},
    )
    finalize_batch_from_instructions(instruction.batch)
    return instruction


def record_instruction_failure(instruction, reason, microservice_response=None):
    from_status = instruction.status
    instruction.status = PaymentInstruction.Status.FAILED
    instruction.failure_reason = reason[:255]
    instruction.microservice_response = microservice_response or {}
    instruction.save(update_fields=["status", "failure_reason", "microservice_response", "updated_at"])
    record_transaction_event(
        "payment_instruction",
        instruction.id,
        "payment_instruction.failed",
        from_status=from_status,
        to_status=instruction.status,
        microservice_request_id=instruction.microservice_request_id,
        payload={"reason": reason, "microservice_response": microservice_response or {}},
    )
    finalize_batch_from_instructions(instruction.batch)
    return instruction


def get_batch_for_user(user, batch_id):
    batch = get_object_or_404(
        PaymentBatch.objects.select_related("organization", "user", "submitted_by", "approved_by"),
        id=batch_id,
    )
    if batch.organization_id:
        get_organization_for_user(
            user,
            batch.organization_id,
            allowed_roles=[
                OrganizationMembership.Role.ADMIN,
                OrganizationMembership.Role.MAKER,
                OrganizationMembership.Role.CHECKER,
            ],
        )
    elif batch.user_id != user.id and not can_manage_all_organizations(user):
        raise PermissionDeniedError("You do not have access to this batch.")
    return batch


def list_batches(user, organization_id=None, filters=None):
    filters = filters or {}
    queryset = PaymentBatch.objects.all().order_by("-created_at")
    if organization_id:
        organization = get_organization_for_user(user, organization_id)
        queryset = queryset.filter(organization=organization)
    elif can_manage_all_organizations(user):
        queryset = queryset
    elif user.account_type == User.AccountType.CORPORATE:
        memberships = OrganizationMembership.objects.filter(user=user, is_active=True).values_list("organization_id", flat=True)
        queryset = queryset.filter(Q(user=user) | Q(organization_id__in=memberships))
    else:
        queryset = queryset.filter(user=user)

    if filters.get("status") in PaymentBatch.Status.values:
        queryset = queryset.filter(status=filters["status"])
    if filters.get("batch_kind") in PaymentBatch.BatchKind.values:
        queryset = queryset.filter(batch_kind=filters["batch_kind"])
    if filters.get("payment_mode") in PaymentBatch.PaymentMode.values:
        queryset = queryset.filter(payment_mode=filters["payment_mode"])
    return queryset


def quick_pay(user, payload):
    payee_id = payload.get("payee_id")
    amount_minor = int(payload.get("amount_minor") or 0)
    if not payee_id or amount_minor <= 0:
        raise ValidationError("payee_id and amount_minor are required.")

    payment_mode = payload.get("payment_mode", user.default_payment_mode)
    if payment_mode not in PaymentBatch.PaymentMode.values:
        raise ValidationError("Invalid payment_mode.")

    payee = get_object_or_404(Payee, id=payee_id)
    organization = None
    if payload.get("organization_id"):
        organization = get_organization_for_user(
            user,
            payload["organization_id"],
            allowed_roles=[
                OrganizationMembership.Role.ADMIN,
                OrganizationMembership.Role.MAKER,
                OrganizationMembership.Role.CHECKER,
            ],
        )
        if payee.organization_id and payee.organization_id != organization.id:
            raise ValidationError("payee_id does not belong to the selected organization.")
        if payee.user_id:
            raise ValidationError("Organization quick pay requires an organization payee.")
    elif payee.organization_id:
        get_organization_for_user(
            user,
            payee.organization_id,
            allowed_roles=[
                OrganizationMembership.Role.ADMIN,
                OrganizationMembership.Role.MAKER,
                OrganizationMembership.Role.CHECKER,
            ],
        )
        organization = payee.organization
    elif payee.user_id != user.id and not can_manage_all_organizations(user):
        raise PermissionDeniedError("You do not own this payee.")

    batch = PaymentBatch.objects.create(
        batch_kind=PaymentBatch.BatchKind.CORPORATE_UPLOAD if organization else PaymentBatch.BatchKind.INDIVIDUAL_ADHOC,
        status=PaymentBatch.Status.DRAFT if organization else (
            PaymentBatch.Status.PENDING_APPROVAL if user.payouts_require_owner_approval else PaymentBatch.Status.PROCESSING
        ),
        payment_mode=payment_mode,
        user=None if organization else user,
        organization=organization,
        scheduled_for=_parse_date(payload.get("scheduled_for") or timezone.localdate()),
        description=(payload.get("description") or f"Quick pay to {payee.label}").strip(),
        submitted_by=user if organization or user.payouts_require_owner_approval else None,
        submitted_at=timezone.now() if not organization and user.payouts_require_owner_approval else None,
        metadata={"requires_owner_approval": user.payouts_require_owner_approval} if not organization else {},
    )
    PaymentInstruction.objects.create(
        batch=batch,
        payee=payee,
        recipient_name=payee.label,
        recipient_type=payee.payee_type,
        destination=_build_destination_from_payee(payee),
        amount_minor=amount_minor,
        fee_amount_minor=_calculate_instruction_fee(amount_minor),
        category=payee.expense_category,
        external_reference=(payload.get("external_reference") or payee.account_reference or "").strip(),
    )
    batch.recalculate_totals()
    _recalculate_batch_fee(batch)

    if organization:
        if payload.get("submit_for_approval", True):
            batch = submit_batch_for_approval(user, batch.id)
        return batch

    if user.payouts_require_owner_approval:
        AuditLog.objects.create(
            actor=user,
            action="quick_pay.pending_owner_approval",
            target_type="batch",
            target_id=batch.id,
            metadata={
                "batch_description": batch.description,
                "payee_label": payee.label,
                "instruction_count": batch.instructions.count(),
                "amount_minor": batch.total_amount_minor,
                "fee_amount_minor": batch.fee_amount_minor,
            },
        )
        return batch

    settle_batch(batch, actor=user, simulate_collection=should_simulate_payment_collection(payment_mode, payload))
    return batch


def list_approval_batches(user, organization_id=None):
    return (
        list_batches(
            user,
            organization_id,
            {"status": PaymentBatch.Status.PENDING_APPROVAL},
        )
        .select_related("submitted_by", "organization")
        .prefetch_related("instructions")
    )


def build_approval_queue(user, organization_id=None):
    queue = []
    for batch in list_approval_batches(user, organization_id)[:50]:
        sample_instructions = list(batch.instructions.all()[:5])
        queue.append(
            {
                "batch_id": str(batch.id),
                "name": batch.description or batch.source_file_name or "Approval item",
                "organization_id": str(batch.organization_id) if batch.organization_id else None,
                "organization_name": batch.organization.name if batch.organization_id else None,
                "submitted_by": batch.submitted_by.full_name if batch.submitted_by_id else "",
                "scheduled_for": batch.scheduled_for.isoformat(),
                "base_amount_minor": batch.total_amount_minor,
                "fee_amount_minor": batch.fee_amount_minor,
                "gross_amount_minor": batch.total_amount_minor + batch.fee_amount_minor,
                "instruction_count": batch.instructions.count(),
                "status": batch.status,
                "sample_instructions": [
                    {
                        "recipient_name": instruction.recipient_name,
                        "recipient_type": instruction.recipient_type,
                        "amount_minor": instruction.amount_minor,
                    }
                    for instruction in sample_instructions
                ],
            }
        )
    return queue


def build_transaction_summary(user, organization_id=None, filters=None):
    filters = filters or {}
    instruction_queryset = _instruction_queryset_for_user(user, organization_id)
    if filters.get("status") in PaymentInstruction.Status.values:
        instruction_queryset = instruction_queryset.filter(status=filters["status"])
    if filters.get("category"):
        instruction_queryset = instruction_queryset.filter(category=filters["category"].strip())
    if filters.get("recipient_type") in Payee.PayeeType.values:
        instruction_queryset = instruction_queryset.filter(recipient_type=filters["recipient_type"])
    if filters.get("q"):
        term = filters["q"].strip()
        instruction_queryset = instruction_queryset.filter(
            Q(recipient_name__icontains=term)
            | Q(external_reference__icontains=term)
            | Q(microservice_request_id__icontains=term)
        )

    date_from = _parse_date(filters["date_from"]) if filters.get("date_from") else timezone.localdate() - timedelta(days=30)
    date_to = _parse_date(filters["date_to"]) if filters.get("date_to") else timezone.localdate()
    instruction_queryset = instruction_queryset.filter(created_at__date__gte=date_from, created_at__date__lte=date_to)

    if organization_id:
        wallet_queryset = LedgerTransactionRecord.objects.select_related("account", "transaction_type").filter(account__organization_id=organization_id)
    else:
        wallet_queryset = LedgerTransactionRecord.objects.select_related("account", "transaction_type").filter(account__user=user)
    wallet_queryset = wallet_queryset.filter(created_at__date__gte=date_from, created_at__date__lte=date_to).order_by("created_at")

    summary = instruction_queryset.aggregate(
        total_base=Sum("amount_minor"),
        total_fees=Sum("fee_amount_minor"),
    )
    total_base = summary["total_base"] or 0
    total_fees = summary["total_fees"] or 0
    total_debits = total_base + total_fees
    total_credits = sum(entry.amount_minor for entry in wallet_queryset if entry.transaction_type.name == "WalletTopup")
    opening_balance = 0
    first_entry = wallet_queryset.first()
    if first_entry:
        opening_balance = first_entry.balance_after_minor

    transactions = [_serialize_activity_instruction(instruction) for instruction in instruction_queryset[:200]]
    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "opening_balance_minor": opening_balance,
        "total_debits_minor": total_debits,
        "total_credits_minor": total_credits,
        "total_fees_minor": total_fees,
        "transaction_count": len(transactions),
        "transactions": transactions,
    }


def build_dashboard(user, organization_id=None):
    dashboard = {"account_type": user.account_type}
    if can_access_individual_features(user):
        primary_wallet, vault_wallet = ensure_user_wallets(user)
        active_schedules = PaymentSchedule.objects.filter(payee__user=user, active=True, payee__active=True).select_related("payee")
        monthly_commitments = active_schedules.aggregate(total=Sum("amount_minor"))["total"] or 0
        month_start = _start_of_month(timezone.localdate())
        month_end = _end_of_month(timezone.localdate())
        due_this_month = active_schedules.filter(next_due_date__gte=month_start, next_due_date__lte=month_end).order_by("next_due_date", "payee__label")
        personal_instruction_queryset = PaymentInstruction.objects.select_related("batch", "payee").filter(batch__user=user).order_by("-created_at")
        recent_transactions = [_serialize_activity_instruction(item) for item in personal_instruction_queryset[:5]]
        category_source = personal_instruction_queryset.filter(created_at__date__gte=timezone.localdate() - timedelta(days=30))
        category_totals = {}
        for instruction in category_source:
            category_totals.setdefault(instruction.category, 0)
            category_totals[instruction.category] += instruction.amount_minor + instruction.fee_amount_minor

        monthly_trend = []
        for offset in range(5, -1, -1):
            month_anchor = _start_of_month(_add_months(timezone.localdate(), -offset, 1))
            month_key = (month_anchor.year, month_anchor.month)
            month_income = 0
            month_spend = 0
            for entry in LedgerTransactionRecord.objects.select_related("transaction_type").filter(account__user=user, created_at__year=month_key[0], created_at__month=month_key[1]):
                if entry.transaction_type.name == "WalletTopup":
                    month_income += entry.amount_minor
            for instruction in personal_instruction_queryset.filter(created_at__year=month_key[0], created_at__month=month_key[1]):
                month_spend += instruction.amount_minor + instruction.fee_amount_minor
            monthly_trend.append(
                {
                    "label": month_anchor.strftime("%b"),
                    "income_minor": month_income,
                    "spend_minor": month_spend,
                }
            )

        individual_dashboard = {
            "wallet_balance_minor": primary_wallet.available_balance_minor,
            "vault_balance_minor": vault_wallet.available_balance_minor if vault_wallet else 0,
            "monthly_commitments_minor": monthly_commitments,
            "missing_capital_minor": max(monthly_commitments - primary_wallet.available_balance_minor, 0),
            "upcoming_schedules": [
                {
                    "schedule_id": str(schedule.id),
                    "payee_label": schedule.payee.label,
                    "amount_minor": schedule.amount_minor,
                    "fee_amount_minor": _calculate_instruction_fee(schedule.amount_minor),
                    "gross_amount_minor": schedule.amount_minor + _calculate_instruction_fee(schedule.amount_minor),
                    "next_due_date": schedule.next_due_date.isoformat(),
                    "interval_months": schedule.interval_months,
                    "cadence_label": _format_schedule_cadence(schedule.interval_months),
                    "category": schedule.payee.expense_category,
                }
                for schedule in active_schedules.order_by("next_due_date", "payee__label")[:10]
            ],
            "due_this_month": [
                {
                    "schedule_id": str(schedule.id),
                    "payee_label": schedule.payee.label,
                    "category": schedule.payee.expense_category,
                    "next_due_date": schedule.next_due_date.isoformat(),
                    "interval_months": schedule.interval_months,
                    "cadence_label": _format_schedule_cadence(schedule.interval_months),
                    "base_amount_minor": schedule.amount_minor,
                    "fee_amount_minor": _calculate_instruction_fee(schedule.amount_minor),
                    "gross_amount_minor": schedule.amount_minor + _calculate_instruction_fee(schedule.amount_minor),
                }
                for schedule in due_this_month
            ],
            "due_this_month_total_minor": sum(
                schedule.amount_minor + _calculate_instruction_fee(schedule.amount_minor) for schedule in due_this_month
            ),
            "recent_transactions": recent_transactions,
            "spending_by_category": [
                {"category": category, "amount_minor": amount}
                for category, amount in sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
            ],
            "monthly_trend": monthly_trend,
        }
        if user.account_type == User.AccountType.INDIVIDUAL:
            return {**dashboard, **individual_dashboard}
        dashboard["individual"] = individual_dashboard

    organizations = []
    selected_organization = None
    if can_access_corporate_features(user):
        membership_map = {}
        if can_manage_all_organizations(user):
            organizations_qs = list(Organization.objects.all().order_by("name"))
        else:
            memberships = OrganizationMembership.objects.filter(user=user, is_active=True).select_related("organization")
            membership_map = {membership.organization_id: membership.role for membership in memberships}
            organizations_qs = [membership.organization for membership in memberships]
        if organization_id:
            selected_organization = get_organization_for_user(user, organization_id)
        elif organizations_qs:
            selected_organization = organizations_qs[0]
        for organization in organizations_qs:
            wallet = ensure_organization_wallet(organization)
            pending_approvals = PaymentBatch.objects.filter(
                organization=organization,
                status=PaymentBatch.Status.PENDING_APPROVAL,
            ).count()
            organizations.append(
                {
                    "organization_id": str(organization.id),
                    "name": organization.name,
                    "role": membership_map.get(
                        organization.id,
                        "SERVICE_PROVIDER" if user.account_type == User.AccountType.SERVICE_PROVIDER else "SUPERADMIN",
                    ),
                    "kyc_status": organization.kyc_status,
                    "wallet_balance_minor": wallet.available_balance_minor,
                    "pending_approvals": pending_approvals,
                }
            )

    if selected_organization:
        org_wallet = ensure_organization_wallet(selected_organization)
        recent_org_transactions = [_serialize_activity_instruction(item) for item in _instruction_queryset_for_user(user, str(selected_organization.id))[:5]]
        member_counts = {
            "total": OrganizationMembership.objects.filter(organization=selected_organization, is_active=True).count(),
            "admins": OrganizationMembership.objects.filter(
                organization=selected_organization, is_active=True, role=OrganizationMembership.Role.ADMIN
            ).count(),
            "makers": OrganizationMembership.objects.filter(
                organization=selected_organization, is_active=True, role=OrganizationMembership.Role.MAKER
            ).count(),
            "checkers": OrganizationMembership.objects.filter(
                organization=selected_organization, is_active=True, role=OrganizationMembership.Role.CHECKER
            ).count(),
            "viewers": OrganizationMembership.objects.filter(
                organization=selected_organization, is_active=True, role=OrganizationMembership.Role.VIEWER
            ).count(),
        }
        dashboard["selected_organization"] = {
            "organization_id": str(selected_organization.id),
            "name": selected_organization.name,
            "registration_number": selected_organization.registration_number,
            "kyc_status": selected_organization.kyc_status,
            "wallet_balance_minor": org_wallet.available_balance_minor,
            "pending_approvals": PaymentBatch.objects.filter(
                organization=selected_organization,
                status=PaymentBatch.Status.PENDING_APPROVAL,
            ).count(),
            "recent_batches": [
                {
                    "batch_id": str(batch.id),
                    "description": batch.description,
                    "status": batch.status,
                    "scheduled_for": batch.scheduled_for.isoformat(),
                    "instruction_count": batch.instructions.count(),
                    "gross_amount_minor": batch.total_amount_minor + batch.fee_amount_minor,
                }
                for batch in list_batches(user, str(selected_organization.id))[:5]
            ],
            "recent_transactions": recent_org_transactions,
            "member_counts": member_counts,
        }

    if organizations:
        dashboard["organizations"] = organizations
    return dashboard


def create_due_notifications(run_date=None):
    run_date = run_date or timezone.localdate()
    reminder_day = max(run_date.day + 3, 1)
    schedules = PaymentSchedule.objects.filter(day_of_month=reminder_day, active=True, payee__active=True).select_related("payee__user")
    created = 0
    user_rollups = {}
    for schedule in schedules:
        user = schedule.payee.user
        if not user:
            continue
        if user.id not in user_rollups:
            user_rollups[user.id] = {
                "user": user,
                "total_amount_minor": 0,
                "schedule_count": 0,
            }
        user_rollups[user.id]["total_amount_minor"] += schedule.amount_minor
        user_rollups[user.id]["schedule_count"] += 1

    for rollup in user_rollups.values():
        events = queue_notifications_for_user(
            rollup["user"],
            "T_MINUS_3",
            {
                "total_amount_minor": rollup["total_amount_minor"],
                "schedule_count": rollup["schedule_count"],
                "due_in_days": 3,
                "due_day": reminder_day,
            },
            scheduled_for=timezone.now(),
        )
        created += len(events)
    return created


def export_transactions_csv_rows(user, organization_id=None):
    queryset = PaymentInstruction.objects.select_related("batch").order_by("-created_at")
    if organization_id:
        organization = get_organization_for_user(user, organization_id)
        queryset = queryset.filter(batch__organization=organization)
    elif can_manage_all_organizations(user):
        queryset = queryset
    else:
        queryset = queryset.filter(batch__user=user)
    for instruction in queryset:
        yield {
            "instruction_id": str(instruction.id),
            "batch_id": str(instruction.batch_id),
            "recipient_name": instruction.recipient_name,
            "recipient_type": instruction.recipient_type,
            "amount_minor": instruction.amount_minor,
            "fee_amount_minor": instruction.fee_amount_minor,
            "gross_amount_minor": instruction.amount_minor + instruction.fee_amount_minor,
            "status": instruction.status,
            "category": instruction.category,
            "scheduled_for": instruction.batch.scheduled_for.isoformat(),
            "created_at": instruction.created_at.isoformat(),
        }
