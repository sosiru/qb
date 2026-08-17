import calendar
import json
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase
from django.test.utils import override_settings
from django.utils import timezone

from audit.models import AuditLog
from eusers.models import AccessToken, User
from notifications.models import NotificationEvent
from notifications.services import (
    NotificationDispatchError,
    NotificationInterface,
    build_email_message,
    build_notification_payload,
    create_all_reminder_notifications,
    queue_notifications_for_user,
    serialize_in_app_notification,
    validate_notification_configuration,
)
from reports.models import ReportExport
from ledger.models import Account, PaymentRequest
from ledger.services import PaymentService, get_or_create_user_account, initiate_payout, unique_transaction_reference

from .models import (
    ExpenseCategory,
    OrganizationMembership,
    OutboxEvent,
    Payee,
    PayeePreset,
    PaymentBatch,
    PaymentInstruction,
    PaymentSchedule,
)
from .services import (
    calculate_payout_fee_amount_minor,
    ensure_user_wallets,
    mark_wallet_entry_cleared,
    place_wallet_hold,
    post_uncleared_wallet_entry,
    release_wallet_hold,
    run_due_wallet_autopayments,
    should_simulate_wallet_topup,
    top_up_wallet,
)


@override_settings(PAYMENT_MICROSERVICE_URL="", PAYMENT_MICROSERVICE_SANDBOX=True)
class QuickBillsPlatformTests(TestCase):
    fixtures = ["notification_templates.json"]

    def setUp(self):
        self.client = Client()
        self._idempotency_counter = 0
        PayeePreset.objects.get_or_create(
            label="KPLC",
            defaults={
                "payee_type": Payee.PayeeType.PAYBILL,
                "paybill_number": "888880",
                "expense_category": "utilities",
                "active": True,
            },
        )

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

    @override_settings(PAYMENT_MICROSERVICE_URL="https://payments.example")
    def test_wallet_topup_simulation_respects_settings_and_request_override(self):
        self.assertTrue(should_simulate_wallet_topup())
        self.assertFalse(should_simulate_wallet_topup({"simulate_collection": False}))
        self.assertTrue(should_simulate_wallet_topup({"simulate_collection": True}))

        with self.settings(PAYMENT_MICROSERVICE_SANDBOX=False):
            self.assertFalse(should_simulate_wallet_topup())

    def test_payment_categories_are_predefined_and_searchable(self):
        user = User.objects.create_user(
            phone_number="254700000601",
            password="StrongPass123!",
            full_name="Category User",
            account_type="INDIVIDUAL",
        )
        _, token = AccessToken.issue(user)

        response = self.client.get("/api/v1/payee-categories/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 200)
        categories = response.json()["categories"]
        slugs = {category["slug"] for category in categories}
        self.assertGreaterEqual(len(slugs), 15)
        self.assertIn("electricity", slugs)
        self.assertIn("professional_business_services", slugs)

        response = self.client.get("/api/v1/payee-categories/?q=internet", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["categories"][0]["slug"], "internet")

    def test_access_tokens_are_jwt_sessions_with_sliding_idle_expiry(self):
        user = User.objects.create_user(
            phone_number="254700000603",
            password="StrongPass123!",
            full_name="Session User",
            account_type="INDIVIDUAL",
        )
        token_record, token = AccessToken.issue(user)
        self.assertEqual(len(token.split(".")), 3)
        first_expiry = token_record.expires_at

        response = self.client.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 200)
        token_record.refresh_from_db()
        self.assertGreater(token_record.expires_at, first_expiry)

        token_record.expires_at = timezone.now() - timedelta(seconds=1)
        token_record.save(update_fields=["expires_at", "updated_at"])
        response = self.client.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 401)

    def test_payee_category_must_be_predefined_slug(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000602",
                "password": "StrongPass123!",
                "full_name": "Taxonomy User",
                "account_type": "INDIVIDUAL",
            },
        )
        self.assertEqual(response.status_code, 201)
        token = response.json()["token"]

        response = self._post(
            "/api/v1/payees/",
            {
                "label": "Kenya Power",
                "payee_type": "PAYBILL",
                "paybill_number": "888880",
                "account_reference": "ACC123",
                "expense_category": "electricity",
            },
            token=token,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["payee"]["expense_category"], "electricity")
        self.assertEqual(response.json()["payee"]["expense_category_label"], "Electricity")

        response = self._post(
            "/api/v1/payees/",
            {
                "label": "Loose Category",
                "payee_type": "MOBILE",
                "phone_number": "254700111222",
                "expense_category": "My Monthly Bills",
            },
            token=token,
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ExpenseCategory.objects.filter(name="My Monthly Bills").exists())

    def test_payee_creation_accepts_mobile_destination_payload(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000603",
                "password": "StrongPass123!",
                "full_name": "Destination User",
                "account_type": "INDIVIDUAL",
            },
        )
        self.assertEqual(response.status_code, 201)
        token = response.json()["token"]

        response = self._post(
            "/api/v1/payees/",
            {
                "label": "Quick bill",
                "type": "PAYBILL",
                "destination": "888880",
                "reference": "ACC123",
                "expense_category": "utilities",
            },
            token=token,
        )
        self.assertEqual(response.status_code, 201)
        payee = response.json()["payee"]
        self.assertEqual(payee["payee_type"], "PAYBILL")
        self.assertEqual(payee["paybill_number"], "888880")
        self.assertEqual(payee["account_reference"], "ACC123")

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

    @override_settings(DEBUG=True)
    def test_login_requires_otp_and_validates_generated_code_for_all_accounts(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000633",
                "password": "StrongPass123!",
                "full_name": "OTP User",
                "email": "otp-user@example.com",
                "account_type": "INDIVIDUAL",
            },
        )
        self.assertEqual(response.status_code, 201)

        response = self._post(
            "/api/v1/auth/login/",
            {
                "phone_number": "0700000633",
                "password": "StrongPass123!",
            },
        )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["otp_required"])
        self.assertEqual(response.json()["phone_number"], "254700000633")
        dev_otp = response.json()["dev_otp"]
        self.assertRegex(dev_otp, r"^\d{5}$")
        otp_events = NotificationEvent.objects.filter(event_type="LOGIN_OTP")
        self.assertEqual({event.channel for event in otp_events}, {"EMAIL", "SMS"})
        self.assertIn(dev_otp, otp_events.get(channel="SMS").context["message"])
        self.assertIn(dev_otp, otp_events.get(channel="EMAIL").context["message"])

        response = self._post(
            "/api/v1/auth/login/",
            {
                "phone_number": "254700000633",
                "password": "StrongPass123!",
                "otp": "654321",
            },
        )
        self.assertEqual(response.status_code, 400)

        response = self._post(
            "/api/v1/auth/login/",
            {
                "phone_number": "254700000633",
                "password": "StrongPass123!",
                "otp": dev_otp,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("token", response.json())
        self.assertEqual(response.json()["user"]["phone_number"], "254700000633")

    @override_settings(DEBUG=True)
    def test_login_otp_queues_sms_and_email_even_when_preferences_are_disabled(self):
        user = User.objects.create_user(
            phone_number="254700000198",
            password="StrongPass123!",
            full_name="OTP Required",
            email="otp-required@example.com",
            account_type="INDIVIDUAL",
            sms_notifications_enabled=False,
            email_notifications_enabled=False,
        )

        response = self._post(
            "/api/v1/auth/login/",
            {
                "phone_number": user.phone_number,
                "password": "StrongPass123!",
            },
        )

        self.assertEqual(response.status_code, 202)
        events = NotificationEvent.objects.filter(event_type="LOGIN_OTP")
        self.assertEqual({event.channel for event in events}, {"EMAIL", "SMS"})

    @override_settings(DEBUG=True)
    def test_password_reset_uses_otp_and_revokes_existing_sessions(self):
        user = User.objects.create_user(
            phone_number="254700000199",
            password="OldPass123!",
            full_name="Reset User",
            email="reset@example.com",
            account_type="INDIVIDUAL",
            sms_notifications_enabled=False,
            email_notifications_enabled=False,
        )
        token_record, token = AccessToken.issue(user)

        response = self._post("/api/v1/auth/password-reset/request/", {"phone_number": "0700000199"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["phone_number"], "254700000199")
        dev_otp = response.json()["dev_otp"]
        events = NotificationEvent.objects.filter(event_type="PASSWORD_RESET")
        self.assertEqual({event.channel for event in events}, {"EMAIL", "SMS"})
        self.assertIn(dev_otp, events.get(channel="SMS").context["message"])

        response = self._post(
            "/api/v1/auth/password-reset/confirm/",
            {
                "phone_number": "254700000199",
                "otp": dev_otp,
                "new_password": "NewPass123!",
            },
        )
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password("NewPass123!"))
        token_record.refresh_from_db()
        self.assertIsNotNone(token_record.revoked_at)
        response = self.client.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 401)

    def test_registration_notification_queues_sms_and_email_even_when_preferences_are_disabled(self):
        user = User.objects.create_user(
            phone_number="254700000197",
            password="StrongPass123!",
            full_name="Registered User",
            email="registered-user@example.com",
            account_type="INDIVIDUAL",
            sms_notifications_enabled=False,
            email_notifications_enabled=False,
        )

        events = queue_notifications_for_user(
            user,
            "SELF_ONBOARDING",
            {
                "user_name": user.full_name,
                "phone_number": user.phone_number,
                "account_type": user.account_type,
            },
        )

        self.assertEqual({event.channel for event in events}, {"EMAIL", "SMS"})

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

        response = self._post("/api/v1/wallets/topups/", {"amount_minor": 150000, "simulate": True}, token=token)
        self.assertEqual(response.status_code, 200)

        response = self.client.get("/api/v1/wallets/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 200)
        wallets = response.json()["wallets"]
        self.assertTrue(wallets[0]["account_number"])

        response = self.client.get("/api/v1/wallets/ledger/?limit=1&offset=0", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["entries"]), 1)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["limit"], 1)
        self.assertEqual(response.json()["offset"], 0)
        self.assertFalse(response.json()["has_next"])
        entry = response.json()["entries"][0]
        self.assertEqual(entry["entry_type"], "TOP_UP")
        self.assertEqual(entry["description"], "STK push wallet top-up")
        self.assertTrue(entry["reference"].startswith("STK"))
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
        response = self._post(
            "/api/v1/payments/pay-all/",
            {"payment_mode": "WALLET", "simulate_collection": True},
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
        self._post("/api/v1/wallets/topups/", {"amount_minor": 100000, "simulate": True}, token=token)

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

    def test_quick_pay_accepts_multiple_selected_payees(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000077",
                "password": "StrongPass123!",
                "full_name": "Multi Pay User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]
        payee_one_id = self._post(
            "/api/v1/payees/",
            {
                "label": "Water Utility",
                "payee_type": "PAYBILL",
                "paybill_number": "777001",
                "account_reference": "WATER-1",
                "expense_category": "utilities",
            },
            token=token,
        ).json()["payee"]["id"]
        payee_two_id = self._post(
            "/api/v1/payees/",
            {
                "label": "Internet Provider",
                "payee_type": "PAYBILL",
                "paybill_number": "777002",
                "account_reference": "NET-1",
                "expense_category": "internet",
            },
            token=token,
        ).json()["payee"]["id"]
        self._post("/api/v1/wallets/topups/", {"amount_minor": 200000, "simulate": True}, token=token)

        response = self._post(
            "/api/v1/payments/quick-pay/",
            {
                "recipients": [
                    {"payee_id": payee_one_id, "amount_minor": 25000},
                    {"payee_id": payee_two_id, "amount_minor": 30000},
                ],
                "payment_mode": "WALLET",
            },
            token=token,
        )

        self.assertEqual(response.status_code, 200)
        batch = PaymentBatch.objects.get(id=response.json()["batch"]["id"])
        self.assertEqual(batch.instructions.count(), 2)
        self.assertEqual(batch.description, "Quick pay to 2 recipients")
        self.assertEqual(response.json()["batch"]["status"], "SUCCEEDED")
        self.assertEqual(response.json()["batch"]["total_amount_minor"], 55000)
        self.assertEqual(response.json()["batch"]["fee_amount_minor"], 1100)
        self.assertEqual(response.json()["batch"]["gross_amount_minor"], 56100)
        self.assertEqual(response.json()["batch"]["bill_total_minor"], 55000)
        self.assertEqual(response.json()["batch"]["total_charged_minor"], 56100)

        detail_response = self.client.get(
            f"/api/v1/batches/{batch.id}/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(detail_response.status_code, 200)
        detail = detail_response.json()
        self.assertEqual(detail["batch"]["lifecycle_status"], "COMPLETED")
        self.assertEqual(detail["batch"]["disbursement_status"], "SUCCEEDED")
        self.assertEqual(sum(item["amount_minor"] for item in detail["instructions"]), 55000)
        self.assertEqual(sum(item["fee_amount_minor"] for item in detail["instructions"]), 1100)

    def test_quick_pay_accepts_recipient_amount_alias(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000079",
                "password": "StrongPass123!",
                "full_name": "Amount Alias User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]
        payee_id = self._post(
            "/api/v1/payees/",
            {
                "label": "Alias Bill",
                "payee_type": "PAYBILL",
                "paybill_number": "777005",
                "account_reference": "ALIAS-1",
                "expense_category": "utilities",
            },
            token=token,
        ).json()["payee"]["id"]
        self._post("/api/v1/wallets/topups/", {"amount_minor": 100, "simulate": True}, token=token)

        response = self._post(
            "/api/v1/payments/quick-pay/",
            {
                "recipients": [{"payee_id": payee_id, "amount": 20}],
                "payment_mode": "WALLET",
            },
            token=token,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["status"], "SUCCEEDED")
        self.assertEqual(response.json()["batch"]["total_amount_minor"], 20)

    def test_quick_pay_requires_explicit_amounts_for_multiple_bills(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000078",
                "password": "StrongPass123!",
                "full_name": "Explicit Allocation User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]
        payee_one_id = self._post(
            "/api/v1/payees/",
            {
                "label": "Bill One",
                "payee_type": "PAYBILL",
                "paybill_number": "777003",
                "account_reference": "BILL-1",
                "expense_category": "utilities",
            },
            token=token,
        ).json()["payee"]["id"]
        payee_two_id = self._post(
            "/api/v1/payees/",
            {
                "label": "Bill Two",
                "payee_type": "PAYBILL",
                "paybill_number": "777004",
                "account_reference": "BILL-2",
                "expense_category": "utilities",
            },
            token=token,
        ).json()["payee"]["id"]

        response = self._post(
            "/api/v1/payments/quick-pay/",
            {
                "payee_ids": [payee_one_id, payee_two_id],
                "amount_minor": 25000,
                "payment_mode": "WALLET",
            },
            token=token,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("amount_minor", response.json()["error"])

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

    def test_default_cors_settings_support_production_frontend_origin(self):
        response = self.client.options(
            "/api/v1/auth/login/",
            HTTP_ORIGIN="https://qb-ui.lipasync.com",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="Content-Type, Idempotency-Key, X-Idempotency-Key",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://qb-ui.lipasync.com")
        self.assertEqual(response["Access-Control-Allow-Credentials"], "true")
        allowed_headers = response["Access-Control-Allow-Headers"].lower()
        self.assertIn("content-type", allowed_headers)
        self.assertIn("idempotency-key", allowed_headers)
        self.assertIn("x-idempotency-key", allowed_headers)

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
                "organization_name": "QuickBills",
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
                "organization_name": "QuickBills Corp",
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

    @override_settings(PAYMENT_MICROSERVICE_INLINE_DISPATCH=False)
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

    @override_settings(PAYMENT_MICROSERVICE_INLINE_DISPATCH=True)
    def test_microservice_enabled_wallet_flow_dispatches_inline_when_enabled(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000021",
                "password": "StrongPass123!",
                "full_name": "Inline Microservice User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]
        payee_id = self._post(
            "/api/v1/payees/",
            {
                "label": "Inline Allowance",
                "payee_type": "MOBILE",
                "phone_number": "254733333334",
                "expense_category": "family",
            },
            token=token,
        ).json()["payee"]["id"]
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
        batch = PaymentBatch.objects.get(id=response.json()["batch"]["id"])
        instruction = PaymentInstruction.objects.get(batch=batch)
        event = OutboxEvent.objects.get(topic="payment.instruction.dispatch", aggregate_id=instruction.id)
        self.assertEqual(batch.status, PaymentBatch.Status.SUCCEEDED)
        self.assertEqual(instruction.status, PaymentInstruction.Status.SUCCEEDED)
        self.assertEqual(event.status, OutboxEvent.Status.DONE)
        self.assertTrue(instruction.microservice_request_id.startswith("SIM-"))

    @override_settings(
        PAYMENT_MICROSERVICE_URL="https://payments.lipasync.com/api/v1/core",
        PAYMENT_MICROSERVICE_SANDBOX=False,
        PESAWAY_SYSTEM_SLUG="qb",
        PESAWAY_B2C_EVENT_SLUG="b2c",
    )
    def test_live_wallet_payout_batch_stays_pending_and_debits_only_after_success_callback(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000022",
                "password": "StrongPass123!",
                "full_name": "Live Payout User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]
        payee_id = self._post(
            "/api/v1/payees/",
            {
                "label": "Live Allowance",
                "payee_type": "MOBILE",
                "phone_number": "254733333335",
                "expense_category": "family",
            },
            token=token,
        ).json()["payee"]["id"]
        self._post(
            "/api/v1/schedules/",
            {
                "payee_id": payee_id,
                "amount_minor": 100000,
                "day_of_month": timezone.localdate().day,
            },
            token=token,
        )
        self._post("/api/v1/wallets/topups/", {"amount_minor": 200000, "simulate": True}, token=token)

        with patch.object(
            PaymentService,
            "_post",
            return_value={
                "success": True,
                "message": "Outbound transfer queued",
                "data": {"outbound_transfer_id": "OUT-BATCH-PENDING-001", "status": "QUEUED"},
            },
        ):
            response = self._post(
                "/api/v1/payments/pay-all/",
                {"payment_mode": "WALLET", "simulate_collection": False},
                token=token,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch"]["status"], "PROCESSING")
        self.assertEqual(response.json()["batch"]["lifecycle_status"], "DISBURSEMENT_PROCESSING")
        batch = PaymentBatch.objects.get(id=response.json()["batch"]["id"])
        instruction = PaymentInstruction.objects.get(batch=batch)
        request = PaymentRequest.objects.get(request_id="OUT-BATCH-PENDING-001")
        wallet = get_or_create_user_account(batch.user)
        wallet.refresh_from_db()
        self.assertEqual(instruction.status, PaymentInstruction.Status.PENDING)
        self.assertEqual(request.status, PaymentRequest.Status.PROCESSING)
        self.assertEqual(wallet.available_balance_minor, 200000)
        self.assertEqual(wallet.current_balance_minor, 200000)
        self.assertEqual(wallet.reserved_balance_minor, 0)

        PaymentService().handle_webhook(
            {
                "event": "outbound_transfer.success",
                "outbound_transfer_id": "OUT-BATCH-PENDING-001",
                "status": "SUCCESS",
                "external_reference": request.originator_ref,
                "provider_transaction_id": "PHY-BATCH-001",
            }
        )

        batch.refresh_from_db()
        instruction.refresh_from_db()
        request.refresh_from_db()
        wallet.refresh_from_db()
        self.assertEqual(instruction.status, PaymentInstruction.Status.SUCCEEDED)
        self.assertEqual(batch.status, PaymentBatch.Status.SUCCEEDED)
        self.assertEqual(request.status, PaymentRequest.Status.COMPLETED)
        self.assertEqual(wallet.available_balance_minor, 98000)
        self.assertEqual(wallet.current_balance_minor, 98000)
        self.assertEqual(wallet.reserved_balance_minor, 0)

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

        PaymentService(sandbox=True).initiate_instruction_payout(
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

    def test_payout_fee_uses_decimal_rounding_and_wallet_debits_fee_on_top(self):
        self.assertEqual(calculate_payout_fee_amount_minor(150000), 3000)
        self.assertEqual(calculate_payout_fee_amount_minor(101), 2)

        user = User.objects.create_user(
            phone_number="254700000122",
            password="StrongPass123!",
            full_name="Fee Sender",
            account_type="INDIVIDUAL",
        )
        payee = user.payees.create(
            label="Fee Recipient",
            payee_type="MOBILE",
            phone_number="254711222334",
            expense_category="family",
        )
        top_up_wallet(user, {"amount_minor": 153000, "simulate": True})
        batch = PaymentBatch.objects.create(
            batch_kind=PaymentBatch.BatchKind.INDIVIDUAL_ADHOC,
            status=PaymentBatch.Status.PROCESSING,
            payment_mode=PaymentBatch.PaymentMode.WALLET,
            user=user,
            scheduled_for=timezone.localdate(),
        )
        PaymentInstruction.objects.create(
            batch=batch,
            payee=payee,
            recipient_name=payee.label,
            recipient_type=payee.payee_type,
            destination={"phone_number": payee.phone_number},
            amount_minor=150000,
            fee_amount_minor=calculate_payout_fee_amount_minor(150000),
            category="family",
        )
        batch.recalculate_totals()
        from base.services import _recalculate_batch_fee, settle_batch

        _recalculate_batch_fee(batch)
        settled = settle_batch(batch, actor=user, simulate_collection=True)
        wallet = get_or_create_user_account(user)
        wallet.refresh_from_db()
        self.assertEqual(settled.total_amount_minor, 150000)
        self.assertEqual(settled.fee_amount_minor, 3000)
        self.assertEqual(wallet.available_balance_minor, 0)

    def test_kplc_successful_payout_queues_post_payout_sms_and_email_once(self):
        user = User.objects.create_user(
            phone_number="254700000123",
            password="StrongPass123!",
            full_name="KPLC Sender",
            email="kplc-sender@example.com",
            account_type="INDIVIDUAL",
            email_notifications_enabled=True,
            sms_notifications_enabled=True,
        )
        payee = user.payees.create(
            label="KPLC Prepaid",
            payee_type=Payee.PayeeType.PAYBILL,
            paybill_number="888880",
            account_number="37123456789",
            account_reference="37123456789",
            expense_category="utilities",
        )
        batch = PaymentBatch.objects.create(
            batch_kind=PaymentBatch.BatchKind.INDIVIDUAL_ADHOC,
            status=PaymentBatch.Status.PROCESSING,
            payment_mode=PaymentBatch.PaymentMode.WALLET,
            user=user,
            scheduled_for=timezone.localdate(),
            total_amount_minor=150000,
            fee_amount_minor=3000,
        )
        instruction = PaymentInstruction.objects.create(
            batch=batch,
            payee=payee,
            recipient_name=payee.label,
            recipient_type=payee.payee_type,
            destination={"paybill_number": "888880", "account_number": "37123456789"},
            amount_minor=150000,
            fee_amount_minor=3000,
            category="utilities",
            external_reference="37123456789",
        )
        top_up_wallet(user, {"amount_minor": 153000, "simulate": True})
        account = get_or_create_user_account(user)
        ledger_transaction = initiate_payout(
            account,
            amount_minor=153000,
            reference=unique_transaction_reference("POT"),
            idempotency_key=f"test-kplc-payout:{instruction.id}",
            description="KPLC payout",
            metadata={"batch_id": str(batch.id), "instruction_id": str(instruction.id), "fee_amount_minor": 3000},
        )
        batch.metadata["ledger_transaction_id"] = str(ledger_transaction.id)
        batch.save(update_fields=["metadata", "updated_at"])

        PaymentService(sandbox=True).initiate_instruction_payout(
            instruction,
            transaction_record=ledger_transaction,
            metadata={"batch_id": str(batch.id), "instruction_id": str(instruction.id)},
        )

        self.assertEqual(
            OutboxEvent.objects.filter(topic="payment.instruction.kplc_notification", aggregate_id=instruction.id).count(),
            1,
        )
        payout_request = PaymentRequest.objects.get(operation=PaymentRequest.Operation.PAYOUT, request_payload__instruction_id=str(instruction.id))
        self.assertEqual(payout_request.request_payload["amount_minor"], 150000)
        self.assertEqual(payout_request.request_payload["amount"], 1500.0)
        meter_payload = {"data": {"colPrepayment": [{"trnTimestamp": 1710000000000, "tokenNo": "12345678901234567890", "concepts": [{"codConcept": "RESSTEP0", "amount": 1200.0}], "trnAmount": 1500.0, "trnUnits": 12.5, "msno": "37123456789"}]}}
        with patch("base.providers.service_callbacks.KPLCInterface.get_meter_data", return_value=meter_payload) as get_meter_data:
            call_command("process_outbox")
            call_command("process_outbox")

        get_meter_data.assert_called_once_with("37123456789")
        instruction.refresh_from_db()
        self.assertEqual(instruction.microservice_response["kplc_notification"]["status"], "NOTIFICATION_QUEUED")
        events = NotificationEvent.objects.filter(context__instruction_id=str(instruction.id), event_type="PAYMENT_SUCCESS")
        self.assertEqual(events.count(), 3)
        self.assertEqual({event.channel for event in events}, {"EMAIL", "SMS", "IN_APP"})
        self.assertTrue(all("Token:" in event.context["kplc_message"] for event in events))
        in_app = events.get(channel="IN_APP")
        self.assertEqual(in_app.status, NotificationEvent.Status.SENT)
        notification = serialize_in_app_notification(in_app)
        self.assertEqual(notification["title"], "KPLC payment completed")
        self.assertIn("KPLC payment was completed", notification["body"])

    def test_non_kplc_successful_payout_does_not_queue_kplc_lookup(self):
        user = User.objects.create_user(
            phone_number="254700000124",
            password="StrongPass123!",
            full_name="Non KPLC Sender",
            account_type="INDIVIDUAL",
        )
        payee = user.payees.create(
            label="Water",
            payee_type=Payee.PayeeType.PAYBILL,
            paybill_number="123456",
            account_reference="WATER-1",
            expense_category="utilities",
        )
        batch = PaymentBatch.objects.create(
            batch_kind=PaymentBatch.BatchKind.INDIVIDUAL_ADHOC,
            status=PaymentBatch.Status.PROCESSING,
            payment_mode=PaymentBatch.PaymentMode.WALLET,
            user=user,
            scheduled_for=timezone.localdate(),
            total_amount_minor=50000,
        )
        instruction = PaymentInstruction.objects.create(
            batch=batch,
            payee=payee,
            recipient_name=payee.label,
            recipient_type=payee.payee_type,
            destination={"paybill_number": "123456"},
            amount_minor=50000,
            category="utilities",
        )
        top_up_wallet(user, {"amount_minor": 50000, "simulate": True})
        account = get_or_create_user_account(user)
        ledger_transaction = initiate_payout(
            account,
            amount_minor=50000,
            reference=unique_transaction_reference("POT"),
            idempotency_key=f"test-non-kplc-payout:{instruction.id}",
            description="Non KPLC payout",
            metadata={"batch_id": str(batch.id), "instruction_id": str(instruction.id)},
        )

        PaymentService(sandbox=True).initiate_instruction_payout(
            instruction,
            transaction_record=ledger_transaction,
            metadata={"batch_id": str(batch.id), "instruction_id": str(instruction.id)},
        )

        self.assertFalse(OutboxEvent.objects.filter(topic="payment.instruction.kplc_notification", aggregate_id=instruction.id).exists())

    def test_stk_collection_uses_amount_plus_fee_before_instruction_payout(self):
        response = self._post(
            "/api/v1/auth/register/",
            {
                "phone_number": "254700000125",
                "password": "StrongPass123!",
                "full_name": "STK Fee User",
                "account_type": "INDIVIDUAL",
            },
        )
        token = response.json()["token"]
        payee_id = self._post(
            "/api/v1/payees/",
            {
                "label": "STK KPLC",
                "payee_type": "PAYBILL",
                "paybill_number": "888880",
                "account_number": "37123456780",
                "account_reference": "37123456780",
                "expense_category": "utilities",
            },
            token=token,
        ).json()["payee"]["id"]

        response = self._post(
            "/api/v1/payments/quick-pay/",
            {
                "payee_id": payee_id,
                "amount_minor": 150000,
                "payment_mode": "STK",
                "simulate_collection": False,
            },
            token=token,
        )

        self.assertEqual(response.status_code, 200)
        batch_id = response.json()["batch"]["id"]
        self.assertEqual(response.json()["batch"]["total_amount_minor"], 150000)
        self.assertEqual(response.json()["batch"]["fee_amount_minor"], 3000)
        self.assertEqual(response.json()["batch"]["gross_amount_minor"], 153000)

        with patch("base.payment_microservice_executor._sandbox_enabled", return_value=True):
            call_command("process_outbox")

        collection_request = PaymentRequest.objects.get(operation=PaymentRequest.Operation.STK_PUSH, request_payload__metadata__batch_id=batch_id)
        self.assertEqual(collection_request.request_payload["amount_minor"], 153000)
        self.assertEqual(collection_request.request_payload["amount"], 1530.0)

    @override_settings(
        NOTIFY_URL="https://notify.example/api/send",
        NOTIFY_API_KEY="notify-key",
        NOTIFY_SYSTEM="radicrunch",
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="QuickBills <mvpmtech@gmail.com>",
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
            status_code = 200
            text = '{"status":"queued"}'

            def raise_for_status(self):
                return None

        def fake_post(url, headers=None, json=None, timeout=None):
            sent_payloads.append(
                {
                    "url": url,
                    "headers": headers,
                    "body": json,
                    "timeout": timeout,
                }
            )
            return FakeResponse()

        with patch("notifications.services.requests.post", side_effect=fake_post):
            call_command("process_notifications")

        self.assertEqual(len(sent_payloads), 2)
        self.assertEqual(NotificationEvent.objects.filter(status=NotificationEvent.Status.SENT).count(), 2)
        self.assertEqual({payload["body"]["notification_type"] for payload in sent_payloads}, {"sms", "email"})
        self.assertEqual({payload["body"]["template"] for payload in sent_payloads}, {"sms_default", "email_default"})
        self.assertTrue(all(set(payload["body"]) == {
            "notification_type", "template", "unique_identifier", "recipients", "context"
        } for payload in sent_payloads))
        sms_payload = next(payload["body"] for payload in sent_payloads if payload["body"]["notification_type"] == "sms")
        email_payload = next(payload["body"] for payload in sent_payloads if payload["body"]["notification_type"] == "email")
        self.assertEqual(set(sms_payload["context"]), {"message"})
        self.assertEqual(set(email_payload["context"]), {"message", "subject"})
        self.assertTrue(email_payload["context"]["subject"])
        self.assertTrue(all(payload["headers"].get("X-API-KEY") == "notify-key" for payload in sent_payloads))
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(NOTIFY_URL="", NOTIFY_API_KEY="")
    def test_notification_config_validation_requires_provider_key(self):
        with self.assertRaises(NotificationDispatchError) as context:
            validate_notification_configuration()

        message = str(context.exception)
        self.assertIn("NOTIFY must be configured", message)
        self.assertIn("NOTIFY_API_KEY must be configured", message)

    @override_settings(
        NOTIFY_URL="https://notify.example/api/send",
        NOTIFY_API_KEY="notify-key",
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        EMAIL_HOST_PASSWORD="",
    )
    def test_notification_config_validation_can_require_email_backup_password(self):
        with self.assertRaises(NotificationDispatchError) as context:
            validate_notification_configuration(require_email_backup=True)

        self.assertIn("EMAIL_HOST_PASSWORD must be configured", str(context.exception))

    def test_login_success_notification_is_email_only(self):
        user = User.objects.create_user(
            phone_number="254700000098",
            password="StrongPass123!",
            full_name="Login User",
            email="login@example.com",
            account_type="INDIVIDUAL",
            email_notifications_enabled=True,
            sms_notifications_enabled=True,
        )

        events = queue_notifications_for_user(user, "LOGIN_SUCCESS", {"login_time": "Now"})

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].channel, "EMAIL")
        self.assertFalse(NotificationEvent.objects.filter(event_type="LOGIN_SUCCESS", channel="SMS").exists())
        subject, text_body, _ = build_email_message(events[0])
        self.assertIn("New sign-in to your QuickBills account", subject)
        self.assertIn("If this was not you, change your password immediately.", text_body)

    def test_notifications_use_customer_friendly_language(self):
        user = User.objects.create_user(
            phone_number="254700000097",
            password="StrongPass123!",
            full_name="Payment Customer",
            email="customer@example.com",
            account_type="CORPORATE",
            email_notifications_enabled=True,
            sms_notifications_enabled=True,
        )
        cases = {
            "SELF_ONBOARDING": {
                "context": {"phone_number": user.phone_number, "account_type": "CORPORATE"},
                "expected": "your account is ready",
            },
            "LOGIN_OTP": {
                "context": {"otp": "12345", "expires_in": "10 minutes"},
                "expected": "enter this code to sign in",
            },
            "ORGANIZATION_INVITE": {
                "context": {
                    "organization_name": "Example Company",
                    "invited_by": "Alex",
                    "role": "CHECKER",
                },
                "expected": "payment approver",
            },
            "WALLET_TOPUP_REQUESTED": {
                "context": {"amount_minor": 100000},
                "expected": "request to add kes 1,000.00",
            },
            "WALLET_TOPUP_COMPLETED": {
                "context": {"amount_minor": 100000, "wallet_balance_minor": 250000},
                "expected": "has been added to your wallet",
            },
            "WALLET_WITHDRAWAL_REQUESTED": {
                "context": {"amount_minor": 50000},
                "expected": "request to withdraw kes 500.00",
            },
            "WALLET_LOW": {
                "context": {
                    "wallet_balance_minor": 100000,
                    "total_amount_minor": 150000,
                    "shortfall_minor": 50000,
                    "schedule_count": 2,
                },
                "expected": "wallet is short by kes 500.00",
            },
            "OVERDUE_PAYMENT": {
                "context": {"schedule_count": 2, "total_amount_minor": 150000},
                "expected": "you have 2 payments overdue",
            },
            "T_MINUS_3": {
                "context": {"schedule_count": 2, "total_amount_minor": 150000},
                "expected": "you have 2 payments totalling kes 1,500.00 due in 3 days",
            },
            "DUE_TODAY": {
                "context": {"schedule_count": 1, "total_amount_minor": 75000},
                "expected": "you have 1 payment totalling kes 750.00 due today",
            },
            "PAYMENT_SUCCESS": {
                "context": {"batch_id": "PAY-123", "total_amount_minor": 450000},
                "expected": "successfully sent your payment",
            },
            "PAYMENT_FAILURE": {
                "context": {"batch_id": "PAY-124", "status": "FAILED"},
                "expected": "could not send one or more of your payments",
            },
            "APPROVAL_REQUEST": {
                "context": {"batch_id": "PAY-125", "total_amount_minor": 300000},
                "expected": "ready for your review",
            },
            "BATCH_APPROVED": {
                "context": {"batch_id": "PAY-126"},
                "expected": "approved and are now being sent",
            },
            "BATCH_REJECTED": {
                "context": {"batch_id": "PAY-127", "reason": "The amount needs to be corrected."},
                "expected": "were not approved",
            },
        }

        for event_type, case in cases.items():
            with self.subTest(event_type=event_type):
                events = queue_notifications_for_user(user, event_type, case["context"])
                sms_event = next(event for event in events if event.channel == "SMS")
                email_event = next(event for event in events if event.channel == "EMAIL")
                sms_message = build_notification_payload(sms_event)["context"]["message"]
                subject, text_body, _ = build_email_message(email_event)
                customer_copy = f"{sms_message} {subject} {text_body}".lower()

                self.assertIn(case["expected"].lower(), customer_copy)
                for internal_term in ("batch", "instruction", "settlement", "execution", "payout"):
                    self.assertNotIn(internal_term, customer_copy)

    @override_settings(
        NOTIFY_SMS_TEMPLATE="sms_default",
        NOTIFY_EMAIL_TEMPLATE="email_default",
    )
    def test_notification_interface_sends_exact_notify_contract(self):
        sent_requests = []

        class FakeResponse:
            status_code = 200
            text = '{"status":"queued"}'

            def raise_for_status(self):
                return None

        def fake_post(url, headers=None, json=None, timeout=None):
            sent_requests.append(
                {
                    "body": json,
                    "headers": headers,
                }
            )
            return FakeResponse()

        interface = NotificationInterface(
            base_url="https://notify.example/api/send",
            api_key="notify-key",
        )
        with patch("notifications.services.requests.post", side_effect=fake_post):
            interface.send_sms(
                "Your SMS message",
                ["254700000099"],
                unique_identifier="sms-reference",
            )
            interface.send_email(
                "Your email message",
                ["notify@example.com"],
                unique_identifier="email-reference",
            )

        self.assertEqual(
            [request_data["body"] for request_data in sent_requests],
            [
                {
                    "notification_type": "sms",
                    "template": "sms_default",
                    "unique_identifier": "sms-reference",
                    "recipients": ["254700000099"],
                    "context": {"message": "Your SMS message"},
                },
                {
                    "notification_type": "email",
                    "template": "email_default",
                    "unique_identifier": "email-reference",
                    "recipients": ["notify@example.com"],
                    "context": {"message": "Your email message"},
                },
            ],
        )
        self.assertTrue(
            all(request_data["headers"].get("X-API-KEY") == "notify-key" for request_data in sent_requests)
        )

    @override_settings(
        NOTIFY_URL="https://notify.example/api/send",
        NOTIFY_API_KEY="notify-key",
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="QuickBills <mvpmtech@gmail.com>",
    )
    def test_sms_dispatch_failure_does_not_break_notification_worker(self):
        user = User.objects.create_user(
            phone_number="254700000199",
            password="StrongPass123!",
            full_name="SMS Safe User",
            email="sms-safe@example.com",
            account_type="INDIVIDUAL",
            email_notifications_enabled=False,
            sms_notifications_enabled=True,
        )
        queue_notifications_for_user(user, "PAYMENT_SUCCESS", {"batch_id": "batch-safe", "total_amount_minor": 120000})

        sent_channels = []

        class FakeResponse:
            status_code = 200
            text = '{"status":"queued"}'

            def raise_for_status(self):
                return None

        def fail_sms_only(url, headers=None, json=None, timeout=None):
            channel = json["notification_type"]
            sent_channels.append(channel)
            if channel == "sms":
                raise OSError("SMS provider unavailable")
            return FakeResponse()

        with patch("notifications.services.requests.post", side_effect=fail_sms_only):
            call_command("process_notifications")

        sms_event = NotificationEvent.objects.get(channel="SMS", event_type="PAYMENT_SUCCESS")
        email_event = NotificationEvent.objects.get(channel="EMAIL", event_type="PAYMENT_SUCCESS")
        self.assertEqual(sms_event.status, NotificationEvent.Status.FAILED)
        self.assertIn("SMS provider unavailable", sms_event.last_error)
        self.assertEqual(sms_event.provider_response["email_backup"]["status"], "sent")
        self.assertEqual(email_event.status, NotificationEvent.Status.SENT)
        self.assertEqual(email_event.context["batch_id"], "batch-safe")
        self.assertEqual(sent_channels, ["sms", "email"])
        self.assertEqual(len(mail.outbox), 0)

    def test_in_app_product_announcement_can_be_listed_and_marked_read(self):
        user = User.objects.create_user(
            phone_number="254700000188",
            password="StrongPass123!",
            full_name="Announcement User",
            account_type=User.AccountType.INDIVIDUAL,
        )
        admin = User.objects.create_user(
            phone_number="254700000187",
            password="StrongPass123!",
            full_name="Support Admin",
            account_type=User.AccountType.SUPERADMIN,
        )
        _, admin_token = AccessToken.issue(admin)
        _, user_token = AccessToken.issue(user)

        response = self._post(
            "/api/v1/notifications/announcements/",
            {"title": "Transactions tab is live", "body": "You can now review every payment status in one place."},
            token=admin_token,
        )
        self.assertEqual(response.status_code, 201)
        self.assertGreaterEqual(response.json()["created"], 2)

        response = self.client.get("/api/v1/notifications/", HTTP_AUTHORIZATION=f"Bearer {user_token}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["unread_count"], 1)
        notification = payload["notifications"][0]
        self.assertEqual(notification["title"], "Transactions tab is live")
        self.assertIsNone(notification["read_at"])

        response = self._post(f"/api/v1/notifications/{notification['id']}/read/", {}, token=user_token)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()["notification"]["read_at"])

    def test_in_app_notification_popup_copy_has_quickbills_fallbacks(self):
        user = User.objects.create_user(
            phone_number="254700000189",
            password="StrongPass123!",
            full_name="Fallback User",
            account_type=User.AccountType.INDIVIDUAL,
        )
        event = NotificationEvent.objects.create(
            user=user,
            channel="IN_APP",
            event_type="PRODUCT_UPDATE",
            status=NotificationEvent.Status.SENT,
            scheduled_for=timezone.now(),
            sent_at=timezone.now(),
            recipients=[str(user.id)],
            context={"title": " ", "body": " ", "badge": " ", "cta_label": " "},
        )

        notification = serialize_in_app_notification(event)

        self.assertEqual(notification["title"], "QuickBills update")
        self.assertEqual(notification["badge"], "Product Update")
        self.assertIn("QuickBills", notification["body"])
        self.assertEqual(notification["cta_label"], "Open QuickBills")

    def test_reminder_generation_covers_due_low_wallet_and_overdue_payments(self):
        today = timezone.localdate()
        user = User.objects.create_user(
            phone_number="254700000177",
            password="StrongPass123!",
            full_name="Reminder User",
            email="reminder@example.com",
            account_type=User.AccountType.INDIVIDUAL,
            default_payment_mode=User.PaymentMode.WALLET,
        )
        ensure_user_wallets(user)
        payee = Payee.objects.create(
            user=user,
            label="Rent",
            payee_type=Payee.PayeeType.PAYBILL,
            paybill_number="123456",
            expense_category="housing",
        )
        PaymentSchedule.objects.create(
            payee=payee,
            amount_minor=100000,
            day_of_month=(today + timedelta(days=3)).day,
            next_due_date=today + timedelta(days=3),
        )
        PaymentSchedule.objects.create(
            payee=payee,
            amount_minor=200000,
            day_of_month=today.day,
            next_due_date=today,
        )
        PaymentSchedule.objects.create(
            payee=payee,
            amount_minor=300000,
            day_of_month=(today - timedelta(days=2)).day,
            next_due_date=today - timedelta(days=2),
        )

        created = create_all_reminder_notifications(today)

        self.assertEqual(created["t_minus_3"], 2)
        self.assertEqual(created["due_today"], 2)
        self.assertEqual(created["low_wallet"], 2)
        self.assertEqual(created["overdue"], 2)
        self.assertEqual(NotificationEvent.objects.filter(event_type="WALLET_LOW").count(), 2)
        overdue_event = NotificationEvent.objects.filter(event_type="OVERDUE_PAYMENT", channel="SMS").get()
        self.assertEqual(overdue_event.context["oldest_overdue_days"], 2)

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
            {"name": "QuickBills Ops"},
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
