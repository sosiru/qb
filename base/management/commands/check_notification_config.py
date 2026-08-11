from django.core.management.base import BaseCommand, CommandError

from notifications.services import (
    NotificationDispatchError,
    NotificationInterface,
    validate_notification_configuration,
)


class Command(BaseCommand):
    help = "Validate notification provider settings and optionally send a live test notification."

    def add_arguments(self, parser):
        parser.add_argument("--recipient", help="Email address or phone number to receive a live test.")
        parser.add_argument("--type", choices=["email", "sms"], default="email")
        parser.add_argument("--require-email-backup", action="store_true")

    def handle(self, *args, **options):
        try:
            result = validate_notification_configuration(
                require_email_backup=options["require_email_backup"]
            )
        except NotificationDispatchError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Notification provider configuration is present."))
        self.stdout.write(f"Provider URL: {result['notify_url']}")
        self.stdout.write(f"API key length: {result['api_key_length']}")
        for warning in result["warnings"]:
            self.stdout.write(self.style.WARNING(warning))

        recipient = options.get("recipient")
        if not recipient:
            return

        interface = NotificationInterface()
        message = "QuickBills notification configuration test."
        try:
            if options["type"] == "sms":
                response = interface.send_sms(message, [recipient], unique_identifier="notify-config-test")
            else:
                response = interface.send_email(message, [recipient], unique_identifier="notify-config-test")
        except NotificationDispatchError as exc:
            raise CommandError(f"Live notification test failed: {exc}") from exc

        self.stdout.write(self.style.SUCCESS(f"Live {options['type']} test sent: {response}"))
