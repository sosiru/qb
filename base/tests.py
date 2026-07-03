import json
from unittest.mock import patch

from django.core.management import call_command
from django.test import Client, TestCase
from django.test.utils import override_settings
from django.utils import timezone

from eusers.models import User
from notifications.models import NotificationEvent
from notifications.services import queue_notifications_for_user

from .models import OrganizationMembership, OutboxEvent, PaymentBatch, PaymentInstruction, Wallet


class RoutePlatformTests(TestCase):
    fixtures = ["notification_templates.json"]

    def setUp(self):
        self.client = Client()

    def _post(self, path, payload, token=None):
        headers = {}
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return self.client.post(path, data=json.dumps(payload), content_type="application/json", **headers)

    def _patch(self, path, payload, token=None):
        headers = {}
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return self.client.patch(path, data=json.dumps(payload), content_type="application/json", **headers)

    def _delete(self, path, token=None):
        headers = {}
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return self.client.delete(path, content_type="application/json", **headers)

    def test_individual_payment_flow(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "+254700000001",
                "password": "StrongPass123!",
                "full_name": "Alice Example",
                "account_type": "INDIVIDUAL",
            },
        )
        self.assertEqual(response.status_code, 201)
        token = response.json()["token"]

        response = self._post(
            "/api/v1/payees/",
            {
                "label": "KPLC",
                "payee_type": "PAYBILL",
                "paybill_number": "888880",
                "account_reference": "ACC123",
                "expense_category": "utilities",
            },
            token=token,
        )
        self.assertEqual(response.status_code, 201)
        payee_id = response.json()["payee"]["id"]

        response = self._post(
            "/api/v1/schedules/",
            {
                "payee_id": payee_id,
                "amount_minor": 300000,
                "day_of_month": timezone.localdate().day,
            },
            token=token,
        )
        self.assertEqual(response.status_code, 201)

        response = self._post("/api/v1/wallets/topups/", {"amount_minor": 500000}, token=token)
        self.assertEqual(response.status_code, 200)

        response = self._post("/api/v1/payments/pay-all/", {"payment_mode": "WALLET"}, token=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["status"], "SUCCEEDED")

    def test_individual_vault_transfer(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "+254700000002",
                "password": "StrongPass123!",
                "full_name": "Vault User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]

        self._post("/api/v1/wallets/topups/", {"amount_minor": 200000}, token=token)
        response = self._post("/api/v1/wallets/vault/", {"amount_minor": 50000}, token=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["vault_wallet"]["available_balance_minor"], 50000)

    def test_individual_can_top_up_directly_to_vault(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "+254700000005",
                "password": "StrongPass123!",
                "full_name": "Direct Vault User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]

        response = self._post(
            "/api/v1/wallets/topups/",
            {"amount_minor": 120000, "wallet_type": "VAULT"},
            token=token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["wallet"]["wallet_type"], "VAULT")
        self.assertEqual(response.json()["wallet"]["available_balance_minor"], 120000)

        primary_wallet = Wallet.objects.get(user__phone_number="+254700000005", wallet_type=Wallet.WalletType.PRIMARY)
        vault_wallet = Wallet.objects.get(user__phone_number="+254700000005", wallet_type=Wallet.WalletType.VAULT)
        self.assertEqual(primary_wallet.available_balance_minor, 0)
        self.assertEqual(vault_wallet.available_balance_minor, 120000)

    def test_profile_update_and_wallet_ledger_endpoint(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "+254700000003",
                "password": "StrongPass123!",
                "full_name": "Profile User",
                "email": "before@example.com",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]

        response = self._patch(
            "/api/v1/auth/me/",
            {
                "full_name": "Updated Profile User",
                "email": "after@example.com",
                "sms_notifications_enabled": False,
                "default_payment_mode": "STK",
            },
            token=token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["full_name"], "Updated Profile User")
        self.assertEqual(response.json()["user"]["default_payment_mode"], "STK")
        self.assertFalse(response.json()["user"]["sms_notifications_enabled"])

        response = self._post("/api/v1/wallets/topups/", {"amount_minor": 150000}, token=token)
        self.assertEqual(response.status_code, 200)

        response = self.client.get("/api/v1/wallets/ledger/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["entries"]), 1)
        self.assertEqual(response.json()["entries"][0]["entry_type"], "TOP_UP")

    def test_payee_and_schedule_crud_endpoints(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "+254700000004",
                "password": "StrongPass123!",
                "full_name": "CRUD User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]

        response = self._post(
            "/api/v1/payees/",
            {
                "label": "Family Transfer",
                "payee_type": "MOBILE",
                "phone_number": "+254700123123",
                "expense_category": "family",
            },
            token=token,
        )
        self.assertEqual(response.status_code, 201)
        payee_id = response.json()["payee"]["id"]

        response = self._patch(
            f"/api/v1/payees/{payee_id}/",
            {"label": "Updated Family Transfer", "active": False},
            token=token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payee"]["label"], "Updated Family Transfer")
        self.assertFalse(response.json()["payee"]["active"])

        response = self.client.get(
            "/api/v1/payees/?active=false&q=Updated",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["payees"]), 1)

        response = self._post(
            "/api/v1/schedules/",
            {
                "payee_id": payee_id,
                "amount_minor": 25000,
                "day_of_month": 12,
                "active": True,
            },
            token=token,
        )
        self.assertEqual(response.status_code, 201)
        schedule_id = response.json()["schedule"]["id"]

        response = self._patch(
            f"/api/v1/schedules/{schedule_id}/",
            {"day_of_month": 14, "active": False},
            token=token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["schedule"]["day_of_month"], 14)
        self.assertFalse(response.json()["schedule"]["active"])

        response = self.client.get(
            "/api/v1/schedules/?active=false&category=family",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["schedules"]), 1)

        response = self._delete(f"/api/v1/schedules/{schedule_id}/", token=token)
        self.assertEqual(response.status_code, 200)
        response = self._delete(f"/api/v1/payees/{payee_id}/", token=token)
        self.assertEqual(response.status_code, 200)

    def test_corporate_maker_checker_flow(self):
        admin_response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "+254700000010",
                "password": "StrongPass123!",
                "full_name": "Finance Admin",
                "account_type": "CORPORATE",
                "organization_name": "Bundl Labs",
            },
        )
        admin_token = admin_response.json()["token"]
        admin_user_id = admin_response.json()["user"]["id"]
        self.assertTrue(admin_user_id)

        checker_response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "+254700000011",
                "password": "StrongPass123!",
                "full_name": "Checker User",
                "account_type": "CORPORATE",
            },
        )
        checker_token = checker_response.json()["token"]
        checker_user_id = checker_response.json()["user"]["id"]

        dashboard = self.client.get("/api/v1/dashboard/", HTTP_AUTHORIZATION=f"Bearer {admin_token}").json()
        organization_id = dashboard["dashboard"]["organizations"][0]["organization_id"]

        response = self._post(
            f"/api/v1/organizations/{organization_id}/members/",
            {"user_id": checker_user_id, "role": "CHECKER"},
            token=admin_token,
        )
        self.assertEqual(response.status_code, 201)

        response = self._post(
            "/api/v1/wallets/topups/",
            {"organization_id": organization_id, "amount_minor": 900000},
            token=admin_token,
        )
        self.assertEqual(response.status_code, 200)

        csv_content = "\n".join(
            [
                "recipient_name,recipient_type,amount_minor,category,phone_number,external_reference",
                "Vendor A,MOBILE,100000,payroll,+254711111111,EMP001",
                "Vendor B,MOBILE,50000,payroll,+254722222222,EMP002",
            ]
        )
        response = self._post(
            "/api/v1/corporate/batches/upload/",
            {
                "organization_id": organization_id,
                "scheduled_for": str(timezone.localdate()),
                "payment_mode": "WALLET",
                "csv_content": csv_content,
            },
            token=admin_token,
        )
        self.assertEqual(response.status_code, 201)
        batch_id = response.json()["batch"]["id"]

        response = self._post(f"/api/v1/corporate/batches/{batch_id}/submit/", {}, token=admin_token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["status"], "PENDING_APPROVAL")

        response = self._post(f"/api/v1/corporate/batches/{batch_id}/approve/", {}, token=checker_token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["status"], "SUCCEEDED")

        batch = PaymentBatch.objects.get(id=batch_id)
        wallet = Wallet.objects.get(organization_id=organization_id, wallet_type=Wallet.WalletType.PRIMARY)
        self.assertEqual(batch.status, PaymentBatch.Status.SUCCEEDED)
        self.assertEqual(wallet.available_balance_minor, 750000)
        self.assertTrue(
            OrganizationMembership.objects.filter(
                organization_id=organization_id,
                user_id=checker_user_id,
                role="CHECKER",
            ).exists()
        )

    def test_corporate_member_management_and_batch_rejection(self):
        admin_response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "+254700000012",
                "password": "StrongPass123!",
                "full_name": "Ops Admin",
                "account_type": "CORPORATE",
                "organization_name": "Route Corp",
            },
        )
        admin_token = admin_response.json()["token"]

        maker_response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "+254700000013",
                "password": "StrongPass123!",
                "full_name": "Maker User",
                "account_type": "CORPORATE",
            },
        )
        maker_user_id = maker_response.json()["user"]["id"]

        checker_response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "+254700000014",
                "password": "StrongPass123!",
                "full_name": "Checker User Two",
                "email": "checker2@example.com",
                "account_type": "CORPORATE",
            },
        )
        checker_token = checker_response.json()["token"]
        checker_user_id = checker_response.json()["user"]["id"]

        organization_id = self.client.get(
            "/api/v1/dashboard/",
            HTTP_AUTHORIZATION=f"Bearer {admin_token}",
        ).json()["dashboard"]["organizations"][0]["organization_id"]

        maker_membership = self._post(
            f"/api/v1/organizations/{organization_id}/members/",
            {"user_id": maker_user_id, "role": "MAKER"},
            token=admin_token,
        ).json()["membership"]["id"]
        checker_membership = self._post(
            f"/api/v1/organizations/{organization_id}/members/",
            {"user_id": checker_user_id, "role": "CHECKER"},
            token=admin_token,
        ).json()["membership"]["id"]

        response = self.client.get(
            f"/api/v1/organizations/{organization_id}/members/?role=CHECKER",
            HTTP_AUTHORIZATION=f"Bearer {admin_token}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["memberships"]), 1)

        response = self._patch(
            f"/api/v1/organizations/{organization_id}/members/{maker_membership}/",
            {"role": "ADMIN"},
            token=admin_token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["membership"]["role"], "ADMIN")

        response = self._delete(
            f"/api/v1/organizations/{organization_id}/members/{maker_membership}/",
            token=admin_token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["membership"]["is_active"])

        self._post(
            "/api/v1/wallets/topups/",
            {"organization_id": organization_id, "amount_minor": 500000},
            token=admin_token,
        )
        csv_content = "\n".join(
            [
                "recipient_name,recipient_type,amount_minor,category,phone_number,external_reference",
                "Vendor A,MOBILE,100000,payroll,+254711111111,EMP001",
            ]
        )
        response = self._post(
            "/api/v1/corporate/batches/upload/",
            {
                "organization_id": organization_id,
                "scheduled_for": str(timezone.localdate()),
                "payment_mode": "WALLET",
                "csv_content": csv_content,
            },
            token=admin_token,
        )
        batch_id = response.json()["batch"]["id"]

        response = self._post(f"/api/v1/corporate/batches/{batch_id}/submit/", {}, token=admin_token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["status"], "PENDING_APPROVAL")

        response = self._post(
            f"/api/v1/corporate/batches/{batch_id}/reject/",
            {"reason": "Incorrect beneficiary amount"},
            token=checker_token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["status"], "REJECTED")
        self.assertEqual(response.json()["batch"]["metadata"]["rejection_reason"], "Incorrect beneficiary amount")

        response = self.client.get(
            f"/api/v1/batches/{batch_id}/",
            HTTP_AUTHORIZATION=f"Bearer {admin_token}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["status"], "REJECTED")

    @override_settings(
        PESAWAY_ENABLED=True,
        PESAWAY_CLIENT_ID="client-id",
        PESAWAY_CLIENT_SECRET="client-secret",
        PESAWAY_RESULTS_URL="https://example.com/api/v1/providers/pesaway/results/",
    )
    def test_provider_enabled_wallet_flow_uses_outbox_dispatch(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "+254700000020",
                "password": "StrongPass123!",
                "full_name": "Provider User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]

        response = self._post(
            "/api/v1/payees/",
            {
                "label": "Family Allowance",
                "payee_type": "MOBILE",
                "phone_number": "+254733333333",
                "expense_category": "family",
            },
            token=token,
        )
        payee_id = response.json()["payee"]["id"]
        self._post(
            "/api/v1/schedules/",
            {
                "payee_id": payee_id,
                "amount_minor": 100000,
                "day_of_month": timezone.localdate().day,
            },
            token=token,
        )
        self._post("/api/v1/wallets/topups/", {"amount_minor": 200000}, token=token)

        response = self._post("/api/v1/payments/pay-all/", {"payment_mode": "WALLET"}, token=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["status"], "PROCESSING")

        batch_id = response.json()["batch"]["id"]
        self.assertTrue(
            OutboxEvent.objects.filter(topic="payment.instruction.dispatch", aggregate_type="payment_instruction").exists()
        )

        class FakePesaWayClient:
            def send_b2c_payment(self, **kwargs):
                return {"data": {"TransactionReference": "PW-12345"}, "request": kwargs}

        with patch("base.provider_executor.build_pesaway_client", return_value=FakePesaWayClient()):
            call_command("process_outbox")

        batch = PaymentBatch.objects.get(id=batch_id)
        instruction = PaymentInstruction.objects.get(batch=batch)
        self.assertEqual(batch.status, PaymentBatch.Status.SUCCEEDED)
        self.assertEqual(instruction.status, PaymentInstruction.Status.SUCCEEDED)
        self.assertEqual(instruction.provider_reference, "PW-12345")

    @override_settings(NOTIFY_URL="https://notify.example/api/send", NOTIFY_API_KEY="notify-key", NOTIFY_SYSTEM="radicrunch")
    def test_process_notifications_dispatches_sms_and_email(self):
        user = User.objects.create_user(
            phone_number="+254700000099",
            password="StrongPass123!",
            full_name="Notify User",
            email="notify@example.com",
            account_type="INDIVIDUAL",
            email_notifications_enabled=True,
            sms_notifications_enabled=True,
        )
        queue_notifications_for_user(
            user,
            "PAYMENT_SUCCESS",
            {"batch_id": "batch-123", "total_amount_minor": 450000},
        )

        sent_payloads = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"status":"queued"}'

        def fake_urlopen(req, timeout):
            sent_payloads.append(
                {
                    "url": req.full_url,
                    "headers": dict(req.header_items()),
                    "body": json.loads(req.data.decode("utf-8")),
                    "timeout": timeout,
                }
            )
            return FakeResponse()

        with patch("notifications.services.request.urlopen", side_effect=fake_urlopen):
            call_command("process_notifications")

        self.assertEqual(len(sent_payloads), 2)
        self.assertEqual(NotificationEvent.objects.filter(status=NotificationEvent.Status.SENT).count(), 2)
        self.assertEqual({payload["body"]["notification_type"] for payload in sent_payloads}, {"sms", "email"})
        self.assertEqual({payload["body"]["system"] for payload in sent_payloads}, {"radicrunch"})

    def test_superadmin_can_use_individual_and_corporate_flows(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "+254700000120",
                "password": "StrongPass123!",
                "full_name": "Super Admin",
                "email": "superadmin@example.com",
                "account_type": "SUPERADMIN",
            },
        )
        self.assertEqual(response.status_code, 201)
        token = response.json()["token"]

        response = self._post(
            "/api/v1/payees/",
            {
                "label": "Home Internet",
                "payee_type": "TILL",
                "till_number": "123456",
                "expense_category": "utilities",
            },
            token=token,
        )
        self.assertEqual(response.status_code, 201)
        payee_id = response.json()["payee"]["id"]

        response = self._post(
            "/api/v1/schedules/",
            {
                "payee_id": payee_id,
                "amount_minor": 50000,
                "day_of_month": timezone.localdate().day,
            },
            token=token,
        )
        self.assertEqual(response.status_code, 201)

        response = self._post("/api/v1/wallets/topups/", {"amount_minor": 200000}, token=token)
        self.assertEqual(response.status_code, 200)

        response = self._post("/api/v1/payments/pay-all/", {"payment_mode": "WALLET"}, token=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["status"], "SUCCEEDED")

        response = self._post(
            "/api/v1/organizations/",
            {"name": "Route Ops"},
            token=token,
        )
        self.assertEqual(response.status_code, 201)
        organization_id = response.json()["organization"]["id"]

        response = self._post(
            "/api/v1/wallets/topups/",
            {"organization_id": organization_id, "amount_minor": 300000},
            token=token,
        )
        self.assertEqual(response.status_code, 200)

        csv_content = "\n".join(
            [
                "recipient_name,recipient_type,amount_minor,category,phone_number,external_reference",
                "Vendor A,MOBILE,100000,payroll,+254711111111,EMP001",
            ]
        )
        response = self._post(
            "/api/v1/corporate/batches/upload/",
            {
                "organization_id": organization_id,
                "scheduled_for": str(timezone.localdate()),
                "payment_mode": "WALLET",
                "csv_content": csv_content,
            },
            token=token,
        )
        self.assertEqual(response.status_code, 201)
        batch_id = response.json()["batch"]["id"]

        response = self._post(f"/api/v1/corporate/batches/{batch_id}/submit/", {}, token=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["status"], "PENDING_APPROVAL")

        response = self._post(f"/api/v1/corporate/batches/{batch_id}/approve/", {}, token=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["status"], "SUCCEEDED")

        dashboard = self.client.get("/api/v1/dashboard/", HTTP_AUTHORIZATION=f"Bearer {token}").json()["dashboard"]
        self.assertEqual(dashboard["account_type"], "SUPERADMIN")
        self.assertIn("individual", dashboard)
        self.assertTrue(any(org["organization_id"] == organization_id for org in dashboard["organizations"]))

    def test_user_can_create_and_use_integration_api_key(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "+254700000130",
                "password": "StrongPass123!",
                "full_name": "API Integrator",
                "email": "api@example.com",
                "account_type": "INDIVIDUAL",
            },
        )
        self.assertEqual(response.status_code, 201)
        token = response.json()["token"]

        response = self._post(
            "/api/v1/integrations/api-keys/",
            {
                "name": "ERP Sync",
                "scopes": ["read", "write"],
            },
            token=token,
        )
        self.assertEqual(response.status_code, 201)
        secret = response.json()["secret"]
        api_key_id = response.json()["api_key"]["id"]
        self.assertTrue(secret.startswith("rtk_"))

        response = self.client.get("/api/v1/auth/me/", HTTP_X_API_KEY=secret)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["phone_number"], "+254700000130")

        response = self.client.get("/api/v1/integrations/api-keys/", HTTP_X_API_KEY=secret)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["api_keys"]), 1)
        self.assertEqual(response.json()["api_keys"][0]["id"], api_key_id)

        response = self.client.post(
            f"/api/v1/integrations/api-keys/{api_key_id}/revoke/",
            data=json.dumps({}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["api_key"]["is_active"])

        response = self.client.get("/api/v1/auth/me/", HTTP_X_API_KEY=secret)
        self.assertEqual(response.status_code, 401)
