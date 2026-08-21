#!/usr/bin/env python3

import os
import uuid
import requests


operation = "collection"
amount = "300.00"
phone_number = "254710956633"
channel = "MPESA"
reference = f"TEST-{uuid.uuid4().hex[:12].upper()}"

system_slug = os.getenv("PESAWAY_SYSTEM_SLUG", "qb")

config = {
    "collection": {
        "event_slug": os.getenv("PESAWAY_COLLECTION_EVENT_SLUG"),
        "path": "inbound-payments",
        "reason": "Wallet top-up",
    },
    "payout": {
        "event_slug": os.getenv("PESAWAY_B2C_EVENT_SLUG"),
        "path": "outbound-transfers",
        "reason": "Test payout",
    },
}

payment = config[operation]

url = "https://payments.lipasync.com/api/v1/core/outbound-transfers/qb/b2c/initiate/"

payload = {
    "amount": amount,
    "idempotency_key": reference,
    "external_reference": reference,
    "provider_payload": {
        "phone_number": phone_number,
        "channel": channel,
        "reason": payment["reason"],
    },
}

response = requests.post(
    url,
    headers={"X-API-KEY": "PsRdVR9pM4uZXGi-gSeTih7I7_vKwR1Mw_iN7exf2ko"},
    json=payload,
    timeout=30,
)

print(response.status_code)
print(response.text)