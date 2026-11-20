"""Chat management preserves ownership, in-flight work and refund audit records.
会话管理保留归属校验、运行中的任务和退款审计记录。
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_resolution_workflow import decide, plan
from test_resolution_workflow import demo_tickets as demo_tickets

from app.db import SessionLocal
from app.events import ConversationRemoved, ConversationStream, DurableConversationStream
from app.models import AppliedResolution, Conversation, Message
from app.worker import run_once


def test_rename_pin_ordering_and_ownership(client):
    first = client.post("/conversations", json={"title": "First"}).json()
    second = client.post("/conversations", json={"title": "Second"}).json()
    url = f"/conversations/{first['id']}"
    updated = client.patch(url, json={"title": "  团队 💬  ", "is_pinned": True})
    assert updated.status_code == 200
    assert updated.json()["title"] == "团队 💬"
    assert client.get("/conversations").json()[0]["id"] == first["id"]
    client.patch(f"/conversations/{second['id']}", json={"title": "Newer"})
    assert client.get("/conversations").json()[0]["id"] == first["id"]
    assert client.patch(url, json={"is_pinned": False}).json()["is_pinned"] is False
    client.auth = ("bob", "bob-test-password")
    assert client.patch(url, json={"title": "Not mine"}).status_code == 404
    assert client.delete(url).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": " "},
        {"title": None},
        {"title": "x" * 201},
        {"is_pinned": "yes"},
        {"is_pinned": None},
        {"user_id": str(uuid4())},
    ],
)
def test_invalid_chat_updates(client, payload):
    chat = client.post("/conversations", json={}).json()
    assert client.patch(f"/conversations/{chat['id']}", json=payload).status_code == 422


def test_delete_rejects_active_work_then_hides_chat_and_stops_streams(client):
    chat = client.post("/conversations", json={}).json()
    url = f"/conversations/{chat['id']}"
    receipt = client.post(url + "/messages", json={"content": "Hello", "submission_key": str(uuid4())}).json()
    assert client.delete(url).status_code == 409
    assert run_once()
    streams = [ConversationStream(UUID(chat["id"]), 0), DurableConversationStream(UUID(chat["id"]), 0)]
    for stream in streams:
        stream.poll()
    assert client.delete(url).status_code == 204
    assert client.delete(url).status_code == 204
    assert client.get("/conversations").json() == []
    assert client.get(url + "/messages").status_code == 404
    assert client.get(url + "/events").status_code == 404
    assert client.get(f"/jobs/{receipt['job_id']}").status_code == 404
    assert client.get(f"/jobs/{receipt['job_id']}/steps").status_code == 404
    assert client.patch(url, json={"title": "Restore"}).status_code == 404
    assert (
        client.post(
            url + "/messages", json={"content": "Hidden work", "submission_key": str(uuid4())}
        ).status_code
        == 404
    )
    for stream in streams:
        with pytest.raises(ConversationRemoved):
            stream.poll()
    with SessionLocal() as session:
        assert session.get(Conversation, UUID(chat["id"])).deleted_at is not None
        assert len(session.scalars(select(Message)).all()) == 2


def test_delete_keeps_refund_and_blocks_pending_approval(client, demo_tickets):
    path, _, _, proposal = plan(client)
    url = path.removesuffix("/messages")
    assert client.delete(url).status_code == 409
    assert decide(client, proposal).status_code == 200
    assert client.delete(url).status_code == 409
    assert run_once()
    execution_id = client.get(path).json()[0]["execution_id"]
    assert client.get(f"/executions/{execution_id}").json()["result"] is not None
    assert client.delete(url).status_code == 204
    assert client.get(f"/executions/{execution_id}").status_code == 404
    assert decide(client, proposal).status_code == 404
    with SessionLocal() as session:
        assert session.scalar(select(AppliedResolution)) is not None
