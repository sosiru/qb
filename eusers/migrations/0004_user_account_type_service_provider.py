from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("eusers", "0003_user_payout_controls"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="account_type",
            field=models.CharField(
                choices=[
                    ("INDIVIDUAL", "Individual"),
                    ("CORPORATE", "Corporate"),
                    ("SERVICE_PROVIDER", "Service Provider"),
                    ("SUPERADMIN", "Superadmin"),
                ],
                max_length=20,
            ),
        ),
    ]
