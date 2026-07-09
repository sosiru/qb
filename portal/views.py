from collections import defaultdict
from decimal import Decimal

from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from base.models import Organization, PaymentBatch, PaymentInstruction
from base.services import (
    OtpRequired,
    ValidationError,
    build_dashboard,
    can_access_individual_features,
    can_manage_all_organizations,
    export_transactions_csv_rows,
    get_batch_for_user,
    get_organization_for_user,
    list_batches,
    list_payees,
    list_schedules,
    list_wallet_ledger,
    login_user,
)


def _money(amount_minor, keep_cents=False):
    amount = Decimal(amount_minor or 0) / Decimal("100")
    rendered = f"{amount:,.2f}"
    if not keep_cents and rendered.endswith(".00"):
        rendered = rendered[:-3]
    return f"Ksh {rendered}"


def _percent(value, total):
    if not total:
        return 0
    return round((value / total) * 100)


def _status_tone(value):
    value = (value or "").upper()
    if value in {"SUCCEEDED", "DONE", "VERIFIED", "ACTIVE", "COMPLETED", "APPROVED"}:
        return "success"
    if value in {"FAILED", "REJECTED"}:
        return "danger"
    if value in {"PENDING", "PENDING_APPROVAL", "PROCESSING", "DRAFT"}:
        return "warning"
    return "neutral"


def _type_tone(value):
    return {
        "PAYBILL": "blue",
        "TILL": "purple",
        "MOBILE": "rose",
        "BANK": "green",
        "WALLET": "blue",
        "STK": "amber",
    }.get((value or "").upper(), "neutral")


def _scope_instruction_queryset(user, organization=None):
    queryset = PaymentInstruction.objects.select_related("batch", "payee").order_by("-created_at")
    if organization:
        return queryset.filter(batch__organization=organization)
    return queryset.filter(batch__user=user)


def _scope_batches(user, organization=None, **filters):
    return list(list_batches(user, str(organization.id) if organization else None, filters))


def _scope_wallet_ledger(user, organization=None):
    return list(list_wallet_ledger(user, str(organization.id) if organization else None))


def _scope_payees(user, organization=None):
    return list(list_payees(user, str(organization.id) if organization else None, {"active": None}))


def _scope_schedules(user, organization=None):
    return list(list_schedules(user, str(organization.id) if organization else None, {"active": None}))


def _summarize_transactions(instructions):
    total_amount = sum(instruction.amount_minor for instruction in instructions)
    total_fees = sum(instruction.fee_amount_minor for instruction in instructions)
    success_count = sum(1 for instruction in instructions if instruction.status == PaymentInstruction.Status.SUCCEEDED)
    category_totals = defaultdict(int)
    method_totals = defaultdict(int)
    monthly_totals = defaultdict(int)
    for instruction in instructions:
        category_totals[instruction.category or "general"] += instruction.amount_minor
        method_totals[instruction.recipient_type] += instruction.amount_minor
        month_key = timezone.localtime(instruction.created_at).strftime("%b %Y")
        monthly_totals[month_key] += instruction.amount_minor

    sorted_categories = sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
    return {
        "count": len(instructions),
        "total_amount_minor": total_amount,
        "total_amount_display": _money(total_amount),
        "total_fee_minor": total_fees,
        "total_fee_display": _money(total_fees),
        "gross_amount_display": _money(total_amount + total_fees),
        "success_rate": _percent(success_count, len(instructions)),
        "categories": [
            {
                "name": name.replace("_", " ").title(),
                "amount_minor": amount,
                "amount_display": _money(amount),
                "percent": _percent(amount, total_amount),
            }
            for name, amount in sorted_categories[:6]
        ],
        "methods": [
            {
                "name": name.title(),
                "amount_display": _money(amount),
                "percent": _percent(amount, total_amount),
                "tone": _type_tone(name),
            }
            for name, amount in sorted(method_totals.items(), key=lambda item: item[1], reverse=True)
        ],
        "monthly": [
            {"label": label, "amount_minor": amount, "amount_display": _money(amount)}
            for label, amount in list(monthly_totals.items())[-6:]
        ],
    }


def _build_sidebar(user, organization):
    if organization or user.account_type in {"CORPORATE", "SERVICE_PROVIDER", "SUPERADMIN"}:
        items = [
            ("dashboard", "Home"),
            ("payments", "Payments"),
            ("approvals", "Approvals"),
            ("bills", "Beneficiaries"),
            ("wallet", "Treasury"),
            ("reports", "Reports"),
            ("statements", "Statements"),
        ]
    else:
        items = [
            ("dashboard", "Home"),
            ("bills", "Bills"),
            ("wallet", "Wallet"),
            ("vault", "Vault"),
            ("reports", "Reports"),
            ("statements", "Statements"),
        ]
    return [{"key": key, "label": label, "url": reverse(f"portal:{key}")} for key, label in items]


def _resolve_workspace(request):
    dashboard = build_dashboard(request.user)
    organizations = dashboard.get("organizations", [])
    selected_org_id = request.GET.get("organization_id") or request.session.get("portal_organization_id")
    selected_org = None
    selected_meta = None

    if organizations:
        organization_ids = {organization["organization_id"] for organization in organizations}
        if selected_org_id not in organization_ids:
            selected_org_id = organizations[0]["organization_id"]
        selected_org = get_organization_for_user(request.user, selected_org_id)
        selected_meta = next(
            (organization for organization in organizations if organization["organization_id"] == selected_org_id),
            None,
        )
        request.session["portal_organization_id"] = selected_org_id
    else:
        request.session.pop("portal_organization_id", None)

    return dashboard, organizations, selected_org, selected_meta


def _portal_context(request, active_page):
    dashboard, organizations, selected_org, selected_meta = _resolve_workspace(request)
    return {
        "active_page": active_page,
        "dashboard_data": dashboard,
        "sidebar_items": _build_sidebar(request.user, selected_org),
        "organizations": organizations,
        "selected_organization": selected_org,
        "selected_organization_meta": selected_meta,
        "scope_label": selected_org.name if selected_org else "Personal workspace",
        "role_label": (selected_meta or {}).get("role", request.user.account_type).replace("_", " ").title(),
        "is_global_operator": can_manage_all_organizations(request.user),
        "is_individual_workspace": selected_org is None,
    }


def root_redirect(request):
    if request.user.is_authenticated:
        return redirect("portal:dashboard")
    return redirect("portal:login")


def login_view(request):
    context = {"page_title": "Sign in"}
    if request.method == "POST":
        payload = {
            "phone_number": request.POST.get("phone_number"),
            "password": request.POST.get("password"),
            "otp": request.POST.get("otp"),
        }
        try:
            user, _token = login_user(payload)
        except OtpRequired as exc:
            context.update(
                {
                    "otp_required": True,
                    "message": str(exc),
                    "phone_number": payload["phone_number"],
                    "dev_otp": exc.dev_otp,
                }
            )
            return render(request, "portal/login.html", context)
        except ValidationError as exc:
            context.update({"error": str(exc), "phone_number": payload["phone_number"]})
            return render(request, "portal/login.html", context, status=400)

        auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect("portal:dashboard")

    return render(request, "portal/login.html", context)


@login_required(login_url="portal:login")
def logout_view(request):
    auth_logout(request)
    return redirect("portal:login")


@login_required(login_url="portal:login")
def dashboard_view(request):
    context = _portal_context(request, "dashboard")
    organization = context["selected_organization"]
    if organization:
        batches = _scope_batches(request.user, organization)[:5]
        instructions = list(_scope_instruction_queryset(request.user, organization)[:12])
        payees = _scope_payees(request.user, organization)[:5]
        transaction_summary = _summarize_transactions(instructions)
        total_batches = len(batches)
        pending_count = sum(1 for batch in batches if batch.status == PaymentBatch.Status.PENDING_APPROVAL)
        success_count = sum(1 for batch in batches if batch.status == PaymentBatch.Status.SUCCEEDED)
        context.update(
            {
                "hero": {
                    "eyebrow": "Corporate overview",
                    "title": organization.name,
                    "subtitle": f"{pending_count} approvals pending across {total_batches} recent batches",
                    "primary_metric": _money((context["selected_organization_meta"] or {}).get("wallet_balance_minor", 0)),
                    "primary_label": "Treasury balance",
                    "secondary_metric": str(success_count),
                    "secondary_label": "Settled batches",
                },
                "payees_preview": payees,
                "recent_batches": batches,
                "transaction_summary": transaction_summary,
            }
        )
    else:
        dashboard_data = context["dashboard_data"]
        payees = _scope_payees(request.user)[:6]
        schedules = _scope_schedules(request.user)[:6]
        ledger = _scope_wallet_ledger(request.user)[:6]
        category_totals = defaultdict(int)
        for schedule in schedules:
            category_totals[schedule.payee.expense_category] += schedule.amount_minor
        total_category_amount = sum(category_totals.values())
        context.update(
            {
                "hero": {
                    "eyebrow": "Overview",
                    "title": f"Good morning, {request.user.full_name.split()[0]}",
                    "subtitle": f"{len(schedules)} scheduled bills and {_money(dashboard_data.get('monthly_commitments_minor', 0))} committed",
                    "primary_metric": _money(dashboard_data.get("wallet_balance_minor", 0)),
                    "primary_label": "Wallet balance",
                    "secondary_metric": _money(dashboard_data.get("missing_capital_minor", 0)),
                    "secondary_label": "Missing to fully fund",
                },
                "payees_preview": payees,
                "recent_ledger": ledger,
                "category_breakdown": [
                    {
                        "name": name.replace("_", " ").title(),
                        "amount_display": _money(amount),
                        "percent": _percent(amount, total_category_amount),
                    }
                    for name, amount in sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
                ],
            }
        )
    return render(request, "portal/dashboard.html", context)


@login_required(login_url="portal:login")
def bills_view(request):
    context = _portal_context(request, "bills")
    organization = context["selected_organization"]
    payees = _scope_payees(request.user, organization)
    schedules = {schedule.payee_id: schedule for schedule in _scope_schedules(request.user, organization)}
    active_payees = sum(1 for payee in payees if payee.active)
    committed_total = sum((schedules.get(payee.id).amount_minor if schedules.get(payee.id) else 0) for payee in payees)
    rows = []
    for payee in payees:
        schedule = schedules.get(payee.id)
        rows.append(
            {
                "label": payee.label,
                "type": payee.payee_type,
                "type_tone": _type_tone(payee.payee_type),
                "reference": payee.account_reference or payee.paybill_number or payee.till_number or payee.phone_number or payee.account_number,
                "amount_display": _money(schedule.amount_minor if schedule else 0),
                "day_label": f"Day {schedule.day_of_month}" if schedule else "Not scheduled",
                "next_due_label": schedule.next_due_date.strftime("%d %b %Y") if schedule else "-",
                "cadence_label": (
                    f"Every {schedule.interval_months} month{'s' if schedule.interval_months != 1 else ''}"
                    if schedule
                    else "-"
                ),
                "requires_approval": bool(schedule.requires_approval) if schedule else False,
                "status": "Active" if payee.active else "Paused",
                "status_tone": _status_tone("ACTIVE" if payee.active else "PENDING"),
                "category": payee.expense_category.replace("_", " ").title(),
            }
        )
    context.update(
        {
            "page_title": "Bills" if not organization else "Beneficiaries",
            "page_description": f"{active_payees} active entries · {_money(committed_total)} committed monthly",
            "bill_rows": rows,
        }
    )
    return render(request, "portal/bills.html", context)


@login_required(login_url="portal:login")
def payments_view(request):
    context = _portal_context(request, "payments")
    organization = context["selected_organization"]
    batches = _scope_batches(request.user, organization)[:12]
    context.update(
        {
            "page_title": "Payments",
            "page_description": "Recent batches, statuses, and service charges",
            "batches": [
                {
                    "id": batch.id,
                    "label": batch.description or batch.batch_kind.replace("_", " ").title(),
                    "status": batch.status.replace("_", " ").title(),
                    "status_tone": _status_tone(batch.status),
                    "scheduled_for": batch.scheduled_for,
                    "payment_mode": batch.payment_mode,
                    "payment_tone": _type_tone(batch.payment_mode),
                    "instruction_count": batch.instructions.count(),
                    "total_display": _money(batch.total_amount_minor),
                    "fee_display": _money(batch.fee_amount_minor),
                    "gross_display": _money(batch.total_amount_minor + batch.fee_amount_minor),
                }
                for batch in batches
            ],
        }
    )
    return render(request, "portal/payments.html", context)


@login_required(login_url="portal:login")
def wallet_view(request):
    context = _portal_context(request, "wallet")
    organization = context["selected_organization"]
    ledger = _scope_wallet_ledger(request.user, organization)[:10]
    total_in = sum(entry.amount_minor for entry in ledger if entry.amount_minor > 0)
    total_out = abs(sum(entry.amount_minor for entry in ledger if entry.amount_minor < 0))
    balance_minor = (
        (context["selected_organization_meta"] or {}).get("wallet_balance_minor", 0)
        if organization
        else context["dashboard_data"].get("wallet_balance_minor", 0)
    )
    context.update(
        {
            "page_title": "Treasury" if organization else "My Wallet",
            "page_description": "Funding position, cash movement, and recent activity",
            "balance_display": _money(balance_minor, keep_cents=True),
            "incoming_display": _money(total_in),
            "outgoing_display": _money(total_out),
            "ledger_rows": [
                {
                    "created_at": timezone.localtime(entry.created_at),
                    "reference": entry.reference,
                    "entry_type": entry.entry_type.replace("_", " ").title(),
                    "entry_tone": _status_tone("SUCCEEDED" if entry.amount_minor >= 0 else "PENDING"),
                    "amount_display": _money(entry.amount_minor, keep_cents=True),
                    "balance_display": _money(entry.balance_after_minor, keep_cents=True),
                }
                for entry in ledger
            ],
        }
    )
    return render(request, "portal/wallet.html", context)


@login_required(login_url="portal:login")
def vault_view(request):
    context = _portal_context(request, "vault")
    dashboard_data = context["dashboard_data"]
    context.update(
        {
            "page_title": "Vault" if context["is_individual_workspace"] else "Treasury Controls",
            "page_description": "Protected reserves, funding discipline, and payout readiness",
            "wallet_display": _money(dashboard_data.get("wallet_balance_minor", 0)),
            "vault_display": _money(dashboard_data.get("vault_balance_minor", 0)),
            "missing_display": _money(dashboard_data.get("missing_capital_minor", 0)),
        }
    )
    return render(request, "portal/vault.html", context)


@login_required(login_url="portal:login")
def approvals_view(request):
    context = _portal_context(request, "approvals")
    organization = context["selected_organization"]
    if not organization:
        return redirect("portal:payments")

    pending_batches = _scope_batches(request.user, organization, status=PaymentBatch.Status.PENDING_APPROVAL)
    batch_id = request.GET.get("batch_id") or (str(pending_batches[0].id) if pending_batches else None)
    selected_batch = None
    if batch_id:
        try:
            selected_batch = get_batch_for_user(request.user, batch_id)
        except Http404:
            selected_batch = None
    instructions = list(selected_batch.instructions.all().order_by("-amount_minor")[:10]) if selected_batch else []
    context.update(
        {
            "page_title": "Approval Queue",
            "page_description": f"{len(pending_batches)} batches awaiting authorization",
            "pending_batches": pending_batches,
            "selected_batch": selected_batch,
            "preview_instructions": instructions,
            "fees_display": _money(selected_batch.fee_amount_minor if selected_batch else 0),
        }
    )
    return render(request, "portal/approvals.html", context)


@login_required(login_url="portal:login")
def reports_view(request):
    context = _portal_context(request, "reports")
    organization = context["selected_organization"]
    instructions = list(_scope_instruction_queryset(request.user, organization)[:120])
    summary = _summarize_transactions(instructions)
    context.update(
        {
            "page_title": "Reports & Reconciliation" if organization else "Spending Reports",
            "page_description": "Professional summaries across categories, methods, fees, and outcomes",
            "report_summary": summary,
            "export_url": f"{reverse('transaction-report')}{'?organization_id=' + str(organization.id) if organization else ''}",
            "transactions": instructions[:8],
        }
    )
    return render(request, "portal/reports.html", context)


@login_required(login_url="portal:login")
def statements_view(request):
    context = _portal_context(request, "statements")
    organization = context["selected_organization"]
    ledger = _scope_wallet_ledger(request.user, organization)[:12]
    instruction_rows = list(_scope_instruction_queryset(request.user, organization)[:12])
    processing_fees = sum(instruction.fee_amount_minor for instruction in instruction_rows)
    context.update(
        {
            "page_title": "Statements",
            "page_description": "Printable account statement with fees, wallet movements, and payment detail",
            "print_mode": request.GET.get("print") == "1",
            "statement_summary": {
                "scope": organization.name if organization else request.user.full_name,
                "issued_on": timezone.localdate(),
                "transaction_count": len(instruction_rows),
                "processing_fees_display": _money(processing_fees),
                "gross_display": _money(sum(row.amount_minor + row.fee_amount_minor for row in instruction_rows)),
            },
            "statement_rows": instruction_rows,
            "ledger_rows": ledger,
        }
    )
    return render(request, "portal/statements.html", context)
