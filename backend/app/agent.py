"""A bounded, checkpointed agent loop; only the worker can commit its steps.
有界且可恢复的 Agent 循环，只有持有租约的 worker 可提交步骤。
"""

import logging
from dataclasses import asdict
from datetime import timedelta

from sqlalchemy import func, select

from app.agent_contract import TERMINAL, Decision
from app.db import SessionLocal
from app.leases import LostLease, fence_checkpoint, owned_job
from app.models import AgentStep, AppliedResolution, Conversation, Job, Message, Order, User
from app.refund_eligibility import RefundEligibility, evaluate_refund_eligibility
from app.support_workflow import order_facts
from app.ticket_lookup import lookup_ticket

log = logging.getLogger(__name__)


class AgentBudgetExceeded(Exception):
    pass


def lock_agent_job(session, claim):
    job = session.scalar(select(Job).where(owned_job(claim)).with_for_update())
    if job is None:
        raise LostLease()
    return job


def check_deadline(session, job):
    now = session.scalar(select(func.clock_timestamp()))
    if job.agent_deadline_at is None or now >= job.agent_deadline_at:
        raise AgentBudgetExceeded("Agent deadline reached")
    return (job.agent_deadline_at - now).total_seconds()


def actor(session, job):
    source = session.get(Message, job.source_message_id)
    conversation = session.get(Conversation, source.conversation_id)
    if not session.get(User, conversation.user_id).is_active:
        raise ValueError("Account is disabled")
    return conversation.user_id


def read_tool(session, job, decision):
    """Read only account-scoped facts; model arguments never supply authorization.
    只读取账号有权访问的资料，模型参数不能提供授权身份。
    """
    user_id = actor(session, job)
    if decision.name == "lookup_ticket":
        ticket_id = decision.arguments["ticket_id"]
        found = lookup_ticket(session, user_id, ticket_id)
        if found is None:
            return {"outcome": "NOT_FOUND"}
        status, order = found
        return {
            "ticket_id": str(ticket_id),
            "status": status,
            "order": {"id": str(order.id), **order_facts(order)} if order else None,
        }
    order_id = decision.arguments["order_id"]
    order = session.scalar(select(Order).where(Order.id == order_id, Order.assigned_user_id == user_id))
    now = session.scalar(select(func.clock_timestamp()))
    result = evaluate_refund_eligibility(order, now)
    if (
        order is not None
        and session.scalar(
            select(AppliedResolution.proposal_id).where(AppliedResolution.order_id == order.id)
        )
        is not None
    ):
        result = RefundEligibility("INELIGIBLE", ("ALREADY_REFUNDED",), currency=order.currency)
    return {"order_id": str(order_id), "checked_at": now.isoformat(), **asdict(result)}


def run_agent(claim, provider, messages, settings):
    """Replay saved decisions and read results, reserving every external call before it starts.
    重用已保存决策和只读结果，在外部调用前持久预留每次预算。
    """
    while True:
        with SessionLocal.begin() as session:
            job = lock_agent_job(session, claim)
            if job.agent_deadline_at is None:
                job.agent_deadline_at = session.scalar(select(func.clock_timestamp())) + timedelta(
                    seconds=settings.agent_seconds
                )
            remaining = check_deadline(session, job)
            steps = session.scalars(
                select(AgentStep).where(AgentStep.job_id == job.id).order_by(AgentStep.seq)
            ).all()
            pending = steps[-1] if steps and steps[-1].result is None else None
            if pending is not None:
                decision = Decision(pending.name, pending.arguments).validated()
                if decision.name in TERMINAL:
                    return pending.seq
                pending.result = read_tool(session, job, decision)
                pending.finished_at = func.clock_timestamp()
                check_deadline(session, job)
                fence_checkpoint(session, claim)
                check_deadline(session, job)
                log.info("job=%s step=%s action=%s checkpoint=completed", job.id, pending.seq, pending.name)
                continue
            if job.model_calls >= settings.agent_model_calls:
                raise AgentBudgetExceeded("Model call budget reached")
            job.model_calls += 1
            seq = len(steps) + 1
            history = [
                {"seq": step.seq, "name": step.name, "arguments": step.arguments, "result": step.result}
                for step in steps
            ]
            fence_checkpoint(session, claim)
            check_deadline(session, job)

        # No database connection or lock is held while waiting for the model.
        # 等待模型时不持有数据库连接或锁。
        decision = provider.decide(messages, history, timeout=min(settings.timeout, remaining)).validated()
        with SessionLocal.begin() as session:
            job = lock_agent_job(session, claim)
            check_deadline(session, job)
            if decision.name not in {"reply", "ask_clarification"}:
                if job.tool_calls >= settings.agent_tool_calls:
                    raise AgentBudgetExceeded("Tool call budget reached")
                job.tool_calls += 1
            session.add(AgentStep(job_id=job.id, seq=seq, name=decision.name, arguments=decision.arguments))
            fence_checkpoint(session, claim)
            check_deadline(session, job)
        log.info("job=%s step=%s action=%s checkpoint=ready", claim.job_id, seq, decision.name)
