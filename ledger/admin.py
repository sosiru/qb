from django.contrib import admin

from .models import LedgerEntry, LedgerEntryLog, WalletAccount


class LedgerEntryLogInline(admin.TabularInline):
    model = LedgerEntryLog
    extra = 0
    fields = ("sequence", "balance_field", "delta_minor", "balance_before_minor", "balance_after_minor", "reason")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(WalletAccount)
class WalletAccountAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "owner_type",
        "wallet_type",
        "currency",
        "available_balance_minor",
        "current_balance_minor",
        "reserved_balance_minor",
        "uncleared_balance_minor",
        "status",
        "updated_at",
    )
    list_filter = ("owner_type", "wallet_type", "currency", "status", "created_at")
    search_fields = ("id", "name", "user__phone_number", "user__full_name", "organization__name")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "account",
        "transaction_type",
        "amount_minor",
        "balance_before_minor",
        "balance_after_minor",
        "idempotency_key",
        "created_at",
    )
    list_filter = ("transaction_type", "status", "currency", "created_at")
    search_fields = ("id", "reference", "idempotency_key", "account__name")
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = (LedgerEntryLogInline,)


@admin.register(LedgerEntryLog)
class LedgerEntryLogAdmin(admin.ModelAdmin):
    list_display = (
        "entry",
        "account",
        "sequence",
        "balance_field",
        "delta_minor",
        "balance_before_minor",
        "balance_after_minor",
        "created_at",
    )
    list_filter = ("balance_field", "sequence", "created_at")
    search_fields = ("entry__reference", "account__name")
    readonly_fields = ("id", "created_at", "updated_at")
