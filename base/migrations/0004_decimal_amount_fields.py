from decimal import Decimal

from django.db import migrations, models


BASE_AMOUNT_FIELDS = {
    "ReconciliationException": ("expected_amount_minor", "actual_amount_minor"),
    "PaymentSchedule": ("amount_minor",),
    "PaymentBatch": ("total_amount_minor", "fee_amount_minor"),
    "PaymentInstruction": ("amount_minor", "fee_amount_minor"),
}


def minor_units_to_major(apps, schema_editor):
    for model_name, fields in BASE_AMOUNT_FIELDS.items():
        model = apps.get_model("base", model_name)
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
        ("base", "0003_expensecategory_taxonomy"),
    ]

    operations = [
        migrations.AlterField(
            model_name="reconciliationexception",
            name="expected_amount_minor",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AlterField(
            model_name="reconciliationexception",
            name="actual_amount_minor",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AlterField(
            model_name="paymentschedule",
            name="amount_minor",
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.AlterField(
            model_name="paymentbatch",
            name="total_amount_minor",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AlterField(
            model_name="paymentbatch",
            name="fee_amount_minor",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AlterField(
            model_name="paymentinstruction",
            name="amount_minor",
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.AlterField(
            model_name="paymentinstruction",
            name="fee_amount_minor",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.RunPython(minor_units_to_major, migrations.RunPython.noop),
    ]
