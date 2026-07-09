from django.db import models

from base.common import TimestampedModel
from base.utils import generate_uuid


ACTION_DESCRIPTIONS = {
    "integration_api_key.created": "Created an integration API key named {name} with scopes {scopes}.",
    "integration_api_key.revoked": "Revoked integration API key {target_id}.",
    "auth.register.succeeded": "Registered account for {phone_number} from {ip_address}.",
    "auth.register.failed": "Registration failed for {phone_number} from {ip_address}: {reason}.",
    "auth.login.otp_required": "Password accepted for {phone_number}; OTP challenge issued from {ip_address}.",
    "auth.login.succeeded": "Login completed for {phone_number} from {ip_address}.",
    "auth.login.failed": "Login failed for {phone_number} from {ip_address}: {reason}.",
    "user.profile_updated": "Updated user profile fields: {fields}.",
    "user.password_changed": "Changed the account password.",
    "organization.created": "Created organization {organization_name}.",
    "organization.updated": "Updated organization fields: {fields}.",
    "organization.member_upserted": "{member_action} organization member {member_name} as {role}.",
    "organization.member_invited": "Invited {email} to join the organization as {role}.",
    "organization.invite_accepted": "Accepted an organization invite and joined the organization.",
    "organization.member_updated": "Updated organization member fields: {fields}.",
    "organization.member_removed": "Removed or deactivated an organization member.",
    "payee.created": "Created payee {payee_label}.",
    "payee.updated": "Updated payee {payee_label} fields: {fields}.",
    "payee.deleted": "Deleted payee {payee_label}.",
    "schedule.created": "Created payment schedule for {payee_label} worth {amount_minor} minor units.",
    "schedule.updated": "Updated payment schedule fields: {fields}.",
    "schedule.deleted": "Deleted payment schedule for {payee_label}.",
    "wallet.withdrawal_requested": "Requested wallet withdrawal of {amount_minor} minor units to {phone_number}.",
    "individual_batch.pending_owner_approval": "Created individual payment batch requiring owner approval.",
    "quick_pay.pending_owner_approval": "Created quick pay batch requiring owner approval.",
    "batch.uploaded": "Uploaded payment batch {batch_description} with {instruction_count} instructions.",
    "batch.submitted": "Submitted payment batch for approval.",
    "batch.approved": "Approved payment batch and started settlement.",
    "batch.rejected": "Rejected payment batch because: {reason}.",
    "pesaway.callback.received": "Received Pesaway provider callback.",
    "http.request": "{method} {path} completed with HTTP {status_code}.",
}


def _value(metadata, key, fallback=""):
    value = (metadata or {}).get(key, fallback)
    if value is None or value == "":
        return fallback
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _description_context(action, target_id, metadata):
    metadata = metadata or {}
    context = {
        "target_id": target_id,
        "fields": _value(metadata, "fields", "none"),
        "scopes": _value(metadata, "scopes", "none"),
        "name": _value(metadata, "name", str(target_id)),
        "organization_name": _value(metadata, "organization_name", str(target_id)),
        "member_action": "Added" if metadata.get("created") else "Updated",
        "member_name": _value(metadata, "member_name", _value(metadata, "user_id", "member")),
        "role": _value(metadata, "role", "member"),
        "email": _value(metadata, "email", "the invitee"),
        "payee_label": _value(metadata, "payee_label", str(target_id)),
        "amount_minor": _value(metadata, "amount_minor", "the requested amount"),
        "phone_number": _value(metadata, "phone_number", "the selected phone number"),
        "ip_address": _value(metadata, "ip_address", "unknown IP"),
        "batch_description": _value(metadata, "batch_description", str(target_id)),
        "instruction_count": _value(metadata, "instruction_count", "the uploaded"),
        "reason": _value(metadata, "reason", "no reason provided"),
        "method": _value(metadata, "method", "Request"),
        "path": _value(metadata, "path", "unknown path"),
        "status_code": _value(metadata, "status_code", "unknown status"),
    }
    return context


def build_audit_description(action, target_type, target_id, metadata=None):
    template = ACTION_DESCRIPTIONS.get(action)
    if template:
        try:
            return template.format(**_description_context(action, target_id, metadata))
        except KeyError:
            pass
    readable_action = action.replace("_", " ").replace(".", " ")
    return f"Recorded {readable_action} on {target_type} {target_id}."


class AuditLog(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    actor = models.ForeignKey("eusers.User", on_delete=models.SET_NULL, null=True, related_name="audit_logs")
    action = models.CharField(max_length=64)
    description = models.TextField(blank=True)
    target_type = models.CharField(max_length=64)
    target_id = models.UUIDField()
    metadata = models.JSONField(default=dict, blank=True)

    def save(self, *args, **kwargs):
        if not self.description:
            self.description = build_audit_description(self.action, self.target_type, self.target_id, self.metadata)
        super().save(*args, **kwargs)
