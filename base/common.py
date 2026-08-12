from decimal import Decimal

from django.db import models

NOTIFICATION_CHANNEL_CHOICES = [
    ("SMS", "SMS"),
    ("EMAIL", "Email"),
    ("IN_APP", "In App"),
]

NOTIFICATION_EVENT_TYPE_CHOICES = [
    ("T_MINUS_3", "T Minus 3"),
    ("DUE_TODAY", "Due Today"),
    ("PAYMENT_SUCCESS", "Payment Success"),
    ("PAYMENT_FAILURE", "Payment Failure"),
    ("APPROVAL_REQUEST", "Approval Request"),
    ("BATCH_APPROVED", "Batch Approved"),
    ("BATCH_REJECTED", "Batch Rejected"),
    ("SELF_ONBOARDING", "Self Onboarding"),
    ("LOGIN_OTP", "Login OTP"),
    ("PASSWORD_RESET", "Password Reset"),
    ("LOGIN_SUCCESS", "Login Success"),
    ("ORGANIZATION_INVITE", "Organization Invite"),
    ("WALLET_TOPUP_REQUESTED", "Wallet Topup Requested"),
    ("WALLET_TOPUP_COMPLETED", "Wallet Topup Completed"),
    ("WALLET_WITHDRAWAL_REQUESTED", "Wallet Withdrawal Requested"),
    ("WALLET_LOW", "Wallet Low"),
    ("OVERDUE_PAYMENT", "Overdue Payment"),
    ("PRODUCT_UPDATE", "Product Update"),
]


def json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    return value


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
