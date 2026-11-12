"""Fixtures: the rules loaded once, a fresh ledger per test, and a fresh fake model."""
from __future__ import annotations

import pathlib
from typing import Iterator

import pytest

import invariants as inv
from support_agent import context, fakestripe, planner
from support_agent.fake_model import FakeAgentModel

APP_ROOT = pathlib.Path(__file__).resolve().parents[1]


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
