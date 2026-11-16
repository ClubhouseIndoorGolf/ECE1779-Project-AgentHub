# Changelog

## Unreleased

### 2026-11-14

- **Live updates over SSE.** `GET /conversations/{id}/events` streams new messages, job status changes and proposal status changes for an owned conversation. `Last-Event-ID` replays messages after the given sequence, and each stream closes after five minutes so clients reconnect. The browser follows sends and approved continuations through pushed events, reconnects with backoff, and falls back to polling when no stream is live. The footer shows the connection state. No schema change. [Live updates guide](docs/LIVE_UPDATES.md)
- **Live update review fixes.** A history refresh triggered by a pushed event retries with backoff after a failed read. Polled and pushed job states share one cache that never moves backwards, including across retries. A `Last-Event-ID` with non-ASCII digits, such as `²`, starts a fresh stream instead of returning HTTP 500.
- **Expired credentials during live updates.** A 401 from the event stream or from a history refresh it triggered stops live updates and pending refresh retries, keeps the draft, and asks the user to sign in again. Signing in resumes the stream for the open conversation. Other non-retryable client errors stop refresh retries and show the error. Transient 5xx and 429 responses still back off.

### 2026-11-11

- **Ticket lookup** (`d3b49fa`). Fixed mode can look up a simulated support ticket and check the linked order's refund eligibility. Ticket and order access are checked separately. Migration `0004_read_only_ticket_lookup` adds tickets, and `seed-demo-tickets` adds sample tickets and their orders.
- **Refund proposals** (`53c310f`). The worker can save a pending proposal with a refund amount, a customer reply draft, and a proposed ticket status. The card is restored from conversation history after refresh. Migration `0005_resolution_proposals` adds proposal storage. The internal `PrepareRefund` action accepts a ticket ID and optional draft for future agent integration. The backend determines eligibility and amount.
- **Approval and execution** (`0f46e3d`). Users can approve or reject a saved proposal from its card. Approval queues a worker continuation that records the simulated refund and customer reply and resolves the ticket in one PostgreSQL transaction. Identical decisions return the original receipt. Migration `0006_approved_resolutions` adds decision and result storage.
- **Review fixes** (`575a798`). Pending proposals carried over from the planning-only version finish in conversation order. Switching chats immediately clears the previous proposal controls. The browser retries result polling after transient failures and reloads history when another tab pauses the conversation for approval.
- **Local verification** for `575a798`. Passed 87 backend tests against isolated PostgreSQL, 12 offline client tests, Ruff, and the fixed-mode container smoke test. Automated Chrome verification covered rejection, a new proposal, approval, and recovery after reload. The test database contained one simulated refund and one customer reply. [Manual test procedure](docs/MANUAL_TESTING.md#refund-workflow-in-the-browser)

## 2026-11-11 — Read-only refund check merged

Developed by **Jinxi Hu**. [PR #3](https://github.com/RavenHu001/ECE1779-Project-AgentHub/pull/3)

### 2026-11-08

- Documented the design and acceptance cases for the read-only refund check. [Developer guide](docs/developer/zh-CN/AGENTHUB_READ_ONLY_REFUND_CHECK.md)
- In fixed reply mode, users can ask whether a simulated order is eligible for a refund. The worker checks order ownership and returns the outcome, reasons, and eligible amount.
- Alembic revision `0003_read_only_refund_check` adds the `orders` table for simulated orders and their assigned accounts, and the `executions` table for structured eligibility results. The worker saves one result per source message in the same transaction as the chat reply and job completion. [Database reference](docs/BACKEND_DATABASE.md#simulated-refund-data)
- The `seed-demo-orders` admin command creates sample orders for eligible, expired, incomplete, and cross-account test cases. [Demo setup](docs/WORKER_BASELINE.md#read-only-refund-check)

### 2026-11-09

- Development accounts accept any non-empty ASCII password, including a single character.
- Dragging to select text in the sign-in form and releasing over the backdrop keeps the dialog open. The close button and Escape still dismiss it.
