from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("eusers", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="loginotp",
            name="purpose",
            field=models.CharField(
                choices=[("LOGIN", "Login"), ("PASSWORD_RESET", "Password Reset")],
                default="LOGIN",
                max_length=20,
            ),
        ),
    ]
