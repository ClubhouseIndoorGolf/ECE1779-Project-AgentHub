#!/usr/bin/env bash
# Provide a standalone configuration and cluster check without applying a deployment.
# 单独检查部署配置和集群，无需执行部署变更。
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"

mode=${1:---check}
[[ $# -le 1 && ( $mode == --check || $mode == --plan ) ]] || fail 'Usage: bash deploy/scripts/preflight.sh [--check|--plan]'
configure
# Both modes parse the stack locally. Online checks run only in check mode.
# 两种模式都会在本地解析 stack 配置，只有 check 模式执行在线检查。
if [[ $mode == --plan ]]; then
  note 'Configuration parsed. Plan mode does not contact a daemon, registry or SSH host.'
  note 'Online checks will verify manager identity, node labels, secret existence, storage mount/UUID, cached image platforms and existing database settings.'
else
  online_preflight
fi
