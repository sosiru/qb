#!/usr/bin/env python3
"""Send a direct STK collection or mobile payout request to PesaWay."""

import argparse
import os
import sys
import uuid
from decimal import Decimal, InvalidOperation

import requests


def parse_args():
    parser = argparse.ArgumentParser(description="Test the payment microservice directly.")
    parser.add_argument("operation", choices=("collection", "payout"))
    parser.add_argument("amount", help="Major-unit amount, for example 10.00 for KES 10")
    parser.add_argument("phone_number", help="Recipient/customer phone number")
    parser.add_argument("--channel", choices=("MPESA", "Airtel"), default="MPESA")
    parser.add_argument("--reference", help="External reference and idempotency key")
    parser.add_argument("--timeout", type=int, default=30)
    return parser.parse_args()


def main():
    args = parse_args()
    base_url = os.environ.get("PAYMENT_MICROSERVICE_URL", "").rstrip("/")
    api_key = os.environ.get("PAYMENT_MICROSERVICE_API_KEY", "")
    system_slug = os.environ.get("PESAWAY_SYSTEM_SLUG", "qb")

    if not base_url or not api_key:
        raise SystemExit(
            "Set PAYMENT_MICROSERVICE_URL and PAYMENT_MICROSERVICE_API_KEY first."
        )

    try:
        amount = Decimal(args.amount).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise SystemExit("amount must be a number such as 10.00") from exc
    if not amount.is_finite() or amount <= 0:
        raise SystemExit("amount must be greater than zero")

    reference = args.reference or f"TEST-{uuid.uuid4().hex[:12].upper()}"
    if args.operation == "collection":
        event_slug = os.environ.get("PESAWAY_COLLECTION_EVENT_SLUG", "")
        path = f"/inbound-payments/{system_slug}/{event_slug}/initiate/"
        reason = "Wallet top-up"
    else:
        event_slug = os.environ.get("PESAWAY_B2C_EVENT_SLUG", "")
        path = f"/outbound-transfers/{system_slug}/{event_slug}/initiate/"
        reason = "Test payout"

    if not event_slug:
        raise SystemExit(f"The event slug for {args.operation} is not configured.")

    response = requests.post(
        f"{base_url}{path}",
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        },
        json={
            "amount": f"{amount:.2f}",
            "idempotency_key": reference,
            "external_reference": reference,
            "provider_payload": {
                "phone_number": args.phone_number,
                "channel": args.channel,
                "reason": reason,
            },
        },
        timeout=args.timeout,
    )

    print(response.status_code)
    print(response.text)
    return 0 if response.ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.RequestException as exc:
        print(f"Payment request failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
