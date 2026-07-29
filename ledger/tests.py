from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from base.models import PaymentBatch, PaymentInstruction
from eusers.models import User

from .models import Account, BalanceLogEntry, PaymentRequest, Transaction
from .services import (
    IdempotencyConflict,
    PaymentInterface,
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

    def test_pay_in_uses_profiles_to_clear_available_balance(self):
        tx = initiate_pay_in(
            self.account,
            amount_minor=100000,
            reference="TOPUP-001",
            idempotency_key="topup-key-001",
        )

        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance_minor, 100000)
        self.assertEqual(self.account.uncleared_balance_minor, 100000)
        self.assertEqual(self.account.available_balance_minor, 0)

        complete_pay_in(tx, receipt="RCT-001", confirmation_key="CONF-001")
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance_minor, 100000)
        self.assertEqual(self.account.uncleared_balance_minor, 0)
        self.assertEqual(self.account.available_balance_minor, 100000)
        self.assertEqual(BalanceLogEntry.objects.filter(balance_log__transaction=tx).count(), 4)

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

    def test_payment_interface_sandbox_completes_webhook_flow(self):
        payment_request = PaymentInterface(sandbox=True).initiate_stk_push(
            self.account,
            amount_minor=50000,
            phone_number="254700900001",
        )

        payment_request.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.COMPLETED)
        self.assertEqual(self.account.available_balance_minor, 50000)
        self.assertEqual(Account.objects.count(), 1)

    def test_payment_interface_live_stk_uses_lipasync_payload(self):
        response_payload = {
            "message": "Payment initiated successfully",
            "data": {
                "payment_intent_id": "PI-STK-001",
                "status": "INITIATED",
                "amount": "500.00",
                "currency": "KES",
            },
        }

        with patch.object(PaymentInterface, "_post", return_value=response_payload) as post:
            payment_request = PaymentInterface(sandbox=False, base_url="https://payments.example").initiate_stk_push(
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

    def test_payment_interface_live_paybill_payout_uses_lipasync_b2b_payload(self):
        complete_pay_in(initiate_pay_in(self.account, amount_minor=100000, reference="FUND-B2B-001"))
        response_payload = {
            "message": "Payment initiated successfully",
            "data": {"payment_intent_id": "PI-B2B-001", "status": "INITIATED", "amount": "750.00", "currency": "KES"},
        }

        with patch.object(PaymentInterface, "_post", return_value=response_payload) as post:
            payment_request = PaymentInterface(sandbox=False, base_url="https://payments.example").initiate_payout(
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

    def test_lipasync_processing_callback_keeps_payment_processing(self):
        with patch.object(
            PaymentInterface,
            "_post",
            return_value={"data": {"payment_intent_id": "PI-PENDING-001", "status": "INITIATED"}},
        ):
            payment_request = PaymentInterface(sandbox=False, base_url="https://payments.example").initiate_stk_push(
                self.account,
                amount_minor=50000,
                phone_number="254700900001",
            )

        PaymentInterface().handle_webhook(
            {
                "event": "payment.processing",
                "payment_intent_id": "PI-PENDING-001",
                "transaction_id": "TX-PENDING-001",
                "status": "PROCESSING",
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
            PaymentInterface,
            "_post",
            return_value={"data": {"payment_intent_id": "PI-CAPTURED-001", "status": "INITIATED"}},
        ):
            payment_request = PaymentInterface(sandbox=False, base_url="https://payments.example").initiate_stk_push(
                self.account,
                amount_minor=50000,
                phone_number="254700900001",
            )

        PaymentInterface().handle_webhook(
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

    def test_retry_stale_processing_fails_request_after_timeout(self):
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
            created_at=timezone.now() - timezone.timedelta(minutes=4)
        )

        with patch.object(PaymentInterface, "_post", return_value={"status": "PROCESSING"}) as post:
            processed = PaymentInterface(sandbox=False, base_url="http://payments.example").retry_stale_processing()

        self.assertEqual(processed, 1)
        post.assert_not_called()
        payment_request.refresh_from_db()
        tx.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.FAILED)
        self.assertIn("timed out after 180 seconds", payment_request.last_error)
        self.assertEqual(tx.status, Transaction.Status.FAILED)
        self.assertIn("timed out after 180 seconds", tx.failure_reason)

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
            created_at=timezone.now() - timezone.timedelta(minutes=4)
        )

        with patch.object(PaymentInterface, "_post", return_value={"status": "PROCESSING"}) as post:
            processed = PaymentInterface(
                sandbox=False,
                base_url="https://payments.lipasync.com/api/v1/core/payments/qb",
            ).retry_stale_processing(query_status=True)

        self.assertEqual(processed, 1)
        post.assert_not_called()
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.FAILED)
        self.assertIn("timed out after 180 seconds", payment_request.last_error)

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
            created_at=timezone.now() - timezone.timedelta(minutes=4)
        )
        stdout = StringIO()

        with patch.object(PaymentInterface, "_post", return_value={"status": "PROCESSING"}) as post:
            call_command(
                "reconcile_processing_payments",
                "--query-status",
                "--live",
                stdout=stdout,
            )

        post.assert_not_called()
        payment_request.refresh_from_db()
        tx.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.FAILED)
        self.assertEqual(tx.status, Transaction.Status.FAILED)
        self.assertIn("Reconciled 1 processing payment request", stdout.getvalue())

    def test_retry_stale_processing_fails_instruction_with_reason(self):
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
            created_at=timezone.now() - timezone.timedelta(minutes=4)
        )

        with patch.object(PaymentInterface, "_post", return_value={"status": "PROCESSING"}) as post:
            PaymentInterface(sandbox=False, base_url="http://payments.example").retry_stale_processing(query_status=True)

        post.assert_called_once()
        payment_request.refresh_from_db()
        instruction.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequest.Status.FAILED)
        self.assertEqual(instruction.status, PaymentInstruction.Status.FAILED)
        self.assertIn("timed out after 180 seconds", instruction.failure_reason)
        self.assertEqual(batch.status, PaymentBatch.Status.FAILED)
