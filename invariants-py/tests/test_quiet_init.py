"""A quieter `inv init`: capped listings, crown-jewel sinks first, and at most 3 door rules by default."""
from __future__ import annotations

import argparse
import pathlib

import pytest

from cli_support import write_project
from invariants import doors
from invariants.cli import parser

MORE = "(`inv scan` lists them all)"


def _door(back: int = 0, unresolved: int = 0, allow: int = 0, active: bool = True) -> doors.Door:
    d = doors.Door("LEDGER-DOOR", "m.ledger", ["m.service.pay"], ["m.ledger.write"], ["m.api.a", "m.api.b"])
    d.back_doors = [f"m/b{i}.py:{i + 1} m.b{i}.f reaches the sink without passing m.service.pay" for i in range(back)]
    d.unresolved = [(f"m/u{i}.py:{i + 1}:5", "unresolved/inferred-path", f"{i:016x}", f"site {i}")
                    for i in range(unresolved)]
    d.allow = [f"m.b{i}.f" for i in range(allow)]
    d.active = active
    if not active:
        d.why_inactive = "review the findings by hand"
    return d


def _text(d: doors.Door) -> str:
    return "\n".join(doors.render(doors.DoorsFound([d], [], {}, 1)))


# --- listing caps ----------------------------------------------------------------------------------
def test_five_back_doors_and_five_unresolved_sites_are_listed_in_full():
    text = _text(_door(back=5, unresolved=5, allow=5))
    for i in range(5):
        assert f"  #   m/b{i}.py:{i + 1} m.b{i}.f reaches" in text
        assert f"  #   m/u{i}.py:{i + 1}:5 site {i}" in text
    assert "more " + MORE not in text and "…" not in text


def test_a_sixth_back_door_and_site_are_summed_up_in_one_line():
    text = _text(_door(back=6, unresolved=8, allow=6))
    lines = text.splitlines()
    assert sum(1 for x in lines if " reaches the sink without passing" in x) == 5
    assert "m/b5.py" not in text                                  # the sixth is not listed
    # (the drafted allow_unresolved entries are not a listing: they hold every site)
    assert sum(1 for x in lines if x.startswith("  #   m/u")) == 5 and "  #   m/u5.py" not in text
    assert f"  #   … and 1 more {MORE}" in lines                   # back doors: 6 - 5
    assert f"  #   … and 3 more {MORE}" in lines                   # unresolved sites: 8 - 5
    assert "6 back door(s)" in text and "8 unresolved site(s)" in text   # the counts stay whole


@pytest.mark.parametrize("active", [True, False])
def test_the_frozen_allow_entries_are_never_truncated(active):
    text = _text(_door(back=9, allow=9, active=active))
    prefix = "  " if active else "  # "
    for i in range(9):
        assert f'{prefix}    - "py:m.b{i}.f"' in text.splitlines()


def test_the_console_summary_caps_its_file_lists():
    d = _door(back=7, unresolved=6)
    out = doors.summary(doors.DoorsFound([d], [], {}, 1))
    back = next(x for x in out if "back door(s) in" in x)
    assert "m/b4.py" in back and "m/b5.py" not in back and "… and 2 more" in back
    unres = next(x for x in out if "unresolved site(s)" in x)
    assert "m/u4.py" in unres and "m/u5.py" not in unres and "… and 1 more" in unres
    few = doors.summary(doors.DoorsFound([_door(back=5)], [], {}, 1))
    assert not any("more" in x for x in few)


# --- ranking ---------------------------------------------------------------------------------------
def _two(tmp_path: pathlib.Path, first: str, second: str, extra: dict[str, str] | None = None) -> pathlib.Path:
    """Two repositories with no telling names: `first` has three callers, `second` two."""
    files = {
        "m/__init__.py": "",
        "m/repo.py": f"class {first}:\n    def save_alpha(self, session, x):\n        session.add(x)\n"
                     f"class {second}:\n    def save_beta(self, session, x):\n        session.add(x)\n",
        "m/service.py": f"from m.repo import {first}, {second}\n"
                        f"class AlphaService:\n    def do(self, s):\n        {first}().save_alpha(s, 1)\n"
                        f"class BetaService:\n    def do(self, s):\n        {second}().save_beta(s, 1)\n",
        "m/api.py": "from m.service import AlphaService, BetaService\n"
                    "def a1(s): AlphaService().do(s)\ndef a2(s): AlphaService().do(s)\n"
                    "def a3(s): AlphaService().do(s)\n"
                    "def b1(s): BetaService().do(s)\ndef b2(s): BetaService().do(s)\n",
        **(extra or {})}
    return write_project(tmp_path / "m", files)


def test_without_a_telling_name_the_most_called_repository_comes_first(tmp_path):
    found = doors.discover([_two(tmp_path, "AlphaRepo", "BetaRepo")], limit=1)
    assert [d.rid for d in found.doors] == ["ALPHA-REPO-DOOR"]


def test_an_external_gateway_ranks_above_a_keyword_equal_repository(tmp_path):
    found = doors.discover([_two(tmp_path, "AlphaRepo", "BetaGateway")], limit=1)
    assert [d.rid for d in found.doors] == ["BETA-GATEWAY-DOOR"]
    assert [d.rid for d in found.dropped] == ["ALPHA-REPO-DOOR"]
    both = doors.discover([_two(tmp_path / "2", "AlphaRepo", "BetaGateway")], limit=2)
    assert [d.rid for d in both.doors] == ["BETA-GATEWAY-DOOR", "ALPHA-REPO-DOOR"]


@pytest.mark.parametrize("names, external", [
    (("m.repo.StripeGateway",), True), (("m.billing.HTTPClient",), True), (("m.stripe_client",), True),
    (("m.payments.PaypalStore",), True), (("m.repo.LedgerApi",), True), (("m.repo.LedgerAPI",), True),
    (("m.repo.LedgerRepo", "m.http"), True), (("m.mail.MailClient",), True), (("m.payments.stripe",), True),
    (("m.repo.LedgerRepo",), False), (("m.api",), False), (("m.api_routes",), False), (("m.repo.Clients",), False),
    (("m.capital.Table",), False), (("m.repo.LedgerRepo", "m.repo"), False), ((), False),
])
def test_external_names(names, external):
    assert doors._external(*names) is external


def test_the_sinks_own_class_counts_when_the_rule_is_a_module(tmp_path):
    # outside a repository a rule is the writes' module (m.outbound); its writes are StripeClient's methods
    root = write_project(tmp_path / "m", {
        "m/__init__.py": "",
        "m/repo.py": "class AlphaRepo:\n    def save_alpha(self, session, x):\n        session.add(x)\n",
        "m/outbound.py": "class StripeClient:\n    def __init__(self, stripe):\n        self.stripe = stripe\n"
                        "    def send_beta(self, x):\n        self.stripe.Refund.create(x)\n",
        "m/service.py": "from m.repo import AlphaRepo\nfrom m.outbound import StripeClient\n"
                        "class AlphaService:\n    def do(self, s):\n        AlphaRepo().save_alpha(s, 1)\n"
                        "class BetaService:\n    def do(self, s):\n        StripeClient(s).send_beta(1)\n",
        "m/api.py": "from m.service import AlphaService, BetaService\n"
                    "def a1(s): AlphaService().do(s)\ndef a2(s): AlphaService().do(s)\n"
                    "def a3(s): AlphaService().do(s)\n"
                    "def b1(s): BetaService().do(s)\ndef b2(s): BetaService().do(s)\n"})
    found = doors.discover([root], limit=1)
    assert [(d.resource, d.sinks) for d in found.doors] == [("m.outbound", ["m.outbound.StripeClient.send_beta"])]


def test_a_rule_dominated_by_unresolved_sites_ranks_lower(tmp_path):
    # two calls on an unknown receiver that name AlphaRepo.save_alpha: unresolved sites, and nothing else
    misc = {"m/misc.py": "def f(thing, s):\n    thing.save_alpha(s, 2)\ndef g(thing, s):\n    thing.save_alpha(s, 3)\n"}
    found = doors.discover([_two(tmp_path, "AlphaRepo", "BetaRepo", misc)], limit=1)
    assert [d.rid for d in found.doors] == ["BETA-REPO-DOOR"]
    assert [d.rid for d in found.dropped] == ["ALPHA-REPO-DOOR"]
    both = doors.discover([_two(tmp_path / "2", "AlphaRepo", "BetaRepo", misc)], limit=2)
    assert [d.rid for d in both.doors] == ["BETA-REPO-DOOR", "ALPHA-REPO-DOOR"]
    assert len(both.doors[1].unresolved) == 2


def test_a_rule_with_more_back_doors_than_unresolved_sites_keeps_its_rank(tmp_path):
    # one unresolved site and two back doors: not dominated, so the most called repository stays first
    misc = {"m/misc.py": "from m.repo import AlphaRepo\n"
                         "def f(thing, s):\n    thing.save_alpha(s, 2)\n"
                         "def h(s):\n    AlphaRepo().save_alpha(s, 4)\n"
                         "def k(s):\n    AlphaRepo().save_alpha(s, 5)\n"}
    found = doors.discover([_two(tmp_path, "AlphaRepo", "BetaRepo", misc)], limit=1)
    assert [d.rid for d in found.doors] == ["ALPHA-REPO-DOOR"]
    assert len(found.doors[0].back_doors) == 2 and len(found.doors[0].unresolved) == 1


def test_unresolved_dominated():
    assert doors._dominated(unresolved=2, other=1) and doors._dominated(unresolved=1, other=0)
    assert not doors._dominated(unresolved=1, other=1) and not doors._dominated(unresolved=0, other=0)
    assert not doors._dominated(unresolved=1, other=2)


# --- the default number of door rules --------------------------------------------------------------
def test_init_writes_at_most_three_door_rules_by_default():
    a = parser().parse_args(["init"])
    assert a.max_doors == 3
    sub = next(x for x in parser()._actions if isinstance(x, argparse._SubParsersAction))
    assert "(default 3)" in (sub.choices["init"]._option_string_actions["--max-doors"].help or "")
    assert doors.discover.__defaults__ is not None and 3 in doors.discover.__defaults__


def test_propose_rules_drafts_at_most_three_by_default(tmp_path, monkeypatch):
    from invariants.mcp import propose, protocol
    seen: list[int] = []
    real = doors.discover

    def spy(*a, **k):
        seen.append(k["limit"])
        return real(*a, **k)
    monkeypatch.setattr(doors, "discover", spy)
    write_project(tmp_path / "p", {"p/__init__.py": "", "p/store.py": "def save(session, x):\n    session.add(x)\n"})
    propose.tool_propose_rules((tmp_path / "p").resolve(), None, {})
    assert seen == [3]
    assert "(default 3)" in protocol.TOOLS["propose_rules"][3]["max_doors"]["description"]
