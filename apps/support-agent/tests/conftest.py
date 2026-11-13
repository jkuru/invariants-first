"""Fixtures: the rules loaded once, a fresh ledger per test, a fresh fake model, and the replay model.

The defect tests run on the fake (it can be told to misbehave). The happy-path flows in
test_replay.py run on the model APP_MODEL names: replay (the default) answers from
tests/fixtures/traces/; `--record` (or APP_MODEL=record) calls Claude and rewrites those traces.
"""
from __future__ import annotations

import os
import pathlib
from typing import Callable, Iterator

import pytest

import invariants as inv
from support_agent import context, fakestripe, llm, planner
from support_agent.fake_model import FakeAgentModel

APP_ROOT = pathlib.Path(__file__).resolve().parents[1]
TRACES = APP_ROOT / "tests" / "fixtures" / "traces"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--record", action="store_true", default=False,
                     help="APP_MODEL=record: run the replay flows on Claude and rewrite tests/fixtures/traces/ "
                          "(needs the anthropic package and ANTHROPIC_API_KEY)")


@pytest.fixture(scope="session", autouse=True)
def rules_loaded() -> inv.Registry:
    """The plugin loads invariants.yaml (ini inv_rules); load it here too when run without the plugin."""
    if not inv.default_registry().rules:
        inv.load(APP_ROOT / "invariants.yaml")
    return inv.default_registry()


@pytest.fixture
def ctx() -> context.AgentContext:
    fakestripe.reset()
    return context.demo_context()


@pytest.fixture(autouse=True)
def model() -> Iterator[FakeAgentModel]:
    m = FakeAgentModel()
    planner.set_model(m)
    yield m
    planner.set_model(FakeAgentModel())


@pytest.fixture
def model_mode(request: pytest.FixtureRequest) -> str:
    """replay (default), record, anthropic or fake: what the replay flows run on."""
    return "record" if request.config.getoption("--record") else os.environ.get("APP_MODEL", "replay")


@pytest.fixture
def traced(model_mode: str) -> Iterator[Callable[..., llm.AgentModel]]:
    """traced(name, reply="json"): make the planner use the APP_MODEL model for trace
    tests/fixtures/traces/<name>.json, answering as JSON text or (reply="tool") by tool call.
    On replay, every call the trace holds must have been made by the end of the test."""
    made: list[llm.AgentModel] = []

    def make(name: str, reply: str = "json") -> llm.AgentModel:
        m = llm.select(model_mode, TRACES / f"{name}.json", reply=reply)
        planner.set_model(m)
        made.append(m)
        return m
    yield make
    for m in made:
        if isinstance(m, llm.LLMPlanner) and hasattr(m.llm, "assert_done"):
            m.llm.assert_done()
