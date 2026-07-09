from django.db import migrations


def align_wallet_account_indexes(apps, schema_editor):
    if "wallet_accounts" not in schema_editor.connection.introspection.table_names():
        return

    schema_editor.execute('DROP INDEX IF EXISTS "uniq_ledger_user_account_currency"')
    schema_editor.execute('DROP INDEX IF EXISTS "uniq_ledger_org_account_currency"')
    schema_editor.execute('DROP INDEX IF EXISTS "ledger_ledg_account_c54b46_idx"')
    schema_editor.execute('DROP INDEX IF EXISTS "ledger_ledg_status_791714_idx"')

    schema_editor.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS "uniq_wallet_account_user_type_currency" '
        'ON "wallet_accounts" ("user_id", "wallet_type", "currency") '
        'WHERE "user_id" IS NOT NULL'
    )
    schema_editor.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS "uniq_wallet_account_org_type_currency" '
        'ON "wallet_accounts" ("organization_id", "wallet_type", "currency") '
        'WHERE "organization_id" IS NOT NULL'
    )
    schema_editor.execute(
        'CREATE INDEX IF NOT EXISTS "wallet_acco_owner_t_683cb6_idx" '
        'ON "wallet_accounts" ("owner_type", "wallet_type", "currency")'
    )
    schema_editor.execute(
        'CREATE INDEX IF NOT EXISTS "wallet_acco_status_051436_idx" '
        'ON "wallet_accounts" ("status", "created_at")'
    )


def reverse_align_wallet_account_indexes(apps, schema_editor):
    if "wallet_accounts" not in schema_editor.connection.introspection.table_names():
        return

    schema_editor.execute('DROP INDEX IF EXISTS "uniq_wallet_account_user_type_currency"')
    schema_editor.execute('DROP INDEX IF EXISTS "uniq_wallet_account_org_type_currency"')
    schema_editor.execute('DROP INDEX IF EXISTS "wallet_acco_owner_t_683cb6_idx"')
    schema_editor.execute('DROP INDEX IF EXISTS "wallet_acco_status_051436_idx"')

    schema_editor.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS "uniq_ledger_user_account_currency" '
        'ON "wallet_accounts" ("user_id", "owner_type", "currency") '
        'WHERE "user_id" IS NOT NULL'
    )
    schema_editor.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS "uniq_ledger_org_account_currency" '
        'ON "wallet_accounts" ("organization_id", "owner_type", "currency") '
        'WHERE "organization_id" IS NOT NULL'
    )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0003_align_wallet_account_schema"),
    ]

    operations = [
        migrations.RunPython(align_wallet_account_indexes, reverse_align_wallet_account_indexes),
    ]

