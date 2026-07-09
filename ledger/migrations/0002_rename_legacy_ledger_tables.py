from django.db import migrations


def rename_legacy_tables(apps, schema_editor):
    existing_tables = set(schema_editor.connection.introspection.table_names())
    renames = [
        ("ledger_ledgeraccount", "wallet_accounts"),
        ("ledger_ledgerentry", "ledger_entries"),
    ]
    for old_name, new_name in renames:
        if old_name in existing_tables and new_name not in existing_tables:
            schema_editor.execute(f'ALTER TABLE "{old_name}" RENAME TO "{new_name}"')


def reverse_rename_legacy_tables(apps, schema_editor):
    existing_tables = set(schema_editor.connection.introspection.table_names())
    renames = [
        ("wallet_accounts", "ledger_ledgeraccount"),
        ("ledger_entries", "ledger_ledgerentry"),
    ]
    for old_name, new_name in renames:
        if old_name in existing_tables and new_name not in existing_tables:
            schema_editor.execute(f'ALTER TABLE "{old_name}" RENAME TO "{new_name}"')


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(rename_legacy_tables, reverse_rename_legacy_tables),
    ]

