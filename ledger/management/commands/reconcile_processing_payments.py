from django.core.management.base import BaseCommand

from ledger.services import PaymentInterface


class Command(BaseCommand):
    help = "Query and fail payment requests stuck in PROCESSING past the timeout."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-seconds", type=int, default=180)
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        processed = PaymentInterface(sandbox=False).retry_stale_processing(
            older_than_seconds=options["older_than_seconds"],
            limit=options["limit"],
        )
        self.stdout.write(self.style.SUCCESS(f"Reconciled {processed} processing payment request(s)."))
