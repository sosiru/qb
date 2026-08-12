#!/usr/bin/env python3

import os
import sys
import uuid
import requests

NOTIFY_URL = "https://notify.lipasync.com/api/core/send-notification/"

recipient = "stephenosiru@gmail.com"
message = "test"
notification_type = "email"

api_key = "QWIRa-lvoNzOZwyW55hHNMsYclxIYBEP2XOaNjDeY6GnLNeWySks_g"

payload = {
    "notification_type": notification_type,
    "template": f"{notification_type}_default",
    "unique_identifier": str(uuid.uuid4()),
    "recipients": [recipient],
    "context": {"message": message},
}

response = requests.post(
    NOTIFY_URL,
    headers={"X-API-KEY": api_key},
    json=payload,
    timeout=30,
)

print(response.status_code)
print(response.text)