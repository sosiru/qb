from django.db import migrations, models


def populate_instruction_fees(apps, schema_editor):
    PaymentInstruction = apps.get_model("base", "PaymentInstruction")
    PaymentBatch = apps.get_model("base", "PaymentBatch")

    for instruction in PaymentInstruction.objects.all().iterator():
        instruction.fee_amount_minor = max(0, instruction.amount_minor * 200 // 10000)
        instruction.save(update_fields=["fee_amount_minor"])

    for batch in PaymentBatch.objects.all().iterator():
        total_fee = sum(batch.instructions.values_list("fee_amount_minor", flat=True))
        batch.fee_amount_minor = total_fee
        batch.save(update_fields=["fee_amount_minor"])


class Migration(migrations.Migration):

    dependencies = [
        ("base", "0003_payeepreset_and_payee_preset"),
    ]

    operations = [
        migrations.AddField(
            model_name="paymentinstruction",
            name="fee_amount_minor",
            field=models.BigIntegerField(default=0),
        ),
        migrations.RunPython(populate_instruction_fees, migrations.RunPython.noop),
    ]
