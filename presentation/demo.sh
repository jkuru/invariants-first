#!/usr/bin/env bash
# Live demo runner for the 11/18 invariants-first presentation.
# Runs OSS library demos (fastapi, langchain_core) in clean /tmp git repos
# plus repo CI, bypass scan, runtime defense, and ablation demos.
#
# Usage:
#   ./presentation/demo.sh           # run all steps non-interactively
#   ./presentation/demo.sh 1         # Demo 1: OSS FastAPI + LangChain Core ratchet
#   ./presentation/demo.sh 2         # Demo 2: Claude Code hook + support-agent CI defects
#   ./presentation/demo.sh 3         # Demo 3: Static path-law bypass scan (pool-service)
#   ./presentation/demo.sh 4         # Demo 4: Runtime guards + @inv.edge (support-agent)
#   ./presentation/demo.sh 5         # Demo 5: Mutation testing via --inv-ablate (fact-pipeline)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
INV="$ROOT/.venv/bin/inv"
SA_PY="$ROOT/apps/support-agent/.venv/bin/python"

if [ ! -x "$INV" ]; then
  echo "error: missing $INV (activate or set up root .venv)" >&2
  exit 2
fi

banner() {
  printf '\n================================================================\n'
  printf '  %s\n' "$*"
  printf '================================================================\n'
}

demo_1_oss() {
  banner "DEMO 1A: Zero-config inv init + ratchet on FastAPI (open-source)"
  local tmp_fa
  tmp_fa="$(mktemp -d)"
  trap 'rm -rf "$tmp_fa"' RETURN

  "$PY" -c "
import pathlib, shutil, fastapi
src = pathlib.Path(fastapi.__file__).parent
shutil.copytree(src, pathlib.Path('$tmp_fa') / 'fastapi', ignore=shutil.ignore_patterns('__pycache__'))
"
  (
    cd "$tmp_fa"
    git init -q -b main
    git config user.email demo@example.com
    git config user.name Demo
    "$INV" init --root . --path .
    git add -A
    git commit -q -m "Baseline: lock existing FastAPI contracts"

    echo ""
    echo "-- Agent weakens fastapi/openapi/models.py (multipleOf: gt=0 -> ge=0) and re-pins lock --"
    "$PY" -c "
import pathlib
p = pathlib.Path('fastapi/openapi/models.py')
p.write_text(p.read_text().replace('gt=0', 'ge=0', 1))
"
    "$INV" lock --accept-weakening >/dev/null
    echo "-- CI runs: inv check --base main (expected to FAIL) --"
    set +e
    "$INV" check --base main
    rc=$?
    set -e
    echo "(exit code: $rc)"
    [ "$rc" -eq 1 ]
  )

  if [ -x "$SA_PY" ]; then
    banner "DEMO 1B: Tighten passes, weaken fails on langchain_core (35 models, 47 constraints)"
    local tmp_lc site_lc
    tmp_lc="$(mktemp -d)"
    site_lc="$("$SA_PY" -c 'import pathlib, langchain_core; print(pathlib.Path(langchain_core.__file__).parent.parent)')"
    "$SA_PY" -c "
import pathlib, shutil, langchain_core
src = pathlib.Path(langchain_core.__file__).parent
shutil.copytree(src, pathlib.Path('$tmp_lc') / 'langchain_core', ignore=shutil.ignore_patterns('__pycache__'))
"
    (
      cd "$tmp_lc"
      git init -q -b main
      git config user.email demo@example.com
      git config user.name Demo
      "$INV" init --root . --path . --site "$site_lc"
      git add -A
      git commit -q -m "Baseline: lock langchain_core contracts"

      echo ""
      echo "-- Safe tightening: Choice.criteria min_length=1 -> min_length=2 --"
      "$PY" -c "
import pathlib
p = pathlib.Path('langchain_core/decisions/types.py')
p.write_text(p.read_text().replace('min_length=1', 'min_length=2', 1))
"
      "$INV" lock --path . --site "$site_lc"
      "$INV" check --base main --path . --site "$site_lc"

      echo ""
      echo "-- Agent weakens NoulAnswer confidence bound (le=1.0 -> le=2.0) and re-pins lock --"
      "$PY" -c "
import pathlib
p = pathlib.Path('langchain_core/decisions/types.py')
p.write_text(p.read_text().replace('le=1.0', 'le=2.0', 1))
"
      "$INV" lock --accept-weakening --path . --site "$site_lc" >/dev/null
      set +e
      "$INV" check --base main --path . --site "$site_lc"
      rc=$?
      set -e
      echo "(exit code: $rc)"
      [ "$rc" -eq 1 ]
    )
    rm -rf "$tmp_lc"
  fi
}

demo_2_hooks_and_ci() {
  banner "DEMO 2: Claude Code PreToolUse Hook + support-agent CI Defects"
  (
    cd "$ROOT/apps/support-agent"
    echo "-- Simulated Claude Code Edit on invariants.lock --"
    set +e
    printf '{"tool_name":"Edit","tool_input":{"file_path":"invariants.lock"},"cwd":"%s"}\n' "$PWD" | "$INV" hook pretool
    rc=$?
    set -e
    echo "(hook exit code: $rc)"
    [ "$rc" -eq 2 ]

    echo ""
    echo "-- Running support-agent CI defects (D1-D5, D11-D12) --"
    PYTHONPATH=src "$SA_PY" -m pytest tests/test_ci_defects.py -v -W ignore::DeprecationWarning
  )
}

demo_3_scan() {
  banner "DEMO 3: Static Path-Law Bypass Scan (pool-service)"
  (
    cd "$ROOT/apps/pool-service"
    echo "-- Clean scan (src only) --"
    PYTHONPATH=src "$INV" scan invariants.yaml --root src
    echo ""
    echo "-- Scan including bypass/ (expected to FAIL with 10 findings) --"
    set +e
    PYTHONPATH=src "$INV" scan invariants.yaml --root src --root bypass
    rc=$?
    set -e
    echo "(scan exit code: $rc)"
    [ "$rc" -eq 1 ]
  )
}

demo_4_runtime() {
  banner "DEMO 4: Runtime Defense-in-Depth (support-agent D6-D10)"
  (
    cd "$ROOT/apps/support-agent"
    PYTHONPATH=src "$SA_PY" -m pytest tests/test_runtime_defects.py -v -W ignore::DeprecationWarning
  )
}

demo_5_ablation() {
  banner "DEMO 5: Proving Guards Are Load-Bearing via --inv-ablate (fact-pipeline)"
  (
    "$ROOT/apps/fact-pipeline/ci.sh"
    cd "$ROOT/apps/fact-pipeline"
    echo ""
    echo "-- Standing report after ablation (rules promoted to held) --"
    PYTHONPATH=src "$INV" status invariants.yaml --import fact_pipeline.pipeline --ablation build/inv-report.json
  )
}

case "${1:-all}" in
  1) demo_1_oss ;;
  2) demo_2_hooks_and_ci ;;
  3) demo_3_scan ;;
  4) demo_4_runtime ;;
  5) demo_5_ablation ;;
  all)
    demo_1_oss
    demo_2_hooks_and_ci
    demo_3_scan
    demo_4_runtime
    demo_5_ablation
    banner "ALL DEMOS COMPLETED SUCCESSFULLY"
    ;;
  *)
    echo "Usage: $0 [1|2|3|4|5|all]" >&2
    exit 2
    ;;
esac
