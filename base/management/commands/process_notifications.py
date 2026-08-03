from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from notifications.models import NotificationEvent
from notifications.services import process_notification_event


class Command(BaseCommand):
    help = "Dispatch pending SMS and email notifications from the database queue."

    def handle(self, *args, **options):
        processed = 0
        failed = 0
        events = NotificationEvent.objects.filter(
            status=NotificationEvent.Status.PENDING,
            scheduled_for__lte=timezone.now(),
            template__isnull=False,
        ).select_related("template", "user").order_by("scheduled_for")[:100]

        for event in events:
            with transaction.atomic():
                event.refresh_from_db()
                if event.status != NotificationEvent.Status.PENDING:
                    continue
                response = process_notification_event(event)
            if response and response.get("status") == "failed":
                failed += 1
            else:
                processed += 1
        self.stdout.write(self.style.SUCCESS(f"Processed {processed} notifications; {failed} failed."))
