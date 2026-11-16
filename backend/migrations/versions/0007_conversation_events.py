"""Add per-conversation counters and a durable event log.
添加会话级计数器和持久化事件日志。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_conversation_events"
down_revision = "0006_approved_resolutions"
branch_labels = None
depends_on = None


def upgrade():
    """Initialize a zero event cursor for each existing conversation.
    为每个已有会话初始化事件游标，初始值为零。
    """
    # Initialize counters from conversation IDs while leaving business history intact.
    # 根据会话编号初始化计数器，保留已有业务历史。
    op.create_table(
        "conversation_event_heads",
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("last_event_seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("last_event_seq >= 0", name="ck_conversation_event_heads_seq"),
    )
    op.execute("INSERT INTO conversation_event_heads (conversation_id) SELECT id FROM conversations")

    # The composite primary key supports replay without an additional index.
    # 复合主键可直接支持事件回放，无须额外索引。
    op.create_table(
        "conversation_events",
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversation_event_heads.conversation_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("seq > 0", name="ck_conversation_events_seq"),
        sa.CheckConstraint("event_type ~ '[^[:space:]]'", name="ck_conversation_events_type"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_conversation_events_payload"),
    )


def downgrade():
    """Drop replay history and its counters, retaining the original business records.
    移除回放记录及其计数器，保留原有业务记录。
    """
    # Drop the dependent log first so its foreign key no longer references the counters.
    # 先移除依赖计数器的日志表，解除外键引用。
    op.drop_table("conversation_events")
    op.drop_table("conversation_event_heads")
