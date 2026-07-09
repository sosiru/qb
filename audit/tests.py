from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.test import Client, TransactionTestCase

from eusers.models import AccessToken, User

from .models import AuditLog


class AuditRealtimeTests(TransactionTestCase):
    def test_authenticated_user_receives_own_audit_events(self):
        user = User.objects.create_user(
            phone_number="254700090001",
            password="StrongPass123!",
            full_name="Realtime User",
            account_type=User.AccountType.INDIVIDUAL,
        )
        AccessToken.issue(user)
        channel_layer = get_channel_layer()
        channel_name = async_to_sync(channel_layer.new_channel)()
        async_to_sync(channel_layer.group_add)(f"audit.user.{user.id}", channel_name)

        AuditLog.objects.create(
            actor=user,
            action="user.profile_updated",
            target_type="user",
            target_id=user.id,
            metadata={"fields": ["full_name"]},
        )

        message = async_to_sync(channel_layer.receive)(channel_name)
        self.assertEqual(message["type"], "audit.log")
        self.assertEqual(message["event"]["description"], "Updated user profile fields: full_name.")
        self.assertEqual(message["event"]["actor"]["phone_number"], "254700090001")
        async_to_sync(channel_layer.group_discard)(f"audit.user.{user.id}", channel_name)

    def test_login_audit_records_ip_and_user_agent(self):
        client = Client()
        User.objects.create_user(
            phone_number="254700090002",
            password="StrongPass123!",
            full_name="Login Audit User",
            account_type=User.AccountType.INDIVIDUAL,
        )

        response = client.post(
            "/api/v1/auth/login/",
            data='{"phone_number":"254700090002","password":"StrongPass123!"}',
            content_type="application/json",
            REMOTE_ADDR="10.20.30.40",
            HTTP_USER_AGENT="AuditTest/1.0",
        )

        self.assertEqual(response.status_code, 202)
        log = AuditLog.objects.get(action="auth.login.otp_required")
        self.assertEqual(log.metadata["phone_number"], "254700090002")
        self.assertEqual(log.metadata["ip_address"], "10.20.30.40")
        self.assertEqual(log.metadata["user_agent"], "AuditTest/1.0")
        self.assertEqual(log.description, "Password accepted for 254700090002; OTP challenge issued from 10.20.30.40.")
