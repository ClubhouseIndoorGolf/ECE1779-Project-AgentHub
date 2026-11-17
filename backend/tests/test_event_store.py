"""Verify durable event sequencing against isolated PostgreSQL.
在隔离 PostgreSQL 中验证持久化事件序号。
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from app.db import SessionLocal
from app.event_store import EventToAppend, append_events, read_event_head, read_events
from app.manage import seed_demo_orders
from app.models import AppliedResolution, Conversation
from app.worker import run_once


def create_chat(client) -> UUID:
    """Create a chat through its normal endpoint and return its identifier.
    通过正式接口创建会话并返回其编号。
    """
    response = client.post("/conversations", json={})
    assert response.status_code == 201
    return UUID(response.json()["id"])


def test_new_conversation_has_zero_cursor_and_supports_paged_reads(client):
    """Check default counters, ordered pages, and isolation between conversation IDs.
    检查默认计数器、有序分页和不同会话编号之间的隔离。
    """
    first = create_chat(client)
    second = create_chat(client)
    with SessionLocal.begin() as session:
        assert read_event_head(session, first) == read_event_head(session, second) == 0
        assert read_events(session, first) == []
        assert append_events(session, first, []) == []
        assert read_event_head(session, first) == 0

        # Batch one conversation and give the second conversation its own first sequence.
        # 为首个会话批量写入，同时让第二个会话拥有独立的首个序号。
        assert append_events(
            session,
            first,
            [
                EventToAppend("proposal.decided", {"decision": "APPROVE"}),
                EventToAppend("job.status_changed", {"status": "RUNNING", "attempts": 1}),
                EventToAppend("resolution.completed", {"summary": "模拟退款已完成"}),
            ],
        ) == [1, 2, 3]
        assert append_events(session, second, [EventToAppend("proposal.created", {})]) == [1]

    with SessionLocal() as session:
        page = read_events(session, first, after_seq=1, limit=2)
        assert [(row.seq, row.event_type, row.payload) for row in page] == [
            (2, "job.status_changed", {"status": "RUNNING", "attempts": 1}),
            (3, "resolution.completed", {"summary": "模拟退款已完成"}),
        ]
        assert all(row.created_at.utcoffset() is not None for row in page)
        assert read_event_head(session, first) == 3
        assert read_events(session, first, after_seq=3) == []
        assert [row.event_type for row in read_events(session, second)] == ["proposal.created"]


@pytest.mark.parametrize("commit_first", [True, False])
def test_concurrent_appends_follow_commit_order_and_reuse_rolled_back_ranges(client, commit_first):
    """Observe database blocking before releasing the writer and checking the committed sequence.
    先观察数据库锁等待，再释放写入事务并检查已提交的序列。
    """
    conversation = create_chat(client)
    other = create_chat(client)
    waiting_pid = Queue()

    def append_second():
        """Start an independent writer and report its PostgreSQL session ID.
        启动独立的写入事务并报告其 PostgreSQL 会话编号。
        """
        with SessionLocal.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '12s'"))
            waiting_pid.put(session.scalar(select(func.pg_backend_pid())))
            return append_events(session, conversation, [EventToAppend("job.status_changed", {"writer": 2})])

    with ThreadPoolExecutor(max_workers=1) as pool:
        with SessionLocal() as first:
            first_pid = first.scalar(select(func.pg_backend_pid()))
            assert append_events(
                first,
                conversation,
                [
                    EventToAppend("proposal.created", {"writer": 1}),
                    EventToAppend("proposal.decided", {"writer": 1}),
                ],
            ) == [1, 2]
            second = pool.submit(append_second)
            second_pid = waiting_pid.get(timeout=8)

            # Check the actual blocker to prove that the writes overlap at the database.
            # 检查真正持锁的事务，证明两个写入确实在数据库层并发发生。
            deadline = monotonic() + 8
            while first_pid not in first.scalar(select(func.pg_blocking_pids(second_pid))):
                assert monotonic() < deadline
                sleep(0.01)

            # Uncommitted rows stay invisible, and another conversation can still advance.
            # 未提交记录不可见，且另一个会话仍能分配序号。
            with SessionLocal.begin() as reader:
                reader.execute(text("SET LOCAL lock_timeout = '2s'"))
                assert read_event_head(reader, conversation) == 0
                assert read_events(reader, conversation) == []
                assert append_events(reader, other, [EventToAppend("proposal.created", {})]) == [1]
            if commit_first:
                first.commit()
            else:
                first.rollback()
        assert second.result(timeout=12) == ([3] if commit_first else [1])

    with SessionLocal() as session:
        expected = [(1, 1), (2, 1), (3, 2)] if commit_first else [(1, 2)]
        assert [
            (event.seq, event.payload["writer"]) for event in read_events(session, conversation)
        ] == expected
        assert read_event_head(session, conversation) == expected[-1][0]
        assert read_event_head(session, other) == 1


def test_append_rolls_back_with_caller_business_write(client):
    """Keep earlier events while rolling back the new events, cursor and business data together.
    保留先前事件，同时回滚新事件、游标和业务数据。
    """
    conversation = create_chat(client)
    with SessionLocal.begin() as session:
        assert append_events(session, conversation, [EventToAppend("proposal.created", {})]) == [1]

    with pytest.raises(RuntimeError, match="Abort business transaction"), SessionLocal.begin() as session:
        session.get(Conversation, conversation, with_for_update=True).title = "Discard this change"
        assert append_events(session, conversation, [EventToAppend("proposal.decided", {})]) == [2]
        raise RuntimeError("Abort business transaction")

    with SessionLocal.begin() as session:
        assert session.get(Conversation, conversation).title == "New conversation"
        assert read_event_head(session, conversation) == 1
        assert [row.event_type for row in read_events(session, conversation)] == ["proposal.created"]
        assert append_events(session, conversation, [EventToAppend("proposal.decided", {})]) == [2]


def test_upgrade_from_0006_preserves_refund_and_proposal_history(client):
    """Migrate actual workflow data and start each existing chat at an empty cursor.
    迁移实际业务流程数据，并为每个已有会话初始化空游标。
    """
    seed_demo_orders("alice", "bob", with_tickets=True)
    bob_auth = ("bob", "bob-test-password")
    completed_id = create_chat(client)
    pending_response = client.post("/conversations", json={}, auth=bob_auth)
    assert pending_response.status_code == 201
    pending_id = UUID(pending_response.json()["id"])
    completed_path = f"/conversations/{completed_id}/messages"
    pending_path = f"/conversations/{pending_id}/messages"
    for path, ticket, options in [(completed_path, 391, {}), (pending_path, 395, {"auth": bob_auth})]:
        payload = {"content": f"Prepare a refund for ticket {ticket}", "submission_key": str(uuid4())}
        assert client.post(path, json=payload, **options).status_code == 202
        assert run_once("migration-fixture-worker")

    proposal_id = client.get(completed_path).json()[0]["proposal"]["id"]
    decision_path = f"/proposals/{proposal_id}/decision"
    approval = {"version": 1, "decision": "APPROVE"}
    response = client.post(decision_path, json=approval)
    assert response.status_code == 200
    receipt = response.json()
    assert run_once("migration-fixture-worker")
    completed_history = client.get(completed_path).json()
    pending_history = client.get(pending_path, auth=bob_auth).json()
    assert completed_history[0]["proposal"]["status"] == "COMPLETED"
    assert pending_history[0]["proposal"]["status"] == "WAITING_FOR_APPROVAL"
    with SessionLocal() as session:
        refund_before = session.execute(select(AppliedResolution.__table__)).one()

    # Downgrading the additive event migration leaves a populated 0006 business schema.
    # 回退只新增事件表的迁移，得到带有业务数据的 0006 数据库。
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.downgrade(cfg, "0006_approved_resolutions")
    command.upgrade(cfg, "head")

    # Compare public data and the full business result, then check the first allocated sequence.
    # 核对公开数据和完整业务结果，再检查迁移后分配的首个序号。
    assert client.get(completed_path).json() == completed_history
    assert client.get(pending_path, auth=bob_auth).json() == pending_history
    assert client.post(decision_path, json=approval).json() == receipt
    with SessionLocal.begin() as session:
        assert session.execute(select(AppliedResolution.__table__)).one() == refund_before
        for conversation_id in (completed_id, pending_id):
            assert read_event_head(session, conversation_id) == 0
            assert read_events(session, conversation_id) == []
        assert append_events(
            session, pending_id, [EventToAppend("job.status_changed", {"status": "QUEUED"})]
        ) == [1]
