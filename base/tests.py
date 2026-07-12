import calendar
import json
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase
from django.test.utils import override_settings
from django.utils import timezone

from audit.models import AuditLog
from eusers.models import User
from notifications.models import NotificationEvent
from notifications.services import queue_notifications_for_user
from reports.models import ReportExport
from ledger.models import Account
from ledger.services import PaymentInterface, get_or_create_user_account, initiate_payout, unique_transaction_reference

from .models import (
    OrganizationMembership,
    OutboxEvent,
    PaymentBatch,
    PaymentInstruction,
    PaymentSchedule,
)
from .services import (
    ensure_user_wallets,
    mark_wallet_entry_cleared,
    place_wallet_hold,
    post_uncleared_wallet_entry,
    release_wallet_hold,
    run_due_wallet_autopayments,
    top_up_wallet,
)


class QuickBundlPlatformTests(TestCase):
    fixtures = ["notification_templates.json"]

    def setUp(self):
        self.client = Client()
        self._idempotency_counter = 0

    def _post(self, path, payload, token=None):
        headers = {}
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
            self._idempotency_counter += 1
            headers["HTTP_IDEMPOTENCY_KEY"] = f"test-key-{self._idempotency_counter}"
        return self.client.post(path, data=json.dumps(payload), content_type="application/json", **headers)

    def _patch(self, path, payload, token=None):
        headers = {}
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
            self._idempotency_counter += 1
            headers["HTTP_IDEMPOTENCY_KEY"] = f"test-key-{self._idempotency_counter}"
        return self.client.patch(path, data=json.dumps(payload), content_type="application/json", **headers)

    def _delete(self, path, token=None):
        headers = {}
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
            self._idempotency_counter += 1
            headers["HTTP_IDEMPOTENCY_KEY"] = f"test-key-{self._idempotency_counter}"
        return self.client.delete(path, content_type="application/json", **headers)

    def test_individual_payment_flow(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000001",
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
        self.assertEqual(response.json()["batch"]["fee_amount_minor"], 6000)

    def test_login_requires_otp_and_accepts_any_six_digit_code_for_default_test_account(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254710956633",
                "password": "StrongPass123!",
                "full_name": "OTP User",
                "account_type": "INDIVIDUAL",
            },
        )
        self.assertEqual(response.status_code, 201)

        response = self._post(
            "/api/v1/auth/login/",
            {
                "phone_number": "0710956633",
                "password": "StrongPass123!",
            },
        )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["otp_required"])
        self.assertEqual(response.json()["phone_number"], "254710956633")
        self.assertEqual(response.json()["dev_otp"], "123456")

        response = self._post(
            "/api/v1/auth/login/",
            {
                "phone_number": "254710956633",
                "password": "StrongPass123!",
                "otp": "654321",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("token", response.json())
        self.assertEqual(response.json()["user"]["phone_number"], "254710956633")

    def test_individual_vault_transfer(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000002",
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
                "phone_number": "254700000005",
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

        primary_wallet = Account.objects.get(user__phone_number="254700000005", account_kind=Account.AccountKind.PRIMARY)
        vault_wallet = Account.objects.get(user__phone_number="254700000005", account_kind=Account.AccountKind.VAULT)
        self.assertEqual(primary_wallet.available_balance_minor, 0)
        self.assertEqual(vault_wallet.available_balance_minor, 120000)

    def test_wallet_hold_and_uncleared_entry_update_balance_snapshot(self):
        user = User.objects.create(
            phone_number="254700000010",
            full_name="Ledger User",
            account_type=User.AccountType.INDIVIDUAL,
        )
        wallet, _ = ensure_user_wallets(user)

        top_up_wallet(user, {"amount_minor": 500000})
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance_minor, 500000)

        hold = place_wallet_hold(wallet, 200000, reason="authorization", reference="hold-001")
        wallet.refresh_from_db()
        self.assertEqual(wallet.reserved_balance_minor, 200000)
        self.assertEqual(wallet.available_balance_minor, 300000)
        self.assertEqual(hold.direction, "PAY_OUT")

        release_wallet_hold(hold.id)
        wallet.refresh_from_db()
        self.assertEqual(wallet.reserved_balance_minor, 0)
        self.assertEqual(wallet.available_balance_minor, 500000)

        entry = post_uncleared_wallet_entry(
            wallet,
            150000,
            entry_type="TOP_UP",
            reference="uncleared-001",
            metadata={"source": "mpesa"},
        )
        wallet.refresh_from_db()
        self.assertEqual(wallet.current_balance_minor, 650000)
        self.assertEqual(wallet.uncleared_balance_minor, 150000)
        self.assertEqual(wallet.available_balance_minor, 500000)
        self.assertEqual(entry.status, "PROCESSING")

        mark_wallet_entry_cleared(entry.id)
        wallet.refresh_from_db()
        self.assertEqual(wallet.uncleared_balance_minor, 0)
        self.assertEqual(wallet.available_balance_minor, 650000)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "COMPLETED")

    def test_profile_update_and_wallet_ledger_endpoint(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000003",
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
        entry = response.json()["entries"][0]
        self.assertEqual(entry["entry_type"], "TOP_UP")
        self.assertEqual(entry["description"], "Top up of funds")
        self.assertTrue(entry["reference"].startswith("QB"))
        self.assertNotIn("topup:", entry["reference"])
        self.assertNotIn("vault-transfer:", entry["reference"])

    def test_audit_logs_include_descriptions_and_mutating_request_trail(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000099",
                "password": "StrongPass123!",
                "full_name": "Audit User",
                "account_type": "INDIVIDUAL",
            },
        )
        self.assertEqual(response.status_code, 201)
        token = response.json()["token"]

        response = self._post(
            "/api/v1/payees/",
            {
                "label": "Audit Payee",
                "payee_type": "MOBILE",
                "phone_number": "254700555555",
                "expense_category": "audit",
            },
            token=token,
        )
        self.assertEqual(response.status_code, 201)

        payee_log = AuditLog.objects.get(action="payee.created", metadata__payee_label="Audit Payee")
        self.assertEqual(payee_log.description, "Created payee Audit Payee.")

        request_log = AuditLog.objects.filter(action="http.request", metadata__path="/api/v1/payees/").latest("created_at")
        self.assertEqual(request_log.actor.phone_number, "254700000099")
        self.assertEqual(request_log.metadata["method"], "POST")
        self.assertEqual(request_log.description, "POST /api/v1/payees/ completed with HTTP 201.")

    def test_payee_and_schedule_crud_endpoints(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000004",
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
                "phone_number": "254700123123",
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

    def test_payee_presets_can_be_listed_and_used_for_autofill(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000006",
                "password": "StrongPass123!",
                "full_name": "Preset User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]

        response = self.client.get("/api/v1/payee-presets/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 200)

        presets = response.json()["presets"]
        kplc_preset = next((preset for preset in presets if preset["label"] == "KPLC"), None)
        self.assertIsNotNone(kplc_preset)
        self.assertEqual(kplc_preset["payee_type"], "PAYBILL")
        self.assertEqual(kplc_preset["paybill_number"], "888880")

        response = self._post(
            "/api/v1/payees/",
            {
                "preset_id": kplc_preset["id"],
                "account_reference": "12345678",
            },
            token=token,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["payee"]["preset_id"], kplc_preset["id"])
        self.assertEqual(response.json()["payee"]["label"], "KPLC")
        self.assertEqual(response.json()["payee"]["payee_type"], "PAYBILL")
        self.assertEqual(response.json()["payee"]["paybill_number"], "888880")
        self.assertEqual(response.json()["payee"]["account_reference"], "12345678")

    def test_quarterly_schedule_advances_after_successful_payment(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000007",
                "password": "StrongPass123!",
                "full_name": "Quarterly User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]

        payee_id = self._post(
            "/api/v1/payees/",
            {
                "label": "School Fees",
                "payee_type": "PAYBILL",
                "paybill_number": "222333",
                "account_reference": "STU-9001",
                "expense_category": "education",
            },
            token=token,
        ).json()["payee"]["id"]

        schedule_response = self._post(
            "/api/v1/schedules/",
            {
                "payee_id": payee_id,
                "amount_minor": 900000,
                "day_of_month": timezone.localdate().day,
                "interval_months": 3,
                "next_due_date": str(timezone.localdate()),
                "requires_approval": True,
            },
            token=token,
        )
        self.assertEqual(schedule_response.status_code, 201)
        schedule_id = schedule_response.json()["schedule"]["id"]
        self.assertEqual(schedule_response.json()["schedule"]["interval_months"], 3)
        self.assertTrue(schedule_response.json()["schedule"]["requires_approval"])

        self._post("/api/v1/wallets/topups/", {"amount_minor": 1000000}, token=token)
        with patch("base.services.payment_microservice_dispatch_enabled", return_value=True):
            response = self._post(
                "/api/v1/payments/pay-all/",
                {"payment_mode": "WALLET", "simulate_collection": False},
                token=token,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["status"], "SUCCEEDED")

        schedule = PaymentSchedule.objects.get(id=schedule_id)
        today = timezone.localdate()
        month_index = (today.month - 1) + 3
        expected_year = today.year + month_index // 12
        expected_month = month_index % 12 + 1
        expected_day = min(today.day, calendar.monthrange(expected_year, expected_month)[1])
        expected_due_date = today.replace(year=expected_year, month=expected_month, day=expected_day)
        self.assertEqual(schedule.next_due_date, expected_due_date)

    def test_quick_pay_endpoint_returns_design_ready_batch(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000009",
                "password": "StrongPass123!",
                "full_name": "Quick Pay User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]

        payee_id = self._post(
            "/api/v1/payees/",
            {
                "label": "KPLC",
                "payee_type": "PAYBILL",
                "paybill_number": "888880",
                "account_reference": "ACC-9009",
                "expense_category": "utilities",
            },
            token=token,
        ).json()["payee"]["id"]
        self._post("/api/v1/wallets/topups/", {"amount_minor": 100000}, token=token)

        response = self._post(
            "/api/v1/payments/quick-pay/",
            {
                "payee_id": payee_id,
                "amount_minor": 25000,
                "payment_mode": "WALLET",
            },
            token=token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["batch_kind"], "INDIVIDUAL_ADHOC")
        self.assertEqual(response.json()["batch"]["status"], "SUCCEEDED")
        self.assertEqual(response.json()["batch"]["fee_amount_minor"], 500)
        self.assertEqual(response.json()["batch"]["gross_amount_minor"], 25500)

        response = self.client.get(
            "/api/v1/reports/transactions/summary/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["total_fees_minor"], 500)
        self.assertEqual(response.json()["transactions"][0]["gross_amount_minor"], 25500)

    def test_approval_required_schedules_are_skipped_by_autopay_runner(self):
        user = User.objects.create_user(
            phone_number="254700000008",
            password="StrongPass123!",
            full_name="Approval User",
            account_type="INDIVIDUAL",
            default_payment_mode="WALLET",
        )
        payee = user.payees.create(
            label="School Fees",
            payee_type="PAYBILL",
            paybill_number="222333",
            account_reference="STU-3002",
            expense_category="education",
            active=True,
        )
        PaymentSchedule.objects.create(
            payee=payee,
            amount_minor=500000,
            day_of_month=timezone.localdate().day,
            interval_months=3,
            next_due_date=timezone.localdate(),
            requires_approval=True,
            active=True,
        )

        processed = run_due_wallet_autopayments(timezone.localdate())
        self.assertEqual(processed, 0)
        self.assertFalse(PaymentBatch.objects.filter(user=user).exists())

    def test_default_cors_settings_support_local_angular_dev_origin(self):
        response = self.client.options(
            "/api/v1/payees/",
            HTTP_ORIGIN="http://localhost:4200",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS=(
                "Authorization, Content-Type, X-API-Key, "
                "Idempotency-Key, Ngrok-Skip-Browser-Warning"
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://localhost:4200")
        self.assertEqual(response["Access-Control-Allow-Credentials"], "true")
        allowed_headers = response["Access-Control-Allow-Headers"].lower()
        self.assertIn("authorization", allowed_headers)
        self.assertIn("content-type", allowed_headers)
        self.assertIn("x-api-key", allowed_headers)
        self.assertIn("idempotency-key", allowed_headers)
        self.assertIn("ngrok-skip-browser-warning", allowed_headers)

    def test_default_cors_settings_support_ngrok_frontend_origin(self):
        response = self.client.get(
            "/api/v1/reports/exports/",
            HTTP_ORIGIN="https://current-tunnel.ngrok-free.dev",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://current-tunnel.ngrok-free.dev")
        self.assertEqual(response["Access-Control-Allow-Credentials"], "true")

    @override_settings(
        CORS_ALLOW_ALL_ORIGINS=False,
        CORS_ALLOWED_ORIGINS=["http://localhost:3000"],
        CORS_ALLOWED_ORIGIN_REGEXES=[],
        CORS_ALLOW_HEADERS=["Authorization", "Content-Type", "X-API-Key"],
        CORS_ALLOW_METHODS=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        CORS_ALLOW_CREDENTIALS=False,
        CORS_PREFLIGHT_MAX_AGE=600,
    )
    def test_cors_headers_are_added_for_allowed_origin_and_preflight(self):
        response = self.client.options(
            "/api/v1/payees/",
            HTTP_ORIGIN="http://localhost:3000",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="Authorization, Content-Type",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://localhost:3000")
        self.assertIn("POST", response["Access-Control-Allow-Methods"])
        self.assertIn("Authorization", response["Access-Control-Allow-Headers"])
        self.assertIn("Content-Type", response["Access-Control-Allow-Headers"])
        self.assertEqual(response["Access-Control-Max-Age"], "600")

        response = self.client.get("/api/v1/health/", HTTP_ORIGIN="http://localhost:3000")
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://localhost:3000")

    @override_settings(
        CORS_ALLOW_ALL_ORIGINS=False,
        CORS_ALLOWED_ORIGINS=[],
        CORS_ALLOWED_ORIGIN_REGEXES=[r"^https://[-a-zA-Z0-9]+\.ngrok-free\.dev$"],
        CORS_ALLOW_HEADERS=["Authorization", "Content-Type", "X-API-Key"],
        CORS_ALLOW_METHODS=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        CORS_ALLOW_CREDENTIALS=True,
        CORS_PREFLIGHT_MAX_AGE=600,
    )
    def test_cors_headers_are_added_for_regex_origin_and_unauthorized_response(self):
        response = self.client.get(
            "/api/v1/reports/exports/",
            HTTP_ORIGIN="https://current-tunnel.ngrok-free.dev",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://current-tunnel.ngrok-free.dev")
        self.assertEqual(response["Access-Control-Allow-Credentials"], "true")

    def test_dashboard_and_approvals_endpoints_align_with_ui_design(self):
        admin_response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000015",
                "password": "StrongPass123!",
                "full_name": "UI Admin",
                "account_type": "CORPORATE",
                "organization_name": "UI Org",
            },
        )
        admin_token = admin_response.json()["token"]
        checker_response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000016",
                "password": "StrongPass123!",
                "full_name": "UI Checker",
                "account_type": "CORPORATE",
            },
        )
        checker_token = checker_response.json()["token"]
        checker_user_id = checker_response.json()["user"]["id"]

        organization_id = self.client.get(
            "/api/v1/dashboard/",
            HTTP_AUTHORIZATION=f"Bearer {admin_token}",
        ).json()["dashboard"]["organizations"][0]["organization_id"]
        self._post(
            f"/api/v1/organizations/{organization_id}/members/",
            {"user_id": checker_user_id, "role": "CHECKER"},
            token=admin_token,
        )
        response = self._patch(
            f"/api/v1/organizations/{organization_id}/",
            {"registration_number": "PVT-2019-004821"},
            token=admin_token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["organization"]["registration_number"], "PVT-2019-004821")

        self._post(
            "/api/v1/wallets/topups/",
            {"organization_id": organization_id, "amount_minor": 500000},
            token=admin_token,
        )
        csv_content = "\n".join(
            [
                "recipient_name,recipient_type,amount_minor,category,phone_number,external_reference",
                "Vendor A,MOBILE,100000,payroll,254711111111,EMP001",
                "Vendor B,MOBILE,50000,payroll,254722222222,EMP002",
            ]
        )
        batch_id = self._post(
            "/api/v1/corporate/batches/upload/",
            {
                "organization_id": organization_id,
                "scheduled_for": str(timezone.localdate()),
                "payment_mode": "WALLET",
                "description": "Payroll Run",
                "csv_content": csv_content,
            },
            token=admin_token,
        ).json()["batch"]["id"]
        self._post(f"/api/v1/corporate/batches/{batch_id}/submit/", {}, token=admin_token)

        response = self.client.get(
            f"/api/v1/dashboard/?organization_id={organization_id}",
            HTTP_AUTHORIZATION=f"Bearer {admin_token}",
        )
        self.assertEqual(response.status_code, 200)
        selected_org = response.json()["dashboard"]["selected_organization"]
        self.assertEqual(selected_org["registration_number"], "PVT-2019-004821")
        self.assertEqual(selected_org["pending_approvals"], 1)
        self.assertEqual(selected_org["member_counts"]["checkers"], 1)

        response = self.client.get(
            f"/api/v1/approvals/?organization_id={organization_id}",
            HTTP_AUTHORIZATION=f"Bearer {checker_token}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["approvals"]), 1)
        approval = response.json()["approvals"][0]
        self.assertEqual(approval["gross_amount_minor"], 153000)
        self.assertEqual(len(approval["sample_instructions"]), 2)

    def test_report_exports_endpoint_lists_recent_exports(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000017",
                "password": "StrongPass123!",
                "full_name": "Export User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]

        self.client.get("/api/v1/reports/transactions.csv", HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.get("/api/v1/reports/exports/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["exports"]), 1)
        self.assertEqual(response.json()["exports"][0]["status"], ReportExport.Status.GENERATED)

    def test_corporate_maker_checker_flow(self):
        admin_response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000010",
                "password": "StrongPass123!",
                "full_name": "Finance Admin",
                "account_type": "CORPORATE",
                "organization_name": "Quick Bundl",
            },
        )
        admin_token = admin_response.json()["token"]
        admin_user_id = admin_response.json()["user"]["id"]
        self.assertTrue(admin_user_id)

        checker_response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000011",
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
                "Vendor A,MOBILE,100000,payroll,254711111111,EMP001",
                "Vendor B,MOBILE,50000,payroll,254722222222,EMP002",
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
        self.assertEqual(response.json()["batch"]["fee_amount_minor"], 3000)

        batch = PaymentBatch.objects.get(id=batch_id)
        wallet = Account.objects.get(organization_id=organization_id, account_kind=Account.AccountKind.PRIMARY)
        self.assertEqual(batch.status, PaymentBatch.Status.SUCCEEDED)
        self.assertEqual(wallet.available_balance_minor, 747000)
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
                "phone_number": "254700000012",
                "password": "StrongPass123!",
                "full_name": "Ops Admin",
                "account_type": "CORPORATE",
                "organization_name": "Quick Bundl Corp",
            },
        )
        admin_token = admin_response.json()["token"]

        maker_response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000013",
                "password": "StrongPass123!",
                "full_name": "Maker User",
                "account_type": "CORPORATE",
            },
        )
        maker_user_id = maker_response.json()["user"]["id"]

        checker_response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000014",
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
                "Vendor A,MOBILE,100000,payroll,254711111111,EMP001",
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

    def test_microservice_enabled_wallet_flow_uses_outbox_dispatch(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000020",
                "password": "StrongPass123!",
                "full_name": "Microservice User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]

        response = self._post(
            "/api/v1/payees/",
            {
                "label": "Family Allowance",
                "payee_type": "MOBILE",
                "phone_number": "254733333333",
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

        with patch("base.services.payment_microservice_dispatch_enabled", return_value=True):
            response = self._post(
                "/api/v1/payments/pay-all/",
                {"payment_mode": "WALLET", "simulate_collection": False},
                token=token,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["status"], "PROCESSING")

        batch_id = response.json()["batch"]["id"]
        self.assertTrue(
            OutboxEvent.objects.filter(topic="payment.instruction.dispatch", aggregate_type="payment_instruction").exists()
        )

        with patch("base.payment_microservice_executor._sandbox_enabled", return_value=True):
            call_command("process_outbox")

        batch = PaymentBatch.objects.get(id=batch_id)
        instruction = PaymentInstruction.objects.get(batch=batch)
        self.assertEqual(batch.status, PaymentBatch.Status.SUCCEEDED)
        self.assertEqual(instruction.status, PaymentInstruction.Status.SUCCEEDED)
        self.assertTrue(instruction.microservice_request_id.startswith("SIM-"))

    def test_successful_microservice_payout_queues_sms_and_email_with_sender_details(self):
        user = User.objects.create_user(
            phone_number="254700000121",
            password="StrongPass123!",
            full_name="Payout Sender",
            email="sender@example.com",
            account_type="INDIVIDUAL",
            email_notifications_enabled=True,
            sms_notifications_enabled=True,
        )
        batch = PaymentBatch.objects.create(
            batch_kind=PaymentBatch.BatchKind.INDIVIDUAL_ADHOC,
            status=PaymentBatch.Status.PROCESSING,
            payment_mode=PaymentBatch.PaymentMode.WALLET,
            user=user,
            scheduled_for=timezone.localdate(),
            total_amount_minor=150000,
        )
        instruction = PaymentInstruction.objects.create(
            batch=batch,
            recipient_name="Recipient User",
            recipient_type="MOBILE",
            destination={"phone_number": "254711222333"},
            amount_minor=150000,
            category="family",
            external_reference="EXT-001",
        )
        top_up_wallet(user, {"amount_minor": 150000, "simulate": True})
        account = get_or_create_user_account(user)
        ledger_transaction = initiate_payout(
            account,
            amount_minor=150000,
            reference=unique_transaction_reference("POT"),
            idempotency_key=f"test-payout:{instruction.id}",
            description="Test payout",
            metadata={"batch_id": str(batch.id), "instruction_id": str(instruction.id)},
        )
        batch.metadata["ledger_transaction_id"] = str(ledger_transaction.id)
        batch.save(update_fields=["metadata", "updated_at"])

        PaymentInterface(sandbox=True).initiate_instruction_payout(
            instruction,
            transaction_record=ledger_transaction,
            metadata={"batch_id": str(batch.id), "instruction_id": str(instruction.id)},
        )

        instruction.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(instruction.status, PaymentInstruction.Status.SUCCEEDED)
        self.assertEqual(batch.status, PaymentBatch.Status.SUCCEEDED)
        events = NotificationEvent.objects.filter(event_type="PAYMENT_SUCCESS").order_by("channel")
        self.assertEqual(events.count(), 2)
        self.assertEqual({event.channel for event in events}, {"EMAIL", "SMS"})
        for event in events:
            self.assertEqual(event.context["amount_minor"], 150000)
            self.assertEqual(event.context["recipient_phone_number"], "254711222333")
            self.assertEqual(event.context["sender_name"], "Payout Sender")
            self.assertEqual(event.context["sender_phone_number"], "254700000121")

    @override_settings(
        NOTIFY_URL="https://notify.example/api/send",
        NOTIFY_API_KEY="notify-key",
        NOTIFY_SYSTEM="radicrunch",
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="Quick Bundl <mvpmtech@gmail.com>",
    )
    def test_process_notifications_dispatches_sms_and_email(self):
        user = User.objects.create_user(
            phone_number="254700000099",
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

        self.assertEqual(len(sent_payloads), 1)
        self.assertEqual(NotificationEvent.objects.filter(status=NotificationEvent.Status.SENT).count(), 2)
        self.assertEqual({payload["body"]["notification_type"] for payload in sent_payloads}, {"sms"})
        self.assertEqual({payload["body"]["system"] for payload in sent_payloads}, {"radicrunch"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["notify@example.com"])
        self.assertIn("Payment completed", mail.outbox[0].subject)
        self.assertTrue(mail.outbox[0].alternatives)

    def test_superadmin_can_use_individual_and_corporate_flows(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000120",
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
            {"name": "Quick Bundl Ops"},
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
                "Vendor A,MOBILE,100000,payroll,254711111111,EMP001",
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

    def test_service_provider_can_switch_and_access_any_organization(self):
        admin_response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000140",
                "password": "StrongPass123!",
                "full_name": "Corporate Admin",
                "account_type": "CORPORATE",
                "organization_name": "Switchable Org",
            },
        )
        admin_token = admin_response.json()["token"]
        organization_id = self.client.get(
            "/api/v1/dashboard/",
            HTTP_AUTHORIZATION=f"Bearer {admin_token}",
        ).json()["dashboard"]["organizations"][0]["organization_id"]

        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000141",
                "password": "StrongPass123!",
                "full_name": "Provider Operator",
                "account_type": "SERVICE_PROVIDER",
            },
        )
        self.assertEqual(response.status_code, 201)
        token = response.json()["token"]

        dashboard = self.client.get("/api/v1/dashboard/", HTTP_AUTHORIZATION=f"Bearer {token}").json()["dashboard"]
        self.assertEqual(dashboard["account_type"], "SERVICE_PROVIDER")
        self.assertTrue(any(org["organization_id"] == organization_id for org in dashboard["organizations"]))

        response = self.client.get(
            f"/api/v1/organizations/{organization_id}/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["organization"]["role"], "SERVICE_PROVIDER")

        response = self._post(
            "/api/v1/wallets/topups/",
            {"organization_id": organization_id, "amount_minor": 125000},
            token=token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["wallet"]["organization_id"], organization_id)

    def test_user_can_create_and_use_integration_api_key(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000130",
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
        self.assertEqual(response.json()["user"]["phone_number"], "254700000130")

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
