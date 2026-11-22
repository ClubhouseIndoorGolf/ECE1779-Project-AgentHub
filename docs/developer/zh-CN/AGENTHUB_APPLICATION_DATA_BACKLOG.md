# AgentHub 应用与数据后端后续开发计划

更新于 2026 年 11 月 20 日。负责人为 Jinxi Hu，范围包括应用 API、会话存储、数据库迁移、账号权限、审批和 SSE 接口。核查基线为 `main` 的 `f2d27f3`，数据库迁移头为 `0009_conversation_management`。会话与消息存储、账号隔离、提交幂等、模拟退款审批、SSE v2 续传、执行详情和请求错误码均已合入。下一阶段先完成浏览器与云端验收，再决定是否补充退款详情字段。 [项目分工](../../AgentHub_Project_Proposal.md#proposed-division-of-labour) · [数据库参考](../../BACKEND_DATABASE.md) · [会话管理迁移](../../CHAT_MANAGEMENT.md)

## 1. 验收浏览器的 SSE v2 和退款详情

SSE v2 API 已通过本地 Compose 双副本的 HTTP 客户端验收。Chengxi 的浏览器接入 PR #10 仍是草稿，目标分支还是 `feature/agent-execution-completion`。按当前 `main` 模拟合并时，`chat.js`、`index.html` 和 `LIVE_UPDATES.md` 有冲突。Chengxi 负责同步分支和处理客户端重连，Jinxi 负责核对游标、事件格式和账号归属。 [前端 PR #10](https://github.com/RavenHu001/ECE1779-Project-AgentHub/pull/10) · [SSE v2 契约](../../LIVE_UPDATES.md#durable-event-replay) · [既有双副本验收记录](AGENTHUB_DURABLE_SSE_REPLAY_PLAN.md)

- [ ] 在同步 `main` 后的集成版本上按 MT20 验证。浏览器先连接 API A，记录已处理的游标，断线后在 API B 批准方案，再让浏览器连到 B。审批、任务状态和退款结果按顺序恢复，界面不重复显示消息、进度或结果。重复提交原审批仍返回同一回执。
- [ ] 按 MT21 验证只读资格检查和已完成的模拟退款。重开会话后可以打开已保存的执行详情，看到原资格结论、审批结果和实际金额。另一个账号请求同一执行 ID 返回 `404`。

[MT20 双副本续传](../../MANUAL_TESTING.md#mt20-resume-durable-sse-on-another-api-replica) · [MT21 执行详情](../../MANUAL_TESTING.md#mt21-read-saved-execution-details)

## 2. 验收云端恢复后的业务数据

云端 PR #13 已记录从 Spaces 备份恢复到隔离数据库及 schema 校验。Yuzhe 负责 Swarm、HTTPS 和恢复环境，Shawn 负责 worker 接管，Jinxi 负责迁移版本、恢复记录和认证 SSE 的验收。 [云端 PR #13](https://github.com/RavenHu001/ECE1779-Project-AgentHub/pull/13) · [云端演练](../../CLOUD_DEPLOYMENT.md#safe-local-checks-and-consolidated-integration-plan)

- [ ] 在备份前完成一笔模拟退款，记录合成会话、方案和执行 ID。将包含该用例的备份恢复到隔离环境，确认迁移头为 `0009_conversation_management`。通过 API 核对原会话、审批回执和退款详情，再核对同一方案只有一条 `applied_resolutions` 记录，其中保存的客户回复与获批方案一致。
- [ ] 在团队约定的演练环境通过 HTTPS 验证认证 SSE。用有效账号连接，记录游标，断线期间完成审批，再续传并核对审批与退款事件。核对未认证请求返回 `401`，另一个账号请求同一会话流返回 `404`。

## 3. 在退款详情中显示方案失效原因（可选）

`GET /executions/{execution_id}` 已能返回 `INVALIDATED`，会话历史里的方案也已返回 `invalidation_reason` 和 `revalidated_at`。如果详情页需要解释方案为何失效，Jinxi 可以在执行详情响应中增加这两个可空字段，直接读取现有数据，无需迁移。前端按需展示失效原因，原始退款资格结论继续读取 `outcome` 和 `reason_codes`。 [详情响应模型](../../../backend/app/schemas.py) · [历史序列化](../../../backend/app/history.py) · [审批后重新验证规则](../../AGENT_EXECUTION.md#stale-approval-rule)

- [ ] 验证过期、订单变化和政策变化至少各一例。详情的失效原因和重新验证时间与会话历史一致，`result` 为 `null`。
- [ ] 验证旧记录可以返回空字段，已完成退款仍显示原有金额和审批回执，其他账号查询仍返回 `404`。

## 已完成 1. 退款检查、审批和处理结果查询

后端已通过 [PR #8](https://github.com/RavenHu001/ECE1779-Project-AgentHub/pull/8) 合入。

用户重新打开会话时，可以看到订单能不能退款、方案是批准还是拒绝，以及获批后退款有没有完成。比如，用户只问「这笔订单能退吗」，页面就显示检查结论和原因。如果退款已完成，页面还能显示实际的模拟退款金额。后端负责提供这些数据，前端接入接口后负责展示。

退款检查结果保存在 `executions` 表里。历史接口返回来源消息的 `execution_id`，前端可据此读取结构化的资格、审批和模拟退款结果。 [开发说明](../EXECUTION_DETAILS.md) · [API 示例](../../../README.md#read-a-saved-refund-result) · [执行记录模型](../../../backend/app/models.py)

### 后端实现

- [x] **让历史消息指向任务。** 在触发退款检查或审批流程的用户消息上，返回对应的 `execution_id`。这个 ID 对应数据库里已保存的执行记录。沿用现有消息分页和首屏的 SSE v2 游标。
- [x] **增加任务详情接口。** 添加只读接口 `GET /executions/{execution_id}`，返回退款资格、原因码、金额和政策版本。有审批记录时返回决定和时间。有后续任务时返回执行状态。有已保存的处置记录时返回最终结果。
- [x] **检查谁能查看。** 通过任务对应的用户消息找到会话，只有会话所属账号可以读取详情。用公开响应模型明确返回字段，订单编号和以分为单位的金额沿用现有方案接口的字符串格式。 [响应模型](../../../backend/app/schemas.py)

### 回归验收用例

| 操作 | 应该看到什么 |
| --- | --- |
| Alice 只检查一笔订单能否退款，然后重新打开会话 | 根据历史消息里的 `execution_id`，可以查到原来的检查结论和原因码 |
| Alice 批准退款并等待任务完成 | 详情显示原审批决定和已保存的模拟退款结果 |
| Alice 拒绝方案，或方案过期，或获批后的后台任务失败 | 详情显示对应的决定或状态，处置结果为空 |
| Bob 用 Alice 的执行 ID 查询，或查询一个不存在的 ID | 两种请求都返回 `404` |
| 读取普通聊天和已有会话 | 历史记录和分页可正常读取。有执行记录的消息带 `execution_id`，首屏仍返回 SSE v2 游标 |

## 已完成 2. 请求校验和冲突错误码

后端已通过 [PR #12](https://github.com/RavenHu001/ECE1779-Project-AgentHub/pull/12) 合入。标题或正文含 NUL 时返回 `422`。消息提交或审批与当前状态冲突时返回 `409`，并给出前端可以识别的错误码。 [开发说明](../REQUEST_ERROR_CODES.md)

### 在写入数据库前检查标题和正文

标题和正文的输入模型在写入数据库前拒绝 NUL（`U+0000`），错误说明使用英文。 [输入模型](../../../backend/app/schemas.py)

- [x] 创建会话或发送消息时，如果标题或正文含 NUL，返回 `422`，并核对数据库里没有新增对应的会话、消息或任务。
- [x] 用中文和表情符号做回归测试，确认正常输入能保存，消息正文保留原文。

### 给不同的业务冲突加错误码

接口保留 HTTP `409` 和原有英文错误说明 `detail`，并返回顶层 `code`。前端可据此按下表处理。 [提交与审批接口](../../../backend/app/main.py) · [当前页面的错误处理](../../../backend/app/static/chat.js)

| 用户遇到的情况 | `code` | 页面可以怎么处理 |
| --- | --- | --- |
| 发送不同的正文时沿用了上一条消息的提交键 | `IDEMPOTENCY_CONFLICT` | 保留草稿，用户确认发新消息时再生成新键 |
| 会话里还有一份方案等审批、获批后的处理尚未完成，或前序轮次阻挡了审批 | `CONVERSATION_BUSY` | 刷新历史，显示正在等待的步骤 |
| 用户审批时，方案已经过期 | `PROPOSAL_EXPIRED` | 刷新方案，提示用户重新发起请求 |
| 用户提交的决定与已经保存的决定不同 | `DECISION_CONFLICT` | 展示已保存的决定 |
| 提交的方案版本与当前版本不同 | `PROPOSAL_VERSION_CONFLICT` | 刷新方案，再让用户确认决定 |
| 方案已退出待审批状态，且没有已保存的过期原因 | `PROPOSAL_NOT_PENDING` | 刷新并显示当前状态 |

- [x] 测试每种冲突是否返回对应的 `409`、`detail` 和 `code`。
- [x] 用同一个提交键和同一份正文重试，或再次提交相同版本和决定，确认接口仍返回原回执。
- [x] 确认审批发现过期时先提交失效状态和事件，再返回 `409`。事件写入失败时回滚，worker 已处理的过期方案返回同一个 `PROPOSAL_EXPIRED`。 [审批集成测试](../../../backend/tests/test_resolution_workflow.py)

## 团队确认后再排的工作

- **可撤销的登录会话。** 当前用 HTTP Basic 验证账号。如果验收要求服务端登出、会话到期或撤销，再实现登录会话，并约定已打开的 SSE 连接何时关闭。 [认证草案](./AGENTHUB_BACKEND_V1_SPEC.md#通用约定与认证)
- **只回复客户并关闭工单。** 如果业务范围包含无退款关单，再把客户回复和工单更新一起保存为方案，获批后执行。 [业务验收草案](./AGENTHUB_BACKEND_V1_SPEC.md#第一版验收案例)
- **任务列表和旧会话分页。** 如果前端需要在会话里按列表查看多笔任务，或需要查看超过当前列表上限的旧会话，再补对应的分页接口。

## 测试和交接

后端回归使用固定回复模式和 Compose 的隔离 PostgreSQL 测试库。每次验收记录提交版本、迁移版本、环境、操作或命令、预期及实际结果。浏览器人工验收、离线客户端测试、Compose 冒烟、云端实测和真实模型检查分别记录。若详情接口有改动，随实现更新英文 API 示例和开发说明。 [测试命令](../../DEVELOPMENT_HANDOFF.md#validate-changes)
