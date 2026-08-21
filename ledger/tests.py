import hashlib
import hmac
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from base.models import PaymentBatch, PaymentInstruction
from eusers.models import User

from .models import Account, BalanceLog, BalanceLogEntry, PaymentRequest, Transaction
from .services import (
    IdempotencyConflict,
    LedgerError,
    PaymentService,
    complete_pay_in,
    complete_payout,
    get_or_create_user_account,
    initiate_pay_in,
    initiate_payout,
)


class LedgerServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            phone_number="254700900001",
            full_name="Ledger Test User",
            account_type=User.AccountType.INDIVIDUAL,
            password="test-pass",
        )
        self.account = get_or_create_user_account(self.user)

    def test_pay_in_does_not_credit_wallet_until_success(self):
        tx = initiate_pay_in(
            self.account,
            amount_minor=100000,
            reference="TOPUP-001",
            idempotency_key="topup-key-001",
        )

        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance_minor, 0)
        self.assertEqual(self.account.uncleared_balance_minor, 0)
        self.assertEqual(self.account.available_balance_minor, 0)

        complete_pay_in(tx, receipt="RCT-001", confirmation_key="CONF-001")
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance_minor, 100000)
        self.assertEqual(self.account.uncleared_balance_minor, 0)
        self.assertEqual(self.account.available_balance_minor, 100000)
        self.assertEqual(BalanceLogEntry.objects.filter(balance_log__transaction=tx).count(), 2)
        balance_log = BalanceLog.objects.get(transaction=tx)
        self.assertEqual(balance_log.metadata["wallet_id"], str(self.account.id))
        self.assertEqual(balance_log.metadata["transaction_id"], str(tx.id))
        self.assertEqual(balance_log.metadata["transaction_type"], "WalletTopup")
        self.assertEqual(balance_log.metadata["amount_minor"], "100000.00")
        self.assertEqual(balance_log.metadata["currency"], "KES")
        self.assertEqual(balance_log.metadata["direction"], Transaction.Direction.PAY_IN)
        self.assertEqual(balance_log.metadata["previous_balance_minor"], "0.00")
        self.assertEqual(balance_log.metadata["new_balance_minor"], "100000.00")
        self.assertEqual(balance_log.metadata["previous_available_balance_minor"], "0.00")
        self.assertEqual(balance_log.metadata["new_available_balance_minor"], "100000.00")
        self.assertEqual(balance_log.metadata["previous_reserved_balance_minor"], "0.00")
        self.assertEqual(balance_log.metadata["new_reserved_balance_minor"], "0.00")
        self.assertEqual(balance_log.metadata["external_payment_reference"], "TOPUP-001")
        self.assertEqual(balance_log.metadata["idempotency_key"], "topup-key-001")
        self.assertEqual(balance_log.metadata["source_event"], "CompletePayIn")

        complete_pay_in(tx, receipt="RCT-001", confirmation_key="CONF-001")
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance_minor, 100000)
        self.assertEqual(self.account.available_balance_minor, 100000)
        self.assertEqual(BalanceLogEntry.objects.filter(balance_log__transaction=tx).count(), 2)

    def test_payout_reserves_then_settles(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=100000, reference="TOPUP-002"))

        tx = initiate_payout(self.account, amount_minor=100000, reference="WITHDRAW-001")
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_balance_minor, 0)
        self.assertEqual(self.account.reserved_balance_minor, 100000)

        complete_payout(tx, receipt="PAYOUT-001")
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_balance_minor, 0)
        self.assertEqual(self.account.reserved_balance_minor, 0)
        self.assertEqual(self.account.current_balance_minor, 0)
        self.assertEqual(tx.status, Transaction.Status.PROCESSING)
        tx.refresh_from_db()
        self.assertEqual(tx.status, Transaction.Status.COMPLETED)

    def test_reset_customer_ledger_is_dry_run_without_confirmation(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=100000, reference="TOPUP-RESET-DRY"))
        stdout = StringIO()

        call_command("reset_customer_ledger", "0700900001", stdout=stdout)

        self.account.refresh_from_db()
        self.assertEqual(self.account.available_balance_minor, 100000)
        self.assertEqual(Transaction.objects.filter(account=self.account).count(), 1)
        self.assertIn("Dry run only", stdout.getvalue())

    def test_reset_customer_ledger_deletes_transactions_and_zeroes_balances(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=100000, reference="TOPUP-RESET"))
        initiate_payout(self.account, amount_minor=25000, reference="PAYOUT-RESET")
        stdout = StringIO()

        call_command("reset_customer_ledger", "0700900001", "--confirm", stdout=stdout)

        self.account.refresh_from_db()
        self.assertEqual(Transaction.objects.filter(account=self.account).count(), 0)
        self.assertEqual(self.account.current_balance_minor, 0)
        self.assertEqual(self.account.available_balance_minor, 0)
        self.assertEqual(self.account.reserved_balance_minor, 0)
        self.assertEqual(self.account.uncleared_balance_minor, 0)
        self.assertEqual(self.account.charge_balance_minor, 0)
        self.assertIn("deleted 2 ledger transaction(s)", stdout.getvalue())

    def test_idempotency_key_reuse_with_different_payload_is_rejected(self):
        initiate_pay_in(
            self.account,
            amount_minor=100000,
            reference="TOPUP-004",
            idempotency_key="topup-key-004",
        )

        with self.assertRaises(IdempotencyConflict):
            initiate_pay_in(
                self.account,
                amount_minor=200000,
                reference="TOPUP-004-DIFFERENT",
                idempotency_key="topup-key-004",
            )

    def test_payment_service_sandbox_completes_webhook_flow(self):
        payment_request = PaymentService(sandbox=True).initiate_stk_push(
            self.account,
            amount_minor=50000,
            phone_number="254700900001",
        )

        payment_request.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.COMPLETED)
        self.assertEqual(self.account.available_balance_minor, 50000)
        self.assertEqual(Account.objects.count(), 1)

    @override_settings(PAYMENT_CALLBACK_URL="https://qb.example/api/v1/payments/webhook/")
    def test_payment_service_live_stk_uses_lipasync_payload(self):
        response_payload = {
            "message": "Payment initiated successfully",
            "data": {
                "payment_intent_id": "PI-STK-001",
                "status": "INITIATED",
                "amount": "500.00",
                "currency": "KES",
            },
        }

        with patch.object(PaymentService, "_post", return_value=response_payload) as post:
            payment_request = PaymentService(sandbox=False, base_url="https://payments.example").initiate_stk_push(
                self.account,
                amount_minor=50000,
                phone_number="254700900001",
                metadata={"purpose": "batch_collection", "batch_id": "batch-001"},
            )

        path, payload = post.call_args.args
        self.assertEqual(path, "/stk-push/initiate/")
        self.assertEqual(payload["amount"], 500.0)
        self.assertEqual(payload["currency"], "KES")
        self.assertEqual(payload["external_reference"], payment_request.originator_ref)
        self.assertEqual(payload["idempotency_key"], payment_request.originator_ref)
        self.assertEqual(payload["callback_url"], "https://qb.example/api/v1/payments/webhook/")
        self.assertEqual(
            payload["payment_payload"],
            {
                "daraja_flow": "stk_push",
                "phone_number": "254700900001",
                "account_reference": "",
            },
        )
        self.assertEqual(payment_request.request_id, "PI-STK-001")
        self.assertEqual(payment_request.status, PaymentRequest.Status.PROCESSING)
        self.assertEqual(payment_request.request_payload["metadata"]["purpose"], "batch_collection")
        payment_request.transaction.refresh_from_db()
        self.assertEqual(payment_request.transaction.request_id, "PI-STK-001")

    @override_settings(
        PESAWAY_SYSTEM_SLUG="quickbills",
        PESAWAY_COLLECTION_EVENT_SLUG="collection",
    )
    def test_payment_service_pesaway_stk_uses_inbound_collection_payload(self):
        response_payload = {
            "success": True,
            "message": "Inbound payment initiated successfully",
            "data": {
                "inbound_payment_id": "IN-STK-001",
                "status": "INITIATED",
                "amount": "500.000000",
                "currency": "KES",
            },
        }

        with patch.object(PaymentService, "_post", return_value=response_payload) as post:
            payment_request = PaymentService(sandbox=False, base_url="https://payments.lipasync.com/api/v1/core").initiate_stk_push(
                self.account,
                amount_minor=50000,
                phone_number="254700900001",
            )

        path, payload = post.call_args.args
        self.assertEqual(path, "/inbound-payments/quickbills/collection/initiate/")
        self.assertEqual(payload["amount"], "500.00")
        self.assertEqual(payload["external_reference"], payment_request.originator_ref)
        self.assertEqual(payload["idempotency_key"], payment_request.originator_ref)
        self.assertEqual(
            payload["provider_payload"],
            {
                "phone_number": "254700900001",
                "channel": "MPESA",
                "reason": "Wallet top-up",
            },
        )
        self.assertEqual(payment_request.request_id, "IN-STK-001")
        self.assertEqual(payment_request.status, PaymentRequest.Status.PROCESSING)
        payment_request.transaction.refresh_from_db()
        self.assertEqual(payment_request.transaction.request_id, "IN-STK-001")

        PaymentService().handle_webhook(
            {
                "event": "inbound_payment.captured",
                "inbound_payment_id": "IN-STK-001",
                "status": "CAPTURED",
                "external_reference": payment_request.originator_ref,
                "provider_transaction_id": "MPESA-RECEIPT-001",
            }
        )
        payment_request.refresh_from_db()
        payment_request.transaction.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.COMPLETED)
        self.assertEqual(payment_request.transaction.status, Transaction.Status.COMPLETED)
        self.assertEqual(payment_request.transaction.transaction_receipt, "MPESA-RECEIPT-001")
        self.assertEqual(self.account.available_balance_minor, 50000)

    def test_payment_service_live_paybill_payout_uses_lipasync_b2b_payload(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=100000, reference="FUND-B2B-001"))
        response_payload = {
            "message": "Payment initiated successfully",
            "data": {"payment_intent_id": "PI-B2B-001", "status": "INITIATED", "amount": "750.00", "currency": "KES"},
        }

        with patch.object(PaymentService, "_post", return_value=response_payload) as post:
            payment_request = PaymentService(sandbox=False, base_url="https://payments.example").initiate_payout(
                self.account,
                amount_minor=75000,
                destination={"paybill_number": "600000", "account_reference": "INV-001"},
            )

        path, payload = post.call_args.args
        self.assertEqual(path, "/b2b_paybill/initiate/")
        self.assertEqual(payload["amount"], 750.0)
        self.assertEqual(payload["external_reference"], payment_request.originator_ref)
        self.assertEqual(
            payload["payment_payload"],
            {
                "daraja_flow": "b2b_paybill",
                "destination_shortcode": "600000",
                "account_reference": "INV-001",
            },
        )
        self.assertEqual(payment_request.request_id, "PI-B2B-001")
        self.assertEqual(payment_request.status, PaymentRequest.Status.PROCESSING)

    @override_settings(
        PESAWAY_SYSTEM_SLUG="quickbills",
        PESAWAY_B2C_EVENT_SLUG="b2c",
        PESAWAY_B2B_EVENT_SLUG="b2b",
        PESAWAY_BANK_EVENT_SLUG="bank",
    )
    def test_payment_service_pesaway_payout_routes_by_destination(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=300000, reference="FUND-PESAWAY-001"))
        response_payload = {
            "success": True,
            "message": "Outbound transfer queued",
            "data": {
                "outbound_transfer_id": "OUT-001",
                "outbound_transfer_event": "b2c",
                "status": "QUEUED",
                "recipient_type": "B2C",
                "payment_method_type": "MOBILE_MONEY",
                "amount": "1000.000000",
                "currency": "KES",
            },
            "error": "",
        }

        with patch.object(PaymentService, "_post", return_value=response_payload) as post:
            payment_request = PaymentService(sandbox=False, base_url="https://payments.lipasync.com/api/v1/core").initiate_payout(
                self.account,
                amount_minor=100000,
                destination={"phone_number": "254700900001"},
            )

        path, payload = post.call_args.args
        self.assertEqual(path, "/outbound-transfers/quickbills/b2c/initiate/")
        self.assertEqual(payload["amount"], "1000.00")
        self.assertEqual(payload["provider_payload"]["phone_number"], "254700900001")
        self.assertEqual(payload["provider_payload"]["channel"], "MPESA")
        self.assertNotIn("callback_url", payload)
        self.assertEqual(payment_request.request_id, "OUT-001")
        self.assertEqual(payment_request.status, PaymentRequest.Status.PROCESSING)
        self.assertEqual(payment_request.response_payload, response_payload)
        payment_request.transaction.refresh_from_db()
        self.assertEqual(payment_request.transaction.request_id, "OUT-001")

    @override_settings(PESAWAY_SYSTEM_SLUG="quickbills", PESAWAY_B2C_EVENT_SLUG="b2c")
    def test_live_payout_submission_success_waits_for_callback(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=100000, reference="FUND-PAYOUT-CALLBACK"))
        submission = {
            "success": True,
            "message": "Outbound transfer submitted",
            "data": {
                "outbound_transfer_id": "OUT-CALLBACK-001",
                "status": "SUCCESS",
                "external_reference": "ignored-in-favor-of-request-context",
            },
        }

        with patch.object(PaymentService, "_post", return_value=submission):
            payment_request = PaymentService(
                sandbox=False,
                base_url="https://payments.lipasync.com/api/v1/core",
            ).initiate_payout(
                self.account,
                amount_minor=75000,
                destination={"phone_number": "254700900001"},
            )

        payment_request.refresh_from_db()
        payment_request.transaction.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.PROCESSING)
        self.assertEqual(payment_request.transaction.status, Transaction.Status.PROCESSING)
        self.assertEqual(payment_request.transaction.request_id, "OUT-CALLBACK-001")
        self.assertEqual(self.account.available_balance_minor, 25000)
        self.assertEqual(self.account.reserved_balance_minor, 75000)

        PaymentService().handle_webhook(
            {
                "event": "outbound_transfer.success",
                "outbound_transfer_id": payment_request.request_id,
                "external_reference": payment_request.originator_ref,
                "status": "SUCCESS",
                "provider_transaction_id": "MPESA-RECEIPT-001",
            }
        )

        payment_request.refresh_from_db()
        payment_request.transaction.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.COMPLETED)
        self.assertEqual(payment_request.transaction.status, Transaction.Status.COMPLETED)
        self.assertEqual(payment_request.transaction.transaction_receipt, "MPESA-RECEIPT-001")
        self.assertEqual(self.account.reserved_balance_minor, 0)

    @override_settings(
        PESAWAY_SYSTEM_SLUG="qb",
        PESAWAY_B2C_EVENT_SLUG="b2c",
        PESAWAY_B2B_EVENT_SLUG="b2b",
        PESAWAY_BANK_EVENT_SLUG="bank-transfer",
    )
    def test_pesaway_outbound_payloads_match_all_event_contracts(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=400000, reference="FUND-PESAWAY-ROUTES"))
        cases = (
            (
                {"phone_number": "254700900002", "channel": "Airtel", "reason": "Airtel payout"},
                "/outbound-transfers/qb/b2c/initiate/",
                {"phone_number": "254700900002", "channel": "Airtel", "reason": "Airtel payout"},
            ),
            (
                {"paybill_number": "123456", "reason": "Supplier payment"},
                "/outbound-transfers/qb/b2b/initiate/",
                {"account_number": "123456", "channel": "MPESA Paybill", "reason": "Supplier payment"},
            ),
            (
                {"till_number": "654321", "reason": "Merchant payment"},
                "/outbound-transfers/qb/b2b/initiate/",
                {"account_number": "654321", "channel": "MPESA Till", "reason": "Merchant payment"},
            ),
            (
                {"account_number": "0123456789", "bank_name": "KCB", "reason": "Salary payment"},
                "/outbound-transfers/qb/bank-transfer/initiate/",
                {"account_number": "0123456789", "bank_name": "KCB", "reason": "Salary payment"},
            ),
        )

        for index, (destination, expected_path, expected_provider_payload) in enumerate(cases, start=1):
            response_payload = {
                "success": True,
                "data": {"outbound_transfer_id": f"OUT-ROUTE-{index}", "status": "QUEUED"},
            }
            with self.subTest(destination=destination), patch.object(
                PaymentService,
                "_post",
                return_value=response_payload,
            ) as post:
                payment_request = PaymentService(
                    sandbox=False,
                    base_url="https://payments.lipasync.com/api/v1/core",
                ).initiate_payout(self.account, amount_minor=50000, destination=destination)

                path, payload = post.call_args.args
                self.assertEqual(path, expected_path)
                self.assertEqual(payload["amount"], "500.00")
                self.assertEqual(payload["idempotency_key"], payment_request.originator_ref)
                self.assertEqual(payload["external_reference"], payment_request.originator_ref)
                self.assertEqual(payload["provider_payload"], expected_provider_payload)
                self.assertNotIn("currency", payload)
                self.assertNotIn("recipient_type", payload)
                self.assertNotIn("payment_method_type", payload)

    @override_settings(
        PESAWAY_SYSTEM_SLUG="qb",
        PESAWAY_B2C_EVENT_SLUG="b2c",
        PESAWAY_B2B_EVENT_SLUG="b2b",
    )
    def test_pesaway_instruction_recipient_type_controls_outbound_route(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=100000, reference="FUND-PESAWAY-TYPED"))
        batch = PaymentBatch.objects.create(
            batch_kind=PaymentBatch.BatchKind.INDIVIDUAL_ADHOC,
            status=PaymentBatch.Status.PROCESSING,
            payment_mode=PaymentBatch.PaymentMode.WALLET,
            user=self.user,
            scheduled_for=timezone.localdate(),
            total_amount_minor=50000,
        )
        instruction = PaymentInstruction.objects.create(
            batch=batch,
            recipient_name="Supplier Paybill",
            recipient_type="PAYBILL",
            destination={
                "phone_number": "254700900001",
                "paybill_number": "123456",
                "account_reference": "ACC-1",
            },
            amount_minor=50000,
        )

        with patch.object(
            PaymentService,
            "_post",
            return_value={"success": True, "data": {"outbound_transfer_id": "OUT-TYPED-001", "status": "QUEUED"}},
        ) as post:
            PaymentService(
                sandbox=False,
                base_url="https://payments.lipasync.com/api/v1/core",
            ).initiate_instruction_payout(instruction)

        path, payload = post.call_args.args
        self.assertEqual(path, "/outbound-transfers/qb/b2b/initiate/")
        self.assertEqual(
            payload["provider_payload"],
            {
                "account_number": "123456",
                "channel": "MPESA Paybill",
                "reason": "QuickBills payout",
            },
        )
        self.assertNotIn("currency", payload)
        self.assertNotIn("recipient_type", payload)
        self.assertNotIn("payment_method_type", payload)

    @override_settings(PESAWAY_SYSTEM_SLUG="qb", PESAWAY_B2C_EVENT_SLUG="b2c")
    def test_pesaway_rejects_invalid_mobile_channel(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=100000, reference="FUND-PESAWAY-CHANNEL"))

        with self.assertRaisesMessage(LedgerError, "mobile channel must be exactly MPESA or Airtel"):
            PaymentService(
                sandbox=False,
                base_url="https://payments.lipasync.com/api/v1/core",
            ).initiate_payout(
                self.account,
                amount_minor=50000,
                destination={"phone_number": "254700900001", "channel": "AIRTEL"},
            )

    def test_pesaway_requests_use_api_key_header_without_bearer_auth(self):
        class FakeResponse:
            status_code = 200
            text = '{"success":true,"data":{"status":"QUEUED"}}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"success": True, "data": {"status": "QUEUED"}}

        interface = PaymentService(
            sandbox=False,
            base_url="https://payments.lipasync.com/api/v1/core",
            api_key="pesaway-api-key",
        )
        with (
            patch("ledger.services.requests.get", return_value=FakeResponse()) as get,
            patch("ledger.services.requests.post", return_value=FakeResponse()) as post,
        ):
            interface._get("/outbound-transfers/00000000-0000-0000-0000-000000000000/status/")
            interface._post(
                "/inbound-payments/qb/collection/initiate/",
                {
                    "amount": "100.00",
                    "idempotency_key": "test-key",
                    "external_reference": "test-key",
                    "provider_payload": {"phone_number": "254700900001"},
                },
            )

        for call in (get.call_args, post.call_args):
            headers = call.kwargs["headers"]
            self.assertEqual(headers["X-API-KEY"], "pesaway-api-key")
            self.assertNotIn("Authorization", headers)

    @override_settings(
        PESAWAY_SYSTEM_SLUG="qb",
        PESAWAY_B2C_EVENT_SLUG="b2c",
    )
    def test_pesaway_failed_webhook_fails_payout_and_releases_funds(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=100000, reference="FUND-PESAWAY-FAIL"))
        with patch.object(
            PaymentService,
            "_post",
            return_value={
                "success": True,
                "data": {"outbound_transfer_id": "OUT-FAILED-001", "status": "QUEUED"},
            },
        ):
            payment_request = PaymentService(
                sandbox=False,
                base_url="https://payments.lipasync.com/api/v1/core",
            ).initiate_payout(
                self.account,
                amount_minor=75000,
                destination={"phone_number": "254700900001"},
            )

        PaymentService().handle_webhook(
            {
                "event": "outbound_transfer.failed",
                "outbound_transfer_id": "OUT-FAILED-001",
                "status": "FAILED",
                "external_reference": payment_request.originator_ref,
                "failure_code": "RECIPIENT_REJECTED",
                "failure_reason": "Recipient account could not be credited",
            }
        )

        payment_request.refresh_from_db()
        payment_request.transaction.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.FAILED)
        self.assertEqual(payment_request.transaction.status, Transaction.Status.FAILED)
        self.assertEqual(payment_request.last_error, "Recipient account could not be credited")
        self.assertEqual(self.account.available_balance_minor, 100000)
        self.assertEqual(self.account.reserved_balance_minor, 0)

    @override_settings(
        PESAWAY_SYSTEM_SLUG="qb",
        PESAWAY_B2C_EVENT_SLUG="b2c",
    )
    def test_late_success_after_failed_payout_does_not_consume_released_reservation(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=100000, reference="FUND-PESAWAY-LATE-SUCCESS"))
        with patch.object(
            PaymentService,
            "_post",
            return_value={
                "success": True,
                "data": {"outbound_transfer_id": "OUT-LATE-SUCCESS-001", "status": "QUEUED"},
            },
        ):
            payment_request = PaymentService(
                sandbox=False,
                base_url="https://payments.lipasync.com/api/v1/core",
            ).initiate_payout(
                self.account,
                amount_minor=75000,
                destination={"phone_number": "254700900001"},
            )

        PaymentService().handle_webhook(
            {
                "event": "outbound_transfer.failed",
                "outbound_transfer_id": "OUT-LATE-SUCCESS-001",
                "status": "FAILED",
                "external_reference": payment_request.originator_ref,
                "failure_reason": "Recipient account could not be credited",
            }
        )
        entry_count = BalanceLogEntry.objects.filter(balance_log__transaction=payment_request.transaction).count()

        PaymentService().handle_webhook(
            {
                "event": "outbound_transfer.completed",
                "outbound_transfer_id": "OUT-LATE-SUCCESS-001",
                "status": "COMPLETED",
                "external_reference": payment_request.originator_ref,
                "amount": "750.00",
                "currency": "KES",
            }
        )

        payment_request.refresh_from_db()
        payment_request.transaction.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.FAILED)
        self.assertEqual(payment_request.transaction.status, Transaction.Status.FAILED)
        self.assertEqual(self.account.available_balance_minor, 100000)
        self.assertEqual(self.account.reserved_balance_minor, 0)
        self.assertEqual(BalanceLogEntry.objects.filter(balance_log__transaction=payment_request.transaction).count(), entry_count)

    @override_settings(
        PESAWAY_SYSTEM_SLUG="qb",
        PESAWAY_B2C_EVENT_SLUG="b2c",
    )
    def test_live_pesaway_payout_reserves_and_does_not_permanently_debit_until_success_callback(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=100000, reference="FUND-PESAWAY-PENDING"))

        with patch.object(
            PaymentService,
            "_post",
            return_value={
                "success": True,
                "message": "Outbound transfer queued",
                "data": {"outbound_transfer_id": "OUT-PENDING-001", "status": "QUEUED"},
            },
        ):
            payment_request = PaymentService(
                sandbox=False,
                base_url="https://payments.lipasync.com/api/v1/core",
            ).initiate_payout(
                self.account,
                amount_minor=75000,
                destination={"phone_number": "254700900001"},
            )

        payment_request.refresh_from_db()
        payment_request.transaction.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.PROCESSING)
        self.assertEqual(payment_request.transaction.status, Transaction.Status.PROCESSING)
        self.assertEqual(self.account.available_balance_minor, 25000)
        self.assertEqual(self.account.current_balance_minor, 100000)
        self.assertEqual(self.account.reserved_balance_minor, 75000)

        PaymentService().handle_webhook(
            {
                "event": "outbound_transfer.success",
                "outbound_transfer_id": "OUT-PENDING-001",
                "status": "SUCCESS",
                "external_reference": payment_request.originator_ref,
                "provider_transaction_id": "PHY123456",
            }
        )

        payment_request.refresh_from_db()
        payment_request.transaction.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.COMPLETED)
        self.assertEqual(payment_request.transaction.status, Transaction.Status.COMPLETED)
        self.assertEqual(payment_request.transaction.transaction_receipt, "PHY123456")
        self.assertEqual(self.account.available_balance_minor, 25000)
        self.assertEqual(self.account.current_balance_minor, 25000)
        self.assertEqual(self.account.reserved_balance_minor, 0)

        entry_count = BalanceLogEntry.objects.filter(balance_log__transaction=payment_request.transaction).count()
        PaymentService().handle_webhook(
            {
                "event": "outbound_transfer.success",
                "outbound_transfer_id": "OUT-PENDING-001",
                "status": "SUCCESS",
                "external_reference": payment_request.originator_ref,
                "provider_transaction_id": "PHY123456",
            }
        )
        self.account.refresh_from_db()
        payment_request.transaction.refresh_from_db()
        self.assertEqual(self.account.available_balance_minor, 25000)
        self.assertEqual(self.account.current_balance_minor, 25000)
        self.assertEqual(self.account.reserved_balance_minor, 0)
        self.assertEqual(BalanceLogEntry.objects.filter(balance_log__transaction=payment_request.transaction).count(), entry_count)

    @override_settings(
        PESAWAY_SYSTEM_SLUG="qb",
        PESAWAY_B2C_EVENT_SLUG="b2c",
    )
    def test_payout_callback_with_amount_mismatch_does_not_mutate_wallet(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=100000, reference="FUND-PESAWAY-AMOUNT-MISMATCH"))

        with patch.object(
            PaymentService,
            "_post",
            return_value={
                "success": True,
                "message": "Outbound transfer queued",
                "data": {"outbound_transfer_id": "OUT-AMOUNT-MISMATCH", "status": "QUEUED"},
            },
        ):
            payment_request = PaymentService(
                sandbox=False,
                base_url="https://payments.lipasync.com/api/v1/core",
            ).initiate_payout(
                self.account,
                amount_minor=75000,
                destination={"phone_number": "254700900001"},
            )

        with self.assertRaisesMessage(LedgerError, "amount does not match"):
            PaymentService().handle_webhook(
                {
                    "event": "outbound_transfer.success",
                    "outbound_transfer_id": "OUT-AMOUNT-MISMATCH",
                    "status": "SUCCESS",
                    "amount": "760.00",
                    "currency": "KES",
                    "external_reference": payment_request.originator_ref,
                    "provider_transaction_id": "PHY-AMOUNT-MISMATCH",
                }
            )

        payment_request.refresh_from_db()
        payment_request.transaction.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.PROCESSING)
        self.assertEqual(payment_request.transaction.status, Transaction.Status.PROCESSING)
        self.assertEqual(self.account.available_balance_minor, 25000)
        self.assertEqual(self.account.current_balance_minor, 100000)
        self.assertEqual(self.account.reserved_balance_minor, 75000)

    def test_lipasync_processing_callback_keeps_payment_processing(self):
        with patch.object(
            PaymentService,
            "_post",
            return_value={"data": {"payment_intent_id": "PI-PENDING-001", "status": "INITIATED"}},
        ):
            payment_request = PaymentService(sandbox=False, base_url="https://payments.example").initiate_stk_push(
                self.account,
                amount_minor=50000,
                phone_number="254700900001",
            )

        PaymentService().handle_webhook(
            {
                "event": "payment.processing",
                "payment_intent_id": "PI-PENDING-001",
                "transaction_id": "TX-PENDING-001",
                "status": "PROCESSING",
                "success": True,
                "amount": "500.00",
                "currency": "KES",
            }
        )

        payment_request.refresh_from_db()
        payment_request.transaction.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.PROCESSING)
        self.assertEqual(payment_request.transaction.status, Transaction.Status.PROCESSING)

    def test_lipasync_captured_callback_completes_payment(self):
        with patch.object(
            PaymentService,
            "_post",
            return_value={"data": {"payment_intent_id": "PI-CAPTURED-001", "status": "INITIATED"}},
        ):
            payment_request = PaymentService(sandbox=False, base_url="https://payments.example").initiate_stk_push(
                self.account,
                amount_minor=50000,
                phone_number="254700900001",
            )

        PaymentService().handle_webhook(
            {
                "event": "payment.captured",
                "payment_intent_id": "PI-CAPTURED-001",
                "transaction_id": "TX-CAPTURED-001",
                "status": "CAPTURED",
                "amount": "500.00",
                "currency": "KES",
            }
        )

        payment_request.refresh_from_db()
        self.account.refresh_from_db()
        tx = payment_request.transaction
        tx.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.COMPLETED)
        self.assertEqual(tx.status, Transaction.Status.COMPLETED)
        self.assertEqual(tx.transaction_receipt, "TX-CAPTURED-001")
        self.assertEqual(self.account.available_balance_minor, 50000)

        entry_count = BalanceLogEntry.objects.filter(balance_log__transaction=tx).count()
        PaymentService().handle_webhook(
            {
                "event": "payment.failed",
                "payment_intent_id": "PI-CAPTURED-001",
                "status": "FAILED",
                "failure_reason": "Late conflicting callback",
            }
        )
        payment_request.refresh_from_db()
        tx.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.COMPLETED)
        self.assertEqual(tx.status, Transaction.Status.COMPLETED)
        self.assertEqual(self.account.available_balance_minor, 50000)
        self.assertEqual(BalanceLogEntry.objects.filter(balance_log__transaction=tx).count(), entry_count)

    @override_settings(PESAWAY_WEBHOOK_SECRET="callback-secret")
    def test_payment_webhook_rejects_invalid_signature(self):
        response = self.client.post(
            "/api/v1/payments/webhook/",
            data=b'{"success":true,"originator_ref":"STK-001"}',
            content_type="application/json",
            HTTP_X_WEBHOOK_SIGNATURE="invalid-signature",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "Invalid webhook signature.")

    @override_settings(PESAWAY_WEBHOOK_SECRET="callback-secret")
    def test_payment_webhook_accepts_valid_signature(self):
        raw_payload = b'{"success":true,"originator_ref":"STK-001"}'
        signature = hmac.new(b"callback-secret", raw_payload, hashlib.sha256).hexdigest()
        handled_request = SimpleNamespace(
            status=PaymentRequest.Status.COMPLETED,
            originator_ref="STK-001",
            transaction_id=self.account.id,
        )
        with patch("api.views.PaymentService.handle_webhook", return_value=handled_request) as handle_webhook:
            response = self.client.post(
                "/api/v1/payments/webhook/",
                data=raw_payload,
                content_type="application/json",
                HTTP_X_WEBHOOK_SIGNATURE=signature,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], PaymentRequest.Status.COMPLETED)
        handle_webhook.assert_called_once_with({"success": True, "originator_ref": "STK-001"})

    def test_retry_stale_processing_fails_request_after_five_minutes(self):
        tx = initiate_pay_in(self.account, amount_minor=50000, reference="STK-TIMEOUT-001")
        payment_request = PaymentRequest.objects.create(
            transaction=tx,
            operation=PaymentRequest.Operation.STK_PUSH,
            originator_ref="STK-TIMEOUT-001",
            request_id="MS-STK-TIMEOUT-001",
            request_payload={
                "originator_ref": "STK-TIMEOUT-001",
                "amount_minor": 50000,
                "currency": "KES",
                "operation": PaymentRequest.Operation.STK_PUSH,
                "phone_number": "254700900001",
            },
            response_payload={"status": "PROCESSING"},
            sandbox=False,
        )
        PaymentRequest.objects.filter(id=payment_request.id).update(
            created_at=timezone.now() - timezone.timedelta(minutes=6)
        )

        with patch.object(PaymentService, "_post", return_value={"status": "PROCESSING"}) as post:
            processed = PaymentService(sandbox=False, base_url="http://payments.example").retry_stale_processing(
                query_status=False
            )

        self.assertEqual(processed, 1)
        post.assert_not_called()
        payment_request.refresh_from_db()
        tx.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.PROCESSING)
        self.assertIn("status query is unavailable", payment_request.last_error)
        self.assertEqual(tx.status, Transaction.Status.PROCESSING)
        self.assertEqual(len(payment_request.request_payload["status_query_attempts"]), 1)

    def test_retry_stale_processing_waits_five_minutes_before_status_query(self):
        tx = initiate_pay_in(self.account, amount_minor=50000, reference="STK-WAIT-001")
        payment_request = PaymentRequest.objects.create(
            transaction=tx,
            operation=PaymentRequest.Operation.PAYOUT,
            originator_ref="REQ-WAIT-001",
            request_id="MS-PAYOUT-WAIT-001",
            request_payload={
                "originator_ref": "REQ-WAIT-001",
                "amount_minor": 50000,
                "currency": "KES",
                "operation": PaymentRequest.Operation.PAYOUT,
            },
            response_payload={"status": "PROCESSING"},
            sandbox=False,
        )
        PaymentRequest.objects.filter(id=payment_request.id).update(
            created_at=timezone.now() - timezone.timedelta(minutes=4, seconds=59)
        )

        with patch.object(PaymentService, "_post", return_value={"status": "PROCESSING"}) as post:
            processed = PaymentService(sandbox=False, base_url="http://payments.example").retry_stale_processing()

        self.assertEqual(processed, 0)
        post.assert_not_called()
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.PROCESSING)

    def test_pesaway_reconciliation_completes_successful_outbound_transfer(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=100000, reference="FUND-STATUS-001"))
        tx = initiate_payout(self.account, amount_minor=75000, reference="PAYOUT-STATUS-001")
        payment_request = PaymentRequest.objects.create(
            transaction=tx,
            operation=PaymentRequest.Operation.PAYOUT,
            originator_ref="REQ-STATUS-001",
            request_id="00000000-0000-0000-0000-000000000001",
            request_payload={
                "originator_ref": "REQ-STATUS-001",
                "operation": PaymentRequest.Operation.PAYOUT,
                "destination": {"phone_number": "254700900001"},
            },
            response_payload={"status": "PROCESSING"},
            sandbox=False,
        )
        PaymentRequest.objects.filter(id=payment_request.id).update(
            created_at=timezone.now() - timezone.timedelta(minutes=6)
        )
        status_response = {
            "success": True,
            "message": "Outbound transfer retrieved",
            "data": {
                "outbound_transfer_id": payment_request.request_id,
                "status": "SUCCESS",
                "external_reference": payment_request.originator_ref,
                "provider_transaction_id": "PHY123456",
            },
        }
        interface = PaymentService(
            sandbox=False,
            base_url="https://payments.lipasync.com/api/v1/core",
        )
        with patch.object(interface, "_get", return_value=status_response) as get:
            processed = interface.retry_stale_processing(query_status=True)

        self.assertEqual(processed, 1)
        get.assert_called_once_with(
            f"/outbound-transfers/{payment_request.request_id}/status/"
        )
        payment_request.refresh_from_db()
        tx.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.COMPLETED)
        self.assertEqual(tx.status, Transaction.Status.COMPLETED)
        self.assertEqual(tx.transaction_receipt, "PHY123456")
        self.assertEqual(self.account.available_balance_minor, 25000)
        self.assertEqual(self.account.reserved_balance_minor, 0)

    def test_pesaway_outbound_callback_falls_back_to_transfer_id_when_external_reference_differs(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=100000, reference="FUND-CALLBACK-FALLBACK"))
        tx = initiate_payout(self.account, amount_minor=2000, reference="PAYOUT-2002")
        payment_request = PaymentRequest.objects.create(
            transaction=tx,
            operation=PaymentRequest.Operation.PAYOUT,
            originator_ref="REQ-CALLBACK-FALLBACK-001",
            request_id="c9e4ca95-be15-4ed7-8602-e6804491188c",
            request_payload={
                "originator_ref": "REQ-CALLBACK-FALLBACK-001",
                "operation": PaymentRequest.Operation.PAYOUT,
                "destination": {"phone_number": "254700900001"},
            },
            response_payload={"status": "PROCESSING"},
            sandbox=False,
        )

        PaymentService().handle_webhook(
            {
                "event": "outbound_transfer.success",
                "amount": "20.000000",
                "status": "SUCCESS",
                "currency": "KES",
                "failure_code": "",
                "failure_reason": "",
                "recipient_type": "B2C",
                "external_reference": "PAYOUT-2002",
                "payment_method_type": "MOBILE_MONEY",
                "outbound_transfer_id": payment_request.request_id,
                "outbound_transfer_event": "b2c",
                "provider_transaction_id": "PHY45634696E6",
            }
        )

        payment_request.refresh_from_db()
        tx.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.COMPLETED)
        self.assertEqual(tx.status, Transaction.Status.COMPLETED)
        self.assertEqual(tx.transaction_receipt, "PHY45634696E6")
        self.assertEqual(tx.request_id, "c9e4ca95-be15-4ed7-8602-e6804491188c")
        self.assertEqual(self.account.reserved_balance_minor, 0)

    def test_pesaway_reconciliation_completes_successful_inbound_transfer(self):
        tx = initiate_pay_in(self.account, amount_minor=10200, reference="STK-QB-20260812-CC8D2997")
        payment_request = PaymentRequest.objects.create(
            transaction=tx,
            operation=PaymentRequest.Operation.STK_PUSH,
            originator_ref="STK-QB-20260812-CC8D2997",
            request_id="fa15e460-414c-4b22-af19-6eb6406fcf83",
            request_payload={
                "originator_ref": "STK-QB-20260812-CC8D2997",
                "operation": PaymentRequest.Operation.STK_PUSH,
                "phone_number": "254700900001",
            },
            response_payload={"status": "PROCESSING"},
            sandbox=False,
        )
        PaymentRequest.objects.filter(id=payment_request.id).update(
            created_at=timezone.now() - timezone.timedelta(minutes=6)
        )
        status_response = {
            "success": True,
            "message": "Inbound transfer retrieved",
            "data": {
                "inbound_payment_id": payment_request.request_id,
                "status": "SETTLED",
                "amount": "102.000000",
                "currency": "KES",
                "external_reference": payment_request.originator_ref,
                "provider_transaction_id": "bfa91d56-2573-4ca0-b5f9-058a97eb7ea1",
            },
        }
        interface = PaymentService(
            sandbox=False,
            base_url="https://payments.lipasync.com/api/v1/core",
        )
        with patch.object(interface, "_get", return_value=status_response) as get:
            processed = interface.retry_stale_processing(query_status=True)

        self.assertEqual(processed, 1)
        get.assert_called_once_with(
            f"/inbound-transfers/{payment_request.request_id}/status/"
        )
        payment_request.refresh_from_db()
        tx.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.COMPLETED)
        self.assertEqual(tx.status, Transaction.Status.COMPLETED)
        self.assertEqual(tx.transaction_receipt, "bfa91d56-2573-4ca0-b5f9-058a97eb7ea1")
        self.assertEqual(tx.request_id, "fa15e460-414c-4b22-af19-6eb6406fcf83")
        self.assertEqual(self.account.available_balance_minor, 10200)

    def test_retry_stale_processing_does_not_query_lipasync_without_status_endpoint(self):
        tx = initiate_pay_in(self.account, amount_minor=50000, reference="STK-LIPASYNC-TIMEOUT-001")
        payment_request = PaymentRequest.objects.create(
            transaction=tx,
            operation=PaymentRequest.Operation.STK_PUSH,
            originator_ref="STK-LIPASYNC-TIMEOUT-001",
            request_id="PI-LIPASYNC-TIMEOUT-001",
            request_payload={
                "originator_ref": "STK-LIPASYNC-TIMEOUT-001",
                "external_reference": "STK-LIPASYNC-TIMEOUT-001",
                "amount_minor": 50000,
                "currency": "KES",
                "operation": PaymentRequest.Operation.STK_PUSH,
            },
            response_payload={"status": "PROCESSING"},
            sandbox=False,
        )
        PaymentRequest.objects.filter(id=payment_request.id).update(
            created_at=timezone.now() - timezone.timedelta(minutes=6)
        )

        with patch.object(PaymentService, "_post", return_value={"status": "PROCESSING"}) as post:
            processed = PaymentService(
                sandbox=False,
                base_url="https://payments.lipasync.com/api/v1/core/payments/qb",
            ).retry_stale_processing(query_status=True)

        self.assertEqual(processed, 1)
        post.assert_not_called()
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.PROCESSING)
        self.assertIn("status query is unavailable", payment_request.last_error)
        self.assertEqual(len(payment_request.request_payload["status_query_attempts"]), 1)

    @override_settings(
        PAYMENT_MICROSERVICE_URL="https://payments.lipasync.com/api/v1/core/payments/qb",
        PAYMENT_MICROSERVICE_SANDBOX=False,
    )
    def test_reconcile_processing_payments_command_handles_lipasync_timeout(self):
        tx = initiate_pay_in(self.account, amount_minor=50000, reference="STK-CMD-TIMEOUT-001")
        payment_request = PaymentRequest.objects.create(
            transaction=tx,
            operation=PaymentRequest.Operation.STK_PUSH,
            originator_ref="STK-CMD-TIMEOUT-001",
            request_id="PI-CMD-TIMEOUT-001",
            request_payload={
                "originator_ref": "STK-CMD-TIMEOUT-001",
                "external_reference": "STK-CMD-TIMEOUT-001",
                "amount_minor": 50000,
                "currency": "KES",
                "operation": PaymentRequest.Operation.STK_PUSH,
            },
            response_payload={"status": "PROCESSING"},
            sandbox=False,
        )
        PaymentRequest.objects.filter(id=payment_request.id).update(
            created_at=timezone.now() - timezone.timedelta(minutes=6)
        )
        stdout = StringIO()

        with patch.object(PaymentService, "_post", return_value={"status": "PROCESSING"}) as post:
            call_command(
                "reconcile_processing_payments",
                "--query-status",
                "--live",
                stdout=stdout,
            )

        post.assert_not_called()
        payment_request.refresh_from_db()
        tx.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.PROCESSING)
        self.assertEqual(tx.status, Transaction.Status.PROCESSING)
        self.assertEqual(len(payment_request.request_payload["status_query_attempts"]), 1)
        self.assertIn("Reconciled 1 processing payment request", stdout.getvalue())

    def test_retry_stale_processing_queries_then_fails_live_instruction_after_five_minutes(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=150000, reference="FUND-TIMEOUT-001"))
        tx = initiate_payout(self.account, amount_minor=100000, reference="PAYOUT-TIMEOUT-001")
        batch = PaymentBatch.objects.create(
            batch_kind=PaymentBatch.BatchKind.INDIVIDUAL_ADHOC,
            status=PaymentBatch.Status.PROCESSING,
            payment_mode=PaymentBatch.PaymentMode.WALLET,
            user=self.user,
            scheduled_for=timezone.localdate(),
            total_amount_minor=100000,
        )
        instruction = PaymentInstruction.objects.create(
            batch=batch,
            recipient_name="Timeout Recipient",
            recipient_type="MOBILE",
            destination={"phone_number": "254711222333"},
            amount_minor=100000,
            category="family",
        )
        payment_request = PaymentRequest.objects.create(
            transaction=tx,
            operation=PaymentRequest.Operation.PAYOUT,
            originator_ref="REQ-TIMEOUT-001",
            request_id="MS-PAYOUT-TIMEOUT-001",
            request_payload={
                "originator_ref": "REQ-TIMEOUT-001",
                "amount_minor": 100000,
                "currency": "KES",
                "operation": PaymentRequest.Operation.PAYOUT,
                "instruction_id": str(instruction.id),
                "batch_id": str(batch.id),
                "recipient_name": instruction.recipient_name,
                "recipient_type": instruction.recipient_type,
                "destination": instruction.destination,
            },
            response_payload={"status": "PROCESSING"},
            sandbox=False,
        )
        PaymentRequest.objects.filter(id=payment_request.id).update(
            created_at=timezone.now() - timezone.timedelta(minutes=4, seconds=59)
        )

        with patch.object(PaymentService, "_post", return_value={"status": "PROCESSING"}) as post:
            PaymentService(sandbox=False, base_url="http://payments.example").retry_stale_processing(query_status=True)

        post.assert_not_called()
        payment_request.refresh_from_db()
        instruction.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.PROCESSING)
        self.assertEqual(instruction.status, PaymentInstruction.Status.PENDING)
        self.assertEqual(instruction.failure_reason, "")
        self.assertEqual(batch.status, PaymentBatch.Status.PROCESSING)
        self.assertEqual(payment_request.last_error, "")

        PaymentRequest.objects.filter(id=payment_request.id).update(
            created_at=timezone.now() - timezone.timedelta(minutes=6)
        )
        with patch.object(PaymentService, "_post", return_value={"status": "PROCESSING"}) as post:
            PaymentService(sandbox=False, base_url="http://payments.example").retry_stale_processing()

        post.assert_called_once()
        payment_request.refresh_from_db()
        instruction.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.PROCESSING)
        self.assertEqual(instruction.status, PaymentInstruction.Status.PENDING)
        self.assertEqual(instruction.failure_reason, "")
        self.assertEqual(batch.status, PaymentBatch.Status.PROCESSING)
        self.assertEqual(len(payment_request.request_payload["status_query_attempts"]), 1)

    def test_retry_stale_processing_fails_after_five_unknown_status_queries_and_releases_reservation(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=150000, reference="FUND-QUERY-EXHAUSTED"))
        tx = initiate_payout(self.account, amount_minor=100000, reference="PAYOUT-QUERY-EXHAUSTED")
        batch = PaymentBatch.objects.create(
            batch_kind=PaymentBatch.BatchKind.INDIVIDUAL_ADHOC,
            status=PaymentBatch.Status.PROCESSING,
            payment_mode=PaymentBatch.PaymentMode.WALLET,
            user=self.user,
            scheduled_for=timezone.localdate(),
            total_amount_minor=100000,
        )
        instruction = PaymentInstruction.objects.create(
            batch=batch,
            recipient_name="Retry Recipient",
            recipient_type="MOBILE",
            destination={"phone_number": "254711222333"},
            amount_minor=100000,
            category="family",
        )
        payment_request = PaymentRequest.objects.create(
            transaction=tx,
            operation=PaymentRequest.Operation.PAYOUT,
            originator_ref="REQ-QUERY-EXHAUSTED",
            request_id="MS-PAYOUT-QUERY-EXHAUSTED",
            request_payload={
                "originator_ref": "REQ-QUERY-EXHAUSTED",
                "amount_minor": 100000,
                "currency": "KES",
                "operation": PaymentRequest.Operation.PAYOUT,
                "instruction_id": str(instruction.id),
                "batch_id": str(batch.id),
                "recipient_name": instruction.recipient_name,
                "recipient_type": instruction.recipient_type,
                "destination": instruction.destination,
                "status_query_attempts": [
                    {"attempt": 1, "outcome": "UNKNOWN"},
                    {"attempt": 2, "outcome": "UNKNOWN"},
                    {"attempt": 3, "outcome": "UNKNOWN"},
                    {"attempt": 4, "outcome": "UNKNOWN"},
                ],
            },
            response_payload={"status": "PROCESSING"},
            sandbox=False,
        )
        PaymentRequest.objects.filter(id=payment_request.id).update(
            created_at=timezone.now() - timezone.timedelta(minutes=6)
        )

        with patch.object(PaymentService, "_post", return_value={"status": "PROCESSING"}):
            PaymentService(sandbox=False, base_url="http://payments.example").retry_stale_processing(query_status=True)

        payment_request.refresh_from_db()
        tx.refresh_from_db()
        instruction.refresh_from_db()
        batch.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.FAILED)
        self.assertEqual(tx.status, Transaction.Status.FAILED)
        self.assertEqual(instruction.status, PaymentInstruction.Status.FAILED)
        self.assertEqual(batch.status, PaymentBatch.Status.FAILED)
        self.assertEqual(self.account.available_balance_minor, 150000)
        self.assertEqual(self.account.reserved_balance_minor, 0)
        self.assertEqual(len(payment_request.request_payload["status_query_attempts"]), 5)
