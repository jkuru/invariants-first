"""`inv hook`: the pretool and stop hooks for AI coding agents (shell-write parsing, brace and ANSI-C
expansion, computed targets, weakening switches)."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from typing import Any

from .. import lockfile
from .commands import cmd_check
from .common import ALLOW_WEAKENING_ENV, DEFAULT_FILES, OK, _captured, _err


WEAKENING_FLAGS = ("--accept-weakening", "--allow-weakening")
WEAKENING_SWITCHES = (*WEAKENING_FLAGS, ALLOW_WEAKENING_ENV)

PROTECTED = (lockfile.LOCK_NAME, "pyproject.toml")
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
    return any((d / lockfile.LOCK_NAME).exists() for d in (here, *here.parents))


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
                return named[0] if named else lockfile.LOCK_NAME
    if _lock_dir(cwd) and any(_computed_target(words, redirects) for words, redirects in segments):
        return lockfile.LOCK_NAME
    return None


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


def _invariants_table(text: str) -> Any:
    """[tool.invariants] of a pyproject.toml text; the text itself when it does not parse."""
    import tomllib
    try:
        return (tomllib.loads(text).get("tool") or {}).get("invariants")
    except tomllib.TOMLDecodeError:
        return text if "invariants" in text else None


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
    """An edit of pyproject.toml that changes its [tool.invariants] tables (the scan roots and excludes).

    The file is read under the name given or, when no file has that exact name, under one that matches
    it ignoring case: on a case-insensitive filesystem `PyProject.toml` is pyproject.toml, and judging
    it as an empty file there would miss the edit."""
    try:
        before = path.read_text()
    except OSError:
        before = ""
        try:
            twin = next((p for p in path.parent.iterdir() if p.name.casefold() == path.name.casefold()), None)
            before = twin.read_text() if twin is not None else ""
        except OSError:
            pass
    after = _edited_text(tool_input, before)
    if after is None:
        return "invariants" in json.dumps(tool_input)
    return not lockfile.same(_invariants_table(before), _invariants_table(after))


def _pretool(event: dict[str, Any]) -> int:
    given = event.get("tool_input")
    tool_input: dict[str, Any] = given if isinstance(given, dict) else {}
    cwd = str(event.get("cwd") or os.getcwd())
    command = str(tool_input.get("command") or "")
    files = [pathlib.Path(os.path.realpath(os.path.join(cwd, str(tool_input[k]))))
             for k in ("file_path", "path", "notebook_path") if tool_input.get(k)]
    written = _shell_writes(command, cwd)
    lock, pyproject = lockfile.LOCK_NAME.casefold(), "pyproject.toml"   # a case-insensitive filesystem
    if any(f.name.casefold() == lock for f in files) or written == lockfile.LOCK_NAME:
        _err(f"invariants: blocked a direct edit of {lockfile.LOCK_NAME}. The lock is written only by "
             f"`inv lock` or `inv check --relock`, which refuse a weakening; edit invariants.yaml or the "
             f"code, then run one of them.")
        return 2
    if written or any(f.name.casefold() == pyproject and _touches_invariants_table(f, tool_input) for f in files):
        _err("invariants: blocked an edit of the [tool.invariants] settings in pyproject.toml (or a shell "
             "command that writes pyproject.toml). The scan roots and excludes decide which code is checked "
             "for back doors; tell the user what you want to change and let them make the edit.")
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
             tables, or a shell command that writes pyproject.toml.
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
