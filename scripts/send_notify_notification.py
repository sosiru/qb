#!/usr/bin/env python3
"""Send a single SMS or email through the Notify API."""

import argparse
import os
import sys
import uuid

import requests


DEFAULT_NOTIFY_URL = "https://notify.lipasync.com/api/core/send-notification/"


def parse_args():
    parser = argparse.ArgumentParser(description="Send a notification through Notify.")
    parser.add_argument("recipient", help="Phone number or email address")
    parser.add_argument("message", help="Notification message")
    parser.add_argument(
        "--type",
        choices=("sms", "email"),
        default="sms",
        dest="notification_type",
        help="Notification channel (default: sms)",
    )
    parser.add_argument("--template", help="Notify template name")
    parser.add_argument("--identifier", default=None, help="Unique request identifier")
    parser.add_argument(
        "--url",
        default=os.environ.get("NOTIFY", DEFAULT_NOTIFY_URL),
        help="Notify endpoint (or set NOTIFY)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("NOTIFY_API_KEY") or os.environ.get("X-API-KEY", ""),
        help="Notify API key (or set NOTIFY_API_KEY)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("NOTIFY_TIMEOUT_SECONDS", "30")),
        help="Request timeout in seconds (default: 30)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.api_key:
        raise SystemExit("Notify API key is required. Set NOTIFY_API_KEY or pass --api-key.")

    template = args.template or os.environ.get(
        "NOTIFY_EMAIL_TEMPLATE" if args.notification_type == "email" else "NOTIFY_SMS_TEMPLATE",
        "email_default" if args.notification_type == "email" else "sms_default",
    )
    payload = {
        "notification_type": args.notification_type,
        "template": template,
        "unique_identifier": args.identifier or str(uuid.uuid4()),
        "recipients": [args.recipient],
        "context": {"message": args.message},
    }

    try:
        response = requests.post(
            args.url,
            headers={
                "X-API-KEY": args.api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=args.timeout,
        )
    except requests.RequestException as exc:
        print(f"Notify request failed: {exc}", file=sys.stderr)
        return 1

    print(response.status_code)
    print(response.text)
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
