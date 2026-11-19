"""Persist pinned chats and retain audit records when removing chat history.
持久保存置顶状态，移除聊天历史时保留审计记录。
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_conversation_management"
down_revision = "0008_merge_agent_events"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "conversations", sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column("conversations", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    # Avoid silently restoring removed conversations on downgrade.
    # 避免降级时静默恢复已移除的会话。
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM conversations WHERE deleted_at IS NOT NULL LIMIT 1"))
        .first()
    ):
        raise RuntimeError("Removed conversations exist; use a forward migration")
    op.drop_column("conversations", "deleted_at")
    op.drop_column("conversations", "is_pinned")
