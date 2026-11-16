# AgentHub 审批和执行事件持久化与 SSE 回放计划

**后端已实现，固定模式双 API 副本批准路径验收通过。** 实现从 `main` 的 `3d5bf59` 开始，验收版本为 `756382d`。目标是让客户端断线期间发生的批准、拒绝、任务进度和执行结果都能按顺序补回，连接另一 API 副本时也能用同一游标续传。

默认 SSE 的数字游标对应消息的 `seq`。消息可按游标补发，待办和方案更新则根据连接期间观察到的状态变化推送。历史接口恢复当前状态，新版流补发各次审批和执行的公开状态变化。 [当前 SSE 行为](../../LIVE_UPDATES.md)

## 数据结构与事务边界

从现有 `0006_approved_resolutions` 增加 Alembic revision `0007`。

| 表 | 字段和用途 |
| --- | --- |
| `conversation_event_heads` | `conversation_id` 为主键并引用会话，`last_event_seq` 为 `BIGINT`，初始值为 `0`。迁移为已有会话创建计数器，新会话与计数器同事务创建。 |
| `conversation_events` | 使用 `(conversation_id, seq)` 作为主键，记录 `event_type`、`payload`（JSONB）和 `created_at`。`conversation_id` 引用对应的计数器行。按主键范围读取同一会话的事件页。 |

追加函数接收调用方的数据库 session。调用方完成需要加锁的业务读写后，再锁定本会话的计数器，递增序号并插入事件。同一事务产生多个事件时，按业务顺序分配一段连续序号。取得计数器锁后，后续操作只触及已锁定的业务行和事件表，避免改变现有锁顺序。持有 worker 租约的路径在完成写入前再次核验当前令牌和有效期。

计数器更新与事件插入随业务事务一起提交或回滚。同一会话的事务通过计数器行串行分配序号。这样，一旦订阅读到了事件 `43`，事件 `42` 就已经可读。客户端以序号排序，时间戳供诊断使用。

| 事件 | 写入位置和条件 |
| --- | --- |
| `message.created` | `main.py` 首次受理用户消息时写入，`worker.py` 或 `resolution_workflow.py` 保存最终回复时写入。事件含消息 ID 和消息分页用的 `message_seq`。相同提交键的重试沿用原回执。 |
| `proposal.created` | `support_workflow.py` 保存不可修改的方案时写入，随执行状态 `WAITING_FOR_APPROVAL` 和初始待办完成一起提交。 |
| `proposal.decided` | `main.py` 首次保存 `APPROVE` 或 `REJECT` 时写入。批准事件包含恢复待办 ID，与审批记录和入队一起提交。相同决定的重试沿用原回执，冲突决定返回 `409`。 |
| `job.status_changed` | `worker.py` 领取、接管、重试、完成、失败或耗尽尝试次数时写入。包括公开状态和尝试次数。租约心跳只更新租约。 |
| `resolution.completed` | `resolution_workflow.py` 完成处置时写入，与模拟退款、客户回复、工单更新、最终聊天回复和恢复待办完成一起提交。 |
| `resolution.invalidated` | `resolution_workflow.py` 的过期扫描或执行前复核，以及 `main.py` 处理已过期方案的路径，在首次保存失效状态时写入。 |

事件载荷保存变化发生时的公开事实，例如资源 ID、审批决定、待办状态、尝试次数或失效原因。回放直接读取已提交的载荷。页面通过现有历史或待办接口刷新详细内容，并保持已有的请求时序保护。新版序列从部署启用后开始记录，已有消息、方案和结果通过历史接口加载。

## SSE 游标和兼容行为

| 场景 | 契约 |
| --- | --- |
| 旧版订阅 | `GET /conversations/{id}/events`。沿用数字 `Last-Event-ID: 42` 及现有消息回放规则。 |
| 新版订阅 | `GET /conversations/{id}/events?version=2`。使用 `Last-Event-ID: v2:42`，按顺序返回会话内 `seq > 42` 的已提交事件。事件帧的 `id` 使用 `v2:<seq>`。 |
| 首次打开或从旧版切换 | 历史接口保持原有 JSON 列表，在响应头 `X-Conversation-Event-Cursor` 返回新游标。首屏历史和计数器在同一个一致性快照中读取。客户端以此游标开启新版流，后续消息分页仍用 `after_seq`。 |
| 无效新版游标 | 新版请求需要有效游标。显式传 `v2:0` 可回放迁移启用以来的全部事件。缺少游标、纯数字旧游标、格式错误或超过会话上界时，在开流前返回 `422`，客户端重新取历史快照。 |

游标序号限定为非负 ASCII 十进制、在 PostgreSQL `BIGINT` 范围内。客户端完整接收事件帧后才推进游标，并按会话与事件序号去重。连接通知、心跳和错误帧不推进游标。现有认证和会话归属检查在开流前执行，数据库查询继续使用短会话。客户端收到回放事件后刷新当前状态，避免旧事件直接覆盖较新的页面状态。

例如，历史接口读取待审批方案和游标 `v2:42`。客户端连流前，另一页面批准方案，worker 领取并完成处置。客户端连接任一 API 副本并发送 `Last-Event-ID: v2:42`，应收到批准、执行进度和完成事件。

## 开发顺序与验收

| 顺序 | 模块 | 验收 |
| --- | --- | --- |
| 1. 添加迁移及事件读写 | `backend/app/models.py`、新 Alembic revision、新增事件存储模块 | 从 `0006` 升级后，原有消息、待审批方案、决定和退款仍可读取。两个并发事务向同一会话追加事件时，已提交事件按序可见。回滚后计数器和事件同时恢复。 |
| 2. 接入原有事务 | `main.py`、`support_workflow.py`、`resolution_workflow.py`、`worker.py` | 覆盖批准、拒绝、过期、订单变更、重复决定、任务失败和 worker 接管。注入完成事务失败后，退款、最终回复和完成事件一起回滚。过期租约的 worker 无法写入结果或事件。 |
| 3. 增加新版 SSE 和快照游标 | `events.py`、`history.py`、`main.py`、`schemas.py`、`test_events.py` | 验证旧数字游标、新版分页回放和边界值、跨账号 `404`、历史取完后开流前发生审批的窗口。向前端提供事件格式及切换、重连和去重约定。 |
| 4. 固定模式双副本验收 | Compose 固定模式、隔离 PostgreSQL 测试、部署联调 | 从 API A 取得待审批快照和游标后断线，通过另一个页面批准并等到执行完成，再连到 API B。确认按序补回批准、进度和完成事件，页面恢复同一份方案，数据库仅有一笔模拟退款。用独立样例复测拒绝和失败。 |

发布时沿用现有部署脚本的顺序，等旧 API 和 worker 退出后执行迁移，再启动新副本。新副本就绪后启用新版订阅。首版验收使用固定模式。 [现有部署顺序](../../CLOUD_DEPLOYMENT.md#deployment-procedure) · [本地验证命令](../../DEVELOPMENT_HANDOFF.md#validate-changes)

## 第四步双 API 副本验收结果

2026 年 11 月 15 日，使用提交 `756382d` 在独立本地 Compose 项目中运行固定模式。API A 和 API B 是分别绑定回环端口的两个容器，共用一个 worker 和全新的 PostgreSQL。HTTP 客户端从 A 读到工单 `391` 的 `WAITING_FOR_APPROVAL` 快照与游标 `v2:5`，打开 SSE 并收到 `ready` 后断开。随后通过 B 批准方案，向 A 重复提交相同决定，得到同一回执。worker 完成后，用原游标连接 B，得到以下顺序。

| 游标 | 事件 | 记录内容 |
| --- | --- | --- |
| `v2:6` | `proposal.decided` | `APPROVE`，关联恢复待办 |
| `v2:7` | `job.status_changed` | `QUEUED` |
| `v2:8` | `job.status_changed` | `RUNNING` |
| `v2:9` | `resolution.completed` | `COMPLETED` |
| `v2:10` | `message.created` | 最终助手回复 |
| `v2:11` | `job.status_changed` | `SUCCEEDED` |

B 的历史接口返回同一方案，状态为 `COMPLETED`，首屏游标为 `v2:11`。只读核查数据库确认订单 `1001` 只有一条模拟处置记录，金额为 CAD 129.99，保存了方案中的客户回复。工单 `391` 为 `RESOLVED`，同时只有一个恢复待办和一条完成事件。本轮双副本验收范围为批准路径，使用 HTTP 客户端直连本地 Compose 容器。验收后已清理独立环境。 [手动复现 MT20](MANUAL_TESTING.md#mt20-跨-api-副本续传持久化-sse)
