from django.test import Client, TestCase
from django.utils import timezone

from base.models import Organization, PaymentBatch, PaymentInstruction
from eusers.models import User


class PortalSmokeTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_service_provider_can_open_portal_for_any_organization(self):
        provider = User.objects.create_user(
            phone_number="254700001001",
            password="StrongPass123!",
            full_name="Provider Operator",
            account_type=User.AccountType.SERVICE_PROVIDER,
        )
        organization = Organization.objects.create(name="Acme Ltd", slug="acme-ltd")

        self.client.force_login(provider)
        response = self.client.get(f"/app/?organization_id={organization.id}")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Acme Ltd")
        self.assertContains(response, "Corporate overview")

    def test_statement_page_renders_processing_fees(self):
        user = User.objects.create_user(
            phone_number="254700001002",
            password="StrongPass123!",
            full_name="Statement User",
            account_type=User.AccountType.INDIVIDUAL,
        )
        batch = PaymentBatch.objects.create(
            batch_kind=PaymentBatch.BatchKind.INDIVIDUAL_MONTHLY,
            status=PaymentBatch.Status.SUCCEEDED,
            payment_mode=PaymentBatch.PaymentMode.WALLET,
            user=user,
            scheduled_for=timezone.localdate(),
            total_amount_minor=300000,
            fee_amount_minor=6000,
        )
        PaymentInstruction.objects.create(
            batch=batch,
            recipient_name="KPLC",
            recipient_type="PAYBILL",
            destination={"paybill_number": "888880"},
            amount_minor=300000,
            fee_amount_minor=6000,
            category="utilities",
            status=PaymentInstruction.Status.SUCCEEDED,
        )

        self.client.force_login(user)
        response = self.client.get("/app/statements/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Processing fees")
        self.assertContains(response, "KPLC")
