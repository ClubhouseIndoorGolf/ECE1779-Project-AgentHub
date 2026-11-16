"""Append and read durable conversation events within caller-owned transactions.
在调用方管理的事务内追加和读取会话持久化事件。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.models import ConversationEvent, ConversationEventHead

MAX_EVENT_PAGE_SIZE = 500
MAX_EVENT_SEQ = 2**63 - 1


@dataclass(frozen=True)
class EventToAppend:
    """Describe a public event and its JSON object at the time of the change.
    描述状态变化发生时的公开事件及其 JSON 对象。
    """

    event_type: str
    payload: dict


def append_events(session: Session, conversation_id: UUID, events: Sequence[EventToAppend]) -> list[int]:
    """Write a contiguous batch, leaving commit or rollback to the caller.
    连续写入一批事件，由调用方提交或回滚事务。
    """
    if not events:
        return []

    # Flush any pending business writes first. Once the counter is held, callers may
    # only touch business rows they already locked, such as for a final lease check.
    # 先写入待保存的业务变更。取得计数器锁后，调用方只能操作已锁定的业务行，
    # 例如对已锁定的待办执行最后一次租约核验。
    session.flush()
    last_seq = session.scalar(
        update(ConversationEventHead)
        .where(ConversationEventHead.conversation_id == conversation_id)
        .values(last_event_seq=ConversationEventHead.last_event_seq + len(events))
        .returning(ConversationEventHead.last_event_seq)
    )
    if last_seq is None:
        raise LookupError("Conversation event head not found")

    # The atomic update holds the counter lock until commit. Insert the batch in the
    # same transaction so a rollback releases both the rows and their reserved range.
    # 原子更新会持有计数器锁直至提交。在同一事务内插入整批事件，
    # 使回滚同时撤销事件记录并释放预留的序号范围。
    sequences = list(range(last_seq - len(events) + 1, last_seq + 1))
    session.execute(
        insert(ConversationEvent),
        [
            {
                "conversation_id": conversation_id,
                "seq": seq,
                "event_type": event.event_type,
                "payload": event.payload,
            }
            for seq, event in zip(sequences, events, strict=True)
        ],
    )
    return sequences


def read_event_head(session: Session, conversation_id: UUID) -> int | None:
    """Read the cursor in the caller's snapshot, or return None for a missing counter.
    在调用方的快照中读取游标。计数器不存在时返回 None。
    """
    return session.scalar(
        select(ConversationEventHead.last_event_seq).where(
            ConversationEventHead.conversation_id == conversation_id
        )
    )


def read_events(
    session: Session, conversation_id: UUID, after_seq: int = 0, limit: int = MAX_EVENT_PAGE_SIZE
) -> Sequence[ConversationEvent]:
    """Read an ordered page after the exclusive cursor for a previously authorized conversation.
    为已通过归属校验的会话读取指定游标之后的一页有序事件。
    """
    if not 0 <= after_seq <= MAX_EVENT_SEQ:
        raise ValueError("Event cursor is outside the supported range")
    if not 1 <= limit <= MAX_EVENT_PAGE_SIZE:
        raise ValueError("Event page limit is outside the supported range")

    # Use the composite primary key for account-scoped replay after authorization.
    # 归属校验之后，通过复合主键限定会话与游标范围。
    return session.scalars(
        select(ConversationEvent)
        .where(ConversationEvent.conversation_id == conversation_id, ConversationEvent.seq > after_seq)
        .order_by(ConversationEvent.seq)
        .limit(limit)
    ).all()
