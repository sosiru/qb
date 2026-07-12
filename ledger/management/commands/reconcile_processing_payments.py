from django.core.management.base import BaseCommand

from ledger.services import PaymentInterface


class Command(BaseCommand):
    help = "Query the payment microservice for payment requests stuck in PROCESSING."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-seconds", type=int, default=120)
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        processed = PaymentInterface(sandbox=False).retry_stale_processing(
            older_than_seconds=options["older_than_seconds"],
            limit=options["limit"],
        )
        self.stdout.write(self.style.SUCCESS(f"Queried {processed} processing payment request(s)."))
