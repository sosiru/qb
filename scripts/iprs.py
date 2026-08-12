import datetime
import json
import logging
import os
from base64 import b64encode
import requests
from requests.auth import HTTPBasicAuth



USERNAME = os.environ.get("IPRS_USERNAME", "YOUR_USERNAME")
PASSWORD = os.environ.get("IPRS_PASSWORD", "YOUR_PASSWORD")

IPRS_BASE_URL_2 = os.environ.get(
    "IPRS_BASE_URL_2",
    "YOUR_IPRS_BASE_URL_2"
)

IPRS_BASE_URL = os.environ.get(
    "IPRS_BASE_URL",
    "YOUR_IPRS_BASE_URL"
)

IPRS_ENCODED_PIN = os.environ.get(
    "IPRS_ENCODED_PIN",
    "YOUR_ENCODED_PIN"
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

lgr = logging.getLogger(__name__)


class Search:

    def __init__(self, username, password):
        self.base_url = IPRS_BASE_URL
        self.username = username
        self.password = password
        self.encoded_pin = IPRS_ENCODED_PIN

    @staticmethod
    def _basic_auth(username, password):
        token = b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")

        return f"Basic {token}"

    @staticmethod
    def transform_id_data_to_patient_json(response):
        data = response.get("data", {})

        now = datetime.datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        gender = (
            "Male"
            if data.get("sex", "").upper() == "M"
            else "Female"
        )

        return {
            "message": {
                "total": 1,
                "result": [
                    {
                        "resourceType": "Patient",
                        "id": f"CR{data.get('serial_number', '')}",
                        "meta": {
                            "versionId": "1",
                            "creationTime": now,
                            "lastUpdated": now,
                            "source": "http://cr.tiberbu.app",
                        },
                        "originSystem": {
                            "system": "EP-00025",
                            "record_id": "",
                        },
                        "first_name": data.get("first_name", ""),
                        "middle_name": data.get("other_names", ""),
                        "id_serial": data.get("serial_number", ""),
                        "last_name": data.get("last_name", ""),
                        "gender": gender,
                        "date_of_birth": data.get("date_of_birth", ""),
                        "place_of_birth": data.get("place_of_birth", ""),
                        "citizenship": data.get(
                            "nationality",
                            "Kenyan"
                        ).upper(),
                        "identification_type": "National ID",
                        "identification_number": data.get("id_number", ""),
                        "county": data.get("home_county", "").title(),
                        "sub_county": data.get("home_district", "").title(),
                        "ward": data.get("home_division", "").title(),
                        "village_estate": data.get("village", "").title(),
                    }
                ],
            }
        }

    def fetch_identity(self, identifier):
        url = IPRS_BASE_URL_2

        params = {
            "id_number": str(identifier).strip(),
            "type": "citizen",
        }

        print("\nCalling IPRS...")
        print(f"URL       : {url}")
        print(f"ID Number : {identifier}")

        response = requests.get(
            url=url,
            params=params,
            auth=HTTPBasicAuth(
                self.username,
                self.password
            ),
            timeout=300,
        )

        print(f"Status Code: {response.status_code}")

        response.raise_for_status()

        raw_response = response.json()

        print("\nRAW RESPONSE")
        print("=" * 80)

        print(
            json.dumps(
                raw_response,
                indent=4,
                default=str
            )
        )

        try:
            transformed = self.transform_id_data_to_patient_json(
                raw_response
            )

            result = transformed.get(
                "message",
                {}
            ).get("result")

            if not result:
                raise ValueError("No result found")

            identity = result[0]

            for key in (
                "resourceType",
                "meta",
                "originSystem"
            ):
                identity.pop(key, None)

            return identity

        except Exception as exc:
            print(f"\nTransformation failed: {exc}")
            print("Trying fallback endpoint...")

            return self.fetch_identity_fallback(identifier)

    def fetch_identity_fallback(self, identifier):

        payload = {
            "payload": {
                "identification_number": str(identifier).strip(),
                "identification_type": "National ID",
                "encoded_pin": self.encoded_pin,
            }
        }

        headers = {
            "Authorization": self._basic_auth(
                self.username,
                self.password
            ),
            "Content-Type": "application/json",
        }

        response = requests.post(
            url=self.base_url,
            json=payload,
            headers=headers,
            timeout=300,
            verify=True,
        )

        print(
            f"Fallback Status Code: "
            f"{response.status_code}"
        )

        response.raise_for_status()

        data = response.json()

        print("\nFALLBACK RESPONSE")
        print("=" * 80)

        print(
            json.dumps(
                data,
                indent=4,
                default=str
            )
        )

        result = data.get(
            "message",
            {}
        ).get("result")

        if not result:
            raise Exception(
                "No valid result returned from fallback endpoint"
            )

        identity = result[0]

        for key in (
            "resourceType",
            "meta",
            "originSystem"
        ):
            identity.pop(key, None)

        return identity


# ============================================================
# RUN LOCALLY
# ============================================================

if __name__ == "__main__":

    # Put the National ID you want to test here
    ID_NUMBER = "12345678"

    search = Search(
        username=USERNAME,
        password=PASSWORD
    )

    try:
        result = search.fetch_identity(ID_NUMBER)

        print("\n")
        print("=" * 80)
        print("FINAL RESULT")
        print("=" * 80)

        print(
            json.dumps(
                result,
                indent=4,
                default=str
            )
        )
    except requests.RequestException as exc:
        print("\nIPRS request failed:")
        print(exc)

    except Exception as exc:
        print("\nError:")
        print(exc)