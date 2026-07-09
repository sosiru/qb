from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("eusers", "0002_loginotp_normalize_phone_numbers"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="payouts_require_owner_approval",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="user",
            name="mpesa_withdrawal_phone",
            field=models.CharField(blank=True, max_length=20),
        ),
    ]
