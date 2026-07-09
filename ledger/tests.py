from django.test import TestCase

from eusers.models import User

from .models import LedgerEntryLog, WalletAccount
from .services import IdempotencyConflict, get_or_create_user_ledger_account, post_top_up, post_withdrawal


class LedgerServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            phone_number="254700900001",
            full_name="Ledger Test User",
            account_type=User.AccountType.INDIVIDUAL,
            password="test-pass",
        )
        self.account = get_or_create_user_ledger_account(self.user)

    def test_top_up_logs_uncleared_current_then_available(self):
        entry = post_top_up(
            self.account,
            amount_minor=100000,
            reference="TOPUP-001",
            idempotency_key="topup-key-001",
        )

        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance_minor, 100000)
        self.assertEqual(self.account.uncleared_balance_minor, 0)
        self.assertEqual(self.account.reserved_balance_minor, 0)
        self.assertEqual(self.account.available_balance_minor, 100000)
        self.assertEqual(entry.balance_before_minor, 0)
        self.assertEqual(entry.balance_after_minor, 100000)
        self.assertEqual(
            list(entry.logs.order_by("sequence", "created_at").values_list("balance_field", "delta_minor")),
            [
                (LedgerEntryLog.BalanceField.UNCLEARED, 100000),
                (LedgerEntryLog.BalanceField.CURRENT, 100000),
                (LedgerEntryLog.BalanceField.UNCLEARED, -100000),
                (LedgerEntryLog.BalanceField.AVAILABLE, 100000),
            ],
        )

    def test_withdrawal_logs_available_reserved_then_current_reserved(self):
        post_top_up(
            self.account,
            amount_minor=100000,
            reference="TOPUP-002",
            idempotency_key="topup-key-002",
        )

        entry = post_withdrawal(
            self.account,
            amount_minor=100000,
            reference="WITHDRAW-001",
            idempotency_key="withdraw-key-001",
        )

        self.account.refresh_from_db()
        self.assertEqual(self.account.available_balance_minor, 0)
        self.assertEqual(self.account.reserved_balance_minor, 0)
        self.assertEqual(self.account.current_balance_minor, 0)
        self.assertEqual(self.account.uncleared_balance_minor, 0)
        self.assertEqual(entry.balance_before_minor, 100000)
        self.assertEqual(entry.balance_after_minor, 0)
        self.assertEqual(
            list(entry.logs.order_by("sequence", "created_at").values_list("balance_field", "delta_minor")),
            [
                (LedgerEntryLog.BalanceField.AVAILABLE, -100000),
                (LedgerEntryLog.BalanceField.RESERVED, 100000),
                (LedgerEntryLog.BalanceField.CURRENT, -100000),
                (LedgerEntryLog.BalanceField.RESERVED, -100000),
            ],
        )

    def test_idempotency_key_returns_existing_entry_without_duplicate_logs(self):
        first = post_top_up(
            self.account,
            amount_minor=100000,
            reference="TOPUP-003",
            idempotency_key="topup-key-003",
        )
        second = post_top_up(
            self.account,
            amount_minor=100000,
            reference="TOPUP-003",
            idempotency_key="topup-key-003",
        )

        self.account.refresh_from_db()
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.account.available_balance_minor, 100000)
        self.assertEqual(LedgerEntryLog.objects.filter(entry=first).count(), 4)
        self.assertEqual(WalletAccount.objects.count(), 1)

    def test_idempotency_key_reuse_with_different_payload_is_rejected(self):
        post_top_up(
            self.account,
            amount_minor=100000,
            reference="TOPUP-004",
            idempotency_key="topup-key-004",
        )

        with self.assertRaises(IdempotencyConflict):
            post_top_up(
                self.account,
                amount_minor=200000,
                reference="TOPUP-004-DIFFERENT",
                idempotency_key="topup-key-004",
            )
