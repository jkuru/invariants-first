"""Reading a rule file from disk (YAML or JSON), and where the bundled JSON Schema lives."""
from __future__ import annotations

import json
import pathlib
from typing import Any

from ..errors import LoadError
from .short_forms import expand_short_forms

YAML_SUFFIXES = (".yaml", ".yml")


def read_document(source: str | pathlib.Path) -> dict[str, Any]:
    """Read a rule file from disk: YAML for .yaml/.yml, JSON otherwise."""
    path = pathlib.Path(source)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise LoadError(f"{source}: {e}") from e
    if path.suffix.lower() in YAML_SUFFIXES:
        import yaml
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise LoadError(f"{source}: {e}") from e
    else:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            raise LoadError(f"{source}: {e}") from e
    if not isinstance(doc, dict):
        raise LoadError(f"{source}: the rule file must be a mapping")
    return expand_short_forms(doc)


def schema_path() -> pathlib.Path:
    """Path to the bundled JSON Schema for rule files."""
    return pathlib.Path(__file__).parent.with_name("schema") / "invariants.schema.json"
