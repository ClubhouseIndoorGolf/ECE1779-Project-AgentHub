"""Shared fencing rules for replies and agent checkpoints.
回复与 Agent 检查点共用的租约保护规则。
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, func, update

from app.models import Job


class LostLease(Exception):
    pass


@dataclass(frozen=True)
class Claim:
    job_id: UUID
    token: UUID
    kind: str


def owned_job(claim):
    return and_(
        Job.id == claim.job_id,
        Job.status == "RUNNING",
        Job.lease_token == claim.token,
        Job.lease_expires_at > func.clock_timestamp(),
    )


def fence_checkpoint(session, claim):
    """Recheck live time after lock waits and roll back all writes if ownership expired.
    锁等待后重新核验实时时间，失去领取权时回滚全部写入。
    """
    session.flush()
    changed = session.execute(
        update(Job)
        .where(owned_job(claim))
        .values(updated_at=func.clock_timestamp())
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        raise LostLease()
