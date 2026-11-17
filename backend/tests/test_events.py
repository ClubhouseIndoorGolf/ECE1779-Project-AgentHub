# Verify the conversation event stream: ownership, replay cursor, job progress and proposal changes.
# 验证会话事件流：归属检查、续传游标、任务进度与方案状态变化。

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest

from app import events, history
from app.db import SessionLocal
from app.event_store import EventToAppend, append_events
from app.manage import seed_demo_orders
from app.models import ConversationEventHead
from app.worker import run_once


def submit(client, content="Hello"):
    conversation = client.post("/conversations", json={}).json()["id"]
    payload = {"content": content, "submission_key": str(uuid4())}
    response = client.post(f"/conversations/{conversation}/messages", json=payload)
    assert response.status_code == 202, response.text
    return conversation, payload, response.json()


@pytest.fixture(autouse=True)
def short_streams(monkeypatch):
    """Keep each stream finite so the synchronous test client can read it to the end.
    让每个事件流在有限时间内结束，使同步测试客户端可以完整读取。
    """
    monkeypatch.setattr(events, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(events, "MAX_STREAM_SECONDS", 0.6)


def read_stream(client, conversation, last_event_id=None, **kwargs):
    """Read one bounded stream and return parsed (event, id, data) frames.
    读取一个有时限的事件流，并解析为 (事件名, id, 数据) 列表。
    """
    if last_event_id is not None and not isinstance(last_event_id, bytes):
        last_event_id = str(last_event_id)
    headers = {} if last_event_id is None else {"Last-Event-ID": last_event_id}
    response = client.get(f"/conversations/{conversation}/events", headers=headers, **kwargs)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = []
    for block in response.text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line and not line.startswith(":"):
                name, _, value = line.partition(": ")
                fields[name] = value
        if "event" in fields:
            frames.append((fields["event"], fields.get("id"), json.loads(fields["data"])))
    return frames


def named(frames, event):
    return [(event_id, data) for name, event_id, data in frames if name == event]


@pytest.mark.parametrize("version", ["1", "2"])
def test_stream_requires_owner(client, version):
    """Another account and anonymous requests cannot open the stream.
    其他账号与未认证请求都不能打开事件流。
    """
    conversation = client.post("/conversations", json={}).json()["id"]
    url = f"/conversations/{conversation}/events?version={version}"
    assert client.get(url, auth=("bob", "bob-test-password")).status_code == 404
    client.auth = None
    assert client.get(url).status_code == 401


def test_fresh_stream_starts_at_current_end(client):
    """Without Last-Event-ID the stream reports the cursor and does not replay history.
    未提供 Last-Event-ID 时，事件流只报告当前游标，不重放历史。
    """
    conversation, _, _ = submit(client)
    assert run_once("events-worker")
    frames = read_stream(client, conversation)
    assert frames[0] == ("ready", "2", {"cursor": 2})
    assert named(frames, "message") == []


def test_replay_after_last_event_id(client):
    """Last-Event-ID replays later messages in order, and ignores malformed values.
    Last-Event-ID 按顺序补发之后的消息，格式无效时按新连接处理。
    """
    conversation, _, receipt = submit(client, content="你好 👋")
    frames = read_stream(client, conversation, last_event_id=0)
    assert frames[0] == ("ready", "0", {"cursor": 0})
    messages = named(frames, "message")
    assert [(event_id, data["content"]) for event_id, data in messages] == [("1", "你好 👋")]
    assert "proposal" not in messages[0][1]
    jobs = named(frames, "job")
    assert [(data["id"], data["status"], data["kind"]) for _, data in jobs] == [
        (receipt["job_id"], "QUEUED", "PROCESS_MESSAGE")
    ]
    assert "lease_token" not in jobs[0][1]

    assert run_once("events-worker")
    frames = read_stream(client, conversation, last_event_id=1)
    assert [(i, d["role"], d["content"]) for i, d in named(frames, "message")] == [
        ("2", "assistant", "Message received.")
    ]
    assert [d["status"] for _, d in named(frames, "job")] == ["SUCCEEDED"]
    assert read_stream(client, conversation, last_event_id="not-a-number")[0][2] == {"cursor": 2}


def test_live_job_progress_and_reply(client):
    """Changes committed while the stream is open arrive as job and message events.
    事件流打开期间提交的变化会以任务事件与消息事件推送。
    """
    conversation, _, receipt = submit(client)
    worker = threading.Timer(0.2, run_once, args=("events-worker",))
    worker.start()
    frames = read_stream(client, conversation)
    worker.join()
    assert frames[0][0] == "ready"
    statuses = [d["status"] for _, d in named(frames, "job") if d["id"] == receipt["job_id"]]
    assert statuses[0] == "QUEUED" and statuses[-1] == "SUCCEEDED"
    assert [d["content"] for _, d in named(frames, "message")] == ["Message received."]


def test_proposal_changes_are_streamed(client):
    """A new proposal and its approved execution are pushed without a history refresh.
    新方案及批准后的执行进度无需刷新历史即可推送。
    """
    seed_demo_orders("alice", "bob", with_tickets=True)
    conversation, _, _ = submit(client, content="处理工单 391，符合条件就准备退款")
    worker = threading.Timer(0.2, run_once, args=("events-worker",))
    worker.start()
    frames = read_stream(client, conversation)
    worker.join()
    proposals = named(frames, "proposal")
    assert [d["status"] for _, d in proposals] == ["WAITING_FOR_APPROVAL"]
    proposal = proposals[0][1]

    decision = client.post(
        f"/proposals/{proposal['id']}/decision", json={"decision": "APPROVE", "version": proposal["version"]}
    )
    assert decision.status_code == 200, decision.text

    def resume():
        time.sleep(0.2)
        run_once("events-worker")

    worker = threading.Thread(target=resume)
    worker.start()
    frames = read_stream(client, conversation, last_event_id=1)
    worker.join()
    assert named(frames, "proposal")[-1][1]["status"] == "COMPLETED"
    assert {d["kind"] for _, d in named(frames, "job")} >= {"RESUME_EXECUTION"}


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("", None),
        ("7", 7),
        (" 12 ", 12),
        ("-1", None),
        ("x", None),
        # Unicode digits that str.isdigit() accepts but are not ASCII sequence numbers.
        # str.isdigit() 接受但并非 ASCII 顺序号的 Unicode 数字字符。
        ("²", None),
        ("١٢", None),
        (str(2**63 - 1), 2**63 - 1),
        (str(2**63), None),
        ("9" * 20, None),
    ],
)
def test_parse_last_event_id(value, expected):
    assert events.parse_last_event_id(value) == expected


def test_unicode_last_event_id_starts_fresh(client):
    """A cursor that int() cannot parse is treated as a new connection instead of failing.
    int() 无法解析的游标按新连接处理，而不是返回服务器错误。
    """
    conversation, _, _ = submit(client)
    # Send raw Latin-1 bytes as a browser or proxy could; the server decodes them back to "²".
    # 像浏览器或代理那样发送原始 Latin-1 字节；服务端会将其解码回 "²"。
    assert read_stream(client, conversation, last_event_id="²".encode("latin-1"))[0] == (
        "ready",
        "1",
        {"cursor": 1},
    )


def test_durable_history_cursor_is_only_on_the_first_page(client):
    """Expose the event head on a fresh snapshot while retaining the history list shape.
    为新快照附加事件游标，同时保持历史响应为列表。
    """
    conversation = client.post("/conversations", json={}).json()["id"]
    url = f"/conversations/{conversation}/messages"
    empty_page = client.get(url)
    assert empty_page.status_code == 200 and empty_page.json() == []
    assert empty_page.headers["X-Conversation-Event-Cursor"] == "v2:0"
    assert client.post(url, json={"content": "Hello", "submission_key": str(uuid4())}).status_code == 202
    assert run_once("snapshot-worker")

    # The event head covers the snapshot, even when more messages need to be paged in.
    # 即使还需要翻页，事件游标也对应整个会话的当前快照。
    first = client.get(url, params={"limit": 1})
    assert first.headers["X-Conversation-Event-Cursor"] == "v2:5"
    assert [row["seq"] for row in first.json()] == [1]
    more = client.get(url, params={"after_seq": 1, "limit": 1})
    assert "X-Conversation-Event-Cursor" not in more.headers
    assert [row["seq"] for row in more.json()] == [2]


@pytest.mark.parametrize(
    "cursor",
    [None, "0", "v2:", "v2:-1", b"v2:\xb2", "v2:9223372036854775808", "v2:1"],
)
def test_invalid_durable_cursor_returns_422_before_streaming(client, cursor):
    """Reject legacy, malformed, and future cursors in the durable endpoint.
    在新版接口拒绝旧版、格式错误和超前的游标。
    """
    conversation = client.post("/conversations", json={}).json()["id"]
    headers = {} if cursor is None else {"Last-Event-ID": cursor}
    response = client.get(f"/conversations/{conversation}/events?version=2", headers=headers)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")


def test_durable_stream_paginates_and_resumes_exclusively(client, monkeypatch):
    """Replay each stored event in order, then resume after the last complete frame.
    按顺序回放每条已保存的事件，再从最后一条完整帧之后续传。
    """
    monkeypatch.setattr(events, "REPLAY_PAGE", 2)
    conversation, _, _ = submit(client, content="你好 👋")
    assert run_once("replay-worker")
    frames = read_stream(client, conversation, "v2:0", params={"version": "2"})
    assert frames[0] == ("ready", None, {"cursor": "v2:0"})
    durable = frames[1:]
    assert [(name, cursor) for name, cursor, _ in durable] == [
        ("message.created", "v2:1"),
        ("job.status_changed", "v2:2"),
        ("job.status_changed", "v2:3"),
        ("message.created", "v2:4"),
        ("job.status_changed", "v2:5"),
    ]
    for seq, (name, _, data) in enumerate(durable, start=1):
        assert (data["seq"], data["type"], data["conversation_id"]) == (str(seq), name, conversation)
        assert data["created_at"]
    assert [data["payload"]["status"] for _, data in named(frames, "job.status_changed")] == [
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
    ]
    assert durable[0][2]["payload"]["message_seq"] == 1
    assert "content" not in durable[0][2]["payload"]
    resumed = read_stream(client, conversation, durable[2][1], params={"version": "2"})
    assert resumed == [("ready", None, {"cursor": "v2:3"}), *durable[3:]]


def test_durable_cursor_keeps_bigint_precision(client):
    """Use strings for the cursor and sequence even at the PostgreSQL limit.
    即使达到 PostgreSQL 序号上限，也以字符串传输游标和序号。
    """
    conversation = client.post("/conversations", json={}).json()["id"]
    high = 2**63 - 1
    with SessionLocal.begin() as session:
        session.get(ConversationEventHead, UUID(conversation)).last_event_seq = high - 1
        append_events(session, UUID(conversation), [EventToAppend("proposal.created", {})])

    response = client.get(f"/conversations/{conversation}/messages")
    assert response.headers["X-Conversation-Event-Cursor"] == f"v2:{high}"
    frames = read_stream(client, conversation, f"v2:{high - 1}", params={"version": "2"})
    [(cursor, data)] = named(frames, "proposal.created")
    assert (cursor, data["seq"]) == (f"v2:{high}", str(high))


def test_approval_during_history_snapshot_and_before_connect_is_replayed(client, monkeypatch):
    """Commit approval between the history and cursor reads, then replay it with the resolution.
    在历史读取与游标读取之间提交审批，再回放审批及执行结果。
    """
    seed_demo_orders("alice", "bob", with_tickets=True)
    conversation, _, _ = submit(client, content="处理工单 391，符合条件就准备退款")
    assert run_once("snapshot-worker")
    url = f"/conversations/{conversation}/messages"
    proposal = client.get(url).json()[0]["proposal"]
    page_read = threading.Event()
    allow_cursor_read = threading.Event()
    original_read_history = history.read_history

    def pause_after_history(*args):
        """Stop after the page query so a separate transaction can submit the approval.
        在历史页查询后暂停，让独立事务提交审批。
        """
        page = original_read_history(*args)
        page_read.set()
        assert allow_cursor_read.wait(timeout=5)
        return page

    with monkeypatch.context() as snapshot_patch, ThreadPoolExecutor(max_workers=1) as pool:
        snapshot_patch.setattr(history, "read_history", pause_after_history)
        future = pool.submit(client.get, url)
        try:
            assert page_read.wait(timeout=5)
            decision = client.post(
                f"/proposals/{proposal['id']}/decision",
                json={"decision": "APPROVE", "version": proposal["version"]},
            )
            assert decision.status_code == 200, decision.text
        finally:
            allow_cursor_read.set()
        snapshot = future.result(timeout=5)

    assert snapshot.status_code == 200
    assert snapshot.json()[0]["proposal"]["status"] == "WAITING_FOR_APPROVAL"
    cursor = snapshot.headers["X-Conversation-Event-Cursor"]
    assert cursor == "v2:5"
    assert run_once("resolution-worker")

    # Complete the refund before opening the stream. Replay still exposes the original decision and progress.
    # 开流前完成退款，回放仍应呈现原始审批决定和执行进度。
    frames = read_stream(client, conversation, cursor, params={"version": "2"})
    assert [(name, event_id) for name, event_id, _ in frames] == [
        ("ready", None),
        ("proposal.decided", "v2:6"),
        ("job.status_changed", "v2:7"),
        ("job.status_changed", "v2:8"),
        ("resolution.completed", "v2:9"),
        ("message.created", "v2:10"),
        ("job.status_changed", "v2:11"),
    ]
    assert frames[1][2]["payload"]["decision"] == "APPROVE"
    assert frames[3][2]["payload"]["status"] == "RUNNING"
    assert frames[4][2]["payload"]["status"] == "COMPLETED"
