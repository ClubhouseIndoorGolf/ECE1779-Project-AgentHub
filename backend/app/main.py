# Serve authenticated chat requests; background processing runs in a separate container.
# 提供经过身份验证的聊天接口；后台处理在独立容器运行。

import logging
import os
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import SessionLocal, engine, get_session
from app.events import ConversationStream, conversation_events, parse_last_event_id
from app.history import read_history
from app.models import (
    Conversation,
    ConversationEventHead,
    Execution,
    Job,
    Message,
    ProposalDecision,
    ResolutionProposal,
    User,
)
from app.resolution_workflow import (
    EXPIRED_REPLY,
    PROPOSAL_VERSION,
    active_resolution_exists,
    close_proposal,
    unfinished_turn_before,
)
from app.schemas import (
    ConversationCreate,
    ConversationOut,
    JobOut,
    MessageCreate,
    MessageOut,
    ProposalDecisionIn,
    ProposalDecisionOut,
    SubmissionOut,
)
from app.security import current_user

app = FastAPI(
    title="AgentHub Backend", version="0.2.0", description="Durable chat jobs with optional model replies."
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
log = logging.getLogger(__name__)


@app.get("/", include_in_schema=False)
def demo():
    """Serve the public development client; business requests still require authentication.
    提供公开的开发用聊天页面；页面发出的业务请求仍需通过身份认证。
    """
    return FileResponse(Path(__file__).parent / "static" / "index.html")


# Return a safe English error without exposing database parameters.
# 返回安全的英文错误，不公开数据库参数。
@app.exception_handler(SQLAlchemyError)
def database_error(_request: Request, exc: SQLAlchemyError):
    """Expose a retryable database outage without serializing SQL or connection details.
    返回可稍后重试的数据库不可用响应，不公开 SQL 参数或数据库连接详情。
    """
    log.error("Database operation failed: %s", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable"})


# Check database connectivity without requiring a user account.
# 无需用户账号即可检查数据库连通性。
@app.get("/health", description="Check API and database availability. Authentication is not required.")
def health():
    """Probe database connectivity and report configured mode without calling a model.
    探测数据库连接并报告配置的回复模式，此接口不调用模型或验证 Worker 心跳。
    """
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    # This is configured mode, not a claim that the worker/model is healthy.
    # 此字段仅代表配置值，不表示 Worker 或模型服务已通过可用性验证。
    return {"status": "ok", "database": "ok", "reply_mode": os.getenv("REPLY_MODE", "fixed")}


# Return the authenticated account without its password hash.
# 返回已验证账号的信息，不返回密码哈希。
@app.get("/me", description="Return the authenticated account without password or credential fields.")
def me(user: User = Depends(current_user)):
    """Return only the authenticated account's public identity fields.
    仅返回已验证账号的公开身份字段，不包含密码哈希或其他认证信息。
    """
    return {"id": user.id, "username": user.username, "display_name": user.display_name}


# Derive ownership from authentication rather than the request body.
# 从身份验证结果确定归属，不从请求体读取归属用户。
@app.post(
    "/conversations",
    response_model=ConversationOut,
    status_code=201,
    description="Create a conversation owned by the authenticated account. The default title is New conversation.",
)
def create_conversation(
    payload: ConversationCreate, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    """Create an owned conversation and its zero event cursor in one transaction.
    在同一事务中为已验证用户创建会话及初始值为零的事件游标。
    """
    with session.begin():
        conversation = Conversation(user_id=user.id, title=payload.title)
        session.add(conversation)
        session.flush()
        # The generated conversation ID must exist before its event counter can reference it.
        # 必须先写入会话编号，事件计数器才能引用它。
        session.add(ConversationEventHead(conversation_id=conversation.id))
    return conversation


# Restrict the list to the current account and bound its size.
# 只列出当前账号的会话，并限制返回数量。
@app.get(
    "/conversations",
    response_model=list[ConversationOut],
    description="List your conversations, most recently updated first.",
)
def list_conversations(
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Return a bounded account-scoped list, newest activity first with deterministic ties.
    返回数量受限且仅属于当前账号的会话，按最近活动排序并用编号稳定处理并列情况。
    """
    return session.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc(), Conversation.id)
        .limit(limit)
    ).all()


# Lock one conversation and atomically save its message, sequence and job.
# 锁定对应会话，在同一事务内保存消息、顺序号和待办。
@app.post(
    "/conversations/{conversation_id}/messages",
    response_model=SubmissionOut,
    status_code=202,
    description="Save a message and its background job atomically. Reuse submission_key only when retrying the same request.",
)
def submit_message(
    conversation_id: UUID,
    payload: MessageCreate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Atomically save one logical message and job, returning stable IDs for identical retries.
    原子保存一条逻辑消息及其任务；相同内容和去重标识的重试始终返回原有编号。
    """
    with session.begin():
        # Ownership and sequence allocation share a row lock with worker completion.
        # 检查会话归属并锁定会话，与 Worker 保存回复时共同保护消息顺序号的分配。
        conversation = session.scalar(
            select(Conversation)
            .where(Conversation.id == conversation_id, Conversation.user_id == user.id)
            .with_for_update()
        )
        if not conversation:
            raise HTTPException(404, "Conversation not found")
        # A retry must reuse both key and text; a key cannot silently change an accepted message.
        # 重试必须同时复用标识和原文，不能借用已有标识静默修改已接受的消息。
        existing = session.scalar(
            select(Message).where(
                Message.conversation_id == conversation_id, Message.submission_key == payload.submission_key
            )
        )
        if existing:
            if existing.content != payload.content:
                raise HTTPException(409, "submission_key already used for different content")
            job_id = session.scalar(
                select(Job.id).where(Job.source_message_id == existing.id, Job.kind == "PROCESS_MESSAGE")
            )
            return SubmissionOut(message_id=existing.id, job_id=job_id)
        # Check for a pause after duplicate detection so earlier retries keep their original receipt.
        # 先检查重复提交，再检查审批暂停，使旧请求重试仍能取得原始回执。
        if active_resolution_exists(session, conversation.id):
            raise HTTPException(409, "Resolve the active refund proposal before sending a new message.")
        # Allocate the sequence and persist the message before creating its foreign-keyed job.
        # 分配顺序号并保存消息，再创建通过外键引用它的任务；任一步失败都会整体回滚。
        message = Message(
            conversation_id=conversation_id,
            role="user",
            content=payload.content,
            seq=conversation.next_message_seq,
            submission_key=payload.submission_key,
        )
        conversation.next_message_seq += 1
        conversation.updated_at = func.clock_timestamp()
        session.add(message)
        session.flush()
        job = Job(source_message_id=message.id)
        session.add(job)
        session.flush()
        result = SubmissionOut(message_id=message.id, job_id=job.id)
    return result


# Check ownership before returning ordered, paginated history.
# 先检查归属，再返回按顺序分页的历史记录。
@app.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
    response_model_exclude_unset=True,
    description="Read your conversation's messages and any attached proposals. Use after_seq and limit for pagination.",
)
def list_messages(
    conversation_id: UUID,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Read owned message pages with proposals attached to their source messages.
    分页读取当前账号的消息，并在来源消息上附带对应的方案。
    """
    conversation = session.scalar(
        select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id)
    )
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    # Read messages and any attached proposals in one snapshot; the event stream shares this shape.
    # 在同一快照中读取消息及其附带方案；实时事件流复用相同的数据结构。
    return read_history(session, conversation_id, after_seq, limit)


# Check ownership once, then stream committed changes for that conversation.
# 先检查一次会话归属，再推送该会话中已提交的变化。
@app.get(
    "/conversations/{conversation_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
    description=(
        "Stream your conversation's new messages, job status changes and proposal status changes as "
        "Server-Sent Events. Send Last-Event-ID with the last message sequence received to replay later messages."
    ),
)
async def stream_events(
    conversation_id: UUID,
    request: Request,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    user: User = Depends(current_user),
):
    """Authorize the stream in a short session; each poll later opens its own read-only session.
    在短会话中完成授权；之后每次轮询各自打开只读会话，流期间不长期占用数据库连接。
    """

    def owned() -> bool:
        with SessionLocal() as session:
            return (
                session.scalar(
                    select(Conversation.id).where(
                        Conversation.id == conversation_id, Conversation.user_id == user.id
                    )
                )
                is not None
            )

    if not await run_in_threadpool(owned):
        raise HTTPException(404, "Conversation not found")
    stream = ConversationStream(conversation_id, parse_last_event_id(last_event_id))
    return StreamingResponse(
        conversation_events(stream, request.is_disconnected),
        media_type="text/event-stream",
        # Disable caching and proxy buffering so each frame reaches the browser promptly.
        # 禁用缓存与代理缓冲，使每个事件帧及时到达浏览器。
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/proposals/{proposal_id}/decision", response_model=ProposalDecisionOut)
def decide_proposal(
    proposal_id: UUID,
    payload: ProposalDecisionIn,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Record one human decision and enqueue an approved continuation atomically.
    原子保存一份人工决定，并在批准时加入恢复待办。
    """
    with session.begin():
        # Serialize decisions with message submissions and final replies in the same conversation.
        # 将审批与同一会话的消息提交、最终回复串行化。
        row = session.execute(
            select(Conversation, ResolutionProposal, Message.seq)
            .join(Message, Message.conversation_id == Conversation.id)
            .join(Execution, Execution.source_message_id == Message.id)
            .join(ResolutionProposal, ResolutionProposal.execution_id == Execution.id)
            .where(ResolutionProposal.id == proposal_id, Conversation.user_id == user.id)
            .with_for_update(of=Conversation)
        ).one_or_none()
        if row is None:
            raise HTTPException(404, "Proposal not found")
        conversation, proposal, source_seq = row
        if payload.version != PROPOSAL_VERSION:
            raise HTTPException(409, "Proposal version has changed")
        execution = session.get(Execution, proposal.execution_id, with_for_update=True)

        # Look up the decision after locking so concurrent retries see the committed receipt.
        # 加锁后读取决定，使并发重试能看到已提交的审批回执。
        existing = session.get(ProposalDecision, proposal.id)
        if existing is not None:
            if existing.decision != payload.decision:
                raise HTTPException(409, "A different decision has already been recorded")
            return ProposalDecisionOut.model_validate(existing)
        if execution.status != "WAITING_FOR_APPROVAL":
            raise HTTPException(409, "Proposal is no longer waiting for approval")
        # Upgraded conversations can contain several pending proposals from the planning-only release.
        # 从仅支持准备方案的版本升级时，同一会话可能保留多个待审批方案。
        if session.scalar(select(unfinished_turn_before(conversation.id, source_seq))):
            raise HTTPException(
                409, "Resolve earlier turns in this conversation before deciding this proposal"
            )
        now = session.scalar(select(func.clock_timestamp()))
        if now >= proposal.expires_at:
            close_proposal(session, conversation, execution, "INVALIDATED", EXPIRED_REPLY)
        else:
            # Only approval queues work. Rejection records a final reply without business writes.
            # 只有批准才入队。拒绝仅保存最终回复，不写入业务数据。
            job = None
            if payload.decision == "APPROVE":
                job = Job(source_message_id=execution.source_message_id, kind="RESUME_EXECUTION")
                session.add(job)
                session.flush([job])
                execution.status = "APPROVED"
                conversation.updated_at = now
            else:
                close_proposal(
                    session, conversation, execution, "REJECTED", "The refund proposal was rejected."
                )
            decision = ProposalDecision(
                proposal_id=proposal.id,
                user_id=user.id,
                decision=payload.decision,
                decided_at=now,
                resume_job_id=job.id if job else None,
            )
            session.add(decision)
            return ProposalDecisionOut.model_validate(decision)

    # Commit the expiration and its reply before returning the conflict to the client.
    # 先提交失效状态及其回复，再向客户端返回冲突。
    raise HTTPException(409, "Proposal has expired")


# Follow the source message to enforce conversation ownership.
# 通过来源消息检查会话归属，限制待办访问。
@app.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    description="Read the processing status of a job belonging to your account.",
)
def get_job(job_id: UUID, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Resolve job ownership through its source message and expose the public job schema.
    通过来源消息和会话确认任务归属，再由公开响应模型过滤掉领取令牌等内部字段。
    """
    job = session.scalar(
        select(Job)
        .join(Message, Job.source_message_id == Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Job.id == job_id, Conversation.user_id == user.id)
    )
    if not job:
        raise HTTPException(404, "Job not found")
    return job
