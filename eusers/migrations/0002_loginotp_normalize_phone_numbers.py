import re

import django.db.models.deletion
from django.db import migrations, models

import base.utils


def normalize_phone_number(phone_number):
    digits = re.sub(r"\D", "", str(phone_number or ""))
    if not digits:
        return ""
    if digits.startswith("254"):
        return digits
    if digits.startswith("0") and len(digits) >= 10:
        return f"254{digits[1:]}"
    if len(digits) == 9:
        return f"254{digits}"
    return digits


def normalize_existing_users(apps, schema_editor):
    User = apps.get_model("eusers", "User")
    seen = set()
    for user in User.objects.all().order_by("date_joined"):
        normalized = normalize_phone_number(user.phone_number)
        if not normalized or normalized in seen:
            continue
        if normalized != user.phone_number and not User.objects.filter(phone_number=normalized).exclude(id=user.id).exists():
            user.phone_number = normalized
            user.save(update_fields=["phone_number"])
        seen.add(user.phone_number)


class Migration(migrations.Migration):

    dependencies = [
        ("eusers", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="LoginOtp",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=base.utils.generate_uuid, editable=False, primary_key=True, serialize=False)),
                ("purpose", models.CharField(choices=[("LOGIN", "Login")], default="LOGIN", max_length=20)),
                ("code_hash", models.CharField(max_length=64)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("max_attempts", models.PositiveSmallIntegerField(default=5)),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="login_otps", to="eusers.user")),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddIndex(
            model_name="loginotp",
            index=models.Index(fields=["user", "purpose", "consumed_at", "expires_at"], name="eusers_logi_user_id_17f33e_idx"),
        ),
        migrations.RunPython(normalize_existing_users, migrations.RunPython.noop),
    ]
