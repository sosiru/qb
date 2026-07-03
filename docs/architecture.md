# Route (Bundl Pay) MVP Architecture

## 1. Goal
Build the smallest backend that can safely support:

- Individual recurring bill setup and one-tap settlement
- Wallet and vault balances
- Corporate maker-checker approval for bulk payouts
- Async-friendly payment execution and notifications

The implementation is intentionally minimal, but the boundaries are chosen so the system can scale without reworking auth, money movement, or approvals.

## 2. System Architecture

```text
Clients
  - Mobile app (individual)
  - Web dashboard (corporate)
  - Checker mobile approval view
  - External integrators via API keys
        |
        v
API Layer (Django)
  - Auth + account onboarding
  - Payee directory + schedules
  - Wallets + vault
  - Corporate batch upload
  - Approval endpoints
  - Reporting export
        |
        +--> PostgreSQL in production / SQLite for local dev
        |
        +--> Outbox events
        |      - payment.batch.succeeded
        |      - wallet.topup.completed
        |      - collection.stk.requested
        |
        +--> Background workers
               - reminder generator
               - outbox dispatcher
               - due-payment scheduler
        |
        +--> External adapters
               - M-Pesa STK Push
               - Pesapal / Stripe top-ups
               - SMS gateway
               - Push notification provider
```

## 3. Why this shape

- `Custom user model now`: avoids painful auth migration later.
- `Wallet + ledger`: all balance changes remain auditable.
- `PaymentBatch + PaymentInstruction`: one aggregate payment with many downstream splits.
- `OrganizationMembership`: supports maker-checker without duplicating user models.
- `OutboxEvent`: keeps external side effects asynchronous and retryable.
- `Stateless bearer token auth`: simple for MVP, horizontally scalable behind a load balancer.

## 4. Scaling path to millions of users

### Current MVP
- Single Django API process
- SQLite for local development
- Synchronous request handling
- Outbox persisted in database

### Production evolution
1. Move to PostgreSQL with read replicas.
2. Put API behind Nginx or an L7 load balancer.
3. Run multiple stateless Django instances.
4. Push outbox processing to Celery/RQ workers with Redis or SQS.
5. Store uploads and exports in object storage.
6. Add idempotency keys on payment-creating endpoints.
7. Partition large tables:
   - `payment_instruction`
   - `wallet_ledger_entry`
   - `notification_event`
   - `audit_log`
8. Add provider webhook endpoints with signature validation.
9. Cache dashboard summaries and provider directory lookups.

## 5. Runtime concerns

- Money is stored in `amount_minor` integer fields.
- API processes are stateless.
- Long-running payment work is modeled as batch state transitions.
- Notifications are persisted first, then dispatched asynchronously.
- TLS termination belongs at the ingress/load balancer layer.

## 6. File Structure

```text
qb/
├── manage.py
├── db.sqlite3
├── route_platform.sqlite3
├── docs/
│   ├── api-endpoints.md
│   ├── architecture.md
│   └── database-schema.md
├── postman/
│   └── route-mvp.postman_collection.json
├── qb/
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py
│   └── wsgi.py
├── api/
│   ├── auth.py
│   ├── models.py
│   ├── services.py
│   ├── urls.py
│   ├── views.py
│   └── migrations/
├── audit/
│   ├── models.py
│   └── migrations/
├── base/
│   ├── admin.py
│   ├── apps.py
│   ├── common.py
│   ├── management/
│   │   └── commands/
│   │       ├── process_notifications.py
│   │       ├── process_outbox.py
│   │       ├── run_due_payments.py
│   │       └── send_reminders.py
│   ├── models.py
│   ├── provider_executor.py
│   ├── providers/
│   │   └── pesaway.py
│   ├── services.py
│   ├── tests.py
│   ├── utils.py
│   └── migrations/
├── eusers/
│   ├── managers.py
│   ├── models.py
│   ├── services.py
│   └── migrations/
├── notifications/
│   ├── fixtures/
│   │   └── notification_templates.json
│   ├── models.py
│   ├── services.py
│   └── migrations/
└── reports/
    ├── models.py
    ├── services.py
    └── migrations/
```

## 7. Implemented MVP scope

- Phone-number registration and login
- Profile update and notification preference management
- Individual / corporate fork at onboarding
- Individual payee directory
- Payee and schedule CRUD with search/filter support
- Wallet top-up and vault transfer
- Wallet ledger history
- Individual pay-all execution
- Corporate batch upload via CSV text
- Corporate membership administration
- Maker submit / checker approve / reject
- Transaction CSV export
- Reminder event generation
- Outbox persistence for async integrations
- PesaWay adapter for real collection and payout dispatch
- DB-driven SMS and email notifications
- API-key based external integrations

## 8. Explicit MVP tradeoffs

- Local build uses SQLite; production should use PostgreSQL.
- Corporate upload currently supports CSV. The upload service boundary is ready for an XLSX parser adapter next.
- PesaWay is integrated, but callback verification and reconciliation rules still need the provider's exact webhook contract.
- Bearer tokens are database-backed; for high scale, replace with short-lived JWT access tokens plus refresh tokens or move token storage to Redis-backed session infrastructure.

## 9. PesaWay integration

Real provider execution is enabled with:

- `PESAWAY_ENABLED=1`
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

Execution path:

1. The API creates or approves a batch.
2. Wallet debit happens synchronously for wallet-funded flows.
3. Outbox events are created for collection or payout work.
4. `python manage.py process_outbox` performs the PesaWay API calls.
5. Batch status finalizes after instruction-level outcomes are known.

## 10. Notification system

Notification flow:

1. Business logic queues `NotificationEvent` rows.
2. Each event references a `NotificationTemplate`.
3. Templates are loaded from fixture data.
4. `python manage.py process_notifications` sends pending SMS and email jobs.

Bootstrapping:

```bash
./venv/bin/python manage.py loaddata notifications/fixtures/notification_templates.json
```

Required notify settings:

- `NOTIFY` or `NOTIFY_URL`
- `NOTIFY_API_KEY` or `X-API-KEY`
- `NOTIFY_SYSTEM`

## 11. App split

The backend is now split into real Django apps by responsibility:

- `eusers`: user identity and bearer access tokens
- `api`: API-key integrations, request auth helpers, HTTP views, and URL routing
- `base`: organizations, wallets, payees, schedules, payment batches, instructions, and outbox
- `notifications`: templates, notification events, and dispatch logic
- `audit`: audit trail tables
- `reports`: report-export tracking and report services
