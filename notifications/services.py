import json
import logging
import time
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

logger = logging.getLogger(__name__)


class NotificationDispatchError(Exception):
    pass


class NotificationInterface:
    def __init__(self, *, base_url=None, api_key=None, timeout=None):
        self.base_url = base_url or settings.NOTIFY_URL
        self.api_key = api_key or settings.NOTIFY_API_KEY
        self.timeout = timeout or settings.NOTIFY_TIMEOUT_SECONDS

    def send_sms(self, message, recipients, *, unique_identifier=None, template=None):
        return self._send(
            "sms",
            message,
            recipients,
            unique_identifier=unique_identifier,
            template=template or settings.NOTIFY_SMS_TEMPLATE,
        )

    def send_email(self, message, recipients, *, unique_identifier=None, template=None):
        return self._send(
            "email",
            message,
            recipients,
            unique_identifier=unique_identifier,
            template=template or settings.NOTIFY_EMAIL_TEMPLATE,
        )

    def _send(self, notification_type, message, recipients, *, unique_identifier=None, template=None):
        logger.info("Starting notification dispatch.")
        if isinstance(recipients, str):
            recipients = [recipients]
        recipients = [r.strip() for r in recipients if r.strip()]
        logger.info("Recipients: %s", recipients)
        if not recipients:
            logger.error("No recipients provided.")
            raise NotificationDispatchError("No recipients provided.")
        payload = {
            "notification_type": notification_type,
            "template": template,
            "unique_identifier": unique_identifier or str(uuid.uuid4()),
            "recipients": recipients,
            "context": {
                "message": str(message),
            },
        }
        logger.info("Payload prepared: %s", payload)
        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self.api_key,
        }
        logger.info("Sending POST request to %s", self.base_url)
        try:
            response = requests.post(
                self.base_url,
                json=payload,
                headers=headers,
            )
            logger.info("Response received. Status code: %s", response.status_code)
            response.raise_for_status()
            result = response.json()
            logger.info("Notification sent successfully. Response: %s", result)
            return result
        except requests.HTTPError:
            logger.exception(
                "HTTP error while sending notification. Status: %s, Body: %s",
                response.status_code,
                response.text,
            )
            raise NotificationDispatchError(response.text)
        except requests.RequestException as e:
            logger.exception("Network error while sending notification.")
            raise NotificationDispatchError(str(e))
        except Exception:
            logger.exception("Unexpected error while sending notification.")
            raise


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
    if channel == "IN_APP" and user:
        return [str(user.id)]
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
    dedupe_daily_event_types = {
        "LOGIN_SUCCESS",
        "T_MINUS_3",
        "DUE_TODAY",
        "WALLET_LOW",
        "OVERDUE_PAYMENT",
    }
    if event_type in dedupe_daily_event_types and user:
        day_start, day_end = _local_day_bounds(scheduled_for)
        already_queued_today = NotificationEvent.objects.filter(
            user=user,
            event_type=event_type,
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
            status=NotificationEvent.Status.SENT if template.channel == "IN_APP" else NotificationEvent.Status.PENDING,
            scheduled_for=scheduled_for,
            sent_at=scheduled_for if template.channel == "IN_APP" else None,
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
    view_model = _event_view_model(event)
    message = (
        (event.context or {}).get("message")
        or (event.context or {}).get("body")
        or view_model["intro"]
    )
    template = (
        settings.NOTIFY_EMAIL_TEMPLATE
        if event.channel == "EMAIL"
        else settings.NOTIFY_SMS_TEMPLATE
    )
    return {
        "notification_type": event.channel.lower(),
        "template": template,
        "unique_identifier": event.unique_identifier,
        "recipients": event.recipients,
        "context": {"message": str(message)},
    }


def _notification_message(event):
    return build_notification_payload(event)["context"]["message"]


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
    cta_label = context.get("cta_label") or "Open Ratiba"
    cta_url = context.get("cta_url") or context.get("invite_link") or getattr(settings, "FRONTEND_BASE_URL", "http://localhost:4200")
    title = context.get("title") or "Ratiba notification"
    intro = context.get("intro") or "A Ratiba activity update is available for your account."
    badge = context.get("badge") or event.event_type.replace("_", " ").title()

    if event.event_type == "SELF_ONBOARDING":
        title = "Welcome to Ratiba"
        intro = "Your account is ready. You can now manage wallets, payees, scheduled payments, approvals, and reports from one secure workspace."
        badge = "Account Created"
        details = [
            ("Account", user_name),
            ("Phone", context.get("phone_number", "")),
            ("Account type", str(context.get("account_type", "")).replace("_", " ").title()),
        ]
    elif event.event_type == "LOGIN_OTP":
        title = "Your Ratiba login code"
        intro = "Use this one-time code to complete your login. Do not share it with anyone."
        badge = "Secure Login"
        cta_label = "Continue login"
        details = [
            ("Login code", context.get("otp", "")),
            ("Expires in", context.get("expires_in", "10 minutes")),
            ("Phone", context.get("phone_number", "")),
        ]
    elif event.event_type == "LOGIN_SUCCESS":
        title = "New Ratiba login"
        intro = "A login to your account was completed. If this was not you, change your password immediately."
        badge = "Login Complete"
        details = [
            ("Account", user_name),
            ("Phone", context.get("phone_number", "")),
            ("Time", context.get("login_time", "")),
        ]
    elif event.event_type == "ORGANIZATION_INVITE":
        title = f"You're invited to {context.get('organization_name', 'Ratiba')}"
        intro = f"{context.get('invited_by', 'A team admin')} invited you to join Ratiba as {context.get('role', 'a member')}."
        badge = "Team Invite"
        cta_label = "Accept invite"
        details = [
            ("Organization", context.get("organization_name", "")),
            ("Role", context.get("role", "")),
            ("Invite email", context.get("email", "")),
        ]
    elif event.event_type == "WALLET_TOPUP_REQUESTED":
        title = "Wallet top-up started"
        intro = "Your wallet top-up request has been received and is being processed."
        badge = "Top-Up Started"
        details = [
            ("Wallet", context.get("wallet_type", "")),
            ("Amount", _money_minor(context.get("amount_minor"))),
            ("Phone", context.get("phone_number", "")),
        ]
    elif event.event_type == "WALLET_TOPUP_COMPLETED":
        title = "Wallet top-up completed"
        intro = "Funds have been added to your wallet and are available for payments."
        badge = "Wallet Funded"
        details = [
            ("Wallet", context.get("wallet_type", "")),
            ("Amount", _money_minor(context.get("amount_minor"))),
            ("Available balance", _money_minor(context.get("wallet_balance_minor"))),
        ]
    elif event.event_type == "WALLET_WITHDRAWAL_REQUESTED":
        title = "Wallet withdrawal requested"
        intro = "Your wallet withdrawal request has been recorded for processing."
        badge = "Withdrawal"
        details = [
            ("Amount", _money_minor(context.get("amount_minor"))),
            ("Phone", context.get("phone_number", "")),
            ("Available balance", _money_minor(context.get("wallet_balance_minor"))),
        ]
    elif event.event_type == "WALLET_LOW":
        title = "Wallet balance is low"
        intro = "Your wallet may not have enough funds for upcoming payments. Top up before the due date."
        badge = "Low Wallet"
        details = [
            ("Available balance", _money_minor(context.get("wallet_balance_minor"))),
            ("Upcoming payments", _money_minor(context.get("total_amount_minor"))),
            ("Shortfall", _money_minor(context.get("shortfall_minor"))),
            ("Schedules", context.get("schedule_count", "")),
        ]
    elif event.event_type == "OVERDUE_PAYMENT":
        title = "Payment is overdue"
        intro = "One or more scheduled payments are overdue. Review them and pay as soon as possible."
        badge = "Overdue"
        details = [
            ("Overdue payments", context.get("schedule_count", "")),
            ("Total amount", _money_minor(context.get("total_amount_minor"))),
            ("Oldest due date", context.get("oldest_due_date", "")),
            ("Days overdue", context.get("oldest_overdue_days", "")),
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
    elif event.event_type == "PRODUCT_UPDATE":
        title = context.get("title") or "Ratiba update"
        intro = context.get("intro") or context.get("body") or "A new Ratiba update is available."
        badge = context.get("badge") or "Product Update"
        cta_label = context.get("cta_label") or "View update"

    extra_details = context.get("details") or []
    if isinstance(extra_details, list):
        details.extend(tuple(item) for item in extra_details if isinstance(item, (list, tuple)) and len(item) == 2)

    return {
        "brand_name": "Ratiba",
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


def send_sms_failure_backup_email(event, failure):
    if event.channel != "SMS" or not event.user_id or not event.user.email:
        return {"status": "skipped", "reason": "email recipient unavailable"}

    template = NotificationTemplate.objects.filter(event_type=event.event_type, channel="EMAIL", active=True).first()
    if not template:
        return {"status": "skipped", "reason": "email template unavailable"}

    backup_event = (
        NotificationEvent.objects.filter(
            user=event.user,
            channel="EMAIL",
            event_type=event.event_type,
            scheduled_for=event.scheduled_for,
            context=event.context,
        )
        .exclude(id=event.id)
        .order_by("created_at")
        .first()
    )
    if backup_event and backup_event.status == NotificationEvent.Status.SENT:
        return {"status": "already_sent", "event_id": str(backup_event.id)}
    if not backup_event:
        backup_event = NotificationEvent.objects.create(
            user=event.user,
            template=template,
            channel="EMAIL",
            event_type=event.event_type,
            status=NotificationEvent.Status.PENDING,
            scheduled_for=timezone.now(),
            unique_identifier=str(uuid.uuid4()),
            recipients=[str(event.user.email)],
            context=_merge_context(template, event.context),
            provider_response={
                "backup_for_event_id": str(event.id),
                "backup_reason": str(failure),
            },
        )
    backup_event.status = NotificationEvent.Status.PROCESSING
    backup_event.attempts += 1
    backup_event.save(update_fields=["status", "attempts", "updated_at"])
    try:
        response = send_notification_event(backup_event)
    except Exception as exc:
        backup_event.status = NotificationEvent.Status.FAILED
        backup_event.last_error = str(exc)[:255]
        backup_event.provider_response = {"error": str(exc), "transport": "email_backup"}
        backup_event.save(update_fields=["status", "last_error", "provider_response", "updated_at"])
        return {"status": "failed", "event_id": str(backup_event.id), "error": str(exc)}

    backup_event.status = NotificationEvent.Status.SENT
    backup_event.sent_at = timezone.now()
    backup_event.last_error = ""
    backup_event.provider_response = {**(response or {}), "transport": "email_backup"}
    backup_event.save(
        update_fields=["status", "sent_at", "last_error", "provider_response", "updated_at"]
    )
    return {"status": "sent", "event_id": str(backup_event.id), "response": response}


def send_notification_event(event):
    if event.channel == "IN_APP":
        return {"status": "delivered_in_app"}

    if not notifications_dispatch_enabled():
        if event.channel == "EMAIL":
            return send_email_event(event)
        raise NotificationDispatchError("Notification provider is not configured.")

    interface = NotificationInterface()
    send = interface.send_email if event.channel == "EMAIL" else interface.send_sms
    return send(
        _notification_message(event),
        event.recipients,
        unique_identifier=event.unique_identifier,
    )


def process_notification_event(event):
    if event.status == NotificationEvent.Status.SENT:
        return {"status": "skipped", "reason": "already sent"}
    event.status = NotificationEvent.Status.PROCESSING
    event.attempts += 1
    event.save(update_fields=["status", "attempts", "updated_at"])
    try:
        response = send_notification_event(event)
    except Exception as exc:
        backup_response = send_sms_failure_backup_email(event, exc)
        event.status = NotificationEvent.Status.FAILED
        event.last_error = str(exc)[:255]
        event.provider_response = {"error": str(exc), "email_backup": backup_response}
        event.save(update_fields=["status", "last_error", "provider_response", "updated_at"])
        logger.warning(
            "notification.dispatch_failed event_id=%s channel=%s event_type=%s error=%s",
            event.id,
            event.channel,
            event.event_type,
            exc,
        )
        return {"status": "failed", "error": str(exc)}
    event.status = NotificationEvent.Status.SENT
    event.sent_at = timezone.now()
    event.provider_response = response or {}
    event.last_error = ""
    event.save(update_fields=["status", "sent_at", "provider_response", "last_error", "updated_at"])
    return response


def ensure_product_update_template():
    template, _ = NotificationTemplate.objects.get_or_create(
        code="in_app_product_update",
        defaults={
            "event_type": "PRODUCT_UPDATE",
            "channel": "IN_APP",
            "system": "ratiba",
            "provider_template": "in_app_product_update",
            "subject_template": "",
            "description": "In-app announcement for major Ratiba updates.",
            "default_context": {"badge": "Product Update"},
            "active": True,
        },
    )
    changed = False
    if template.event_type != "PRODUCT_UPDATE":
        template.event_type = "PRODUCT_UPDATE"
        changed = True
    if template.channel != "IN_APP":
        template.channel = "IN_APP"
        changed = True
    if not template.active:
        template.active = True
        changed = True
    if changed:
        template.save(update_fields=["event_type", "channel", "active", "updated_at"])
    return template


def queue_product_update_notifications(title, body, users=None, context=None):
    template = ensure_product_update_template()
    base_context = {
        "title": title,
        "intro": body,
        "body": body,
        "badge": "Major Update",
    }
    base_context.update(context or {})
    if users is None:
        from eusers.models import User

        users = User.objects.filter(is_active=True)
    created = []
    now = timezone.now()
    for user in users:
        event = NotificationEvent.objects.create(
            user=user,
            template=template,
            channel="IN_APP",
            event_type="PRODUCT_UPDATE",
            status=NotificationEvent.Status.SENT,
            scheduled_for=now,
            sent_at=now,
            unique_identifier=str(uuid.uuid4()),
            recipients=[str(user.id)],
            context=_merge_context(template, base_context),
        )
        created.append(event)
    return created


def serialize_in_app_notification(event):
    context = event.context or {}
    view_model = _event_view_model(event)
    return {
        "id": str(event.id),
        "event_type": event.event_type,
        "title": context.get("title") or view_model["title"],
        "body": context.get("body") or context.get("intro") or view_model["intro"],
        "badge": context.get("badge") or view_model["badge"],
        "severity": context.get("severity") or "info",
        "cta_label": context.get("cta_label") or view_model["cta_label"],
        "cta_url": context.get("cta_url") or view_model["cta_url"],
        "read_at": event.read_at.isoformat() if event.read_at else None,
        "created_at": event.created_at.isoformat(),
        "sent_at": event.sent_at.isoformat() if event.sent_at else None,
    }


def _wallet_user_recipients(wallet):
    if wallet.user_id:
        return [wallet.user]
    if not wallet.organization_id:
        return []

    from base.models import OrganizationMembership

    memberships = (
        OrganizationMembership.objects.filter(
            organization=wallet.organization,
            is_active=True,
            role__in=[OrganizationMembership.Role.ADMIN, OrganizationMembership.Role.CHECKER],
        )
        .select_related("user")
        .order_by("created_at")
    )
    return [membership.user for membership in memberships]


def queue_wallet_owner_notifications(wallet, event_type, context=None, scheduled_for=None):
    context = context or {}
    base_context = {
        "wallet_id": str(wallet.id),
        "wallet_type": wallet.wallet_type,
        "wallet_balance_minor": wallet.available_balance_minor,
        "organization_id": str(wallet.organization_id) if wallet.organization_id else "",
        "organization_name": wallet.organization.name if wallet.organization_id else "",
    }
    base_context.update(context)
    created = []
    for user in _wallet_user_recipients(wallet):
        created.extend(queue_notifications_for_user(user, event_type, base_context, scheduled_for=scheduled_for))
    return created


def queue_wallet_topup_completed_notification(transaction_record):
    wallet = transaction_record.account
    wallet.refresh_from_db()
    return queue_wallet_owner_notifications(
        wallet,
        "WALLET_TOPUP_COMPLETED",
        {
            "amount_minor": transaction_record.amount_minor,
            "transaction_id": str(transaction_record.id),
            "reference": transaction_record.internal_reference,
        },
        scheduled_for=timezone.now(),
    )


def create_due_notifications(run_date=None):
    run_date = run_date or timezone.localdate()
    reminder_date = run_date + timedelta(days=3)
    schedules = PaymentSchedule.objects.filter(next_due_date=reminder_date, active=True, payee__active=True).select_related("payee__user")
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
                "due_date": reminder_date.isoformat(),
            },
            scheduled_for=timezone.now(),
        )
        created += len(events)
    return created


def create_due_today_notifications(run_date=None):
    run_date = run_date or timezone.localdate()
    schedules = PaymentSchedule.objects.filter(next_due_date=run_date, active=True, payee__active=True).select_related("payee__user")
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


def create_low_wallet_notifications(run_date=None):
    run_date = run_date or timezone.localdate()
    reminder_until = run_date + timedelta(days=3)
    schedules = PaymentSchedule.objects.filter(
        next_due_date__lte=reminder_until,
        active=True,
        payee__active=True,
        payee__user__default_payment_mode="WALLET",
    ).select_related("payee__user")
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
                "wallet_balance_minor": wallet.available_balance_minor if wallet else 0,
                "total_amount_minor": 0,
                "schedule_count": 0,
            }
        user_rollups[user.id]["total_amount_minor"] += schedule.amount_minor
        user_rollups[user.id]["schedule_count"] += 1

    for rollup in user_rollups.values():
        shortfall_minor = rollup["total_amount_minor"] - rollup["wallet_balance_minor"]
        if shortfall_minor <= 0:
            continue
        events = queue_notifications_for_user(
            rollup["user"],
            "WALLET_LOW",
            {
                "wallet_balance_minor": rollup["wallet_balance_minor"],
                "total_amount_minor": rollup["total_amount_minor"],
                "shortfall_minor": shortfall_minor,
                "schedule_count": rollup["schedule_count"],
                "reminder_until": reminder_until.isoformat(),
            },
            scheduled_for=timezone.now(),
        )
        created += len(events)
    return created


def create_overdue_payment_notifications(run_date=None):
    run_date = run_date or timezone.localdate()
    schedules = PaymentSchedule.objects.filter(next_due_date__lt=run_date, active=True, payee__active=True).select_related("payee__user")
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
                "oldest_due_date": schedule.next_due_date,
            }
        user_rollups[user.id]["total_amount_minor"] += schedule.amount_minor
        user_rollups[user.id]["schedule_count"] += 1
        if schedule.next_due_date < user_rollups[user.id]["oldest_due_date"]:
            user_rollups[user.id]["oldest_due_date"] = schedule.next_due_date

    for rollup in user_rollups.values():
        oldest_due_date = rollup["oldest_due_date"]
        events = queue_notifications_for_user(
            rollup["user"],
            "OVERDUE_PAYMENT",
            {
                "total_amount_minor": rollup["total_amount_minor"],
                "schedule_count": rollup["schedule_count"],
                "oldest_due_date": oldest_due_date.isoformat(),
                "oldest_overdue_days": (run_date - oldest_due_date).days,
            },
            scheduled_for=timezone.now(),
        )
        created += len(events)
    return created


def create_all_reminder_notifications(run_date=None):
    return {
        "t_minus_3": create_due_notifications(run_date),
        "due_today": create_due_today_notifications(run_date),
        "low_wallet": create_low_wallet_notifications(run_date),
        "overdue": create_overdue_payment_notifications(run_date),
    }
