# Cookbook

Ten short recipes. Each one was run as written, and the output shown is what it printed. Every recipe is a directory of its own: save the files, then run the commands from that directory (`pip install 'invariants-first[fastapi,schema]' pytest` covers them all: the `[fastapi]` extra brings pydantic, and httpx for `fastapi.testclient`; `[schema]` brings jsonschema, for the schema check in recipe 10's `inv validate`).

1. [Roll a new rule out in detect mode](#1-roll-a-new-rule-out-in-detect-mode)
2. [Guard a service method before it writes](#2-guard-a-service-method-before-it-writes)
3. [Compare before and after with `old_self`](#3-compare-before-and-after-with-old_self)
4. [Send a case to a person instead of failing](#4-send-a-case-to-a-person-instead-of-failing)
5. [Check an LLM reply, retry it, and re-check it at the edge](#5-check-an-llm-reply-retry-it-and-re-check-it-at-the-edge)
6. [Guard a FastAPI route](#6-guard-a-fastapi-route)
7. [Lock a repository behind its service](#7-lock-a-repository-behind-its-service)
8. [Find old rows that break a new rule](#8-find-old-rows-that-break-a-new-rule)
9. [Assert which rule fired](#9-assert-which-rule-fired)
10. [Run every check in CI](#10-run-every-check-in-ci)

## 1. Roll a new rule out in detect mode

Start a rule at `status: detect`. It logs every violation and never blocks, so you can watch it on real traffic before you enforce it. `inv status` reports that it logs a bad call and does not block it (its standing, in `--json`, is `monitored`).

`invariants.yaml`:

```yaml
spec: invariant/v0.1
targets:
  "py:shipping.ship": {effects: external}
rules:
  - id: SHIP-1
    status: detect              # observed, never blocks: the safe first step for a new rule
    statement: one shipment is at most 100 units
    subject: {target: "py:shipping.ship"}
    bad: 'args.qty > 100'
    enforcers:
      - {point: decorator, timing: pre_effect, mode: detect}
```

`shipping.py`:

```python
import invariants as inv


@inv.guard("SHIP-1")
def ship(order_id: str, qty: int) -> str:
    return f"shipped {qty} for {order_id}"
```

`run.py`:

```python
import logging
import invariants as inv
from shipping import ship

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
inv.load("invariants.yaml")
print(ship("o-1", 500))                  # a violation is logged; the call still goes ahead
```

```text
$ python run.py
WARNING invariants: invariant {'rule': 'SHIP-1', 'rev': None, 'outcome': 'violation', 'mode': 'detect', 'point': 'decorator', 'timing': 'pre_effect', 'messages': ['one shipment is at most 100 units'], 'error': '', 'attempts': None, 'action': 'proceed', 'start_ns': 1794632986723788000, 'end_ns': 1794632986723831000}
shipped 500 for o-1
```

```text
$ inv status --import shipping
SHIP-1 (detect: logs a bad call, does not block it)
  checks:  py:shipping.ship refuses a call when: one shipment is at most 100 units
  guard:   yes
  ablation proved it: not run
  next:    write a test that makes it refuse (expects inv.Rejected) and list it under tests: (then the rule blocks instead of logging)
verify: pass
```

## 2. Guard a service method before it writes

A `pre_effect` value law runs before the method, so a refused call writes nothing. A provider gives the rule data the call does not carry (here, the current balance); it is read just before the call. A `post_effect` check on an `external` effect can only detect, since the write has already happened: the loader refuses one that enforces ("post_effect enforcer on py:accounts.Bank.withdraw (effects external); rewrite as a precondition or use detect").

A check on a method's arguments is not a lock: two concurrent callers can both pass it. Keeping concurrent writes consistent (the store's constraints, its transactions) is the implementation's business.

`invariants.yaml`:

```yaml
spec: invariant/v0.1
targets:
  "py:accounts.Bank.withdraw": {effects: external}
providers:
  balance:
    type: int
    from: "py:accounts.current_balance"
    args: {ledger: self.ledger, account: args.account}
rules:
  - id: BANK-1
    status: enforce
    statement: a withdrawal is positive and never more than the balance
    subject: {target: "py:accounts.Bank.withdraw"}
    bad:
      - {when: 'args.amount <= 0', message: a withdrawal must be positive}
      - {when: 'args.amount > balance', message: a withdrawal cannot overdraw the account}
    enforcers:
      - {point: decorator, timing: pre_effect, mode: enforce, on_violation: reject}
    falsified_by: [test_accounts.py::test_overdraft_is_refused]
```

`accounts.py`:

```python
import invariants as inv


class Ledger:                                   # stands in for the repository and its database
    def __init__(self) -> None:
        self.balances: dict[str, int] = {"alice": 100}


@inv.provider("balance")
def current_balance(ledger: Ledger, account: str) -> int:
    return ledger.balances[account]


class Bank:
    def __init__(self) -> None:
        self.ledger = Ledger()

    @inv.guard("BANK-1")
    def withdraw(self, account: str, amount: int) -> None:
        self.ledger.balances[account] -= amount
```

`run.py`:

```python
import invariants as inv
from accounts import Bank

inv.load("invariants.yaml")
bank = Bank()
bank.withdraw("alice", 30)                           # ok: balance 70
try:
    bank.withdraw("alice", 500)                      # refused before the write
except inv.Rejected as e:
    print("rejected:", e)
print("balance:", bank.ledger.balances["alice"])     # 70: nothing was written
```

`test_accounts.py`:

```python
import pytest
import invariants as inv
from accounts import Bank


def test_overdraft_is_refused():
    bank = Bank()
    with pytest.raises(inv.Rejected):
        bank.withdraw("alice", 500)
    assert bank.ledger.balances["alice"] == 100
```

```text
$ python run.py
rejected: BANK-1 violation: a withdrawal cannot overdraw the account
balance: 70
```

```text
$ pytest -q --inv-rules invariants.yaml --inv-ablate
.                                                                        [100%]
----------------------------- invariants ablation ------------------------------
  BANK-1: load-bearing
      caught         test_accounts.py::test_overdraft_is_refused
1 passed in 0.06s
```

## 3. Compare before and after with `old_self`

A transition rule reads `old_self.<path>`. Only those paths are copied before the call, and `inv.check_snapshot` proves at test time that they are plain data. The check runs after the effect, so on an `external` target it detects and reports; to refuse the edit, write the rule over the arguments as a `pre_effect` check (`args.cents * 2 > self.prices[args.sku] * 3`).

`invariants.yaml`:

```yaml
spec: invariant/v0.1
targets:
  "py:prices.Catalog.set_price": {effects: external}
rules:
  - id: PRICE-2
    status: detect               # a post-effect check on an external effect can only detect
    statement: one edit never raises a price by more than 50%
    subject: {target: "py:prices.Catalog.set_price"}
    scope: transition
    bad: 'self.prices[args.sku] * 2 > old_self.prices[args.sku] * 3'
    enforcers:
      - {point: decorator, timing: post_effect, mode: detect}
```

`prices.py`:

```python
import invariants as inv


class Catalog:
    def __init__(self) -> None:
        self.prices: dict[str, int] = {"tea": 400}

    @inv.guard("PRICE-2")
    def set_price(self, sku: str, cents: int) -> None:
        self.prices[sku] = cents
```

`run.py`:

```python
import invariants as inv
from prices import Catalog

inv.load("invariants.yaml")
c = Catalog()
print("snapshot paths:", inv.check_snapshot("PRICE-2", c))   # only old_self.prices is copied
with inv.capture() as events:
    c.set_price("tea", 550)                                  # +37%: ok
    c.set_price("tea", 900)                                  # +64%: reported, not blocked
print([(e["rule"], e["outcome"]) for e in events])
print(c.prices)
```

`test_prices.py`:

```python
import invariants as inv
from prices import Catalog


def test_snapshot_paths_are_plain_data():
    inv.check_snapshot("PRICE-2", Catalog())         # raises LoadError if old_self.prices is not plain data


def test_a_big_jump_is_reported(inv_events):
    Catalog().set_price("tea", 900)
    assert [(e["rule"], e["outcome"]) for e in inv_events] == [("PRICE-2", "violation")]
```

```text
$ python run.py
snapshot paths: ['PRICE-2:old_self.prices']
[('PRICE-2', 'ok'), ('PRICE-2', 'violation')]
{'tea': 900}
```

```text
$ pytest -q --inv-rules invariants.yaml
..                                                                       [100%]
2 passed in 0.05s
```

## 4. Send a case to a person instead of failing

`on_violation: route` opens a review and raises `inv.Routed`; the call does not proceed. Give it a durable store. If no review can be opened, `inv.ReviewNotOpened` is raised instead, and the call still does not proceed.

`invariants.yaml`:

```yaml
spec: invariant/v0.1
targets:
  "py:payouts.pay_vendor": {effects: external}
rules:
  - id: PAYOUT-1
    status: enforce
    statement: a payout above 10,000 needs a person to look at it
    subject: {target: "py:payouts.pay_vendor"}
    bad: 'args.cents > 1000000'
    enforcers:
      - {point: decorator, timing: pre_effect, mode: enforce, on_violation: route}
    falsified_by: [test_payouts.py::test_large_payout_is_routed]
```

`payouts.py`:

```python
import invariants as inv


@inv.guard("PAYOUT-1")
def pay_vendor(vendor: str, cents: int) -> str:
    return f"paid {vendor} {cents}"
```

`run.py`:

```python
import json
import invariants as inv
from payouts import pay_vendor

inv.load("invariants.yaml")
inv.configure(review_store=inv.JsonlReviews("reviews.jsonl"))   # durable: one JSON line per hand-off
try:
    pay_vendor("acme", 2_500_000)
except inv.Routed as e:
    print("routed:", e.rule, "review", e.review_id)
except inv.ReviewNotOpened as e:                                   # the store failed: still not paid
    print("could not open a review:", e)
with open("reviews.jsonl") as f:
    print(sorted(json.loads(f.readline())))
```

`test_payouts.py`:

```python
import pytest
import invariants as inv
from payouts import pay_vendor


def test_large_payout_is_routed():
    with pytest.raises(inv.Routed):
        pay_vendor("acme", 2_500_000)
```

```text
$ python run.py
routed: PAYOUT-1 review jsonl-26cbf39cda3741668857ffd2ff9a51cc
['action', 'attempts', 'bound', 'event', 'id', 'outcome', 'reason', 'rule', 'time']
```

```text
$ pytest -q --inv-rules invariants.yaml --inv-ablate
.                                                                        [100%]
----------------------------- invariants ablation ------------------------------
  PAYOUT-1: load-bearing
      caught         test_payouts.py::test_large_payout_is_routed
1 passed in 0.05s
```

## 5. Check an LLM reply, retry it, and re-check it at the edge

`@inv.model_call` parses the raw reply into a closed schema, retries a malformed reply with the parser error as feedback, resamples a reply that breaks `bad`, and returns a `Checked` value. `@inv.edge` re-validates and re-checks whatever it receives, so a forged or mutated value cannot reach the effect.

`invariants.yaml`:

```yaml
spec: invariant/v0.1
targets:
  "py:triage.classify": {effects: none}
  "py:triage.issue_refund": {effects: external}
rules:
  - id: LLM-1
    status: enforce
    statement: the model never proposes a refund larger than the order
    subject: {target: "py:triage.classify"}
    bad: 'reply.action == "REFUND" && reply.amount_cents > args.order_cents'
    model_policy:
      schema: Decision
      format_retries: 2                 # a malformed reply is retried with the parser error as feedback
      semantic_resamples: 1             # a reply that breaks `bad` is resampled once
      feedback: {format: parser_error, semantic: none}
      abstain: {field: action, value: CANNOT_DECIDE}
    enforcers:
      - {point: model_call, timing: post_effect, mode: enforce, on_violation: reject}
      - {point: edge, target: "py:triage.issue_refund", timing: pre_effect, mode: enforce, on_violation: reject}
    falsified_by: [test_triage.py::test_oversized_refund_is_refused]
```

`triage.py`:

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict

import invariants as inv


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")          # closed schema: required
    action: Literal["REFUND", "REPLACE", "CANNOT_DECIDE"]
    amount_cents: int


class CheckedDecision(inv.Checked): ...


REPLIES: list[str] = []                                 # stand-in for an LLM client


def call_model(ticket: str, feedback: str | None) -> str:
    return REPLIES.pop(0)


@inv.model_call("LLM-1", schema=Decision, returns=CheckedDecision)
def classify(ticket: str, order_cents: int, inv_feedback: str | None = None) -> str:
    return call_model(ticket, inv_feedback)             # raw text; the library parses and checks it


@inv.edge("LLM-1")                                      # re-validates and re-checks what it receives
def issue_refund(decision: CheckedDecision, order_cents: int) -> str:
    return f"refunded {decision.value.amount_cents}"
```

`run.py`:

```python
import invariants as inv
import invariants.testing
import triage

inv.load("invariants.yaml")
triage.REPLIES[:] = ['{"action": "REFUND"}',                              # missing field: retried
                     '{"action": "REFUND", "amount_cents": 99999}',       # breaks LLM-1: resampled
                     '{"action": "REFUND", "amount_cents": 1500}']        # ok
d = triage.classify("box arrived crushed", order_cents=2000)
print(d.verdict, d.value.amount_cents, "after", d.attempts, "attempts")
print(triage.issue_refund(d, order_cents=2000))
bad = inv.parse('{"action": "REFUND", "amount_cents": 99999}', triage.Decision)
forged = inv.testing.make_checked(triage.CheckedDecision, bad)    # verdict "ok", but never checked
try:
    triage.issue_refund(forged, order_cents=2000)       # a value that never passed the model-call check
except inv.Rejected as e:
    print("edge:", e)
```

`test_triage.py`:

```python
import pytest
import invariants as inv
import triage


def test_oversized_refund_is_refused():
    triage.REPLIES[:] = ['{"action": "REFUND", "amount_cents": 99999}'] * 2
    with pytest.raises(inv.Rejected):
        triage.classify("box arrived crushed", order_cents=2000)
```

```text
$ python run.py
ok 1500 after 3 attempts
refunded 1500
edge: LLM-1 violation: the model never proposes a refund larger than the order
```

```text
$ pytest -q --inv-rules invariants.yaml --inv-ablate
.                                                                        [100%]
----------------------------- invariants ablation ------------------------------
  LLM-1: load-bearing
      caught         test_triage.py::test_oversized_refund_is_refused
1 passed in 0.08s
```

## 6. Guard a FastAPI route

Rules bind to the parameters as FastAPI parsed them. `install(app)` turns guard exceptions into `application/problem+json`, and the body never echoes the request's values. A `Response` the handler returns itself (`return JSONResponse({"detail": "no such region"}, status_code=404)`) is sent as it is, never validated into the response model.

`invariants.yaml`:

```yaml
spec: invariant/v0.1
targets:
  "py:shop.create_order": {effects: external}
rules:
  - id: ORD-1
    status: enforce
    statement: an order has 1 to 50 items
    subject: {target: "py:shop.create_order"}
    bad: 'args.order.quantity < 1 || args.order.quantity > 50'
    enforcers:
      - {point: middleware, timing: pre_effect, mode: enforce, on_violation: reject}
    falsified_by: [test_shop.py::test_too_many_items_is_422]
```

`shop.py`:

```python
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from invariants.integrations import fastapi as inv_fastapi


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str
    quantity: int


app = FastAPI()


@app.post("/orders/{region}", operation_id="createOrder")
@inv_fastapi.guard("py:shop.create_order")            # under the route decorator
def create_order(region: str, order: Order) -> dict:
    return {"region": region, "sku": order.sku, "quantity": order.quantity}


inv_fastapi.install(app)                               # guard exceptions become problem+json
```

`run.py`:

```python
from fastapi.testclient import TestClient
import invariants as inv
from shop import app

inv.load("invariants.yaml")
client = TestClient(app)
print(client.post("/orders/eu", json={"sku": "tea", "quantity": 3}).status_code)
r = client.post("/orders/eu", json={"sku": "tea", "quantity": 500})
print(r.status_code, r.headers["content-type"], r.json())
```

`test_shop.py`:

```python
from fastapi.testclient import TestClient
from shop import app


def test_too_many_items_is_422():
    r = TestClient(app).post("/orders/eu", json={"sku": "tea", "quantity": 500})
    assert r.status_code == 422
```

```text
$ python run.py
200
422 application/problem+json {'type': 'urn:invariants:rejected', 'title': 'The request violates an invariant', 'status': 422, 'rule': 'ORD-1', 'outcome': 'violation'}
```

```text
$ pytest -q --inv-rules invariants.yaml --inv-ablate
.                                                                        [100%]
----------------------------- invariants ablation ------------------------------
  ORD-1: load-bearing
      caught         test_shop.py::test_too_many_items_is_422
1 passed in 0.14s
```

## 7. Lock a repository behind its service

A door rule is a path law: only the service method may call the repository write. Nothing runs in production; `inv scan` (inside `inv check`) reads the source. `allow` freezes the back doors that exist today. An allowed function is not a door: a path to the sink passes only when every function on it is in `allow`, so a new caller of `fix_counts` is a bypass too. When `fix_counts` is fixed, its entry fails the scan (`missing/allow-unused`) until it is deleted.

`invariants.yaml`:

```yaml
spec: invariant/v0.1
rules:
  - id: STOCK-DOOR
    door: py:stock_service.StockService.receive     # the upper API: it checks the quantity
    sink: py:stock_repo.StockRepo.save              # the lower API it protects
    allow: [py:admin.fix_counts]                    # the one back door today, frozen
```

`stock_repo.py`:

```python
class StockRepo:
    def __init__(self) -> None:
        self.on_hand: dict[str, int] = {}

    def save(self, sku: str, on_hand: int) -> None:
        self.on_hand[sku] = on_hand
```

`stock_service.py`:

```python
from stock_repo import StockRepo


class StockService:
    def __init__(self, repo: StockRepo) -> None:
        self.repo = repo

    def receive(self, sku: str, qty: int) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        self.repo.save(sku, self.repo.on_hand.get(sku, 0) + qty)
```

`admin.py`:

```python
from stock_repo import StockRepo


def fix_counts(repo: StockRepo, sku: str, on_hand: int) -> None:
    repo.save(sku, on_hand)                          # skips the service: a known back door
```

```text
$ inv scan
STOCK-DOOR: pass (no findings)
scanned 3 file(s), 5 function(s)
```

Now add two files: `restock.py`, which calls the repository directly, and `nightly.py`, which calls the allowed back door.

`restock.py`:

```python
from stock_repo import StockRepo


def restock(repo: StockRepo, sku: str) -> None:
    repo.save(sku, 100)                              # new code that skips the door
```

`nightly.py`:

```python
from admin import fix_counts
from stock_repo import StockRepo


def reset_all(repo: StockRepo) -> None:
    fix_counts(repo, "tea", 0)                       # a new caller of the allowed back door
```

```text
$ inv scan
bypass     STOCK-DOOR admin.py:5:5  nightly.reset_all -> admin.fix_counts -> stock_repo.StockRepo.save reaches the sink without passing stock_service.StockService.receive
bypass     STOCK-DOOR restock.py:5:5  restock.restock -> stock_repo.StockRepo.save reaches the sink without passing stock_service.StockService.receive
STOCK-DOOR: fail (2 bypass)
scanned 5 file(s), 7 function(s)
```

The exit code is 1. If `reset_all` is a reviewed back door, add it to `allow`; the lock reports that as a weakening, so a person accepts it.

## 8. Find old rows that break a new rule

Data written before a rule existed is not checked by its guard. A `monitor` enforcer runs the same `bad` over stored rows, reports, and never blocks.

`invariants.yaml`:

```yaml
spec: invariant/v0.1
targets:
  "py:subs.create_subscription": {effects: external}
rules:
  - id: SUB-1
    status: enforce
    statement: a subscription never ends before it starts
    subject: {target: "py:subs.create_subscription"}
    bad: 'args.end_day < args.start_day'
    enforcers:
      - {point: decorator, timing: pre_effect, mode: enforce, on_violation: reject}
      - {point: monitor, timing: post_effect, mode: detect}       # the same rule over stored rows
    falsified_by: [test_subs.py::test_backwards_subscription_is_rejected]
```

`subs.py`:

```python
import invariants as inv

ROWS = [{"id": "s1", "start_day": 10, "end_day": 40},
        {"id": "s2", "start_day": 30, "end_day": 5}]           # written before the rule existed


@inv.guard("SUB-1")
def create_subscription(sub_id: str, start_day: int, end_day: int) -> None:
    ROWS.append({"id": sub_id, "start_day": start_day, "end_day": end_day})


def nightly() -> list[str]:
    rows = ((r["id"], {"args": r}) for r in ROWS)                # (key, base environment) per row
    return [key for key, out in inv.monitor("SUB-1", rows) if out.kind != "ok"]
```

`run.py`:

```python
import invariants as inv
import subs

inv.load("invariants.yaml")
print("bad rows:", subs.nightly())                             # reports; never blocks
```

`test_subs.py`:

```python
import pytest
import invariants as inv
from subs import create_subscription


def test_backwards_subscription_is_rejected():
    with pytest.raises(inv.Rejected):
        create_subscription("s3", start_day=9, end_day=1)
```

```text
$ python run.py
bad rows: ['s2']
```

```text
$ pytest -q --inv-rules invariants.yaml --inv-ablate
.                                                                        [100%]
----------------------------- invariants ablation ------------------------------
  SUB-1: load-bearing
      caught         test_subs.py::test_backwards_subscription_is_rejected
1 passed in 0.05s
```

## 9. Assert which rule fired

The plugin's fixtures let a test assert on outcomes rather than on exception text. This recipe uses `invariants.yaml` and `payments.py` from the README quickstart, with `falsified_by` pointing at the first test below.

`test_events.py`:

```python
import pytest
import invariants as inv
from payments import refund


def test_the_right_rule_fired(inv_events):
    with pytest.raises(inv.Rejected) as exc:
        refund("ch_1", charged=5000, refunded=1500, amount=5000)
    assert exc.value.rule == "PAY-1" and exc.value.outcome.kind == "violation"
    assert [(e["rule"], e["outcome"]) for e in inv_events] == [("PAY-1", "violation")]


def test_without_the_rule_the_bad_refund_goes_through(inv_ablated):
    with inv_ablated("PAY-1"):                       # what --inv-ablate does, by hand
        refund("ch_1", charged=5000, refunded=1500, amount=5000)
```

```text
$ pytest -q --inv-rules invariants.yaml --inv-ablate --inv-report
..                                                                       [100%]
----------------------------- invariants ablation ------------------------------
  PAY-1: load-bearing
      caught         test_events.py::test_the_right_rule_fired
------------------------------ invariants report -------------------------------
  rule       status      reach     ok violation rule_error parse_error abstain
  PAY-1      enforce         1      0         1          0           0       0
2 passed in 0.06s
```

## 10. Run every check in CI

One script, run from the quickstart project (after its `inv lock --lock invariants.lock`) with `bash ci.sh` (or `chmod +x ci.sh` once, then `./ci.sh`). Every check exits non-zero on a failure (`inv status` only reports). In GitHub Actions, upload `scan.sarif` with `github/codeql-action/upload-sarif` to see bypasses inline.

`ci.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
inv validate invariants.yaml                 # schema and every load-time check
inv lock --check --lock invariants.lock      # the lock matches: no silent weakening
inv verify --import payments                 # every target carries its guard
inv scan --format sarif --output scan.sarif  # no path reaches a sink around its door
python -m pytest -q --inv-rules invariants.yaml --inv-verify --inv-ablate --inv-report-json build/inv-report.json
inv status --import payments --ablation build/inv-report.json   # standing per rule
```

```text
$ bash ci.sh
ok: invariants.yaml: 1 rule(s) PAY-1
fresh: invariants.lock
verify: pass (1 targeted rule(s) guarded; checked the guards in 1 imported module(s), each names a rule; a module nothing imports is not checked: add it with --import MODULE)
..                                                                       [100%]
---------------- invariants verify: 1 targeted rule(s) guarded -----------------
----------------------------- invariants ablation ------------------------------
  PAY-1: load-bearing
      caught         test_payments.py::test_over_refund_is_rejected
invariants report written to build/inv-report.json
2 passed in 0.08s
PAY-1 (enforce: blocks a bad call; held)
  checks:  py:payments.refund refuses a call when: refund must be positive; refund exceeds what is left of the charge; and every call to py:payments.send_to_gateway goes through py:payments.refund
  guard:   yes
  scan:    pass, no path skips it
  ablation proved it: yes
verify: pass
```

`inv status` reports `held` only with the ablation report: without `--ablation` the same rule is
`held (unproven: no --ablation)`. The ablation runs before `inv status` so that it can read the report.
