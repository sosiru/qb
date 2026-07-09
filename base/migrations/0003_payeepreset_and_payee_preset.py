import base.utils
import django.db.models.deletion
from django.db import migrations, models


def seed_default_payee_presets(apps, schema_editor):
    PayeePreset = apps.get_model("base", "PayeePreset")
    PayeePreset.objects.get_or_create(
        label="KPLC",
        defaults={
            "payee_type": "PAYBILL",
            "paybill_number": "888880",
            "expense_category": "utilities",
            "active": True,
        },
    )


def remove_default_payee_presets(apps, schema_editor):
    PayeePreset = apps.get_model("base", "PayeePreset")
    PayeePreset.objects.filter(label="KPLC", paybill_number="888880").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("base", "0002_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="PayeePreset",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=base.utils.generate_uuid, editable=False, primary_key=True, serialize=False)),
                ("label", models.CharField(max_length=255, unique=True)),
                (
                    "payee_type",
                    models.CharField(
                        choices=[
                            ("PAYBILL", "M-Pesa Paybill"),
                            ("TILL", "M-Pesa Till"),
                            ("MOBILE", "Mobile Send Money"),
                            ("BANK", "Bank Account"),
                        ],
                        max_length=16,
                    ),
                ),
                ("paybill_number", models.CharField(blank=True, max_length=20)),
                ("till_number", models.CharField(blank=True, max_length=20)),
                ("expense_category", models.CharField(default="general", max_length=64)),
                ("active", models.BooleanField(default=True)),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddField(
            model_name="payee",
            name="preset",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="payees",
                to="base.payeepreset",
            ),
        ),
        migrations.RunPython(seed_default_payee_presets, remove_default_payee_presets),
    ]
