"""Kill actual worker processes on either side of the durable boundaries.
在持久化边界两侧终止真实 worker 进程。
"""

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from test_agent import Scripted
from test_resolution_workflow import decide, plan, submit
from test_resolution_workflow import demo_tickets as demo_tickets

from app.agent_contract import Decision
from app.db import SessionLocal
from app.models import AgentStep, AppliedResolution, Job, Ticket
from app.worker import run_once


@pytest.mark.parametrize("phase", ["during_model", "after_checkpoint", "before_commit", "after_commit"])
def test_process_death_and_two_worker_recovery(client, demo_tickets, tmp_path, phase):
    if phase in {"before_commit", "after_commit"}:
        path, _, _, proposal = plan(client)
        job_id = decide(client, proposal).json()["resume_job_id"]
    else:
        path, _, receipt = submit(client, "Please review ticket 391")
        job_id = receipt["job_id"]
    marker = tmp_path / "checkpoint"
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("crash_worker.py")), phase, str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.025)
    finally:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
    assert marker.exists(), (stdout, stderr)
    assert not stderr, (stdout, stderr)
    with SessionLocal() as session:
        job = session.get(Job, UUID(job_id))
        assert job.status == ("SUCCEEDED" if phase == "after_commit" else "RUNNING")
        if phase == "after_checkpoint":
            step = session.scalars(select(AgentStep)).one()
            assert step.finished_at and step.result["ticket_id"] == "391"
        assert (session.scalar(select(AppliedResolution)) is not None) == (phase == "after_commit")
        assert session.get(Ticket, 391).status == ("RESOLVED" if phase == "after_commit" else "OPEN")
    time.sleep(1.2)
    choices = [Decision("prepare_refund", {"ticket_id": 391})]
    if phase == "after_checkpoint":
        choices.insert(0, Decision("lookup_ticket", {"ticket_id": 391}))

    def replacement(i):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if run_once(f"replacement-{i}", provider=Scripted(*choices)):
                return True
            with SessionLocal() as session:
                if session.get(Job, UUID(job_id)).status == "SUCCEEDED":
                    return False
            time.sleep(0.05)
        pytest.fail("Replacement worker could not recover the job")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(replacement, range(2)))
    assert sum(results) == (0 if phase == "after_commit" else 1)
    if phase in {"during_model", "after_checkpoint"}:
        proposal = client.get(path).json()[0]["proposal"]
        assert proposal["status"] == "WAITING_FOR_APPROVAL"
        assert decide(client, proposal).status_code == 200
        assert run_once()
    with SessionLocal() as session:
        assert len(session.scalars(select(AppliedResolution)).all()) == 1
        assert session.get(Ticket, 391).status == "RESOLVED"
        job = session.get(Job, UUID(job_id))
        assert job.status == "SUCCEEDED"
        assert job.attempts == (1 if phase == "after_commit" else 2)
    assert len(client.get(path).json()) == 2
    assert not run_once()
    submit(client, "Next turn", conversation=path.split("/")[2])
    assert run_once()
    assert len(client.get(path).json()) == 4
