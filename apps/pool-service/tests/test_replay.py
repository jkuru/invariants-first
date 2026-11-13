"""The happy-path triage flows on a real-model path: a replayed trace by default (APP_MODEL=replay).

The committed traces in tests/fixtures/traces/ are hand-written fixtures in the shape of a recording,
not recordings (see the README there). `pytest --record` with ANTHROPIC_API_KEY set replaces them
with real ones, marked "fixture": "recorded".
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from pool_service import store
from conftest import make_pool

OOM_LOG = ("2026-11-02T03:14:07Z trainer[4242]: allocating 6.0 GiB for batch 118\n"
           "2026-11-02T03:14:09Z kernel: Out of memory: Killed process 4242 (trainer) total-vm:9123456kB")
SLOW_LOG = "2026-11-02T04:00:00Z worker: step 41/80\n2026-11-02T04:10:00Z worker: deadline exceeded after 600s"
VAGUE_LOG = "2026-11-02T05:00:00Z process exited"


@pytest.fixture
def jobs(client: TestClient) -> TestClient:
    make_pool(client, "gpu-a", 10)
    for job, kw in (("J-oom", {"oom_killed": True, "elapsed_s": 10, "exit_code": 137}),
                    ("J-slow", {"oom_killed": False, "elapsed_s": 600, "exit_code": 1}),
                    ("J-vague", {"oom_killed": False, "elapsed_s": 5, "exit_code": 1})):
        r = client.post("/jobs", json={"job_id": job, "pool_id": "gpu-a", "timeout_s": 600, **kw})
        assert r.status_code == 201, r.text
    return client


def triage_job(client: TestClient, job_id: str, log: str):
    return client.post(f"/jobs/{job_id}/triage", json={"log_excerpt": log})


def test_an_oom_and_a_timeout_are_remediated(jobs: TestClient, traced, conn: sqlite3.Connection) -> None:
    traced("oom_and_timeout")
    r = triage_job(jobs, "J-oom", OOM_LOG)
    assert r.status_code == 200, r.text
    assert r.json()["cause"] == "OUT_OF_MEMORY" and r.json()["action"] == "raise_memory_limit"
    r = triage_job(jobs, "J-slow", SLOW_LOG)
    assert r.status_code == 200, r.text
    assert r.json()["cause"] == "TIMEOUT" and r.json()["action"] == "extend_timeout"
    assert len(store.remediations(conn, "J-oom")) == 1


def test_a_log_that_shows_no_cause_goes_to_a_person(jobs: TestClient, traced, conn: sqlite3.Connection) -> None:
    traced("cannot_comply")
    r = triage_job(jobs, "J-vague", VAGUE_LOG)
    assert r.status_code == 202 and r.json()["rule"] == "R-7"
    assert store.remediations(conn, "J-vague") == []


def test_an_extra_field_is_fixed_on_the_second_turn(jobs: TestClient, traced, model_mode: str) -> None:
    """The first reply adds a confidence field the closed schema forbids; the parser error goes back
    as a follow-up turn in the same conversation, and the second reply drops it."""
    if model_mode == "record":
        pytest.skip("a scripted format failure: a recording would replace it with whatever the model does")
    traced("oom_format_fix")
    r = triage_job(jobs, "J-oom", OOM_LOG)
    assert r.status_code == 200 and r.json()["cause"] == "OUT_OF_MEMORY"
    if model_mode == "replay":
        assert r.json()["attempts"] == 2
