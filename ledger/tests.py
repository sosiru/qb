from django.test import TestCase

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
