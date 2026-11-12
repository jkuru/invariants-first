"""CI mode: the defects a coding agent can introduce in a pull request, each caught by `inv check`.

Each test copies the app, makes one change the way an agent would, and runs `inv check` in a fresh
process, as CI and the agent's stop hook do. Safe changes must still pass. The numbers match the
defect table in README.md.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

APP_ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP = shutil.ignore_patterns(".venv", "build", "__pycache__", "*.egg-info", ".pytest_cache", "tests")


@pytest.fixture
def app(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "app"
    shutil.copytree(APP_ROOT, root, ignore=SKIP)
    return root


def check(root: pathlib.Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(root / "src")}           # the copy shadows the installed app
    env.pop("INV_ABLATE", None)
    return subprocess.run([sys.executable, "-m", "invariants.cli", "check", *extra], cwd=root, env=env,
                          capture_output=True, text=True, timeout=120)


def edit(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new, 1))


def add_tool(root: pathlib.Path, body: str) -> None:
    (root / "src/support_agent/vip.py").write_text(
        "from .context import AgentContext\nfrom .models import RefundToolInput\nfrom . import admin\n"
        "from .repo import StripeGateway\n\n\n" + body)


def test_the_app_passes_as_committed(app: pathlib.Path) -> None:
    r = check(app)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no path skips a door or guard" in r.stdout


# --- safe changes pass ----------------------------------------------------------------------------
def test_safe_a_new_tool_through_the_door_passes(app: pathlib.Path) -> None:
    add_tool(app, "def vip_refund_tool(ctx: AgentContext, inp: RefundToolInput) -> str:\n"
                  "    return ctx.billing.refund(inp.order_id, inp.amount_cents, 'vip ' + inp.reason)\n")
    r = check(app)
    assert r.returncode == 0, r.stdout + r.stderr


def test_safe_a_tighter_cap_is_re_pinned_by_relock(app: pathlib.Path) -> None:
    edit(app / "src/support_agent/models.py", "amount_cents: int = Field(gt=0, le=50_000)",
         "amount_cents: int = Field(gt=0, le=20_000)")
    r = check(app, "--relock")
    assert r.returncode == 0 and "re-pinned invariants.lock" in r.stdout, r.stdout + r.stderr


# --- 1. a new tool calls the payment gateway directly -------------------------------------------
@pytest.mark.parametrize("body", [
    "def vip_refund_tool(ctx: AgentContext, inp: RefundToolInput) -> str:\n"
    "    return ctx.billing.stripe.refund(inp.order_id, inp.amount_cents)\n",        # through the context
    "def vip_refund_tool(ctx: AgentContext, inp: RefundToolInput) -> str:\n"
    "    return StripeGateway().refund(inp.order_id, inp.amount_cents)\n",           # a fresh gateway
])
def test_d1_a_tool_that_skips_billing_service_fails(app: pathlib.Path, body: str) -> None:
    add_tool(app, body)
    r = check(app)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "support_agent.vip.vip_refund_tool -> support_agent.repo.StripeGateway.refund" in r.stdout


# --- 2. the gateway itself registered as a tool ----------------------------------------------------
def test_d2_the_sink_in_the_tool_registry_fails(app: pathlib.Path) -> None:
    edit(app / "src/support_agent/tools.py", '    "CANCEL": (CancelToolInput, cancel_subscription_tool),\n',
         '    "CANCEL": (CancelToolInput, cancel_subscription_tool),\n'
         '    "VIP": (RefundToolInput, StripeGateway.refund),                # type: ignore[dict-item]\n')
    edit(app / "src/support_agent/tools.py", "from .context import AgentContext\n",
         "from .context import AgentContext\nfrom .repo import StripeGateway\n")
    r = check(app)
    assert r.returncode == 1, r.stdout + r.stderr                    # unresolved: check never passes it
    assert "the sink support_agent.repo.StripeGateway.refund is used as a value" in r.stdout


# --- 3. a raised refund cap on the tool's input --------------------------------------------------
def test_d3_a_raised_cap_is_a_weakening(app: pathlib.Path) -> None:
    edit(app / "src/support_agent/models.py", "amount_cents: int = Field(gt=0, le=50_000)",
         "amount_cents: int = Field(gt=0, le=500_000)")
    r = check(app, "--relock")                                     # even --relock will not take it
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "maximum raised" in out and "WEAKENED" in out


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_d3_a_cap_the_agent_re_pinned_itself_fails_against_the_base(app: pathlib.Path) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(["git", *cmd], cwd=app, check=True, env=env, capture_output=True)
    edit(app / "src/support_agent/models.py", "amount_cents: int = Field(gt=0, le=50_000)",
         "amount_cents: int = Field(gt=0, le=500_000)")
    lock = subprocess.run([sys.executable, "-m", "invariants.cli", "lock", "--accept-weakening"], cwd=app,
                          env={**os.environ, "PYTHONPATH": str(app / "src")}, capture_output=True, text=True)
    assert lock.returncode == 0, lock.stdout + lock.stderr            # the agent re-pins it itself
    assert check(app).returncode == 0                                 # the PR's own lock now agrees
    r = check(app, "--base", "main")                                  # CI compares with the base branch
    assert r.returncode == 1 and "since main" in r.stdout and "maximum raised" in r.stdout, r.stdout


# --- 4. more resamples so a flaky prompt passes ---------------------------------------------------
def test_d4_more_resamples_is_a_weakening(app: pathlib.Path) -> None:
    edit(app / "invariants.yaml", "semantic_resamples: 1", "semantic_resamples: 5")
    r = check(app, "--relock")
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "semantic_resamples: 1 -> 5" in out and "WEAKENED" in out


# --- 5. a new tool routed through the legacy back door in allow ----------------------------------
def test_d5_a_new_caller_of_an_allowed_back_door_fails(app: pathlib.Path) -> None:
    add_tool(app, "def instant_refund_tool(ctx: AgentContext, inp: RefundToolInput) -> str:\n"
                  "    return admin.goodwill_refund(ctx.billing.stripe, inp.order_id, inp.amount_cents)\n")
    r = check(app)
    assert r.returncode == 1, r.stdout + r.stderr
    assert ("support_agent.vip.instant_refund_tool -> support_agent.admin.goodwill_refund -> "
            "support_agent.repo.StripeGateway.refund") in r.stdout


def test_d5_allowing_it_is_a_weakening_a_person_must_accept(app: pathlib.Path) -> None:
    add_tool(app, "def instant_refund_tool(ctx: AgentContext, inp: RefundToolInput) -> str:\n"
                  "    return admin.goodwill_refund(ctx.billing.stripe, inp.order_id, inp.amount_cents)\n")
    edit(app / "invariants.yaml", '      - "py:support_agent.admin.goodwill_refund"\n',
         '      - "py:support_agent.admin.goodwill_refund"\n      - "py:support_agent.vip.instant_refund_tool"\n')
    r = check(app, "--relock")
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "a back door is now allowed: py:support_agent.vip.instant_refund_tool" in out


# --- 11. the guard deleted from the door --------------------------------------------------------
def test_d11_a_deleted_guard_fails_verify(app: pathlib.Path) -> None:
    edit(app / "src/support_agent/service.py", '    @inv.guard("BILL-1")\n', "")
    r = check(app)
    assert r.returncode == 1 and "not wrapped by its guard" in r.stdout, r.stdout + r.stderr


# --- 12. a rule deleted from the rule file -------------------------------------------------------
def test_d12_a_deleted_rule_is_a_weakening(app: pathlib.Path) -> None:
    text = (app / "invariants.yaml").read_text()
    start = text.index("  - id: BILL-2\n")
    end = text.index("  # The planner", start)
    (app / "invariants.yaml").write_text(text[:start] + text[end:])
    edit(app / "src/support_agent/service.py", '    @inv.guard("BILL-2")\n', "")
    r = check(app, "--relock")
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "rule BILL-2: removed  WEAKENED" in out
