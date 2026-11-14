"""`inv hook`: the pretool and stop hooks for AI coding agents (shell-write parsing, brace and ANSI-C
expansion, computed targets, weakening switches)."""
from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib
import re
import sys
from typing import Any, Iterator

from .. import lock
from ..scan.contracts import SKIP_DIRS
from .commands import cmd_check
from .common import ALLOW_WEAKENING_ENV, DEFAULT_FILES, OK, _captured, _err
from .project import scan_inputs


WEAKENING_FLAGS = ("--accept-weakening", "--allow-weakening")
WEAKENING_SWITCHES = (*WEAKENING_FLAGS, ALLOW_WEAKENING_ENV)

PROTECTED = (lock.LOCK_NAME, "pyproject.toml")
# what a shell command may do with a protected file and still pass the hook: read it
READ_COMMANDS = frozenset({"cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "jq", "diff",
                           "ls", "stat", "cmp", "cd", "pushd", "popd", "pwd", "echo", "printf", "true", "false",
                           "test", "[", "which", "sha256sum", "shasum", "md5", "md5sum", "file"})
READ_GIT = frozenset({"diff", "show", "log", "status", "blame", "add"})   # add stages the file as it is
READ_INV = frozenset({"check", "status", "scan", "validate", "verify"})
GREP_COMMANDS = frozenset({"grep", "egrep", "fgrep", "rg"})              # their -o is --only-matching
# commands that write the files they name: with a computed name ($f) under the lock's directory, blocked
WRITE_COMMANDS = frozenset({"cp", "mv", "tee", "dd", "rm", "ln", "install", "rsync", "unlink", "truncate", "shred",
                            "touch", "sort", "patch", "sed", "gsed", "perl", "ruby", "awk", "gawk", "ditto", "scp",
                            "curl", "wget", "tar", "unzip", "chmod", "chown"})
# a command that runs other code the hook cannot read: blocked when it mentions the lock or pyproject
OPAQUE = re.compile(r"\$\(|`|\$\{|<\(|>\(|\beval\b|\bpython[0-9.]*\s+-[c\s]|\bpython[0-9.]*\s+-$"
                    r"|\b(?:ba|z|da|k)?sh\s+-c\b|\bxargs\b|\b(?:perl|ruby|node)\s+-e\b|\bGIT_[A-Z_]*=|\bgit\s+config\b")
SUSPECT = re.compile(r"lock|pyproject|toml|invariants", re.IGNORECASE)
_GLOB = re.compile(r"[*?\[{]")
_BRACE_LIMIT = 256


def _brace_alternatives(body: str) -> list[str] | None:
    """What the inside of one {...} expands to: its top-level comma alternatives or an a..b range;
    None when the braces stay literal."""
    parts: list[str] = []
    depth, cur = 0, ""
    for c in body:
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        depth += (c == "{") - (c == "}")
        cur += c
    if parts:
        return [*parts, cur]
    m = re.fullmatch(r"(-?\d+)\.\.(-?\d+)(?:\.\.(-?\d+))?", body)
    if m:
        a, b, step = int(m[1]), int(m[2]), abs(int(m[3] or 1)) or 1
        if abs(b - a) // step > _BRACE_LIMIT:
            return None
        return [str(i) for i in range(a, b + (1 if b >= a else -1), step if b >= a else -step)]
    m = re.fullmatch(r"(.)\.\.(.)", body)
    if m:
        lo, hi = sorted((ord(m[1]), ord(m[2])))
        return [chr(i) for i in range(lo, hi + 1)] if hi - lo <= _BRACE_LIMIT else None
    return None


def _expand_braces(word: str) -> list[str] | None:
    """The words a shell brace expansion makes of one word (invariants{,}.lock -> two, x{1..3} ->
    three); None when that is more than the hook will list."""
    todo, done = [word], []
    while todo:
        w = todo.pop()
        expanded = False
        start = 0
        while not expanded and (i := w.find("{", start)) >= 0:
            depth, j = 0, i
            for j in range(i, len(w)):
                depth += (w[j] == "{") - (w[j] == "}")
                if depth == 0:
                    break
            if depth:
                break                               # no closing brace: literal from here on
            alts = _brace_alternatives(w[i + 1:j])
            if alts is None:
                start = i + 1                       # a literal {...}: look for the next one
                continue
            todo.extend(w[:i] + a + w[j + 1:] for a in alts)
            expanded = True
        if not expanded:
            done.append(w)
        if len(todo) + len(done) > _BRACE_LIMIT:
            return None
    return done


def _ansi_c(command: str) -> str:
    """The command with each $'...' (bash's ANSI-C quoting, $'\\x69nvariants.lock') decoded into a
    plain single-quoted word."""
    import codecs
    import shlex

    def decode(m: re.Match[str]) -> str:
        body = re.sub(r"\\c(.)", lambda c: chr(ord(c[1].upper()) ^ 64), m[1])
        body = re.sub(r"\\([0-7]{1,3})", lambda o: f"\\x{int(o[1], 8) & 0xFF:02x}", body)
        body = re.sub(r"\\([eE])", r"\\x1b", body)
        body = re.sub(r"\\(['\"?])", r"\1", body)
        try:
            text = codecs.decode(body.encode("latin-1", "backslashreplace"), "unicode_escape")
        except (UnicodeDecodeError, ValueError):
            text = body
        return shlex.quote(text)
    return re.sub(r"\$'((?:[^'\\]|\\.)*)'", decode, command)


def _mentions(command: str, name: str) -> bool:
    """The text names the file, or a glob that could match it (invariants.l*, pyproject.*), in any
    letter case (a case-insensitive filesystem opens INVARIANTS.LOCK as the lock) and after brace
    expansion (invariants{,}.lock)."""
    text, stem = command.casefold(), name.partition(".")[0]
    if name in text or re.search(re.escape(stem) + r"\.\w*[*?\[{]", text) is not None:
        return True
    for chunk in re.findall(r"\S*\{\S*", text):
        words = _expand_braces(chunk)
        if words is None:
            if stem in chunk:
                return True                         # too many to list: any word with the stem
        elif words != [chunk] and any(_mentions(w, name) for w in words):
            return True
    return False


def _names(word: str, name: str) -> bool:
    """A shell word (unquoted) that may name the file: the name, a glob matching it, or text holding it,
    in any letter case and after brace expansion."""
    import fnmatch
    folded = word.casefold()
    if _mentions(folded, name):
        return True
    for w in _expand_braces(folded) or [folded]:
        base = w.rstrip("/").rsplit("/", 1)[-1]
        if bool(_GLOB.search(base)) and fnmatch.fnmatchcase(name, base):
            return True
    return False


def _tokens(command: str) -> list[str] | None:
    """The command as shell words and operators (quotes removed), or None when it does not parse."""
    import shlex
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _segments(tokens: list[str]) -> list[tuple[list[str], list[tuple[str, str]]]]:
    """Simple commands: their words and their (redirect operator, target) pairs."""
    out: list[tuple[list[str], list[tuple[str, str]]]] = []
    words: list[str] = []
    redirects: list[tuple[str, str]] = []
    it = iter(tokens)
    for t in it:
        if t and all(c in ";|&()" for c in t):
            out.append((words, redirects))
            words, redirects = [], []
        elif t and all(c in "<>&|" for c in t) and ("<" in t or ">" in t):
            redirects.append((t, next(it, "")))
        else:
            words.append(t)
    out.append((words, redirects))
    return [s for s in out if s[0] or s[1]]


def _writes_redirect(redirects: list[tuple[str, str]]) -> bool:
    for op, target in redirects:
        if ">" not in op or target == "/dev/null" or (op.endswith("&") and target.isdigit()):
            continue
        return True
    return False


def _reads_only(words: list[str], redirects: list[tuple[str, str]]) -> bool:
    """A simple command that only reads files: no redirect into a file, no --output, a reading command."""
    if _writes_redirect(redirects):
        return False
    assigned: list[str] = []
    while words and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0])
                     or (words[0] == "env" and len(words) > 1)
                     or (assigned and words[0].startswith("-") and "env" in assigned)):
        assigned.append("env" if words[0] == "env" else words[0].partition("=")[0])
        words = words[1:]                           # VAR=value (or env VAR=value) before the command
    if not words:
        return True                                 # only assignments: they write no file
    cmd, rest = pathlib.PurePath(words[0]).name, words[1:]
    if any(w == "--output" or w.startswith("--output=") or (w == "-o" and cmd not in GREP_COMMANDS)
           for w in rest):
        return False                                # writes the file it names
    if cmd in READ_COMMANDS:
        return not (cmd == "rg" and any(w.partition("=")[0] in ("--pre", "--pre-glob") for w in rest))
    if cmd == "git":
        if assigned:
            return False                            # GIT_EXTERNAL_DIFF=..., GIT_PAGER=... run code
        while rest[:1] == ["-C"] or rest[:1] == ["--no-pager"]:
            rest = rest[2:] if rest[0] == "-C" else rest[1:]
        return bool(rest) and rest[0] in READ_GIT   # -c, -p and other options are not read-only
    if cmd == "inv" and rest:
        return rest[0] in READ_INV or (rest[0] == "lock" and "--check" in rest)
    return False


def _computed_target(words: list[str], redirects: list[tuple[str, str]]) -> bool:
    """A simple command that writes a file whose name the shell computes ($f, `...`): a redirect, or
    a write-capable command with $ or a backtick in a path it writes."""
    if any(">" in op and any(c in t.rstrip("/").rsplit("/", 1)[-1] for c in "$`") for op, t in redirects):
        return True
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words = words[1:]
    while words and words[0] in ("env", "sudo", "command", "exec", "nice", "nohup", "time", "builtin"):
        words = words[1:]
        while words and (words[0].startswith("-") or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0])):
            words = words[1:]
    if not words:
        return False
    cmd, args = pathlib.PurePath(words[0]).name, words[1:]
    if cmd not in WRITE_COMMANDS:
        return False
    if cmd in ("sed", "gsed", "perl", "ruby") and not any(a.startswith("-i") or a == "--in-place" for a in args):
        return False
    paths = [a for a in args if not a.startswith("-") or "=" in a]
    if cmd in ("sed", "gsed", "perl", "ruby", "awk", "gawk") and paths and not any(
            a in ("-e", "-f", "--expression", "--file") for a in args):
        paths = paths[1:]                           # the script, not a path
    bases = [a.rstrip("/").rsplit("/", 1)[-1] for a in paths]    # "$TMPDIR/out.txt" names out.txt
    return any("$" in b or "`" in b for b in bases)


def _lock_dir(cwd: str | None) -> bool:
    """The directory, or one above it, holds invariants.lock."""
    if not cwd:
        return False
    here = pathlib.Path(cwd)
    return any((d / lock.LOCK_NAME).exists() for d in (here, *here.parents))


def _shell_writes(command: str, cwd: str | None = None) -> str | None:
    """The protected file a shell command may write: one it names (quoted, escaped, as a glob, in any
    letter case, behind a brace expansion or $'...') in a command that is not clearly read-only. A
    command the hook cannot read (it does not parse, runs code from a string, or expands a name) is
    blocked when it mentions the lock or pyproject, and, under a directory that holds the lock, so is
    a write to a file whose name the shell computes ($f, a backtick)."""
    command = _ansi_c(command)
    lowered = command.lower()
    named = [n for n, word in zip(PROTECTED, ("lock", "pyproject")) if word in lowered]
    tokens = _tokens(command)
    if tokens is None or OPAQUE.search(command):
        hit = next((n for n in PROTECTED if _mentions(command, n)), None) or (named[0] if named else None)
        if hit:
            return hit
        if tokens is None:
            return None
    segments = _segments(tokens or [])
    for name in PROTECTED:
        for words, redirects in segments:
            texts = [*words, *(t for _, t in redirects)]
            if any(_names(w, name) for w in texts) and not _reads_only(words, redirects):
                return name
    if SUSPECT.search(command):
        for words, redirects in segments:
            texts = [*words, *(t for _, t in redirects)]
            if any("$" in w for w in texts) and not _reads_only(words, redirects):
                return named[0] if named else lock.LOCK_NAME
    if _lock_dir(cwd) and any(_computed_target(words, redirects) for words, redirects in segments):
        return lock.LOCK_NAME
    return None


HOOK_CONFIG = ("settings.json", "settings.local.json")      # in a .claude directory: the hooks' own config
_WRAPPERS = ("env", "sudo", "command", "exec", "nice", "nohup", "time", "builtin")
_UV_VALUE_OPTIONS = frozenset({"--with", "--with-editable", "--with-requirements", "--python", "-p", "--project",
                               "--directory", "--package", "--extra", "--group", "--env-file", "--index",
                               "--default-index", "--index-url", "--extra-index-url", "--cache-dir", "--config-file"})
_SCRIPT_MAX = 2_000_000
_IMPORT_MAX = 400                                # project files read behind one Python run
_PYTEST = frozenset({"pytest", "py.test"})
_WALK_MAX = 20_000


def _hook_config(path: pathlib.Path) -> bool:
    """path is .claude/settings.json or .claude/settings.local.json (any letter case): Claude Code's
    project (or user) settings, where the hooks themselves are configured."""
    return path.parent.name.casefold() == ".claude" and path.name.casefold() in HOOK_CONFIG


def _shell_hook_config(command: str) -> bool:
    """A shell command that may write a .claude settings file (or the .claude directory): it names one
    (or the directory, or a glob in it) and is not clearly read-only, or the hook cannot read it."""
    command = _ansi_c(command)
    if ".claude" not in command.casefold():
        return False
    tokens = _tokens(command)
    if tokens is None or OPAQUE.search(command):
        return "settings" in command.casefold()
    for words, redirects in _segments(tokens):
        for w in [*words, *(t for _, t in redirects)]:
            folded = w.casefold().replace("\\", "/")
            i = folded.find(".claude")
            if i < 0:
                continue
            rest = folded[i + len(".claude"):].strip("/")
            if (not rest or any(_names(rest, n) for n in HOOK_CONFIG)) and not _reads_only(words, redirects):
                return True
    return False


def _command_words(words: list[str]) -> list[str]:
    """The words of a simple command from the command itself: leading VAR=value assignments and
    wrappers (env, sudo, nohup, ...) with their options dropped."""
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words = words[1:]
    while words and pathlib.PurePath(words[0]).name in _WRAPPERS:
        words = words[1:]
        while words and (words[0].startswith("-") or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0])):
            words = words[1:]
    return words


def _is_python(word: str) -> bool:
    return re.fullmatch(r"python[0-9.]*", pathlib.PurePath(word).name) is not None


def _python_scripts(words: list[str], redirects: list[tuple[str, str]], cwd: str) -> list[pathlib.Path]:
    """The files a simple command runs as Python: `python[3] FILE`, `python -m MOD` (FILE under cwd),
    `python - < FILE`, `uv run [python] FILE`, or `./FILE.py` run directly."""
    return _python_run(words, redirects, cwd)[0]


def _python_run(words: list[str], redirects: list[tuple[str, str]],
                cwd: str) -> tuple[list[pathlib.Path], str | None, list[str] | None]:
    """(the files a simple command runs as Python, the module of `python -m MOD`, the arguments of a
    pytest run: `pytest`, `py.test`, `python -m pytest`, `uv run pytest`; None when it is not one)."""
    words = _command_words(words)
    if not words:
        return [], None, None
    names: list[str] = []
    if pathlib.PurePath(words[0]).name == "uv" and words[1:2] == ["run"]:
        rest = words[2:]
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in _UV_VALUE_OPTIONS else rest[1:]
        if rest and pathlib.PurePath(rest[0]).name in _PYTEST:
            return [], None, rest[1:]
        if rest and not _is_python(rest[0]):
            return ([pathlib.Path(cwd, rest[0])] if rest[0].endswith(".py") else []), None, None
        words = rest
    if not words:
        return [], None, None
    if pathlib.PurePath(words[0]).name in _PYTEST:
        return [], None, words[1:]
    if words[0].endswith(".py"):
        return [pathlib.Path(cwd, words[0])], None, None
    if not _is_python(words[0]):
        return [], None, None
    args = words[1:]
    while args:
        a = args.pop(0)
        if a == "-m" and args:
            if args[0] == "pytest":
                return [], None, args[1:]
            mod = pathlib.Path(cwd, *args[0].split("."))
            return [mod.with_suffix(".py"), mod / "__main__.py"], args[0], None
        if a in ("-W", "-X"):
            args = args[1:]
        elif a == "-":
            break
        elif not a.startswith("-"):
            names.append(a)
            break
    if not names:                                   # `python - < FILE`, `python < FILE`
        names = [t for op, t in redirects if op == "<"]
    return [pathlib.Path(cwd, n) for n in names], None, None


def _script_rewrites(path: pathlib.Path) -> str | None:
    """The protected file a Python script inside a directory tree that holds the lock names in its text
    (so may rewrite): the lock, or pyproject.toml. None for a script outside such a tree, or one that
    names neither."""
    try:
        p = pathlib.Path(os.path.realpath(path))
        if not p.is_file() or not _lock_dir(str(p.parent)):
            return None
        with p.open("rb") as fh:
            text = fh.read(_SCRIPT_MAX).decode("utf-8", "replace").casefold()
    except OSError:
        return None
    if lock.LOCK_NAME in text or "lock_name" in text:
        return lock.LOCK_NAME
    return "pyproject.toml" if "pyproject" in text else None


def _lock_tree(d: pathlib.Path) -> pathlib.Path | None:
    """The nearest directory, d or one above it, that holds invariants.lock."""
    return next((x for x in (d, *d.parents) if (x / lock.LOCK_NAME).exists()), None)


def _module_rewrites(text: str) -> str | None:
    """The protected file a module a script imports (or a conftest.py) names in its text (casefolded):
    the lock (or LOCK_NAME), or pyproject.toml together with the settings in it the hook guards
    ([tool.invariants], [tool.setuptools]). Narrower than a script's own text, where pyproject alone
    counts: a library module that reads pyproject.toml is common, one that edits those tables is not."""
    if lock.LOCK_NAME in text or "lock_name" in text:
        return lock.LOCK_NAME
    return "pyproject.toml" if "pyproject" in text and ("invariants" in text or "setuptools" in text) else None


def _imports(tree: ast.AST, file: pathlib.Path) -> Iterator[tuple[list[pathlib.Path] | None, str]]:
    """(the directory a relative import starts from, else None; the dotted name) for each import in a
    module: `import a.b`, `from a import b` (b may be a module), `from . import x`, a constant
    importlib.import_module("a.b") or __import__("a"), and a conftest's pytest_plugins strings."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((None, a.name) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            start = None
            if node.level:
                d = file.parent
                for _ in range(node.level - 1):
                    d = d.parent
                start = [d]
            mod = node.module or ""
            if mod:
                yield start, mod
            yield from ((start, f"{mod}.{a.name}" if mod else a.name) for a in node.names if a.name != "*")
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str) and (
                    (isinstance(node.func, ast.Name) and node.func.id in ("__import__", "import_module"))
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")):
            yield None, node.args[0].value
        elif isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id == "pytest_plugins"
                                                  for x in node.targets):
            vals = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            yield from ((None, v.value) for v in vals if isinstance(v, ast.Constant) and isinstance(v.value, str))


def _module_files(name: str, bases: list[pathlib.Path]) -> list[pathlib.Path]:
    """The files an import of name runs, from the first base holding its top-level package: each
    package's __init__.py on the way, then the module (a.py, or a/__init__.py)."""
    parts = [x for x in name.split(".") if x]
    if not parts or not all(x.isidentifier() for x in parts):
        return []
    for base in bases:
        if not ((base / parts[0]).is_dir() or (base / f"{parts[0]}.py").is_file()):
            continue
        out = [f for i in range(1, len(parts)) if (f := base.joinpath(*parts[:i], "__init__.py")).is_file()]
        mod = base.joinpath(*parts)
        out += [f for f in (mod.with_suffix(".py"), mod / "__init__.py") if f.is_file()]
        return out
    return []


def _conftests(cwd: pathlib.Path, tree: pathlib.Path, args: list[str]) -> list[pathlib.Path]:
    """The conftest.py files a pytest run under tree may load: in cwd and each directory above it up to
    tree, and under cwd and the directories the arguments name (hidden and always-skipped directories
    left out; the walk is bounded)."""
    out = [d / "conftest.py" for d in (cwd, *cwd.parents) if d.is_relative_to(tree)]
    starts = [cwd, *(p for a in args if not a.startswith("-")
                     and (p := pathlib.Path(cwd, a.split("::")[0])).is_dir())]
    seen = 0
    for start in starts:
        for top, dirs, files in os.walk(start):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS)
            if "conftest.py" in files:
                out.append(pathlib.Path(top, "conftest.py"))
            seen += len(files) + len(dirs)
            if seen > _WALK_MAX:
                break
    return [f for f in dict.fromkeys(out) if f.is_file()]


def _run_rewrites(words: list[str], redirects: list[tuple[str, str]], cwd: str) -> tuple[str, str] | None:
    """(protected file, the project file that names it) for a Python run inside a tree that holds the
    lock: one of the project-local modules the script (or `python -m MOD`) imports, transitively, or,
    for pytest, a conftest.py, a `-p` plugin, or a module one of them imports. A module is
    project-local when it is found in that tree (from the script's directory, cwd, the tree or its
    src/; a relative import that climbs above the tree is not followed); a symlinked one is read where
    it points. Bounded at _IMPORT_MAX files; test modules themselves are not read (a test that merely
    mentions the lock is ordinary)."""
    files, module, pytest_args = _python_run(words, redirects, cwd)
    here = pathlib.Path(os.path.realpath(cwd))
    names: list[str] = []
    queue: list[tuple[pathlib.Path, bool]] = []    # (file, it is the script itself)
    if pytest_args is not None:
        tree = _lock_tree(here)
        if tree is None:
            return None
        queue += [(f, False) for f in _conftests(here, tree, pytest_args)]
        names += [b for a, b in zip(pytest_args, pytest_args[1:]) if a == "-p" and not b.startswith("no:")]
    else:
        found = [pathlib.Path(os.path.realpath(f)) for f in files if f.is_file()]
        if not found and not module:
            return None
        tree = _lock_tree(found[0].parent if found else here)   # `-m MOD` may live in src/, not under cwd
        if tree is None:
            return None
        queue += [(f, True) for f in found]
        if module:
            names.append(module)                      # its packages' __init__.py run first
    bases = list(dict.fromkeys([*(pathlib.Path(os.path.realpath(f.parent)) for f in files), here, tree,
                                tree / "src"]))
    queue += [(f, False) for n in names for f in _module_files(n, bases)]
    seen: set[pathlib.Path] = set()
    while queue and len(seen) < _IMPORT_MAX:
        at, script = queue.pop(0)
        f = pathlib.Path(os.path.realpath(at))         # a module found in the tree is read where it points
        if f in seen or not pathlib.Path(os.path.abspath(at)).is_relative_to(tree):
            continue
        seen.add(f)
        try:
            with f.open("rb") as fh:
                raw = fh.read(_SCRIPT_MAX)
        except OSError:
            continue
        hit = None if script else _module_rewrites(raw.decode("utf-8", "replace").casefold())
        if hit:                                       # (a script's own text is _script_rewrites' to read)
            return hit, os.path.relpath(f, here)
        try:
            parsed = ast.parse(raw)
        except (SyntaxError, ValueError):
            continue
        for start, name in _imports(parsed, f):
            queue += [(m, False) for m in _module_files(name, start or [f.parent, *bases])]
    return None


def _tree_holds(d: pathlib.Path) -> str | None:
    """The protected file a directory holds, at any depth (.git skipped); the lock as well when the
    tree is larger than the hook will walk."""
    seen = 0
    for top, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x != ".git"]
        folded = {f.casefold() for f in files}
        hit = next((n for n in PROTECTED if n in folded), None)
        if hit:
            return hit
        seen += len(files) + len(dirs)
        if seen > _WALK_MAX:
            return lock.LOCK_NAME
    return None


def _pathspec_hits(spec: str, base: str) -> str | None:
    """The protected file a git pathspec may match without naming it: a magic one (`:/`,
    `:(glob)...`) or a directory ('.', a parent) holding it. One that names it, or a glob matching
    it, is a write that names the file, which _shell_writes blocks before this is asked."""
    if spec.startswith(":"):
        return lock.LOCK_NAME
    p = pathlib.Path(os.path.realpath(os.path.join(base, spec)))
    return _tree_holds(p) if p.is_dir() else None


def _stash_touches(base: str, ref: str | None) -> str | None:
    """The protected file a stash entry changes (git stash show --name-only, untracked files included
    when this git can list them); None when git cannot tell (git stash pop then fails as well)."""
    import subprocess
    for extra in (["--include-untracked"], []):
        try:
            p = subprocess.run(["git", "stash", "show", "--name-only", *extra, *([ref] if ref else [])],
                               cwd=base, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        if p.returncode == 0:
            names = {pathlib.PurePath(n).name.casefold() for n in p.stdout.splitlines()}
            return next((n for n in PROTECTED if n in names), None)
    return None


def _patch_touches(paths: list[str], base: str) -> str | None:
    """The protected file a patch may change: one its text names; the lock when there is no patch file
    to read (stdin) or it cannot be read."""
    if not paths or "-" in paths:
        return lock.LOCK_NAME
    for name in paths:
        try:
            text = pathlib.Path(base, name).read_text(encoding="utf-8", errors="replace").casefold()
        except OSError:
            return lock.LOCK_NAME
        hit = next((n for n in PROTECTED if n in text), None)
        if hit:
            return hit
    return None


def _git_rewrites(words: list[str], redirects: list[tuple[str, str]], cwd: str) -> str | None:
    """The protected file a git command may rewrite from somewhere the hook cannot see: `git apply` or
    `git am` of a patch naming it, `git stash pop|apply` of an entry changing it, and
    `git checkout REF [--] PATHS` or `git restore --source REF PATHS` with a pathspec that may match it."""
    words = _command_words(words)
    if not words or pathlib.PurePath(words[0]).name != "git":
        return None
    rest, base = words[1:], cwd
    while rest and rest[0].startswith("-"):
        opt = rest.pop(0)
        if opt in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") and rest:
            value = rest.pop(0)
            if opt == "-C":
                base = os.path.join(base, value)
    if not rest:
        return None
    sub, args = rest[0], rest[1:]
    if sub in ("apply", "am"):
        if sub == "apply" and any(a in ("--check", "--stat", "--numstat", "--summary") for a in args) \
                and "--apply" not in args:
            return None                             # reports on the patch, applies nothing
        files = [a for a in args if not a.startswith("-") or a == "-"]
        files = files or [t for op, t in redirects if op == "<"]
        return _patch_touches(files, base)
    if sub == "stash" and args[:1] and args[0] in ("pop", "apply"):
        ref = next((a for a in args[1:] if not a.startswith("-")), None)
        return _stash_touches(base, ref)
    if any(a == "--pathspec-from-file" or a.startswith("--pathspec-from-file=") for a in args):
        return lock.LOCK_NAME if sub in ("checkout", "restore") else None
    specs: list[str] = []
    if sub == "checkout":
        if "--" in args:
            i = args.index("--")
            if [a for a in args[:i] if not a.startswith("-")]:
                specs = args[i + 1:]                # REF -- PATHS
        else:
            plain = [a for a in args if not a.startswith("-")]
            specs = plain[1:]                       # REF PATHS
    elif sub == "restore":
        sourced = any(a in ("-s", "--source") or a.startswith("--source=") or (a.startswith("-s") and a[2:3].isalnum())
                      for a in args)
        if sourced:
            out, skip = [], False
            for a in args:
                if skip:
                    skip = False
                elif a in ("-s", "--source"):
                    skip = True
                elif not a.startswith("-"):
                    out.append(a)
            specs = out
    return next((h for s in specs if (h := _pathspec_hits(s, base))), None)


def _indirect_write(command: str, cwd: str) -> tuple[str, str, str | None] | None:
    """(protected file, the command, the project file it runs that names it, else None) for a command
    that may rewrite the lock or pyproject.toml without naming it: a Python script of the project whose
    text names it (or a project module it imports, or a conftest.py pytest loads), or a git command
    that applies a patch, a stash or another commit's version of it."""
    tokens = _tokens(_ansi_c(command))
    if tokens is None:
        return None
    dirs = [cwd]               # every directory a segment may run in: `cd tools && python ../x.py`, a subshell
    for words, redirects in _segments(tokens):
        if words[:1] in (["cd"], ["pushd"]) and len(words) == 2 and not _computed(words[1]):
            dirs += [d for d in (os.path.normpath(os.path.join(c, os.path.expanduser(words[1]))) for c in dirs)
                     if d not in dirs and os.path.isdir(d)]
            continue
        for here in dirs:
            hit = next((h for s in _python_scripts(words, redirects, here) if (h := _script_rewrites(s))), None)
            hit = hit or _git_rewrites(words, redirects, here)
            if hit:
                return hit, " ".join(words), None
            via = _run_rewrites(words, redirects, here)
            if via:
                return via[0], " ".join(words), via[1]
    return None


def _computed(word: str) -> bool:
    """A shell word whose value the hook cannot know ($VAR, `cmd`, $(cmd))."""
    return "$" in word or "`" in word


def _weakening_switch(command: str) -> str | None:
    """A switch that accepts a weakening: the flags, the environment variable, or an abbreviation of a
    flag (5 characters or more; older argparse took them) the command passes."""
    hit = next((s for s in WEAKENING_SWITCHES if s in command), None)
    if hit:
        return hit
    command = _ansi_c(command)
    runs_inv = re.search(r"\binv\b|invariants", command) is not None
    words = _tokens(command) or command.split()
    for w in words:                                 # after unquoting: env INV_ALLOW_WEAK''ENING=1, export, X=1 cmd
        if any(s in w for s in WEAKENING_SWITCHES):
            return next(s for s in WEAKENING_SWITCHES if s in w)
    for w in words:
        flag = w.partition("=")[0]
        if len(flag) >= 5 and any(f.startswith(flag) for f in WEAKENING_FLAGS) and (flag != "--all" or runs_inv):
            return w
    return None


# words in a pyproject.toml text, or an edit of it, that may name a table the scan settings come from
_SCAN_WORDS = ("invariants", "setuptools")


def _invariants_table(text: str) -> Any:
    """What the scan settings of a pyproject.toml text are derived from (project.scan_inputs: all of
    [tool.invariants] and [tool.setuptools.packages]); the text itself when it does not parse and
    could name one of them."""
    import tomllib
    try:
        return scan_inputs(tomllib.loads(text))
    except tomllib.TOMLDecodeError:
        return text if any(w in text for w in _SCAN_WORDS) else None


def _edited_text(tool_input: dict[str, Any], before: str) -> str | None:
    """The file's text after an Edit, MultiEdit or Write tool call, or None when it cannot be told."""
    if isinstance(tool_input.get("content"), str):
        return str(tool_input["content"])
    given = tool_input.get("edits")
    edits: list[Any] = given if isinstance(given, list) else [tool_input]
    text = before
    for e in edits:
        old, new = (e.get("old_string"), e.get("new_string")) if isinstance(e, dict) else (None, None)
        if not (isinstance(old, str) and isinstance(new, str)):
            return None
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
    return text


def _touches_invariants_table(path: pathlib.Path, tool_input: dict[str, Any]) -> bool:
    """An edit of pyproject.toml that changes what the scan settings are derived from: its
    [tool.invariants] tables (the scan roots and excludes) or [tool.setuptools.packages] (find.where,
    the default source roots when no [tool.invariants.scan] roots are set).

    The file is read under the name given or, when no file has that exact name, under one that matches
    it ignoring case: on a case-insensitive filesystem `PyProject.toml` is pyproject.toml, and judging
    it as an empty file there would miss the edit."""
    try:
        before = path.read_text(encoding="utf-8")
    except OSError:
        before = ""
        try:
            twin = next((p for p in path.parent.iterdir() if p.name.casefold() == path.name.casefold()), None)
            before = twin.read_text(encoding="utf-8") if twin is not None else ""
        except OSError:
            pass
    after = _edited_text(tool_input, before)
    if after is None:
        return any(w in json.dumps(tool_input) for w in _SCAN_WORDS)
    return not lock.same(_invariants_table(before), _invariants_table(after))


def _pretool(event: dict[str, Any]) -> int:
    given = event.get("tool_input")
    tool_input: dict[str, Any] = given if isinstance(given, dict) else {}
    cwd = str(event.get("cwd") or os.getcwd())
    command = str(tool_input.get("command") or "")
    files = [pathlib.Path(os.path.realpath(os.path.join(cwd, str(tool_input[k]))))
             for k in ("file_path", "path", "notebook_path") if tool_input.get(k)]
    if any(_hook_config(f) for f in files) or _shell_hook_config(command):
        _err("invariants: blocked an edit of .claude/settings.json or .claude/settings.local.json, where the "
             "agent hooks (this one included) are configured. Tell the user what you want to change in them "
             "and let them make the edit.")
        return 2
    written = _shell_writes(command, cwd)
    indirect = None if written else _indirect_write(command, cwd)
    if indirect:
        name, how, via = indirect
        through = f" (through {via}, a project file it imports or loads)" if via else ""
        _err(f"invariants: blocked `{how}`{through}: it can rewrite {name} without naming it (a project script whose "
             f"text names it, or a patch, stash or other commit's copy). The lock is written only by "
             f"`inv lock` or `inv check --relock`, which refuse a weakening, and the scan settings in "
             f"pyproject.toml are the user's to edit; run those or tell the user.")
        return 2
    lock_name, pyproject = lock.LOCK_NAME.casefold(), "pyproject.toml"   # a case-insensitive filesystem
    if any(f.name.casefold() == lock_name for f in files) or written == lock.LOCK_NAME:
        _err(f"invariants: blocked a direct edit of {lock.LOCK_NAME}. The lock is written only by "
             f"`inv lock` or `inv check --relock`, which refuse a weakening; edit invariants.yaml or the "
             f"code, then run one of them.")
        return 2
    if written or any(f.name.casefold() == pyproject and _touches_invariants_table(f, tool_input) for f in files):
        _err("invariants: blocked an edit of the scan settings in pyproject.toml ([tool.invariants], or "
             "[tool.setuptools.packages] whose find.where gives the default scan roots), or a shell command "
             "that writes pyproject.toml. The scan roots and excludes decide which code is checked for back "
             "doors; tell the user what you want to change and let them make the edit.")
        return 2
    hit = _weakening_switch(command)
    if hit:
        _err(f"invariants: blocked `{hit}`. Accepting a weakened rule is a person's decision. Tell the user "
             f"which rule your change weakens and why (run `inv check` to list it), and let them accept it.")
        return 2
    return OK


def cmd_hook(a: argparse.Namespace) -> int:
    """An AI coding agent's hook (Claude Code's JSON on stdin; exit 2 blocks and tells the agent why).

    pretool: refuse a shell command that accepts a weakening (--accept-weakening, --allow-weakening,
             INV_ALLOW_WEAKENING, or a flag abbreviated to 5+ characters), so only a person can; any
             direct edit of invariants.lock (an edit or write tool on it, symlinks resolved, or a
             shell command naming it, quoted or as a glob, that is not clearly read-only, and one the
             hook cannot read that mentions it): only `inv lock` and `inv check --relock` write the
             lock, and they refuse a weakening; and an edit of pyproject.toml's [tool.invariants]
             or [tool.setuptools.packages] tables (the scan settings are derived from them), or a
             shell command that writes pyproject.toml; a command that may rewrite either without
             naming it: running a Python script inside the lock's directory tree whose text names
             the lock (or LOCK_NAME) or pyproject, `git apply`/`git am` of a patch naming one,
             `git stash pop|apply` of an entry that changes one, `git checkout REF -- PATHS` or
             `git restore --source REF PATHS` whose pathspec may match one ('.', a directory
             holding it, a glob); and any edit of .claude/settings.json or
             .claude/settings.local.json, where the hooks themselves are configured.
    stop:    refuse to finish while `inv check` fails, once per stop (stop_hook_active ends the loop).

    The hooks are an early warning against an agent's shortcuts, not the gate: a determined command
    can always get past a text check. `inv check --base` in CI is the gate."""
    event = _event()
    if a.event == "pretool":
        return _pretool(event)
    return _stop(a, event)


def _event() -> dict[str, Any]:
    """The hook's JSON event from stdin; {} when there is none or it is not a JSON object."""
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        event = {}
    return event if isinstance(event, dict) else {}


def _stop(a: argparse.Namespace, event: dict[str, Any]) -> int:
    """The stop hook: exit 2 with `inv check`'s report while it fails, once per stop."""
    if event.get("stop_hook_active"):
        return OK                                   # already sent back once: do not loop
    base = pathlib.Path(str(event.get("cwd") or "."))
    rule = next((base / n for n in DEFAULT_FILES if (base / n).is_file()), None)
    if a.rulefile:
        rule = pathlib.Path(a.rulefile)
    if rule is None:
        return OK
    code, text = _captured(cmd_check, argparse.Namespace(rulefile=str(rule), path=a.path, relock=True,
                                                         fail_on_repin=False, base=None, root=[], exclude=[],
                                                         allow_weakening=False, imports=[]))
    if code == OK:
        return OK
    _err("invariants: `inv check` fails, so this change is not done:")
    _err(text)
    _err("Fix the code so it keeps the rule. If the rule itself must change, say so to the user; do not "
         "accept a weakening yourself.")
    return 2
