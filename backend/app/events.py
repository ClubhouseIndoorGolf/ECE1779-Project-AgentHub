# Stream conversation changes as Server-Sent Events by reading committed rows; no schema change is needed.
# 通过读取已提交的数据行，以 Server-Sent Events 推送会话变化；无需修改数据库结构。

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal
from app.history import read_history, read_proposals
from app.models import Job, Message
from app.schemas import JobEventOut

log = logging.getLogger(__name__)

# Tunables are module attributes so tests can shorten them without new settings.
# 参数定义为模块属性，测试可以直接缩短，无需新增配置项。
POLL_SECONDS = 0.5
HEARTBEAT_SECONDS = 15.0
# Bounded streams let load balancers rebalance and let clients resume with Last-Event-ID.
# 限制单个流的时长，便于负载均衡重新分配连接，客户端随后用 Last-Event-ID 续传。
MAX_STREAM_SECONDS = 300.0
# Rows committed slightly after their timestamp was taken must still be seen by the next poll.
# 时间戳取得后稍晚才提交的行，仍需在下一次查询中被看到。
JOB_LOOKBACK = timedelta(seconds=5)
REPLAY_PAGE = 500
ACTIVE_JOB_STATES = ("QUEUED", "RUNNING")


def encode(event: str, data: str, event_id: int | None = None) -> str:
    """Format one SSE frame; JSON data never contains raw newlines.
    格式化单个 SSE 帧；JSON 数据中不会出现原始换行符。
    """
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {data}\n\n"


def parse_last_event_id(value: str | None) -> int | None:
    """Accept a non-negative message sequence and ignore anything else.
    只接受非负整数的消息顺序号，其他值一律忽略并按新连接处理。
    """
    if value is None or not value.strip().isdigit():
        return None
    return int(value.strip())


class ConversationStream:
    """Track what one connection has sent and emit only differences on each poll.
    记录单个连接已发送的内容，每次轮询只推送差异部分。
    """

    def __init__(self, conversation_id: UUID, last_seq: int | None):
        self.conversation_id = conversation_id
        self.last_seq = last_seq
        self.job_states: dict[UUID, tuple[str, int]] = {}
        self.proposal_states: dict[UUID, tuple[str, str | None]] | None = None
        self.since = None

    def poll(self) -> list[str]:
        """Read new messages, job changes and proposal changes in one short read-only session.
        在一个短暂的只读会话中读取新消息、任务变化与方案变化。
        """
        frames: list[str] = []
        with SessionLocal() as session:
            now = session.scalar(select(func.clock_timestamp()))
            if self.last_seq is None:
                # A fresh connection starts at the current end; clients load earlier history over REST.
                # 新连接从当前末尾开始；更早的历史由客户端通过 REST 接口读取。
                self.last_seq = session.scalar(
                    select(func.coalesce(func.max(Message.seq), 0)).where(
                        Message.conversation_id == self.conversation_id
                    )
                )
            if self.since is None:
                frames.append(encode("ready", json.dumps({"cursor": self.last_seq}), self.last_seq))

            # Replay or forward messages in sequence order; the id is the resume cursor.
            # 按顺序号补发或推送消息；事件 id 即续传游标。
            while True:
                page = read_history(session, self.conversation_id, self.last_seq, REPLAY_PAGE)
                for message in page:
                    frames.append(encode("message", message.model_dump_json(exclude_unset=True), message.seq))
                    self.last_seq = message.seq
                if len(page) < REPLAY_PAGE:
                    break

            # Active jobs and recently changed jobs; duplicates are filtered by the tracked state.
            # 读取进行中及最近变化的任务；重复状态由已记录的状态过滤。
            since = (self.since or now) - JOB_LOOKBACK
            jobs = session.scalars(
                select(Job)
                .join(Message, Job.source_message_id == Message.id)
                .where(
                    Message.conversation_id == self.conversation_id,
                    or_(Job.status.in_(ACTIVE_JOB_STATES), Job.updated_at >= since),
                )
                .order_by(Job.created_at, Job.id)
            ).all()
            for job in jobs:
                state = (job.status, job.attempts)
                if self.job_states.get(job.id) != state:
                    self.job_states[job.id] = state
                    frames.append(encode("job", JobEventOut.model_validate(job).model_dump_json()))

            # Proposals change status in place, so compare them against the connection baseline.
            # 方案状态在原记录上变化，因此与连接建立时的基线进行比较。
            proposals = read_proposals(session, self.conversation_id)
            current = {proposal.id: (proposal.status, proposal.decision) for proposal in proposals}
            if self.proposal_states is not None:
                for proposal in proposals:
                    if self.proposal_states.get(proposal.id) != current[proposal.id]:
                        frames.append(encode("proposal", proposal.model_dump_json()))
            self.proposal_states = current
            self.since = now
        return frames


async def conversation_events(stream: ConversationStream, is_disconnected) -> AsyncIterator[str]:
    """Yield frames until the client leaves, the stream reaches its lifetime, or the database fails.
    持续输出事件帧，直到客户端断开、流达到最长时长或数据库出错。
    """
    started = last_sent = time.monotonic()
    # Suggest a reconnect delay to clients that honour the SSE retry field.
    # 向支持 SSE retry 字段的客户端建议重连间隔。
    yield "retry: 2000\n\n"
    while time.monotonic() - started < MAX_STREAM_SECONDS:
        if await is_disconnected():
            return
        try:
            frames = await run_in_threadpool(stream.poll)
        except SQLAlchemyError as exc:
            # End the stream so the client reconnects; never send SQL details.
            # 结束事件流让客户端重连；不向客户端发送 SQL 细节。
            log.error("Event stream poll failed: %s", type(exc).__name__)
            yield encode("error", json.dumps({"detail": "Database temporarily unavailable"}))
            return
        for frame in frames:
            yield frame
        if frames:
            last_sent = time.monotonic()
        elif time.monotonic() - last_sent >= HEARTBEAT_SECONDS:
            # Comment lines keep proxies from closing an idle connection.
            # 注释行用于防止代理关闭空闲连接。
            yield ": keepalive\n\n"
            last_sent = time.monotonic()
        await asyncio.sleep(POLL_SECONDS)
