"""The 24 changes as exact text edits. make_patches.py turns each into a unified diff against pristine/."""

P, F = "pool-service", "fact-pipeline"

CHANGES = {
# ---------------------------------------------------------------- pool-service
"P01": (P, "C", [("src/pool_service/work.py",
    "for i in sorted(range(len(capacities)), key=lambda i: (-rems[i], i))[:left]:",
    "for i in sorted(range(len(capacities)), key=lambda i: (-rems[i], i))[:left + 1]:")]),

"P02": (P, "C", [("src/pool_service/api.py",
    '@router.delete("/leases/{lease_id}"',
    '''class BulkLeaseIn(Closed):
    holder: str = Field(min_length=1)
    count: int = Field(ge=1)
    slots: int


@router.post("/pools/{pool_id}/leases/bulk", status_code=201, operation_id="bulkLease", response_model=list[LeaseOut])
def bulk_lease(pool_id: str, body: BulkLeaseIn, conn: Conn) -> list[LeaseOut] | JSONResponse:
    """Lease `count` blocks of `slots` in one call (faster than N round trips)."""
    if store.size(conn, pool_id) is None:
        return problem(404, f"pool {pool_id} not found")
    out = []
    with storage_rejections():
        for _ in range(body.count):
            lid = store.insert_lease(conn, pool_id, body.holder, body.slots)
            out.append(LeaseOut(lease_id=lid, pool_id=pool_id, holder=body.holder, slots=body.slots))
    return out


@router.delete("/leases/{lease_id}"''')], "openapi"),

"P03": (P, "C", [("src/pool_service/service.py",
    'store.set_proposal_state(conn, proposal_id, "APPROVED", approved_by=approver)',
    'store.set_proposal_state(conn, proposal_id, "APPROVED", approved_by=p["proposed_by"])')]),

"P04": (P, "C", [
    ("invariants.yaml",
     "statement: One resize never grows a pool to more than four times its size; such a write is rolled back.",
     "statement: One resize never grows a pool to more than ten times its size; such a write is rolled back."),
    ("invariants.yaml",
     "self.sizes[req.pool_id] > 4 * old_self.sizes[req.pool_id]'",
     "self.sizes[req.pool_id] > 10 * old_self.sizes[req.pool_id]'"),
    ("tests/test_resize.py",
     '''    """Falsifier: 10 -> 50 is approved but breaks R-22 after the write; the transaction rolls back."""
    make_pool(client, "gpu-a", 10)
    pid = propose(client, "gpu-a", 50)''',
     '''    """Falsifier: 10 -> 150 is approved but breaks R-22 after the write; the transaction rolls back."""
    make_pool(client, "gpu-a", 10)
    pid = propose(client, "gpu-a", 150)'''),
]),

"P05": (P, "U", [("src/pool_service/api.py",
    "    return PoolOut(**p, leased=store.leased(conn, pool_id))",
    "    return PoolOut(**p, leased=len(store.lease_slots(conn, pool_id)))")]),

"P06": (P, "U", [("src/pool_service/service.py",
    '''    if p["state"] == "PENDING":
        store.set_proposal_state(conn, proposal_id, "APPROVED", approved_by=approver)''',
    '''    store.set_proposal_state(conn, proposal_id, "APPROVED", approved_by=approver)''')]),

"P07": (P, "U", [("src/pool_service/api.py",
    'shares={f"lease-{lid}": s for',
    'shares={f"lease-{lid + 1}": s for')]),

"P08": (P, "U", [("src/pool_service/api.py",
    "store.insert_job(conn, body.job_id, body.pool_id, body.oom_killed, body.elapsed_s, body.timeout_s,",
    "store.insert_job(conn, body.job_id, body.pool_id, body.oom_killed, body.timeout_s, body.elapsed_s,")]),

"P09": (P, "B", [("src/pool_service/store.py",
    '''def leased(conn: sqlite3.Connection, pool_id: str) -> int:
    return int(conn.execute("SELECT COALESCE(SUM(slots), 0) FROM leases WHERE pool_id = ?",
                            (pool_id,)).fetchone()[0])''',
    '''_LEASED_SQL = "SELECT COALESCE(SUM(slots), 0) FROM leases WHERE pool_id = ?"


def leased(conn: sqlite3.Connection, pool_id: str) -> int:
    """Slots leased in the pool."""
    (total,) = conn.execute(_LEASED_SQL, (pool_id,)).fetchone()
    return int(total)''')]),

"P10": (P, "B", [
    ("src/pool_service/store.py",
     "def size(conn: sqlite3.Connection, pool_id: str) -> int | None:",
     '''def list_pools(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [{"pool_id": r[0], "name": r[1], "size": r[2]} for r in
            conn.execute("SELECT id, name, size FROM pools ORDER BY id")]


def size(conn: sqlite3.Connection, pool_id: str) -> int | None:'''),
    ("src/pool_service/api.py",
     '@router.get("/pools/{pool_id}", operation_id="getPool", response_model=PoolOut)',
     '''@router.get("/pools", operation_id="listPools", response_model=list[PoolOut])
def list_pools(conn: Conn) -> list[PoolOut]:
    return [PoolOut(**p, leased=store.leased(conn, p["pool_id"])) for p in store.list_pools(conn)]


@router.get("/pools/{pool_id}", operation_id="getPool", response_model=PoolOut)'''),
    ("tests/test_api.py",
     "def test_r24_pool_size_out_of_range(client: TestClient) -> None:",
     '''def test_list_pools(client: TestClient) -> None:
    make_pool(client, "gpu-b", 4)
    make_pool(client, "gpu-a", 8)
    assert [p["pool_id"] for p in client.get("/pools").json()] == ["gpu-a", "gpu-b"]


def test_r24_pool_size_out_of_range(client: TestClient) -> None:'''),
], "openapi"),

"P11": (P, "B", [
    ("src/pool_service/work.py", "import invariants as inv\n", "import heapq\n\nimport invariants as inv\n"),
    ("src/pool_service/work.py",
     "for i in sorted(range(len(capacities)), key=lambda i: (-rems[i], i))[:left]:",
     "for i in heapq.nsmallest(left, range(len(capacities)), key=lambda i: (-rems[i], i)):"),
]),

"P12": (P, "B", [
    ("src/pool_service/api.py", "import contextlib\n", "import contextlib\nimport logging\n"),
    ("src/pool_service/api.py", "router = APIRouter()\n", "router = APIRouter()\nlog = logging.getLogger(\"pool_service.api\")\n"),
    ("src/pool_service/api.py",
     "    return LeaseOut(lease_id=lease_id, pool_id=pool_id, holder=body.holder, slots=body.slots)",
     '''    log.info("lease %s: %d slots in %s for %s", lease_id, body.slots, pool_id, body.holder)
    return LeaseOut(lease_id=lease_id, pool_id=pool_id, holder=body.holder, slots=body.slots)'''),
]),

# ---------------------------------------------------------------- fact-pipeline
"F01": (F, "C", [("src/fact_pipeline/pipeline.py",
    'chunk.start + f["end"], f["quote"], f["polarity"])',
    'chunk.start + f["end"] - 1, f["quote"], f["polarity"])')]),

"F02": (F, "C", [("src/fact_pipeline/store.py",
    "    def status(self, fid: str) -> str | None:",
    '''    def bulk_import(self, proposals: list[Proposal]) -> list[str]:
        """Load many already-checked proposals in one transaction (backfills; much faster than ingest)."""
        with self.txn():
            for p in proposals:
                if not self._q("SELECT 1 FROM facts WHERE id = ?", (p.id,)):
                    insert_fact_row(self.conn, p)
        return [p.id for p in proposals]

    def status(self, fid: str) -> str | None:''')]),

"F03": (F, "C", [("src/fact_pipeline/store.py",
    '''"WHERE e.fact_id = ? AND s.deleted_at IS NULL AND s.trust = 'trusted' "''',
    '''"WHERE e.fact_id = ? AND s.deleted_at IS NULL "''')]),

"F04": (F, "C", [
    ("src/fact_pipeline/store.py",
     '''    def promote(self, fid: str) -> None:
        """Promote a CHECKED fact to BELIEVED through the promotion door."""
        with self.txn():
            if self.status(fid) != "CHECKED":
                raise ValueError(f"only a CHECKED fact can be promoted; {fid} is {self.status(fid)}")
            self.promote_door(fid)

    @inv.guard("py:fact_pipeline.store.Store.promote_door")
    def promote_door(self, fact_id: str) -> None:
        """The only door to BELIEVED (K-12; K-16 watches in detect mode)."""
        write_status(self.conn, fact_id, "BELIEVED", "two independent trusted sources")''',
     '''    def promote(self, fid: str) -> None:
        """Promote a CHECKED fact to BELIEVED through the promotion door."""
        with self.txn():
            if self.status(fid) != "CHECKED":
                raise ValueError(f"only a CHECKED fact can be promoted; {fid} is {self.status(fid)}")
            self.promote_door([fid])

    @inv.guard("py:fact_pipeline.store.Store.promote_door")
    def promote_door(self, fact_ids: list[str]) -> None:
        """The only door to BELIEVED (K-12; K-16 watches in detect mode). Batch form: one round trip."""
        for fact_id in fact_ids:
            write_status(self.conn, fact_id, "BELIEVED", "two independent trusted sources")'''),
]),

"F05": (F, "U", [("src/fact_pipeline/pipeline.py",
    'if f["retry_count"] > 0 and f["status"] != "RETRACTED"]',
    'if f["retry_count"] > 1 and f["status"] != "RETRACTED"]')]),

"F06": (F, "U", [("src/fact_pipeline/store.py",
    "return [c for _, _, c in sorted(scored, key=lambda t: (t[0], t[1]))[:k]]",
    "return [c for _, _, c in sorted(scored, key=lambda t: (t[0], t[1]))[:k - 1]]")]),

"F07": (F, "U", [("src/fact_pipeline/pipeline.py",
    '''                    "chunk_id": chunk.id, "index": index, "error": str(e), "attempts": e.attempts}, rule=e.rule))''',
    '''                    "chunk_id": chunk.id, "index": index}, rule=e.rule))''')]),

"F08": (F, "U", [("src/fact_pipeline/store.py",
    '''rows = self._q("SELECT entity_id, pinned FROM aliases WHERE alias = ? ORDER BY entity_id", (mention.lower(),))''',
    '''rows = self._q("SELECT entity_id, pinned FROM aliases WHERE alias = ? ORDER BY entity_id", (mention,))''')]),

"F09": (F, "B", [("src/fact_pipeline/store.py",
    '''        out = []
        for r in self._q("SELECT * FROM facts ORDER BY recorded_at, id"):
            row = dict(r)
            row["status"] = self.status(r["id"])
            if status is None or row["status"] == status:
                out.append(row)
        return out''',
    '''        rows = [{**dict(r), "status": self.status(r["id"])} for r in self._q("SELECT * FROM facts ORDER BY recorded_at, id")]
        return [row for row in rows if status is None or row["status"] == status]''')]),

"F10": (F, "B", [
    ("src/fact_pipeline/cli.py",
     '''    sub.add_parser("conflicts", help="list Conflict records")''',
     '''    sub.add_parser("stats", help="count facts per status")
    sub.add_parser("conflicts", help="list Conflict records")'''),
    ("src/fact_pipeline/cli.py",
     '''        elif a.command == "conflicts":''',
     '''        elif a.command == "stats":
            counts: dict[str, int] = {}
            for f in store.facts():
                counts[f["status"]] = counts.get(f["status"], 0) + 1
            _print(counts)
        elif a.command == "conflicts":'''),
    ("tests/test_cli.py",
     "def test_cli_unknown_fault_is_a_usage_error(",
     '''def test_cli_stats(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "f.db")
    (tmp_path / "s1.txt").write_text(S1)
    run(capsys, "--db", db, "--inject", "drop_qualifier", "ingest", str(tmp_path / "s1.txt"), "--publisher", "a")
    code, out = run(capsys, "--db", db, "stats")
    assert code == 0 and json.loads(out) == {"CHECKED": 2}


def test_cli_unknown_fault_is_a_usage_error('''),
]),

"F11": (F, "B", [("src/fact_pipeline/pipeline.py",
    '''        for chunk in self.store.chunks_for(source_id):
            got, rv = self._extract_chunk(chunk)
            items += got
            reviews += rv
        return items, reviews''',
    '''        for chunk in self.store.chunks_for(source_id):
            got, rv = self._extract_chunk(chunk)
            items.extend(got)
            reviews.extend(rv)
        return items, reviews''')]),

"F12": (F, "B", [
    ("src/fact_pipeline/models.py",
     'PREDICATES: tuple[str, ...] = ("acquired", "acquisition_price", "in_talks", "ceo_of", "headquartered_in")',
     'PREDICATES: tuple[str, ...] = ("acquired", "acquisition_price", "in_talks", "ceo_of", "headquartered_in",\n                               "founded_in")'),
    ("src/fact_pipeline/models.py",
     '''Predicate = Literal["acquired", "acquisition_price", "in_talks", "ceo_of", "headquartered_in",
                    "NONE", "CANNOT_COMPLY"]''',
     '''Predicate = Literal["acquired", "acquisition_price", "in_talks", "ceo_of", "headquartered_in", "founded_in",
                    "NONE", "CANNOT_COMPLY"]'''),
    ("invariants.yaml",
     '''bad: '!(args.proposal.predicate in ["acquired", "acquisition_price", "in_talks", "ceo_of", "headquartered_in"])\'''',
     '''bad: '!(args.proposal.predicate in ["acquired", "acquisition_price", "in_talks", "ceo_of", "headquartered_in", "founded_in"])\''''),
    ("tests/test_store_rules.py",
     "def test_k7_guard_refuses_inverted_interval(",
     '''def test_founded_in_is_accepted(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    fid = store.ingest(make_proposal(predicate="founded_in", object="1999"))
    assert store.status(fid) == "CHECKED"


def test_k7_guard_refuses_inverted_interval('''),
]),
}

# ---------------------------------------------------------------- re-based edits
# The patches and edits above are the record of the published runs (2026-11-10). Since then the apps
# changed (R-22 now refuses at the door before the write instead of rolling the write back), so P04's
# text no longer exists. REBASED makes the same change against the current apps; run.py uses it only
# when neither the patch nor the edits above apply, and says so in each result ("applied_by").
REBASED = {
"P04": (P, "C", [
    ("invariants.yaml",
     "statement: One resize never grows a pool to more than four times its current size.",
     "statement: One resize never grows a pool to more than ten times its current size."),
    ("invariants.yaml",
     "req.new_size > 4 * self.sizes[req.pool_id]'",
     "req.new_size > 10 * self.sizes[req.pool_id]'"),
    ("tests/test_resize.py",
     '''    """Falsifier: 10 -> 50 is approved but breaks R-22; the door refuses before the write."""
    make_pool(client, "gpu-a", 10)
    pid = propose(client, "gpu-a", 50)''',
     '''    """Falsifier: 10 -> 150 is approved but breaks R-22; the door refuses before the write."""
    make_pool(client, "gpu-a", 10)
    pid = propose(client, "gpu-a", 150)'''),
]),
}
