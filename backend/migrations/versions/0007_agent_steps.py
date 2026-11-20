"""Persist bounded agent checkpoints and revalidation evidence.
持久保存有界 Agent 检查点与重新核验依据。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_agent_steps"
down_revision = "0006_approved_resolutions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("model_calls", sa.Integer(), server_default="0", nullable=False))
    op.add_column("jobs", sa.Column("tool_calls", sa.Integer(), server_default="0", nullable=False))
    op.add_column("jobs", sa.Column("agent_deadline_at", sa.DateTime(timezone=True)))
    op.create_check_constraint("ck_jobs_agent_budget", "jobs", "model_calls >= 0 AND tool_calls >= 0")
    op.add_column("executions", sa.Column("revalidated_at", sa.DateTime(timezone=True)))
    op.add_column("executions", sa.Column("invalidation_reason", sa.Text()))
    op.create_table(
        "agent_steps",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(none_as_null=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("seq > 0", name="ck_agent_steps_seq"),
        sa.CheckConstraint(
            "name IN ('reply', 'ask_clarification', 'lookup_ticket', 'check_refund', 'prepare_refund')",
            name="ck_agent_steps_name",
        ),
        sa.CheckConstraint("jsonb_typeof(arguments) = 'object'", name="ck_agent_steps_arguments"),
        sa.CheckConstraint(
            "(result IS NULL AND finished_at IS NULL) OR (result IS NOT NULL AND jsonb_typeof(result) = 'object' AND finished_at IS NOT NULL)",
            name="ck_agent_steps_result",
        ),
    )


def downgrade():
    # Preserve audit history rather than silently discarding completed model work.
    # 保留审计历史，不静默丢弃已完成的模型工作。
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM agent_steps)")):
        raise RuntimeError("Cannot downgrade while agent steps exist")
    op.drop_table("agent_steps")
    op.drop_column("executions", "invalidation_reason")
    op.drop_column("executions", "revalidated_at")
    op.drop_constraint("ck_jobs_agent_budget", "jobs", type_="check")
    for name in ("agent_deadline_at", "tool_calls", "model_calls"):
        op.drop_column("jobs", name)
