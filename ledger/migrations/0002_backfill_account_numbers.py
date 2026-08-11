from django.db import migrations


def backfill_account_numbers(apps, schema_editor):
    Account = apps.get_model("ledger", "Account")
    existing = set(
        Account.objects.exclude(account_number="")
        .values_list("account_number", flat=True)
    )

    for account in Account.objects.filter(account_number="").order_by("created_at", "id"):
        seed = str(account.id).replace("-", "")[-10:].upper()
        candidate = f"QB{seed}"
        suffix = 1
        while candidate in existing:
            candidate = f"QB{seed[:8]}{suffix:02d}"
            suffix += 1
        account.account_number = candidate
        account.save(update_fields=["account_number"])
        existing.add(candidate)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(backfill_account_numbers, migrations.RunPython.noop),
    ]
