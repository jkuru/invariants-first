"""The HTTP API. Route guards check request-level rules (H-1, H-2); the service doors check the rest.

Guard exceptions become application/problem+json through invariants.integrations.fastapi.install.
The schema's own triggers also refuse an over-lease (two concurrent leases can both pass the R-20
check); that refusal is the store's, below the API, and the routes answer it with a plain 409.
"""
from __future__ import annotations

import contextlib
import pathlib
import sqlite3
from typing import Annotated, Iterator

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

import invariants as inv
from invariants.integrations import fastapi as inv_fastapi
from invariants.integrations import pydantic as inv_pydantic

from . import config, db, service, store, triage, work


# --- request and response models ----------------------------------------------------------------
class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PoolIn(Closed):
    pool_id: str
    name: str
    size: int


class PoolOut(Closed):
    pool_id: str
    name: str
    size: int
    leased: int


class LeaseIn(Closed):
    holder: str = Field(min_length=1)
    slots: int


class LeaseOut(Closed):
    lease_id: int
    pool_id: str
    holder: str
    slots: int


class ResizeIn(Closed):
    new_size: int = Field(ge=0)
    proposed_by: str = Field(min_length=1)


class ApproveIn(Closed):
    approver: str = Field(min_length=1)


class ProposalOut(Closed):
    proposal_id: int
    pool_id: str
    new_size: int
    proposed_by: str
    approved_by: str
    state: str


class SplitIn(Closed):
    total_jobs: int = Field(ge=0)


class SplitOut(Closed):
    pool_id: str
    shares: dict[str, int]


class JobIn(Closed):
    job_id: str = Field(min_length=1)
    pool_id: str
    oom_killed: bool
    elapsed_s: int = Field(ge=0)
    timeout_s: int = Field(ge=0)
    exit_code: int


class TriageIn(Closed):
    log_excerpt: str = Field(min_length=1, max_length=20000)


class TriageOut(Closed):
    job_id: str
    cause: str
    action: str
    attempts: int


# --- plumbing ------------------------------------------------------------------------------------
def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def problem(status: int, title: str, rule: str | None = None, **extra: object) -> JSONResponse:
    body: dict[str, object] = {"type": "about:blank", "title": title, "status": status}
    if rule:
        body["rule"] = rule
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type=inv_fastapi.PROBLEM_JSON)


class StoreConflict(Exception):
    """The store refused a write (its own triggers): answered 409."""


@contextlib.contextmanager
def store_conflicts() -> Iterator[None]:
    """A trigger refusal from the schema becomes StoreConflict; any other database error passes through."""
    try:
        yield
    except sqlite3.IntegrityError as e:
        if db.STORE_REFUSAL in str(e):
            raise StoreConflict(str(e)) from e
        raise


router = APIRouter()


@router.post("/pools", status_code=201, operation_id="createPool", response_model=PoolOut)
@inv_fastapi.guard("py:pool_service.api.create_pool")
def create_pool(body: PoolIn, conn: Conn) -> PoolOut | JSONResponse:
    try:
        service.create_pool(conn, body.pool_id, body.name, body.size)
    except sqlite3.IntegrityError:
        return problem(409, f"pool {body.pool_id} already exists")
    return PoolOut(pool_id=body.pool_id, name=body.name, size=body.size, leased=0)


@router.get("/pools/{pool_id}", operation_id="getPool", response_model=PoolOut)
def get_pool(pool_id: str, conn: Conn) -> PoolOut | JSONResponse:
    p = store.get_pool(conn, pool_id)
    if p is None:
        return problem(404, f"pool {pool_id} not found")
    return PoolOut(**p, leased=store.leased(conn, pool_id))


@router.post("/pools/{pool_id}/leases", status_code=201, operation_id="leaseSlots", response_model=LeaseOut)
@inv_fastapi.guard("py:pool_service.api.lease")
def lease(pool_id: str, body: LeaseIn, conn: Conn) -> LeaseOut | JSONResponse:
    if store.size(conn, pool_id) is None:
        return problem(404, f"pool {pool_id} not found")
    try:
        with store_conflicts():
            lease_id = service.lease_slots(conn, pool_id, body.slots, body.holder)
    except StoreConflict:
        return problem(409, "the store refused the lease: it would exceed the pool size")
    return LeaseOut(lease_id=lease_id, pool_id=pool_id, holder=body.holder, slots=body.slots)


@router.delete("/leases/{lease_id}", status_code=204, operation_id="releaseLease")
def release(lease_id: int, conn: Conn) -> Response:
    try:
        service.release(conn, lease_id)
    except service.NotFound:
        return problem(404, f"lease {lease_id} not found")
    return Response(status_code=204)


@router.post("/pools/{pool_id}/resizes", status_code=201, operation_id="proposeResize", response_model=ProposalOut)
def propose_resize(pool_id: str, body: ResizeIn, conn: Conn) -> ProposalOut | JSONResponse:
    try:
        pid = service.propose_resize(conn, pool_id, body.new_size, body.proposed_by)
    except service.NotFound:
        return problem(404, f"pool {pool_id} not found")
    return ProposalOut(**(store.get_proposal(conn, pid) or {}))


@router.post("/resizes/{proposal_id}/approve", operation_id="approveResize", response_model=ProposalOut)
def approve_resize(proposal_id: int, body: ApproveIn, conn: Conn) -> ProposalOut | JSONResponse:
    try:
        service.approve_resize(conn, proposal_id, body.approver)
    except service.NotFound:
        return problem(404, f"proposal {proposal_id} not found")
    return ProposalOut(**(store.get_proposal(conn, proposal_id) or {}))


@router.post("/resizes/{proposal_id}/apply", operation_id="applyResize", response_model=PoolOut)
def apply_resize(proposal_id: int, conn: Conn) -> PoolOut | JSONResponse:
    try:
        with store_conflicts():
            service.ResizeService(conn).apply(proposal_id)
    except StoreConflict:
        return problem(409, "the store refused the resize: the pool's leases exceed the new size")
    except service.NotFound:
        return problem(404, f"proposal {proposal_id} not found")
    p = store.get_proposal(conn, proposal_id) or {}
    pool = store.get_pool(conn, p["pool_id"]) or {}
    return PoolOut(**pool, leased=store.leased(conn, p["pool_id"]))


@router.post("/pools/{pool_id}/split", operation_id="splitWork", response_model=SplitOut)
def split(pool_id: str, body: SplitIn, conn: Conn) -> SplitOut | JSONResponse:
    leases = store.lease_slots(conn, pool_id)
    if store.size(conn, pool_id) is None:
        return problem(404, f"pool {pool_id} not found")
    try:
        shares = work.split_work(body.total_jobs, [slots for _, _, slots in leases])
    except work.NotEnoughCapacity:
        return problem(409, "not enough leased capacity for this many jobs")
    return SplitOut(pool_id=pool_id, shares={f"lease-{lid}": s for (lid, _, _), s in zip(leases, shares)})


@router.post("/jobs", status_code=201, operation_id="recordJob", response_model=JobIn)
def record_job(body: JobIn, conn: Conn) -> JobIn | JSONResponse:
    try:
        store.insert_job(conn, body.job_id, body.pool_id, body.oom_killed, body.elapsed_s, body.timeout_s,
                         body.exit_code)
    except sqlite3.IntegrityError:
        return problem(409, f"job {body.job_id} cannot be recorded")
    return body


@router.post("/jobs/{job_id}/triage", operation_id="triageJob", response_model=TriageOut)
def triage_job(job_id: str, body: TriageIn, conn: Conn) -> TriageOut | JSONResponse:
    if store.get_job(conn, job_id) is None:
        return problem(404, f"job {job_id} not found")
    cause = triage.propose_cause(conn, job_id, body.log_excerpt)    # Escalation -> 202 via install()
    done = triage.remediate(conn, cause, job_id)
    return TriageOut(job_id=job_id, cause=done.cause, action=done.action, attempts=cause.attempts)


# --- the app -------------------------------------------------------------------------------------
def load_rules(rules: str | pathlib.Path | None = None) -> inv.Registry:
    """Load the rule file into the current registry and check every guard, route and contract."""
    path = pathlib.Path(rules or config.rules_path())
    reg = inv.load(path)
    inv.verify(reg)
    inv_pydantic.check_contracts(path, reg)
    return reg


def create_app(db_path: str | pathlib.Path | None = None, *, rules: str | pathlib.Path | None = None,
               load: bool = True) -> FastAPI:
    """Build the app. With load=True the rule file is loaded and checked first."""
    app = FastAPI(title="pool-service", version="0.1.0")
    app.state.db_path = str(db_path or config.db_path())
    db.init(app.state.db_path)
    app.include_router(router)
    inv_fastapi.install(app, reject_status=422)
    if load:
        load_rules(rules)
    inv_fastapi.verify_routes(app, pathlib.Path(rules or config.rules_path()))
    return app
