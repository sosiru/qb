from django.db import migrations


def align_wallet_account_schema(apps, schema_editor):
    table_names = set(schema_editor.connection.introspection.table_names())
    if "wallet_accounts" not in table_names:
        return

    columns = {
        column.name
        for column in schema_editor.connection.introspection.get_table_description(
            schema_editor.connection.cursor(),
            "wallet_accounts",
        )
    }
    if "account_type" in columns and "owner_type" not in columns:
        schema_editor.execute('ALTER TABLE "wallet_accounts" RENAME COLUMN "account_type" TO "owner_type"')
    if "wallet_type" not in columns:
        schema_editor.execute('ALTER TABLE "wallet_accounts" ADD COLUMN "wallet_type" varchar(20) NOT NULL DEFAULT "PRIMARY"')


def reverse_align_wallet_account_schema(apps, schema_editor):
    table_names = set(schema_editor.connection.introspection.table_names())
    if "wallet_accounts" not in table_names:
        return

    columns = {
        column.name
        for column in schema_editor.connection.introspection.get_table_description(
            schema_editor.connection.cursor(),
            "wallet_accounts",
        )
    }
    if "owner_type" in columns and "account_type" not in columns:
        schema_editor.execute('ALTER TABLE "wallet_accounts" RENAME COLUMN "owner_type" TO "account_type"')


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0002_rename_legacy_ledger_tables"),
    ]

    operations = [
        migrations.RunPython(align_wallet_account_schema, reverse_align_wallet_account_schema),
    ]

