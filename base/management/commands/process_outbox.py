from django.core.management.base import BaseCommand
from django.db import transaction

from base.models import OutboxEvent
from base.provider_executor import fail_instruction_event, process_outbox_event


class Command(BaseCommand):
    help = "Process pending outbox events, including PesaWay collection and payout dispatch."

    def handle(self, *args, **options):
        processed = 0
        failed = 0
        events = OutboxEvent.objects.filter(status=OutboxEvent.Status.PENDING).order_by("available_at")[:100]
        for event in events:
            try:
                with transaction.atomic():
                    event.status = OutboxEvent.Status.PROCESSING
                    event.attempts += 1
                    event.save(update_fields=["status", "attempts", "updated_at"])
                    process_outbox_event(event)
                    event.status = OutboxEvent.Status.DONE
                    event.last_error = ""
                    event.save(update_fields=["status", "last_error", "updated_at"])
                    processed += 1
            except Exception as exc:
                event.status = OutboxEvent.Status.FAILED
                event.last_error = str(exc)[:255]
                event.save(update_fields=["status", "last_error", "updated_at"])
                fail_instruction_event(event, exc)
                failed += 1
        self.stdout.write(self.style.SUCCESS(f"Processed {processed} outbox events; {failed} failed."))
