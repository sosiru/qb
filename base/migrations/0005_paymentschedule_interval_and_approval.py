import calendar

from django.db import migrations, models
from django.utils import timezone


def _calculate_initial_due_date(day_of_month):
    today = timezone.localdate()
    year = today.year
    month = today.month
    target_day = min(day_of_month, calendar.monthrange(year, month)[1])
    due_date = today.replace(day=target_day)
    if due_date < today:
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        target_day = min(day_of_month, calendar.monthrange(year, month)[1])
        due_date = due_date.replace(year=year, month=month, day=target_day)
    return due_date


def populate_schedule_defaults(apps, schema_editor):
    PaymentSchedule = apps.get_model("base", "PaymentSchedule")
    for schedule in PaymentSchedule.objects.all().iterator():
        schedule.interval_months = 1
        schedule.next_due_date = _calculate_initial_due_date(schedule.day_of_month)
        schedule.requires_approval = False
        schedule.save(update_fields=["interval_months", "next_due_date", "requires_approval"])


class Migration(migrations.Migration):

    dependencies = [
        ("base", "0004_paymentinstruction_fee_amount_minor"),
    ]

    operations = [
        migrations.AddField(
            model_name="paymentschedule",
            name="interval_months",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="paymentschedule",
            name="next_due_date",
            field=models.DateField(default=timezone.localdate),
        ),
        migrations.AddField(
            model_name="paymentschedule",
            name="requires_approval",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="paymentschedule",
            constraint=models.CheckConstraint(condition=models.Q(("interval_months__gte", 1)), name="schedule_interval_months_gte_1"),
        ),
        migrations.RunPython(populate_schedule_defaults, migrations.RunPython.noop),
    ]
