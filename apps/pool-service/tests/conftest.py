"""Fixtures: a fresh SQLite file per test, the app on it, and the fake model client."""
from __future__ import annotations

import pathlib
import sqlite3
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

import invariants as inv
from pool_service import api, db, triage
from pool_service.fake_model import FakeModelClient

APP_ROOT = pathlib.Path(__file__).resolve().parents[1]


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


def make_pool(client: TestClient, pool_id: str = "gpu-a", size: int = 10) -> None:
    r = client.post("/pools", json={"pool_id": pool_id, "name": pool_id.upper(), "size": size})
    assert r.status_code == 201, r.text
