# PRD Coverage

This file tracks the backend status against the current Quick Bundl PRD from a software-engineering perspective.

## Implemented

- Authentication
  - Phone-number registration
  - Login
  - Session-style bearer access tokens
  - API-key auth for integrators
- User profile
  - View profile
  - Edit profile
  - Change password/PIN-equivalent secret
  - Notification preferences
- Dynamic account type
  - Individual
  - Corporate
  - Superadmin cross-surface access
- Individual module
  - Payee CRUD
  - Schedule CRUD
  - Pay-all wallet flow
  - STK collection path scaffold
  - Wallet, vault, ledger, history
  - Dashboard summary
- Corporate module
  - Organization creation
  - Membership CRUD-style management
  - Roles: Admin, Maker, Checker, Viewer
  - CSV upload
  - Submit, approve, reject workflow
  - Batch detail and transaction history
- Notification engine
  - DB-driven templates
  - SMS and email dispatch queue
  - Reminder, approval, success, failure, approval-result events
- Reporting
  - CSV transaction export
- Admin portal
  - Detailed Django admin with list displays, filters, search, and inlines
- Background processing
  - Due-payment scheduler
  - Reminder generator
  - Notification worker
  - Outbox worker
- Audit
  - User, organization, payee, schedule, batch, and integration-key audit rows

## Partial

- OTP verification
  - Notification transport exists
  - OTP issuance and verification endpoints are not implemented yet
- Forgot PIN
  - Password change exists
  - Reset flow is not implemented yet
- Corporate KYC
  - `kyc_status` exists
  - No external KYC workflow or document ingestion yet
- STK callbacks and reconciliation
  - Collection callback endpoint exists
  - Signature verification and full reconciliation rules are still pending
- XLSX processing
  - CSV upload is implemented
  - XLSX parser adapter is not implemented yet
- Reporting
  - CSV export exists
  - PDF and Excel export are still pending
- Analytics
  - Core queryable data exists
  - Aggregation/chart endpoints are not implemented yet

## Not Yet Implemented

- MFA and biometric login
- JWT/OAuth flows
- Push-notification transport
- Master provider directory with favorites
- Departments, limits, and advanced corporate settings
- Idempotency keys on write endpoints
- Rate limiting
- Webhook signature validation
- Dedicated microservice split

## Engineering Readout

The backend is now a credible Phase 1 MVP modular monolith:

- bounded contexts are separated into `base`, `eusers`, `api`, `notifications`, `audit`, and `reports`
- the API surface supports core entity lifecycle operations
- money movement is ledger-backed
- external side effects are queued through the outbox
- notifications are template-driven and asynchronous

The biggest remaining production gaps are security hardening, reset/OTP flows, XLSX ingestion, and operational reconciliation.
