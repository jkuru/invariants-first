"""Tool arguments: read with their types checked, and every path they name confined to the server's
--root (symlinks followed), with the pyproject.toml settings a rule file brings along."""
from __future__ import annotations

import pathlib
from typing import Any

RULE_FILES = ("invariants.yaml", "invariants.yml", "invariants.json")


class ToolError(Exception):
    """A tool failed for a reason the agent should read (reported with isError: true)."""


# --- the project --------------------------------------------------------------------------------
# Every path a tool argument names (root, rules, source_roots, graphify) resolves inside the server's
# --root, after following symlinks: `check` and `status` import the code they find, so a path outside
# would run code the person who started the server never pointed it at.
def _inside(server_root: pathlib.Path, base: pathlib.Path, given: str, what: str) -> pathlib.Path:
    """base / given, resolved (symlinks followed); ToolError unless it is inside server_root."""
    path = (base / given).resolve()
    if not path.is_relative_to(server_root):
        raise ToolError(f"{what} {given!r} resolves to {path}, outside the server's --root {server_root}; "
                        f"a tool reads only inside it (start inv-mcp with a wider --root to reach it)")
    return path


def _opt_str(args: dict[str, Any], key: str) -> str | None:
    """A string argument, None when absent, null or empty; ToolError for any other type (an argument
    of the wrong type is never read as absent, which would fall back to a default path)."""
    v = args.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ToolError(f"{key} must be a string, not {type(v).__name__}")
    return v or None


def _opt_bool(args: dict[str, Any], key: str) -> bool:
    """A boolean argument, False when absent or null; ToolError for any other type."""
    v = args.get(key)
    if v is None:
        return False
    if not isinstance(v, bool):
        raise ToolError(f"{key} must be a boolean, not {type(v).__name__}")
    return v


def _root(server_root: pathlib.Path, args: dict[str, Any]) -> pathlib.Path:
    given = _opt_str(args, "root")
    root = _inside(server_root, server_root, given, "root") if given else server_root
    if root == pathlib.Path(root.anchor):
        raise ToolError(f"project root {root} is the filesystem root; start inv-mcp with --root at a project")
    if not root.is_dir():
        raise ToolError(f"project root {root} is not a directory")
    return root


def _rule_file(root: pathlib.Path, args: dict[str, Any], default: pathlib.Path | None,
               server_root: pathlib.Path) -> pathlib.Path:
    given = _opt_str(args, "rules")
    if given:
        path = _inside(server_root, root, given, "rules")
    elif default is not None and not _opt_str(args, "root"):
        path = default                                  # the server's own --rules: its operator chose it
    else:
        found = next((root / n for n in RULE_FILES if (root / n).is_file()), root / RULE_FILES[0])
        path = _inside(server_root, root, found.name, "rule file")
    if not path.is_file():
        raise ToolError(f"no rule file at {path}; run propose_rules to draft one (a person writes it)")
    _settings_inside(server_root, path)
    return path


def _strs(args: dict[str, Any], key: str) -> list[str]:
    v = args.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ToolError(f"{key} must be a list of strings")
    return list(v)


def _source_roots(server_root: pathlib.Path, root: pathlib.Path, args: dict[str, Any]) -> list[str]:
    """The source_roots argument, each resolved inside the server's --root."""
    return [str(_inside(server_root, root, r, "source root")) for r in _strs(args, "source_roots")]


def _settings_inside(server_root: pathlib.Path, rule_file: pathlib.Path) -> None:
    """The directories pyproject.toml names for scanning or importing ([tool.invariants.scan] roots,
    setuptools packages.find.where) stay inside the server's --root, like a source_roots argument:
    `scan` reads them and `check` puts them on sys.path. A rule file outside --root is the operator's
    own --rules, and its settings are theirs."""
    from .. import cli
    if not rule_file.resolve().is_relative_to(server_root):
        return
    seen: set[pathlib.Path] = set()
    for proj in (rule_file.resolve().parent, cli.project_root(rule_file)):
        py = proj / "pyproject.toml"
        if proj in seen or not py.is_file():
            continue
        seen.add(proj)
        if not py.resolve().is_relative_to(server_root):     # a symlink to a file outside --root
            raise ToolError(f"{py} resolves to {py.resolve()}, outside the server's --root {server_root}; a "
                            f"tool reads only inside it (start inv-mcp with a wider --root)")
        for d, what in ((proj, "the project root (nearest pyproject.toml)"), (proj / "src", "the default src/")):
            if d.exists() and not d.resolve().is_relative_to(server_root):
                raise ToolError(f"{what} {d} resolves to {d.resolve()}, outside the server's --root {server_root}; "
                                f"`check` and `status` import from it (start inv-mcp with a wider --root)")
        try:
            tool = cli._pyproject(proj).get("tool") or {}
        except cli.Usage as e:
            raise ToolError(str(e)) from e
        find = ((tool.get("setuptools") or {}).get("packages") or {})
        find = (find.get("find") or {}) if isinstance(find, dict) else {}
        for setting, values in (("[tool.invariants.scan] roots", ((tool.get("invariants") or {}).get("scan") or {})
                                 .get("roots")), ("[tool.setuptools.packages.find] where", find.get("where"))):
            for r in cli._str_list(values):
                path = (proj / r).resolve()
                if not path.is_relative_to(server_root):
                    raise ToolError(f"{py}: {setting} names {r!r}, which resolves to {path}, outside the "
                                    f"server's --root {server_root}; a tool reads and imports only inside it "
                                    f"(start inv-mcp with a wider --root, or change the setting)")


def _rel(path: str | None, root: pathlib.Path) -> str | None:
    if not path:
        return path
    try:
        return pathlib.Path(path).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return path
