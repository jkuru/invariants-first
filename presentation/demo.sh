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

# ---- presentation helpers for demo 1 (colours off when not a terminal, or NO_COLOR is set) ----
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_R=$'\e[0m'; C_B=$'\e[1m'; C_DIM=$'\e[2m'; C_CMD=$'\e[1;36m'; C_BAD=$'\e[1;31m'
  C_OK=$'\e[1;32m'; C_FOCUS=$'\e[1;33m'; C_HEAD=$'\e[1;35m'; GIT_COLOR=always
else
  C_R=; C_B=; C_DIM=; C_CMD=; C_BAD=; C_OK=; C_FOCUS=; C_HEAD=; GIT_COLOR=never
fi
export C_R C_BAD C_OK C_FOCUS

intro() {   # intro NAME WHAT LICENSE WHERE INSTALL VERSION
  printf '\n%s┏━━ OPEN-SOURCE TARGET: %s ━━%s\n' "$C_HEAD" "$1" "$C_R"
  printf '%s┃%s  What:     %s\n' "$C_HEAD" "$C_R" "$2"
  printf '%s┃%s  License:  %s (free to use, read and copy)\n' "$C_HEAD" "$C_R" "$3"
  printf '%s┃%s  Source:   %s\n' "$C_HEAD" "$C_R" "$4"
  printf '%s┃%s  Get it:   %s\n' "$C_HEAD" "$C_R" "$5"
  printf '%s┃%s  Version:  %s%s%s  (read from the installed package)\n' "$C_HEAD" "$C_R" "$C_B" "$6" "$C_R"
  printf '%s┃%s  How:      we copy the installed package, untouched, into a fresh git repo\n' "$C_HEAD" "$C_R"
  printf '%s┗━━%s\n' "$C_HEAD" "$C_R"
}
step()   { printf '\n%s━━ STEP %s ━━%s %s%s%s\n' "$C_HEAD" "$1" "$C_R" "$C_B" "$2" "$C_R"; }
say()    { printf '   %s\n' "$*"; }
pause()  { if [ "${DEMO_PAUSE:-0}" = 1 ] && [ -t 0 ]; then printf '%s   [press Enter]%s' "$C_DIM" "$C_R"; read -r _; fi; }
colour() {   # highlight what the audience should look at
  if [ -z "$C_R" ]; then cat; return; fi
  perl -pe 's/\b(FAIL|WEAKENED|fail)\b/$ENV{C_BAD}$1$ENV{C_R}/g;
            s/^(\s*)(ok|pass)\b/$1$ENV{C_OK}$2$ENV{C_R}/;
            s/(\S+ -> \S+)/$ENV{C_FOCUS}$1$ENV{C_R}/g;
            s/(pinned \d+ pydantic model\(s\) with \d+ constraint\(s\))/$ENV{C_FOCUS}$1$ENV{C_R}/'
}
run_inv() {  # show the command as typed, run it, colour the output; returns its exit code
  local disp="inv $*"
  [ -n "${SITE_SHOWN:-}" ] && disp=${disp//$SITE_SHOWN/<site-packages>}
  printf '   %s$ %s%s\n' "$C_CMD" "$disp" "$C_R"
  set +e; "$INV" "$@" 2>&1 | colour | sed 's/^/     /'; local rc=${PIPESTATUS[0]}; set -e
  return "$rc"
}
show_diff() {   # the one line the "agent" changed, as a coloured diff
  say "${C_B}The agent's edit:${C_R}"
  git --no-pager diff --color="$GIT_COLOR" -U0 -- "$1" | grep -v '^\(diff\|index\)' | sed 's/^/     /'
}
expect() {   # expect WANTED_RC GOT_RC MESSAGE
  if [ "$2" -eq "$1" ]; then printf '   %s✓ exit code %s: %s%s\n' "$C_OK" "$2" "$3" "$C_R"
  else printf '   %s✗ expected exit code %s, got %s%s\n' "$C_BAD" "$1" "$2" "$C_R"; return 1; fi
}
edit_once() {   # edit_once FILE OLD NEW: replace the first OLD with NEW
  "$PY" -c "
import pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
assert sys.argv[2] in s, 'demo: text to edit not found in ' + sys.argv[1]
p.write_text(s.replace(sys.argv[2], sys.argv[3], 1))" "$1" "$2" "$3"
}

demo_1_oss() {
  banner "DEMO 1: inv on real open-source code (FastAPI, then langchain_core)"
  say "Open source means the code is public: anyone can download it, read it and run tools on it."
  say "We use two popular Python projects nobody here wrote, with no rules prepared in advance."
  pause

  # ------------------------------------------------------------------ FastAPI
  local tmp_fa fa_ver
  tmp_fa="$(mktemp -d)"
  trap 'rm -rf "$tmp_fa"' RETURN
  fa_ver="$("$PY" -c 'import fastapi; print(fastapi.__version__)')"
  intro "FastAPI" "a popular Python web framework for building APIs" "MIT" \
        "github.com/fastapi/fastapi" "pip install fastapi" "$fa_ver"
  "$PY" -c "
import pathlib, shutil, fastapi
src = pathlib.Path(fastapi.__file__).parent
shutil.copytree(src, pathlib.Path('$tmp_fa') / 'fastapi', ignore=shutil.ignore_patterns('__pycache__'))
"
  pause
  (
    cd "$tmp_fa"
    git init -q -b main
    git config user.email demo@example.com
    git config user.name Demo

    step "1/4" "Find the rules FastAPI already has"
    say "No rules written by us. inv init reads the code and locks what it finds."
    run_inv init --root . --path .
    git add -A
    git commit -q -m "Baseline: lock existing FastAPI contracts"
    say "Committed to main: this is the reviewed, agreed starting point."
    pause

    step "2/4" "A coding agent loosens a limit"
    say "multipleOf must be ${C_FOCUS}greater than 0${C_R}. The agent changes it to ${C_FOCUS}0 or more${C_R}."
    edit_once fastapi/openapi/models.py 'gt=0' 'ge=0'
    show_diff fastapi/openapi/models.py
    pause

    step "3/4" "The agent tries to hide it by updating the lock itself"
    run_inv lock --accept-weakening
    say "The local lock now matches the loosened code. Locally, everything looks fine."
    pause

    step "4/4" "CI compares against main, not the agent's lock"
    if run_inv check --base main; then rc=0; else rc=$?; fi
    expect 1 "$rc" "CI blocks the loosened rule until a person accepts it"
  )
  pause

  # ------------------------------------------------------------------ langchain_core
  if [ -x "$SA_PY" ]; then
    local tmp_lc site_lc lc_ver
    tmp_lc="$(mktemp -d)"
    site_lc="$("$SA_PY" -c 'import pathlib, langchain_core; print(pathlib.Path(langchain_core.__file__).parent.parent)')"
    lc_ver="$("$SA_PY" -c 'import langchain_core; print(langchain_core.__version__)')"
    intro "langchain_core" "the core library of LangChain, used to build LLM apps and agents" "MIT" \
          "github.com/langchain-ai/langchain" "pip install langchain-core" "$lc_ver"
    "$SA_PY" -c "
import pathlib, shutil, langchain_core
src = pathlib.Path(langchain_core.__file__).parent
shutil.copytree(src, pathlib.Path('$tmp_lc') / 'langchain_core', ignore=shutil.ignore_patterns('__pycache__'))
"
    pause
    (
      cd "$tmp_lc"
      SITE_SHOWN="$site_lc"
      git init -q -b main
      git config user.email demo@example.com
      git config user.name Demo

      step "1/3" "Find the rules langchain_core already has"
      run_inv init --root . --path . --site "$site_lc"
      git add -A
      git commit -q -m "Baseline: lock langchain_core contracts"
      pause

      step "2/3" "A safe change: make a rule ${C_OK}stricter${C_R}"
      say "Choice.criteria must have at least 1 entry. Now it must have at least 2."
      edit_once langchain_core/decisions/types.py 'min_length=1' 'min_length=2'
      show_diff langchain_core/decisions/types.py
      run_inv lock --path . --site "$site_lc"
      if run_inv check --base main --path . --site "$site_lc"; then rc=0; else rc=$?; fi
      expect 0 "$rc" "stricter passes, nobody has to approve it"
      git add -A
      git commit -q -m "Tighten Choice.criteria"
      say "Reviewed and merged to main."
      pause

      step "3/3" "A sneaky change: make a rule ${C_BAD}looser${C_R}, then re-lock"
      say "NoulAnswer.noul is a score that must be ${C_FOCUS}at most 1.0${C_R}. The agent allows ${C_FOCUS}up to 2.0${C_R}."
      edit_once langchain_core/decisions/types.py 'le=1.0' 'le=2.0'
      show_diff langchain_core/decisions/types.py
      say "The agent re-pins the lock to hide it:"
      run_inv lock --accept-weakening --path . --site "$site_lc"
      if run_inv check --base main --path . --site "$site_lc"; then rc=0; else rc=$?; fi
      expect 1 "$rc" "looser fails CI, even after the agent re-locked"
    )
    rm -rf "$tmp_lc"
  else
    say "(langchain_core part skipped: $SA_PY not found)"
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
