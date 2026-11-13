"""HTTP-level rules (H-1, H-2, R-24), problem+json answers, and the startup checks."""
from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

import invariants as inv
from invariants.integrations import fastapi as inv_fastapi
from invariants.integrations import pydantic as inv_pydantic
from pool_service import api, triage
from conftest import APP_ROOT, make_pool

RULES = APP_ROOT / "invariants.yaml"


def test_create_and_get_pool(client: TestClient) -> None:
    make_pool(client, "gpu-a", 8)
    assert client.get("/pools/gpu-a").json() == {"pool_id": "gpu-a", "name": "GPU-A", "size": 8, "leased": 0}
    r = client.post("/pools", json={"pool_id": "gpu-a", "name": "again", "size": 8})
    assert r.status_code == 409
    assert client.get("/pools/nope").status_code == 404


def test_r24_pool_size_out_of_range(client: TestClient) -> None:
    """Falsifier: the service door refuses a pool of size 0 or above 10000."""
    for size in (0, 10001):
        r = client.post("/pools", json={"pool_id": f"p-{size}", "name": "x", "size": size})
        assert r.status_code == 422 and r.json()["rule"] == "R-24"
    assert client.get("/pools/p-0").status_code == 404


def test_h1_lease_request_size(client: TestClient) -> None:
    """Falsifier: the route guard refuses a lease of 0 or more than 64 slots."""
    make_pool(client, "big", 1000)
    for slots in (0, 65):
        r = client.post("/pools/big/leases", json={"holder": "w", "slots": slots})
        assert r.status_code == 422 and r.json()["rule"] == "H-1"
    assert client.get("/pools/big").json()["leased"] == 0


def test_h2_bad_pool_id(client: TestClient) -> None:
    """Falsifier: the route guard refuses a malformed pool id."""
    for pid in ("ab", "has space", "a/b", "x" * 33):
        r = client.post("/pools", json={"pool_id": pid, "name": "n", "size": 1})
        assert r.status_code == 422 and r.json()["rule"] == "H-2", pid


def test_problem_body_never_leaks_messages(client: TestClient) -> None:
    r = client.post("/pools", json={"pool_id": "ab", "name": "", "size": 1})
    assert r.headers["content-type"] == "application/problem+json"
    assert "violations" not in r.json() and "message" not in json.dumps(r.json())


def test_pydantic_rejects_unknown_fields(client: TestClient) -> None:
    r = client.post("/pools", json={"pool_id": "abc", "name": "n", "size": 1, "owner": "eve"})
    assert r.status_code == 422 and "rule" not in r.json()


def test_load_rules_checks_everything() -> None:
    with inv.isolated():
        reg = api.load_rules(RULES)
        assert set(reg.rules) == {"R-1", "R-7", "R-20", "R-21", "R-22", "R-23", "R-24", "H-1", "H-2"}
        assert inv_pydantic.check_contracts(RULES, reg) == {"triage_reply": triage.CauseReply}
        assert inv.verify(reg) == 9


def test_routes_bind_their_rules(app) -> None:
    assert inv_fastapi.verify_routes(app, RULES) == 2


def test_standing_without_scan(rules_loaded: inv.Registry) -> None:
    standing, why = inv.standing("R-20", rules_loaded)
    assert standing == "enforce(known-callers)" and why[-1] == "inv scan not run"
    assert any("concurrent leases" in w for w in why)
    assert inv.standing("R-21", rules_loaded, scan=True) == ("held", [])


def test_openapi_contract_app_is_the_served_app(app) -> None:
    """The pool_api contract reads pool_service.openapi_app:app (inv lock pins its operations); it must
    describe the same API as the app the service runs."""
    from pool_service import openapi_app
    assert openapi_app.app.openapi() == app.openapi()


def test_rules_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    from pool_service import config
    monkeypatch.setenv("POOL_SERVICE_RULES", str(tmp_path / "x.yaml"))
    monkeypatch.setenv("POOL_SERVICE_DB", str(tmp_path / "x.db"))
    assert config.rules_path() == tmp_path / "x.yaml" and config.db_path() == tmp_path / "x.db"
