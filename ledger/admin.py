from django.contrib import admin

from .models import (
    Account,
    AccountFieldType,
    BalanceEntryType,
    BalanceLog,
    BalanceLogEntry,
    EntryType,
    ExecutionProfile,
    PaymentMethod,
    PaymentRequest,
    RuleProfile,
    RuleProfileCommand,
    State,
    Transaction,
    TransactionType,
)


class RuleProfileCommandInline(admin.TabularInline):
    model = RuleProfileCommand
    extra = 0


class RuleProfileInline(admin.TabularInline):
    model = RuleProfile
    extra = 0


class BalanceLogEntryInline(admin.TabularInline):
    model = BalanceLogEntry
    extra = 0
    readonly_fields = (
        "entry_type",
        "account_field_type",
        "amount_transacted_minor",
        "balance_before_minor",
        "balance_after_minor",
        "state",
        "created_at",
    )
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(State)
class StateAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    search_fields = ("name", "description")


@admin.register(AccountFieldType, PaymentMethod, TransactionType, BalanceEntryType, EntryType)
class LookupAdmin(admin.ModelAdmin):
    list_display = ("name", "state", "created_at")
    search_fields = ("name", "description")


@admin.register(ExecutionProfile)
class ExecutionProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "state", "created_at")
    search_fields = ("name", "description")
    inlines = (RuleProfileInline,)


@admin.register(RuleProfile)
class RuleProfileAdmin(admin.ModelAdmin):
    list_display = ("execution_profile", "name", "order", "sleep_seconds", "state")
    list_filter = ("execution_profile", "state")
    search_fields = ("name", "execution_profile__name")
    inlines = (RuleProfileCommandInline,)


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = (
        "account_number",
        "name",
        "owner_type",
        "account_kind",
        "currency",
        "available_balance_minor",
        "current_balance_minor",
        "reserved_balance_minor",
        "uncleared_balance_minor",
        "status",
    )
    list_filter = ("owner_type", "account_kind", "currency", "status", "created_at")
    search_fields = ("account_number", "name", "user__phone_number", "user__full_name", "organization__name")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = (
        "internal_reference",
        "account",
        "transaction_type",
        "direction",
        "amount_minor",
        "status",
        "transaction_receipt",
        "created_at",
    )
    list_filter = ("transaction_type", "direction", "status", "currency", "created_at")
    search_fields = ("internal_reference", "request_id", "transaction_receipt", "account__account_number", "account__name")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(BalanceLog)
class BalanceLogAdmin(admin.ModelAdmin):
    list_display = ("transaction", "balance_entry_type", "amount_transacted_minor", "total_balance_minor", "status", "created_at")
    list_filter = ("balance_entry_type", "status", "created_at")
    search_fields = ("transaction__internal_reference", "reference", "receipt")
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = (BalanceLogEntryInline,)


@admin.register(BalanceLogEntry)
class BalanceLogEntryAdmin(admin.ModelAdmin):
    list_display = (
        "balance_log",
        "entry_type",
        "account_field_type",
        "amount_transacted_minor",
        "balance_before_minor",
        "balance_after_minor",
        "created_at",
    )
    list_filter = ("entry_type", "account_field_type", "created_at")
    search_fields = ("balance_log__transaction__internal_reference",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(PaymentRequest)
class PaymentRequestAdmin(admin.ModelAdmin):
    list_display = ("originator_ref", "operation", "status", "sandbox", "request_id", "created_at")
    list_filter = ("operation", "status", "sandbox", "created_at")
    search_fields = ("originator_ref", "request_id", "transaction__internal_reference")
    readonly_fields = ("id", "created_at", "updated_at")
