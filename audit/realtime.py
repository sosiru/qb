import hashlib
import json
from urllib.parse import parse_qs

from asgiref.sync import async_to_sync
from channels.generic.websocket import WebsocketConsumer
from channels.layers import get_channel_layer
from django.utils import timezone

from eusers.models import AccessToken, User


def serialize_audit_log(log):
    actor = log.actor
    return {
        "id": str(log.id),
        "action": log.action,
        "description": log.description,
        "target_type": log.target_type,
        "target_id": str(log.target_id),
        "metadata": log.metadata,
        "actor": {
            "id": str(actor.id),
            "name": actor.full_name,
            "phone_number": actor.phone_number,
            "account_type": actor.account_type,
        }
        if actor
        else None,
        "created_at": log.created_at.isoformat(),
    }


def publish_audit_log(log):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    message = {"type": "audit.log", "event": serialize_audit_log(log)}
    async_to_sync(channel_layer.group_send)("audit.global", message)
    if log.actor_id:
        async_to_sync(channel_layer.group_send)(f"audit.user.{log.actor_id}", message)


class AuditFeedConsumer(WebsocketConsumer):
    def connect(self):
        self.user = self._authenticate()
        if not self.user:
            self.close(code=4401)
            return

        self.groups_to_join = [f"audit.user.{self.user.id}"]
        if self.user.account_type in {User.AccountType.SUPERADMIN, User.AccountType.SERVICE_PROVIDER}:
            self.groups_to_join.append("audit.global")

        for group_name in self.groups_to_join:
            async_to_sync(self.channel_layer.group_add)(group_name, self.channel_name)
        self.accept()
        self.send_json(
            {
                "type": "connection.ready",
                "scope": "global" if "audit.global" in self.groups_to_join else "user",
                "connected_at": timezone.now().isoformat(),
            }
        )
        self.send_json({"type": "audit.snapshot", "events": self._recent_events()})

    def disconnect(self, close_code):
        for group_name in getattr(self, "groups_to_join", []):
            async_to_sync(self.channel_layer.group_discard)(group_name, self.channel_name)

    def audit_log(self, event):
        self.send_json({"type": "audit.log", "event": event["event"]})

    def send_json(self, payload):
        self.send(text_data=json.dumps(payload))

    def _authenticate(self):
        query = parse_qs(self.scope.get("query_string", b"").decode("utf-8"))
        raw_token = (query.get("token") or [""])[0].strip()
        if not raw_token:
            return None
        digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        token = AccessToken.objects.select_related("user").filter(token_hash=digest, revoked_at__isnull=True).first()
        if not token or not token.is_active():
            return None
        return token.user

    def _recent_events(self):
        from .models import AuditLog

        queryset = AuditLog.objects.select_related("actor").order_by("-created_at")
        if self.user.account_type not in {User.AccountType.SUPERADMIN, User.AccountType.SERVICE_PROVIDER}:
            queryset = queryset.filter(actor=self.user)
        return [serialize_audit_log(log) for log in reversed(list(queryset[:25]))]
