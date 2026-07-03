from django.core.management.base import BaseCommand

from base.services import run_due_wallet_autopayments


class Command(BaseCommand):
    help = "Execute due individual wallet-funded schedules."

    def handle(self, *args, **options):
        processed = run_due_wallet_autopayments()
        self.stdout.write(self.style.SUCCESS(f"Processed {processed} due wallet autopayments."))
