from decimal import Decimal

from django.db import migrations, models


LEDGER_AMOUNT_FIELDS = {
    "Account": (
        "current_balance_minor",
        "reserved_balance_minor",
        "available_balance_minor",
        "uncleared_balance_minor",
        "charge_balance_minor",
    ),
    "Transaction": ("amount_minor",),
    "BalanceLog": ("amount_transacted_minor", "total_balance_minor"),
    "BalanceLogEntry": ("amount_transacted_minor", "balance_before_minor", "balance_after_minor"),
}


def minor_units_to_major(apps, schema_editor):
    for model_name, fields in LEDGER_AMOUNT_FIELDS.items():
        model = apps.get_model("ledger", model_name)
        for instance in model.objects.all().only("pk", *fields).iterator():
            updates = []
            for field in fields:
                value = getattr(instance, field)
                if value is None:
                    continue
                setattr(instance, field, (Decimal(value) / Decimal("100")).quantize(Decimal("0.01")))
                updates.append(field)
            if updates:
                instance.save(update_fields=updates)


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0002_backfill_account_numbers"),
    ]

    operations = [
        migrations.AlterField(
            model_name="account",
            name="current_balance_minor",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AlterField(
            model_name="account",
            name="reserved_balance_minor",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AlterField(
            model_name="account",
            name="available_balance_minor",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AlterField(
            model_name="account",
            name="uncleared_balance_minor",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AlterField(
            model_name="account",
            name="charge_balance_minor",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AlterField(
            model_name="transaction",
            name="amount_minor",
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.AlterField(
            model_name="balancelog",
            name="amount_transacted_minor",
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.AlterField(
            model_name="balancelog",
            name="total_balance_minor",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AlterField(
            model_name="balancelogentry",
            name="amount_transacted_minor",
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.AlterField(
            model_name="balancelogentry",
            name="balance_before_minor",
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.AlterField(
            model_name="balancelogentry",
            name="balance_after_minor",
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.RunPython(minor_units_to_major, migrations.RunPython.noop),
    ]
