from django.db import migrations, models


NOTIFICATION_EVENT_TYPE_CHOICES = [
    ("T_MINUS_3", "T Minus 3"),
    ("DUE_TODAY", "Due Today"),
    ("PAYMENT_SUCCESS", "Payment Success"),
    ("PAYMENT_FAILURE", "Payment Failure"),
    ("APPROVAL_REQUEST", "Approval Request"),
    ("BATCH_APPROVED", "Batch Approved"),
    ("BATCH_REJECTED", "Batch Rejected"),
    ("SELF_ONBOARDING", "Self Onboarding"),
    ("LOGIN_OTP", "Login OTP"),
    ("PASSWORD_RESET", "Password Reset"),
    ("LOGIN_SUCCESS", "Login Success"),
    ("ORGANIZATION_INVITE", "Organization Invite"),
    ("WALLET_TOPUP_REQUESTED", "Wallet Topup Requested"),
    ("WALLET_TOPUP_COMPLETED", "Wallet Topup Completed"),
    ("WALLET_WITHDRAWAL_REQUESTED", "Wallet Withdrawal Requested"),
    ("WALLET_LOW", "Wallet Low"),
    ("OVERDUE_PAYMENT", "Overdue Payment"),
    ("PRODUCT_UPDATE", "Product Update"),
]


def seed_password_reset_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    templates = (
        ("SMS", "4ed97131-f7f6-4da8-ac47-40cb87755f33", "sms_password_reset", ""),
        ("EMAIL", "4ed97131-f7f6-4da8-ac47-40cb87755f34", "email_password_reset", "Reset your QuickBills password"),
    )
    for channel, template_id, code, subject in templates:
        NotificationTemplate.objects.get_or_create(
            event_type="PASSWORD_RESET",
            channel=channel,
            defaults={
                "id": template_id,
                "code": code,
                "system": "qb",
                "provider_template": f"{channel.lower()}_default",
                "subject_template": subject,
                "description": f"{channel.title()} password reset code notification.",
            },
        )


class Migration(migrations.Migration):
    dependencies = [("notifications", "0005_customer_friendly_email_subjects")]

    operations = [
        migrations.AlterField(
            model_name="notificationevent",
            name="event_type",
            field=models.CharField(choices=NOTIFICATION_EVENT_TYPE_CHOICES, max_length=32),
        ),
        migrations.AlterField(
            model_name="notificationtemplate",
            name="event_type",
            field=models.CharField(choices=NOTIFICATION_EVENT_TYPE_CHOICES, max_length=32),
        ),
        migrations.RunPython(seed_password_reset_templates, migrations.RunPython.noop),
    ]
