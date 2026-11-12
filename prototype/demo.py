"""Runs every check in the prototype and prints short, numbered lines. Exit 1 on any failure.

    python3 demo.py                 # all checks pass
    INV_ABLATE=R-1 python3 demo.py  # mutation sanity: R-1's checks must now FAIL
"""
import asyncio, copy, json, pathlib, random, sqlite3, sys
import invariants as inv

HERE = pathlib.Path(__file__).resolve().parent
RULES = json.loads((HERE / "invariants.json").read_text())
_n, _failed = 0, []

def check(label, ok, detail=""):
    global _n
    _n += 1
    print(f"{_n:2}. {'ok  ' if ok else 'FAIL'} {label}" + (f"  [{detail}]" if detail else ""))
    if not ok: _failed.append(_n)

def section(title): print(f"--- {title}")

def raises(exc, fn, *a, **kw):
    try: fn(*a, **kw)
    except exc as e: return e
    return None

def variant(mutate):
    """A deep copy of the rule file with one change, for load negatives and `inv.using` runs."""
    doc = copy.deepcopy(RULES); mutate(doc); return doc

def rule(doc, rid): return next(r for r in doc["rules"] if r["id"] == rid)
def load_variant(mutate): return inv.load(variant(mutate), install=False, base_dir=HERE)
def load_error(mutate): return raises(inv.LoadError, load_variant, mutate)

# --- load, coverage ---------------------------------------------------------------------------
section("load and verify (D2, D3, D8)")
print(f"CEL backend: {inv.CEL_BACKEND}")
reg = inv.load(HERE / "invariants.json")
import jobs, pool, triage                                 # targets were imported by load already
check("rule file loads and validates", len(reg.rules) == 6 and len(reg.targets) == 6 and len(reg.providers) == 4,
      f"rules {', '.join(reg.rules)}; providers {', '.join(reg.providers)}")
check("verify: every rule target carries its guard", inv.verify is inv.verify_coverage and inv.verify() == 6)

e = load_error(lambda d: rule(d, "R-1").update(severity="high"))
check("load rejects an unknown field", e is not None, str(e))
e = load_error(lambda d: rule(d, "R-1")["subject"].update(target="py:jobs.split_wrok"))
check("load rejects a target that does not exist", e is not None, str(e))
e = load_error(lambda d: rule(d, "P-2")["enforcers"][0].update(timing="post_effect"))
check("load refuses status enforce when a post_effect enforcer sits on an external target (2.2, D2)",
      e is not None and "timing check fails" in str(e), str(e)[:110])
e = load_error(lambda d: rule(d, "R-1").update(bad="inv.sum(result) != args.total"))
check("load rejects args.<name> that is not in the signature (rename)", e is not None, str(e))
e = load_error(lambda d: rule(d, "R-21").update(bad="!has(approval)"))
check("load rejects bare has(approval) (CEL needs has(a.b))", e is not None, str(e))
def tamper(d):
    enf = rule(d, "R-20")["enforcers"][1]; enf["artifact"] = enf["artifact"][:-4] + "beef"
e = load_error(tamper)
check("load fails when the storage artifact hash differs from the record", e is not None, str(e)[:80])
e = raises(inv.CoverageError, inv.verify, load_variant(lambda d: d["rules"].append(
    {"id": "R-23x", "status": "detect", "statement": "set_size only via the door", "subject": {"target": "py:pool.set_size"},
     "bad": "false", "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}]})))
check("verify fails for a rule whose target has no guard", e is not None, str(e))
def remediate_unguarded(cause, job_id): return f"remediate {job_id}"   # no @inv.edge
e = raises(inv.CoverageError, inv.verify, load_variant(lambda d: rule(d, "R-7")["enforcers"][1].update(target="py:__main__.remediate_unguarded")))
check("verify fails when an edge enforcer's target is not an @inv.edge (D8)", e is not None, str(e)[:90])

# D2: status and mode
e = load_error(lambda d: rule(d, "R-21").pop("falsified_by"))
check("load refuses status enforce without falsified_by (D2)", e is not None and "highest allowed: detect" in str(e), str(e))
table = {(m, s): inv.effective_mode(m, s) for m in ("off", "detect", "enforce")
         for s in ("candidate", "contested", "detect", "enforce", "retired")}
check("effective mode = min(enforcer.mode, cap(status)) (D2)",
      table[("enforce", "candidate")] == table[("enforce", "contested")] == table[("enforce", "detect")] == "detect"
      and table[("enforce", "enforce")] == "enforce" and table[("enforce", "retired")] == "off"
      and table[("detect", "enforce")] == "detect" and table[("off", "enforce")] == "off")

# D3: effects belong to the target
e = load_error(lambda d: rule(d, "R-1").update(effects="none"))
check("load rejects effects on a rule: it belongs to the target (D3)", e is not None, str(e))
e = load_error(lambda d: d["targets"]["py:pool.PoolService.resize_door"].pop("transaction"))
check("load rejects a transactional target that names no transaction boundary (D3)", e is not None, str(e))

# D1: providers are checked at load
e = load_error(lambda d: d["providers"]["approval"].update(type="py:pool.ResizePoolRequest | null"))
check("load rejects a provider whose return type differs from its declaration (D1)", e is not None, str(e)[:110])
e = load_error(lambda d: d["providers"]["approval"].update(**{"from": "py:pool.pool_size"}))
check("load rejects a provider function registered under another name (D1)", e is not None, str(e)[:110])
e = load_error(lambda d: d["providers"]["pool_leased"].update(consistency="same_txn"))
check("load rejects a same_txn provider read by a rule whose target is not effects: transactional (D1, D3)",
      e is not None and "not effects: transactional" in str(e), str(e)[:110])

# --- R-1: property test, mutant, ablation -----------------------------------------------------
section("R-1: pure target, post-effect guard")
def r1_property(seed=20261109, cases=500):
    rnd, first_bad = random.Random(seed), None
    for _ in range(cases):
        caps = [rnd.randint(0, 50) for _ in range(rnd.randint(1, 8))]
        total = rnd.randint(0, sum(caps))
        try: jobs.split_work(total, caps)
        except inv.Rejected as err: first_bad = first_bad or (total, caps, str(err))
    return first_bad

runs0 = reg.evidence["R-1"]["runs"]
check("R-1 property test: 500 seeded inputs, guard never fires", r1_property() is None)
check("R-1 reach counter counted every evaluation", reg.evidence["R-1"]["runs"] - runs0 == 500,
      f"runs={reg.evidence['R-1']['runs'] - runs0}")
good = jobs._allocate
jobs._allocate = jobs._allocate_rounding_bug
caught = r1_property()                                   # falsifier demo.py::r1_mutant
check("R-1 guard catches the seeded rounding-bug mutant", caught is not None, caught and caught[2][:70])
with inv.ablated("R-1"):
    check("ablation: with R-1 disabled the falsifier fails (guard is load-bearing)", r1_property() is None)
jobs._allocate = lambda total, caps: None                # a result the rule cannot read
viol0, err0 = reg.evidence["R-1"]["violation"], reg.evidence["R-1"]["rule_error"]
e = raises(inv.Rejected, jobs.split_work, 3, [3])
check("R-1 on a None result: rule_error, fail_closed rejects, not counted as a violation",
      e is not None and e.outcome.kind == "rule_error" and reg.evidence["R-1"]["violation"] == viol0
      and reg.evidence["R-1"]["rule_error"] == err0 + 1, e and e.outcome.error[:60])
jobs._allocate = good

zip_r1 = inv.load({"rules": [{"id": "R-1g", "status": "candidate", "subject": {"target": "py:jobs.split_work"},
    "bad": "inv.sum(result) != args.total_jobs || inv.zip(result, args.capacities).exists(p, p[0] < 0 || p[0] > p[1])"}]},
    install=False).rules["R-1g"]
base = {"args": {"total_jobs": 10, "capacities": [5, 5]}, "result": [5, 4, 1]}
out_zip, out_ours = inv.evaluate(zip_r1, base), inv.evaluate(reg.rules["R-1"], base)
check("3 shares for 2 workers: zip-only R-1 gives rule_error, clause list gives violation",
      out_zip.kind == "rule_error" and out_ours.kind == "violation", f"{out_zip.error}")

@inv.guard("R-1")
async def split_work_async(total_jobs: int, capacities: list[int]) -> list[int]:
    return [-1, total_jobs + 1]
e = raises(inv.Rejected, asyncio.run, split_work_async(10, [5, 5]))
check("async guard: post-effect R-1 rejects a negative share", e is not None, e and e.outcome.messages)

# --- outcomes (D7) ----------------------------------------------------------------------------
section("outcomes (D7)")
jobs.restart_job(None)                                   # size(null): rule_error -> fail_open_and_page
ev = reg.sink[-1]
check("P-2 rule_error with fail_open_and_page: call proceeds, event paged",
      jobs.RESTARTED == [None] and ev["outcome"] == "rule_error" and ev["action"] == "page", ev["error"])
e = raises(inv.Rejected, jobs.restart_job, "")           # falsifier demo.py::p2_empty_id
check("P-2 violation still rejects before the effect", e is not None and jobs.RESTARTED == [None])
clauses = inv.load({"rules": [{"id": "C-1", "status": "candidate", "bad": [
    {"when": "args.x > 1", "message": "x > 1"}, {"when": "args.y.z", "message": "reads a missing field"},
    {"when": "args.x > 2", "message": "x > 2"}]}]}, install=False).rules["C-1"]
o_v, o_e = inv.evaluate(clauses, {"args": {"x": 5, "y": {}}}), inv.evaluate(clauses, {"args": {"x": 0, "y": {}}})
check("clause list: any true -> violation listing every fired clause; else any error -> rule_error (D7)",
      o_v.kind == "violation" and o_v.messages == ["x > 1", "x > 2"] and o_e.kind == "rule_error",
      f"{o_v.messages}; {o_e.kind}: {o_e.error}")

# --- R-21, P-1: providers, transactions, old_self ---------------------------------------------
section("R-21 and P-1: providers (D1), transaction boundary (D3), old_self (D6), route (D7)")
conn = pool.connect()
conn.execute("INSERT INTO pools VALUES ('p1', 100), ('p2', 100)")
svc = pool.PoolService(conn)
def resize(pid, new):
    with svc.txn(): svc.resize_door(pool.ResizePoolRequest(pid, new))
e = raises(inv.Rejected, resize, "p1", 150)              # falsifier demo.py::r21_without_approval
check("R-21 rejects resize with no approval row (approval read by provider, same_txn); nothing written",
      e is not None and e.outcome.kind == "violation" and pool.size(conn, "p1") == 100)
pool.approve(conn, "p1", 150, "PENDING")
e1 = raises(inv.Rejected, resize, "p1", 150)
pool.approve(conn, "p1", 999)
e2 = raises(inv.Rejected, resize, "p1", 150)
check("R-21 rejects a PENDING approval and an approval for another size; nothing written",
      e1 is not None and e2 is not None and pool.size(conn, "p1") == 100)
pool.approve(conn, "p1", 150)
resize("p1", 150)
check("R-21 allows resize with a matching APPROVED approval, inside PoolService.txn", pool.size(conn, "p1") == 150)

pool.approve(conn, "p2", 120)
e = raises(inv.InvariantError, svc.resize_door, pool.ResizePoolRequest("p2", 120))
check("resize outside the declared transaction: same_txn provider cannot be confirmed -> rule_error, nothing written (D1, D3)",
      isinstance(e, inv.Rejected) and e.outcome.kind == "rule_error" and pool.size(conn, "p2") == 100, e and e.outcome.error[:80])
real_fn = reg.providers["approval"].fn
reg.providers["approval"].fn = lambda conn, pool_id: (_ for _ in ()).throw(RuntimeError("approvals store down"))
e1 = raises(inv.Rejected, resize, "p2", 120)
reg.providers["approval"].fn = lambda conn, pool_id: {"state": "APPROVED"}
e2 = raises(inv.Rejected, resize, "p2", 120)
reg.providers["approval"].fn = real_fn
check("provider that raises, or returns the wrong type: rule_error (never violation), fail_closed rejects (D1)",
      e1 and e2 and e1.outcome.kind == e2.outcome.kind == "rule_error" and pool.size(conn, "p2") == 100,
      f"{e1 and e1.outcome.error}; {e2 and e2.outcome.error}")

ECHO_CALLS = []
@inv.provider("echo")
def echo(x: int) -> int:
    ECHO_CALLS.append(x); return x
@inv.guard("py:__main__.two_rules")
def two_rules(a: int, b: int) -> None: pass
two = inv.load({"targets": {"py:__main__.two_rules": {"effects": "none"}},
                "providers": {"echo": {"type": "int", "from": "py:__main__.echo", "args": {"x": "v"}, "consistency": "stale_ok"}},
                "rules": [{"id": rid, "status": "enforce", "subject": {"target": "py:__main__.two_rules"}, "bind": {"v": path},
                           "bad": "echo != v", "falsified_by": ["demo.py::none"],
                           "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}]}
                          for rid, path in (("C-a", "args.a"), ("C-b", "args.b"))]}, install=False)
with inv.using(two):
    try: two_rules(1, 2); calls_12 = list(ECHO_CALLS); two_rules(3, 3)
    except inv.Rejected: calls_12 = None                 # a shared, stale value would make C-b fire
check("two rules bind a provider's args differently on one target: one value per (provider, resolved args) (D1)",
      calls_12 == [1, 2] and ECHO_CALLS == [1, 2, 3], f"provider calls {ECHO_CALLS}")

check("P-1 snapshot inferred from bad: only old_self.sizes, plain data on a real PoolService (D6)",
      reg.rules["P-1"].snapshot == [("sizes",)] and inv.check_snapshot("P-1", svc) == ["P-1:old_self.sizes"])
p1_conn = load_variant(lambda d: rule(d, "P-1").update(bad="old_self.conn != null"))
e1 = raises(inv.LoadError, inv.check_snapshot, "P-1", svc, p1_conn)
e2 = load_error(lambda d: rule(d, "P-1").update(bad="old_self == null"))
check("old_self.conn (a connection) fails the snapshot test with a real instance; bare old_self fails to load (D6)",
      e1 is not None and e2 is not None, f"{e1}; {e2}")

pool.approve(conn, "p1", 80)
rev0 = len(reg.reviews)
e = raises(inv.Routed, resize, "p1", 80)                 # falsifier demo.py::p1_shrink
check("P-1 shrink: post-effect guard inside the txn routes (Routed, review id) and rolls back (D3, D7)",
      e is not None and e.review_id == f"review-{rev0 + 1}" and reg.reviews[-1]["id"] == e.review_id
      and pool.size(conn, "p1") == 150, e and str(e)[:90])
def swallow():
    with svc.txn():
        try: svc.resize_door(pool.ResizePoolRequest("p1", 80))
        except inv.Routed: pass                          # caller ignores the guard ...
e = raises(inv.RolledBack, swallow)
check("a caller that swallows the guard's exception still gets a rollback (RolledBack) (D3)",
      e is not None and pool.size(conn, "p1") == 150)
with inv.using(load_variant(lambda d: rule(d, "P-1").update(status="detect"))) as det:
    resize("p1", 80)
    ev = next((x for x in reversed(det.sink) if x["rule"] == "P-1"), {})
check("P-1 with status detect: the shrink is reported, the call completes and commits (D2, D7)",
      ev.get("outcome") == "violation" and ev.get("mode") == "detect" and ev.get("action") == "proceed" and pool.size(conn, "p1") == 80)

# --- R-7: model call --------------------------------------------------------------------------
section("R-7: model call (D4) and edge (D9)")
GOOD = '{"cause": "OUT_OF_MEMORY", "message": "killed at 8 GiB"}'
def model(*replies, job="J-oom"):
    triage.client.script(*replies)
    try: return triage.propose_cause("stack trace ...", job), None
    except inv.InvariantError as err: return None, err

c, _ = model(GOOD)
check("R-7 good reply -> CheckedCause, verdict ok, attempts=1",
      isinstance(c, triage.CheckedCause) and c.verdict == "ok" and c.attempts == 1, repr(c))
c, _ = model('{"cause": "OUT_OF_MEMORY", "message": "x", "job_id": "J9"}', GOOD)
check("R-7 extra field: parse_error, retried WITH the parser error (inv_feedback), attempts=2",
      c and c.attempts == 2 and "extra fields" in (triage.client.calls[1]["feedback"] or ""), triage.client.calls[1]["feedback"])
c, _ = model('{"cause": "COSMIC_RAY", "message": "x"}', GOOD)
check("R-7 made-up cause: the schema enum rejects it as parse_error, fed back, attempts=2",
      c and c.attempts == 2 and "COSMIC_RAY" in (triage.client.calls[1]["feedback"] or ""))
c, esc = model('{"cause": "TIMEOUT", "message": "x"}', GOOD)          # falsifier demo.py::r7_held_data_mismatch
check("R-7 held-data mismatch: compares_to held_data -> no resample, on_violation escalates at once",
      isinstance(esc, inv.Escalation) and len(esc.attempts) == 1 and len(triage.client.calls) == 1
      and len(esc.outcome.messages) == 2, esc and esc.outcome.messages)
with inv.using(load_variant(lambda d: (rule(d, "R-7").pop("compares_to"),
                                       rule(d, "R-7")["model_policy"].update(semantic_resamples=1)))):
    c, _ = model('{"cause": "TIMEOUT", "message": "x"}', GOOD)
check("R-7 without compares_to: one fresh resample WITHOUT feedback, attempts=2",
      c and c.attempts == 2 and triage.client.calls[1]["feedback"] is None)
runs7 = reg.evidence["R-7"]["runs"]
c, esc = model('{"cause": "CANNOT_COMPLY", "message": ""}')
check("R-7 abstain value: straight to escalation, bad not evaluated, no resample",
      isinstance(esc, inv.Escalation) and len(esc.attempts) == 1 and reg.evidence["R-7"]["runs"] == runs7, esc and str(esc))
c, esc = model("not json", '{"cause": 1, "message": "x"}', "{}")
check("R-7 three parse errors: 2 format retries, then on_violation (escalate)",
      isinstance(esc, inv.Escalation) and len(esc.attempts) == 3, esc and esc.attempts[-1]["error"])
c, esc = model('{"cause": "TIMEOUT", "message": "x"}', GOOD, job="J-unknown")
check("R-7 rule_error (job provider raises): never resampled; on_error fail_closed -> on_violation",
      isinstance(esc, inv.Escalation) and esc.outcome.kind == "rule_error" and len(triage.client.calls) == 1,
      esc and esc.outcome.error)

e = load_error(lambda d: rule(d, "R-7")["model_policy"]["abstain"].update(value="UNKNOWN"))
check("load rejects an abstain value outside the closed enum (D4)", e is not None, str(e)[:100])
e = load_error(lambda d: rule(d, "R-7")["model_policy"].update(on_exhaust="escalate"))
check("load rejects on_exhaust in model_policy: on_violation is the one outcome field (D4)", e is not None, str(e)[:100])
e = load_error(lambda d: rule(d, "R-7")["model_policy"].update(semantic_resamples=1))
check("load refuses semantic_resamples 1 on a compares_to: held_data rule (D4)", e is not None, str(e)[:100])

@inv.model_call("R-7", schema=triage.CauseReply)
def propose_without_feedback(log_excerpt: str, job_id: str) -> triage.CheckedCause:
    return triage.client.create(prompt=log_excerpt)
e = load_error(lambda d: rule(d, "R-7")["subject"].update(target="py:__main__.propose_without_feedback"))
check("load requires an inv_feedback parameter when feedback.format is parser_error (D4)", e is not None, str(e)[:100])
enum_reg = load_variant(lambda d: rule(d, "R-7")["bad"].append(
    {"when": "!(reply.cause in [\"TIMEOUT\", \"OUT_OF_MEMORY\", \"BAD_INPUT\"])", "message": "closed set"}))
check("a clause that only restates the enum loads and is reported 'held by the schema (rung 1)' (D4)",
      len(enum_reg.notes) == 1 and "rung 1" in enum_reg.notes[0], enum_reg.notes and enum_reg.notes[0])

with inv.using(load_variant(lambda d: rule(d, "R-7")["enforcers"][0].update(mode="detect"))):
    c, _ = model('{"cause": "TIMEOUT", "message": "x"}')
    e = raises(inv.Rejected, triage.remediate, c, "J-oom")
check("detect-mode model call returns Checked with verdict violation; the enforcing edge refuses it (D4)",
      c is not None and c.verdict == "violation" and e is not None, f"{c!r}; {e}")
with inv.using(load_variant(lambda d: rule(d, "R-7")["enforcers"][0].update(on_error="fail_open_and_page"))):
    c, _ = model(GOOD, job="J-unknown")
check("fail_open_and_page returns Checked with verdict rule_error, never ok (D4)", c is not None and c.verdict == "rule_error", repr(c))

check("remediate refuses a raw CauseReply (type gate)", raises(TypeError, triage.remediate, triage.CauseReply("TIMEOUT", ""), "J1") is not None)
check("CheckedCause cannot be constructed directly", raises(TypeError, triage.CheckedCause, None) is not None)
c, _ = model(GOOD)
check("edge accepts an honest Checked: verdict ok and bad re-checked ok (D9)", triage.remediate(c, "J-oom") == "remediate J-oom: OUT_OF_MEMORY")
forged = object.__new__(triage.CheckedCause)             # falsifier demo.py::r7_forged_checked
for k, v in (("value", triage.CauseReply("TIMEOUT", "forged")), ("attempts", 1), ("rule", "R-7"), ("verdict", "ok")):
    object.__setattr__(forged, k, v)
e = raises(inv.Rejected, triage.remediate, forged, "J-oom")
check("edge refuses a forged Checked (object.__new__, verdict ok) carrying a bad value: re-check fires (D9)",
      e is not None and e.outcome.kind == "violation", e and e.outcome.messages[0])

# --- R-20: two views, storage enforcer, forced interleaving, differential ---------------------
section("R-20: door view via providers, storage view via rows (D5)")
db = pool.connect(triggers=False); db.execute("INSERT INTO pools VALUES ('p', 100)")
pool.acquire(db, "p", 60)
e = raises(inv.Rejected, pool.acquire, db, "p", 60)
check("door reads leased/size from providers: a sequential over-lease is refused by the door alone",
      e is not None and pool.leased(db, "p") == 60, e and e.outcome.messages)

def interleaving(triggers):
    """B runs to completion right after A's door read pool_leased: both doors see 0/100 (2.7)."""
    db = pool.connect(triggers=triggers); db.execute("INSERT INTO pools VALUES ('p', 100)")
    refused = 0
    def lease():
        nonlocal refused
        try: pool.acquire(db, "p", 60)
        except sqlite3.IntegrityError: refused += 1
    pool._AFTER_READ.append(lease)                       # B, run inside A's provider read
    lease()                                              # A
    while pool._AFTER_READ: pool._AFTER_READ.pop(0)()    # door ablated: no read happened, B runs after A
    return pool.leased(db, "p"), refused

ok20 = reg.evidence["R-20"]["ok"]
check("R-20 interleaving without trigger: door passes both (snapshot provider), pool leased 120/100",
      interleaving(False) == (120, 0) and reg.evidence["R-20"]["ok"] - ok20 == 2)
check("R-20 interleaving with trigger: second insert refused, 60/100", interleaving(True) == (60, 1))

def differential_r20(schema=pool.SCHEMA, seed=7, ops=600):
    """Fill both views of the one bad: the door view (providers, as the SDK fills it) and the storage
    view (rows grouped by pool_id). They must agree, and storage refuses exactly when bad is true."""
    rnd, db, r20 = random.Random(seed), pool.connect(schema=schema), reg.rules["R-20"]
    db.execute("INSERT INTO pools VALUES ('a', 50), ('b', 80), ('c', 20)")
    agree = views = refused = 0
    for _ in range(ops):
        p = rnd.choice("abc")
        rows = dict(db.execute("SELECT pool_id, SUM(n) FROM leases GROUP BY pool_id").fetchall())
        sizes = dict(db.execute("SELECT id, size FROM pools").fetchall())
        if rnd.random() < 0.8: n, new = rnd.randint(1, 15), sizes[p]
        else: n, new = 0, rnd.randint(0, 120)
        rows_bad = inv.evaluate_env(r20, {"leased": rows.get(p, 0), "n": n, "size": new}).kind == "violation"
        if n:
            door_bad = inv.evaluate(r20, {"args": {"conn": db, "pool_id": p, "slots": n}}, 0).kind == "violation"
            views += door_bad == rows_bad
        else: views += 1                                 # a resize does not pass the door: storage view only
        try:
            if n: db.execute("INSERT INTO leases (pool_id, n) VALUES (?, ?)", (p, n))
            else: pool.set_size(db, p, new)
            was_refused = False
        except sqlite3.IntegrityError: was_refused = True
        agree += was_refused == rows_bad; refused += was_refused
        if rnd.random() < 0.3: db.execute("DELETE FROM leases WHERE id = (SELECT MIN(id) FROM leases WHERE pool_id = ?)", (p,))
    return agree, views, refused, ops

a, v, r, n = differential_r20()
check("R-20 differential: door and rows views agree, and storage refuses exactly when bad is true (D5)",
      a == v == n and 0 < r < n, f"{a}/{n} storage agree, {v}/{n} views agree, {r} refused")
a, v, r, n = differential_r20(schema=pool.SCHEMA.replace("+ NEW.n > (SELECT", "+ NEW.n >= (SELECT"))
check("differential catches an off-by-one trigger mutant (> vs >=)", a < n, f"{n - a} disagreements")

# --- standing (D1, D2, D8) --------------------------------------------------------------------
section("standing: a computed report (D1, D2, D8)")
s20, why20 = inv.standing("R-20", scan=True)
nostore = load_variant(lambda d: rule(d, "R-20")["enforcers"].pop(1))
s20b, why20b = inv.standing("R-20", nostore, scan=True)
check("R-20 without its storage enforcer: the snapshot-provider door cannot count as held (D1)",
      not any("pool_leased" in w for w in why20) and s20b != "held" and any("pool_leased (snapshot)" in w for w in why20b),
      why20b[-1][:100])
s1, why1 = inv.standing("R-1", scan=True)
s1n, why1n = inv.standing("R-1")
check("R-1 is held only when inv scan also passes; the prototype does not run the scan (D8)",
      s1 == "held" and s1n == "enforce(known-callers)" and "scan" in why1n[0], f"{s1} with scan assumed; {s1n} without")

s7, why7 = inv.standing("R-7", scan=True)
s7b, why7b = inv.standing("R-7", load_variant(lambda d: d["providers"]["job"].update(consistency="snapshot")), scan=True)
check("R-7's job provider is immutable, which counts like same_txn: held with scan assumed; as snapshot it is not (D1)",
      reg.providers["job"].consistency == "immutable" and s7 == "held" and s7b != "held"
      and any("job (snapshot)" in w for w in why7b), f"{s7}; with job snapshot, {s7b}")

# --- report -----------------------------------------------------------------------------------
for rid in reg.rules:
    st, why = inv.standing(rid)
    print(f"    standing {rid}: {st}" + (f" ({'; '.join(why)})" if why else ""))
for rid, c in reg.evidence.items():
    print(f"    evidence {rid}: " + ", ".join(f"{k}={c[k]}" for k in ("runs", "ok", "violation", "rule_error", "parse_error") if c[k]))
print(f"    sink: {len(reg.sink)} events; reviews routed: {len(reg.reviews)}")
print(f"ALL {_n} CHECKS PASSED" if not _failed else f"FAILED checks: {_failed}")
sys.exit(1 if _failed else 0)
