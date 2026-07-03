import csv
import io
import uuid
from datetime import date
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
from eusers.models import AccessToken, User
from notifications.services import queue_notifications_for_user

from .models import Organization, OrganizationMembership, OutboxEvent, Payee, PaymentBatch, PaymentInstruction, PaymentSchedule, Wallet, WalletLedgerEntry

SERVICE_FEE_BPS = 150


class DomainError(Exception):
    pass


class PermissionDeniedError(DomainError):
    pass


class ValidationError(DomainError):
    pass


def can_access_individual_features(user):
    return user.account_type in {User.AccountType.INDIVIDUAL, User.AccountType.SUPERADMIN}


def can_access_corporate_features(user):
    return user.account_type in {User.AccountType.CORPORATE, User.AccountType.SUPERADMIN}


def _parse_date(value):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValidationError("Dates must use ISO format YYYY-MM-DD.") from exc


def provider_dispatch_enabled():
    return bool(
        settings.PESAWAY_ENABLED
        and settings.PESAWAY_CLIENT_ID
        and settings.PESAWAY_CLIENT_SECRET
    )


def amount_minor_to_provider_amount(amount_minor):
    return str((Decimal(amount_minor) / Decimal("100")).quantize(Decimal("0.01")))


def build_provider_reference(prefix, entity_id):
    return f"{prefix}-{str(entity_id).split('-')[0]}-{uuid.uuid4().hex[:8]}"


def ensure_user_wallets(user):
    primary, _ = Wallet.objects.get_or_create(
        user=user,
        wallet_type=Wallet.WalletType.PRIMARY,
        defaults={"owner_type": Wallet.OwnerType.USER},
    )
    vault = None
    if can_access_individual_features(user):
        vault, _ = Wallet.objects.get_or_create(
            user=user,
            wallet_type=Wallet.WalletType.VAULT,
            defaults={"owner_type": Wallet.OwnerType.USER},
        )
    return primary, vault


def ensure_organization_wallet(organization):
    wallet, _ = Wallet.objects.get_or_create(
        organization=organization,
        wallet_type=Wallet.WalletType.PRIMARY,
        defaults={"owner_type": Wallet.OwnerType.ORGANIZATION},
    )
    return wallet


def issue_token(user):
    _, raw_token = AccessToken.issue(user)
    return raw_token


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
        metadata={"organization_id": str(organization.id) if organization else None, "scopes": scopes},
    )
    return api_key, raw_key


def list_integration_api_keys(user, organization_id=None):
    queryset = IntegrationApiKey.objects.select_related("user", "organization", "created_by").order_by("-created_at")
    if user.account_type == User.AccountType.SUPERADMIN and not organization_id:
        return queryset
    if organization_id:
        organization = get_organization_for_user(user, organization_id, allowed_roles=[OrganizationMembership.Role.ADMIN])
        return queryset.filter(organization=organization)
    return queryset.filter(user=user)


def revoke_integration_api_key(user, api_key_id):
    api_key = get_object_or_404(IntegrationApiKey.objects.select_related("organization", "user"), id=api_key_id)
    if api_key.organization_id:
        get_organization_for_user(user, api_key.organization_id, allowed_roles=[OrganizationMembership.Role.ADMIN])
    elif api_key.user_id != user.id and user.account_type != User.AccountType.SUPERADMIN:
        raise PermissionDeniedError("You do not have permission to revoke this API key.")
    api_key.is_active = False
    api_key.revoked_at = timezone.now()
    api_key.save(update_fields=["is_active", "revoked_at", "updated_at"])
    AuditLog.objects.create(
        actor=user,
        action="integration_api_key.revoked",
        target_type="integration_api_key",
        target_id=api_key.id,
    )
    return api_key


@transaction.atomic
def register_user(payload):
    phone_number = (payload.get("phone_number") or "").strip()
    password = payload.get("password") or ""
    full_name = (payload.get("full_name") or "").strip()
    account_type = payload.get("account_type")

    if not phone_number or not password or not full_name or not account_type:
        raise ValidationError("phone_number, password, full_name, and account_type are required.")
    if account_type not in User.AccountType.values:
        raise ValidationError("account_type must be INDIVIDUAL, CORPORATE, or SUPERADMIN.")

    user = User.objects.create_user(
        phone_number=phone_number,
        password=password,
        full_name=full_name,
        email=(payload.get("email") or "").strip(),
        account_type=account_type,
        default_payment_mode=payload.get("default_payment_mode", User.PaymentMode.WALLET),
    )
    ensure_user_wallets(user)

    if can_access_corporate_features(user) and payload.get("organization_name"):
        create_organization(
            user,
            {
                "name": payload["organization_name"],
                "kyc_status": payload.get("kyc_status", Organization.KycStatus.PENDING),
                "role": payload.get("organization_role", OrganizationMembership.Role.ADMIN),
            },
        )

    token = issue_token(user)
    return user, token


def login_user(payload):
    phone_number = payload.get("phone_number")
    password = payload.get("password")
    if not phone_number or not password:
        raise ValidationError("phone_number and password are required.")
    user = authenticate(phone_number=phone_number, password=password)
    if not user:
        raise ValidationError("Invalid credentials.")
    token = issue_token(user)
    ensure_user_wallets(user)
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
    ]:
        if field_name in payload:
            setattr(user, field_name, bool(payload.get(field_name)))
            update_fields.append(field_name)

    if not update_fields:
        return user

    user.save(update_fields=update_fields + ["updated_at"])
    AuditLog.objects.create(actor=user, action="user.profile_updated", target_type="user", target_id=user.id)
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
    elif user.account_type not in {User.AccountType.CORPORATE, User.AccountType.SUPERADMIN}:
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
        kyc_status=payload.get("kyc_status", Organization.KycStatus.PENDING),
    )
    OrganizationMembership.objects.create(user=user, organization=organization, role=role)
    ensure_organization_wallet(organization)
    AuditLog.objects.create(actor=user, action="organization.created", target_type="organization", target_id=organization.id)
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
        metadata={"fields": update_fields},
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
        metadata={"created": created, "role": role},
    )
    return membership


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
        metadata={"fields": update_fields},
    )
    return membership


def deactivate_organization_member(actor, organization_id, membership_id):
    membership = update_organization_member(actor, organization_id, membership_id, {"is_active": False})
    AuditLog.objects.create(
        actor=actor,
        action="organization.member_removed",
        target_type="membership",
        target_id=membership.id,
    )
    return membership


def get_organization_for_user(user, organization_id, allowed_roles=None):
    if user.account_type == User.AccountType.SUPERADMIN:
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


def create_payee(user, payload):
    _validate_payee(payload)
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

    payee = Payee.objects.create(
        user=None if organization else user,
        organization=organization,
        payee_type=payload["payee_type"],
        label=payload["label"].strip(),
        account_reference=(payload.get("account_reference") or "").strip(),
        phone_number=(payload.get("phone_number") or "").strip(),
        paybill_number=(payload.get("paybill_number") or "").strip(),
        till_number=(payload.get("till_number") or "").strip(),
        bank_name=(payload.get("bank_name") or "").strip(),
        bank_code=(payload.get("bank_code") or "").strip(),
        account_number=(payload.get("account_number") or "").strip(),
        expense_category=(payload.get("expense_category") or "general").strip(),
        active=payload.get("active", True),
    )
    AuditLog.objects.create(actor=user, action="payee.created", target_type="payee", target_id=payee.id)
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
        "phone_number": payload.get("phone_number", payee.phone_number),
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
        metadata={"fields": update_fields},
    )
    return payee


def delete_payee(user, payee_id):
    payee = get_payee_for_user(user, payee_id)
    payee_identifier = payee.id
    payee.delete()
    AuditLog.objects.create(actor=user, action="payee.deleted", target_type="payee", target_id=payee_identifier)
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

    schedule = PaymentSchedule.objects.create(
        payee=payee,
        amount_minor=int(amount_minor),
        day_of_month=int(day_of_month),
        active=payload.get("active", True),
    )
    AuditLog.objects.create(actor=user, action="schedule.created", target_type="schedule", target_id=schedule.id)
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
        metadata={"fields": update_fields},
    )
    return schedule


def delete_schedule(user, schedule_id):
    schedule = get_schedule_for_user(user, schedule_id)
    schedule_identifier = schedule.id
    schedule.delete()
    AuditLog.objects.create(actor=user, action="schedule.deleted", target_type="schedule", target_id=schedule_identifier)
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
    return queryset.order_by("day_of_month", "payee__label")


def top_up_wallet(user, payload):
    amount_minor = int(payload.get("amount_minor") or 0)
    if amount_minor <= 0:
        raise ValidationError("amount_minor must be greater than 0.")

    requested_wallet_type = payload.get("wallet_type", Wallet.WalletType.PRIMARY)
    if requested_wallet_type not in Wallet.WalletType.values:
        raise ValidationError("Invalid wallet_type.")

    if payload.get("organization_id"):
        if requested_wallet_type != Wallet.WalletType.PRIMARY:
            raise ValidationError("Organization top-ups can only target the primary wallet.")
        organization = get_organization_for_user(
            user,
            payload["organization_id"],
            allowed_roles=[OrganizationMembership.Role.ADMIN, OrganizationMembership.Role.CHECKER],
        )
        wallet = ensure_organization_wallet(organization)
    else:
        primary_wallet, vault_wallet = ensure_user_wallets(user)
        if requested_wallet_type == Wallet.WalletType.VAULT:
            if not vault_wallet:
                raise ValidationError("Vault top-ups are only available for individual accounts.")
            wallet = vault_wallet
        else:
            wallet = primary_wallet

    wallet.available_balance_minor += amount_minor
    wallet.save(update_fields=["available_balance_minor", "updated_at"])
    WalletLedgerEntry.objects.create(
        wallet=wallet,
        entry_type=WalletLedgerEntry.EntryType.TOP_UP,
        amount_minor=amount_minor,
        balance_after_minor=wallet.available_balance_minor,
        reference=f"topup:{timezone.now().isoformat()}",
        metadata={
            "provider": payload.get("provider", "simulated"),
            "wallet_type": wallet.wallet_type,
        },
    )
    OutboxEvent.objects.create(
        topic="wallet.topup.completed",
        aggregate_type="wallet",
        aggregate_id=wallet.id,
        payload={"amount_minor": amount_minor, "wallet_type": wallet.wallet_type},
    )
    return wallet


def list_wallet_ledger(user, organization_id=None, filters=None):
    filters = filters or {}
    if organization_id:
        organization = get_organization_for_user(user, organization_id)
        wallets = Wallet.objects.filter(organization=organization)
    else:
        wallets = Wallet.objects.filter(user=user)

    if filters.get("wallet_type") in Wallet.WalletType.values:
        wallets = wallets.filter(wallet_type=filters["wallet_type"])

    queryset = WalletLedgerEntry.objects.select_related("wallet").filter(wallet__in=wallets)
    if filters.get("entry_type") in WalletLedgerEntry.EntryType.values:
        queryset = queryset.filter(entry_type=filters["entry_type"])
    return queryset.order_by("-created_at")


@transaction.atomic
def transfer_to_vault(user, payload):
    amount_minor = int(payload.get("amount_minor") or 0)
    if amount_minor <= 0:
        raise ValidationError("amount_minor must be greater than 0.")
    primary_wallet, vault_wallet = ensure_user_wallets(user)
    if not vault_wallet:
        raise ValidationError("Vaulting is only available for individual accounts.")
    if primary_wallet.available_balance_minor < amount_minor:
        raise ValidationError("Insufficient primary wallet balance.")

    primary_wallet.available_balance_minor -= amount_minor
    vault_wallet.available_balance_minor += amount_minor
    primary_wallet.save(update_fields=["available_balance_minor", "updated_at"])
    vault_wallet.save(update_fields=["available_balance_minor", "updated_at"])
    WalletLedgerEntry.objects.create(
        wallet=primary_wallet,
        entry_type=WalletLedgerEntry.EntryType.TRANSFER_TO_VAULT,
        amount_minor=-amount_minor,
        balance_after_minor=primary_wallet.available_balance_minor,
        reference=f"vault-transfer:{vault_wallet.id}",
    )
    WalletLedgerEntry.objects.create(
        wallet=vault_wallet,
        entry_type=WalletLedgerEntry.EntryType.TRANSFER_FROM_VAULT,
        amount_minor=amount_minor,
        balance_after_minor=vault_wallet.available_balance_minor,
        reference=f"vault-transfer:{primary_wallet.id}",
    )
    return primary_wallet, vault_wallet


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


def _calculate_fee(total_amount_minor, payment_mode):
    if payment_mode == PaymentBatch.PaymentMode.WALLET:
        return 0
    return max(0, total_amount_minor * SERVICE_FEE_BPS // 10000)


def _queue_instruction_dispatches(batch):
    for instruction in batch.instructions.filter(status=PaymentInstruction.Status.PENDING):
        OutboxEvent.objects.create(
            topic="payment.instruction.dispatch",
            aggregate_type="payment_instruction",
            aggregate_id=instruction.id,
            payload={"batch_id": str(batch.id)},
        )


def _mark_batch_success(batch, actor=None):
    batch.status = PaymentBatch.Status.SUCCEEDED
    batch.processed_at = timezone.now()
    batch.save(update_fields=["status", "processed_at", "updated_at"])
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
            {"batch_id": str(batch.id), "total_amount_minor": batch.total_amount_minor},
            scheduled_for=timezone.now(),
        )


def _mark_batch_failure(batch, actor, reason, status=PaymentBatch.Status.FAILED):
    batch.status = status
    batch.processed_at = timezone.now()
    batch.save(update_fields=["status", "processed_at", "updated_at"])
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
        batch.status = status
        batch.processed_at = timezone.now()
        batch.save(update_fields=["status", "processed_at", "updated_at"])


def _batch_notification_user(batch):
    return batch.user or batch.approved_by or batch.submitted_by


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
        schedules = schedules.filter(day_of_month=timezone.localdate().day)
    schedules = list(schedules)
    if not schedules:
        raise ValidationError("No active schedules found for payment.")

    batch = PaymentBatch.objects.create(
        batch_kind=PaymentBatch.BatchKind.INDIVIDUAL_MONTHLY,
        status=PaymentBatch.Status.PROCESSING,
        payment_mode=payment_mode,
        user=user,
        scheduled_for=timezone.localdate(),
        description="Individual pay-all execution",
    )
    for schedule in schedules:
        PaymentInstruction.objects.create(
            batch=batch,
            payee=schedule.payee,
            recipient_name=schedule.payee.label,
            recipient_type=schedule.payee.payee_type,
            destination=_build_destination_from_payee(schedule.payee),
            amount_minor=schedule.amount_minor,
            category=schedule.payee.expense_category,
            external_reference=schedule.payee.account_reference,
        )
    batch.recalculate_totals()
    batch.fee_amount_minor = _calculate_fee(batch.total_amount_minor, payment_mode)
    batch.save(update_fields=["fee_amount_minor", "updated_at"])
    settle_batch(batch, actor=user, simulate_collection=payload.get("simulate_collection", True))
    return batch


def run_due_wallet_autopayments(run_date=None):
    run_date = run_date or timezone.localdate()
    processed = 0
    users = User.objects.filter(
        default_payment_mode=User.PaymentMode.WALLET,
        payees__schedules__active=True,
        payees__active=True,
        payees__schedules__day_of_month=run_date.day,
    ).filter(account_type__in=[User.AccountType.INDIVIDUAL, User.AccountType.SUPERADMIN]).distinct()

    for user in users:
        already_processed = PaymentBatch.objects.filter(
            user=user,
            batch_kind=PaymentBatch.BatchKind.INDIVIDUAL_MONTHLY,
            scheduled_for=run_date,
            status__in=[PaymentBatch.Status.PROCESSING, PaymentBatch.Status.SUCCEEDED],
        ).exists()
        if already_processed:
            continue
        schedules = PaymentSchedule.objects.filter(
            payee__user=user,
            active=True,
            payee__active=True,
            day_of_month=run_date.day,
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
            "phone_number": (row.get("phone_number") or "").strip(),
            "paybill_number": (row.get("paybill_number") or "").strip(),
            "till_number": (row.get("till_number") or "").strip(),
            "bank_name": (row.get("bank_name") or "").strip(),
            "bank_code": (row.get("bank_code") or "").strip(),
            "account_number": (row.get("account_number") or "").strip(),
            "account_reference": (row.get("account_reference") or "").strip(),
        },
        amount_minor=amount_minor,
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
    batch.fee_amount_minor = _calculate_fee(batch.total_amount_minor, batch.payment_mode)
    batch.save(update_fields=["fee_amount_minor", "updated_at"])
    AuditLog.objects.create(actor=user, action="batch.uploaded", target_type="batch", target_id=batch.id)
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
    AuditLog.objects.create(actor=user, action="batch.submitted", target_type="batch", target_id=batch.id)
    return batch


@transaction.atomic
def approve_batch(user, batch_id):
    batch = get_object_or_404(
        PaymentBatch.objects.select_related("organization"),
        id=batch_id,
        batch_kind=PaymentBatch.BatchKind.CORPORATE_UPLOAD,
    )
    get_organization_for_user(
        user,
        batch.organization_id,
        allowed_roles=[OrganizationMembership.Role.ADMIN, OrganizationMembership.Role.CHECKER],
    )
    if batch.status != PaymentBatch.Status.PENDING_APPROVAL:
        raise ValidationError("Only pending approval batches can be approved.")

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
    if batch.submitted_by_id:
        queue_notifications_for_user(
            batch.submitted_by,
            "BATCH_APPROVED",
            {"batch_id": str(batch.id), "organization_id": str(batch.organization_id)},
            scheduled_for=timezone.now(),
        )
    settle_batch(batch, actor=user, simulate_collection=True)
    AuditLog.objects.create(actor=user, action="batch.approved", target_type="batch", target_id=batch.id)
    return batch


@transaction.atomic
def reject_batch(user, batch_id, payload):
    batch = get_object_or_404(
        PaymentBatch.objects.select_related("organization", "submitted_by"),
        id=batch_id,
        batch_kind=PaymentBatch.BatchKind.CORPORATE_UPLOAD,
    )
    get_organization_for_user(
        user,
        batch.organization_id,
        allowed_roles=[OrganizationMembership.Role.ADMIN, OrganizationMembership.Role.CHECKER],
    )
    if batch.status != PaymentBatch.Status.PENDING_APPROVAL:
        raise ValidationError("Only pending approval batches can be rejected.")

    rejection_reason = (payload.get("reason") or "").strip()
    if not rejection_reason:
        raise ValidationError("reason is required.")

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

    if batch.submitted_by_id:
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
        metadata={"reason": rejection_reason},
    )
    return batch


@transaction.atomic
def settle_batch(batch, actor, simulate_collection=True):
    if batch.payment_mode == PaymentBatch.PaymentMode.WALLET:
        if batch.organization_id:
            wallet = ensure_organization_wallet(batch.organization)
        else:
            wallet, _ = ensure_user_wallets(batch.user)
        required_total = batch.total_amount_minor + batch.fee_amount_minor
        if wallet.available_balance_minor < required_total:
            _mark_batch_failure(batch, actor, "insufficient_wallet_balance")
            raise ValidationError("Insufficient wallet balance.")

        wallet.available_balance_minor -= required_total
        wallet.save(update_fields=["available_balance_minor", "updated_at"])
        WalletLedgerEntry.objects.create(
            wallet=wallet,
            entry_type=WalletLedgerEntry.EntryType.DISBURSEMENT,
            amount_minor=-required_total,
            balance_after_minor=wallet.available_balance_minor,
            reference=f"batch:{batch.id}",
            metadata={"fee_amount_minor": batch.fee_amount_minor},
        )
        if provider_dispatch_enabled():
            batch.status = PaymentBatch.Status.PROCESSING
            batch.save(update_fields=["status", "updated_at"])
            _queue_instruction_dispatches(batch)
            return batch
    else:
        if not simulate_collection:
            batch.status = PaymentBatch.Status.PROCESSING
            batch.save(update_fields=["status", "updated_at"])
            OutboxEvent.objects.create(
                topic="collection.stk.requested",
                aggregate_type="payment_batch",
                aggregate_id=batch.id,
                payload={
                    "amount_minor": batch.total_amount_minor + batch.fee_amount_minor,
                    "phone_number": batch.user.phone_number if batch.user_id else "",
                },
            )
            return batch

    batch.instructions.update(status=PaymentInstruction.Status.SUCCEEDED, failure_reason="")
    _mark_batch_success(batch, actor=actor)
    return batch


def mark_batch_collection_complete(batch, provider_response):
    batch.metadata["collection_response"] = provider_response
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
    if summary["failed"] and summary["succeeded"]:
        batch.status = PaymentBatch.Status.PARTIAL
    elif summary["failed"]:
        batch.status = PaymentBatch.Status.FAILED
    else:
        batch.status = PaymentBatch.Status.SUCCEEDED
    batch.processed_at = timezone.now()
    batch.save(update_fields=["status", "processed_at", "updated_at"])
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
            {"batch_id": str(batch.id), "total_amount_minor": batch.total_amount_minor},
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


def record_instruction_success(instruction, provider_response, provider_reference=""):
    instruction.status = PaymentInstruction.Status.SUCCEEDED
    instruction.failure_reason = ""
    instruction.provider_reference = provider_reference or instruction.provider_reference
    instruction.provider_response = provider_response or {}
    instruction.save(
        update_fields=["status", "failure_reason", "provider_reference", "provider_response", "updated_at"]
    )
    finalize_batch_from_instructions(instruction.batch)
    return instruction


def record_instruction_failure(instruction, reason, provider_response=None):
    instruction.status = PaymentInstruction.Status.FAILED
    instruction.failure_reason = reason[:255]
    instruction.provider_response = provider_response or {}
    instruction.save(update_fields=["status", "failure_reason", "provider_response", "updated_at"])
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
    elif batch.user_id != user.id and user.account_type != User.AccountType.SUPERADMIN:
        raise PermissionDeniedError("You do not have access to this batch.")
    return batch


def list_batches(user, organization_id=None, filters=None):
    filters = filters or {}
    queryset = PaymentBatch.objects.all().order_by("-created_at")
    if organization_id:
        organization = get_organization_for_user(user, organization_id)
        queryset = queryset.filter(organization=organization)
    elif user.account_type == User.AccountType.SUPERADMIN:
        queryset = queryset
    elif user.account_type == User.AccountType.CORPORATE:
        memberships = OrganizationMembership.objects.filter(user=user, is_active=True).values_list("organization_id", flat=True)
        queryset = queryset.filter(organization_id__in=memberships)
    else:
        queryset = queryset.filter(user=user)

    if filters.get("status") in PaymentBatch.Status.values:
        queryset = queryset.filter(status=filters["status"])
    if filters.get("batch_kind") in PaymentBatch.BatchKind.values:
        queryset = queryset.filter(batch_kind=filters["batch_kind"])
    if filters.get("payment_mode") in PaymentBatch.PaymentMode.values:
        queryset = queryset.filter(payment_mode=filters["payment_mode"])
    return queryset


def build_dashboard(user):
    if can_access_individual_features(user):
        primary_wallet, vault_wallet = ensure_user_wallets(user)
        active_schedules = PaymentSchedule.objects.filter(payee__user=user, active=True, payee__active=True).select_related("payee")
        monthly_commitments = active_schedules.aggregate(total=Sum("amount_minor"))["total"] or 0
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
                    "day_of_month": schedule.day_of_month,
                    "category": schedule.payee.expense_category,
                }
                for schedule in active_schedules.order_by("day_of_month")[:10]
            ],
        }
        if user.account_type == User.AccountType.INDIVIDUAL:
            return {
                "account_type": user.account_type,
                **individual_dashboard,
            }

    organizations = []
    if can_access_corporate_features(user):
        membership_map = {}
        if user.account_type == User.AccountType.SUPERADMIN:
            organizations_qs = Organization.objects.all().order_by("name")
        else:
            memberships = OrganizationMembership.objects.filter(user=user, is_active=True).select_related("organization")
            membership_map = {membership.organization_id: membership.role for membership in memberships}
            organizations_qs = [membership.organization for membership in memberships]
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
                    "role": membership_map.get(organization.id, "SUPERADMIN"),
                    "kyc_status": organization.kyc_status,
                    "wallet_balance_minor": wallet.available_balance_minor,
                    "pending_approvals": pending_approvals,
                }
            )
    if user.account_type == User.AccountType.SUPERADMIN:
        return {
            "account_type": user.account_type,
            "individual": individual_dashboard,
            "organizations": organizations,
        }
    if user.account_type == User.AccountType.CORPORATE:
        return {"account_type": user.account_type, "organizations": organizations}

    if can_access_individual_features(user):
        upcoming = [
            {
                "schedule_id": str(schedule.id),
                "payee_label": schedule.payee.label,
                "amount_minor": schedule.amount_minor,
                "day_of_month": schedule.day_of_month,
                "category": schedule.payee.expense_category,
            }
            for schedule in active_schedules.order_by("day_of_month")[:10]
        ]
        return {
            "account_type": user.account_type,
            "wallet_balance_minor": primary_wallet.available_balance_minor,
            "vault_balance_minor": vault_wallet.available_balance_minor if vault_wallet else 0,
            "monthly_commitments_minor": monthly_commitments,
            "missing_capital_minor": max(monthly_commitments - primary_wallet.available_balance_minor, 0),
            "upcoming_schedules": upcoming,
        }
    return {"account_type": user.account_type, "organizations": organizations}


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
    else:
        queryset = queryset.filter(batch__user=user)
    for instruction in queryset:
        yield {
            "instruction_id": str(instruction.id),
            "batch_id": str(instruction.batch_id),
            "recipient_name": instruction.recipient_name,
            "recipient_type": instruction.recipient_type,
            "amount_minor": instruction.amount_minor,
            "status": instruction.status,
            "category": instruction.category,
            "scheduled_for": instruction.batch.scheduled_for.isoformat(),
            "created_at": instruction.created_at.isoformat(),
        }
