from django.core.management.base import BaseCommand

from notifications.services import create_all_reminder_notifications


class Command(BaseCommand):
    help = "Create reminder notification events for due, low-wallet, and overdue schedules."

    def handle(self, *args, **options):
        created = create_all_reminder_notifications()
        total = sum(created.values())
        self.stdout.write(self.style.SUCCESS(f"Created {total} reminder events: {created}."))
