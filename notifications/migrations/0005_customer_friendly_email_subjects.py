from django.db import migrations


EMAIL_SUBJECTS = {
    "T_MINUS_3": "You have payments due in 3 days",
    "DUE_TODAY": "You have payments due today",
    "PAYMENT_SUCCESS": "Your payments were sent successfully",
    "PAYMENT_FAILURE": "A payment needs your attention",
    "APPROVAL_REQUEST": "Payments are waiting for your approval",
    "BATCH_APPROVED": "Your payments were approved",
    "BATCH_REJECTED": "Your payments were not approved",
    "LOGIN_SUCCESS": "New sign-in to your QuickBills account",
    "WALLET_TOPUP_REQUESTED": "We are adding money to your wallet",
    "WALLET_TOPUP_COMPLETED": "Money added to your wallet",
    "WALLET_WITHDRAWAL_REQUESTED": "Your withdrawal is being processed",
    "WALLET_LOW": "Add money for your upcoming payments",
    "OVERDUE_PAYMENT": "You have overdue payments",
}


def update_email_subjects(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    for event_type, subject in EMAIL_SUBJECTS.items():
        NotificationTemplate.objects.filter(
            event_type=event_type,
            channel="EMAIL",
        ).update(subject_template=subject)


class Migration(migrations.Migration):
    dependencies = [("notifications", "0004_seed_notification_templates")]

    operations = [migrations.RunPython(update_email_subjects, migrations.RunPython.noop)]
