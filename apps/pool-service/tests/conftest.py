"""Fixtures: a fresh SQLite file per test, the app on it, the fake model client, and the replay model.

The defect tests run on the fake (it can be told to misbehave). The happy-path flows in
test_replay.py run on the model APP_MODEL names: replay (the default) answers from
tests/fixtures/traces/; `--record` (or APP_MODEL=record) calls Claude and rewrites those traces.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
from typing import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

import invariants as inv
from pool_service import api, db, llm, triage
from pool_service.fake_model import FakeModelClient

APP_ROOT = pathlib.Path(__file__).resolve().parents[1]
TRACES = APP_ROOT / "tests" / "fixtures" / "traces"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--record", action="store_true", default=False,
                     help="APP_MODEL=record: run the replay flows on Claude and rewrite tests/fixtures/traces/ "
                          "(needs the anthropic package and ANTHROPIC_API_KEY)")


@pytest.fixture(scope="session", autouse=True)
def rules_loaded() -> inv.Registry:
    """The plugin loads invariants.yaml (ini inv_rules); load it here too when run without the plugin."""
    if not inv.default_registry().rules:
        api.load_rules(APP_ROOT / "invariants.yaml")
    return inv.default_registry()


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    p = tmp_path / "pool.db"
    db.init(p)
    return p


@pytest.fixture
def conn(db_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    c = db.connect(db_path)
    yield c
    c.close()


@pytest.fixture
def app(db_path: pathlib.Path):
    return api.create_app(db_path, rules=APP_ROOT / "invariants.yaml", load=False)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def model() -> Iterator[FakeModelClient]:
    c = FakeModelClient()
    triage.set_client(c)
    yield c
    triage.set_client(FakeModelClient())


@pytest.fixture
def model_mode(request: pytest.FixtureRequest) -> str:
    """replay (default), record, anthropic or fake: what the replay flows run on."""
    return "record" if request.config.getoption("--record") else os.environ.get("APP_MODEL", "replay")


@pytest.fixture
def traced(model_mode: str) -> Iterator[Callable[[str], llm.ModelClient]]:
    """traced(name): make triage use the APP_MODEL model for trace tests/fixtures/traces/<name>.json.
    On replay, every call the trace holds must have been made by the end of the test."""
    made: list[llm.ModelClient] = []

    def make(name: str) -> llm.ModelClient:
        m = llm.select(model_mode, TRACES / f"{name}.json")
        triage.set_client(m)
        made.append(m)
        return m
    yield make
    for m in made:
        if hasattr(m, "assert_done"):
            m.assert_done()


def make_pool(client: TestClient, pool_id: str = "gpu-a", size: int = 10) -> None:
    r = client.post("/pools", json={"pool_id": pool_id, "name": pool_id.upper(), "size": size})
    assert r.status_code == 201, r.text
