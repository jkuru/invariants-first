#!/usr/bin/env bash
# CI for fact-pipeline: rule file, lock, coverage, scan, standing, then the tests with the invariants plugin.
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
        -e ../../packages/invariants-scan -e ../../packages/invariants-schema-diff -e "../../invariants-py[pydantic,schema]"
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
# Scan roots come from pyproject ([tool.invariants.scan]); the bypass root is left out there.

echo "== inv validate";     "$INV" validate invariants.yaml
echo "== inv lock --check"; "$INV" lock invariants.yaml --check
echo "== inv verify";       "$INV" verify invariants.yaml --import fact_pipeline.pipeline
echo "== inv scan";         "$INV" scan invariants.yaml
echo "== inv status";       "$INV" status invariants.yaml --import fact_pipeline.pipeline
echo "== pytest";           "$PY" -m pytest -q --inv-verify --inv-ablate --inv-report \
                              --inv-report-json build/inv-report.json
echo "ci: ok"
