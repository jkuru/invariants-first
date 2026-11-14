"""Fix round 26: holes in mediation.allow_unresolved (reviewed acceptances of unresolved findings).

1. Entries are compared as a multiset: two identical statements in one function share a sha, so a site
   named by more entries than before (a copy of a reviewed line plus a second entry) is a weakening; a
   site named by fewer is a tightening.
2. The sha (16 hex) hashes the whole statement (every line of it, spacing normalized; a compound
   statement's header), its code and its function, so an edit of any line of a multi-line call drops the
   acceptance. An entry still follows its statement when it moves (reported as moved); entries at their
   own line are matched before moved ones.
3. An unresolved/inferred-path finding's sha also covers what reaches its site (the entrypoints and the
   name-matched calls on the paths through it), so a new untyped caller of the helper is a new,
   unaccepted finding.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import textwrap

import pytest
from cli_support import run_inv, write_project

from invariants import scan
from invariants.lock.rule_diff import _accept_changes
from invariants.scan import accept

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, env=GIT_ENV, capture_output=True)


def _codes(rep):
    return sorted(f.code for f in rep.findings)


def _doc(entries=None):
    m = {"sink": "py:app.store.Store.save", "door": "py:app.svc.create"}
    if entries is not None:
        m["allow_unresolved"] = entries
    return {"rules": [{"id": "SAVE-DOOR", "mediation": m}]}


STORE = "class Store:\n    def save(self, x):\n        return x\n"
SVC = """\
from app.store import Store


def create(x):
    return Store().save(x)


def export(obj, name):
    handler = getattr(obj, name)
    return handler()
"""


def _app(tmp_path, svc=SVC, **extra):
    return write_project(tmp_path, {"app/__init__.py": "", "app/store.py": STORE, "app/svc.py": svc,
                                    **{k: textwrap.dedent(v) for k, v in extra.items()}})


def _entry(f, why="export only reads report fields", col=True):
    return {"at": f"{f.file}:{f.line}" + (f":{f.col}" if col else ""), "code": f.code, "sha": f.sha, "why": why}


def _only(rep, code):
    return [f for f in rep.findings if f.code == code]


# --- 1. a duplicate entry is a weakening -----------------------------------------------------------------
E = {"at": "a.py:5:3", "code": "unresolved/reflection", "sha": "0123456789abcdef", "why": "x"}


def _weak(old, new):
    return [c.weakened for c in _accept_changes("rule R", old, new)]


def test_a_second_entry_for_the_same_site_is_a_weakening():
    # the same file, code and sha at another line: one more finding of that statement is accepted
    assert _weak([E], [E, {**E, "at": "a.py:12:3", "why": "same"}]) == [True]
    assert _weak([E], [E, E]) == [True]
    assert _weak([E, E], [E, E]) == [False]
    msg = _accept_changes("rule R", [E], [E, {**E, "at": "a.py:12:3"}])[0].note
    assert "2 entries for it instead of 1" in msg, msg


def test_fewer_entries_for_a_site_is_a_tightening():
    assert _weak([E, {**E, "at": "a.py:12:3"}], [E]) == [False]
    c = _accept_changes("rule R", [E, {**E, "at": "a.py:12:3"}], [E])
    assert len(c) == 1 and "1 entries instead of 2" in c[0].note, c


def test_one_more_entry_without_a_column_is_a_weakening():
    # same count, but one more entry matches any column of its line
    assert _weak([E, E], [E, {**E, "at": "a.py:5"}]) == [True]
    assert _weak([E, {**E, "at": "a.py:5"}], [E, E]) == [False]


DUP_HEAD = SVC.replace("    return handler()\n", "    handler()\n    obj, name = Store(), \"save\"\n"
                       "    handler = getattr(obj, name)\n    return handler(\"unchecked\")\n")
RULES = "spec: invariant/v0.1\nrules:\n  - id: SAVE-DOOR\n    door: py:app.svc.create\n    sink: py:app.store.Store.save\n"


def _rules(entries):
    out = RULES + "    allow_unresolved:\n"
    for e in entries:
        out += f'      - {{at: "{e["at"]}", code: {e["code"]}, sha: "{e["sha"]}", why: "{e["why"]}"}}\n'
    return out


@needs_git
def test_check_against_the_base_refuses_a_copied_reviewed_line_with_a_second_entry(tmp_path):
    root = _app(tmp_path)
    (root / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n')
    f = _only(scan.scan(_doc([]), [root]), "unresolved/computed-attribute")[0]
    (root / "invariants.yaml").write_text(_rules([_entry(f)]))
    assert run_inv("lock", "--accept-weakening", cwd=root).returncode == 0
    r = run_inv("check", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "reviewed")
    # the attack: a copy of the reviewed line on a Store, accepted by a second, identical entry
    (root / "app/svc.py").write_text(DUP_HEAD)
    rep = scan.scan(_doc([]), [root])
    found = _only(rep, "unresolved/computed-attribute")
    assert len(found) == 2 and found[0].sha == found[1].sha
    (root / "invariants.yaml").write_text(_rules([_entry(found[0]), {**_entry(found[1]), "why": "same"}]))
    assert run_inv("lock", "--accept-weakening", cwd=root).returncode == 0
    r = run_inv("check", "--base", "HEAD", cwd=root)
    assert r.returncode == 1 and "WEAKENED" in r.stdout and "2 entries for it instead of 1" in r.stdout, r.stdout


# --- 2. the sha covers the whole statement -----------------------------------------------------------------
ML = SVC.replace("    handler = getattr(obj, name)\n    return handler()\n",
                 "    return getattr(\n        obj, name)()\n")


def test_the_sha_is_16_hex_and_stable():
    assert accept.SHA_HEX == 16
    sha = accept.site_sha("unresolved/reflection", "app.svc.export", "  return  eval(x)")
    assert sha == accept.site_sha("unresolved/reflection", "app.svc.export", "return eval(x)")
    assert len(sha) == 16 and int(sha, 16) >= 0
    assert sha == "34385c1591d27b48"          # pinned: a change of the hash input breaks every entry
    # an f-string is one token on every Python version (3.12+ tokenizes its parts): the same sha on 3.11
    fs = 'x = getattr(o, f"{n!r:>{w}} " f"""a\n  {b}""")  # c'
    assert accept.site_sha("c", "o", fs) == "72d54884f935f6a7"


def test_a_reformat_keeps_the_sha_and_a_token_change_does_not():
    one = 'mod = __import__(name, fromlist=["default_handlers"])'
    wrapped = 'mod = __import__(\n        name,  # the package\n        fromlist=["default_handlers"],\n)'
    assert accept._normalized(one) != accept._normalized(wrapped)          # the trailing comma is a token
    assert accept._normalized(one) == accept._normalized(wrapped.replace(",\n)", "\n)"))
    assert accept._normalized("if not x: y()") != accept._normalized("if notx: y()")
    assert accept._normalized('s = "a  b"') != accept._normalized('s = "a b"')


def test_an_edit_of_a_later_line_of_a_multi_line_call_drops_the_acceptance(tmp_path):
    root = _app(tmp_path, ML)
    f = _only(scan.scan(_doc([]), [root]), "unresolved/computed-attribute")[0]
    assert f.line == 9
    e = _entry(f, col=False)
    rep = scan.scan(_doc([e]), [root])
    assert rep.findings == [] and len(rep.accepted) == 1
    # line 9 is unchanged; line 10, the rest of the same call, now names a Store and the sink
    (root / "app/svc.py").write_text(ML.replace("        obj, name)()", '        Store(), "sa" + "ve")("x")'))
    rep = scan.scan(_doc([e]), [root])
    assert rep.accepted == [] and "missing/allow-unresolved-unused" in _codes(rep), _codes(rep)
    assert rep.rule_result("SAVE-DOOR") == "fail"


def test_a_compound_statement_is_hashed_by_its_header(tmp_path):
    svc = SVC.replace("    handler = getattr(obj, name)\n    return handler()\n",
                      "    if getattr(obj, name)():\n        return 1\n    return 2\n")
    root = _app(tmp_path, svc)
    f = _only(scan.scan(_doc([]), [root]), "unresolved/computed-attribute")[0]
    e = _entry(f)
    (root / "app/svc.py").write_text(svc.replace("        return 1\n", "        print('x')\n        return 1\n"))
    rep = scan.scan(_doc([e]), [root])                 # its body is other statements
    assert rep.findings == [] and len(rep.accepted) == 1
    (root / "app/svc.py").write_text(svc.replace("getattr(obj, name)()", "getattr(obj, name)(1)"))
    assert scan.scan(_doc([e]), [root]).accepted == []


def test_a_one_line_body_is_its_own_statement(tmp_path):
    # `if (...\n ...): getattr(...)()`: the finding is in the body's statement, the innermost; the header
    # line of the `if` above it is not part of it (as for any statement in a body)
    svc = SVC.replace("    handler = getattr(obj, name)\n    return handler()\n",
                      "    if (obj and\n            name): getattr(obj, name)()\n")
    root = _app(tmp_path, svc)
    f = _only(scan.scan(_doc([]), [root]), "unresolved/computed-attribute")[0]
    assert f.line == 10
    e = _entry(f)
    (root / "app/svc.py").write_text(svc.replace("(obj and", "(obj or"))
    rep = scan.scan(_doc([e]), [root])
    assert rep.findings == [] and len(rep.accepted) == 1
    (root / "app/svc.py").write_text(svc.replace("name): getattr", "name ): getattr").replace("()\n", "(1)\n"))
    assert scan.scan(_doc([e]), [root]).accepted == []


def test_a_multi_line_header_with_a_one_line_body_is_hashed_whole(tmp_path):
    svc = SVC.replace("    handler = getattr(obj, name)\n    return handler()\n",
                      "    if (obj and\n            getattr(obj, name)()): return 1\n")
    root = _app(tmp_path, svc)
    f = _only(scan.scan(_doc([]), [root]), "unresolved/computed-attribute")[0]
    assert f.line == 10
    e = _entry(f)
    assert len(scan.scan(_doc([e]), [root]).accepted) == 1
    (root / "app/svc.py").write_text(svc.replace("(obj and", "(not obj and"))
    assert scan.scan(_doc([e]), [root]).accepted == []


def test_an_entry_moves_with_its_statement_and_the_move_is_reported(tmp_path):
    root = _app(tmp_path, ML)
    e = _entry(_only(scan.scan(_doc([]), [root]), "unresolved/computed-attribute")[0])
    (root / "app/svc.py").write_text("# a comment\n" + ML)
    rep = scan.scan(_doc([e]), [root])
    assert rep.findings == [] and [(f.line, f.moved_from) for f in rep.accepted] == [(10, 9)]
    assert "moved from line 9: update its at to app/svc.py:10" in scan.to_text(rep)


def test_an_entry_at_its_own_line_is_matched_before_a_moved_one(tmp_path):
    # two identical statements, two entries; the first entry's statement is deleted. The entry still at
    # its line keeps its site; the other entry follows nothing (one finding is left for two entries)
    svc = SVC.replace("    handler = getattr(obj, name)\n    return handler()\n",
                      "    a = getattr(obj, name)\n    a = getattr(obj, name)\n    return a\n")
    root = _app(tmp_path, svc)
    fs = _only(scan.scan(_doc([]), [root]), "unresolved/computed-attribute")
    assert [f.line for f in fs] == [9, 10] and fs[0].sha == fs[1].sha
    second = _entry(fs[1])
    first = {**_entry(fs[0]), "at": "app/svc.py:9"}
    (root / "app/svc.py").write_text(svc.replace("    a = getattr(obj, name)\n", "    pass\n", 1))
    rep = scan.scan(_doc([first, second]), [root])
    assert [(f.line, f.moved_from) for f in rep.accepted] == [(10, None)]
    assert _codes(rep) == ["missing/allow-unresolved-unused"]


# --- 3. an inferred path covers what reaches it --------------------------------------------------------------
PAYMENTS = """
    class Repo:
        def save(self, c, a):
            pass

    def refund(charge_id, amount):
        Repo().save(charge_id, amount)
        Legacy().refund(charge_id, amount)

    class Legacy:
        def refund(self, c, a):
            Repo().save(c, a)
"""
OPS = """
    def go(x):
        x.refund(1, 2)
"""
NUKE = """
    import payments

    def nuke(obj):
        obj.refund(3, 4)

    def main():
        nuke(payments.Legacy())
"""
PAY = {"rules": [{"id": "PAY-1", "mediation": {"sink": "py:payments.Repo.save", "door": "py:payments.refund"}}]}


def _pay(entries):
    return {"rules": [{"id": "PAY-1", "mediation": {**PAY["rules"][0]["mediation"], "allow_unresolved": entries}}]}


def _inferred(tmp_path, **files):
    root = write_project(tmp_path, {k: textwrap.dedent(v) for k, v in {"payments.py": PAYMENTS, "ops.py": OPS,
                                                                      **files}.items()})
    rep = scan.scan(PAY, [root])
    return root, _only(rep, "unresolved/inferred-path")


@pytest.mark.parametrize("name", ["evil.py", "zz_late.py"])          # before and after ops in the search
def test_a_new_untyped_caller_of_an_accepted_inferred_path_is_not_accepted(tmp_path, name):
    root, [f] = _inferred(tmp_path)
    e = _entry(f, "ops.go only ever receives a Service")
    rep = scan.scan(_pay([e]), [root])
    assert rep.findings == [] and len(rep.accepted) == 1
    (root / name).write_text(textwrap.dedent(NUKE))
    rep = scan.scan(_pay([e]), [root])
    assert rep.accepted == [] and _codes(rep) == ["missing/allow-unresolved-unused", "unresolved/inferred-path"]
    assert f.reached_by == ["entrypoint ops.go", "ops.go -> .refund"]
    new = _only(rep, "unresolved/inferred-path")[0]
    assert f"{name[:-3]}.nuke -> .refund" in new.reached_by
    assert "new untyped caller" in _only(rep, "missing/allow-unresolved-unused")[0].message


def test_a_new_typed_caller_of_the_reviewed_function_is_not_accepted(tmp_path):
    root, [f] = _inferred(tmp_path)
    e = _entry(f)
    (root / "zz.py").write_text("import ops, payments\n\ndef z():\n    ops.go(payments.Legacy())\n")
    rep = scan.scan(_pay([e]), [root])
    assert rep.accepted == []
    assert "entrypoint zz.z" in _only(rep, "unresolved/inferred-path")[0].reached_by


def test_unrelated_code_keeps_an_accepted_inferred_path(tmp_path):
    root, [f] = _inferred(tmp_path)
    e = _entry(f)
    (root / "zz.py").write_text("def unrelated(y):\n    return y.other()\n")
    (root / "ops.py").write_text("# moved down\n" + (root / "ops.py").read_text())
    rep = scan.scan(_pay([e]), [root])
    assert rep.findings == [] and len(rep.accepted) == 1


def test_the_text_output_shows_what_an_inferred_path_covers(tmp_path):
    root, _ = _inferred(tmp_path)
    text = scan.to_text(scan.scan(_pay([]), [root]))
    assert "covers 1 entrypoint(s) and 1 name-matched call(s): ops.go -> .refund]" in text, text
    data = scan.scan(_pay([]), [root]).as_dict()
    assert data["findings"][0]["reached_by"] == ["entrypoint ops.go", "ops.go -> .refund"]


def test_an_inferred_path_through_a_cycle_covers_every_caller_in_it(tmp_path):
    # a name-matched call into the sink at the site itself, reached through two functions that call
    # each other: the basis is worked out per strongly connected component
    ops = """
        def helper(r, n):
            r.save(1, 2)
            if n:
                ping(r, n - 1)

        def ping(r, n):
            helper(r, n)

        def main(r):
            ping(r, 3)
    """
    root = write_project(tmp_path, {"payments.py": textwrap.dedent(PAYMENTS), "ops.py": textwrap.dedent(ops)})
    [f] = _only(scan.scan(PAY, [root]), "unresolved/inferred-path")
    assert f.reached_by == ["entrypoint ops.main", "ops.helper -> .save"]
    (root / "late.py").write_text("import ops\n\ndef other(x):\n    ops.ping(x, 1)\n")
    [g] = _only(scan.scan(PAY, [root]), "unresolved/inferred-path")
    assert "entrypoint late.other" in g.reached_by and g.sha != f.sha


@pytest.mark.parametrize("old, new", [("args.x > 1", "args.x > 1 && true"), ("args.a > args.b", "true && args.a > args.b"),
                                      ("args.x > 1", "args.x > 1 || false"),
                                      ("args.x > 1 || args.y < 2", "false || args.y < 2 || args.x > 1")])
def test_an_identity_constant_in_a_bad_chain_is_the_same_condition(old, new):
    """`a && true` and `a || false` are a, for a bool a: no weakening, and never 'matches fewer cases'."""
    from invariants.cel import subset
    from invariants.lock.cel_mono import judge_bad
    assert judge_bad(old, new, subset.parse, None)[0] == "same"


@pytest.mark.parametrize("old, new", [("args.x", "args.x && true"), ("args.x > 1", "args.x > 1 && args.y > 1")])
def test_a_conjunct_that_may_not_be_a_bool_is_still_not_the_same(old, new):
    from invariants.cel import subset
    from invariants.lock.cel_mono import judge_bad
    assert judge_bad(old, new, subset.parse, None)[0] != "same"
