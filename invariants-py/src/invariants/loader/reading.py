"""Reading a rule file from disk (YAML or JSON), and where the bundled JSON Schema lives.

A file's errors name it: a YAML syntax error says `in "invariants.yaml", line 12`, and an error in one
rule, provider or target is prefixed `invariants.yaml:19: ` (the line where that entry starts, when the
file is YAML; the file alone for JSON)."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import pathlib
from typing import Any, Iterator

from ..errors import LoadError
from .short_forms import expand_short_forms

YAML_SUFFIXES = (".yaml", ".yml")


@dataclasses.dataclass
class Where:
    """Where a rule file's entries are: the file, and the YAML line of each rule (by index), provider
    and target (by name). Lines are 1-based; empty for JSON."""
    file: str
    rules: dict[int, int] = dataclasses.field(default_factory=dict)
    providers: dict[str, int] = dataclasses.field(default_factory=dict)
    targets: dict[str, int] = dataclasses.field(default_factory=dict)

    def at(self, line: int | None) -> str:
        """'file:line' (or 'file' when the line is not known)."""
        return f"{self.file}:{line}" if line else self.file

    @contextlib.contextmanager
    def located(self, line: int | None) -> Iterator[None]:
        """Prefix a LoadError raised in the block with the file and line (once)."""
        try:
            yield
        except LoadError as e:
            prefix = self.at(line)
            if str(e).startswith(self.file):
                raise
            raise LoadError(f"{prefix}: {e}") from None    # one traceback; the original stays its context


def read_document(source: str | pathlib.Path) -> dict[str, Any]:
    """Read a rule file from disk: YAML for .yaml/.yml, JSON otherwise."""
    return read_located(source)[0]


def read_located(source: str | pathlib.Path) -> tuple[dict[str, Any], Where]:
    """The rule document (short forms expanded) and where its entries are in the file."""
    path = pathlib.Path(source)
    where = Where(str(source))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise LoadError(f"{source}: {e}") from e
    if path.suffix.lower() in YAML_SUFFIXES:
        import yaml
        try:
            doc = yaml.safe_load(_named(text, str(source)))
            _yaml_lines(yaml.compose(_named(text, str(source)), Loader=yaml.SafeLoader), where)
        except yaml.YAMLError as e:
            raise LoadError(f"{source}: {e}") from e
    else:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            raise LoadError(f"{source}: {e}") from e
    if not isinstance(doc, dict):
        raise LoadError(f"{source}: the rule file must be a mapping")
    rules = doc.get("rules")
    for i, raw in enumerate(rules if isinstance(rules, list) else []):
        with where.located(where.rules.get(i)):
            expand_short_forms({"rules": [raw]})
    return doc, where


def _named(text: str, name: str) -> io.StringIO:
    """A stream PyYAML names in its error marks (it reads the stream's `name`)."""
    s = io.StringIO(text)
    s.name = name
    return s


def _yaml_lines(root: Any, where: Where) -> None:
    """The start line of each rule, provider and target, from the composed YAML node tree."""
    import yaml
    if not isinstance(root, yaml.MappingNode):
        return
    for key, value in root.value:
        section = getattr(key, "value", None)
        if section == "rules" and isinstance(value, yaml.SequenceNode):
            where.rules = {i: n.start_mark.line + 1 for i, n in enumerate(value.value)}
        elif section in ("providers", "targets") and isinstance(value, yaml.MappingNode):
            setattr(where, section, {str(k.value): k.start_mark.line + 1 for k, _ in value.value
                                     if isinstance(k, yaml.ScalarNode)})


def schema_path() -> pathlib.Path:
    """Path to the bundled JSON Schema for rule files."""
    return pathlib.Path(__file__).parent.with_name("schema") / "invariants.schema.json"
