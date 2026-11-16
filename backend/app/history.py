# Build public history rows shared by the REST history endpoint and the live event stream.
# 构建公开的历史记录，供 REST 历史接口与实时事件流共用，保证两者返回相同结构。

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Execution, Job, Message, ProposalDecision, ResolutionProposal
from app.resolution_workflow import PROPOSAL_VERSION
from app.schemas import MessageOut, ProposalOut


def _history_query(conversation_id: UUID):
    """Join each message to its optional proposal, decision and continuation job.
    将每条消息与其可选的方案、审批决定及恢复待办关联，且每条消息只返回一行。
    """
    return (
        select(Message, Execution, ResolutionProposal, ProposalDecision, Job)
        .outerjoin(Execution, Execution.source_message_id == Message.id)
        .outerjoin(ResolutionProposal, ResolutionProposal.execution_id == Execution.id)
        .outerjoin(ProposalDecision, ProposalDecision.proposal_id == ResolutionProposal.id)
        .outerjoin(Job, Job.id == ProposalDecision.resume_job_id)
        .where(Message.conversation_id == conversation_id)
    )


def proposal_out(execution, proposal, decision, job) -> ProposalOut:
    """Expose only public proposal fields with the continuation status once approved.
    只公开方案的公开字段；批准后用恢复待办的状态表示执行进度。
    """
    return ProposalOut(
        id=proposal.id,
        version=PROPOSAL_VERSION,
        status=job.status if execution.status == "APPROVED" else execution.status,
        ticket_id=str(proposal.ticket_id),
        order_id=str(execution.requested_order_id),
        refund_amount_minor=str(execution.refund_amount_minor),
        currency=execution.currency,
        customer_reply=proposal.customer_reply,
        proposed_ticket_status=proposal.proposed_ticket_status,
        policy_version=execution.policy_version,
        delivered_at=proposal.expected_order_facts["delivered_at"],
        checked_at=execution.checked_at,
        expires_at=proposal.expires_at,
        decision=decision.decision if decision else None,
        decided_at=decision.decided_at if decision else None,
        resume_job_id=decision.resume_job_id if decision else None,
        revalidated_at=execution.revalidated_at,
        invalidation_reason=execution.invalidation_reason,
    )


def read_history(session: Session, conversation_id: UUID, after_seq: int, limit: int) -> list[MessageOut]:
    """Read one ordered page after a sequence number; callers must check ownership first.
    读取指定顺序号之后的一页有序消息；调用方必须先完成会话归属检查。
    """
    rows = session.execute(
        _history_query(conversation_id).where(Message.seq > after_seq).order_by(Message.seq).limit(limit)
    )
    history = []
    for message, execution, proposal, decision, job in rows:
        result = MessageOut.model_validate(message)
        # Keep ordinary history in its original shape and attach proposals only when present.
        # 普通消息保持原有结构，仅在存在方案时附带方案字段。
        if proposal is not None:
            result.proposal = proposal_out(execution, proposal, decision, job)
        history.append(result)
    return history


def read_proposals(session: Session, conversation_id: UUID) -> list[ProposalOut]:
    """Read every proposal in one conversation so the stream can detect status changes.
    读取会话内全部方案，供事件流检测状态变化；单个会话中的方案数量很少。
    """
    rows = session.execute(
        _history_query(conversation_id).where(ResolutionProposal.id.is_not(None)).order_by(Message.seq)
    )
    return [
        proposal_out(execution, proposal, decision, job) for _, execution, proposal, decision, job in rows
    ]
