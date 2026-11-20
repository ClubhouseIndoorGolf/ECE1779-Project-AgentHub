"""Join the independently deployed agent and durable event migrations.
合并独立部署的 Agent 与持久事件迁移分支。
"""

revision = "0008_merge_agent_events"
down_revision = ("0007_agent_steps", "0007_conversation_events")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
