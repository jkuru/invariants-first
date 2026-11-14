#!/usr/bin/env bash
# pool-service CI: rule file checks, coverage, the static scan, then the tests with ablation.
set -euo pipefail
cd "$(dirname "$0")"

# Pick the interpreter: $PYTHON, else an activated venv, else a private .venv made on first run.
# The private venv installs the library (and the three libraries it depends on, packages/) from this
# checkout when the example sits in apps/ of the monorepo (else from the package index), then this app
# with its dev extras.
if [ -n "${PYTHON:-}" ]; then PY="$PYTHON"
elif [ -n "${VIRTUAL_ENV:-}" ]; then PY="$VIRTUAL_ENV/bin/python"
else
  if [ ! -f .venv/.ci-ready-2 ]; then        # made on the first run; a failed setup is redone; -2: venvs made
    # before the libraries split out of invariants-first lack them, so they are made again
    BASE="${BASE_PYTHON:-python3}"           # the interpreter the venv is made from (3.11 or later)
    "$BASE" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
      || { echo "ci.sh: $BASE is older than 3.11; set BASE_PYTHON or PYTHON" >&2; exit 2; }
    echo "ci.sh: creating .venv from $BASE (set PYTHON to reuse an existing interpreter)" >&2
    "$BASE" -m venv --clear .venv
    if [ -f ../../invariants-py/pyproject.toml ] && [ -d ../../invariants-py/src/invariants ]; then
      # the three libraries invariants-first depends on (packages/), then invariants-first itself
      .venv/bin/python -m pip install -q --disable-pip-version-check -e ../../packages/invariants-cel \
        -e ../../packages/invariants-scan -e ../../packages/invariants-schema-diff -e "../../invariants-py[fastapi,pydantic,schema]"
    fi
    .venv/bin/python -m pip install -q --disable-pip-version-check -e '.[dev]'
    touch .venv/.ci-ready-2
  fi
  PY=.venv/bin/python
fi
INV="$(dirname "$PY")/inv"
[ -x "$INV" ] || INV="$(command -v inv || true)"
if [ -z "$INV" ]; then echo "ci.sh: no inv next to $PY or on PATH; install the invariants library there" >&2; exit 2; fi
# This checkout's src/ comes first, so another installed copy of the app cannot shadow it.
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p build
CLEAN_ROOTS=(--root src)                 # bypass/ is left out of the clean scan on purpose

step() { printf '\n== %s\n' "$*"; }

step "inv validate";      "$INV" validate invariants.yaml
step "inv lock --check";  "$INV" lock --check
step "inv verify";        "$INV" verify --import pool_service.api
step "inv scan (clean)";  "$INV" scan "${CLEAN_ROOTS[@]}"
step "inv status";        "$INV" status "${CLEAN_ROOTS[@]}" --import pool_service.api
step "pytest (R-23's bypass scan is tests/test_scan.py)"
"$PY" -m pytest --inv-verify --inv-ablate --inv-report --inv-report-json build/inv-report.json
step "ok"
