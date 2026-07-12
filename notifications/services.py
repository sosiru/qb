import json
import uuid
from datetime import timedelta
from urllib import error, request

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.template import Template, Context
from django.utils import timezone

from base.models import PaymentSchedule
from ledger.models import Account

from .models import NotificationEvent, NotificationTemplate


class NotificationDispatchError(Exception):
    pass


def notifications_dispatch_enabled():
    return bool(settings.NOTIFY_URL and settings.NOTIFY_API_KEY)


def _merge_context(template, context):
    merged = dict(template.default_context or {})
    merged.update(context or {})
    return merged


def _recipients_for_channel(user, channel):
    if channel == "SMS" and user and user.sms_notifications_enabled and user.phone_number:
        return [str(user.phone_number)]
    if channel == "EMAIL" and user and user.email_notifications_enabled and user.email:
        return [str(user.email)]
    return []


def _render_string(template, context):
    if not template:
        return ""
    return Template(template).render(Context(context or {}))


def _local_day_bounds(value):
    local_value = timezone.localtime(value or timezone.now())
    start = local_value.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


@transaction.atomic
def queue_notifications_for_user(user, event_type, context=None, scheduled_for=None):
    scheduled_for = scheduled_for or timezone.now()
    if event_type == "LOGIN_SUCCESS" and user:
        day_start, day_end = _local_day_bounds(scheduled_for)
        already_queued_today = NotificationEvent.objects.filter(
            user=user,
            event_type="LOGIN_SUCCESS",
            scheduled_for__gte=day_start,
            scheduled_for__lt=day_end,
        ).exists()
        if already_queued_today:
            return []

    created = []
    templates = NotificationTemplate.objects.filter(event_type=event_type, active=True).order_by("channel")
    for template in templates:
        recipients = _recipients_for_channel(user, template.channel)
        if not recipients:
            continue
        event = NotificationEvent.objects.create(
            user=user,
            template=template,
            channel=template.channel,
            event_type=event_type,
            status=NotificationEvent.Status.PENDING,
            scheduled_for=scheduled_for,
            unique_identifier=str(uuid.uuid4()),
            recipients=recipients,
            context=_merge_context(template, context),
        )
        created.append(event)
    return created


@transaction.atomic
def queue_email_notification(recipients, event_type, context=None, scheduled_for=None, user=None):
    recipients = [str(recipient).strip() for recipient in recipients if str(recipient).strip()]
    if not recipients:
        return None
    template = NotificationTemplate.objects.filter(event_type=event_type, channel="EMAIL", active=True).first()
    if not template:
        return None
    return NotificationEvent.objects.create(
        user=user,
        template=template,
        channel="EMAIL",
        event_type=event_type,
        status=NotificationEvent.Status.PENDING,
        scheduled_for=scheduled_for or timezone.now(),
        unique_identifier=str(uuid.uuid4()),
        recipients=recipients,
        context=_merge_context(template, context),
    )


def build_notification_payload(event):
    return {
        "unique_identifier": event.unique_identifier,
        "system": event.template.system or settings.NOTIFY_SYSTEM,
        "recipients": event.recipients,
        "notification_type": event.channel.lower(),
        "template": event.template.provider_template,
        "context": event.context,
    }


def _money_minor(value):
    try:
        return f"KES {int(value or 0) / 100:,.2f}"
    except (TypeError, ValueError):
        return "KES 0.00"


def _event_view_model(event):
    context = event.context or {}
    user_name = context.get("user_name") or (event.user.full_name if event.user_id else "") or "there"
    amount = context.get("total_amount_minor") or context.get("amount_minor")
    details = []
    cta_label = context.get("cta_label") or "Open Quick Bundl"
    cta_url = context.get("cta_url") or context.get("invite_link") or getattr(settings, "FRONTEND_BASE_URL", "http://localhost:4200")
    title = context.get("title") or "Quick Bundl notification"
    intro = context.get("intro") or "A Quick Bundl activity update is available for your account."
    badge = context.get("badge") or event.event_type.replace("_", " ").title()

    if event.event_type == "SELF_ONBOARDING":
        title = "Welcome to Quick Bundl"
        intro = "Your account is ready. You can now manage wallets, payees, scheduled payments, approvals, and reports from one secure workspace."
        badge = "Account Created"
        details = [
            ("Account", user_name),
            ("Phone", context.get("phone_number", "")),
            ("Account type", str(context.get("account_type", "")).replace("_", " ").title()),
        ]
    elif event.event_type == "LOGIN_OTP":
        title = "Your Quick Bundl login code"
        intro = "Use this one-time code to complete your login. Do not share it with anyone."
        badge = "Secure Login"
        cta_label = "Continue login"
        details = [
            ("Login code", context.get("otp", "")),
            ("Expires in", context.get("expires_in", "10 minutes")),
            ("Phone", context.get("phone_number", "")),
        ]
    elif event.event_type == "LOGIN_SUCCESS":
        title = "New Quick Bundl login"
        intro = "A login to your account was completed. If this was not you, change your password immediately."
        badge = "Login Complete"
        details = [
            ("Account", user_name),
            ("Phone", context.get("phone_number", "")),
            ("Time", context.get("login_time", "")),
        ]
    elif event.event_type == "ORGANIZATION_INVITE":
        title = f"You're invited to {context.get('organization_name', 'Quick Bundl')}"
        intro = f"{context.get('invited_by', 'A team admin')} invited you to join Quick Bundl as {context.get('role', 'a member')}."
        badge = "Team Invite"
        cta_label = "Accept invite"
        details = [
            ("Organization", context.get("organization_name", "")),
            ("Role", context.get("role", "")),
            ("Invite email", context.get("email", "")),
        ]
    elif event.event_type in {"T_MINUS_3", "DUE_TODAY"}:
        due_copy = "due in 3 days" if event.event_type == "T_MINUS_3" else "due today"
        title = f"Scheduled payments {due_copy}"
        intro = "Review your upcoming commitments and keep your wallet funded before execution."
        badge = "Payment Reminder"
        details = [
            ("Schedules", context.get("schedule_count", "")),
            ("Total amount", _money_minor(context.get("total_amount_minor"))),
            ("Payment mode", context.get("payment_mode", "")),
        ]
    elif event.event_type == "PAYMENT_SUCCESS":
        title = "Payment completed"
        intro = "Your payment batch has been completed successfully."
        badge = "Payment Success"
        details = [
            ("Batch ID", context.get("batch_id", "")),
            ("Amount", _money_minor(amount)),
            ("Sent by", context.get("sender_name", "")),
            ("Sender phone", context.get("sender_phone_number", "")),
            ("Recipient", context.get("recipient_name", "")),
            ("Recipient phone", context.get("recipient_phone_number", "")),
            ("Payouts", context.get("payout_count", "")),
        ]
    elif event.event_type == "PAYMENT_FAILURE":
        title = "Payment needs attention"
        intro = "A payment failed or completed partially. Review the batch and resolve any failed instructions."
        badge = "Action Needed"
        details = [
            ("Batch ID", context.get("batch_id", "")),
            ("Status", context.get("status", "FAILED")),
            ("Reason", context.get("reason", "")),
        ]
    elif event.event_type == "APPROVAL_REQUEST":
        title = "Approval required"
        intro = "A payout batch is waiting for your review before funds are released."
        badge = "Approval"
        details = [
            ("Batch ID", context.get("batch_id", "")),
            ("Amount", _money_minor(amount)),
        ]
    elif event.event_type == "BATCH_APPROVED":
        title = "Batch approved"
        intro = "Your submitted payout batch was approved and settlement has started."
        badge = "Approved"
        details = [("Batch ID", context.get("batch_id", "")), ("Organization", context.get("organization_id", ""))]
    elif event.event_type == "BATCH_REJECTED":
        title = "Batch rejected"
        intro = "Your submitted payout batch was rejected. Review the reason before resubmitting."
        badge = "Rejected"
        details = [
            ("Batch ID", context.get("batch_id", "")),
            ("Reason", context.get("reason", "")),
        ]

    extra_details = context.get("details") or []
    if isinstance(extra_details, list):
        details.extend(tuple(item) for item in extra_details if isinstance(item, (list, tuple)) and len(item) == 2)

    return {
        "brand_name": "Quick Bundl",
        "title": title,
        "intro": intro,
        "badge": badge,
        "user_name": user_name,
        "details": [(label, value) for label, value in details if value is not None and value != ""],
        "cta_label": cta_label,
        "cta_url": cta_url,
        "support_email": settings.EMAIL_HOST_USER,
        "sent_at": timezone.localtime(timezone.now()).strftime("%d %b %Y, %I:%M %p"),
    }


def build_email_message(event):
    view_model = _event_view_model(event)
    subject_context = dict(event.context or {})
    subject_context.update(view_model)
    subject = _render_string(event.template.subject_template, subject_context) or view_model["title"]
    html_body = render_to_string("notifications/email/corporate.html", view_model)
    text_body = render_to_string("notifications/email/corporate.txt", view_model)
    return subject, text_body, html_body


def send_email_event(event):
    if settings.EMAIL_BACKEND.endswith("smtp.EmailBackend") and not settings.EMAIL_HOST_PASSWORD:
        raise NotificationDispatchError("EMAIL_HOST_PASSWORD must be configured for SMTP email notifications.")
    subject, text_body, html_body = build_email_message(event)
    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=event.recipients,
    )
    message.attach_alternative(html_body, "text/html")
    sent_count = message.send(fail_silently=False)
    return {"status": "sent", "sent_count": sent_count, "recipients": event.recipients}


def send_notification_event(event):
    if event.channel == "EMAIL":
        return send_email_event(event)

    if not notifications_dispatch_enabled():
        raise NotificationDispatchError("Notification provider is not configured.")

    payload = build_notification_payload(event)
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": settings.NOTIFY_API_KEY,
    }
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(settings.NOTIFY_URL, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=settings.NOTIFY_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {"status": "sent"}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        raise NotificationDispatchError(raw or f"Notification HTTP error {exc.code}.") from exc
    except error.URLError as exc:
        raise NotificationDispatchError(str(exc.reason)) from exc


def process_notification_event(event):
    event.status = NotificationEvent.Status.PROCESSING
    event.attempts += 1
    event.save(update_fields=["status", "attempts", "updated_at"])
    response = send_notification_event(event)
    event.status = NotificationEvent.Status.SENT
    event.sent_at = timezone.now()
    event.provider_response = response or {}
    event.last_error = ""
    event.save(update_fields=["status", "sent_at", "provider_response", "last_error", "updated_at"])
    return response


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


def create_due_today_notifications(run_date=None):
    run_date = run_date or timezone.localdate()
    schedules = PaymentSchedule.objects.filter(day_of_month=run_date.day, active=True, payee__active=True).select_related("payee__user")
    created = 0
    user_rollups = {}
    for schedule in schedules:
        user = schedule.payee.user
        if not user:
            continue
        if user.id not in user_rollups:
            wallet = Account.objects.filter(user=user, account_kind=Account.AccountKind.PRIMARY).first()
            user_rollups[user.id] = {
                "user": user,
                "total_amount_minor": 0,
                "schedule_count": 0,
                "wallet_balance_minor": wallet.available_balance_minor if wallet else 0,
                "payment_mode": user.default_payment_mode,
            }
        user_rollups[user.id]["total_amount_minor"] += schedule.amount_minor
        user_rollups[user.id]["schedule_count"] += 1

    for rollup in user_rollups.values():
        events = queue_notifications_for_user(
            rollup["user"],
            "DUE_TODAY",
            {
                "total_amount_minor": rollup["total_amount_minor"],
                "schedule_count": rollup["schedule_count"],
                "wallet_balance_minor": rollup["wallet_balance_minor"],
                "payment_mode": rollup["payment_mode"],
            },
            scheduled_for=timezone.now(),
        )
        created += len(events)
    return created
