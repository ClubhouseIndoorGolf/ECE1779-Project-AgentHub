# Execution evaluation

The runner uses synthetic accounts/orders and the real API, queue, dispatcher and
PostgreSQL transactions. It requires `--reset-test-db` and refuses any database
whose name does not end in `_test`. Run it only against the disposable Compose
test database, with no concurrent tests or workers using that database.

```bash
docker compose --profile test build test
docker compose run --rm -v "$PWD/docs/evaluation:/reports" test \
  python -m evaluation.run --reset-test-db --mode deterministic \
  --revision "$(git rev-parse HEAD)" --output /reports/deterministic.json
```

The deterministic cases prescribe decisions for ordinary chat, clarification,
eligible proposals, ineligibility, missing information, inaccessible records and
approval bypass. These demonstrate execution behavior, not language understanding.
The eligible case checks no effect before approval and completion after an explicit
synthetic human decision. Fault and stale-data coverage belongs to the integration
tests, not these seven route cases.

The scaling experiment runs 12 independent conversations per trial with a 100 ms
scripted provider delay, one excluded warm-up and three measured trials each for
one and two worker threads using separate sessions and the production claim path.
It reports initial queue delay, submission-to-completion p50/p95 (nearest rank),
drain throughput and extra attempts. Human approval waiting is excluded. These
are local concurrent-worker measurements, not cloud-container scaling evidence.

For a small live evaluation, privately export a key in the shell and pass it by
environment name; do not put the value in commands or reports:

```bash
docker compose run --rm -e OPENAI_API_KEY -e OPENAI_MODEL \
  -v "$PWD/docs/evaluation:/reports" test \
  python -m evaluation.run --reset-test-db --mode live \
  --revision "$(git rev-parse HEAD)" --output /reports/live.json
```

Unset `OPENAI_MODEL` to use the application default; do not export it as an empty
string. Live mode is seven labeled cases, with the configured six reserved model
calls per initial job and the existing attempt/deadline limits. The runner records
steps, arguments, synthetic results/drafts, timing, settings, revision and reported
token usage for completed responses. Failed or interrupted requests can incur
usage that the provider did not return. Keep this opt-in and outside normal CI.

Automated route checks are a coarse signal: manually review target IDs, grounding,
clarification quality, exact proposed wording and refusal claims. The bypass case
checks approval enforcement; it does not score the prose of a refusal. Do not
describe scripted route scores as live model accuracy or as a guarantee of safety
for arbitrary prompts. The application authorization boundary is tested separately.

`deterministic.json` is generated evidence for this implementation. No live model
report is included until a real provider run has been performed.

## Recorded local verification

Implementation revision: `e081fba67bd86b6626e37fd916e16c89bdc93674`.

| Check | Observed result |
| --- | --- |
| Backend integration suite, isolated PostgreSQL | 138 passed; includes four real process-kill cases |
| Browser state suite | 12 passed |
| Offline deployment regressions in Linux | 48 passed |
| Ruff lint and format | Passed, 35 Python files |
| Compose configuration and runtime/test image builds | Passed |
| Running fixed-mode API/database/worker smoke | Passed, including duplicate-submission check |
| Scripted evaluation | 7/7 passed, no unapproved effects; approved eligible case completed |
| Synthetic worker benchmark | Median 7.46 jobs/s with one worker and 12.14 with two; three trials each, zero extra attempts |
| Live model / cloud deployment | Not run; no local API key configured |

The machine's PostgreSQL container clock was observed moving backward three times
over 55 seconds (about 0.8–1.6 seconds each). Earlier test runs consequently hit
intermittent immediate-claim timing failures, including an existing baseline test.
The process harness now polls like the real worker, and the natural-expiry test
waits for database time. Production scheduling and lease fencing were not relaxed.
The final full suite passed. Database-timestamp latency measurements should be
repeated on a stable-clock host before making course-report reliability/scaling
claims; the saved benchmark is local synthetic evidence, not a production result.

The suite also reports an upstream FastAPI/Starlette `TestClient` deprecation
warning about HTTPX; it does not fail these tests. No dependency upgrade was made
as part of the execution work.

## Integration with main's SSE release

Merged `origin/main` at `3d5bf59` (PR #6) into
`feature/agent-execution-completion`. The shared history serializer now carries
the agent's revalidation fields for both REST and SSE. Agent tool checkpoints and
the authenticated steps endpoint remain intact; no migration collision occurred.

Fresh merged-source checks: **157 backend tests passed**, **20 browser tests
passed**, Ruff lint/format passed (38 Python files), Compose configuration and
image builds passed, and the running API/worker duplicate-submission smoke passed.
A real HTTP SSE stream also replayed the user message and delivered the worker's
assistant message and successful job event. Added regressions verify that agent
proposal completion and stale-approval invalidation match REST, live events and
replay, including the revalidation timestamp and reason.

Earlier integration runs encountered the already observed immediate-claim timing
issue and an SSE test timer race. SSE tests now establish the stream baseline
explicitly before committing worker results and poll for expected work with a
bounded wait. Production scheduling, lease checks and stream behavior were not
relaxed. The final complete run passed; the local clock caveat above still applies.
The earlier benchmark artifact remains tied to its recorded implementation
revision and was not relabeled as a new benchmark. No live provider calls or cloud
deployment were performed during this merge.

## November 18 integration with durable replay

Merged `origin/main` at `eaec131`, including PR #7's durable SSE replay and the
application/data backlog. Resolved overlapping worker changes so agent proposals,
replies and approved resolutions record their events in the same fenced business
transaction. Detailed invalidation evidence remains available through history and
the durable invalidation payload. An expired agent deadline rolls back terminal
checkpoints, proposals and their events together.

The new schema head `0008_merge_agent_events` joins the unchanged independent
`0007_agent_steps` and `0007_conversation_events` revisions. Regression tests upgrade
from each branch with populated records and verify preservation. The event-store
migration fixture targets its own branch when downgrading, retaining agent audit
fields. The running local stack upgraded from the previous agent schema to this
single merge head successfully.

Fresh checks on the merged source: **178 backend tests passed**, **20 browser
tests passed**, Ruff lint/format passed (43 Python files), Compose configuration
and both image builds passed, and the running fixed-mode API/worker smoke passed
including duplicate submission. A real HTTP v2 stream verified the history
snapshot cursor, five ordered message/job events and an exact reconnect replay
after `v2:3`. This was local container verification; no paid model calls, cloud
deployment or new scaling benchmark was performed. The existing upstream
TestClient deprecation warning remains non-failing.
