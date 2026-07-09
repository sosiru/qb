# Quick Bundl UI Feature Specification

This document defines the product features that the UI must support across:

- `Web app`
- `Mobile app`

It is written as a design input document, not as backend implementation notes. Use it to design information architecture, screen flows, interaction patterns, permissions, and visual states.

The system is an expense orchestration and payments platform for:

- individuals paying recurring bills and personal obligations
- organizations running controlled payout operations
- service providers operating across multiple client organizations
- superadmins with full cross-system visibility

The backend is API-first. The intended end state is:

- a production web application
- a production mobile application
- both powered by the same backend API

## 1. Product Summary

The product helps users:

- onboard and authenticate securely
- manage saved payees and bill recipients
- schedule recurring payments
- fund wallets and move funds to savings vaults
- settle bills and payouts with visibility into fees
- run approval workflows where required
- track statements, ledger movements, and transaction history
- manage organizations, teams, and roles
- export reports

The system must make money movement feel:

- transparent
- controlled
- auditable
- simple to scan

Every payment-facing experience must clearly show:

- transaction amount
- processing fee or service charge
- total debit
- destination
- status
- approval state if applicable

Current commercial rule:

- `2% processing fee` is added per transaction

## 2. Platforms

## 2.1 Web App

The web app is the main control surface for:

- corporate users
- service providers
- superadmins
- high-volume individual power users

The web app should prioritize:

- dense information layouts
- table-based workflows
- bulk actions
- advanced filters
- statement and report views
- approvals
- organization switching

## 2.2 Mobile App

The mobile app is the main everyday operating surface for:

- individuals
- approvers on the move
- field service provider staff
- light corporate usage

The mobile app should prioritize:

- fast account access
- payment review
- balance visibility
- recurring payment management
- approvals
- push-worthy alerts and reminders

The mobile app should not be treated as a stripped copy of web. It should focus on the highest-frequency tasks and reduce operational friction.

## 3. Primary User Types

## 3.1 Individual

An individual user manages personal or household payments.

Typical use cases:

- pay electricity bills
- pay school fees
- save funds in a vault
- create recurring schedules
- manually trigger due payments
- review past transactions and fees

## 3.2 Corporate Admin

A corporate admin manages the organization’s settings, wallet, team access, and payout operations.

Typical use cases:

- create or edit organization details
- top up organization wallet
- add team members
- assign roles
- review batches and statements
- oversee approvals

## 3.3 Corporate Maker

A maker prepares payment work but should not be the final approver.

Typical use cases:

- upload payout batches
- create corporate payees
- prepare payment instructions
- submit items for approval

## 3.4 Corporate Checker

A checker is responsible for approval or rejection.

Typical use cases:

- review pending batches
- inspect totals, fees, recipients, and references
- approve or reject with a reason

## 3.5 Corporate Viewer

A viewer can monitor but not execute sensitive actions.

Typical use cases:

- see balances
- inspect transactions
- read statements
- monitor approvals

## 3.6 Service Provider

A service provider is an operator who supports multiple organizations.

This role must be able to:

- access many organizations
- switch organizations easily
- view organization-level data
- operate across clients without re-login

This role needs a strong organization switcher and a clear active-organization context at all times.

## 3.7 Superadmin

A superadmin has top-level access across the platform.

This role must be able to:

- switch across organizations
- view both individual and organizational surfaces
- inspect system-wide activity
- manage operational oversight

## 4. Role and Permission Model

The UI must be explicitly role-aware.

The UI should not only block actions after a user clicks. It should also:

- hide actions the user cannot perform
- disable sensitive controls where partial visibility is still useful
- explain why an action is unavailable when appropriate

### Permissions by role

`Individual`
- manage own payees
- manage own schedules
- top up own wallet
- move funds to vault
- withdraw to M-Pesa
- trigger own payments
- view own statements and ledger

`Corporate Admin`
- full organization access
- manage organization members
- manage organization wallet
- manage corporate payees
- upload batches
- submit, approve, reject
- view reports and statements

`Corporate Maker`
- manage payees and schedules tied to organization operations if allowed by policy
- upload batches
- submit for approval
- view own organization data
- cannot finalize approvals

`Corporate Checker`
- review and approve or reject pending items
- view organization data
- usually should not create the same payment items they approve

`Corporate Viewer`
- read-only access

`Service Provider`
- cross-organization access
- organization switcher
- operational visibility and selected actions depending on policy

`Superadmin`
- global access
- organization switcher
- individual and organization oversight

## 5. Core Product Areas

## 5.1 Authentication and Account Access

### Features

- register by phone number
- login with phone number and password
- OTP verification during login
- bearer-token based app session
- API key management for integrators

### UI needs

- registration flow
- login flow
- OTP entry screen
- wrong OTP state
- expired OTP state
- resend OTP state
- logout

### Mobile emphasis

- very fast OTP entry
- clear keyboard behavior
- low-friction recovery

### Web emphasis

- robust authentication form states
- possible admin and corporate multi-user entry points

## 5.2 User Profile and Preferences

### Features

- view profile
- edit full name
- edit phone and email where allowed
- change password
- choose default payment mode
- manage notification preferences
- set M-Pesa withdrawal phone
- toggle payout approval requirement for withdrawals

### UI needs

- profile summary
- editable preference form
- security section
- notification settings section
- payout preferences section

## 5.3 Dashboard

The dashboard must vary by role.

### Individual dashboard

Must show:

- primary wallet balance
- vault balance
- due or upcoming payments
- total recurring commitments
- shortfall or missing funding amount
- recent transactions
- quick actions:
  - top up
  - pay now
  - add payee
  - add recurring payment

### Corporate dashboard

Must show:

- organization balance
- pending approvals count
- recent batches
- recent wallet activity
- team or role snapshot
- payout activity trend placeholder area
- quick actions:
  - upload batch
  - review approvals
  - top up wallet
  - add member

### Service provider dashboard

Must show:

- organization switcher
- all accessible organizations
- each organization’s:
  - wallet balance
  - pending approvals
  - KYC status
  - role context

### Superadmin dashboard

Must show:

- global organization list
- global operational overview
- optional individual summary block

## 5.4 Organization Switching

This is critical for:

- service providers
- superadmins

### Requirements

- visible active organization selector
- searchable organization list
- fast switching
- persistent current context across pages
- strong visual indication of current organization

### Design note

The active organization should affect:

- wallet summary
- payees
- schedules
- members
- batches
- reports
- statements

The user must never be confused about which organization they are acting on.

## 5.5 Organization Management

### Features

- create organization
- view organization details
- edit organization settings
- track KYC status
- manage notification settings at organization level

### UI needs

- organization profile page
- settings page
- KYC status indicator
- operational metadata panel

## 5.6 Team and Access Management

### Features

- list members
- add members
- edit role
- deactivate member

### UI needs

- team directory table on web
- compact member list on mobile
- role badges
- invitation or add-member form
- permission-aware action buttons

### Key states

- active
- inactive
- role changed
- no members yet

## 5.7 Payee Directory

Payees are saved destinations used for bills and payouts.

### Supported payee types

- `PAYBILL`
- `TILL`
- `MOBILE`
- `BANK`

### Features

- list payees
- search payees
- filter by type
- filter by active state
- create payees
- edit payees
- delete payees
- create from preset

### Preset payees

The system supports preloaded payee presets such as:

- `KPLC` with paybill `888880`

### UI needs

- payee list
- payee detail
- create-payee wizard or modal
- create-from-preset path
- payee type-specific forms

### Form behavior

Fields must change based on payee type:

`PAYBILL`
- label
- paybill number
- account reference
- expense category

`TILL`
- label
- till number
- expense category

`MOBILE`
- label
- phone number
- expense category

`BANK`
- label
- bank name
- bank code
- account number
- account reference if needed
- expense category

## 5.8 Recurring Payment Schedules

This is one of the most important areas for both web and mobile.

### Features

- list schedules
- create schedule
- edit schedule
- delete schedule
- activate or deactivate schedule
- set amount
- set day of month
- set interval in months
- set next due date
- mark whether approval is required

### Example

School fees can be configured as:

- amount: KES 9,000.00
- day of month: 5
- interval: every 3 months
- next due date: specific calendar date
- approval required: yes

### UI needs

- schedule list
- schedule detail
- create/edit form
- calendar-aware due date selection
- recurring cadence selector
- approval-required switch

### Schedule statuses and indicators

Each schedule should visually expose:

- active or inactive
- due soon
- due now
- next due date
- cadence
- approval required

### Design requirement

Show recurring logic in simple language:

- monthly
- every 2 months
- every 3 months
- every 6 months
- yearly if represented as 12 months

## 5.9 Wallets

The wallet experience must feel trustworthy and ledger-backed.

### Wallet types

`Primary wallet`
- operational funds used for payments

`Vault`
- reserved funds for individual users

### Features

- view wallet balances
- view wallet summaries
- top up wallet
- direct top up to vault for individuals
- transfer to vault
- withdraw to M-Pesa
- inspect ledger history

### UI needs

- balance summary cards or panels
- funding form
- vault transfer form
- withdrawal form
- ledger table or list

### Key transaction fields

- amount
- balance after
- date and time
- reference
- entry type
- metadata

## 5.10 Fees and Total Cost Visibility

The UI must clearly represent the platform revenue rule:

- `2% processing fee per transaction`

### Wherever a payment appears, the UI should show:

- base amount
- fee amount
- total debit

### This applies to:

- individual pay-now flows
- recurring payment review
- corporate batch detail
- transaction details
- statements
- approvals

### Approval-specific requirement

Approvers must see:

- total payout amount
- total fee amount
- combined debit

## 5.11 Individual Payment Execution

### Features

- pay all due items
- pay selected schedules
- choose payment mode:
  - wallet
  - STK
- see immediate payment result or processing state

### UI needs

- due payments list
- payment review screen
- confirmation step
- result state

### Important states

- no active schedules
- no due schedules
- insufficient wallet balance
- payment processing
- succeeded
- partial
- failed

## 5.12 Approval-Required Personal Payments

The system already supports recurring schedules marked as requiring approval.

### Product intent

If `requires_approval` is enabled on a schedule:

- it should not auto-pay
- it should wait for manual review and explicit clearance

### UI design implication

The design should include a personal approval queue or review step, even if the deeper approval engine expands later.

For now, plan screens for:

- pending review payments
- payment review detail
- approve for payment or pay now
- defer or cancel

## 5.13 Corporate Batch Payments

This is the core corporate workflow.

### Features

- upload batch
- define scheduled date
- choose payment mode
- inspect parsed entries
- submit for approval
- approve
- reject with reason
- view batch detail
- view instruction-level outcomes

### Input model

Corporate uploads are row-based payment instructions with fields like:

- recipient name
- recipient type
- amount
- category
- destination details
- external reference

### UI needs

- upload screen
- parsed preview
- validation issues panel
- batch detail screen
- approval screen
- rejection modal with reason

### Batch statuses

- draft
- pending approval
- approved
- rejected
- processing
- succeeded
- partial
- failed

These states need strong visual treatment because they are operationally important.

## 5.14 Approval Workflow

Approval design is central to trust and governance.

### Use cases

- corporate batch approval
- recurring schedules that require approval
- owner-controlled payout or withdrawal review

### Information required on approval screens

- initiator
- submission time
- target payees or recipients
- amount
- fee
- total debit
- source wallet or organization
- notes or reason if rejected

### Actions

- approve
- reject
- inspect detail first

### Design principle

Never make the approver act from a blind list row. There should always be a path to deeper detail before final approval.

## 5.15 Statements and Transaction History

Statements should look professional and audit-friendly.

### Required capabilities

- view transaction history
- view wallet ledger
- inspect fees
- inspect gross total
- filter by type, status, date, organization, or category
- download CSV

### Statement content

Each line item should be able to show:

- date
- transaction reference
- beneficiary or payee
- channel or recipient type
- amount
- fee
- gross amount
- status
- organization or personal context

### Statement design requirements

- readable at high density
- clear totals
- easy date grouping
- easy filtering
- print-friendly structure for web
- compact but navigable detail on mobile

## 5.16 Reporting

### Current capabilities

- transaction CSV export

### UI needs

- report request action
- export history or last export acknowledgment
- filters before export

### Future-friendly design

Even if only CSV exists today, the reports surface should be designed to later accommodate:

- PDF statements
- Excel exports
- richer analytics

## 5.17 Notifications and Reminders

### Notification domains

- OTP and login-related notices
- due payment reminders
- batch submitted
- batch approved
- batch rejected
- payment success
- payment failure

### UI needs

- notification preferences
- in-app notification center placeholder
- banners, toasts, or activity feed states

### Mobile emphasis

- approval alerts
- due-payment reminders
- failure recovery prompts

## 5.18 Integrator Access

The platform includes API keys for external systems.

### Features

- create API key
- list keys
- revoke key
- organization-scoped keys

### UI needs

- API key management page in web app
- copy-once secret presentation
- active/revoked state
- scope display
- last-used and expiry indicators

This is mainly a web feature.

## 5.19 Audit and Operational Traceability

The system tracks auditable actions.

UI designs for admin or operations views should reserve room for:

- actor
- action type
- target
- timestamp
- metadata detail

This can be introduced as an operations module later, but the design system should be able to accommodate it cleanly.

## 6. Web App Information Architecture

Recommended top-level web navigation:

- Dashboard
- Wallets
- Payees
- Recurring Payments
- Payments
- Approvals
- Organizations
- Members
- Statements
- Reports
- Integrations
- Profile / Settings

### Suggested role-based behavior

`Individual`
- Dashboard
- Wallets
- Payees
- Recurring Payments
- Payments
- Statements
- Profile

`Corporate`
- Dashboard
- Wallets
- Payees
- Payments
- Approvals
- Members
- Statements
- Reports
- Integrations
- Settings

`Service Provider` and `Superadmin`
- all relevant corporate modules
- persistent organization switcher

## 7. Mobile App Information Architecture

Recommended mobile tabs:

- Home
- Payments
- Wallet
- Activity
- Profile

Secondary screens:

- Payees
- Schedules
- Approvals
- Statements
- Organization switcher
- Members if role allows

### Mobile-first quick actions

- top up
- pay due bill
- add payee
- review approval
- withdraw

## 8. Key Screen Definitions

The design work should at minimum define these screens.

### Shared

- splash or launch
- login
- OTP verification
- profile
- notification settings
- security settings

### Individual screens

- individual dashboard
- payee list
- add payee
- payee detail
- schedule list
- create/edit schedule
- due payments review
- payment confirmation
- wallet summary
- vault transfer
- withdrawal
- statement list
- statement detail

### Corporate screens

- corporate dashboard
- organization selector
- organization detail
- members list
- add/edit member
- payee list
- batch upload
- batch preview
- batch detail
- approval queue
- approval detail
- reports
- statement explorer
- API key management

### Service provider and superadmin screens

- multi-organization dashboard
- organization switcher
- organization operations detail

## 9. States the UI Must Handle

Every major module needs:

- loading state
- empty state
- success state
- validation error state
- permission denied state
- offline or request failure state

Payment modules also need:

- insufficient funds
- pending approval
- processing
- partial success
- failed

## 10. Search, Filter, and Sort Expectations

The web app should support strong filtering in:

- payees
- schedules
- batches
- statements
- wallet ledger
- members

Common filters:

- organization
- status
- role
- payee type
- category
- active or inactive
- date range
- payment mode

The mobile app should support lighter filters:

- status
- date range
- category

## 11. Data Visibility Rules

The UI must make the following highly visible whenever relevant:

- active user identity
- active organization
- role
- balance
- amount
- fee
- total debit
- approval status
- payment status

For any destructive or money-moving action, the user should see a final review step before execution.

## 12. Design Quality Expectations

The product should look operational and professional, not promotional.

### Desired visual character

- clean
- quiet
- efficient
- trust-oriented
- data-forward

### Avoid

- marketing-style hero layouts
- oversized decorative cards
- vague financial summaries
- hidden fee disclosures
- ambiguous approval flows

### Emphasize

- strong hierarchy
- consistent status colors
- precise labels
- readable tables
- compact summaries
- high-quality statement layouts

## 13. Mobile vs Web Split Guidance

Use this split when deciding where to place emphasis.

### Web-first features

- corporate batch upload
- heavy reporting
- member administration
- integration API key management
- dense statement exploration
- multi-organization operations

### Mobile-first features

- OTP login
- due bill review
- quick pay
- wallet checks
- approval actions
- withdrawal
- payment reminders

### Shared priority features

- dashboard
- payees
- recurring schedules
- statements
- transaction history
- fee visibility

## 14. Future-Ready Design Areas

The UI should leave room for these likely extensions:

- biometric login
- stronger MFA
- advanced KYC flows
- richer analytics
- PDF statements
- Excel exports
- provider reconciliation views
- more granular approval routing
- spending limits and policy controls
- favorites and master provider directory

Do not design the current product as if it must stay MVP-small forever.

## 15. Recommended Design Deliverables

For the design phase, produce:

- sitemap for web
- navigation model for mobile
- role-based user flows
- low-fidelity wireframes
- high-fidelity screens
- component inventory
- status and badge system
- table design standards
- statement templates
- approval interaction patterns

## 16. Final Product Principle

The system is not just a payment sender. It is a controlled money movement platform.

That means the UI must consistently balance:

- speed
- visibility
- approval discipline
- trust
- auditability

If a screen handles money, approvals, balances, or statements, it should feel exact and operational rather than decorative.
