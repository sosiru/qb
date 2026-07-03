# API Endpoints

Base URL: `http://127.0.0.1:8006/api/v1`

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

### Register example

```json
{
  "phone_number": "+254700000001",
  "password": "StrongPass123!",
  "full_name": "Alice Example",
  "account_type": "INDIVIDUAL",
  "default_payment_mode": "WALLET"
}
```

## Dashboard

- `GET /dashboard/`

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

## Payee Directory

- `GET /payees/`
- `GET /payees/?organization_id={organization_id}`
- `GET /payees/?q={search}&payee_type={PAYBILL|TILL|MOBILE|BANK}&active={true|false}`
- `POST /payees/`
- `GET /payees/{payee_id}/`
- `PATCH /payees/{payee_id}/`
- `DELETE /payees/{payee_id}/`

### Payee payload examples

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

## Individual payments

- `POST /payments/pay-all/`

```json
{
  "payment_mode": "WALLET",
  "simulate_collection": true
}
```

Optional:

- `schedule_ids`: array of schedule UUIDs
- `payment_mode`: `WALLET` or `STK`
- `simulate_collection`: set `false` to enqueue a real PesaWay collection request when `payment_mode` is `STK`

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
  "csv_content": "recipient_name,recipient_type,amount_minor,category,phone_number,external_reference\nVendor A,MOBILE,100000,payroll,+254711111111,EMP001\nVendor B,MOBILE,50000,payroll,+254722222222,EMP002"
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

## Provider callbacks

- `POST /providers/pesaway/results/`

Configure `PESAWAY_RESULTS_URL` to this endpoint on a public HTTPS domain in production.

## PesaWay environment variables

- `PESAWAY_ENABLED`
- `PESAWAY_CLIENT_ID`
- `PESAWAY_CLIENT_SECRET`
- `PESAWAY_BASE_URL`
- `PESAWAY_RESULTS_URL`
- `PESAWAY_DEFAULT_CURRENCY`
- `PESAWAY_C2B_CHANNEL`
- `PESAWAY_B2C_CHANNEL`
- `PESAWAY_B2B_PAYBILL_CHANNEL`
- `PESAWAY_B2B_TILL_CHANNEL`
- `PESAWAY_BANK_CHANNEL`

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
