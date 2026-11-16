# Verify the conversation event stream: ownership, replay cursor, job progress and proposal changes.
# 验证会话事件流：归属检查、续传游标、任务进度与方案状态变化。

import json
import time
from uuid import UUID, uuid4

import pytest
from test_agent import workflow

from app import events
from app.db import SessionLocal
from app.manage import seed_demo_orders
from app.models import Order
from app.worker import run_once as worker_run_once


def run_once(*args, **kwargs):
    """Poll for expected ready work like the production loop, allowing brief clock adjustments.
    像生产循环一样轮询预期就绪的待办，允许数据库时钟短暂调整。
    """
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if worker_run_once(*args, **kwargs):
            return True
        time.sleep(0.05)
    return False


def stream_with_worker(client, conversation, monkeypatch, last_event_id=None):
    """Commit work after the stream baseline exists, without racing an arbitrary timer.
    在事件流基线建立后提交待办，不依赖任意定时器的竞争顺序。
    """
    original = events.ConversationStream.poll
    processed = False

    def poll(stream):
        nonlocal processed
        frames = original(stream)
        if not processed:
            processed = True
            assert run_once("events-worker")
            frames.extend(original(stream))
        return frames

    with monkeypatch.context() as patch:
        patch.setattr(events.ConversationStream, "poll", poll)
        return read_stream(client, conversation, last_event_id)


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


def test_stream_requires_owner(client):
    """Another account and anonymous requests cannot open the stream.
    其他账号与未认证请求都不能打开事件流。
    """
    conversation = client.post("/conversations", json={}).json()["id"]
    url = f"/conversations/{conversation}/events"
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


def test_live_job_progress_and_reply(client, monkeypatch):
    """Changes committed while the stream is open arrive as job and message events.
    事件流打开期间提交的变化会以任务事件与消息事件推送。
    """
    conversation, _, receipt = submit(client)
    frames = stream_with_worker(client, conversation, monkeypatch)
    assert frames[0][0] == "ready"
    statuses = [d["status"] for _, d in named(frames, "job") if d["id"] == receipt["job_id"]]
    assert statuses[0] == "QUEUED" and statuses[-1] == "SUCCEEDED"
    assert [d["content"] for _, d in named(frames, "message")] == ["Message received."]


def test_proposal_changes_are_streamed(client, monkeypatch):
    """A new proposal and its approved execution are pushed without a history refresh.
    新方案及批准后的执行进度无需刷新历史即可推送。
    """
    seed_demo_orders("alice", "bob", with_tickets=True)
    conversation, _, _ = submit(client, content="处理工单 391，符合条件就准备退款")
    frames = stream_with_worker(client, conversation, monkeypatch)
    proposals = named(frames, "proposal")
    assert [d["status"] for _, d in proposals] == ["WAITING_FOR_APPROVAL"]
    proposal = proposals[0][1]

    decision = client.post(
        f"/proposals/{proposal['id']}/decision", json={"decision": "APPROVE", "version": proposal["version"]}
    )
    assert decision.status_code == 200, decision.text

    frames = stream_with_worker(client, conversation, monkeypatch, last_event_id=1)
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


@pytest.mark.parametrize("change_order", [False, True])
def test_agent_proposal_revalidation_matches_rest_live_events_and_replay(client, change_order):
    """Keep agent approval evidence identical across REST, live changes and replay.
    让 Agent 审批依据在 REST、实时变化和重放结果中保持一致。
    """
    seed_demo_orders("alice", "bob", with_tickets=True)
    conversation, _, receipt = submit(client, "Please prepare a resolution for ticket 391")
    stream = events.ConversationStream(UUID(conversation), None)
    stream.poll()
    assert run_once("agent-events-worker", provider=workflow())
    frames = stream.poll()
    proposal = next(
        json.loads(frame.split("data: ", 1)[1]) for frame in frames if "event: proposal\n" in frame
    )
    path = f"/conversations/{conversation}/messages"
    assert proposal == client.get(path).json()[0]["proposal"]
    assert proposal["status"] == "WAITING_FOR_APPROVAL"
    steps = client.get(f"/jobs/{receipt['job_id']}/steps").json()
    assert [step["name"] for step in steps] == ["lookup_ticket", "check_refund", "prepare_refund"]
    response = client.post(
        f"/proposals/{proposal['id']}/decision",
        json={"decision": "APPROVE", "version": proposal["version"]},
    )
    assert response.status_code == 200
    stream.poll()
    if change_order:
        with SessionLocal.begin() as session:
            session.get(Order, 1001).paid_total_minor = 9999
    assert run_once("agent-events-worker")
    frames = stream.poll()
    final = next(json.loads(frame.split("data: ", 1)[1]) for frame in frames if "event: proposal\n" in frame)
    assert final == client.get(path).json()[0]["proposal"]
    assert final["status"] == ("INVALIDATED" if change_order else "COMPLETED")
    assert final["revalidated_at"] is not None
    assert final["invalidation_reason"] == ("ORDER_CHANGED" if change_order else None)
    replay = read_stream(client, conversation, last_event_id=0)
    assert named(replay, "message")[0][1]["proposal"] == final
