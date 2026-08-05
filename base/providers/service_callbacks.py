import logging
import os
from datetime import datetime

import requests

lgr = logging.getLogger(__name__)
KPLC_REQUEST_BODY = "grant_type=client_credentials&scope=token_public accounts_public attributes_public customers_public documents_public listData_public rccs_public sectorSupplies_public selfReads_public serviceRequests_public services_public streets_public supplies_public users_public workRequests_public publicData_public juaforsure_public calculator_public sscalculator_public token_private accounts_private accounts_public attributes_public attributes_private customers_public customers_private documents_private documents_public listData_public rccs_private rccs_public sectorSupplies_private sectorSupplies_public selfReads_private selfReads_public serviceRequests_private serviceRequests_public services_private services_public streets_public supplies_private supplies_public users_private users_public workRequests_private workRequests_public notification_private outage_private juaforsure_private juaforsure_public prepayment_private pdfbill_private publicData_public selfReadsPeriod_private corporateAccount_private calculator_public sscalculator_public register_public ssaccounts_public addaccount_public summaryLetter_public whtcertificate_public selfService_public"
KPLC_BASIC_AUTH = "Basic aVBXZkZTZTI2NkF2eVZHc2xpWk45Nl8yTzVzYTp3R3lRZEFFa3MzRm9lSkZHU0ZZUndFMERUdGNh"
KPLC_TOKEN_URL= "https://selfservice.kplc.co.ke/api/token"
KPLC_LOCATION_URL= "https://selfservice.kplc.co.ke/api/sectorSupplies/4/?serialNumberMeter="
KPLC_METER_URL="https://selfservice.kplc.co.ke/api/publicData/"

class KPLCInterfa:
    def __init__(self):
        self.token_url =KPLC_TOKEN_URL
        self.basic_auth = KPLC_BASIC_AUTH
        self.request_body = KPLC_REQUEST_BODY
        self.base_url =KPLC_METER_URL

    def get_access_token(self):
        headers = {
            "Authorization": self.basic_auth,
            "Host": "selfservice.kplc.co.ke",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "*/*",
        }

        try:
            response = requests.post(
                self.token_url,
                data=self.request_body,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()

            token = response.json().get("access_token")
            if not token:
                raise Exception("No access token returned.")

            return token

        except Exception as e:
            lgr.exception("Failed to obtain access token: %s", e)
            raise

    def get_meter_data(self, serial_number):
        token = self.get_access_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0",
            "Host": "selfservice.kplc.co.ke",
        }

        params = {
            "serialNumberMeter": serial_number,
        }

        try:
            response = requests.get(
                self.base_url,
                params=params,
                headers=headers,
                timeout=120,
            )

            response.raise_for_status()
            return response.json()

        except requests.HTTPError:
            lgr.exception(
                "KPLC returned %s: %s",
                response.status_code,
                response.text,
            )
            raise

        except Exception as e:
            lgr.exception("Failed to retrieve meter data: %s", e)
            raise


def kplc_sms_message(data):
    transaction = data["data"]["colPrepayment"][-1]
    date = datetime.fromtimestamp(
        transaction["trnTimestamp"] / 1000
    ).strftime("%Y%m%d %H:%M")
    token = transaction["tokenNo"]
    token = "-".join(
        token[i:i + 4]
        for i in range(0, len(token), 4)
    )
    token_amount = next(
        (
            concept["amount"]
            for concept in transaction["concepts"]
            if concept["codConcept"] == "RESSTEP0"
        ),
        0,
    )
    other_charges = transaction["trnAmount"] - token_amount
    return (
        f"Mtr:{transaction['msno']}\n"
        f"Token:{token}\n"
        f"Date:{date}\n"
        f"Units:{transaction['trnUnits']}\n"
        f"Amt:{transaction['trnAmount']:.2f}\n"
        f"TknAmt:{token_amount:.2f}\n"
        f"OtherCharges:{other_charges:.2f}\n"
        f"For Details dial *977#"
    )


if __name__ == "__main__":
    client = KPLC()
    meter_number = "22213061744"
    try:
        result = client.get_meter_data(meter_number)
        sms = kplc_sms(result)
        print(sms)
    except Exception as exc:
        print(f"Error: {exc}")