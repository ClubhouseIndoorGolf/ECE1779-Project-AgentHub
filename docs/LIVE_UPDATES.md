# Live updates (Server-Sent Events)

The browser receives conversation changes over a Server-Sent Events (SSE) stream instead of polling job status every 750 ms. Polling remains as an automatic fallback. This feature adds no schema change, migration or dependency.

## Endpoint

`GET /conversations/{conversation_id}/events` returns `text/event-stream`.

- **Authentication and ownership.** The endpoint uses HTTP Basic, like every other endpoint. It checks ownership once before streaming. An unknown conversation, or one owned by another account, returns `404`. Missing or invalid credentials return `401`.
- **Resume cursor.** Send `Last-Event-ID: <seq>` with the last message sequence received. The stream replays later messages in order. Without the header, or with an invalid value, the stream starts at the conversation's current last message. The client loads earlier history through `GET /conversations/{id}/messages`.
- **Lifetime.** Each stream closes after 300 seconds so load balancers can rebalance connections. The client then reconnects with `Last-Event-ID`. The stream sends a `: keepalive` comment after 15 seconds without events.
- **Database use.** Every 0.5 seconds the stream reads committed rows in a short read-only session. It does not hold a database connection between reads. Tunables are module attributes in [events.py](../backend/app/events.py).

## Events

| Event | `id` | `data` |
| :-- | :-- | :-- |
| `ready` | Current cursor | `{"cursor": <seq>}`. Sent once per connection. |
| `message` | Message `seq` | The same object as one row of `GET /conversations/{id}/messages`, including any `proposal`. |
| `job` | None | Public job fields (`id`, `source_message_id`, `kind`, `status`, `attempts`, `last_error`, `created_at`, `finished_at`). Sent when a job's status or attempt count changes. Lease fields are never included. |
| `proposal` | None | The public proposal object. Sent when a proposal appears or its status or decision changes. |
| `error` | None | `{"detail": "Database temporarily unavailable"}`. The stream then closes, and the client reconnects with backoff. |

Each connection tracks the state it has already sent, so it sends a job or proposal only when it changes. A reconnect resends current active jobs, so listeners should treat events as idempotent.

## Browser client

[chat.js](../backend/app/static/chat.js) reads the stream with `fetch` and a body reader. It does not use `EventSource`, which cannot send the `Authorization` header.

- **Starting and stopping.** Opening or creating a conversation starts its stream. Starting a new chat or signing out closes it.
- **Reconnection.** After a normal close, the client reconnects within 250 ms. After a failure, it backs off from 1 s to at most 15 s with jitter. A stream with no data for 45 seconds is treated as dropped. A `401` or `404` stops the stream without retrying.
- **Reauthentication.** A `401` from the event stream or a history refresh it triggered stops live updates, cancels scheduled refresh retries, and opens the sign-in dialog. The client compares the user ID returned by `GET /me` with the previous user's ID. The same account keeps its open conversation and draft and reconnects. A different account starts with an empty chat and loads its own conversation list. The previous account's messages, drafts, and pending submission state are cleared, and late responses from its workspace are ignored.
- **Using events.** `job` events complete message sends and approved refund continuations immediately. `message`, `proposal` and `ready` events trigger one debounced history refresh, which reuses the existing stale-response guards.
- **Fallback.** While no stream is live, or where the browser lacks body streams, the client polls `/jobs/{id}` as before. When a stream is live, it confirms a job over REST if no event arrives within 5 seconds.
- **Status.** The footer shows **Live updates** (green), **Reconnecting…** (amber) or **Local workspace**.

## Verification

Automated coverage:

- [test_events.py](../backend/tests/test_events.py) covers ownership, the fresh cursor, `Last-Event-ID` replay, live job and reply events, and proposal approval progress.
- [test_chat_client.cjs](../backend/tests/test_chat_client.cjs) covers completing a send from a pushed job event without polling, parsing a frame split across chunks, reconnecting with `Last-Event-ID`, and the polling fallback after `404`. It also verifies draft retention after signing back in to the same account and workspace isolation when a different account signs in during an in-flight send.

Manual check against the running stack, with credentials from your private `.env`:

```bash
curl -sN -u "$BOOTSTRAP_USERNAME:$BOOTSTRAP_PASSWORD" \
  http://127.0.0.1:8000/conversations/<conversation-id>/events
```

Send a message in the browser for that conversation. The terminal shows `message` and `job` frames as they are committed. In the browser, the footer shows **Live updates**. If you stop the API with `docker compose stop api`, the footer changes to **Reconnecting…**. After `docker compose start api`, the stream resumes, and messages saved during the outage appear without a page reload.
