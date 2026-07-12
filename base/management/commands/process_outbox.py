import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from base.models import OutboxEvent
from base.payment_microservice_executor import fail_instruction_event, process_outbox_event

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process pending payment outbox events, including collection and payout dispatch."

    def handle(self, *args, **options):
        processed = 0
        failed = 0
        events = OutboxEvent.objects.filter(status=OutboxEvent.Status.PENDING).order_by("available_at")[:100]
        logger.info("outbox.command.start pending_count=%s", len(events))
        for event in events:
            try:
                with transaction.atomic():
                    logger.info(
                        "outbox.command.event.start event_id=%s topic=%s aggregate_type=%s aggregate_id=%s attempts=%s payload=%s",
                        event.id,
                        event.topic,
                        event.aggregate_type,
                        event.aggregate_id,
                        event.attempts,
                        event.payload,
                    )
                    event.status = OutboxEvent.Status.PROCESSING
                    event.attempts += 1
                    event.save(update_fields=["status", "attempts", "updated_at"])
                    process_outbox_event(event)
                    event.status = OutboxEvent.Status.DONE
                    event.last_error = ""
                    event.save(update_fields=["status", "last_error", "updated_at"])
                    logger.info("outbox.command.event.success event_id=%s topic=%s", event.id, event.topic)
                    processed += 1
            except Exception as exc:
                event.status = OutboxEvent.Status.FAILED
                event.last_error = str(exc)[:255]
                event.save(update_fields=["status", "last_error", "updated_at"])
                logger.exception("outbox.command.event.failed event_id=%s topic=%s error=%s", event.id, event.topic, exc)
                fail_instruction_event(event, exc)
                failed += 1
        self.stdout.write(self.style.SUCCESS(f"Processed {processed} outbox events; {failed} failed."))
