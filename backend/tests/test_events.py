# Verify the conversation event stream: ownership, replay cursor, job progress and proposal changes.
# 验证会话事件流：归属检查、续传游标、任务进度与方案状态变化。

import json
import threading
import time
from uuid import uuid4

import pytest

from app import events
from app.manage import seed_demo_orders
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
    headers = {} if last_event_id is None else {"Last-Event-ID": str(last_event_id)}
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
    "value, expected", [(None, None), ("", None), ("7", 7), (" 12 ", 12), ("-1", None), ("x", None)]
)
def test_parse_last_event_id(value, expected):
    assert events.parse_last_event_id(value) == expected
