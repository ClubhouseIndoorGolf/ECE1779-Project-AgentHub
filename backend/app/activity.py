"""Public execution activity shared by history and live updates.
历史与实时更新共用的公开执行进度。
"""

from sqlalchemy import select

from app.event_store import EventToAppend
from app.models import AgentStep, Job

TOOL_LABELS = {
    "lookup_ticket": ("Looking up ticket and order", "Retrieved ticket and order"),
    "check_refund": ("Checking refund eligibility", "Checked refund eligibility"),
    "prepare_refund": ("Preparing a resolution proposal", "Finished proposal check"),
    "ask_clarification": ("Preparing a clarification", "Requested clarification"),
}


def public_step(step):
    """Expose fixed labels, never model arguments, drafts or raw tool data.
    只公开固定标签，不暴露模型参数、草稿或原始工具数据。
    """
    if step.name not in TOOL_LABELS:
        return None
    completed = step.result is not None
    text = TOOL_LABELS[step.name][int(completed)]
    outcome = (step.result or {}).get("outcome")
    if completed:
        if outcome == "NOT_FOUND":
            text = "Requested record unavailable"
        elif step.name == "check_refund":
            text += {
                "ELIGIBLE": " — eligible",
                "INELIGIBLE": " — not eligible",
                "NEEDS_INFORMATION": " — more information needed",
            }.get(outcome, "")
        elif step.name == "prepare_refund" and outcome == "WAITING_FOR_APPROVAL":
            text = "Prepared proposal for human review"
    return {
        "seq": step.seq,
        "name": step.name,
        "status": "completed" if completed else "running",
        "text": text,
    }


def step_event(job, step):
    """Record activity in the same transaction as its checkpoint.
    在检查点的同一事务中记录进度。
    """
    public = public_step(step)
    if public is None:
        return []
    return [
        EventToAppend(
            "agent.step_completed" if step.result is not None else "agent.step_started",
            {"job_id": str(job.id), "source_message_id": str(job.source_message_id), "step": public},
        )
    ]


def read_activity(session, source_ids):
    """Batch-load initial jobs and their steps for an authorized history page.
    为已授权的历史页面批量读取初始待办及步骤。
    """
    if not source_ids:
        return {}
    jobs = session.scalars(
        select(Job).where(Job.source_message_id.in_(source_ids), Job.kind == "PROCESS_MESSAGE")
    ).all()
    if not jobs:
        return {}
    activity = {
        job.source_message_id: {
            "job_id": str(job.id),
            "status": job.status,
            "attempts": job.attempts,
            "steps": [],
        }
        for job in jobs
    }
    sources = {job.id: job.source_message_id for job in jobs}
    steps = session.scalars(
        select(AgentStep).where(AgentStep.job_id.in_(sources)).order_by(AgentStep.seq)
    ).all()
    for step in steps:
        public = public_step(step)
        if public is not None:
            activity[sources[step.job_id]]["steps"].append(public)
    return activity
