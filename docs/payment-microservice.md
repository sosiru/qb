# Payment Microservice Sample Requests and Responses

This document describes the payment microservice contract used by QuickBills.

QuickBills calls the configured `PAYMENT_MICROSERVICE_URL` with:

- `POST /transactions/initiate/`
- `POST /transactions/status/`

The microservice calls QuickBills back with:

- `POST /api/v1/payments/webhook/`

## Environment

```env
PAYMENT_MICROSERVICE_URL=http://localhost:8001
PAYMENT_MICROSERVICE_API_KEY=dummy-payment-api-key
PAYMENT_MICROSERVICE_TIMEOUT_SECONDS=8
PAYMENT_MICROSERVICE_INLINE_DISPATCH=0
PAYMENT_CALLBACK_URL=https://qb.lipasync.com/api/v1/payments/webhook/
PESAWAY_WEBHOOK_SECRET=replace-with-the-secret-from-pesaway
```

QuickBills sends the API key as a Bearer token:

```http
Authorization: Bearer dummy-payment-api-key
Content-Type: application/json
```

## Operations

Supported `operation` values:

- `STK_PUSH`: collect funds from a customer by phone prompt.
- `PAY_IN`: credit a wallet or account.
- `PAYOUT`: send funds to a recipient.

Amounts are integer minor units. For KES, `100000` means KES 1,000.00.

## Initiate STK Push

QuickBills sends this when collecting money from a user, for example wallet top-up or STK-funded batch payment.

### Request

```bash
curl -X POST "$PAYMENT_MICROSERVICE_URL/transactions/initiate/" \
  -H "Authorization: Bearer $PAYMENT_MICROSERVICE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "originator_ref": "STK-20260712-000001",
    "amount_minor": 250000,
    "currency": "KES",
    "operation": "STK_PUSH",
    "phone_number": "254700900001"
  }'
```

### Success Response

```json
{
  "success": true,
  "originator_ref": "STK-20260712-000001",
  "request_id": "MS-STK-9F2A18C7",
  "message": "STK push request accepted",
  "provider": "payment_microservice",
  "status": "PROCESSING"
}
```

QuickBills stores `request_id` and keeps the related transaction or batch in `PROCESSING` until final confirmation. Live payout initiation responses never complete a payout. After 120 seconds, QuickBills queries the outbound transfer status; a terminal `SUCCESS` or `FAILED` result is applied just like a callback. Pending results remain open, and requests without a final callback or status fail after 300 seconds.

## Initiate Payout

QuickBills sends this when dispatching a payment instruction to a mobile wallet, till, paybill, or bank destination.

### Request

```bash
curl -X POST "$PAYMENT_MICROSERVICE_URL/transactions/initiate/" \
  -H "Authorization: Bearer $PAYMENT_MICROSERVICE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "originator_ref": "REQ-20260712-000042",
    "amount_minor": 150000,
    "currency": "KES",
    "operation": "PAYOUT",
    "instruction_id": "5a081d05-052f-43e0-870f-804a4119e0f4",
    "batch_id": "9e313e55-b6a4-4417-89c8-b03bb9c0dd7c",
    "recipient_name": "Recipient User",
    "recipient_type": "MOBILE",
    "destination": {
      "phone_number": "254711222333"
    }
  }'
```

### Success Response

```json
{
  "success": true,
  "originator_ref": "REQ-20260712-000042",
  "request_id": "MS-PAYOUT-31A8F604",
  "message": "Payout request accepted",
  "provider": "payment_microservice",
  "status": "PROCESSING"
}
```

## Destination Examples

### Mobile Money

```json
{
  "recipient_type": "MOBILE",
  "destination": {
    "phone_number": "254711222333"
  }
}
```

### Paybill

```json
{
  "recipient_type": "PAYBILL",
  "destination": {
    "paybill_number": "400200",
    "account_number": "INV-1001"
  }
}
```

### Till

```json
{
  "recipient_type": "TILL",
  "destination": {
    "till_number": "123456"
  }
}
```

### Bank

```json
{
  "recipient_type": "BANK",
  "destination": {
    "bank_code": "001",
    "bank_name": "Sample Bank",
    "account_number": "1234567890"
  }
}
```

## Query Transaction Status

QuickBills calls a supported status endpoint after a request has remained in `PROCESSING` for 120 seconds. Requests that are still not final after 300 seconds are failed locally.

For PesaWay Core outbound transfers, QuickBills uses the `outbound_transfer_id` returned during initiation:

```text
GET /api/v1/core/outbound-transfers/<OUTBOUND_TRANSFER_ID>/status/
```

`SUCCESS` completes the payout, `FAILED` fails it, and `QUEUED`, `PROCESSING`, or `PENDING` leave it open.

### Request

```bash
curl -X POST "$PAYMENT_MICROSERVICE_URL/transactions/status/" \
  -H "Authorization: Bearer $PAYMENT_MICROSERVICE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "originator_ref": "REQ-20260712-000042",
    "request_id": "MS-PAYOUT-31A8F604"
  }'
```

### Processing Response

```json
{
  "originator_ref": "REQ-20260712-000042",
  "request_id": "MS-PAYOUT-31A8F604",
  "status": "PROCESSING",
  "message": "Transaction is still being processed"
}
```

If the response does not contain `success`, QuickBills treats it as informational and keeps the local payment request open until the 5-minute processing timeout is reached.

### Completed Response

```json
{
  "success": true,
  "originator_ref": "REQ-20260712-000042",
  "request_id": "MS-PAYOUT-31A8F604",
  "transaction_receipt": "RCP-20260712-778812",
  "confirmation_key": "1b351042d92423ff58cbf330",
  "status": "COMPLETED",
  "message": "Payment completed"
}
```

### Failed Response

```json
{
  "success": false,
  "originator_ref": "REQ-20260712-000042",
  "request_id": "MS-PAYOUT-31A8F604",
  "status": "FAILED",
  "failure_reason": "Recipient account could not be credited",
  "error": "Recipient account could not be credited"
}
```

Failure responses should include at least one human-readable reason field. QuickBills checks `failure_reason`, then `message`, then `error`.

## Processing Timeout

QuickBills queries supported status endpoints after 120 seconds and fails any payment request that remains in `PROCESSING` for more than 300 seconds.

Run the reconciliation command periodically, for example every minute:

```bash
python manage.py reconcile_processing_payments
```

The local timeout reason is:

```text
Payment request timed out after 300 seconds without a final callback or status response.
```

If the status check itself fails after the timeout, the local reason is:

```text
Payment status check failed after 300 seconds: <microservice error>
```

Timeout failure reasons are written to:

- `ledger.PaymentRequest.last_error`
- `ledger.Transaction.failure_reason`
- `base.PaymentInstruction.failure_reason`, for payout instruction requests
- the batch failure metadata/event trail, for batch collection requests

## Callback to QuickBills

The microservice should send a callback when the provider reaches a final state.
Configure the callback URL in PesaWay. It must be publicly reachable over HTTPS;
a localhost URL cannot receive callbacks from a remote payment service.

Callback URL:

```text
POST https://quickbills.example.com/api/v1/payments/webhook/
```

At least one of `originator_ref` or `request_id` is required. `originator_ref` is preferred because QuickBills creates it before dispatching the request.

### Successful Callback

```bash
curl -X POST "https://quickbills.example.com/api/v1/payments/webhook/" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: <lowercase-hmac-sha256-of-raw-body>" \
  -d '{
    "success": true,
    "originator_ref": "REQ-20260712-000042",
    "request_id": "MS-PAYOUT-31A8F604",
    "transaction_receipt": "RCP-20260712-778812",
    "confirmation_key": "1b351042d92423ff58cbf330",
    "status": "COMPLETED",
    "message": "Payment completed"
  }'
```

### QuickBills Response

```json
{
  "status": "COMPLETED",
  "originator_ref": "REQ-20260712-000042",
  "transaction_id": "4ad4ef47-90d6-4a58-a945-938918c5d6a4"
}
```

### Failed Callback

```bash
curl -X POST "https://quickbills.example.com/api/v1/payments/webhook/" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: <lowercase-hmac-sha256-of-raw-body>" \
  -d '{
    "success": false,
    "originator_ref": "REQ-20260712-000042",
    "request_id": "MS-PAYOUT-31A8F604",
    "status": "FAILED",
    "failure_reason": "Insufficient provider float",
    "error": "Insufficient provider float"
  }'
```

### QuickBills Response

```json
{
  "status": "FAILED",
  "originator_ref": "REQ-20260712-000042",
  "transaction_id": "4ad4ef47-90d6-4a58-a945-938918c5d6a4"
}
```

## Error Responses from the Microservice

For validation or authorization errors, return a non-2xx HTTP status with JSON.

### Missing Authorization

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json
```

```json
{
  "error": "Missing or invalid API key"
}
```

### Invalid Payload

```http
HTTP/1.1 400 Bad Request
Content-Type: application/json
```

```json
{
  "error": "amount_minor must be greater than zero"
}
```

QuickBills records non-2xx responses as dispatch failures, including the response body in the local error message.

## Field Reference

| Field | Direction | Required | Notes |
| --- | --- | --- | --- |
| `originator_ref` | QuickBills to microservice | Yes | Unique QuickBills request reference. Echo it in every response and callback. |
| `request_id` | Microservice to QuickBills | Recommended | Microservice/provider tracking ID. |
| `amount_minor` | QuickBills to microservice | Yes | Integer amount in minor units. |
| `currency` | QuickBills to microservice | Yes | Usually `KES`. |
| `operation` | QuickBills to microservice | Yes | `STK_PUSH`, `PAY_IN`, or `PAYOUT`. |
| `phone_number` | QuickBills to microservice | Required for `STK_PUSH` | Normalized international format, for example `254700900001`. |
| `destination` | QuickBills to microservice | Required for `PAYOUT` | Recipient-specific routing details. |
| `success` | Microservice to QuickBills | Required for final state | `true` completes the transaction; `false` fails it. Omit while still processing. |
| `transaction_receipt` | Microservice to QuickBills | Recommended on success | Used as the visible microservice reference. |
| `confirmation_key` | Microservice to QuickBills | Optional | Stored on completed ledger transactions. |
| `message` | Microservice to QuickBills | Optional | Human-readable status detail. |
| `failure_reason` | Microservice to QuickBills | Recommended on failure | Preferred human-readable failure reason. |
| `error` | Microservice to QuickBills | Recommended on failure | Fallback human-readable failure reason. |

## Implementation Notes

- Return quickly from `/transactions/initiate/`; final payout settlement may arrive via webhook or the outbound status endpoint.
- Echo `originator_ref` in every response.
- A final payout callback must include `success`; status responses must include a terminal `data.status` before QuickBills applies them.
- Use `success: true` for completed payments and `success: false` for failed payments.
- Do not return `success: false` for pending transactions; omit `success` and return `status: "PROCESSING"` instead.
- Send a final callback for every payout. QuickBills starts status reconciliation after 120 seconds and fails requests that have no final result after 300 seconds.
