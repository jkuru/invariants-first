"""SQLite store. The doors (ingest_door, promote_door, delete_source_door) are the only writers of facts,
evidence and fact statuses: the rules check their arguments, and `inv scan` checks nothing else calls the
row writers. The schema's own triggers and constraints (append-only rows, one canonical id per external
record, evidence before a fact) are the store's business, below the API: no rule depends on them.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from typing import Any

import invariants as inv

from .models import ChunkInfo, Contradiction, EvidenceSpan, Proposal, RetractionReport, SourceInfo, Support

LIVE = ("PROPOSED", "CHECKED", "BELIEVED")
_SENTENCE = re.compile(r"\S.*?(?:[.!?](?=\s|$)|\Z)", re.S)   # "$2.1" does not end a sentence


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def entity_id(name: str) -> str:
    """Code-computed entity id."""
    return "ent_" + sha(name.strip().lower())[:12]


def fact_id(subject_id: str, predicate: str, obj: str, qualifier: str, valid_from: str | None,
            valid_to: str | None) -> str:
    """Code-computed fact id: the same statement always gets the same id."""
    return "fact_" + sha(json.dumps([subject_id, predicate, obj, qualifier, valid_from, valid_to]))[:16]


def ddl() -> str:
    """The schema, with the store's own constraints and triggers. Their names and messages say "db
    constraint", never a rule id: they are the database's own defence, below the API, and no rule names
    them (the triggers once named k1_* and k8_* are dropped when the schema is applied)."""
    k7 = ",\n  CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)"
    k9 = ",\n  UNIQUE (source_system, source_key)"
    parts = [f"""
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, uri TEXT NOT NULL, hash TEXT NOT NULL, text TEXT NOT NULL,
  publisher_root TEXT NOT NULL, trust TEXT NOT NULL CHECK (trust IN ('trusted', 'untrusted')),
  generated_by_pipeline INTEGER NOT NULL DEFAULT 0, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), source_hash TEXT NOT NULL,
  idx INTEGER NOT NULL, start INTEGER NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS aliases (
  alias TEXT NOT NULL, entity_id TEXT NOT NULL REFERENCES entities(id), pinned INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (alias, entity_id));
CREATE TABLE IF NOT EXISTS xrefs (
  source_system TEXT NOT NULL, source_key TEXT NOT NULL, entity_id TEXT NOT NULL REFERENCES entities(id){k9});
CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES entities(id), predicate TEXT NOT NULL,
  object TEXT NOT NULL, object_kind TEXT NOT NULL, qualifier TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
  status TEXT NOT NULL, retry_count INTEGER NOT NULL DEFAULT 0,
  recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')){k7});
CREATE TABLE IF NOT EXISTS evidence (
  fact_id TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources(id), source_hash TEXT NOT NULL,
  chunk_id TEXT NOT NULL, start INTEGER NOT NULL, end_ INTEGER NOT NULL, quote TEXT NOT NULL,
  polarity TEXT NOT NULL, PRIMARY KEY (fact_id, chunk_id, start, end_));
CREATE TABLE IF NOT EXISTS fact_status (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, fact_id TEXT NOT NULL REFERENCES facts(id), status TEXT NOT NULL,
  reason TEXT NOT NULL);
-- (subject_id, predicate, id): contradiction() walks one subject's predicate in id order and stops at
-- the first live row, instead of sorting every row of it (the K-11 provider reads it on every ingest).
CREATE INDEX IF NOT EXISTS facts_by_subject_id ON facts (subject_id, predicate, id);
DROP INDEX IF EXISTS facts_by_subject;
CREATE INDEX IF NOT EXISTS status_by_fact ON fact_status (fact_id, seq);
CREATE INDEX IF NOT EXISTS evidence_by_source ON evidence (source_id);
CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL,
  polarity TEXT NOT NULL, chunk_id TEXT NOT NULL, quote TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, fact_ids TEXT NOT NULL, owner TEXT NOT NULL, reason TEXT NOT NULL,
  opened_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), resolved_at TEXT);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, rule TEXT, payload TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open');
"""]
    parts.append("""
DROP TRIGGER IF EXISTS k1_fact_needs_evidence;
CREATE TRIGGER IF NOT EXISTS fact_needs_evidence BEFORE INSERT ON facts
WHEN NOT EXISTS (SELECT 1 FROM evidence WHERE fact_id = NEW.id AND end_ > start AND length(quote) > 0)
BEGIN SELECT RAISE(ABORT, 'a fact needs an evidence span (db constraint)'); END;
""")
    for table in ("facts", "evidence", "fact_status"):          # history is append-only
        for op in ("UPDATE", "DELETE"):
            parts.append(f"""
DROP TRIGGER IF EXISTS k8_{table}_no_{op.lower()};
CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table}
BEGIN SELECT RAISE(ABORT, '{table} is append-only (db constraint)'); END;
""")
    return "".join(parts)


# --- the sinks: only the doors call these --------------------------------------------------------
def insert_evidence_row(conn: sqlite3.Connection, fid: str, e: EvidenceSpan) -> None:
    """Append one evidence row (ignored when the same span is already recorded)."""
    conn.execute("INSERT OR IGNORE INTO evidence (fact_id, source_id, source_hash, chunk_id, start, end_, quote, "
                 "polarity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (fid, e.source_id, e.source_hash, e.chunk_id, e.start, e.end, e.quote, e.polarity))


def insert_fact_row(conn: sqlite3.Connection, p: Proposal) -> None:
    """Append a new fact with its evidence (evidence first: the store's evidence trigger checks it exists)."""
    for e in p.evidence:
        insert_evidence_row(conn, p.id, e)
    conn.execute("INSERT INTO facts (id, subject_id, predicate, object, object_kind, qualifier, valid_from, "
                 "valid_to, status, retry_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CHECKED', ?)",
                 (p.id, p.subject_id, p.predicate, p.object, p.object_kind, p.qualifier, p.valid_from, p.valid_to,
                  p.retry_count))


def write_status(conn: sqlite3.Connection, fid: str, status: str, reason: str) -> None:
    """Append a status change for a fact."""
    conn.execute("INSERT INTO fact_status (fact_id, status, reason) VALUES (?, ?, ?)", (fid, status, reason))


class Store:
    """The fact store. Use ingest, promote and delete_source; they open the transaction the doors need."""

    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._depth = 0
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(ddl())

    def close(self) -> None:
        self.conn.close()

    @contextlib.contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on an exception. Nested calls join the outer one."""
        with self.lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self.conn
                finally:
                    self._depth -= 1
                return
            self.conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")
            finally:
                self._depth = 0

    def _q(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    # --- intake ---------------------------------------------------------------------------------
    def add_source(self, uri: str, text: str, publisher_root: str, trust: str = "trusted",
                   generated_by_pipeline: bool = False) -> SourceInfo:
        """Record a source (or a new version of it): code sets the hash, trust and chunks."""
        sid, h = "src_" + sha(uri)[:12], sha(text)
        with self.txn() as c:
            row = c.execute("SELECT hash FROM sources WHERE id = ?", (sid,)).fetchone()
            if row is None:
                c.execute("INSERT INTO sources (id, uri, hash, text, publisher_root, trust, generated_by_pipeline) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?)", (sid, uri, h, text, publisher_root, trust,
                                                           int(generated_by_pipeline)))
            elif row["hash"] != h:
                c.execute("UPDATE sources SET hash = ?, text = ?, deleted_at = NULL WHERE id = ?", (h, text, sid))
            if not c.execute("SELECT 1 FROM chunks WHERE source_id = ? AND source_hash = ?", (sid, h)).fetchone():
                for i, m in enumerate(_SENTENCE.finditer(text)):
                    c.execute("INSERT INTO chunks (id, source_id, source_hash, idx, start, text) "
                              "VALUES (?, ?, ?, ?, ?, ?)", (f"{sid}:{h[:8]}:{i}", sid, h, i, m.start(), m.group()))
        src = self.source(sid)
        assert src is not None
        return src

    def source(self, sid: str) -> SourceInfo | None:
        rows = self._q("SELECT * FROM sources WHERE id = ?", (sid,))
        if not rows:
            return None
        r = rows[0]
        return SourceInfo(r["id"], r["uri"], r["hash"], r["publisher_root"], r["trust"],
                          bool(r["generated_by_pipeline"]), r["deleted_at"] is not None)

    def chunks_for(self, sid: str) -> list[ChunkInfo]:
        """Chunks of the source's current version."""
        rows = self._q("SELECT k.*, s.hash AS cur FROM chunks k JOIN sources s ON s.id = k.source_id "
                       "WHERE k.source_id = ? AND k.source_hash = s.hash ORDER BY k.idx", (sid,))
        return [ChunkInfo(r["id"], r["source_id"], r["source_hash"], r["cur"], r["start"], r["text"]) for r in rows]

    def chunk(self, cid: str) -> ChunkInfo | None:
        rows = self._q("SELECT k.*, s.hash AS cur FROM chunks k JOIN sources s ON s.id = k.source_id WHERE k.id = ?",
                       (cid,))
        if not rows:
            return None
        r = rows[0]
        return ChunkInfo(r["id"], r["source_id"], r["source_hash"], r["cur"], r["start"], r["text"])

    def retrieve(self, question: str, k: int = 3) -> list[ChunkInfo]:
        """Top-k chunks by word overlap, from current versions of live sources only."""
        words = {w for w in re.findall(r"[a-z0-9]+", question.lower()) if len(w) > 3}
        rows = self._q("SELECT k.*, s.hash AS cur FROM chunks k JOIN sources s ON s.id = k.source_id "
                       "WHERE s.deleted_at IS NULL AND k.source_hash = s.hash ORDER BY k.id")
        scored = []
        for r in rows:
            score = len(words & set(re.findall(r"[a-z0-9]+", r["text"].lower())))
            if score:
                scored.append((-score, r["id"], ChunkInfo(r["id"], r["source_id"], r["source_hash"], r["cur"],
                                                          r["start"], r["text"])))
        return [c for _, _, c in sorted(scored, key=lambda t: (t[0], t[1]))[:k]]

    # --- entities -------------------------------------------------------------------------------
    def add_entity(self, name: str, aliases: tuple[str, ...] = (), xrefs: tuple[tuple[str, str], ...] = ()) -> str:
        """Mint an entity (code-computed id) with its aliases and external records."""
        eid = entity_id(name)
        with self.txn() as c:
            c.execute("INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)", (eid, name))
            for a in {name, *aliases}:
                c.execute("INSERT OR IGNORE INTO aliases (alias, entity_id) VALUES (?, ?)", (a.lower(), eid))
            for system, key in xrefs:
                self.link_xref(system, key, eid)
        return eid

    def link_xref(self, source_system: str, source_key: str, eid: str) -> None:
        """Link an external record to an entity. The schema's UNIQUE refuses a second canonical id."""
        with self.txn() as c:
            c.execute("INSERT INTO xrefs (source_system, source_key, entity_id) VALUES (?, ?, ?)",
                      (source_system, source_key, eid))

    def pin_alias(self, alias: str, eid: str) -> None:
        """A person's ruling: this alias means this entity."""
        with self.txn() as c:
            c.execute("INSERT OR REPLACE INTO aliases (alias, entity_id, pinned) VALUES (?, ?, 1)", (alias.lower(), eid))

    def candidates(self, mention: str) -> list[str]:
        """Entity ids an alias may mean; a pinned alias wins outright."""
        rows = self._q("SELECT entity_id, pinned FROM aliases WHERE alias = ? ORDER BY entity_id", (mention.lower(),))
        pinned = [r["entity_id"] for r in rows if r["pinned"]]
        return pinned[:1] if pinned else [r["entity_id"] for r in rows]

    def entity_known(self, eid: str) -> bool:
        return bool(self._q("SELECT 1 FROM entities WHERE id = ?", (eid,)))

    # --- facts ----------------------------------------------------------------------------------
    def ingest(self, proposal: Proposal) -> str:
        """Store a checked proposal through the ingest door."""
        with self.txn():
            return self.ingest_door(proposal)

    @inv.guard("py:fact_pipeline.store.Store.ingest_door")
    def ingest_door(self, proposal: Proposal) -> str:
        """The only writer of facts and evidence (K-1, K-4, K-7, K-11, K-18 run here first)."""
        if self._q("SELECT 1 FROM facts WHERE id = ?", (proposal.id,)):
            for e in proposal.evidence:
                insert_evidence_row(self.conn, proposal.id, e)
        else:
            insert_fact_row(self.conn, proposal)
        return proposal.id

    def status(self, fid: str) -> str | None:
        rows = self._q("SELECT status FROM fact_status WHERE fact_id = ? ORDER BY seq DESC LIMIT 1", (fid,))
        if rows:
            return str(rows[0]["status"])
        rows = self._q("SELECT status FROM facts WHERE id = ?", (fid,))
        return str(rows[0]["status"]) if rows else None

    def facts(self, status: str | None = None) -> list[dict[str, Any]]:
        """Every fact with its current status."""
        out = []
        for r in self._q("SELECT * FROM facts ORDER BY recorded_at, id"):
            row = dict(r)
            row["status"] = self.status(r["id"])
            if status is None or row["status"] == status:
                out.append(row)
        return out

    def evidence(self, fid: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM evidence WHERE fact_id = ? ORDER BY source_id, start", (fid,))]

    def support(self, fid: str) -> Support:
        """Distinct trusted, non-generated publishers of live sources behind a fact."""
        rows = self._q("SELECT DISTINCT s.publisher_root FROM evidence e JOIN sources s ON s.id = e.source_id "
                       "WHERE e.fact_id = ? AND s.deleted_at IS NULL AND s.trust = 'trusted' "
                       "AND s.generated_by_pipeline = 0 ORDER BY 1", (fid,))
        return Support(fid, self.status(fid) or "MISSING", [r[0] for r in rows])

    def contradiction(self, subject_id: str, predicate: str, obj: str, fid: str) -> Contradiction | None:
        """A live fact with the same subject and predicate but another value, if any (one indexed query)."""
        rows = self._q("SELECT f.id, f.object FROM facts f WHERE f.subject_id = ? AND f.predicate = ? "
                       "AND f.object != ? AND COALESCE((SELECT s.status FROM fact_status s WHERE s.fact_id = f.id "
                       "ORDER BY s.seq DESC LIMIT 1), f.status) IN ('PROPOSED', 'CHECKED', 'BELIEVED') "
                       "ORDER BY f.id LIMIT 1", (subject_id, predicate, obj))
        if not rows:
            return None
        return Contradiction(rows[0]["id"], rows[0]["object"], self.conflict_recorded(rows[0]["id"], fid))

    def stale_citations(self, fid: str) -> int:
        """Evidence rows citing a source version that is no longer current."""
        rows = self._q("SELECT COUNT(*) FROM evidence e JOIN sources s ON s.id = e.source_id "
                       "WHERE e.fact_id = ? AND e.source_hash != s.hash", (fid,))
        return int(rows[0][0])

    def orphans(self, sid: str) -> int:
        """Live facts citing this source whose every source is deleted."""
        return len(self._orphan_ids(sid))

    def _orphan_ids(self, sid: str) -> list[str]:
        rows = self._q("SELECT DISTINCT e.fact_id FROM evidence e WHERE e.source_id = ? AND NOT EXISTS ("
                       "SELECT 1 FROM evidence e2 JOIN sources s ON s.id = e2.source_id "
                       "WHERE e2.fact_id = e.fact_id AND s.deleted_at IS NULL) ORDER BY 1", (sid,))
        return [r[0] for r in rows if self.status(r[0]) in LIVE]

    def promote(self, fid: str) -> None:
        """Promote a CHECKED fact to BELIEVED through the promotion door."""
        with self.txn():
            if self.status(fid) != "CHECKED":
                raise ValueError(f"only a CHECKED fact can be promoted; {fid} is {self.status(fid)}")
            self.promote_door(fid)

    @inv.guard("py:fact_pipeline.store.Store.promote_door")
    def promote_door(self, fact_id: str) -> None:
        """The only door to BELIEVED (K-12; K-16 watches in detect mode)."""
        write_status(self.conn, fact_id, "BELIEVED", "two independent trusted sources")

    def delete_source(self, source_id: str) -> RetractionReport:
        """Withdraw a source and retract what rested only on it."""
        with self.txn():
            return self.delete_source_door(source_id)

    @inv.guard("py:fact_pipeline.store.Store.delete_source_door")
    def delete_source_door(self, source_id: str) -> RetractionReport:
        """Mark the source deleted, retract its orphans, demote BELIEVED facts that lost support (K-15 reports
        any orphan left after)."""
        self.conn.execute("UPDATE sources SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                          (source_id,))
        retracted = self._retract_orphans(source_id)
        demoted = []
        for r in self._q("SELECT DISTINCT fact_id FROM evidence WHERE source_id = ? ORDER BY 1", (source_id,)):
            fid = r[0]
            if self.status(fid) == "BELIEVED" and len(self.support(fid).trusted_publishers) < 2:
                write_status(self.conn, fid, "CHECKED", f"source {source_id} withdrawn")
                demoted.append(fid)
        return RetractionReport(source_id, retracted, demoted, self.orphans(source_id))

    def _retract_orphans(self, source_id: str) -> list[str]:
        ids = self._orphan_ids(source_id)
        for fid in ids:
            write_status(self.conn, fid, "RETRACTED", f"only source {source_id} withdrawn")
        return ids

    # --- claims, conflicts, reviews ---------------------------------------------------------------
    def add_claim(self, subject: str, predicate: str, obj: str, polarity: str, chunk_id: str, quote: str) -> None:
        """A non-ASSERTED statement is kept as a claim, never as a fact."""
        with self.txn() as c:
            c.execute("INSERT INTO claims (subject, predicate, object, polarity, chunk_id, quote) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (subject, predicate, obj, polarity, chunk_id, quote))

    def claims(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM claims ORDER BY id")]

    def open_conflict(self, fact_ids: list[str], owner: str, reason: str) -> int:
        """Open a Conflict record with an owner (the K-11 route target)."""
        with self.txn() as c:
            cur = c.execute("INSERT INTO conflicts (fact_ids, owner, reason) VALUES (?, ?, ?)",
                            (json.dumps(sorted(fact_ids)), owner, reason))
            return int(cur.lastrowid or 0)

    def conflict_recorded(self, a: str, b: str) -> bool:
        for r in self._q("SELECT fact_ids FROM conflicts WHERE resolved_at IS NULL"):
            ids = json.loads(r[0])
            if a in ids and b in ids:
                return True
        return False

    def conflicts(self) -> list[dict[str, Any]]:
        return [{**dict(r), "fact_ids": json.loads(r["fact_ids"])} for r in self._q("SELECT * FROM conflicts")]

    def add_review(self, kind: str, payload: dict[str, Any], rule: str | None = None) -> int:
        with self.txn() as c:
            cur = c.execute("INSERT INTO reviews (kind, rule, payload) VALUES (?, ?, ?)",
                            (kind, rule, json.dumps(payload, sort_keys=True)))
            return int(cur.lastrowid or 0)

    def close_review(self, rid: int) -> None:
        with self.txn() as c:
            c.execute("UPDATE reviews SET state = 'closed' WHERE id = ?", (rid,))

    def reviews(self, kind: str | None = None, state: str | None = "open") -> list[dict[str, Any]]:
        out = []
        for r in self._q("SELECT * FROM reviews ORDER BY id"):
            if (kind is None or r["kind"] == kind) and (state is None or r["state"] == state):
                out.append({**dict(r), "payload": json.loads(r["payload"])})
        return out

