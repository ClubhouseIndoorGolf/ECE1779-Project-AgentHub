#!/usr/bin/env bash
# Stage the stack, run setup once, and bring up the requested application replicas.
# 分阶段部署 stack，执行一次 setup，再启动所需的应用副本。
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"

# Default to printing the plan. Only apply mode can change services.
# 默认打印部署计划。只有 apply 模式会变更服务。
mode=${1:---plan}
[[ $# -le 1 && ( $mode == --plan || $mode == --apply ) ]] || fail 'Usage: bash deploy/scripts/deploy.sh [--plan|--apply]'
configure
if [[ $mode == --plan ]]; then
  note "PLAN for $STACK (no daemon, registry or SSH calls; no changes):"
  note '1. Acquire the shared manager lock and run online preflight.'
  note '2. Deploy the existing stack with API/worker/setup at zero; wait for old application tasks to stop.'
  note '3. Wait for PostgreSQL health, then run exactly one setup task and require exit code zero.'
  note "4. Return setup to zero; start API=$API_REPLICAS and worker=$WORKER_REPLICAS and verify health on every node."
  note 'Existing deployments incur planned downtime. No data deletion, automatic migration retry, or schema rollback.'
  exit 0
fi

need flock
# Every operator on this manager must use the same lock file, including other checkouts.
# 此管理节点上的所有操作者都须使用同一锁文件，即使从不同的检出目录运行。
exec 9>"$DEPLOY_LOCK_PATH" || fail 'Cannot open deployment lock; arrange manager permissions first'
flock -n 9 || fail 'Another deployment holds the manager lock'
stage=preflight
# Check the live environment under the lock before changing any service.
# 在持有部署锁的情况下检查运行环境，然后才变更服务。
trap 'printf "Deployment stopped during %s. Inspect service/task state before retrying; no automatic recovery performed.\n" "$stage" >&2' ERR
online_preflight

# Save the requested counts before staging the stack with zero application replicas.
# 在以零应用副本部署 stack 之前，保存请求的副本数。
target_api=$API_REPLICAS
target_worker=$WORKER_REPLICAS
stage=database-startup
# Quiesce API and worker tasks before running migrations.
# 执行数据库迁移之前，先停止 API 和 worker 任务。
API_REPLICAS=0 WORKER_REPLICAS=0 docker stack deploy --detach=true --resolve-image never --with-registry-auth -c "$STACK_FILE" "$STACK"
wait_stopped "${STACK}_api"
wait_stopped "${STACK}_worker"
wait_stopped "${STACK}_setup"
# Begin setup only after PostgreSQL is healthy and the old tasks are stopped.
# 等待旧任务停止且 PostgreSQL 健康后，再开始 setup。
wait_healthy "${STACK}_db" 1 "$DB_NODE"

stage=migration-and-bootstrap
# Require a dormant setup service with retries disabled before launching one task.
# 启动单次 setup 前，确认服务处于零副本且禁用了自动重启。
[[ $(docker service inspect --format '{{.Spec.Mode.Replicated.Replicas}}' "${STACK}_setup") == 0 ]] || fail 'Setup must be at zero before launch'
[[ $(docker service inspect --format '{{.Spec.TaskTemplate.RestartPolicy.Condition}}' "${STACK}_setup") == none ]] || fail 'Setup must not restart automatically'
previous=$(docker service ps --no-trunc -q "${STACK}_setup")
# Exclude historical tasks when verifying the new setup run.
# 验证本次 setup 执行结果时排除历史任务。
declare -A seen=()
while IFS= read -r id; do [[ -z $id ]] || seen["$id"]=1; done <<< "$previous"
docker service update --detach=true --replicas 1 --force "${STACK}_setup"
deadline=$((SECONDS + DEPLOY_TIMEOUT))
selected=''; completed=false
# Track one new task and use its terminal state and exit code to decide whether to proceed.
# 跟踪本次新增的唯一任务，根据终态和退出码决定是否继续。
while (( SECONDS < deadline )); do
  tasks=$(probe_before_deadline "$deadline" docker service ps --no-trunc -q "${STACK}_setup") || break
  while IFS= read -r id; do
    [[ -n $id && -z ${seen[$id]:-} ]] || continue
    [[ -z $selected || $selected == "$id" ]] || fail 'Multiple new setup tasks detected; stop for review'
    selected=$id
  done <<< "$tasks"
  if [[ -n $selected ]]; then
    state=$(probe_before_deadline "$deadline" docker inspect --type task --format '{{.Status.State}}' "$selected") || break
    case $state in
      complete)
        exit_code=$(probe_before_deadline "$deadline" docker inspect --type task --format '{{.Status.ContainerStatus.ExitCode}}' "$selected") || break
        # A completed query must still arrive within the setup wait budget.
        # 即使查询已完成，结果仍必须在 setup 等待预算内返回。
        (( SECONDS < deadline )) || break
        [[ $exit_code == 0 ]] || fail 'Setup returned a nonzero exit code'
        completed=true; break ;;
      failed|rejected|orphaned|shutdown) fail "Setup ended unsuccessfully ($state); applications remain stopped" ;;
    esac
  fi
  remaining=$((deadline - SECONDS))
  (( remaining > 0 )) || break
  sleep "$((remaining < 2 ? remaining : 2))"
done
[[ $completed == true ]] || fail 'Setup timed out; it may still be running. Do not restart it blindly.'
note 'Alembic migration and bootstrap completed with exit code 0.'
# Return setup to its dormant state so it is ready for the next deployment.
# 将 setup 恢复为零副本，为下次部署做好准备。
docker service scale --detach=true "${STACK}_setup=0"
wait_stopped "${STACK}_setup"

stage=application-startup
# Restore the requested replica counts after setup exits successfully.
# setup 成功退出后恢复所请求的副本数。
docker service scale --detach=true "${STACK}_api=$target_api" "${STACK}_worker=$target_worker"
wait_healthy "${STACK}_api" "$target_api"
wait_healthy "${STACK}_worker" "$target_worker"
note 'Deployment converged. Run the authenticated deployment smoke check separately.'
