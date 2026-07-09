from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("base", "0005_paymentschedule_interval_and_approval"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="registration_number",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
