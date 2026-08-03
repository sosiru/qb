from django.db import migrations


TEMPLATES = (
    ("T_MINUS_3", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f01", "sms_t_minus_3"),
    ("T_MINUS_3", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f02", "email_t_minus_3"),
    ("DUE_TODAY", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f03", "sms_due_today"),
    ("DUE_TODAY", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f04", "email_due_today"),
    ("PAYMENT_SUCCESS", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f05", "sms_payment_success"),
    ("PAYMENT_SUCCESS", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f06", "email_payment_success"),
    ("PAYMENT_FAILURE", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f07", "sms_payment_failure"),
    ("PAYMENT_FAILURE", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f08", "email_payment_failure"),
    ("APPROVAL_REQUEST", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f09", "sms_approval_request"),
    ("APPROVAL_REQUEST", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f10", "email_approval_request"),
    ("BATCH_APPROVED", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f11", "sms_batch_approved"),
    ("BATCH_APPROVED", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f12", "email_batch_approved"),
    ("BATCH_REJECTED", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f13", "sms_batch_rejected"),
    ("BATCH_REJECTED", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f14", "email_batch_rejected"),
    ("SELF_ONBOARDING", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f15", "sms_self_onboarding"),
    ("SELF_ONBOARDING", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f16", "email_self_onboarding"),
    ("LOGIN_OTP", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f17", "sms_login_otp"),
    ("LOGIN_OTP", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f18", "email_login_otp"),
    ("LOGIN_SUCCESS", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f19", "sms_login_success"),
    ("LOGIN_SUCCESS", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f20", "email_login_success"),
    ("ORGANIZATION_INVITE", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f21", "sms_organization_invite"),
    ("ORGANIZATION_INVITE", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f22", "email_organization_invite"),
    ("WALLET_TOPUP_REQUESTED", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f23", "sms_wallet_topup_requested"),
    ("WALLET_TOPUP_REQUESTED", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f24", "email_wallet_topup_requested"),
    ("WALLET_TOPUP_COMPLETED", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f25", "sms_wallet_topup_completed"),
    ("WALLET_TOPUP_COMPLETED", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f26", "email_wallet_topup_completed"),
    ("WALLET_WITHDRAWAL_REQUESTED", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f27", "sms_wallet_withdrawal_requested"),
    ("WALLET_WITHDRAWAL_REQUESTED", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f28", "email_wallet_withdrawal_requested"),
    ("WALLET_LOW", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f29", "sms_wallet_low"),
    ("WALLET_LOW", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f30", "email_wallet_low"),
    ("OVERDUE_PAYMENT", "SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f31", "sms_overdue_payment"),
    ("OVERDUE_PAYMENT", "EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f32", "email_overdue_payment"),
)


def seed_notification_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    for event_type, channel, template_id, code in TEMPLATES:
        NotificationTemplate.objects.get_or_create(
            event_type=event_type,
            channel=channel,
            defaults={
                "id": template_id,
                "code": code,
                "system": "qb",
                "provider_template": code,
                "subject_template": event_type.replace("_", " ").title() if channel == "EMAIL" else "",
                "description": f"{channel.title()} notification for {event_type}.",
            },
        )


class Migration(migrations.Migration):
    dependencies = [("notifications", "0003_in_app_notifications")]

    operations = [migrations.RunPython(seed_notification_templates, migrations.RunPython.noop)]
