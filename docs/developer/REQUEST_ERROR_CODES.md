# Chat Request Validation and Conflict Responses

AgentHub returns HTTP `422` when a conversation title or message contains a NUL character. Message submission and proposal decision conflicts return HTTP `409` with the existing English `detail` and a machine-readable `code`. These responses let API clients distinguish input errors, actions blocked by another turn, and stale or conflicting decisions. [API quick reference](../../README.md#handle-invalid-input-and-request-conflicts)

## Validate text before writing

`ConversationCreate.check_title()` and `MessageCreate.check_content()` reject `U+0000` during Pydantic field validation. In JSON, `{"title":"Order\u0000review"}` represents a title with one NUL character. The same rule applies to `content` in a message submission. PostgreSQL text fields cannot store that character. FastAPI returns its standard validation response before the endpoint writes any conversation, message, job, or event. The error location identifies `body.title` or `body.content`. [Input models](../../backend/app/schemas.py) · [Write endpoints](../../backend/app/main.py)

JSON `null` still follows the field's existing type rules. A valid title is trimmed, and valid message content retains its original whitespace and Unicode characters. Message idempotency compares the original content when a submitted key is reused. [Input validation and Unicode tests](../../backend/tests/test_backend.py)

## Read a conflict response

`POST /conversations/{conversation_id}/messages` and `POST /proposals/{proposal_id}/decision` raise `ConflictError` after their business checks. The registered handler serializes `ConflictOut` and sets HTTP `409`. OpenAPI lists that same model for both routes. [Exception handling and endpoints](../../backend/app/main.py) · [Response schema](../../backend/app/schemas.py)

```json
{
  "detail": "submission_key already used for different content",
  "code": "IDEMPOTENCY_CONFLICT"
}
```

Clients can branch on `code` and keep a generic message for unknown codes. `detail` remains available for compatibility. A single code can cover more than one existing detail string.

| Code | Trigger | Client action |
| --- | --- | --- |
| `IDEMPOTENCY_CONFLICT` | The same submission key is used with different content. | Preserve the draft. Use a fresh key for a new send the user confirms. |
| `CONVERSATION_BUSY` | An active refund proposal blocks a new message, or an earlier unfinished turn blocks a decision. | Refresh history to see which turn is blocking the action. |
| `PROPOSAL_EXPIRED` | This request closes an expired proposal, or a saved event shows that it expired earlier. | Refresh the proposal and prompt for a new one if needed. |
| `DECISION_CONFLICT` | A different decision is already saved for the proposal. | Refresh and display the saved decision. |
| `PROPOSAL_VERSION_CONFLICT` | The submitted version differs from the current proposal version. | Reload the proposal before asking the user to decide again. |
| `PROPOSAL_NOT_PENDING` | The proposal has left the approval stage and the API has no saved evidence of expiration. | Refresh and display the current state. |

## Preserve ownership and retries

The message endpoint locks the owned conversation and looks for the submission key before checking whether the conversation is paused. The same key and content return the original `202` receipt and job ID. A key reused with different content returns `IDEMPOTENCY_CONFLICT`. The busy check applies when the key represents a new message. [Message submission](../../backend/app/main.py)

The decision endpoint verifies ownership and the requested version, then checks for an existing receipt while holding the conversation and execution locks. The same version and decision return the saved `200` receipt, including its original `resume_job_id`. An opposing decision returns `DECISION_CONFLICT`. This receipt check comes before status and expiration checks, so a retry still succeeds if an already approved proposal later becomes invalid. A record outside the caller's account still returns `404`. [Decision endpoint](../../backend/app/main.py) · [Retry and concurrency tests](../../backend/tests/test_resolution_workflow.py)

## Commit expiration before reporting the conflict

When a decision request finds an overdue proposal still waiting for approval, it uses the database clock to check `expires_at`. Inside the transaction it saves `INVALIDATED`, appends the final assistant reply, and records `resolution.invalidated` followed by `message.created`. The transaction exits successfully before the route raises `ConflictError` with `PROPOSAL_EXPIRED`. If the event write fails, the state and reply roll back together and the API returns `503`. A later retry can finish the expiration. [Decision transaction](../../backend/app/main.py) · [Invalidation workflow](../../backend/app/resolution_workflow.py) · [Rollback regression](../../backend/tests/test_resolution_workflow.py)

The worker can also close a pending proposal before the decision request reaches it. For an already invalidated proposal with no saved decision, `read_invalidation_reason()` looks up the newest matching `resolution.invalidated` event using both the authorized conversation ID and proposal ID. If its `reason` is `EXPIRED`, the API returns `PROPOSAL_EXPIRED` with the existing detail `Proposal is no longer waiting for approval`. An older invalidated record without an event returns `PROPOSAL_NOT_PENDING`. These reads use the existing conversation event log. [Event lookup](../../backend/app/resolution_workflow.py) · [Event storage](../BACKEND_DATABASE.md#conversation-event-replay)

## Verify locally

Use `REPLY_MODE=fixed` and the isolated Compose PostgreSQL test service. From the repository root, build the test image and run the input and decision suites.

```bash
docker compose --profile test build test
docker compose run --rm test pytest -q tests/test_backend.py tests/test_resolution_workflow.py
```

The suites check that NUL validation leaves storage untouched, valid Unicode and whitespace survive, all public conflict codes carry their existing details, and identical retries return their original receipts. The approval tests cover expiration detected by the request or the worker, legacy records without an expiration event, and rollback when writing the event batch fails. To check the running API and worker, follow the fixed-mode smoke setup and repeat the duplicate submission example with a changed message body. [Local validation commands](../DEVELOPMENT_HANDOFF.md#validate-changes) · [Submission example](../../README.md#try-the-api)
