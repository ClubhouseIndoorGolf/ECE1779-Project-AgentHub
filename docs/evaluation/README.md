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
