"""ASGI and FastAPI integration: guarded routes, HTTP status mapping, problem+json, openapi contracts."""
from __future__ import annotations

import copy
import json
from typing import Any, Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import integ_fastapi_app as app
import invariants as inv
from invariants.errors import Escalation, Halted, Rejected, RolledBack, Routed
from invariants.integrations import fastapi as inv_fastapi
from invariants.integrations.asgi import PROBLEM_JSON, InvariantMiddleware, StatusMap, problem
from invariants.model import Outcome

ORDER = {"sku": "widget", "quantity": 3}


def doc_with(mutate: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
    doc = copy.deepcopy(app.RULES)
    if mutate:
        mutate(doc)
    return doc


def rule(doc: dict[str, Any], rid: str) -> dict[str, Any]:
    return next(r for r in doc["rules"] if r["id"] == rid)


def client_for(doc: dict[str, Any], asgi_app: Any = None) -> tuple[TestClient, inv.Registry]:
    """A client whose requests run against a registry loaded from doc (via InvariantMiddleware)."""
    with inv.isolated() as reg:
        inv.load(doc)
    return TestClient(InvariantMiddleware(asgi_app or app.app, registry=reg)), reg


@pytest.fixture
def http() -> Iterator[tuple[TestClient, inv.Registry]]:
    app.PLACED.clear()
    yield client_for(doc_with())


def assert_problem(r: Any, status: int, rid: str | None, outcome: str | None) -> dict[str, Any]:
    assert r.status_code == status, r.text
    assert r.headers["content-type"] == PROBLEM_JSON
    body = r.json()
    assert body["status"] == status and body["type"].startswith("urn:invariants:") and body["title"]
    assert body.get("rule") == rid and body.get("outcome") == outcome
    return body


# --- pre_effect rules on a route ----------------------------------------------------------------
def test_ok_request_reaches_the_handler(http):
    c, reg = http
    r = c.post("/orders/eu", json=ORDER)
    assert r.status_code == 200 and r.json()["quantity"] == 3 and len(app.PLACED) == 1
    assert {"R-H1", "R-H2", "R-H3"} <= {e["rule"] for e in reg.sink}


def test_path_param_violation_is_422_and_handler_not_run(http):
    c, _ = http
    body = assert_problem(c.post("/orders/mars", json=ORDER), 422, "R-H1", "violation")
    assert app.PLACED == [] and "violations" not in body


def test_body_and_provider_violation(http):
    c, _ = http
    assert_problem(c.post("/orders/us", json={"sku": "widget", "quantity": 11}), 422, "R-H2", "violation")
    assert app.PLACED == []


def test_query_param_is_bound(http):
    c, _ = http
    r = c.post("/orders/us?dry_run=true", json=ORDER)
    assert r.status_code == 200 and app.PLACED == []


def test_route_gives_202_with_review_id(http):
    c, reg = http
    body = assert_problem(c.post("/orders/eu", json={"sku": "gizmo", "quantity": 200, "kind": "rush"}),
                          202, "R-H3", "violation")
    assert body["review_id"] == "review-1" and reg.reviews[0]["rule"] == "R-H3" and app.PLACED == []


def test_rule_error_fail_closed_is_503_without_internals(http):
    c, _ = http
    r = c.post("/orders/eu", json={"sku": "broken", "quantity": 1})
    assert_problem(r, 503, "R-H2", "rule_error")
    assert "hunter2" not in r.text and "10.0.0.7" not in r.text and "stock" not in r.text


def test_rule_error_fail_open_proceeds():
    c, reg = client_for(doc_with(lambda d: rule(d, "R-H2")["enforcers"][0].update(on_error="fail_open_and_page")))
    r = c.post("/orders/eu", json={"sku": "broken", "quantity": 1})
    assert r.status_code == 200 and any(e["action"] == "page" for e in reg.sink)


def test_detect_mode_never_blocks():
    c, reg = client_for(doc_with(lambda d: (rule(d, "R-H1").update(status="detect"),
                                            rule(d, "R-H1")["enforcers"][0].update(mode="detect"))))
    r = c.post("/orders/mars", json=ORDER)
    assert r.status_code == 200 and reg.evidence["R-H1"]["violation"] == 1


def test_halt_is_500():
    c, _ = client_for(doc_with(lambda d: rule(d, "R-H1")["enforcers"][0].update(on_violation="halt")))
    assert_problem(c.post("/orders/mars", json=ORDER), 500, "R-H1", "violation")


def test_escalate_is_202():
    c, _ = client_for(doc_with(lambda d: rule(d, "R-H1")["enforcers"][0].update(on_violation="escalate")))
    assert_problem(c.post("/orders/mars", json=ORDER), 202, "R-H1", "violation")


def test_ablated_rule_lets_the_request_through(http):
    c, reg = http
    with inv.ablated("R-H1", reg):
        assert c.post("/orders/mars", json=ORDER).status_code == 200


def test_request_validation_still_belongs_to_fastapi(http):
    c, reg = http
    r = c.post("/orders/eu", json={**ORDER, "extra": 1})
    assert r.status_code == 422 and r.headers["content-type"] == "application/json" and reg.sink == []


# --- post_effect on the response ----------------------------------------------------------------
def test_quote_ok(http):
    c, _ = http
    r = c.get("/quote/widget?quantity=4")
    assert r.status_code == 200 and r.json() == {"sku": "widget", "quantity": 4, "total": 20}


def test_quote_post_effect_rejects_bad_total(http):
    c, _ = http
    assert_problem(c.get("/quote/widget?quantity=60"), 422, "R-Q1", "violation")


def test_quote_falsifier_fails_when_ablated(http):
    c, reg = http
    with inv.ablated("R-Q1", reg):
        assert c.get("/quote/widget?quantity=60").status_code == 200


def test_response_model_is_bound_as_result(http):
    c, reg = http
    c.get("/quote/widget?quantity=4")
    ev = [e for e in reg.sink if e["rule"] == "R-Q1"]
    assert ev and ev[0]["timing"] == "post_effect" and ev[0]["outcome"] == "ok"


def test_post_effect_enforce_needs_effects_none():
    doc = doc_with(lambda d: d["targets"].clear())
    with pytest.raises(inv.LoadError, match="timing check"):
        inv.load(doc, install=False)


def test_expose_messages_and_409():
    alt = FastAPI()
    alt.include_router(app.router)
    inv_fastapi.install(alt, reject_status=409, expose_messages=True)
    c, _ = client_for(doc_with(), alt)
    body = assert_problem(c.get("/quote/widget?quantity=60"), 409, "R-Q1", "violation")
    assert body["violations"] == ["total is not quantity x price"]


def test_reject_status_must_be_4xx():
    with pytest.raises(ValueError):
        StatusMap(reject=200)


def test_guard_coerces_with_explicit_response_model():
    @inv_fastapi.guard("py:integ_fastapi_app.quote", response_model=app.Quote)
    def f(sku: str, quantity: int) -> dict[str, Any]:
        return {"sku": sku, "quantity": quantity, "total": quantity * 5}
    with inv.isolated():
        inv.load(doc_with())
        out = f("widget", 2)
    assert isinstance(out, app.Quote) and f.__inv_rules__ == ("py:integ_fastapi_app.quote",)


def test_guard_keeps_sync_and_async_and_signature():
    import inspect
    assert inspect.iscoroutinefunction(app.quote) and not inspect.iscoroutinefunction(app.create_order)
    assert list(inspect.signature(app.create_order).parameters) == ["region", "order", "dry_run"]


# --- openapi contracts --------------------------------------------------------------------------
def test_verify_routes():
    with inv.isolated() as reg:
        inv.load(doc_with())
        assert inv_fastapi.verify_routes(app.app, doc_with(), reg) == 4


def test_find_route_by_operation_id_and_method_path():
    assert [r.endpoint for r in inv_fastapi.find_route(app.app, "createOrder")] == [app.create_order]
    assert [r.endpoint for r in inv_fastapi.find_route(app.app, "get /quote/{sku}")] == [app.quote]
    assert inv_fastapi.find_route(app.app, "DELETE /quote/{sku}") == []


@pytest.mark.parametrize("mutate,match", [
    (lambda d: rule(d, "R-H1")["subject"].update(operation="nope"), "matches 0 routes"),
    (lambda d: rule(d, "R-H1")["subject"].update(operation="GET /unguarded"), "not wrapped"),
    (lambda d: rule(d, "R-Q1")["subject"].update(contract="orders_api", operation="createOrder"),
     "not wrapped"),
])
def test_verify_routes_problems(mutate, match):
    doc = doc_with(mutate)
    reg = inv.load(doc, install=False)
    with pytest.raises(inv.CoverageError, match=match):
        inv_fastapi.verify_routes(app.app, doc, reg)


def test_verify_routes_handler_must_be_the_target():
    alt = FastAPI()

    @alt.post("/other/{region}")
    @inv_fastapi.guard("py:integ_fastapi_app.create_order")
    def other(region: str, order: app.Order) -> dict[str, str]:
        return {}
    doc = doc_with(lambda d: rule(d, "R-H1")["subject"].update(operation="POST /other/{region}"))
    reg = inv.load(doc, install=False)
    with pytest.raises(inv.CoverageError, match="is not py:integ_fastapi_app.create_order"):
        inv_fastapi.verify_routes(alt, doc, reg)


def test_operations_include_router_prefixes():
    alt = FastAPI()
    alt.include_router(app.router, prefix="/v2")
    ops = {o.path: o for o in inv_fastapi.operations(alt)}
    assert ops["/v2/orders/{region}"].endpoint is app.create_order
    assert ops["/v2/orders/{region}"].operation_id == "createOrder" and "POST" in ops["/v2/orders/{region}"].methods


def test_rules_without_openapi_contract_are_ignored():
    assert inv_fastapi.verify_routes(app.app, {"rules": []}, inv.Registry()) == 0


# --- problem mapping and the plain ASGI middleware ----------------------------------------------
@pytest.mark.parametrize("exc,status,slug", [
    (Rejected("x", Outcome("violation", "R")), 422, "rejected"),
    (Rejected("x", Outcome("rule_error", "R", error="secret")), 503, "rule-error"),
    (Halted("x", Outcome("violation", "R")), 500, "halted"),
    (Halted("x", Outcome("rule_error", "R")), 503, "rule-error"),
    (Routed("x", Outcome("violation", "R"), review_id="review-9"), 202, "routed"),
    (Escalation("x", None, [{"attempt": 1}]), 202, "escalated"),
    (RolledBack("x"), 409, "rolled-back"),
    (Rejected("x", Outcome("parse_error", "R", error="bad")), 422, "rejected"),
])
def test_problem(exc, status, slug):
    got, body = problem(exc)
    assert got == status and body["type"] == f"urn:invariants:{slug}" and "secret" not in json.dumps(body)
    assert ("review_id" in body) == bool(exc.review_id)


def test_problem_names_rule_without_outcome():
    _, body = problem(Escalation("x", None, [{"attempt": 1}], rule="R-P7"))
    assert body["rule"] == "R-P7" and "outcome" not in body
    _, body = problem(RolledBack("x", rule="R-H2"))
    assert body["rule"] == "R-H2"


def test_middleware_point_rule_is_evaluated_by_the_route_guard():
    doc = doc_with(lambda d: rule(d, "R-H1")["enforcers"][0].update(point="middleware"))
    c, reg = client_for(doc)
    with inv.using(reg):
        assert inv.verify() >= 1
    assert_problem(c.post("/orders/mars", json=ORDER), 422, "R-H1", "violation")
    assert app.PLACED == []


def _raw_asgi(guarded: Callable[[int], int]) -> Any:
    async def asgi(scope: Any, receive: Any, send: Any) -> None:
        n = int(scope["query_string"].decode().partition("=")[2] or 0)
        v = guarded(n)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": str(v).encode()})
    return asgi


RAW_DOC = {"rules": [{"id": "R-A1", "status": "enforce", "bad": "args.n < 0", "falsified_by": ["t"],
                      "subject": {"target": "py:integ_fastapi_app.double"},
                      "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}]}]}


def test_middleware_on_a_plain_asgi_app():
    with inv.isolated() as reg:
        inv.load(copy.deepcopy(RAW_DOC))
    c = TestClient(InvariantMiddleware(_raw_asgi(app.double), registry=reg, reject_status=409))
    assert c.get("/?n=4").text == "8"
    assert_problem(c.get("/?n=-1"), 409, "R-A1", "violation")


def test_middleware_reraises_after_the_response_started():
    async def late(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise Rejected("late", Outcome("violation", "R"))
    with pytest.raises(Rejected):
        TestClient(InvariantMiddleware(late)).get("/")


def test_middleware_inside_fastapi_without_install():
    with inv.isolated() as reg:
        inv.load(copy.deepcopy(RAW_DOC))
    fa = FastAPI()

    @fa.get("/double")
    def double(n: int) -> int:
        return app.double(n)
    fa.add_middleware(InvariantMiddleware, registry=reg)
    assert_problem(TestClient(fa).get("/double?n=-2"), 422, "R-A1", "violation")


def test_middleware_passes_other_scopes_through():
    seen = []

    async def lifespan_app(scope: Any, receive: Any, send: Any) -> None:
        seen.append((scope["type"], inv.current()))
    reg = inv.Registry()
    import asyncio
    asyncio.run(InvariantMiddleware(lifespan_app, registry=reg)({"type": "lifespan"}, None, None))  # type: ignore[arg-type]
    assert seen == [("lifespan", reg)]
