"""A subprocess-only fault harness; never imported by the production worker.
仅供子进程故障实验使用，不由生产 worker 导入。
"""

import sys
import time
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.agent_contract import Decision
from app.db import engine
from app.worker import run_once

phase, marker = sys.argv[1:]
committing = False


def pause():
    Path(marker).write_text("ready", encoding="utf-8")
    while True:
        time.sleep(1)


class InterruptedProvider:
    mode = "scripted"

    def decide(self, _messages, steps, *, timeout=None):
        if phase == "during_model" or (phase == "after_checkpoint" and steps):
            pause()
        return Decision("lookup_ticket", {"ticket_id": 391})


def before_statement(_conn, _cursor, statement, parameters, *_args):
    global committing
    if statement.startswith("UPDATE jobs") and parameters.get("status") == "SUCCEEDED":
        committing = True
        if phase == "before_commit":
            pause()


def after_commit(_session):
    if committing and phase == "after_commit":
        pause()


event.listen(engine, "before_cursor_execute", before_statement)
event.listen(Session, "after_commit", after_commit)
deadline = time.monotonic() + 8
while time.monotonic() < deadline:
    if run_once("doomed-process", lease_seconds=1, provider=InterruptedProvider()):
        break
    time.sleep(0.05)
