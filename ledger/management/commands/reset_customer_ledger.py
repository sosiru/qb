from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from eusers.models import User
from eusers.utils import normalize_phone_number
from ledger.models import Account, Transaction


class Command(BaseCommand):
    help = "Delete a customer's user-wallet ledger transactions and reset every balance to zero."

    def add_arguments(self, parser):
        parser.add_argument("phone_number", help="Customer phone number, for example 0712345678 or 254712345678.")
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Apply the reset. Without this flag the command only prints what would change.",
        )

    def handle(self, *args, **options):
        phone_number = normalize_phone_number(options["phone_number"])
        if not phone_number:
            raise CommandError("Enter a valid customer phone number.")

        try:
            user = User.objects.get(phone_number=phone_number)
        except User.DoesNotExist as exc:
            raise CommandError(f"No customer found with phone number {phone_number}.") from exc

        accounts = Account.objects.filter(user=user).order_by("account_kind", "currency")
        account_ids = list(accounts.values_list("id", flat=True))
        transaction_count = Transaction.objects.filter(account_id__in=account_ids).count()
        summary = (
            f"Customer {phone_number}: {len(account_ids)} user wallet(s), "
            f"{transaction_count} ledger transaction(s)."
        )

        if not options["confirm"]:
            self.stdout.write(summary)
            self.stdout.write(self.style.WARNING("Dry run only. Re-run with --confirm to apply the reset."))
            return

        with transaction.atomic():
            locked_account_ids = list(
                Account.objects.select_for_update().filter(id__in=account_ids).values_list("id", flat=True)
            )
            Transaction.objects.filter(account_id__in=account_ids).delete()
            reset_count = Account.objects.filter(id__in=locked_account_ids).update(
                current_balance_minor=0,
                reserved_balance_minor=0,
                available_balance_minor=0,
                uncleared_balance_minor=0,
                charge_balance_minor=0,
            )

        self.stdout.write(summary)
        self.stdout.write(
            self.style.SUCCESS(
                f"Reset {reset_count} user wallet(s) to zero and deleted {transaction_count} ledger transaction(s)."
            )
        )
