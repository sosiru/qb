from django.core.management.base import BaseCommand

from ledger.services import PaymentService


class Command(BaseCommand):
    help = "Query payment status after two minutes and fail requests still pending after five minutes."

    def add_arguments(self, parser):
        parser.add_argument("--query-after-seconds", type=int, default=PaymentService.STATUS_QUERY_AFTER_SECONDS)
        parser.add_argument("--timeout-seconds", type=int, default=PaymentService.PROCESSING_TIMEOUT_SECONDS)
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument(
            "--query-status",
            action="store_true",
            default=True,
            help="Query supported payment status endpoints (enabled by default).",
        )
        parser.add_argument(
            "--no-query-status",
            action="store_false",
            dest="query_status",
            help="Disable status queries and rely on callbacks until the timeout.",
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

        processed = PaymentService(sandbox=sandbox).retry_stale_processing(
            query_after_seconds=options["query_after_seconds"],
            timeout_seconds=options["timeout_seconds"],
            limit=options["limit"],
            query_status=options["query_status"],
        )
        self.stdout.write(self.style.SUCCESS(f"Reconciled {processed} processing payment request(s)."))
