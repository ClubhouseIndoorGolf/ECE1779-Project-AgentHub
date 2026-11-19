"""Verify public activity, live snapshots and atomic checkpoint events.
验证公开进度、实时快照及检查点事件的原子性。
"""

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_agent import workflow
from test_resolution_workflow import demo_tickets as demo_tickets
from test_resolution_workflow import submit

from app import agent
from app.activity import public_step
from app.db import SessionLocal
from app.events import ConversationStream, DurableConversationStream
from app.models import AgentStep, ConversationEvent, Job
from app.replies import ReplySettings
from app.worker import claim_job, complete_job


def test_activity_stream_history_and_replay(client, demo_tickets):
    path, _, receipt = submit(client, "Review ticket 391")
    conversation_id = UUID(path.split("/")[2])
    stream = ConversationStream(conversation_id, 0)
    stream.poll()
    claim = claim_job("activity-test")
    step = agent.run_agent(claim, workflow(), [], ReplySettings())
    activity = client.get(path).json()[0]["activity"]
    assert activity["job_id"] == receipt["job_id"]
    assert [item["status"] for item in activity["steps"]] == ["completed", "completed", "running"]
    assert activity["steps"][1]["text"] == "Checked refund eligibility — eligible"
    frames = stream.poll()
    assert any("event: activity" in frame and "Preparing a resolution" in frame for frame in frames)
    assert not any("event: activity" in frame for frame in stream.poll())
    complete_job(claim, agent_step=step)
    history = client.get(path).json()[0]
    assert history["activity"]["status"] == "SUCCEEDED"
    assert history["activity"]["steps"][-1]["text"] == "Prepared proposal for human review"
    assert history["proposal"]["status"] == "WAITING_FOR_APPROVAL"
    with SessionLocal() as session:
        events = session.scalars(
            select(ConversationEvent)
            .where(ConversationEvent.event_type.like("agent.%"))
            .order_by(ConversationEvent.seq)
        ).all()
    assert [event.event_type for event in events] == ["agent.step_started", "agent.step_completed"] * 3
    assert all(set(event.payload["step"]) == {"seq", "name", "status", "text"} for event in events)
    replay = DurableConversationStream(conversation_id, events[-2].seq).poll()
    assert sum("event: agent.step_completed" in frame for frame in replay) == 1
    client.auth = ("bob", "bob-test-password")
    assert client.get(path).status_code == 404
    assert client.get(f"/conversations/{conversation_id}/events").status_code == 404


@pytest.mark.parametrize("completed", [False, True])
def test_expired_checkpoint_rolls_back_activity(client, demo_tickets, monkeypatch, completed):
    submit(client, "Review ticket 391")
    claim = claim_job("expired-activity")
    original = agent.append_events

    def expire(session, conversation_id, events):
        result = original(session, conversation_id, events)
        if events and events[0].event_type == ("agent.step_completed" if completed else "agent.step_started"):
            session.get(Job, claim.job_id).agent_deadline_at = session.scalar(
                select(func.clock_timestamp())
            ) - timedelta(seconds=1)
        return result

    monkeypatch.setattr(agent, "append_events", expire)
    with pytest.raises(agent.AgentBudgetExceeded):
        agent.run_agent(claim, workflow(), [], ReplySettings())
    with SessionLocal() as session:
        events = session.scalars(
            select(ConversationEvent).where(ConversationEvent.event_type.like("agent.%"))
        ).all()
        assert len(events) == int(completed)
        step = session.get(AgentStep, (claim.job_id, 1))
        assert (step is not None) == completed
        if step:
            assert step.result is None


def test_unavailable_record_activity_does_not_expose_arguments():
    step = AgentStep(
        seq=1,
        name="lookup_ticket",
        arguments={"ticket_id": 395, "secret": "private"},
        result={"outcome": "NOT_FOUND"},
    )
    assert public_step(step)["text"] == "Requested record unavailable"
    assert "private" not in str(public_step(step))
    assert "395" not in str(public_step(step))
