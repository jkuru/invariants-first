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
import re
from typing import Any, Iterator

from ..errors import LoadError
from .short_forms import expand_shared_accepts, expand_short_forms

YAML_SUFFIXES = (".yaml", ".yml")


@dataclasses.dataclass
class Where:
    """Where a rule file's entries are: the file, and the YAML line of each rule (by index), provider
    and target (by name). Lines are 1-based; empty for JSON."""
    file: str
    rules: dict[int, int] = dataclasses.field(default_factory=dict)
    providers: dict[str, int] = dataclasses.field(default_factory=dict)
    targets: dict[str, int] = dataclasses.field(default_factory=dict)
    # the top-level allow_unresolved: the line of its key, and of each entry (by index)
    accepts_key: int | None = None
    accepts: dict[int, int] = dataclasses.field(default_factory=dict)
    # the ids of the rules written in the door:/sink: short form (their allow_unresolved is top-level)
    short_doors: set[str] = dataclasses.field(default_factory=set)

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
            refuse_aliases(text, str(source))
            root = yaml.compose(_named(text, str(source)), Loader=yaml.SafeLoader)
            refuse_duplicate_keys(root, str(source))
            doc = yaml.safe_load(_named(text, str(source)))
            _yaml_lines(root, where)
        except yaml.YAMLError as e:
            raise LoadError(f"{source}: {e}") from e
    else:
        try:
            doc = json.loads(text, object_pairs_hook=_unique_pairs)
        except _Repeated as e:
            raise LoadError(f"{source}: {_json_repeat(text, e.key)}") from None
        except json.JSONDecodeError as e:
            raise LoadError(f"{source}: {e}") from e
    if not isinstance(doc, dict):
        raise LoadError(f"{source}: the rule file must be a mapping")
    rules = doc.get("rules")
    for i, raw in enumerate(rules if isinstance(rules, list) else []):
        if isinstance(raw, dict) and {"door", "sink"} & set(raw) and isinstance(raw.get("id"), str):
            where.short_doors.add(raw["id"])
        with where.located(where.rules.get(i)):
            expand_short_forms({"rules": [raw]})
    try:
        expand_shared_accepts(doc)                     # the top-level allow_unresolved, rule by rule
    except LoadError as e:
        m = re.match(r"allow_unresolved\[(\d+)\]", str(e))
        with where.located(where.accepts.get(int(m[1])) if m else where.accepts_key):
            raise
    return doc, where


class _Repeated(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise _Repeated(k)
        out[k] = v
    return out


def _json_repeat(text: str, key: str) -> str:
    """The message for a JSON key repeated in one object, with the lines that key appears on."""
    pat = re.compile(r'"' + re.escape(json.dumps(key)[1:-1]) + r'"\s*:')
    lines = sorted({text.count("\n", 0, m.start()) + 1 for m in pat.finditer(text)})
    on = f" (lines {', '.join(map(str, lines))})" if len(lines) > 1 else ""
    return (f"the key {key!r} appears twice in one object{on}; JSON keeps only the last one, so write each "
            f"key once")


def _key_ident(k: Any, loader: Any) -> Any:
    """What a scalar key is once read: the value PyYAML builds for it (`1` and `01`, `true` and `yes`,
    `null` and `~`, `1.0` and `1.00` are one key, as they are in the dict it builds), or its tag and
    text when that value cannot key a dict."""
    try:
        v = loader.construct_object(k)
        hash(v)
        return v
    except Exception:
        return (k.tag, k.value)


def refuse_duplicate_keys(root: Any, name: str) -> None:
    """LoadError at a key repeated in one YAML mapping (PyYAML keeps the last value silently, so a
    second `sink:` or `door:` entry with the same key would replace the first unseen), naming both lines.
    Keys are compared as read, not as written: `1` and `01` are the same key."""
    import yaml
    loader = yaml.SafeLoader("")
    work = [root]
    while work:
        node = work.pop()
        if isinstance(node, yaml.MappingNode):
            seen: dict[Any, tuple[int, str]] = {}
            for k, v in node.value:
                if isinstance(k, yaml.ScalarNode):
                    ident = _key_ident(k, loader)
                    line = k.start_mark.line + 1
                    if ident in seen:
                        first, text = seen[ident]
                        same = "" if text == k.value else f", written {text!r} there, which YAML reads as the same key"
                        raise LoadError(f"{name}:{line}: the key {k.value!r} appears twice in one mapping (lines "
                                        f"{first} and {line}{same}); YAML keeps only the last one, so write each "
                                        f"key once")
                    seen[ident] = (line, k.value)
                work.append(k)
                work.append(v)
        elif isinstance(node, yaml.SequenceNode):
            work.extend(node.value)


def refuse_aliases(text: str, name: str) -> None:
    """LoadError at the first YAML anchor (`&a`) or alias (`*a`, a `<<: *a` merge too) in a rule file.
    An alias repeats a node without copying it, so a few lines of nested aliases stand for billions
    of nodes, and every check that walks the document would hang on them; a rule file has no need of
    them. Read from the parser's events, before anything is built (linear in the text)."""
    import yaml
    for ev in yaml.parse(_named(text, name), Loader=yaml.SafeLoader):
        if isinstance(ev, yaml.AliasEvent) or getattr(ev, "anchor", None) is not None:
            kind = "an alias" if isinstance(ev, yaml.AliasEvent) else "an anchor"
            line = ev.start_mark.line + 1 if ev.start_mark is not None else 0
            raise LoadError(f"{name}:{line}: {kind} (YAML &name or *name) is not allowed in a rule file; "
                            f"write the value out where it is used")


EXPANDED_LIMIT = 1_000_000


def check_expanded(doc: Any, name: str, limit: int = EXPANDED_LIMIT) -> None:
    """LoadError when a loaded YAML document (an OpenAPI file, which may use anchors) stands for more
    than `limit` values once its aliases are expanded: counted with a stop at the limit, so a nested
    alias bomb is refused in linear time instead of hanging what walks it."""
    seen, work = 0, [doc]
    while work:
        v = work.pop()
        seen += 1
        if seen > limit:
            raise LoadError(f"{name}: more than {limit} values once its YAML aliases are expanded; not read")
        if isinstance(v, dict):
            work.extend(v.keys())
            work.extend(v.values())
        elif isinstance(v, list):
            work.extend(v)


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
        elif section == "allow_unresolved":
            where.accepts_key = key.start_mark.line + 1
            if isinstance(value, yaml.SequenceNode):
                where.accepts = {i: n.start_mark.line + 1 for i, n in enumerate(value.value)}


def schema_path() -> pathlib.Path:
    """Path to the bundled JSON Schema for rule files."""
    return pathlib.Path(__file__).parent.with_name("schema") / "invariants.schema.json"
