import csv
import uuid

from django.http import Http404, HttpResponse, JsonResponse

from audit.models import AuditLog
from base.models import OrganizationMembership, PaymentBatch, Wallet
from base.services import (
    DomainError,
    ValidationError,
    add_organization_member,
    approve_batch,
    build_dashboard,
    create_organization,
    create_payee,
    create_schedule,
    deactivate_organization_member,
    delete_payee,
    delete_schedule,
    get_batch_for_user,
    get_organization_for_user,
    get_payee_for_user,
    get_schedule_for_user,
    list_batches,
    list_organizations,
    list_organization_members,
    list_payees,
    list_schedules,
    list_wallet_ledger,
    pay_individual_due_items,
    reject_batch,
    submit_batch_for_approval,
    top_up_wallet,
    transfer_to_vault,
    update_organization,
    update_organization_member,
    update_payee,
    update_schedule,
    upload_corporate_batch,
)
from eusers.services import change_user_password, login_user, register_user, update_user_profile
from reports.services import export_transactions_csv_rows, record_transaction_export

from .auth import api_view, json_error, parse_json, require_auth
from .services import issue_integration_api_key, list_integration_api_keys, revoke_integration_api_key


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
        "mfa_enabled": user.mfa_enabled,
    }


def _serialize_wallet(wallet):
    return {
        "id": str(wallet.id),
        "owner_type": wallet.owner_type,
        "wallet_type": wallet.wallet_type,
        "currency": wallet.currency,
        "available_balance_minor": wallet.available_balance_minor,
        "user_id": str(wallet.user_id) if wallet.user_id else None,
        "organization_id": str(wallet.organization_id) if wallet.organization_id else None,
    }


def _serialize_payee(payee):
    return {
        "id": str(payee.id),
        "label": payee.label,
        "payee_type": payee.payee_type,
        "account_reference": payee.account_reference,
        "phone_number": payee.phone_number,
        "paybill_number": payee.paybill_number,
        "till_number": payee.till_number,
        "bank_name": payee.bank_name,
        "bank_code": payee.bank_code,
        "account_number": payee.account_number,
        "expense_category": payee.expense_category,
        "active": payee.active,
        "organization_id": str(payee.organization_id) if payee.organization_id else None,
    }


def _serialize_schedule(schedule):
    return {
        "id": str(schedule.id),
        "payee_id": str(schedule.payee_id),
        "payee_label": schedule.payee.label,
        "amount_minor": schedule.amount_minor,
        "day_of_month": schedule.day_of_month,
        "active": schedule.active,
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
        "organization_id": str(batch.organization_id) if batch.organization_id else None,
        "user_id": str(batch.user_id) if batch.user_id else None,
        "instruction_count": batch.instructions.count(),
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
        "is_active": api_key.is_active,
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
    }


def _serialize_ledger_entry(entry):
    return {
        "id": str(entry.id),
        "wallet_id": str(entry.wallet_id),
        "wallet_type": entry.wallet.wallet_type,
        "entry_type": entry.entry_type,
        "amount_minor": entry.amount_minor,
        "balance_after_minor": entry.balance_after_minor,
        "reference": entry.reference,
        "metadata": entry.metadata,
        "created_at": entry.created_at.isoformat(),
    }


def _serialize_instruction(instruction):
    return {
        "id": str(instruction.id),
        "payee_id": str(instruction.payee_id) if instruction.payee_id else None,
        "recipient_name": instruction.recipient_name,
        "recipient_type": instruction.recipient_type,
        "destination": instruction.destination,
        "amount_minor": instruction.amount_minor,
        "category": instruction.category,
        "external_reference": instruction.external_reference,
        "provider_reference": instruction.provider_reference,
        "status": instruction.status,
        "failure_reason": instruction.failure_reason,
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
def health_view(request):
    return JsonResponse({"status": "ok", "service": "route-backend"})


@api_view
def register_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        payload = parse_json(request)
        user, token = register_user(payload)
    except DomainError as exc:
        return _handle_domain_error(exc)
    except Exception as exc:
        return json_error(str(exc), status=400)
    return JsonResponse({"user": _serialize_user(user), "token": token}, status=201)


@api_view
def login_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        payload = parse_json(request)
        user, token = login_user(payload)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"user": _serialize_user(user), "token": token})


@api_view
@require_auth
def me_view(request):
    try:
        if request.method == "GET":
            return JsonResponse({"user": _serialize_user(request.api_user)})
        if request.method == "PATCH":
            payload = parse_json(request)
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
        payload = parse_json(request)
        change_user_password(request.api_user, payload)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"status": "ok"})


@api_view
@require_auth
def dashboard_view(request):
    return JsonResponse({"dashboard": build_dashboard(request.api_user)})


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
            payload = parse_json(request)
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
            elif request.api_user.account_type == "SUPERADMIN":
                role = "SUPERADMIN"
            return JsonResponse({"organization": _serialize_organization(organization, role=role)})
        if request.method == "PATCH":
            payload = parse_json(request)
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
            payload = parse_json(request)
            membership = add_organization_member(request.api_user, organization_id, payload)
            return JsonResponse({"membership": _serialize_membership(membership)}, status=201)
        return json_error("Method not allowed.", status=405)
    except Http404:
        return json_error("User not found.", status=404)
    except DomainError as exc:
        return _handle_domain_error(exc)


@api_view
@require_auth
def organization_member_detail_view(request, organization_id, membership_id):
    try:
        if request.method == "PATCH":
            payload = parse_json(request)
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
            payload = parse_json(request)
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
            payload = parse_json(request)
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
            payload = parse_json(request)
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
            payload = parse_json(request)
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
def wallet_topup_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        payload = parse_json(request)
        wallet = top_up_wallet(request.api_user, payload)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"wallet": _serialize_wallet(wallet)})


@api_view
@require_auth
def wallet_vault_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        payload = parse_json(request)
        primary_wallet, vault_wallet = transfer_to_vault(request.api_user, payload)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"primary_wallet": _serialize_wallet(primary_wallet), "vault_wallet": _serialize_wallet(vault_wallet)})


@api_view
@require_auth
def wallet_summary_view(request):
    wallets = Wallet.objects.filter(user=request.api_user).order_by("wallet_type")
    if request.GET.get("organization_id"):
        organization_id = request.GET["organization_id"]
        try:
            organization = get_organization_for_user(request.api_user, organization_id)
        except DomainError as exc:
            return _handle_domain_error(exc)
        wallets = Wallet.objects.filter(organization=organization).order_by("wallet_type")
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
@require_auth
def pay_all_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        payload = parse_json(request)
        batch = pay_individual_due_items(request.api_user, payload)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"batch": _serialize_batch(batch)})


@api_view
@require_auth
def corporate_batch_upload_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        payload = parse_json(request)
        batch = upload_corporate_batch(request.api_user, payload)
    except DomainError as exc:
        return _handle_domain_error(exc)
    return JsonResponse({"batch": _serialize_batch(batch)}, status=201)


@api_view
@require_auth
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
def corporate_batch_reject_view(request, batch_id):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        payload = parse_json(request)
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
def pesaway_results_view(request):
    if request.method != "POST":
        return json_error("Method not allowed.", status=405)
    try:
        payload = parse_json(request)
    except ValueError as exc:
        return json_error(str(exc), status=400)
    AuditLog.objects.create(
        actor=None,
        action="pesaway.callback.received",
        target_type="pesaway_callback",
        target_id=uuid.uuid4(),
        metadata=payload,
    )
    return JsonResponse({"status": "received"})


@api_view
@require_auth
def integration_api_keys_view(request):
    try:
        if request.method == "GET":
            api_keys = list_integration_api_keys(request.api_user, request.GET.get("organization_id"))
            return JsonResponse({"api_keys": [_serialize_integration_api_key(api_key) for api_key in api_keys]})
        if request.method == "POST":
            payload = parse_json(request)
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
