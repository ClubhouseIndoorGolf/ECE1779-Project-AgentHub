# Changelog

## Unreleased

### 2026-11-19

- **Chat request validation and conflict codes.** Conversation titles and message content containing NUL return `422` before writes. Message submission and proposal decision conflicts return a stable top-level `code` with their existing HTTP `409` and English `detail`. Identical retries return their original receipts. Expiration detected during approval commits the invalidation and its events before returning the conflict. [Developer guide](docs/developer/REQUEST_ERROR_CODES.md)
- **Local verification** (`c7d4224`). Passed 132 backend tests on isolated PostgreSQL, 20 offline client tests, Ruff lint and format checks, and fixed-mode Compose API and worker smoke checks. The regression tests cover rollback when expiration events cannot be saved.

### 2026-11-18

- **Saved refund execution details.** History exposes `execution_id` on the source message of a saved refund check. `GET /executions/{execution_id}` returns the saved eligibility, approval receipt, continuation status, and applied refund for the owning account. Order numbers and amounts in minor units are decimal strings. The history pagination and SSE v2 cursor contracts remain compatible with existing clients. [API example](README.md#read-a-saved-refund-result) · [Verification procedure](docs/MANUAL_TESTING.md#mt21-read-saved-execution-details)
- **Local verification.** Passed 126 backend tests on isolated PostgreSQL, 20 offline client tests, and Ruff lint and format checks. The fixed-mode Compose smoke test and MT21 API checks passed against the running API and worker.

### 2026-11-16

- **Durable conversation events and v2 replay.** Migration `0007_conversation_events` adds a per-conversation event log. The API and worker commit message, proposal, decision, job and resolution events with the corresponding business changes. The first history page supplies `X-Conversation-Event-Cursor` so clients can resume `/events?version=2` on any replica using the same PostgreSQL database. The browser continues using the default SSE stream with numeric message cursors. [Live updates API](docs/LIVE_UPDATES.md#durable-event-replay)
- **Final replay verification** (`2314ac6`). Passed 123 backend tests on isolated PostgreSQL, 20 offline client tests, and Ruff lint and format checks. A fresh fixed-mode Compose run with two API replicas replayed six consecutive events from `v2:6` to `v2:11` using the original `v2:5` cursor. Duplicate approval returned the same receipt. PostgreSQL recorded one CAD 129.99 simulated refund, one continuation and one resolution completion event. [Manual reproduction](docs/MANUAL_TESTING.md#mt20-resume-durable-sse-on-another-api-replica)

### 2026-11-15

- **Durable SSE replay verified across API replicas** (`756382d`). In an isolated fixed-mode Compose stack, API A returned the pending proposal and cursor `v2:5`. After disconnection, approval and execution finished. API B replayed six events in order, from `v2:6` to `v2:11`. Repeating approval returned the same receipt. PostgreSQL contained one CAD 129.99 simulated refund, one continuation job, and one resolution completion event. [Manual reproduction](docs/MANUAL_TESTING.md#mt20-resume-durable-sse-on-another-api-replica)
- **Account isolation after reauthentication.** Reauthenticating with the same account after an SSE-related `401` preserves the open conversation and its draft and restarts live updates. Signing in with another account clears the previous account's conversation, drafts, and pending submission state before loading the new account's workspace. Responses to earlier requests cannot update the new workspace. [Live updates guide](docs/LIVE_UPDATES.md#browser-client)

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
