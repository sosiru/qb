from django.core.management.base import BaseCommand

from notifications.services import create_due_notifications


class Command(BaseCommand):
    help = "Create T-3 reminder notification events for due schedules."

    def handle(self, *args, **options):
        created = create_due_notifications()
        self.stdout.write(self.style.SUCCESS(f"Created {created} reminder events."))
