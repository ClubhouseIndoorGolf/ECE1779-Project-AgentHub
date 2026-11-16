# Agent execution completion plan

Reviewed on November 15, 2026 against `main` at `506bc66`. Working branch:
`feature/agent-execution-completion`. This is a repository assessment and proposed
implementation sequence, not a claim that the remaining features are implemented.

Implementation update: the subsequent work on this branch is described in
[Agent execution and integration contract](AGENT_EXECUTION.md), with separate
[evaluation evidence](evaluation/README.md). The assessment below preserves the
original reviewed baseline; its verification counts refer to that earlier revision.

## Assessment

The durable worker and deterministic refund workflow are already substantial.
The main missing product capability is a model that can select and use the bounded
business tools. Live mode currently generates text only; fixed mode recognizes
documented commands and executes the refund workflow. The other major gap is
evaluation evidence for a complete agent execution under interruptions.

The instructor's stale-approval scenario already has an execution-time safeguard
and some regression coverage. Extend and demonstrate it; do not replace the
existing approval and transaction design.

## What exists now

| Area | Implemented evidence | Remaining boundary |
| --- | --- | --- |
| API and data | Account-scoped conversations, idempotent submission, ordered messages, initial and continuation jobs, proposal decisions and history in `main.py`, `models.py` and migrations | New tool-step/event contracts need coordination with the application/data owner |
| Worker | PostgreSQL claims with `SKIP LOCKED`, renewable leases, claim tokens, ordered turns, bounded model retries and shutdown handling in `worker.py` | No model-driven multi-step executor or durable tool-call history |
| Business rules | Ticket/order ownership checks, deterministic CAD refund policy, validated `PrepareRefund`, stored proposal facts, explicit approval/rejection | Fixed-mode request parsing is intentionally narrow; natural-language tool selection is absent |
| Approved execution | Order/ticket locks, fresh eligibility check, unique applied-resolution records, atomic business changes, reply and job completion | Broader stale-input tests and actual process-crash evidence are still needed |
| Model integration | Bounded text input/output, request timeout, sanitized errors, SDK retries disabled, offline SDK tests | `OpenAIReply.generate()` sends no tools; its prompt explicitly says business tools are unavailable |
| UI and delivery | Static chat/approval client, Compose, CI, Swarm packaging, deployment helpers, image publication and monitoring scripts | SSE is still open PR #6 at this snapshot; cloud operation, HTTPS and backups remain delivery-team integration/evidence work |

Sources: [worker](../backend/app/worker.py), [reply providers](../backend/app/replies.py),
[support workflow](../backend/app/support_workflow.py),
[approved resolution](../backend/app/resolution_workflow.py),
[API](../backend/app/main.py), [models](../backend/app/models.py),
[proposal](AgentHub_Project_Proposal.md), [cloud guide](CLOUD_DEPLOYMENT.md).
Some older overview documents still describe already implemented work as planned;
use this revision's code and tests as the implementation baseline.

## Instructor feedback: stale approvals

The intended invariant should be explicit:

> Approval authorizes the saved resolution for its saved business facts. It does
> not authorize a replacement amount, target, reply or policy decision selected
> later by the worker or model.

`prepare_refund()` stores the amount/currency, exact customer reply, target ticket,
expected order facts, policy version and expiration. `apply_approved_resolution()`
then locks the order and ticket, checks ownership and linkage, rejects resolved
tickets and previous refunds, compares saved facts and policy version, and
recalculates eligibility using database time. A mismatch produces `INVALIDATED`
without applying the action. This already addresses the instructor's principal
execution-time concern.

For example, approving CAD 129.99 must not silently authorize CAD 99.99 after an
order changes, even if the new order remains eligible. The existing conservative
behavior is appropriate: invalidate, prepare a fresh proposal from current facts,
and obtain a new human decision.

The approval API currently checks ownership, proposal version, status and expiry;
it does not re-read order eligibility at the approval click. An earlier recheck
could improve feedback, but it cannot replace the check inside the execution
transaction: the order can change while an approved job is queued.

`PROPOSAL_VERSION` is currently a constant and proposals have no application edit
endpoint. Preserve that immutability. If replanning/editing is introduced, create
a new proposal identity or a persisted content revision; never overwrite an
already approved proposal or carry its approval forward.

### Expand the deterministic regression matrix

The existing [resolution tests](../backend/tests/test_resolution_workflow.py)
cover expiry before approval, expiry after approval, an amount change after
approval, competing proposals, stale leases and rollback before completion.
Add the following cases with explicit assertions that no refund, ticket update
or successful-action reply is committed:

| Change or race | Expected result |
| --- | --- |
| Order facts change while waiting for approval, then the old proposal is approved | Execution rejects the old proposal; optionally detect the mismatch earlier in the approval API |
| Payment, delivery status, currency or delivery time changes after approval | Old proposal becomes invalid |
| Ticket is resolved/relinked, or ticket/order ownership changes | No action under the old authorization |
| Refund window ends naturally while the approved job waits | Invalidated using current time; test the policy boundary rather than only manually expiring the proposal |
| Policy version changes between planning and execution | Invalidate and require a new proposal/decision |
| Another transaction changes the order while execution waits for its lock | Recheck the committed facts after obtaining the lock |
| Two workers apply approved proposals for the same order | Exactly one applied resolution; the losing proposal leaves no partial business changes |
| A fresh proposal follows invalidation | New facts and new explicit approval; retries of the old decision retain the original receipt |

Store a safe, machine-readable invalidation reason and revalidation time as part
of the agreed audit contract. Keep original planning evidence separate from the
execution-time conclusion. The current generic final messages and original
eligibility reason codes are insufficient for a detailed evaluation trace.

## Recommended implementation sequence for agent execution

### 1. Lock down stale-approval behavior and integration contracts

Start with the regression matrix above, using the existing transaction boundary.
Agree with the data/API owner on immutable proposals, public execution states,
tool-step records, invalidation reasons and event payloads before adding a new
Alembic revision. Do not alter applied migrations.

Completion evidence: deterministic tests cover changes before approval, after
approval and during lock contention; all invalid cases leave business data intact.

### 2. Connect the model to the existing business capabilities

Replace the text-only provider result contract with validated decisions for
ordinary reply, clarification, permitted read-only tool requests and preparation
of a resolution. Keep the workflow small: ticket/order lookup, eligibility and
the proposed resolution, followed by the existing approved continuation.

Use an explicit dispatcher with fixed allowed names and strict argument schemas.
Derive the account, conversation and execution identity from persisted state;
never accept model-supplied ownership, refund amounts or approval flags as authority.
Reuse `lookup_ticket()`, `evaluate_refund_eligibility()` and `PrepareRefund` rather
than duplicating business policy inside prompts. Model-generated customer replies
remain visible in the exact proposal the human approves.

The model must not directly apply business writes. The saved approval and
`RESUME_EXECUTION` path remain the only route to applying a resolution. Unknown
tools, malformed arguments and instructions to bypass approval must fail safely.
Clarification ends the current turn without a business mutation and lets a later
turn provide missing information.

Keep model calls outside database transactions. Set small explicit limits for
model decisions, tool calls, input/output and active execution time. Persist
budget consumption across retries so worker replacement cannot reset those limits.
Human approval waiting time is a separate phase, not model-running time.

Completion evidence: natural-language requests reach a saved approval card through
the real dispatcher; ordinary chat and ambiguous requests do not write business
data; malformed and unauthorized calls cannot cross the application boundary.

### 3. Persist the small agent loop and its progress

The existing `Execution` records one eligibility result per source message; it
is not a general tool-call log. Add only the state this workflow needs: ordered
step identity, validated arguments, safe result/reference, status, timestamps,
attempt/budget information and the next resumable step. Reuse the existing
execution identity where possible instead of creating a second competing state
machine. Coordinate the exact schema with the data owner.

Persist completed steps and progress events under lease fencing. On recovery,
continue from durable state; a stale worker must not write checkpoints or events.
Saved read results may support reasoning, but cannot substitute for fresh checks
before executing an approved refund. Do not hold a worker lease while waiting
for a human decision; the existing separate continuation job is the right pattern.

Agree on event kinds and stable ordering with the SSE owner. The worker produces
durable progress; the API owns authenticated delivery and the UI owns rendering
and reconnect behavior. Do not implement a second SSE transport on this branch.

Completion evidence: restarting a worker retains completed steps and the exact
pending proposal; reconnecting clients can recover public progress from storage.

### 4. Demonstrate crash recovery and bound operational failures

Keep the current atomic boundary: the simulated refund, customer reply, ticket
update, final chat reply and job success commit together. The proposal UUID is
the action key, with unique order/ticket constraints providing additional guards.

In this implementation, a committed refund with an uncommitted worker completion
is not a separate durable state: both are in one transaction. Before commit, a
crash rolls everything back; after commit, the job is already successful. Test
both sides with a real worker process, in addition to existing injected exceptions
and manually expired leases. Describe this as at-least-once work attempts with
atomic, deduplicated simulated effects, not a guarantee that a model is called once.

If a separate simulated-service API is introduced later, its stable-key request
must return the original saved result after a committed action and a lost response.
That boundary needs its own test; local atomicity alone would not prove it. A
separate service is not necessary merely to answer the instructor's feedback.

Add process-level checks for death during a model call, after a tool checkpoint,
before the refund commit and immediately after commit, plus two-worker takeover.
Assert one durable action/reply, bounded attempts and eventual progress of later
conversation turns.

Review transient database handling as part of this work: `run_once()` currently
leaves approved continuations reclaimable on `OperationalError`, but attempts to
mark initial jobs failed. Define a bounded retry policy for recoverable database
failures, add lock/statement time limits where appropriate, and test terminal
failure visibility. Do not retry malformed model output or policy denials blindly.

### 5. Produce separate execution and model evaluations

| Evaluation | Method | What it demonstrates |
| --- | --- | --- |
| Deterministic execution | A scripted model adapter emits fixed decisions through the same dispatcher as live mode; use isolated PostgreSQL, fault injection and multiple workers | Approval enforcement, ownership, state transitions, stale-data rejection, recovery and duplicate-action prevention |
| Live model behavior | A small labeled set of ordinary chat, paraphrases, ambiguous requests, missing information, eligible/ineligible tickets and attempted approval bypasses | Routing, clarification, tool/argument selection and grounded drafting quality |
| Worker scaling | Repeat the same workload with one and two workers, multiple independent conversations and controlled service time | Queue delay, completion time, throughput and recovery time without conflating model/network variation |

The existing fixed-mode regex workflow is a useful demo fallback, but it does not
by itself test the future model-driven dispatcher. Preserve it and add a scripted
adapter for the shared execution path. Live runs should be opt-in, use synthetic
data and a bounded budget, and stay outside ordinary CI.

Record revision, model/configuration, case IDs, counts, failures, latency and usage.
Report model route/tool accuracy separately from deterministic safety assertions.
Use `started_at - created_at` for initial job queue delay and
`finished_at - created_at` for job completion latency; report retries and human
approval waiting separately. Single-conversation turns intentionally serialize,
so the scaling workload must include independent conversations. Report median and
tail latency and repeat runs instead of claiming speedup from one sample.

Add structured, sanitized execution/job/step identifiers, attempts, durations and
outcome reasons to worker logs. Model secrets and raw provider error bodies must
remain excluded. Infrastructure metrics remain the cloud owner's responsibility.

## Ownership and completion boundary

Agent execution owns the typed model adapter, bounded dispatcher, worker-side
checkpoints/progress, business-rule invocation, revalidation and recovery/evaluation
harnesses. The data/API owner reviews migrations and public contracts; the UI/SSE
owners consume those contracts; the cloud owner supplies deployment and scaling
environments. Keep one refund workflow and the existing PostgreSQL queue. RAG,
additional agents, arbitrary integrations and a general workflow engine are not
needed for this end state.

The agent-execution work is complete when a natural-language request can gather
authorized facts, explain missing information or ineligibility, persist an exact
proposal, wait for a human, revalidate current facts and apply one simulated
resolution, while surviving worker interruption and producing separate deterministic
and live-model evaluation evidence.

## Verification for this review

- Fresh isolated Compose backend run at the reviewed revision: **87 passed**.
- Fresh offline browser-state run: **12 passed**.
- No live-model calls, cloud deployment, process-kill experiment or new scaling
  benchmark was performed for this assessment. Those are proposed work above.
- The branch contains this plan and a documentation-index entry; runtime code
  and schema were not changed as part of the review.
