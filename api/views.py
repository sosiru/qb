import csv
import hashlib
import json
import logging
import uuid

from django.db import IntegrityError, models, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.utils import timezone

from audit.models import AuditLog
from base.models import (
    IdempotencyRecord,
    OrganizationInvite,
    OrganizationMembership,
    OutboxEvent,
    PaymentBatch,
    PaymentInstruction,
)
from ledger.models import Account, Transaction as LedgerTransactionRecord
from ledger.services import PaymentInterface
from base.services import (
    DomainError,
    ValidationError,
    accept_organization_invite,
    add_organization_member,
    approve_batch,
    build_dashboard,
    create_organization,
    create_payee,
    create_schedule,
    deactivate_organization_member,
    delete_payee,
    delete_schedule,
    build_approval_queue,
    build_transaction_summary,
    get_batch_for_user,
    get_organization_for_user,
    get_payee_for_user,
    get_schedule_for_user,
    invite_organization_member,
    quick_pay,
    ledger_description,
    list_batches,
    list_banks,
    list_expense_categories,
    list_organizations,
    list_organization_members,
    list_payees,
    list_payee_presets,
    list_schedules,
    list_wallet_ledger,
    OtpRequired,
    pay_individual_due_items,
    reject_batch,
    submit_batch_for_approval,
    top_up_wallet,
    transfer_to_vault,
    update_organization,
    update_organization_member,
    update_payee,
    update_schedule,
    withdraw_to_mpesa,
    upload_corporate_batch,
)
from eusers.models import User
from eusers.services import change_user_password, login_user, register_user, update_user_profile
from reports.models import ReportExport
from reports.services import export_transactions_csv_rows, generate_transaction_statement_pdf, record_transaction_export
from eusers.utils import normalize_phone_number

from .auth import api_view, get_request_data, json_error, require_auth
from .services import issue_integration_api_key, list_integration_api_keys, revoke_integration_api_key

logger = logging.getLogger(__name__)


IDEMPOTENT_MUTATION_TTL_SECONDS = 60


def _client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _request_audit_metadata(request, extra=None):
    metadata = {
        "method": request.method,
        "path": request.path,
        "ip_address": _client_ip(request),
        "user_agent": request.META.get("HTTP_USER_AGENT", ""),
    }
    if extra:
        metadata.update(extra)
    return metadata


def _audit_auth_event(request, action, phone_number, status_code, actor=None, reason=""):
    target_id = actor.id if actor else uuid.uuid4()
    AuditLog.objects.create(
        actor=actor,
        action=action,
        target_type="auth",
        target_id=target_id,
        metadata=_request_audit_metadata(
            request,
            {
                "phone_number": phone_number or "unknown",
                "status_code": status_code,
                "reason": reason,
            },
        ),
    )


def _request_body_hash(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (TypeError, ValueError, UnicodeDecodeError):
        payload = request.body.decode("utf-8", errors="replace")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_idempotency(view_func):
    def wrapped(request, *args, **kwargs):
        if request.method not in {"POST", "PATCH", "DELETE"}:
            return view_func(request, *args, **kwargs)

        key = (request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key") or "").strip()
        if not key:
            return json_error("Idempotency-Key header is required for this mutating payment request.", status=400)
        if len(key) > 128:
            return json_error("Idempotency-Key must be 128 characters or fewer.", status=400)

        request_hash = _request_body_hash(request)
        user = getattr(request, "api_user", None)
        try:
            with transaction.atomic():
                record, created = IdempotencyRecord.objects.select_for_update().get_or_create(
                    user=user,
                    key=key,
                    method=request.method,
                    path=request.path,
                    defaults={
                        "request_hash": request_hash,
                        "status": IdempotencyRecord.Status.PROCESSING,
                        "locked_until": timezone.now() + timezone.timedelta(seconds=IDEMPOTENT_MUTATION_TTL_SECONDS),
                    },
                )
        except IntegrityError:
            record = IdempotencyRecord.objects.get(user=user, key=key, method=request.method, path=request.path)
            created = False

        if not created:
            if record.request_hash != request_hash:
                return json_error("Idempotency-Key was reused with a different request body.", status=409)
            if record.status == IdempotencyRecord.Status.COMPLETED:
                return JsonResponse(record.response_body, status=record.response_status or 200)
            if record.status == IdempotencyRecord.Status.PROCESSING and (
                not record.locked_until or record.locked_until > timezone.now()
            ):
                return json_error("Request with this Idempotency-Key is still processing.", status=409)
            record.status = IdempotencyRecord.Status.PROCESSING
            record.locked_until = timezone.now() + timezone.timedelta(seconds=IDEMPOTENT_MUTATION_TTL_SECONDS)
            record.last_error = "" if hasattr(record, "last_error") else ""
            record.save(update_fields=["status", "locked_until", "updated_at"])

        response = view_func(request, *args, **kwargs)
        try:
            body = json.loads(response.content.decode("utf-8") or "{}")
        except (TypeError, ValueError, UnicodeDecodeError):
            body = {"raw": response.content.decode("utf-8", errors="replace")}
        record.status = IdempotencyRecord.Status.COMPLETED if response.status_code < 500 else IdempotencyRecord.Status.FAILED
        record.response_status = response.status_code
        record.response_body = body
        record.locked_until = None
        record.save(update_fields=["status", "response_status", "response_body", "locked_until", "updated_at"])
        return response

    return wrapped


def _serialize_user(user):
    return {
        "id": str(user.id),
        "phone_number": user.phone_number,
        "email": user.email,
        "full_name": user.full_name,
        "account_type": user.account_type,
        "default_payment_mode": user.default_payment_mode,
        "sms_notifications_enabled": user.sms_notifications_enabled,
        "email_notifications_enabled": user.email_notifications_enabled,
        "push_notifications_enabled": user.push_notifications_enabled,
        "mfa_enabled": user.mfa_enabled,
        "payouts_require_owner_approval": user.payouts_require_owner_approval,
        "mpesa_withdrawal_phone": user.mpesa_withdrawal_phone,
        "is_phone_verified": user.is_phone_verified,
    }


def _serialize_wallet(wallet):
    return {
        "id": str(wallet.id),
        "owner_type": wallet.owner_type,
        "wallet_type": wallet.wallet_type,
        "currency": wallet.currency,
        "available_balance_minor": wallet.available_balance_minor,
        "current_balance_minor": getattr(wallet, "current_balance_minor", wallet.available_balance_minor),
        "reserved_balance_minor": getattr(wallet, "reserved_balance_minor", 0),
        "uncleared_balance_minor": getattr(wallet, "uncleared_balance_minor", 0),
        "user_id": str(wallet.user_id) if wallet.user_id else None,
        "organization_id": str(wallet.organization_id) if wallet.organization_id else None,
        "label": f"{wallet.wallet_type.title()} wallet",
    }


def _serialize_payee(payee):
    destination_label = (
        payee.paybill_number
        or payee.till_number
        or payee.phone_number
        or payee.account_number
        or payee.bank_name
        or ""
    )
    return {
        "id": str(payee.id),
        "preset_id": str(payee.preset_id) if payee.preset_id else None,
        "label": payee.label,
        "payee_type": payee.payee_type,
        "payee_type_label": payee.get_payee_type_display(),
        "account_reference": payee.account_reference,
        "phone_number": payee.phone_number,
        "paybill_number": payee.paybill_number,
        "till_number": payee.till_number,
        "bank_name": payee.bank_name,
        "bank_code": payee.bank_code,
        "account_number": payee.account_number,
        "expense_category": payee.expense_category,
        "active": payee.active,
        "status": "ACTIVE" if payee.active else "INACTIVE",
        "destination_label": destination_label,
        "organization_id": str(payee.organization_id) if payee.organization_id else None,
    }


def _serialize_payee_preset(preset):
    return {
        "id": str(preset.id),
        "label": preset.label,
        "payee_type": preset.payee_type,
        "paybill_number": preset.paybill_number,
        "till_number": preset.till_number,
        "expense_category": preset.expense_category,
        "active": preset.active,
    }


def _serialize_expense_category(category):
    return {
        "id": str(category.id),
        "name": category.name,
        "description": category.description,
        "active": category.active,
    }


def _serialize_bank(bank):
    return {
        "id": str(bank.id),
        "name": bank.name,
        "code": bank.code,
        "active": bank.active,
    }


def _serialize_schedule(schedule):
    fee_amount_minor = (schedule.amount_minor * 200) // 10000
    return {
        "id": str(schedule.id),
        "payee_id": str(schedule.payee_id),
        "payee_label": schedule.payee.label,
        "payee_type": schedule.payee.payee_type,
        "amount_minor": schedule.amount_minor,
        "fee_amount_minor": fee_amount_minor,
        "gross_amount_minor": schedule.amount_minor + fee_amount_minor,
        "day_of_month": schedule.day_of_month,
        "interval_months": schedule.interval_months,
        "cadence_label": "Every month" if schedule.interval_months == 1 else f"Every {schedule.interval_months} months",
        "next_due_date": schedule.next_due_date.isoformat(),
        "requires_approval": schedule.requires_approval,
        "active": schedule.active,
        "status": "ACTIVE" if schedule.active else "INACTIVE",
        "category": schedule.payee.expense_category,
    }


def _serialize_batch(batch):
    return {
        "id": str(batch.id),
        "batch_kind": batch.batch_kind,
        "status": batch.status,
        "payment_mode": batch.payment_mode,
        "scheduled_for": batch.scheduled_for.isoformat(),
        "description": batch.description,
        "source_file_name": batch.source_file_name,
        "total_amount_minor": batch.total_amount_minor,
        "fee_amount_minor": batch.fee_amount_minor,
        "gross_amount_minor": batch.total_amount_minor + batch.fee_amount_minor,
        "organization_id": str(batch.organization_id) if batch.organization_id else None,
        "user_id": str(batch.user_id) if batch.user_id else None,
        "instruction_count": batch.instructions.count(),
        "submitted_by_name": batch.submitted_by.full_name if batch.submitted_by_id else None,
        "approved_by_name": batch.approved_by.full_name if batch.approved_by_id else None,
        "submitted_at": batch.submitted_at.isoformat() if batch.submitted_at else None,
        "approved_at": batch.approved_at.isoformat() if batch.approved_at else None,
        "processed_at": batch.processed_at.isoformat() if batch.processed_at else None,
        "metadata": batch.metadata,
    }


def _serialize_integration_api_key(api_key):
    return {
        "id": str(api_key.id),
        "name": api_key.name,
        "key_prefix": api_key.key_prefix,
        "scopes": api_key.scopes,
        "scope_label": ", ".join(api_key.scopes),
        "is_active": api_key.is_active,
        "status": "ACTIVE" if api_key.is_active else "REVOKED",
        "user_id": str(api_key.user_id),
        "organization_id": str(api_key.organization_id) if api_key.organization_id else None,
        "created_by_id": str(api_key.created_by_id) if api_key.created_by_id else None,
        "last_used_at": api_key.last_used_at.isoformat() if api_key.last_used_at else None,
        "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
        "revoked_at": api_key.revoked_at.isoformat() if api_key.revoked_at else None,
        "created_at": api_key.created_at.isoformat(),
    }


def _serialize_organization(organization, role=None):
    return {
        "id": str(organization.id),
        "name": organization.name,
        "slug": organization.slug,
        "registration_number": organization.registration_number,
        "tax_identification_document": organization.tax_identification_document.url if organization.tax_identification_document else "",
        "business_registration_certificate": organization.business_registration_certificate.url if organization.business_registration_certificate else "",
        "kyc_status": organization.kyc_status,
        "default_currency": organization.default_currency,
        "push_notifications_enabled": organization.push_notifications_enabled,
        "sms_notifications_enabled": organization.sms_notifications_enabled,
        "role": role,
    }


def _serialize_membership(membership):
    return {
        "id": str(membership.id),
        "organization_id": str(membership.organization_id),
        "user_id": str(membership.user_id),
        "full_name": membership.user.full_name,
        "phone_number": membership.user.phone_number,
        "email": membership.user.email,
        "role": membership.role,
        "is_active": membership.is_active,
        "status": "ACTIVE" if membership.is_active else "INACTIVE",
        "last_active_at": membership.user.last_login.isoformat() if membership.user.last_login else None,
    }


def _serialize_organization_invite(invite, invite_link=None):
    payload = {
        "id": str(invite.id),
        "organization_id": str(invite.organization_id),
        "email": invite.email,
        "role": invite.role,
        "status": invite.status,
        "expires_at": invite.expires_at.isoformat(),
        "created_at": invite.created_at.isoformat(),
        "invited_by_id": str(invite.invited_by_id) if invite.invited_by_id else None,
    }
    if invite_link:
        payload["invite_link"] = invite_link
    return payload


def _serialize_ledger_entry(entry):
    metadata = entry.metadata or {}
    fee_amount_minor = int(metadata.get("fee_amount_minor") or 0)
    base_amount_minor = int(metadata.get("base_amount_minor") or entry.amount_minor - fee_amount_minor)
    gross_amount_minor = int(metadata.get("gross_amount_minor") or entry.amount_minor)
    wallet = entry.account
    entry_type = metadata.get("entry_type") or entry.transaction_type.name
    amount_minor = -entry.amount_minor if entry.direction == LedgerTransactionRecord.Direction.PAY_OUT else entry.amount_minor
    return {
        "id": str(entry.id),
        "wallet_id": str(wallet.id),
        "wallet_type": wallet.wallet_type,
        "entry_type": entry_type,
        "amount_minor": amount_minor,
        "base_amount_minor": base_amount_minor,
        "fee_amount_minor": fee_amount_minor,
        "gross_amount_minor": gross_amount_minor,
        "balance_after_minor": entry.balance_after_minor,
        "reference": entry.internal_reference,
        "description": entry.description or ledger_description(entry_type, metadata),
        "metadata": metadata,
        "status": entry.status,
        "created_at": entry.created_at.isoformat(),
    }


def _serialize_instruction(instruction):
    return {
        "id": str(instruction.id),
        "payee_id": str(instruction.payee_id) if instruction.payee_id else None,
        "recipient_name": instruction.recipient_name,
        "recipient_type": instruction.recipient_type,
        "recipient_type_label": instruction.get_recipient_type_display(),
        "destination": instruction.destination,
        "amount_minor": instruction.amount_minor,
        "fee_amount_minor": instruction.fee_amount_minor,
        "gross_amount_minor": instruction.amount_minor + instruction.fee_amount_minor,
        "category": instruction.category,
        "external_reference": instruction.external_reference,
        "microservice_request_id": instruction.microservice_request_id,
        "status": instruction.status,
        "failure_reason": instruction.failure_reason,
    }


def _serialize_report_export(export):
    return {
        "id": str(export.id),
        "export_type": export.export_type,
        "file_format": export.file_format,
        "status": export.status,
        "file_name": export.file_name,
        "organization_id": str(export.organization_id) if export.organization_id else None,
        "requested_by_id": str(export.requested_by_id),
        "requested_at": export.created_at.isoformat(),
        "generated_at": export.generated_at.isoformat() if export.generated_at else None,
        "filters": export.filters,
    }


def _parse_bool_param(value):
    if value is None:
        return None
    value = str(value).strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    return None


def _handle_domain_error(exc):
    status = 400
    if exc.__class__.__name__ == "PermissionDeniedError":
        status = 403
    return json_error(str(exc), status=status)


@api_view
@require_auth
def health_view(request):
    return JsonResponse({"status": "ok", "service": "quick-bundl-backend"})


@api_view
def register_view(request):
    logger.info("api.auth.register.request method=%s", request.method)
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    payload = {}
    try:
        data = get_request_data(request)
        for file_key in ("tax_identification_document", "business_registration_certificate"):
            if file_key in request.FILES:
                data[file_key] = request.FILES[file_key]
        payload = data
        user, token = register_user(payload)
    except DomainError as exc:
        _audit_auth_event(
            request,
            "auth.register.failed",
            normalize_phone_number(payload.get("phone_number")) if payload.get("phone_number") else "",
            400,
            reason=str(exc),
        )
        return _handle_domain_error(exc)
    except Exception as exc:
        _audit_auth_event(
            request,
            "auth.register.failed",
            normalize_phone_number(payload.get("phone_number")) if payload.get("phone_number") else "",
            400,
            reason=str(exc),
        )
        return json_error(str(exc), status=400)
    _audit_auth_event(request, "auth.register.succeeded", user.phone_number, 201, actor=user)
    return JsonResponse({"user": _serialize_user(user), "token": token}, status=201)


@api_view
def login_view(request):
    logger.info("api.auth.login.request method=%s", request.method)
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    payload = {}
    try:
        data = get_request_data(request)
        payload = data
        user, token = login_user(payload)
    except OtpRequired as exc:
        actor = User.objects.filter(phone_number=exc.phone_number).first()
        _audit_auth_event(request, "auth.login.otp_required", exc.phone_number, 202, actor=actor)
        payload = {
            "otp_required": True,
            "message": str(exc),
            "phone_number": exc.phone_number,
            "otp_expires_in_seconds": exc.expires_in_seconds or 600,
            "retry_after_seconds": exc.retry_after_seconds or 60,
        }
        if exc.dev_otp:
            payload["dev_otp"] = exc.dev_otp
        return JsonResponse(payload, status=202)
    except DomainError as exc:
        attempted_phone = normalize_phone_number(payload.get("phone_number")) if payload.get("phone_number") else ""
        _audit_auth_event(request, "auth.login.failed", attempted_phone, 400, reason=str(exc))
        return _handle_domain_error(exc)
    _audit_auth_event(request, "auth.login.succeeded", user.phone_number, 200, actor=user)
    return JsonResponse({"user": _serialize_user(user), "token": token})


@api_view
@require_auth
def me_view(request):
    try:
        if request.method == "GET":
            return JsonResponse({"user": _serialize_user(request.api_user)})
        if request.method == "PATCH":
            data = get_request_data(request)
            payload = data
            user = update_user_profile(request.api_user, payload)
            return JsonResponse({"user": _serialize_user(user)})
        return json_error("Method not allowed.", status=405)
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def change_password_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        data = get_request_data(request)
        payload = data
        change_user_password(request.api_user, payload)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"status": "ok"})


@api_view
@require_auth
def dashboard_view(request):
    return JsonResponse({"dashboard": build_dashboard(request.api_user, request.GET.get("organization_id"))})


@api_view
@require_auth
def account_contexts_view(request):
    user = request.api_user
    contexts = []
    if user.account_type in {User.AccountType.INDIVIDUAL, User.AccountType.SUPERADMIN}:
        contexts.append(
            {
                "id": f"user:{user.id}",
                "kind": "INDIVIDUAL",
                "label": user.full_name or user.phone_number,
                "user_id": str(user.id),
                "organization_id": None,
                "role": "INDIVIDUAL",
                "can_switch": True,
            }
        )

    memberships = OrganizationMembership.objects.select_related("organization").filter(user=user, is_active=True).order_by("organization__name")
    for membership in memberships:
        contexts.append(
            {
                "id": f"org:{membership.organization_id}",
                "kind": "CORPORATE",
                "label": membership.organization.name,
                "user_id": str(user.id),
                "organization_id": str(membership.organization_id),
                "role": membership.role,
                "can_switch": True,
            }
        )

    if user.account_type in {User.AccountType.SUPERADMIN, User.AccountType.SERVICE_PROVIDER}:
        for organization in list_organizations(user):
            context_id = f"support-org:{organization.id}"
            if not any(ctx["organization_id"] == str(organization.id) for ctx in contexts):
                contexts.append(
                    {
                        "id": context_id,
                        "kind": "CORPORATE",
                        "label": f"{organization.name} · support",
                        "user_id": str(user.id),
                        "organization_id": str(organization.id),
                        "role": "ADMIN",
                        "can_switch": True,
                    }
                )
        if user.account_type == User.AccountType.SUPERADMIN:
            for target in User.objects.filter(account_type=User.AccountType.INDIVIDUAL, is_active=True).order_by("full_name", "phone_number")[:500]:
                contexts.append(
                    {
                        "id": f"support-user:{target.id}",
                        "kind": "INDIVIDUAL",
                        "label": f"{target.full_name or target.phone_number} · support",
                        "user_id": str(target.id),
                        "organization_id": None,
                        "role": "INDIVIDUAL",
                        "can_switch": True,
                    }
                )

    can_switch = len(contexts) > 1
    if not can_switch:
        for context in contexts:
            context["can_switch"] = False
    return JsonResponse({"contexts": contexts, "can_switch": can_switch})


@api_view
@require_auth
def organizations_view(request):
    try:
        if request.method == "GET":
            organizations = list_organizations(
                request.api_user,
                {
                    "q": request.GET.get("q"),
                    "kyc_status": request.GET.get("kyc_status"),
                },
            )
            return JsonResponse({"organizations": [_serialize_organization(org) for org in organizations]})
        if request.method == "POST":
            data = get_request_data(request)
            payload = data
            organization = create_organization(request.api_user, payload)
            return JsonResponse(
                {
                    "organization": _serialize_organization(organization),
                },
                status=201,
            )
        return json_error("Method not allowed.", status=405)
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def organization_detail_view(request, organization_id):
    try:
        organization = get_organization_for_user(request.api_user, organization_id)
        if request.method == "GET":
            role = None
            membership = OrganizationMembership.objects.filter(
                organization_id=organization_id,
                user=request.api_user,
                is_active=True,
            ).first()
            if membership:
                role = membership.role
            elif request.api_user.account_type in {"SUPERADMIN", "SERVICE_PROVIDER"}:
                role = request.api_user.account_type
            return JsonResponse({"organization": _serialize_organization(organization, role=role)})
        if request.method == "PATCH":
            data = get_request_data(request)
            payload = data
            organization = update_organization(request.api_user, organization_id, payload)
            return JsonResponse({"organization": _serialize_organization(organization)})
        return json_error("Method not allowed.", status=405)
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def organization_members_view(request, organization_id):
    try:
        if request.method == "GET":
            memberships = list_organization_members(
                request.api_user,
                organization_id,
                {
                    "role": request.GET.get("role"),
                    "is_active": _parse_bool_param(request.GET.get("is_active")),
                    "q": request.GET.get("q"),
                },
            )
            return JsonResponse({"memberships": [_serialize_membership(membership) for membership in memberships]})
        if request.method == "POST":
            data = get_request_data(request)
            payload = data
            membership = add_organization_member(request.api_user, organization_id, payload)
            return JsonResponse({"membership": _serialize_membership(membership)}, status=201)
        return json_error("Method not allowed.", status=405)
    except Http404:
        return json_error("User not found.", status=404)
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def organization_invites_view(request, organization_id):
    try:
        organization = get_organization_for_user(request.api_user, organization_id, allowed_roles=[OrganizationMembership.Role.ADMIN])
        if request.method == "GET":
            invites = OrganizationInvite.objects.filter(organization=organization).order_by("-created_at")[:100]
            return JsonResponse({"invites": [_serialize_organization_invite(invite) for invite in invites]})
        if request.method == "POST":
            data = get_request_data(request)
            payload = data
            invite, invite_link = invite_organization_member(request.api_user, organization_id, payload)
            return JsonResponse({"invite": _serialize_organization_invite(invite, invite_link=invite_link)}, status=201)
        return json_error("Method not allowed.", status=405)
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def organization_invite_accept_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        data = get_request_data(request)
        payload = data
        user, token, membership = accept_organization_invite(payload)
        return JsonResponse(
            {
                "user": _serialize_user(user),
                "token": token,
                "membership": _serialize_membership(membership),
            },
            status=201,
        )
    except Http404:
        return json_error("Invite not found.", status=404)
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def organization_member_detail_view(request, organization_id, membership_id):
    try:
        if request.method == "PATCH":
            data = get_request_data(request)
            payload = data
            membership = update_organization_member(request.api_user, organization_id, membership_id, payload)
            return JsonResponse({"membership": _serialize_membership(membership)})
        if request.method == "DELETE":
            membership = deactivate_organization_member(request.api_user, organization_id, membership_id)
            return JsonResponse({"membership": _serialize_membership(membership)})
        return json_error("Method not allowed.", status=405)
    except Http404:
        return json_error("Membership not found.", status=404)
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def payee_presets_view(request):
    try:
        if request.method != "GET":
            return json_error("Method not allowed.", status=405)
        presets = list_payee_presets(
            {
                "q": request.GET.get("q"),
                "payee_type": request.GET.get("payee_type"),
                "active": _parse_bool_param(request.GET.get("active")),
            }
        )
        return JsonResponse({"presets": [_serialize_payee_preset(preset) for preset in presets]})
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def expense_categories_view(request):
    try:
        if request.method != "GET":
            return json_error("Method not allowed.", status=405)
        categories = list_expense_categories(
            {
                "q": request.GET.get("q"),
                "active": _parse_bool_param(request.GET.get("active")),
            }
        )
        return JsonResponse({"categories": [_serialize_expense_category(category) for category in categories]})
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def banks_view(request):
    try:
        if request.method != "GET":
            return json_error("Method not allowed.", status=405)
        banks = list_banks(
            {
                "q": request.GET.get("q"),
                "active": _parse_bool_param(request.GET.get("active")),
            }
        )
        return JsonResponse({"banks": [_serialize_bank(bank) for bank in banks]})
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def payees_view(request):
    try:
        if request.method == "GET":
            payees = list_payees(
                request.api_user,
                request.GET.get("organization_id"),
                {
                    "q": request.GET.get("q"),
                    "payee_type": request.GET.get("payee_type"),
                    "active": _parse_bool_param(request.GET.get("active")),
                },
            )
            return JsonResponse({"payees": [_serialize_payee(payee) for payee in payees]})
        if request.method == "POST":
            data = get_request_data(request)
            payload = data
            payee = create_payee(request.api_user, payload)
            return JsonResponse({"payee": _serialize_payee(payee)}, status=201)
        return json_error("Method not allowed.", status=405)
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def payee_detail_view(request, payee_id):
    try:
        if request.method == "GET":
            payee = get_payee_for_user(request.api_user, payee_id)
            return JsonResponse({"payee": _serialize_payee(payee)})
        if request.method == "PATCH":
            data = get_request_data(request)
            payload = data
            payee = update_payee(request.api_user, payee_id, payload)
            return JsonResponse({"payee": _serialize_payee(payee)})
        if request.method == "DELETE":
            delete_payee(request.api_user, payee_id)
            return JsonResponse({"status": "deleted"})
        return json_error("Method not allowed.", status=405)
    except Http404:
        return json_error("Payee not found.", status=404)
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def schedules_view(request):
    try:
        if request.method == "GET":
            schedules = list_schedules(
                request.api_user,
                request.GET.get("organization_id"),
                {
                    "q": request.GET.get("q"),
                    "category": request.GET.get("category"),
                    "active": _parse_bool_param(request.GET.get("active")),
                },
            )
            return JsonResponse({"schedules": [_serialize_schedule(schedule) for schedule in schedules]})
        if request.method == "POST":
            data = get_request_data(request)
            payload = data
            schedule = create_schedule(request.api_user, payload)
            return JsonResponse({"schedule": _serialize_schedule(schedule)}, status=201)
        return json_error("Method not allowed.", status=405)
    except Http404:
        return json_error("Payee not found.", status=404)
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def schedule_detail_view(request, schedule_id):
    try:
        if request.method == "GET":
            schedule = get_schedule_for_user(request.api_user, schedule_id)
            return JsonResponse({"schedule": _serialize_schedule(schedule)})
        if request.method == "PATCH":
            data = get_request_data(request)
            payload = data
            schedule = update_schedule(request.api_user, schedule_id, payload)
            return JsonResponse({"schedule": _serialize_schedule(schedule)})
        if request.method == "DELETE":
            delete_schedule(request.api_user, schedule_id)
            return JsonResponse({"status": "deleted"})
        return json_error("Method not allowed.", status=405)
    except Http404:
        return json_error("Schedule not found.", status=404)
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
@require_idempotency
def wallet_topup_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        data = get_request_data(request)
        payload = data
        logger.info(
            "api.wallet_topup.request user_id=%s phone=%s payload=%s",
            request.api_user.id,
            request.api_user.phone_number,
            payload,
        )
        wallet = top_up_wallet(request.api_user, payload)
    except DomainError as exc:
        logger.warning(
            "api.wallet_topup.domain_error user_id=%s error=%s",
            getattr(request.api_user, "id", None),
            exc,
        )
        return _handle_domain_error(exc)
    logger.info(
        "api.wallet_topup.success user_id=%s wallet_id=%s available_balance_minor=%s uncleared_balance_minor=%s",
        request.api_user.id,
        wallet.id,
        getattr(wallet, "available_balance_minor", None),
        getattr(wallet, "uncleared_balance_minor", None),
    )
    return JsonResponse({"wallet": _serialize_wallet(wallet)})


@api_view
@require_auth
@require_idempotency
def wallet_vault_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        data = get_request_data(request)
        payload = data
        primary_wallet, vault_wallet = transfer_to_vault(request.api_user, payload)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"primary_wallet": _serialize_wallet(primary_wallet), "vault_wallet": _serialize_wallet(vault_wallet)})


@api_view
@require_auth
@require_idempotency
def wallet_withdrawal_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        data = get_request_data(request)
        payload = data
        wallet, entry = withdraw_to_mpesa(request.api_user, payload)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"wallet": _serialize_wallet(wallet), "entry": _serialize_ledger_entry(entry)}, status=201)


@api_view
@require_auth
def wallet_summary_view(request):
    wallets = Account.objects.filter(user=request.api_user).order_by("account_kind")
    if request.GET.get("organization_id"):
        organization_id = request.GET["organization_id"]
        try:
            organization = get_organization_for_user(request.api_user, organization_id)
        except DomainError as exc:
            return _handle_domain_error(exc)
        wallets = Account.objects.filter(organization=organization).order_by("account_kind")
    return JsonResponse({"wallets": [_serialize_wallet(wallet) for wallet in wallets]})


@api_view
@require_auth
def wallet_ledger_view(request):
    try:
        entries = list_wallet_ledger(
            request.api_user,
            request.GET.get("organization_id"),
            {
                "wallet_type": request.GET.get("wallet_type"),
                "entry_type": request.GET.get("entry_type"),
            },
        )
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"entries": [_serialize_ledger_entry(entry) for entry in entries]})


@api_view
def payment_webhook_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        payload = get_request_data(request)
        payment_request = PaymentInterface().handle_webhook(payload)
    except Exception as exc:
        logger.warning("payment.webhook.failed error=%s", exc, exc_info=True)
        return json_error(str(exc), status=400)
    return JsonResponse(
        {
            "status": payment_request.status,
            "originator_ref": payment_request.originator_ref,
            "transaction_id": str(payment_request.transaction_id),
        }
    )


@api_view
@require_auth
@require_idempotency
def pay_all_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        data = get_request_data(request)
        payload = data
        batch = pay_individual_due_items(request.api_user, payload)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"batch": _serialize_batch(batch)})


@api_view
@require_auth
@require_idempotency
def quick_pay_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        data = get_request_data(request)
        payload = data
        batch = quick_pay(request.api_user, payload)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"batch": _serialize_batch(batch)})


@api_view
@require_auth
@require_idempotency
def corporate_batch_upload_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        data = get_request_data(request)
        payload = data
        batch = upload_corporate_batch(request.api_user, payload)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"batch": _serialize_batch(batch)}, status=201)


@api_view
@require_auth
@require_idempotency
def corporate_batch_submit_view(request, batch_id):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        batch = submit_batch_for_approval(request.api_user, batch_id)
    except Http404:
        return json_error("Batch not found.", status=404)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"batch": _serialize_batch(batch)})


@api_view
@require_auth
@require_idempotency
def corporate_batch_approve_view(request, batch_id):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        batch = approve_batch(request.api_user, batch_id)
    except Http404:
        return json_error("Batch not found.", status=404)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"batch": _serialize_batch(batch)})


@api_view
@require_auth
@require_idempotency
def corporate_batch_reject_view(request, batch_id):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        data = get_request_data(request)
        payload = data
        batch = reject_batch(request.api_user, batch_id, payload)
    except Http404:
        return json_error("Batch not found.", status=404)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"batch": _serialize_batch(batch)})


@api_view
@require_auth
def batches_view(request):
    try:
        batches = list_batches(
            request.api_user,
            request.GET.get("organization_id"),
            {
                "status": request.GET.get("status"),
                "batch_kind": request.GET.get("batch_kind"),
                "payment_mode": request.GET.get("payment_mode"),
            },
        )
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"batches": [_serialize_batch(batch) for batch in batches]})


@api_view
@require_auth
def approvals_view(request):
    try:
        queue = build_approval_queue(request.api_user, request.GET.get("organization_id"))
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"approvals": queue})


@api_view
@require_auth
def batch_detail_view(request, batch_id):
    try:
        batch = get_batch_for_user(request.api_user, batch_id)
    except Http404:
        return json_error("Batch not found.", status=404)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse(
        {
            "batch": _serialize_batch(batch),
            "instructions": [_serialize_instruction(instruction) for instruction in batch.instructions.all().order_by("created_at")],
        }
    )


@api_view
@require_auth
def transaction_report_view(request):
    try:
        organization = None
        organization_id = request.GET.get("organization_id")
        if organization_id:
            organization = get_organization_for_user(request.api_user, organization_id)
        rows = list(export_transactions_csv_rows(request.api_user, organization_id))
    except DomainError as exc:
        return _handle_domain_error(exc)

    record_transaction_export(
        request.api_user,
        organization=organization,
        filters={"organization_id": organization_id} if organization_id else {},
    )
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="transactions.csv"'
    writer = csv.DictWriter(
        response,
        fieldnames=[
            "instruction_id",
            "batch_id",
            "recipient_name",
            "recipient_type",
            "amount_minor",
            "fee_amount_minor",
            "gross_amount_minor",
            "status",
            "category",
            "scheduled_for",
            "created_at",
        ],
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return response


@api_view
@require_auth
def transaction_statement_pdf_view(request):
    try:
        organization = None
        organization_id = request.GET.get("organization_id")
        if organization_id:
            organization = get_organization_for_user(request.api_user, organization_id)
        pdf_bytes, file_name = generate_transaction_statement_pdf(
            request.api_user,
            organization=organization,
            filters={
                "organization_id": organization_id,
                "date_from": request.GET.get("date_from"),
                "date_to": request.GET.get("date_to"),
                "status": request.GET.get("status"),
                "category": request.GET.get("category"),
                "recipient_type": request.GET.get("recipient_type"),
                "q": request.GET.get("q"),
            },
        )
    except DomainError as exc:
        return _handle_domain_error(exc)

    record_transaction_export(
        request.api_user,
        organization=organization,
        file_name=file_name,
        file_format=ReportExport.FileFormat.PDF,
        filters={key: value for key, value in {
            "organization_id": request.GET.get("organization_id"),
            "date_from": request.GET.get("date_from"),
            "date_to": request.GET.get("date_to"),
            "status": request.GET.get("status"),
            "category": request.GET.get("category"),
            "recipient_type": request.GET.get("recipient_type"),
            "q": request.GET.get("q"),
        }.items() if value},
    )
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{file_name}"'
    return response


@api_view
@require_auth
def transaction_summary_view(request):
    try:
        summary = build_transaction_summary(
            request.api_user,
            request.GET.get("organization_id"),
            {
                "date_from": request.GET.get("date_from"),
                "date_to": request.GET.get("date_to"),
                "status": request.GET.get("status"),
                "category": request.GET.get("category"),
                "recipient_type": request.GET.get("recipient_type"),
                "q": request.GET.get("q"),
            },
        )
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"summary": summary, "transactions": summary["transactions"]})


@api_view
@require_auth
def report_exports_view(request):
    if request.method != "GET":
        return json_error("Method not allowed.", status=405)
    queryset = ReportExport.objects.select_related("requested_by", "organization").order_by("-created_at")
    organization_id = request.GET.get("organization_id")
    try:
        if organization_id:
            organization = get_organization_for_user(request.api_user, organization_id)
            queryset = queryset.filter(organization=organization)
        elif request.api_user.account_type == "CORPORATE":
            memberships = OrganizationMembership.objects.filter(
                user=request.api_user,
                is_active=True,
            ).values_list("organization_id", flat=True)
            queryset = queryset.filter(models.Q(requested_by=request.api_user) | models.Q(organization_id__in=memberships))
        elif request.api_user.account_type == "INDIVIDUAL":
            queryset = queryset.filter(requested_by=request.api_user)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"exports": [_serialize_report_export(export) for export in queryset[:50]]})


@api_view
@require_auth
def integration_api_keys_view(request):
    try:
        if request.method == "GET":
            api_keys = list_integration_api_keys(request.api_user, request.GET.get("organization_id"))
            return JsonResponse({"api_keys": [_serialize_integration_api_key(api_key) for api_key in api_keys]})
        if request.method == "POST":
            data = get_request_data(request)
            payload = data
            api_key, raw_key = issue_integration_api_key(request.api_user, payload)
            return JsonResponse(
                {
                    "api_key": _serialize_integration_api_key(api_key),
                    "secret": raw_key,
                },
                status=201,
            )
        return json_error("Method not allowed.", status=405)
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def integration_api_key_revoke_view(request, api_key_id):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        api_key = revoke_integration_api_key(request.api_user, api_key_id)
    except Http404:
        return json_error("API key not found.", status=404)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"api_key": _serialize_integration_api_key(api_key)})
