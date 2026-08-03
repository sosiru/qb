from django.db import migrations, models


NOTIFICATION_CHANNEL_CHOICES = [
    ("SMS", "SMS"),
    ("EMAIL", "Email"),
    ("IN_APP", "In App"),
]

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
    ("LOGIN_SUCCESS", "Login Success"),
    ("ORGANIZATION_INVITE", "Organization Invite"),
    ("WALLET_TOPUP_REQUESTED", "Wallet Topup Requested"),
    ("WALLET_TOPUP_COMPLETED", "Wallet Topup Completed"),
    ("WALLET_WITHDRAWAL_REQUESTED", "Wallet Withdrawal Requested"),
    ("WALLET_LOW", "Wallet Low"),
    ("OVERDUE_PAYMENT", "Overdue Payment"),
    ("PRODUCT_UPDATE", "Product Update"),
]


def create_product_update_template(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    NotificationTemplate.objects.update_or_create(
        code="in_app_product_update",
        defaults={
            "event_type": "PRODUCT_UPDATE",
            "channel": "IN_APP",
            "system": "qb",
            "provider_template": "in_app_product_update",
            "subject_template": "",
            "description": "In-app announcement for major Quick Bundl updates.",
            "default_context": {"badge": "Product Update"},
            "active": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0002_expand_notification_event_types"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notificationevent",
            name="channel",
            field=models.CharField(choices=NOTIFICATION_CHANNEL_CHOICES, max_length=8),
        ),
        migrations.AlterField(
            model_name="notificationtemplate",
            name="channel",
            field=models.CharField(choices=NOTIFICATION_CHANNEL_CHOICES, max_length=8),
        ),
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
        migrations.AddField(
            model_name="notificationevent",
            name="read_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(create_product_update_template, migrations.RunPython.noop),
    ]
