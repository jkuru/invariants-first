#!/usr/bin/env bash
# support-agent CI: `inv check` (rules, lock, guards, scan), then the tests with ablation.
set -euo pipefail
cd "$(dirname "$0")"

# Pick the interpreter: $PYTHON, else an activated venv, else a private .venv made on first run.
# The private venv installs the library from this checkout when the example sits in
# apps/ of the monorepo (else from the package index), then this app with its dev and langgraph
# extras. When langgraph cannot be installed (offline, say), it falls back to the dev extra alone
# and says so: tests/test_langgraph.py then skips. REQUIRE_LANGGRAPH=1 (the monorepo's CI sets it)
# makes a missing langgraph an error instead, whichever interpreter runs the tests.
if [ -n "${PYTHON:-}" ]; then PY="$PYTHON"
elif [ -n "${VIRTUAL_ENV:-}" ]; then PY="$VIRTUAL_ENV/bin/python"
else
  if [ ! -f .venv/.ci-ready ]; then          # made on the first run; a failed setup is redone
    BASE="${BASE_PYTHON:-python3}"           # the interpreter the venv is made from (3.11 or later)
    "$BASE" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
      || { echo "ci.sh: $BASE is older than 3.11; set BASE_PYTHON or PYTHON" >&2; exit 2; }
    echo "ci.sh: creating .venv from $BASE (set PYTHON to reuse an existing interpreter)" >&2
    "$BASE" -m venv --clear .venv
    if [ -f ../../invariants-py/pyproject.toml ] && [ -d ../../invariants-py/src/invariants ]; then
      .venv/bin/python -m pip install -q --disable-pip-version-check -e "../../invariants-py[pydantic,schema]"
    fi
    if ! .venv/bin/python -m pip install -q --disable-pip-version-check -e '.[dev,langgraph]'; then
      [ "${REQUIRE_LANGGRAPH:-}" != 1 ] \
        || { echo "ci.sh: the langgraph extra did not install, and REQUIRE_LANGGRAPH=1" >&2; exit 2; }
      echo "ci.sh: the langgraph extra did not install; installing '.[dev]' alone (delete .venv to retry)" >&2
      .venv/bin/python -m pip install -q --disable-pip-version-check -e '.[dev]'
    fi
    touch .venv/.ci-ready
  fi
  PY=.venv/bin/python
fi
INV="$(dirname "$PY")/inv"
[ -x "$INV" ] || INV="$(command -v inv || true)"
if [ -z "$INV" ]; then echo "ci.sh: no inv next to $PY or on PATH; install the invariants library there" >&2; exit 2; fi
# This checkout's src/ comes first, so another installed copy of the app cannot shadow it.
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p build
step() { printf '\n== %s\n' "$*"; }

if ! "$PY" -c 'import langgraph' 2>/dev/null; then
  [ "${REQUIRE_LANGGRAPH:-}" != 1 ] \
    || { echo "ci.sh: langgraph is not importable from $PY, and REQUIRE_LANGGRAPH=1 (pip install -e '.[langgraph]')" >&2; exit 2; }
  echo "ci.sh: langgraph is not installed for $PY; tests/test_langgraph.py will skip (pip install -e '.[langgraph]')" >&2
fi

step "inv check";         "$INV" check
step "inv status";        "$INV" status --import support_agent.graph
step "pytest (defects 1-5, 11, 12 run inv check on a copy; 6-10 at run time)"
"$PY" -m pytest --inv-verify --inv-ablate --inv-report --inv-report-json build/inv-report.json
step "ok"
