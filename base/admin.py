from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from api.models import IntegrationApiKey
from audit.models import AuditLog
from eusers.models import AccessToken, User
from notifications.models import NotificationEvent, NotificationTemplate
from reports.models import ReportExport

from .models import Organization, OrganizationMembership, OutboxEvent, Payee, PaymentBatch, PaymentInstruction, PaymentSchedule, Wallet, WalletLedgerEntry


class TimestampedAdminMixin:
    readonly_fields = ("id", "created_at", "updated_at")


class OrganizationMembershipInline(admin.TabularInline):
    model = OrganizationMembership
    extra = 0
    autocomplete_fields = ("user",)


class WalletInline(admin.TabularInline):
    model = Wallet
    extra = 0
    fields = ("wallet_type", "currency", "available_balance_minor", "organization")
    readonly_fields = ("available_balance_minor",)


class AccessTokenInline(admin.TabularInline):
    model = AccessToken
    extra = 0
    fields = ("prefix", "expires_at", "last_used_at", "revoked_at")
    readonly_fields = ("prefix", "expires_at", "last_used_at", "revoked_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class UserIntegrationApiKeyInline(admin.TabularInline):
    model = IntegrationApiKey
    fk_name = "user"
    extra = 0
    fields = ("name", "key_prefix", "organization", "is_active", "last_used_at", "expires_at", "revoked_at")
    readonly_fields = ("key_prefix", "last_used_at", "expires_at", "revoked_at")
    autocomplete_fields = ("organization",)
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class OrganizationIntegrationApiKeyInline(admin.TabularInline):
    model = IntegrationApiKey
    fk_name = "organization"
    extra = 0
    fields = ("name", "key_prefix", "user", "is_active", "last_used_at", "expires_at", "revoked_at")
    readonly_fields = ("key_prefix", "last_used_at", "expires_at", "revoked_at")
    autocomplete_fields = ("user",)
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class PaymentScheduleInline(admin.TabularInline):
    model = PaymentSchedule
    extra = 0
    fields = ("amount_minor", "day_of_month", "active", "created_at")
    readonly_fields = ("created_at",)


class WalletLedgerEntryInline(admin.TabularInline):
    model = WalletLedgerEntry
    extra = 0
    fields = ("entry_type", "amount_minor", "balance_after_minor", "reference", "created_at")
    readonly_fields = ("entry_type", "amount_minor", "balance_after_minor", "reference", "created_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class PaymentInstructionInline(admin.TabularInline):
    model = PaymentInstruction
    extra = 0
    fields = (
        "recipient_name",
        "recipient_type",
        "amount_minor",
        "category",
        "status",
        "external_reference",
        "provider_reference",
    )
    readonly_fields = (
        "recipient_name",
        "recipient_type",
        "amount_minor",
        "category",
        "status",
        "external_reference",
        "provider_reference",
    )
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class NotificationEventInline(admin.TabularInline):
    model = NotificationEvent
    extra = 0
    fields = ("event_type", "channel", "status", "scheduled_for", "sent_at", "attempts")
    readonly_fields = ("event_type", "channel", "status", "scheduled_for", "sent_at", "attempts")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(User)
class UserAdmin(TimestampedAdminMixin, BaseUserAdmin):
    ordering = ("-date_joined",)
    list_display = (
        "phone_number",
        "full_name",
        "account_type",
        "email",
        "is_active",
        "is_staff",
        "is_superuser",
        "mfa_enabled",
        "primary_wallet_balance",
        "date_joined",
    )
    list_filter = (
        "account_type",
        "is_active",
        "is_staff",
        "is_superuser",
        "mfa_enabled",
        "is_phone_verified",
        "sms_notifications_enabled",
        "email_notifications_enabled",
        "date_joined",
    )
    search_fields = ("id", "phone_number", "full_name", "email")
    inlines = (OrganizationMembershipInline, WalletInline, AccessTokenInline, UserIntegrationApiKeyInline, NotificationEventInline)
    fieldsets = (
        (
            "Identity",
            {
                "fields": ("id", "phone_number", "password", "full_name", "email"),
            },
        ),
        (
            "Account",
            {
                "fields": (
                    "account_type",
                    "default_payment_mode",
                    "is_phone_verified",
                    "mfa_enabled",
                    "sms_notifications_enabled",
                    "email_notifications_enabled",
                    "push_notifications_enabled",
                ),
            },
        ),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ),
            },
        ),
        (
            "Dates",
            {
                "fields": ("last_login", "date_joined", "created_at", "updated_at"),
            },
        ),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("phone_number", "full_name", "email", "account_type", "password1", "password2"),
            },
        ),
    )

    @admin.display(description="Primary Balance")
    def primary_wallet_balance(self, obj):
        wallet = obj.wallets.filter(wallet_type=Wallet.WalletType.PRIMARY).first()
        return wallet.available_balance_minor if wallet else 0


@admin.register(AccessToken)
class AccessTokenAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("prefix", "user", "is_currently_active", "expires_at", "last_used_at", "revoked_at", "created_at")
    list_filter = ("expires_at", "revoked_at", "created_at")
    search_fields = ("prefix", "user__phone_number", "user__full_name", "user__email")
    autocomplete_fields = ("user",)

    @admin.display(boolean=True, description="Active")
    def is_currently_active(self, obj):
        return obj.is_active()


@admin.register(Organization)
class OrganizationAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "kyc_status",
        "default_currency",
        "member_count",
        "wallet_count",
        "sms_notifications_enabled",
        "created_at",
    )
    list_filter = ("kyc_status", "default_currency", "sms_notifications_enabled", "push_notifications_enabled", "created_at")
    search_fields = ("id", "name", "slug")
    inlines = (OrganizationMembershipInline, WalletInline, OrganizationIntegrationApiKeyInline)

    @admin.display(description="Members")
    def member_count(self, obj):
        return obj.memberships.count()

    @admin.display(description="Wallets")
    def wallet_count(self, obj):
        return obj.wallets.count()


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("organization", "user", "role", "is_active", "created_at")
    list_filter = ("role", "is_active", "created_at")
    search_fields = (
        "organization__name",
        "organization__slug",
        "user__phone_number",
        "user__full_name",
        "user__email",
    )
    autocomplete_fields = ("organization", "user")


@admin.register(Wallet)
class WalletAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "owner_display",
        "owner_type",
        "wallet_type",
        "currency",
        "available_balance_minor",
        "created_at",
    )
    list_filter = ("owner_type", "wallet_type", "currency", "created_at")
    search_fields = (
        "id",
        "user__phone_number",
        "user__full_name",
        "organization__name",
        "organization__slug",
    )
    autocomplete_fields = ("user", "organization")
    inlines = (WalletLedgerEntryInline,)

    @admin.display(description="Owner")
    def owner_display(self, obj):
        if obj.user_id:
            return f"{obj.user.full_name} ({obj.user.phone_number})"
        if obj.organization_id:
            return obj.organization.name
        return "-"


@admin.register(WalletLedgerEntry)
class WalletLedgerEntryAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("wallet", "entry_type", "amount_minor", "balance_after_minor", "reference", "created_at")
    list_filter = ("entry_type", "created_at", "wallet__wallet_type", "wallet__owner_type")
    search_fields = (
        "reference",
        "wallet__id",
        "wallet__user__phone_number",
        "wallet__user__full_name",
        "wallet__organization__name",
    )
    autocomplete_fields = ("wallet",)


@admin.register(Payee)
class PayeeAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = (
        "label",
        "payee_type",
        "owner_scope",
        "expense_category",
        "active",
        "schedule_count",
        "created_at",
    )
    list_filter = ("payee_type", "active", "expense_category", "created_at")
    search_fields = (
        "id",
        "label",
        "account_reference",
        "phone_number",
        "paybill_number",
        "till_number",
        "bank_name",
        "bank_code",
        "account_number",
        "user__phone_number",
        "user__full_name",
        "organization__name",
    )
    autocomplete_fields = ("user", "organization")
    inlines = (PaymentScheduleInline,)

    @admin.display(description="Scope")
    def owner_scope(self, obj):
        if obj.organization_id:
            return f"Org: {obj.organization.name}"
        if obj.user_id:
            return f"User: {obj.user.full_name}"
        return "-"

    @admin.display(description="Schedules")
    def schedule_count(self, obj):
        return obj.schedules.count()


@admin.register(PaymentSchedule)
class PaymentScheduleAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("payee", "amount_minor", "day_of_month", "active", "owner_scope", "created_at")
    list_filter = ("active", "day_of_month", "payee__payee_type", "created_at")
    search_fields = (
        "id",
        "payee__label",
        "payee__account_reference",
        "payee__phone_number",
        "payee__user__phone_number",
        "payee__organization__name",
    )
    autocomplete_fields = ("payee",)

    @admin.display(description="Scope")
    def owner_scope(self, obj):
        if obj.payee.organization_id:
            return obj.payee.organization.name
        if obj.payee.user_id:
            return obj.payee.user.full_name
        return "-"


@admin.register(PaymentBatch)
class PaymentBatchAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "batch_kind",
        "status",
        "payment_mode",
        "owner_display",
        "total_amount_minor",
        "fee_amount_minor",
        "instruction_count",
        "scheduled_for",
        "processed_at",
    )
    list_filter = ("batch_kind", "status", "payment_mode", "scheduled_for", "created_at", "processed_at")
    search_fields = (
        "id",
        "description",
        "source_file_name",
        "user__phone_number",
        "user__full_name",
        "organization__name",
        "submitted_by__full_name",
        "approved_by__full_name",
    )
    autocomplete_fields = ("user", "organization", "submitted_by", "approved_by")
    readonly_fields = TimestampedAdminMixin.readonly_fields + ("processed_at", "submitted_at", "approved_at")
    inlines = (PaymentInstructionInline,)

    @admin.display(description="Owner")
    def owner_display(self, obj):
        if obj.organization_id:
            return obj.organization.name
        if obj.user_id:
            return obj.user.full_name
        return "-"

    @admin.display(description="Instructions")
    def instruction_count(self, obj):
        return obj.instructions.count()


@admin.register(PaymentInstruction)
class PaymentInstructionAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = (
        "recipient_name",
        "batch",
        "recipient_type",
        "amount_minor",
        "category",
        "status",
        "external_reference",
        "provider_reference",
        "created_at",
    )
    list_filter = ("recipient_type", "status", "category", "created_at")
    search_fields = (
        "id",
        "recipient_name",
        "external_reference",
        "provider_reference",
        "failure_reason",
        "batch__id",
        "payee__label",
    )
    autocomplete_fields = ("batch", "payee")


@admin.register(NotificationTemplate)
class NotificationTemplateAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "event_type", "channel", "system", "provider_template", "active", "created_at")
    list_filter = ("event_type", "channel", "active", "created_at")
    search_fields = ("code", "provider_template", "system", "description", "subject_template")
    inlines = (NotificationEventInline,)


@admin.register(IntegrationApiKey)
class IntegrationApiKeyAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "key_prefix",
        "user",
        "organization",
        "is_active",
        "last_used_at",
        "expires_at",
        "revoked_at",
        "created_at",
    )
    list_filter = ("is_active", "organization", "expires_at", "revoked_at", "created_at")
    search_fields = (
        "id",
        "name",
        "key_prefix",
        "user__phone_number",
        "user__full_name",
        "user__email",
        "organization__name",
        "created_by__full_name",
    )
    autocomplete_fields = ("user", "organization", "created_by")


@admin.register(NotificationEvent)
class NotificationEventAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = (
        "unique_identifier",
        "event_type",
        "channel",
        "status",
        "user",
        "template",
        "recipient_summary",
        "scheduled_for",
        "sent_at",
        "attempts",
    )
    list_filter = ("event_type", "channel", "status", "scheduled_for", "sent_at", "created_at")
    search_fields = (
        "unique_identifier",
        "user__phone_number",
        "user__email",
        "user__full_name",
        "template__code",
        "template__provider_template",
        "last_error",
    )
    autocomplete_fields = ("user", "template")
    readonly_fields = TimestampedAdminMixin.readonly_fields + ("provider_response", "recipients", "context", "sent_at")

    @admin.display(description="Recipients")
    def recipient_summary(self, obj):
        return ", ".join(obj.recipients[:3])


@admin.register(OutboxEvent)
class OutboxEventAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("topic", "aggregate_type", "aggregate_id", "status", "attempts", "available_at", "created_at")
    list_filter = ("status", "topic", "aggregate_type", "available_at", "created_at")
    search_fields = ("topic", "aggregate_type", "aggregate_id", "last_error")
    readonly_fields = TimestampedAdminMixin.readonly_fields + ("payload",)


@admin.register(AuditLog)
class AuditLogAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("action", "actor", "target_type", "target_id", "created_at")
    list_filter = ("target_type", "action", "created_at")
    search_fields = ("action", "target_type", "target_id", "actor__phone_number", "actor__full_name", "actor__email")
    autocomplete_fields = ("actor",)
    readonly_fields = TimestampedAdminMixin.readonly_fields + ("metadata",)


@admin.register(ReportExport)
class ReportExportAdmin(TimestampedAdminMixin, admin.ModelAdmin):
    list_display = ("export_type", "file_format", "status", "requested_by", "organization", "file_name", "generated_at")
    list_filter = ("export_type", "file_format", "status", "generated_at", "created_at")
    search_fields = ("file_name", "requested_by__phone_number", "requested_by__full_name", "organization__name")
    autocomplete_fields = ("requested_by", "organization")


admin.site.site_header = "Route Administration"
admin.site.site_title = "Route Admin"
admin.site.index_title = "Operations Console"
