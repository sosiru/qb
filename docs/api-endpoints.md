# API Endpoints

Base URL: `http://127.0.0.1:8000/api/v1`

Auth uses `Authorization: Bearer <token>`.

For machine-to-machine integrations, you can also authenticate with:

- `X-API-Key: <secret>`

## Health

- `GET /health/`

## Auth

- `POST /auth/register/`
- `POST /auth/login/`
- `GET /auth/me/`
- `PATCH /auth/me/`
- `POST /auth/change-password/`

Login is OTP-based:

1. `POST /auth/login/` with `phone_number` and `password`
2. API returns `202` with `otp_required: true`
3. `POST /auth/login/` again with `phone_number`, `password`, and `otp`

### Register example

```json
{
  "phone_number": "254700000001",
  "password": "StrongPass123!",
  "full_name": "Alice Example",
  "account_type": "INDIVIDUAL",
  "default_payment_mode": "WALLET"
}
```

### Login verify payload

```json
{
  "phone_number": "254700000001",
  "password": "StrongPass123!",
  "otp": "123456"
}
```

For the default test account `254710956633`, the backend accepts any 6-digit OTP during login.

## Dashboard

- `GET /dashboard/`
- `GET /dashboard/?organization_id={organization_id}`

Dashboard responses are now screen-oriented and include richer UI data such as:

- wallet and vault balances
- due-this-month schedules with fee and gross totals
- recent transactions
- category breakdowns
- monthly trend data
- selected organization summary when `organization_id` is supplied

## Integrations

- `GET /integrations/api-keys/`
- `GET /integrations/api-keys/?organization_id={organization_id}`
- `POST /integrations/api-keys/`
- `POST /integrations/api-keys/{api_key_id}/revoke/`

### Create API key payload

```json
{
  "name": "ERP Sync",
  "scopes": ["read", "write"]
}
```

### Create organization-scoped API key payload

```json
{
  "name": "Treasury Integration",
  "organization_id": "org-uuid",
  "scopes": ["read", "write"]
}
```

## Organizations

- `GET /organizations/`
- `POST /organizations/`
- `GET /organizations/{organization_id}/`
- `PATCH /organizations/{organization_id}/`
- `GET /organizations/{organization_id}/members/`
- `POST /organizations/{organization_id}/members/`
- `PATCH /organizations/{organization_id}/members/{membership_id}/`
- `DELETE /organizations/{organization_id}/members/{membership_id}/`

### Add member example

```json
{
  "user_id": "checker-user-uuid",
  "role": "CHECKER"
}
```

### Organization payload fields

- `name`
- `registration_number`
- `default_currency`
- `push_notifications_enabled`
- `sms_notifications_enabled`
- `kyc_status` for superadmins only

## Payee Directory

- `GET /payee-presets/`
- `GET /payee-presets/?q={search}&payee_type={PAYBILL|TILL|MOBILE|BANK}&active={true|false}`
- `GET /payees/`
- `GET /payees/?organization_id={organization_id}`
- `GET /payees/?q={search}&payee_type={PAYBILL|TILL|MOBILE|BANK}&active={true|false}`
- `POST /payees/`
- `GET /payees/{payee_id}/`
- `PATCH /payees/{payee_id}/`
- `DELETE /payees/{payee_id}/`

### Payee payload examples

Preset response example:

```json
{
  "id": "preset-uuid",
  "label": "KPLC",
  "payee_type": "PAYBILL",
  "paybill_number": "888880",
  "till_number": "",
  "expense_category": "utilities",
  "active": true
}
```

Create a payee from a preset:

```json
{
  "preset_id": "preset-uuid",
  "account_reference": "12345678"
}
```

```json
{
  "label": "KPLC",
  "payee_type": "PAYBILL",
  "paybill_number": "888880",
  "account_reference": "ACC123",
  "expense_category": "utilities"
}
```

```json
{
  "organization_id": "org-uuid",
  "label": "Vendor Settlement",
  "payee_type": "BANK",
  "bank_name": "KCB",
  "bank_code": "01",
  "account_number": "00123456789",
  "expense_category": "vendors"
}
```

## Schedules

- `GET /schedules/`
- `GET /schedules/?organization_id={organization_id}`
- `GET /schedules/?q={search}&category={category}&active={true|false}`
- `POST /schedules/`
- `GET /schedules/{schedule_id}/`
- `PATCH /schedules/{schedule_id}/`
- `DELETE /schedules/{schedule_id}/`

```json
{
  "payee_id": "payee-uuid",
  "amount_minor": 300000,
  "day_of_month": 5,
  "interval_months": 3,
  "next_due_date": "2026-07-05",
  "requires_approval": true,
  "active": true
}
```

## Wallets

- `GET /wallets/`
- `GET /wallets/?organization_id={organization_id}`
- `GET /wallets/ledger/`
- `GET /wallets/ledger/?organization_id={organization_id}&wallet_type={PRIMARY|VAULT}&entry_type={TOP_UP|DISBURSEMENT|TRANSFER_TO_VAULT|TRANSFER_FROM_VAULT|ADJUSTMENT}`
- `POST /wallets/topups/`
- `POST /wallets/vault/`
- `POST /wallets/withdrawals/`

### Top-up payload

```json
{
  "amount_minor": 900000,
  "provider": "pesapal",
  "wallet_type": "PRIMARY"
}
```

For individual accounts, set `"wallet_type": "VAULT"` to send the deposit directly to the vault.

### Corporate top-up payload

```json
{
  "organization_id": "org-uuid",
  "amount_minor": 900000,
  "provider": "stripe",
  "wallet_type": "PRIMARY"
}
```

### Vault payload

```json
{
  "amount_minor": 50000
}
```

### M-Pesa withdrawal payload

```json
{
  "amount_minor": 10000,
  "phone_number": "254711223344"
}
```

## Individual payments

- `POST /payments/pay-all/`
- `POST /payments/quick-pay/`

```json
{
  "payment_mode": "WALLET",
  "simulate_collection": true
}
```

Optional:

- `schedule_ids`: array of schedule UUIDs
- `payment_mode`: `WALLET` or `STK`
- `simulate_collection`: set `false` to enqueue a real payment microservice collection request when `payment_mode` is `STK`

### Quick pay payload

```json
{
  "payee_id": "payee-uuid",
  "amount_minor": 25000,
  "payment_mode": "WALLET",
  "simulate_collection": true,
  "description": "Quick pay from UI"
}
```

For organization flows:

```json
{
  "organization_id": "org-uuid",
  "payee_id": "payee-uuid",
  "amount_minor": 25000,
  "payment_mode": "WALLET",
  "submit_for_approval": true,
  "description": "Corporate quick pay from UI"
}
```

When `organization_id` is supplied, the backend creates a single-instruction corporate batch and can submit it straight into the approval queue.

## Approvals

- `GET /approvals/`
- `GET /approvals/?organization_id={organization_id}`

This endpoint returns screen-ready approval queue items with:

- base payout
- fee amount
- gross debit
- submitter
- scheduled run date
- sample instructions

## Corporate batches

- `POST /corporate/batches/upload/`
- `POST /corporate/batches/{batch_id}/submit/`
- `POST /corporate/batches/{batch_id}/approve/`
- `POST /corporate/batches/{batch_id}/reject/`
- `GET /batches/`
- `GET /batches/?organization_id={organization_id}`
- `GET /batches/?status={status}&batch_kind={kind}&payment_mode={mode}`
- `GET /batches/{batch_id}/`

### Upload payload

```json
{
  "organization_id": "org-uuid",
  "scheduled_for": "2026-07-15",
  "payment_mode": "WALLET",
  "source_file_name": "vendor-batch.csv",
  "description": "July vendor run",
  "csv_content": "recipient_name,recipient_type,amount_minor,category,phone_number,external_reference\nVendor A,MOBILE,100000,payroll,254711111111,EMP001\nVendor B,MOBILE,50000,payroll,254722222222,EMP002"
}
```

### Reject payload

```json
{
  "reason": "Incorrect beneficiary amount"
}
```

## Reporting

- `GET /reports/transactions.csv`
- `GET /reports/transactions.csv?organization_id={organization_id}`
- `GET /reports/transactions/summary/`
- `GET /reports/transactions/summary/?organization_id={organization_id}&date_from={YYYY-MM-DD}&date_to={YYYY-MM-DD}`
- `GET /reports/exports/`
- `GET /reports/exports/?organization_id={organization_id}`

### Transaction summary response

Provides statement-style aggregates and rows for the UI:

- opening balance
- total debits
- total credits
- total fees
- transaction rows with base, fee, and gross amounts

## Payment microservice

Quick Bundl dispatches real payment operations through the configured payment microservice.
See `docs/payment-microservice.md` for sample requests, responses, callbacks, and status checks.

## Background commands

- `python manage.py loaddata notifications/fixtures/notification_templates.json`
- `python manage.py process_notifications`
- `python manage.py run_due_payments`
- `python manage.py send_reminders`
- `python manage.py process_outbox`

## Notification configuration

Notify provider settings:

- `NOTIFY` or `NOTIFY_URL`
- `NOTIFY_API_KEY` or `X-API-KEY`
- `NOTIFY_SYSTEM`
- `NOTIFY_TIMEOUT_SECONDS`
