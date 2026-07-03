import json
from urllib import error, request


class PesaWayAPIError(Exception):
    pass


class PesaWayAPIClient:
    def __init__(self, client_id, client_secret, base_url="https://api.sandbox.pesaway.com", timeout=30):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.access_token = self._authenticate()

    def _request(self, method, endpoint, payload=None, authenticated=True, retry=True):
        url = f"{self.base_url}{endpoint}"
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.access_token}"

        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")

        req = request.Request(url, data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            if exc.code == 401 and authenticated and retry:
                self.access_token = self._authenticate()
                return self._request(method, endpoint, payload=payload, authenticated=True, retry=False)
            raise PesaWayAPIError(raw or f"PesaWay HTTP error {exc.code}.") from exc
        except error.URLError as exc:
            raise PesaWayAPIError(str(exc.reason)) from exc

    def _authenticate(self):
        payload = {
            "consumer_key": self.client_id,
            "consumer_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        response = self._request("POST", "/api/v1/token/", payload=payload, authenticated=False)
        try:
            return response["data"]["token"]
        except (KeyError, TypeError) as exc:
            raise PesaWayAPIError("PesaWay token response did not include data.token.") from exc

    def get_account_balance(self):
        return self._request("GET", "/api/v1/account-balance/")

    def send_mobile_money(self, amount, currency, recipient_number, reference):
        payload = {
            "amount": amount,
            "currency": currency,
            "recipient_number": recipient_number,
            "reference": reference,
        }
        return self._request("POST", "/api/v1/mobile-money/send-payment/", payload=payload)

    def send_b2b_payment(self, external_reference, amount, account_number, channel, reason, results_url):
        payload = {
            "ExternalReference": external_reference,
            "Amount": amount,
            "AccountNumber": account_number,
            "Channel": channel,
            "Reason": reason,
            "ResultsUrl": results_url,
        }
        return self._request("POST", "/api/v1/mobile-money/send-payment/", payload=payload)

    def send_b2c_payment(self, external_reference, amount, phone_number, channel, reason, results_url):
        payload = {
            "ExternalReference": external_reference,
            "Amount": amount,
            "PhoneNumber": phone_number,
            "Channel": channel,
            "Reason": reason,
            "ResultsUrl": results_url,
        }
        return self._request("POST", "/api/v1/mobile-money/send-payment/", payload=payload)

    def receive_c2b_payment(self, external_reference, amount, phone_number, channel, reason, results_url):
        payload = {
            "ExternalReference": external_reference,
            "Amount": amount,
            "PhoneNumber": phone_number,
            "Channel": channel,
            "Reason": reason,
            "ResultsUrl": results_url,
        }
        return self._request("POST", "/api/v1/mobile-money/receive-payment/", payload=payload)

    def authorize_transaction(self, transaction_id, otp):
        payload = {"TransactionID": transaction_id, "OTP": otp}
        return self._request("POST", "/api/v1/mobile-money/authorize-transaction/", payload=payload)

    def send_bank_payment(self, external_reference, amount, account_number, channel, bank_code, currency, reason, results_url):
        payload = {
            "ExternalReference": external_reference,
            "Amount": amount,
            "AccountNumber": account_number,
            "Channel": channel,
            "BankCode": bank_code,
            "Currency": currency,
            "Reason": reason,
            "ResultsUrl": results_url,
        }
        return self._request("POST", "/api/v1/bank/send-payment/", payload=payload)

    def query_bank_transaction(self, transaction_reference):
        payload = {"TransactionReference": transaction_reference}
        return self._request("POST", "/api/v1/bank/transaction-query/", payload=payload)

    def query_mobile_money_transaction(self, transaction_reference):
        payload = {"TransactionReference": transaction_reference}
        return self._request("POST", "/api/v1/mobile-money/transaction-query/", payload=payload)

    def send_airtime(self, external_reference, amount, phone_number, reason, results_url):
        payload = {
            "ExternalReference": external_reference,
            "Amount": amount,
            "PhoneNumber": phone_number,
            "Reason": reason,
            "ResultsUrl": results_url,
        }
        return self._request("POST", "/api/v1/airtime/send-airtime/", payload=payload)
