"""docs/api.md against the package: the `invariants` tables name exactly invariants.__all__, and each
integration table names only what its module makes public (its __all__, else no leading underscore)."""
from __future__ import annotations

import importlib
import pathlib
import re

import pytest

import invariants

API = pathlib.Path(__file__).resolve().parents[1] / "docs" / "api.md"
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _section(title: str) -> str:
    """The text of one `## ` or `### ` section of api.md, up to the next heading of its level or above."""
    text = API.read_text()
    level = title.split(" ", 1)[0]
    start = text.index(f"\n{title}\n")
    stops = [m.start() for m in re.finditer(r"\n#{1,%d} " % len(level), text) if m.start() > start]
    return text[start:stops[0] if stops else len(text)]


def _documented(section: str) -> set[str]:
    """The names in the first column of the section's Name | Kind | Purpose tables (each backticked
    name, up to its signature)."""
    out: set[str] = set()
    in_names = False
    for line in section.splitlines():
        if line.startswith("| Name | Kind |"):
            in_names = True
        elif not line.startswith("|"):
            in_names = False
        if not (in_names and line.startswith("| `")):
            continue
        first = re.split(r"(?<!\\)\|", line)[1]
        out |= {m.group(0) for code in re.findall(r"`([^`]+)`", first) if (m := NAME.match(code))}
    return out


def test_every_public_name_is_documented_and_every_documented_name_exists() -> None:
    documented = _documented(_section("## `invariants`"))
    public = set(invariants.__all__)
    assert public - documented == set(), "in invariants.__all__ but not in docs/api.md"
    assert documented - public == set(), "in docs/api.md but not in invariants.__all__"


@pytest.mark.parametrize("module", ["fastapi", "asgi", "pydantic", "otel", "exporters", "anthropic"])
def test_integration_tables_name_public_names(module: str) -> None:
    mod = importlib.import_module(f"invariants.integrations.{module}")
    documented = _documented(_section(f"### `invariants.integrations.{module}`"))
    assert documented
    exported = set(getattr(mod, "__all__", None) or (n for n in vars(mod) if not n.startswith("_")))
    for name in documented:
        head = name.split(".")[0]
        assert head in exported and hasattr(mod, head), f"{module}: {name} is documented but not public"


@pytest.mark.parametrize("module, names", [
    ("exporters", {"ON_FULL"}),
    ("pydantic", {"json_fields", "MODEL_ATTR", "closed"}),
    ("anthropic", {"MODES", "DEFAULT_MODEL", "FEEDBACK_TEMPLATE", "TOOL_FEEDBACK_TEMPLATE", "TOOL_CHOICE"}),
])
def test_public_constants_are_documented(module: str, names: set[str]) -> None:
    documented = _documented(_section(f"### `invariants.integrations.{module}`"))
    assert names <= documented, names - documented


@pytest.mark.parametrize("module", ["exporters", "anthropic", "fastapi", "asgi", "otel"])
def test_every_name_of_an_integrations_all_is_documented(module: str) -> None:
    mod = importlib.import_module(f"invariants.integrations.{module}")
    documented = _documented(_section(f"### `invariants.integrations.{module}`"))
    if not hasattr(mod, "__all__"):
        pytest.skip(f"{module} has no __all__")
    assert set(mod.__all__) - documented == set()
