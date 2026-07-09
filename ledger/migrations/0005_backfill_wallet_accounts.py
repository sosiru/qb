from django.db import migrations


def backfill_wallet_accounts(apps, schema_editor):
    User = apps.get_model("eusers", "User")
    Organization = apps.get_model("base", "Organization")
    WalletAccount = apps.get_model("ledger", "WalletAccount")

    for user in User.objects.all().iterator():
        WalletAccount.objects.get_or_create(
            user=user,
            wallet_type="PRIMARY",
            currency="KES",
            defaults={
                "owner_type": "USER",
                "name": user.full_name,
                "available_balance_minor": 0,
                "current_balance_minor": 0,
                "reserved_balance_minor": 0,
                "uncleared_balance_minor": 0,
                "metadata": {"backfilled": True},
            },
        )
        WalletAccount.objects.get_or_create(
            user=user,
            wallet_type="VAULT",
            currency="KES",
            defaults={
                "owner_type": "USER",
                "name": user.full_name,
                "available_balance_minor": 0,
                "current_balance_minor": 0,
                "reserved_balance_minor": 0,
                "uncleared_balance_minor": 0,
                "metadata": {"backfilled": True},
            },
        )

    for organization in Organization.objects.all().iterator():
        WalletAccount.objects.get_or_create(
            organization=organization,
            wallet_type="PRIMARY",
            currency=organization.default_currency or "KES",
            defaults={
                "owner_type": "ORGANIZATION",
                "name": organization.name,
                "available_balance_minor": 0,
                "current_balance_minor": 0,
                "reserved_balance_minor": 0,
                "uncleared_balance_minor": 0,
                "metadata": {"backfilled": True},
            },
        )


def reverse_backfill_wallet_accounts(apps, schema_editor):
    WalletAccount = apps.get_model("ledger", "WalletAccount")
    WalletAccount.objects.filter(metadata__backfilled=True, entries__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0004_align_wallet_account_indexes"),
    ]

    operations = [
        migrations.RunPython(backfill_wallet_accounts, reverse_backfill_wallet_accounts),
    ]

