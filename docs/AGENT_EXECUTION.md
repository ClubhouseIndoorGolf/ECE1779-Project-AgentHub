# Agent execution and integration contract

This branch implements the worker portion of the [completion plan](EXECUTION_COMPLETION_PLAN.md).
OpenAI mode can select bounded tools; fixed mode keeps its documented commands.
The model can read facts, ask a question, reply, or prepare a proposal. Only a saved
human approval can queue the separate transaction that applies a simulated refund.

## Decisions and durable recovery

`agent_contract.py` validates every model decision with strict target IDs, bounded
text and forbidden extra arguments. The allowlist is `lookup_ticket`, `check_refund`,
`prepare_refund`, `ask_clarification`, and an ordinary text reply. There is no model
tool for approval, applying a refund, choosing an account, overriding an amount,
or sending an external message. The worker derives authority from the stored
conversation and checks current business rules in application code.

The Responses adapter sends strict function schemas, disables parallel tool calls
and SDK retries, and uses `store=False`. Each response must contain at most one
function call or usable text. Completed tool evidence is reconstructed from local
storage on each request. This follows the [OpenAI function-calling contract](https://developers.openai.com/api/docs/guides/function-calling).
The configured model must support these options and `reasoning.effort=none`.

`agent.py` reserves a model call durably before making the external request. It
saves the validated decision, then executes and checkpoints a read tool in a
separate short transaction. Terminal decisions commit with their final reply or
proposal and the job success. All checkpoint writes recheck the current lease
token and database time after lock waits. Worker replacement reuses completed
steps and pending terminal decisions. A crash before a model decision is saved
can repeat that model request; its consumed budget is retained.

Model calls occur outside database transactions. Waiting for approval releases
the initial job and lease. The existing `RESUME_EXECUTION` job applies the saved
proposal without calling the model.

## Default time and call limits

| Setting | Default | Meaning |
| --- | --- | --- |
| `MODEL_TIMEOUT_SECONDS` | 30 seconds | Timeout supplied to each SDK request, further capped by remaining agent time |
| `AGENT_TIMEOUT_SECONDS` | 120 seconds | Persisted deadline from the first agent attempt, including retry/backoff and recovery time; excludes later approval wait |
| `AGENT_MAX_MODEL_CALLS` | 6 | Reserved requests per initial job across all attempts |
| `AGENT_MAX_TOOL_CALLS` | 4 | Read/preparation decisions per initial job; reply/clarification do not consume this limit |
| `WORKER_MAX_ATTEMPTS` | 3 | Existing queue claim/retry limit |
| `WORKER_LEASE_SECONDS` | 60 seconds | Renewable ownership lease; heartbeat every third of the interval |
| `DB_LOCK_TIMEOUT_MS` | 5000 ms | PostgreSQL lock wait limit for application sessions |
| `DB_STATEMENT_TIMEOUT_MS` | 15000 ms | PostgreSQL statement execution limit for application sessions |

These are initial operational settings, not measured optimal values or a spending
cap. Tune from live latency evidence. The agent deadline is checked before/after
operations and before commit; it prevents late results from committing. SDK
timeouts and PostgreSQL limits bound waits, but this is not an OS-level hard kill
at exactly 120 seconds. Graceful shutdown may be interrupted by Compose's existing
45-second grace period; durable checkpoints make that interruption recoverable.

Model input includes serialized tool evidence within `MODEL_CONTEXT_CHARS` (12000);
output is bounded by `MODEL_MAX_OUTPUT_TOKENS` (600) and application string limits.
An oversized transcript fails explicitly. Fixed mode retains the API's full
20000-character input limit. Transient model and database failures use the queue's
bounded backoff. Invalid output and exhausted budgets fail visibly without
blindly retrying. Provider error bodies and credentials are not logged.

## Schema and API handoff

Migration **`0007_agent_steps`**, following `0006_approved_resolutions`, adds:

- `jobs.model_calls`, `tool_calls`, `agent_deadline_at` for durable budgets.
- `agent_steps`, keyed by `(job_id, seq)`, with validated arguments, safe result,
  creation time and completion time. A null result/completion time means pending.
- `executions.revalidated_at` and `invalidation_reason`, separate from the original
  planning evidence.

Revision **`0008_merge_agent_events`** joins the unchanged
`0007_agent_steps` and `0007_conversation_events` revisions, both based on `0006`.
`alembic upgrade head` works from either branch and preserves existing checkpoints
and event records; no applied revision was renamed or rewritten.
The current head is **`0009_conversation_management`**, which adds the pin and
removal fields described in [Chat management](CHAT_MANAGEMENT.md).

Apply migrations before starting the new API/worker images. Keep API and workers
on the same release; old workers do not understand pending agent checkpoints.
Before rollout, the data/API owner should review this additive migration and the
public fields below. No applied migration was edited. Downgrade refuses to discard
agent steps; use a reviewed forward fix once audit records exist.

`GET /jobs/{job_id}/steps?after_seq=0` uses the existing authenticated job ownership
check and returns up to 100 **completed** steps in sequence order. Each row exposes
`seq`, `name`, `result`, `created_at`, `finished_at`. It omits model arguments,
internal lease fields and raw provider responses. Unknown and other-account jobs
both return 404. Advance the cursor to the last returned sequence. Pending steps
are withheld, so a reconnecting consumer cannot skip their later completion.
Completed steps remain immutable. Fixed-mode and approval jobs can return no
steps; their status/history endpoints remain authoritative.

PR #6's SSE transport is integrated on this branch. REST and SSE share the proposal
serializer in `history.py`, including revalidation time and invalidation reason.
The stream carries message, job, proposal and activity updates. History attaches
an `activity` snapshot to each user message: job ID, status, attempt count and
public tool steps. The chat renders these as a muted timeline below the request.
Labels describe executed tools and saved outcomes, not model reasoning. Arguments,
drafts and raw tool responses are not included in the public activity snapshot.

PR #7's durable replay is also integrated. Agent-generated proposals, terminal
replies, job transitions and approved resolutions append events in their business
transaction. Completion checks the lease and agent deadline after waiting for the
event counter, so late results roll back both business changes and events. Durable
invalidation events retain the existing broad `reason` and additionally expose
`invalidation_reason` and `revalidated_at` matching the execution audit record.
Clients opt into `version=2` using the history snapshot's `v2:` cursor; the existing
browser continues to use the compatible default stream. See [Live updates](LIVE_UPDATES.md).

Tool checkpoints now append `agent.step_started` and `agent.step_completed` in the
same transaction, under the final lease/deadline checks. `(job_id, step.seq)` is
the stable step identity. The default stream emits changed `activity` snapshots;
the browser refreshes authoritative history, so reconnects and reopening a chat
restore completed and pending steps without adding duplicate lines. Fast tools
may appear already completed on the next poll; no artificial delay is added.
The v2 stream replays individual saved transitions. Existing steps are shown in
history but are not backfilled into the event log. No schema migration is needed.
Step completion is durable progress, not proof of an applied refund. The proposal
and continuation status provide the business outcome.

Proposal history includes optional `revalidated_at` and `invalidation_reason`.
Reasons are `PROPOSAL_EXPIRED`, `FACTS_UNAVAILABLE`, `OWNERSHIP_CHANGED`,
`TICKET_ORDER_CHANGED`, `TICKET_CLOSED`, `ALREADY_REFUNDED`, `POLICY_CHANGED`,
`ORDER_CHANGED`, or `ELIGIBILITY_CHANGED`. Existing generic user messages remain
safe; these codes support audit and UI explanations without exposing other-account
facts. Old rows can have null fields.

## Stale approval rule

Approval authorizes the immutable saved proposal and its saved business facts.
Execution locks the current order and ticket, checks ownership/linkage, compares
policy and order facts, checks expiry, recomputes eligibility and amount, and
checks that no prior refund exists. A mismatch invalidates the old proposal.
Preparing a new proposal creates a new identity and requires a new decision.
Retrying the old approval returns its original receipt and cannot approve the new
proposal. An earlier recheck at the approval click would improve feedback but
cannot replace this transaction-time check.

The refund record, exact approved customer reply, ticket update, final chat reply
and successful continuation commit atomically in PostgreSQL. This demonstrates
at-least-once work attempts with deduplicated simulated effects. It does not claim
atomicity with a future external payment or messaging service.

## Verification and evaluation

Run ordinary CI checks as documented in [the handoff](DEVELOPMENT_HANDOFF.md).
The added tests use a scripted provider through the same dispatcher as live mode,
the real API, and isolated PostgreSQL. They cover malformed/unauthorized decisions,
budget persistence, stale claim fencing, checkpoint replay and stable progress
cursors. SDK request tests use an offline HTTP transport.

The revalidation suite changes facts before and after approval, exercises natural
refund-window expiry, waits for a concurrent order update, and requires renewed
approval after invalidation. Process tests start a separate Python worker, kill
it during model work, after a saved read, before business commit, and after commit,
then race two replacement workers. They assert one action/reply and progress of a
later turn. They do not simulate loss of the PostgreSQL server itself.

See [evaluation instructions and evidence](evaluation/README.md). Deterministic
execution and synthetic scaling results must be reported separately from live
model tool selection and draft quality. Cloud deployment, full live model acceptance
and team schema review remain integration work.
