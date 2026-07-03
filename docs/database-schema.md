# Database Schema

Every primary key in the platform is a UUID.

All monetary values use integer minor units, for example `300000 = KES 3,000.00` if you treat two decimals as cents.

## eusers app

### `eusers_user`
- `id` UUID PK
- `phone_number` unique
- `full_name`
- `email`
- `account_type` (`INDIVIDUAL`, `CORPORATE`, `SUPERADMIN`)
- `default_payment_mode` (`WALLET`, `STK`)
- `sms_notifications_enabled`
- `email_notifications_enabled`
- `push_notifications_enabled`
- `mfa_enabled`
- `is_phone_verified`
- auth flags and timestamps

### `eusers_accesstoken`
- `id` UUID PK
- `user_id` FK -> `eusers_user`
- `prefix`
- `token_hash` unique
- `last_used_at`
- `expires_at`
- `revoked_at`

## api app

### `api_integrationapikey`
- `id` UUID PK
- `user_id` FK -> `eusers_user`
- `organization_id` nullable FK -> `base_organization`
- `created_by_id` nullable FK -> `eusers_user`
- `name`
- `key_prefix`
- `key_hash` unique
- `scopes` JSON
- `is_active`
- `last_used_at`
- `expires_at`
- `revoked_at`

## base app

### `base_organization`
- `id` UUID PK
- `name`
- `slug` unique
- `kyc_status`
- `default_currency`
- `push_notifications_enabled`
- `sms_notifications_enabled`

### `base_organizationmembership`
- `id` UUID PK
- `user_id` FK -> `eusers_user`
- `organization_id` FK -> `base_organization`
- `role` (`ADMIN`, `MAKER`, `CHECKER`, `VIEWER`)
- `is_active`
- unique `(user_id, organization_id)`

### `base_wallet`
- `id` UUID PK
- `owner_type` (`USER`, `ORGANIZATION`)
- `wallet_type` (`PRIMARY`, `VAULT`)
- `user_id` nullable FK -> `eusers_user`
- `organization_id` nullable FK -> `base_organization`
- `currency`
- `available_balance_minor`
- unique per `(user, wallet_type)` and `(organization, wallet_type)`
- individual top-ups may target either `PRIMARY` or `VAULT`; organization top-ups target `PRIMARY` only

### `base_walletledgerentry`
- `id` UUID PK
- `wallet_id` FK -> `base_wallet`
- `entry_type`
- `amount_minor`
- `balance_after_minor`
- `reference`
- `metadata` JSON

### `base_payee`
- `id` UUID PK
- `user_id` nullable FK -> `eusers_user`
- `organization_id` nullable FK -> `base_organization`
- `payee_type` (`PAYBILL`, `TILL`, `MOBILE`, `BANK`)
- destination fields:
  - `account_reference`
  - `phone_number`
  - `paybill_number`
  - `till_number`
  - `bank_name`
  - `bank_code`
  - `account_number`
- `expense_category`
- `active`

### `base_paymentschedule`
- `id` UUID PK
- `payee_id` FK -> `base_payee`
- `amount_minor`
- `day_of_month` check `1..31`
- `active`

### `base_paymentbatch`
- `id` UUID PK
- `batch_kind` (`INDIVIDUAL_MONTHLY`, `CORPORATE_UPLOAD`)
- `status` (`DRAFT`, `PENDING_APPROVAL`, `APPROVED`, `REJECTED`, `PROCESSING`, `SUCCEEDED`, `PARTIAL`, `FAILED`)
- `payment_mode` (`WALLET`, `STK`)
- `user_id` nullable FK -> `eusers_user`
- `organization_id` nullable FK -> `base_organization`
- `scheduled_for`
- `description`
- `source_file_name`
- `total_amount_minor`
- `fee_amount_minor`
- `submitted_by_id`
- `approved_by_id`
- lifecycle timestamps
- `metadata` JSON

### `base_paymentinstruction`
- `id` UUID PK
- `batch_id` FK -> `base_paymentbatch`
- `payee_id` nullable FK -> `base_payee`
- `recipient_name`
- `recipient_type`
- `destination` JSON
- `amount_minor`
- `category`
- `external_reference`
- `provider_reference`
- `provider_response` JSON
- `status`
- `failure_reason`

### `base_outboxevent`
- `id` UUID PK
- `topic`
- `aggregate_type`
- `aggregate_id`
- `status`
- `attempts`
- `available_at`
- `last_error`
- `payload` JSON

## notifications app

### `notifications_notificationtemplate`
- `id` UUID PK
- `code` unique
- `event_type`
- `channel` (`SMS`, `EMAIL`)
- `system`
- `provider_template`
- `subject_template`
- `description`
- `default_context` JSON
- `active`

### `notifications_notificationevent`
- `id` UUID PK
- `user_id` nullable FK -> `eusers_user`
- `template_id` nullable FK -> `notifications_notificationtemplate`
- `channel` (`SMS`, `EMAIL`)
- `event_type`
- `status`
- `scheduled_for`
- `sent_at`
- `unique_identifier`
- `recipients` JSON
- `context` JSON
- `provider_response` JSON
- `attempts`
- `last_error`

## audit app

### `audit_auditlog`
- `id` UUID PK
- `actor_id` nullable FK -> `eusers_user`
- `action`
- `target_type`
- `target_id`
- `metadata` JSON

## reports app

### `reports_reportexport`
- `id` UUID PK
- `requested_by_id` FK -> `eusers_user`
- `organization_id` nullable FK -> `base_organization`
- `export_type`
- `file_format`
- `status`
- `file_name`
- `filters` JSON
- `generated_at`
- `last_error`

## Recommended production indexes to add next

- `base_paymentbatch(status, scheduled_for)`
- `base_paymentinstruction(batch_id, status)`
- `base_walletledgerentry(wallet_id, created_at desc)`
- `notifications_notificationevent(status, scheduled_for)`
- `base_outboxevent(status, available_at)`
- `audit_auditlog(actor_id, created_at desc)`
