import json
import logging
import time
import uuid
from datetime import timedelta
from urllib import error, request
from urllib.parse import urlparse
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
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(self.base_url, data=data, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                status_code = getattr(response, "status", None)
            logger.info("Response received. Status code: %s", status_code)
            result = json.loads(body or "{}")
            logger.info("Notification sent successfully. Response: %s", result)
            return result
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.exception(
                "HTTP error while sending notification. Status: %s, Body: %s",
                exc.code,
                body,
            )
            raise NotificationDispatchError(body or str(exc))
        except (error.URLError, OSError) as e:
            logger.exception("Network error while sending notification.")
            raise NotificationDispatchError(str(e))
        except Exception:
            logger.exception("Unexpected error while sending notification.")
            raise


def notifications_dispatch_enabled():
    return bool(settings.NOTIFY_URL and settings.NOTIFY_API_KEY)


def validate_notification_configuration(*, require_email_backup=False):
    errors = []
    warnings = []
    notify_url = (settings.NOTIFY_URL or "").strip()
    api_key = (settings.NOTIFY_API_KEY or "").strip()

    if not notify_url:
        errors.append("NOTIFY must be configured with the notification provider URL.")
    else:
        parsed = urlparse(notify_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append("NOTIFY must be a valid http(s) URL.")

    if not api_key:
        errors.append("NOTIFY_API_KEY must be configured.")
    elif len(api_key) < 8:
        warnings.append("NOTIFY_API_KEY is unusually short; confirm the full provider key is set.")

    if require_email_backup and settings.EMAIL_BACKEND.endswith("smtp.EmailBackend") and not settings.EMAIL_HOST_PASSWORD:
        errors.append("EMAIL_HOST_PASSWORD must be configured for SMTP email backup.")

    if errors:
        raise NotificationDispatchError(" ".join(errors))

    return {
        "configured": True,
        "notify_url": notify_url,
        "api_key_present": bool(api_key),
        "api_key_length": len(api_key),
        "email_backup_configured": not (
            settings.EMAIL_BACKEND.endswith("smtp.EmailBackend") and not settings.EMAIL_HOST_PASSWORD
        ),
        "warnings": warnings,
    }


def notification_channel_enabled(event_type, channel):
    if event_type == "LOGIN_SUCCESS" and channel == "SMS":
        return settings.NOTIFY_LOGIN_SUCCESS_SMS_ENABLED
    return True


def _merge_context(template, context):
    merged = dict(template.default_context or {})
    merged.update(context or {})
    return merged


MANDATORY_AUTH_EVENT_TYPES = {"SELF_ONBOARDING", "LOGIN_OTP"}


def _recipients_for_channel(user, channel, event_type=None):
    bypass_preferences = event_type in MANDATORY_AUTH_EVENT_TYPES
    if channel == "SMS" and user and (bypass_preferences or user.sms_notifications_enabled) and user.phone_number:
        return [str(user.phone_number)]
    if channel == "EMAIL" and user and (bypass_preferences or user.email_notifications_enabled) and user.email:
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
        if not notification_channel_enabled(event_type, template.channel):
            continue
        recipients = _recipients_for_channel(user, template.channel, event_type)
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


def _friendly_value(value, labels=None):
    value = str(value or "").strip()
    if not value:
        return ""
    labels = labels or {}
    return labels.get(value.upper(), value.replace("_", " ").title())


def _payment_count(value):
    try:
        count = int(value)
    except (TypeError, ValueError):
        return "Payments"
    return f"{count} payment" if count == 1 else f"{count} payments"


def _payment_failure_reason(value):
    reason = str(value or "").strip()
    if not reason:
        return ""
    known_reasons = {
        "insufficient_wallet_balance": "There was not enough money in your wallet.",
    }
    return known_reasons.get(
        reason.lower(),
        "We could not complete the payment. Try again, or contact us if the problem continues.",
    )


def _clean_text(*values, fallback=""):
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return fallback


def _event_view_model(event):
    context = event.context or {}
    user_name = context.get("user_name") or (event.user.full_name if event.user_id else "") or "there"
    amount = context.get("total_amount_minor") or context.get("amount_minor")
    details = []
    cta_label = _clean_text(context.get("cta_label"), fallback="Open QuickBills")
    cta_url = _clean_text(
        context.get("cta_url"),
        context.get("invite_link"),
        fallback=getattr(settings, "FRONTEND_BASE_URL", "http://localhost:4200"),
    )
    title = _clean_text(context.get("title"), fallback="QuickBills update")
    intro = _clean_text(context.get("intro"), fallback="A QuickBills activity update is available for your account.")
    badge = _clean_text(context.get("badge"), fallback=event.event_type.replace("_", " ").title())

    if event.event_type == "SELF_ONBOARDING":
        title = "Welcome to QuickBills"
        intro = "Your account is ready. You can now add money, choose who to pay, schedule payments, approve team payments, and view your reports."
        badge = "Account ready"
        cta_label = "Go to your account"
        details = [
            ("Name", user_name),
            ("Phone", context.get("phone_number", "")),
            ("Account type", _friendly_value(context.get("account_type"))),
        ]
    elif event.event_type == "LOGIN_OTP":
        title = "Your QuickBills login code"
        intro = "Enter this code to sign in to your QuickBills account. For your security, do not share it with anyone."
        badge = "Login code"
        cta_label = "Continue signing in"
        details = [
            ("Login code", context.get("otp", "")),
            ("Expires in", context.get("expires_in", "10 minutes")),
            ("Phone", context.get("phone_number", "")),
        ]
    elif event.event_type == "LOGIN_SUCCESS":
        title = "New sign-in to your QuickBills account"
        intro = "Your QuickBills account was signed in to. If this was not you, change your password immediately."
        badge = "Account sign-in"
        cta_label = "Review your account"
        details = [
            ("Account", user_name),
            ("Phone", context.get("phone_number", "")),
            ("Time", context.get("login_time", "")),
        ]
    elif event.event_type == "ORGANIZATION_INVITE":
        title = f"You're invited to {context.get('organization_name', 'QuickBills')}"
        role = _friendly_value(
            context.get("role", "VIEWER"),
            {
                "ADMIN": "an administrator",
                "MAKER": "a payment creator",
                "CHECKER": "a payment approver",
                "VIEWER": "a viewer",
            },
        )
        intro = f"{context.get('invited_by', 'A team administrator')} invited you to join their QuickBills team as {role}."
        badge = "Team invitation"
        cta_label = "Accept invitation"
        details = [
            ("Team", context.get("organization_name", "")),
            ("Your access", role.removeprefix("a ").removeprefix("an ").capitalize()),
            ("Email", context.get("email", "")),
        ]
    elif event.event_type == "WALLET_TOPUP_REQUESTED":
        title = "We are adding money to your wallet"
        intro = f"We received your request to add {_money_minor(context.get('amount_minor'))} to your wallet. We will let you know when the money is ready to use."
        badge = "Money being added"
        cta_label = "View your wallet"
        details = [
            ("Wallet", context.get("wallet_type", "")),
            ("Amount", _money_minor(context.get("amount_minor"))),
            ("Phone", context.get("phone_number", "")),
        ]
    elif event.event_type == "WALLET_TOPUP_COMPLETED":
        title = "Money added to your wallet"
        intro = f"{_money_minor(context.get('amount_minor'))} has been added to your wallet and is ready to use."
        badge = "Money available"
        cta_label = "View your wallet"
        details = [
            ("Wallet", context.get("wallet_type", "")),
            ("Amount", _money_minor(context.get("amount_minor"))),
            ("Available balance", _money_minor(context.get("wallet_balance_minor"))),
        ]
    elif event.event_type == "WALLET_WITHDRAWAL_REQUESTED":
        title = "Your withdrawal is being processed"
        intro = f"We received your request to withdraw {_money_minor(context.get('amount_minor'))} from your wallet. We will update you when it is complete."
        badge = "Withdrawal started"
        cta_label = "View your wallet"
        details = [
            ("Amount", _money_minor(context.get("amount_minor"))),
            ("Phone", context.get("phone_number", "")),
            ("Available balance", _money_minor(context.get("wallet_balance_minor"))),
        ]
    elif event.event_type == "WALLET_LOW":
        title = "Add money for your upcoming payments"
        intro = f"Your wallet is short by {_money_minor(context.get('shortfall_minor'))} for payments due soon. Add money before they are due to avoid missed payments."
        badge = "More money needed"
        cta_label = "Add money"
        details = [
            ("Available balance", _money_minor(context.get("wallet_balance_minor"))),
            ("Payments due soon", _money_minor(context.get("total_amount_minor"))),
            ("Amount to add", _money_minor(context.get("shortfall_minor"))),
            ("Number of payments", context.get("schedule_count", "")),
        ]
    elif event.event_type == "OVERDUE_PAYMENT":
        title = "You have overdue payments"
        intro = f"You have {_payment_count(context.get('schedule_count')).lower()} overdue, totalling {_money_minor(context.get('total_amount_minor'))}. Review them and pay as soon as you can."
        badge = "Payment overdue"
        cta_label = "Review overdue payments"
        details = [
            ("Number of payments", context.get("schedule_count", "")),
            ("Amount overdue", _money_minor(context.get("total_amount_minor"))),
            ("Earliest due date", context.get("oldest_due_date", "")),
            ("Longest overdue", f"{context.get('oldest_overdue_days')} days" if context.get("oldest_overdue_days") is not None else ""),
        ]
    elif event.event_type in {"T_MINUS_3", "DUE_TODAY"}:
        due_copy = "due in 3 days" if event.event_type == "T_MINUS_3" else "due today"
        title = f"You have payments {due_copy}"
        intro = f"You have {_payment_count(context.get('schedule_count')).lower()} totalling {_money_minor(context.get('total_amount_minor'))} {due_copy}. Check that you have enough money available."
        badge = "Payment reminder"
        cta_label = "Review payments"
        details = [
            ("Number of payments", context.get("schedule_count", "")),
            ("Amount due", _money_minor(context.get("total_amount_minor"))),
            ("How you will pay", _friendly_value(context.get("payment_mode"), {"WALLET": "QuickBills wallet", "STK": "M-PESA prompt"})),
        ]
    elif event.event_type == "PAYMENT_SUCCESS":
        if context.get("kplc_message"):
            title = _clean_text(context.get("title"), fallback="KPLC payment completed")
            intro = _clean_text(context.get("intro"), fallback="Your KPLC payment was completed. Your token or meter response is below.")
            badge = _clean_text(context.get("badge"), fallback="KPLC payment")
            cta_label = "View payment details"
            details = [
                ("Payment reference", context.get("payment_reference") or context.get("batch_id", "")),
                ("Amount", _money_minor(context.get("amount_minor"))),
                ("Fee", _money_minor(context.get("fee_amount_minor"))),
                ("Total charged", _money_minor(context.get("total_amount_minor"))),
                ("KPLC meter", context.get("kplc_meter_number", "")),
                ("KPLC response", context.get("kplc_message", "")),
            ]
        else:
            title = "Your payments were sent successfully"
            payout_count = context.get("payout_count")
            recipient_name = context.get("recipient_name")
            if payout_count:
                payment_description = f"{_payment_count(payout_count).lower()} totalling {_money_minor(amount)}"
            elif recipient_name:
                payment_description = f"your payment of {_money_minor(amount)} to {recipient_name}"
            else:
                payment_description = f"your payment of {_money_minor(amount)}"
            intro = f"We successfully sent {payment_description}. You can view the payment details in QuickBills."
            badge = "Payment sent"
            cta_label = "View payment details"
            details = [
                ("Payment reference", context.get("batch_id", "")),
                ("Amount", _money_minor(amount)),
                ("Sent by", context.get("sender_name", "")),
                ("Sender phone", context.get("sender_phone_number", "")),
                ("Recipient", context.get("recipient_name", "")),
                ("Recipient phone", context.get("recipient_phone_number", "")),
                ("Number of recipients", context.get("payout_count", "")),
            ]
    elif event.event_type == "PAYMENT_FAILURE":
        title = "A payment needs your attention"
        intro = "We could not send one or more of your payments. Open QuickBills to see what happened and try again."
        badge = "Action needed"
        cta_label = "Review payments"
        details = [
            ("Payment reference", context.get("batch_id", "")),
            ("Result", _friendly_value(context.get("status", "FAILED"), {"FAILED": "Not sent", "PARTIAL": "Some payments were not sent"})),
            ("What happened", _payment_failure_reason(context.get("reason"))),
        ]
    elif event.event_type == "APPROVAL_REQUEST":
        title = "Payments are waiting for your approval"
        intro = f"Payments totalling {_money_minor(amount)} are ready for your review. Check the details and approve them before the money is sent."
        badge = "Approval needed"
        cta_label = "Review and approve"
        details = [
            ("Payment reference", context.get("batch_id", "")),
            ("Total amount", _money_minor(amount)),
        ]
    elif event.event_type == "BATCH_APPROVED":
        title = "Your payments were approved"
        intro = "The payments you submitted were approved and are now being sent."
        badge = "Payments approved"
        cta_label = "View payments"
        details = [("Payment reference", context.get("batch_id", ""))]
    elif event.event_type == "BATCH_REJECTED":
        title = "Your payments were not approved"
        intro = "The payments you submitted were not approved. Review the reason, make any needed changes, and submit them again."
        badge = "Changes needed"
        cta_label = "Review payments"
        details = [
            ("Payment reference", context.get("batch_id", "")),
            ("Why they were not approved", context.get("reason", "")),
        ]
    elif event.event_type == "PRODUCT_UPDATE":
        title = _clean_text(context.get("title"), fallback="QuickBills update")
        intro = _clean_text(context.get("intro"), context.get("body"), fallback="A new QuickBills update is available.")
        badge = _clean_text(context.get("badge"), fallback="Product Update")
        cta_label = _clean_text(context.get("cta_label"), fallback="Open QuickBills")

    extra_details = context.get("details") or []
    if isinstance(extra_details, list):
        details.extend(tuple(item) for item in extra_details if isinstance(item, (list, tuple)) and len(item) == 2)

    return {
        "brand_name": "QuickBills",
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

    if not notification_channel_enabled(event.event_type, event.channel):
        return {"status": "skipped", "reason": "notification channel disabled"}

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
            "system": "quickbills",
            "provider_template": "in_app_product_update",
            "subject_template": "",
            "description": "In-app announcement for major QuickBills updates.",
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
        "title": _clean_text(title, fallback="QuickBills update"),
        "intro": _clean_text(body, fallback="A new QuickBills update is available."),
        "body": _clean_text(body, fallback="A new QuickBills update is available."),
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
    fallback_body = "A QuickBills activity update is available for your account."
    return {
        "id": str(event.id),
        "event_type": event.event_type,
        "title": _clean_text(context.get("title"), view_model["title"], fallback="QuickBills update"),
        "body": _clean_text(context.get("body"), context.get("intro"), view_model["intro"], fallback=fallback_body),
        "badge": _clean_text(context.get("badge"), view_model["badge"], fallback="QuickBills"),
        "severity": _clean_text(context.get("severity"), fallback="info").lower(),
        "cta_label": _clean_text(context.get("cta_label"), view_model["cta_label"], fallback="Open QuickBills"),
        "cta_url": _clean_text(context.get("cta_url"), view_model["cta_url"], fallback=getattr(settings, "FRONTEND_BASE_URL", "http://localhost:4200")),
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
