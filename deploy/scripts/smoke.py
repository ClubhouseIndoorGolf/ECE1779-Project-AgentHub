#!/usr/bin/env python3
# 通过公开 HTTP 接口验证 API 契约，无需导入后端或数据库模块。
"""Verify the public API contract without importing backend/database internals."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID, uuid4


class CheckFailed(Exception):
    # 可直接输出的诊断信息，省略服务器响应体和凭据。
    """A diagnostic that is safe to print without server bodies or credentials."""


# Report a failed API contract check with a controlled diagnostic.
# 用约定的诊断信息报告 API 契约检查失败。
def require(condition, message):
    if not condition:
        raise CheckFailed(message)


# Keep Basic authentication requests on the configured API origin.
# 将 HTTP Basic 认证请求限制在配置指定的 API 地址。
class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Fail before urllib can open the redirect target.
    # 在 urllib 访问重定向目标之前直接失败。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CheckFailed("Redirect refused; verify the configured API URL")


# Require a base origin without embedded credentials or request-specific URL components.
# 基础地址须省略凭据及单个请求专用的路径、查询参数和片段。
def validate_url(value):
    url = urllib.parse.urlsplit(value)
    require(url.scheme in ("http", "https") and url.hostname, "Expected an HTTP(S) API URL")
    require(not url.username and not url.password, "Credentials must not appear in the URL")
    require(not url.query and not url.fragment and url.path in ("", "/"), "Use the API origin without a path, query or fragment")
    # Basic authentication over HTTP is permitted only through a loopback SSH tunnel.
    # HTTP Basic 认证仅允许使用回环 HTTP 地址，远程部署可通过 SSH 隧道访问。
    require(url.scheme == "https" or url.hostname in ("localhost", "127.0.0.1", "::1"), "Use HTTPS or a loopback SSH tunnel for authentication")
    return value.rstrip("/")


# Centralize authentication, API response checks, and message pagination.
# 统一处理身份认证、API 响应校验和消息分页。
class Client:
    # Keep the Basic authorization header and request opener in memory.
    # 在内存中保存 Basic 认证请求头和用于发起请求的 opener。
    def __init__(self, base_url, username, password):
        self.base_url = validate_url(base_url)
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.authorization = "Basic " + token
        # Do not forward deployment credentials through environment-configured proxies.
        # 禁用环境变量配置的代理，避免部署凭据经过代理转发。
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    # Send a bounded JSON request, adding authorization only when requested.
    # 发送有超时限制的 JSON 请求，根据需要添加认证请求头。
    def call(self, path, body=None, expected=200, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = self.authorization
        request = urllib.request.Request(
            self.base_url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers,
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                require(response.status == expected, "Unexpected API response status")
                return json.load(response)
        # Report the status code without exposing the response body or request credentials.
        # 报告 HTTP 状态码，省略响应体和请求凭据。
        except urllib.error.HTTPError as error:
            raise CheckFailed(f"API returned HTTP {error.code}; response body withheld") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise CheckFailed("API connection failed or response was not valid JSON") from None

    # Read one conversation in strictly increasing sequence order across pages.
    # 跨分页读取同一会话，并要求消息序号严格递增。
    def history(self, conversation):
        result = []
        cursor = 0
        while True:
            page = self.call(f"/conversations/{conversation}/messages?limit=500&after_seq={cursor}")
            require(isinstance(page, list), "Expected a message list")
            for message in page:
                require(message["conversation_id"] == conversation and message["seq"] > cursor, "Conversation history is not ordered or belongs to another conversation")
                cursor = message["seq"]
                result.append(message)
            # A short page ends the history. Full pages are subject to a size guard.
            # 不满一页表示历史读取结束，否则检查已读取的消息数量上限。
            if len(page) < 500:
                return result
            require(len(result) <= 10000, "History exceeded the smoke check limit")


# Hash a stable JSON serialization while preserving message order and Unicode content.
# 对稳定序列化后的 JSON 计算哈希，并保留消息顺序和 Unicode 内容。
def fingerprint(messages):
    canonical = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# Rewrite the receipt, trim stale bytes, and flush it to disk at each checkpoint.
# 在每个检查点覆盖写入回执，截去旧内容并将文件同步到磁盘。
def write_record(handle, record):
    handle.seek(0)
    json.dump(record, handle, indent=2)
    handle.write("\n")
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


# Submit one synthetic message and record the job and conversation history for a later check.
# 提交一条模拟消息，记录对应任务和会话历史，供后续校验。
def exercise(client, username, record_path, timeout):
    # Reserve the receipt before creating anything. Never overwrite a previous test.
    # 写入服务端之前先独占创建回执文件，保留已有测试记录。
    descriptor = os.open(record_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        # Save identifiers as they become known so a failed run leaves a diagnostic record.
        # 获取标识符后立即写入回执，以便运行失败时仍可定位已创建的记录。
        record = {"version": 1, "status": "pending", "api_url": client.base_url, "username": username}
        write_record(handle, record)
        conversation = client.call("/conversations", {"title": "Deployment smoke check"}, expected=201)["id"]
        UUID(conversation)
        record.update(conversation_id=conversation, submission_key=str(uuid4()))
        write_record(handle, record)
        payload = {"content": "Hello", "submission_key": record["submission_key"]}
        path = f"/conversations/{conversation}/messages"
        receipt = client.call(path, payload, expected=202)
        UUID(receipt["job_id"])
        UUID(receipt["message_id"])
        record.update(job_id=receipt["job_id"], message_id=receipt["message_id"])
        write_record(handle, record)
        # Reuse the same idempotency contract exercised by backend/app/smoke.py.
        # 用相同的提交键重试，验证与 backend/app/smoke.py 相同的幂等约定。
        require(client.call(path, payload, expected=202) == receipt, "Duplicate submission returned a different receipt")
        # Poll the job with a monotonic deadline so wall-clock changes do not alter the wait.
        # 用单调时钟计算任务轮询的截止时间，避免系统时间变化影响等待。
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = client.call(f"/jobs/{receipt['job_id']}")
            require(job["status"] != "FAILED", "Worker reported failure; inspect its logs privately")
            if job["status"] == "SUCCEEDED":
                require(job["source_message_id"] == receipt["message_id"], "Job references a different message")
                break
            time.sleep(0.5)
        else:
            raise CheckFailed("Timed out waiting for job completion; receipt retained, no automatic resubmission")
        # The existing API exposes conversation metadata through its list endpoint.
        # 通过现有会话列表接口查询会话元数据。
        conversations = client.call("/conversations?limit=500")
        require(any(item["id"] == conversation for item in conversations), "Created conversation is absent from the recent conversation list")
        # Check the saved request and its single linked reply before completing the receipt.
        # 确认已保存请求及其唯一关联回复，再将回执标为完成。
        messages = client.history(conversation)
        require(len(messages) == 2, "Expected exactly one request and one reply")
        require(messages[0]["id"] == receipt["message_id"] and messages[0]["role"] == "user" and messages[0]["content"] == "Hello", "Saved request differs from the submission")
        require(messages[1]["role"] == "assistant" and messages[1]["reply_to_message_id"] == receipt["message_id"], "Reply does not match its request")
        digest = fingerprint(messages)
        require(fingerprint(client.history(conversation)) == digest, "History changed between independent API reads")
        record.update(status="complete", message_count=len(messages), history_sha256=digest)
        write_record(handle, record)
    print(f"PASS: health, database, authentication, submission, worker and history; conversation_id={conversation}")


# Check that the receipt's exact conversation history and completed job still exist.
# 确认回执中的会话历史内容保持一致，且已完成任务仍可读取。
def verify(client, username, record_path):
    record = json.loads(record_path.read_text(encoding="utf-8"))
    require(record["version"] == 1 and record["status"] == "complete", "Receipt is incomplete or unsupported; inspect before retrying")
    require(record["api_url"] == client.base_url and record["username"] == username, "Receipt belongs to a different endpoint or account")
    # Validate identifiers before placing them in request paths.
    # 将标识符用于请求路径前，先校验其 UUID 格式。
    conversation = str(UUID(record["conversation_id"]))
    job_id = str(UUID(record["job_id"]))
    messages = client.history(conversation)
    require(len(messages) == record["message_count"] and fingerprint(messages) == record["history_sha256"], "Persisted conversation history differs from the saved baseline")
    job = client.call(f"/jobs/{job_id}")
    require(job["status"] == "SUCCEEDED" and job["source_message_id"] == record["message_id"], "Persisted job differs from the baseline")
    print(f"PASS: exact saved conversation history and completed job; conversation_id={conversation}")


# Route the CLI to plan, write, or read-only verification mode.
# 根据命令行参数进入计划、写入或只读验证模式。
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("API_BASE_URL", "http://127.0.0.1:18000"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="Describe checks without network access (default)")
    mode.add_argument("--write", action="store_true", help="Create one synthetic conversation and job")
    mode.add_argument("--verify-state", type=Path, help="Read-only comparison with a saved receipt")
    parser.add_argument("--record", type=Path, help="New receipt file outside the repository; required with --write")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)
    try:
        base_url = validate_url(args.url)
        require(1 <= args.timeout <= 3600, "Timeout must be between 1 and 3600 seconds")
        require(not args.record or args.write, "--record requires --write")
        # Exit before reading credentials or making requests in plan mode.
        # 计划模式在读取凭据或发出请求前结束。
        if not args.write and not args.verify_state:
            print("PLAN: check health/database and authentication; --write creates one fixed-mode conversation/job, verifies duplicate submission and records a history hash. --verify-state performs GET requests only. No requests sent.")
            return 0
        require(not args.write or args.record is not None, "--write requires a new --record path")
        username = os.environ.get("SMOKE_USERNAME", "")
        password = os.environ.get("SMOKE_PASSWORD", "")
        password_file = os.environ.get("SMOKE_PASSWORD_FILE", "")
        # Accept one credential source and remove line endings from a password file.
        # 接受一种凭据来源，并去掉密码文件末尾的换行。
        require(not (password and password_file), "Set only one of SMOKE_PASSWORD or SMOKE_PASSWORD_FILE")
        if password_file:
            password = Path(password_file).read_text(encoding="utf-8").rstrip("\r\n")
        require(username and password and ":" not in username, "Set SMOKE_USERNAME and a private password or password file")
        client = Client(base_url, username, password)
        # Require fixed reply mode and confirm the account before writing any test data.
        # 写入任何测试数据前，确认服务处于固定回复模式并校验登录账号。
        health = client.call("/health", authenticated=False)
        require(health.get("status") == "ok" and health.get("database") == "ok", "API/database health check failed")
        require(health.get("reply_mode") == "fixed", "This smoke check requires fixed reply mode")
        require(client.call("/me")["username"] == username, "Authenticated account differs from the requested account")
        if args.verify_state:
            verify(client, username, args.verify_state)
        else:
            exercise(client, username, args.record, args.timeout)
        return 0
    except CheckFailed as error:
        print(f"FAIL: {error}", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError):
        print("FAIL: invalid configuration, receipt, file access or API contract; sensitive details withheld", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
