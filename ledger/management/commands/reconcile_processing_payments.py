from django.core.management.base import BaseCommand

from ledger.services import PaymentInterface


class Command(BaseCommand):
    help = "Fail payment requests stuck in PROCESSING past the timeout."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-seconds", type=int, default=180)
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument(
            "--query-status",
            action="store_true",
            help="Query the payment microservice before failing timed-out requests.",
        )
        sandbox_group = parser.add_mutually_exclusive_group()
        sandbox_group.add_argument("--sandbox", action="store_true", help="Use sandbox mode for this run.")
        sandbox_group.add_argument("--live", action="store_true", help="Use live payment microservice mode for this run.")

    def handle(self, *args, **options):
        sandbox = None
        if options["sandbox"]:
            sandbox = True
        elif options["live"]:
            sandbox = False

        processed = PaymentInterface(sandbox=sandbox).retry_stale_processing(
            older_than_seconds=options["older_than_seconds"],
            limit=options["limit"],
            query_status=options["query_status"],
        )
        self.stdout.write(self.style.SUCCESS(f"Reconciled {processed} processing payment request(s)."))
