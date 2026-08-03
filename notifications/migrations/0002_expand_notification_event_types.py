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
    ("LOGIN_SUCCESS", "Login Success"),
    ("ORGANIZATION_INVITE", "Organization Invite"),
    ("WALLET_TOPUP_REQUESTED", "Wallet Topup Requested"),
    ("WALLET_TOPUP_COMPLETED", "Wallet Topup Completed"),
    ("WALLET_WITHDRAWAL_REQUESTED", "Wallet Withdrawal Requested"),
    ("WALLET_LOW", "Wallet Low"),
    ("OVERDUE_PAYMENT", "Overdue Payment"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0001_initial"),
    ]

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
    ]
