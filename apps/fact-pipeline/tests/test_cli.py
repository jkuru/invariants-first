"""The argparse CLI, end to end on a temporary database."""
from __future__ import annotations

import json
import pathlib

import pytest

from fact_pipeline.cli import main

from .conftest import S1, S2


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_cli_flow(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "f.db")
    (tmp_path / "s1.txt").write_text(S1)
    (tmp_path / "s2.txt").write_text(S2)
    code, out = run(capsys, "--db", db, "--inject", "drop_qualifier", "ingest", str(tmp_path / "s1.txt"),
                    "--publisher", "newswire-a")
    assert code == 0 and len(json.loads(out)["stored"]) == 2
    code, out = run(capsys, "--db", db, "ingest", "--async", str(tmp_path / "s2.txt"), "--publisher", "wire-b")
    assert code == 0 and len(json.loads(out)["conflicts"]) == 1
    code, out = run(capsys, "--db", db, "promote")
    assert code == 0 and len(json.loads(out)["promoted"]) == 1 and json.loads(out)["audit_sample"]
    code, out = run(capsys, "--db", db, "ask", "Who completed the acquisition of Beta Ltd?")
    assert json.loads(out)["status"] == "ANSWERED"
    assert run(capsys, "--db", db, "monitor")[0] == 0
    code, out = run(capsys, "--db", db, "facts", "--status", "BELIEVED")
    assert len(json.loads(out)) == 1
    code, out = run(capsys, "--db", db, "conflicts")
    assert json.loads(out)[0]["owner"] == "team-knowledge"


def test_cli_entities_and_review(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "f.db")
    (tmp_path / "s1.txt").write_text(S1)
    _, uk = run(capsys, "--db", db, "add-entity", "Beta Ltd (UK)", "--alias", "Beta Ltd", "--xref", "ch:1")
    run(capsys, "--db", db, "add-entity", "Beta Ltd (Cayman)", "--alias", "Beta Ltd")
    run(capsys, "--db", db, "ingest", str(tmp_path / "s1.txt"), "--publisher", "newswire-a")
    _, out = run(capsys, "--db", db, "reviews")
    rid = json.loads(out)[0]["id"]
    code, out = run(capsys, "--db", db, "resolve-review", str(rid), uk.strip())
    assert code == 0 and len(json.loads(out)["stored"]) == 2


def test_cli_k9_duplicate_xref_fails(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "f.db")
    run(capsys, "--db", db, "add-entity", "Beta Ltd (UK)", "--xref", "ch:1")
    with pytest.raises(Exception, match="UNIQUE"):
        main(["--db", db, "add-entity", "Beta Ltd (US)", "--xref", "ch:1"])


def test_cli_delete_source_and_monitor(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "f.db")
    (tmp_path / "s1.txt").write_text(S1)
    _, out = run(capsys, "--db", db, "ingest", str(tmp_path / "s1.txt"), "--publisher", "a")
    sid = json.loads(out)["source_id"]
    code, out = run(capsys, "--db", db, "delete-source", sid)
    assert code == 0 and json.loads(out)["live_orphans"] == 0
    assert run(capsys, "--db", db, "monitor")[0] == 0


def test_cli_unknown_fault_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        main(["--inject", "nonsense", "promote"])
    assert e.value.code == 2


def test_cli_bad_rule_file_exits_2(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "rules.yaml"
    bad.write_text("spec: invariant/v0.1\nrules: [{id: X}]\n")
    assert main(["--rules", str(bad), "--db", str(tmp_path / "f.db"), "promote"]) == 2


def test_cli_resolve_review_with_no_such_review_is_a_clean_error(tmp_path: pathlib.Path,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    # one "Beta Ltd" alias: the mention resolves, no review opens, and review 1 does not exist
    db = str(tmp_path / "f.db")
    (tmp_path / "s1.txt").write_text(S1)
    _, uk = run(capsys, "--db", db, "add-entity", "Beta Ltd (UK)", "--alias", "Beta Ltd")
    run(capsys, "--db", db, "ingest", str(tmp_path / "s1.txt"), "--publisher", "newswire-a")
    assert json.loads(run(capsys, "--db", db, "reviews")[1]) == []
    code = main(["--db", db, "resolve-review", "1", uk.strip()])
    err = capsys.readouterr().err
    assert code == 2 and "no open entity review 1" in err and "none is open" in err, err
