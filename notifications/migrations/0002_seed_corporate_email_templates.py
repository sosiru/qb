from django.db import migrations


TEMPLATES = [
    ("email_self_onboarding", "SELF_ONBOARDING", "Welcome to Quick Bundl"),
    ("email_login_otp", "LOGIN_OTP", "Your Quick Bundl login code"),
    ("email_login_success", "LOGIN_SUCCESS", "New Quick Bundl login"),
    ("email_organization_invite", "ORGANIZATION_INVITE", "You're invited to {{ organization_name }}"),
]


def seed_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    for code, event_type, subject in TEMPLATES:
        NotificationTemplate.objects.update_or_create(
            code=code,
            defaults={
                "event_type": event_type,
                "channel": "EMAIL",
                "system": "quickbundl",
                "provider_template": "quickbundl_corporate_email",
                "subject_template": subject,
                "description": f"Quick Bundl corporate email for {event_type.lower().replace('_', ' ')}.",
                "default_context": {},
                "active": True,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_templates, migrations.RunPython.noop),
    ]
