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
            try:
                with transaction.atomic():
                    process_notification_event(event)
                    processed += 1
            except Exception as exc:
                event.status = NotificationEvent.Status.FAILED
                event.last_error = str(exc)[:255]
                event.provider_response = {"error": str(exc)}
                event.save(update_fields=["status", "last_error", "provider_response", "updated_at"])
                failed += 1
        self.stdout.write(self.style.SUCCESS(f"Processed {processed} notifications; {failed} failed."))
