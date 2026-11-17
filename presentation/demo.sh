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
            s/(pinned \d+ pydantic model\(s\) with \d+ constraint\(s\))/$ENV{C_FOCUS}$1$ENV{C_R}/;
            s/: (pass)\b/: $ENV{C_OK}$1$ENV{C_R}/; s/: (fail)\b/: $ENV{C_BAD}$1$ENV{C_R}/;
            s/\b(load-bearing|held|yes)\b/$ENV{C_OK}$1$ENV{C_R}/g; s/\b(passed)\b/$ENV{C_OK}$1$ENV{C_R}/;
            s/^(bypass|forbidden)\b/$ENV{C_BAD}$1$ENV{C_R}/; s/\b(blocked)\b/$ENV{C_BAD}$1$ENV{C_R}/;
            s/(rogue\.py:\d+)/$ENV{C_FOCUS}$1$ENV{C_R}/'
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
  banner "DEMO 2: Stop the agent before it even tries (support-agent)"
  printf '\n%s┏━━ WHAT A HOOK IS ━━%s\n' "$C_HEAD" "$C_R"
  printf '%s┃%s  A script Claude Code runs before each action the agent takes.\n' "$C_HEAD" "$C_R"
  printf '%s┃%s  It reads the action as JSON. %sExit code 2%s means: blocked, and the agent is told why.\n' "$C_HEAD" "$C_R" "$C_FOCUS" "$C_R"
  printf '%s┃%s  Here we play the agent: we send the hook the action Claude Code would send.\n' "$C_HEAD" "$C_R"
  printf '%s┗━━%s\n' "$C_HEAD" "$C_R"
  pause
  (
    cd "$ROOT/apps/support-agent"

    step "1/3" "The agent tries to ${C_BAD}edit invariants.lock${C_R} by hand"
    say "Action: ${C_FOCUS}{\"tool_name\": \"Edit\", \"file_path\": \"invariants.lock\"}${C_R}"
    printf '   %s$ inv hook pretool%s\n' "$C_CMD" "$C_R"
    set +e
    printf '{"tool_name":"Edit","tool_input":{"file_path":"invariants.lock"},"cwd":"%s"}\n' "$PWD" \
      | "$INV" hook pretool 2>&1 | colour | fold -s -w 100 | sed 's/^/     /'
    rc=${PIPESTATUS[1]}
    set -e
    expect 2 "$rc" "blocked: only inv lock may write the lock"
    pause

    step "2/3" "The agent tries to ${C_BAD}accept its own loosening${C_R}"
    say "Action: ${C_FOCUS}{\"tool_name\": \"Bash\", \"command\": \"inv lock --accept-weakening\"}${C_R}"
    printf '   %s$ inv hook pretool%s\n' "$C_CMD" "$C_R"
    set +e
    printf '{"tool_name":"Bash","tool_input":{"command":"inv lock --accept-weakening"},"cwd":"%s"}\n' "$PWD" \
      | "$INV" hook pretool 2>&1 | colour | fold -s -w 100 | sed 's/^/     /'
    rc=${PIPESTATUS[1]}
    set -e
    expect 2 "$rc" "blocked: accepting a loosening is a person's decision"
    pause

    step "3/3" "And if a shortcut gets past the hook, ${C_OK}CI catches it${C_R}"
    say "Each test makes a change an agent might make, then checks that inv check fails."
    say "The three 'safe' tests check that honest changes still pass."
    if run_tests "" tests/test_ci_defects.py; then rc=0; else rc=$?; fi
    expect 0 "$rc" "every shortcut caught, every honest change passes"
  )
}

demo_3_scan() {
  banner "DEMO 3: The scan finds back doors (pool-service)"
  printf '\n%s┏━━ THE APP: pool-service (in this repo, apps/pool-service) ━━%s\n' "$C_HEAD" "$C_R"
  printf '%s┃%s  What:   a service that leases slots in worker pools, and resizes pools with approval\n' "$C_HEAD" "$C_R"
  printf '%s┃%s  Doors:  %slease_slots%s, %sResizeService.resize_door%s (the safe ways in)\n' "$C_HEAD" "$C_R" "$C_FOCUS" "$C_R" "$C_FOCUS" "$C_R"
  printf '%s┃%s  Sinks:  %sstore.insert_lease%s, %sstore.set_size%s (the writes behind them)\n' "$C_HEAD" "$C_R" "$C_FOCUS" "$C_R" "$C_FOCUS" "$C_R"
  printf '%s┃%s  How:    inv scan reads the code. It does not run it.\n' "$C_HEAD" "$C_R"
  printf '%s┗━━%s\n' "$C_HEAD" "$C_R"
  pause
  (
    cd "$ROOT/apps/pool-service"
    export PYTHONPATH=src
    shorten() { sed -E 's#pool_service/rogue\.py:([0-9]+):[0-9]+#rogue.py:\1#; s/pool_service\.(service|store|rogue)\.//g;
                       s/ reaches the sink without passing / skips the door /'; }

    step "1/3" "Scan the real code"
    printf '   %s$ inv scan invariants.yaml --root src%s\n' "$C_CMD" "$C_R"
    set +e
    "$INV" scan invariants.yaml --root src 2>&1 | grep -v 'not scanned' | colour | sed 's/^/     /'
    rc=${PIPESTATUS[0]}
    set -e
    expect 0 "$rc" "no back doors in the real code"
    pause

    step "2/3" "An agent adds an 'ops hotfix' file"
    say "Four ways to reach a sink without its door:"
    for ln in 10 15 20 25; do
      code=$(sed -n "${ln}p" bypass/pool_service/rogue.py | sed 's/^ *//')
      case $ln in
        10) why="calls the sink directly";;
        15) why="unwraps the guard with __wrapped__";;
        20) why="hides the name with getattr";;
        25) why="writes a lease without its door";;
      esac
      printf '     %srogue.py:%s%s  %s\n         %s← %s%s\n' "$C_FOCUS" "$ln" "$C_R" "$code" "$C_DIM" "$why" "$C_R"
    done
    pause

    step "3/3" "Scan again, with that file"
    printf '   %s$ inv scan invariants.yaml --root src --root bypass%s\n' "$C_CMD" "$C_R"
    set +e
    out=$("$INV" scan invariants.yaml --root src --root bypass 2>&1); rc=$?
    set -e
    printf '%s\n' "$out" | grep -E '^(bypass|forbidden)' | shorten | colour | sed 's/^/     /'
    n=$(printf '%s\n' "$out" | grep -cE '^(bypass|forbidden)')
    printf '\n     %s%s findings%s, all in rogue.py. A text search would miss the getattr and __wrapped__ ones.\n' "$C_BAD" "$n" "$C_R"
    expect 1 "$rc" "the scan fails CI on every back door"
  )
}

run_tests() {   # run_tests KEYWORDS [FILE]: run the matching support-agent tests, one line per test, coloured
  local file="${2:-tests/test_runtime_defects.py}" sel=()
  [ -n "$1" ] && sel=(-k "$1")
  if [ -n "$1" ]; then printf '   %s$ pytest %s -k "%s"%s\n' "$C_CMD" "$file" "$1" "$C_R"
  else printf '   %s$ pytest %s%s\n' "$C_CMD" "$file" "$C_R"; fi
  set +e
  PYTHONPATH=src "$SA_PY" -m pytest "$file" -vv ${sel[@]+"${sel[@]}"} --no-header --tb=short \
      -W ignore::DeprecationWarning 2>&1 \
    | grep -E '::|passed|failed|error|Error|assert' \
    | sed -E 's/^tests\/[a-z_]+\.py:://; s/ +\[ *[0-9]+%\]$//' \
    | { if [ -n "$C_R" ]; then perl -pe 's/\bPASSED\b/$ENV{C_OK}PASSED$ENV{C_R}/g;
            s/\b(FAILED|ERROR)\b/$ENV{C_BAD}$1$ENV{C_R}/g; s/(\d+ passed)/$ENV{C_OK}$1$ENV{C_R}/'; else cat; fi; } \
    | sed 's/^/     /'
  local rc=${PIPESTATUS[0]}; set -e
  return "$rc"
}

demo_4_runtime() {
  banner "DEMO 4: When the AI model gets it wrong, at run time (support-agent)"
  printf '\n%s┏━━ THE APP: support-agent (in this repo, apps/support-agent) ━━%s\n' "$C_HEAD" "$C_R"
  printf '%s┃%s  What:   a support agent that reads a ticket, asks an AI planner what to do, and refunds\n' "$C_HEAD" "$C_R"
  printf '%s┃%s  Flow:   AI planner %s→%s execute_plan %s→%s BillingService.refund %s→%s Stripe\n' "$C_HEAD" "$C_R" "$C_DIM" "$C_R" "$C_DIM" "$C_R" "$C_DIM" "$C_R"
  printf '%s┃%s  Checks: %s@inv.model_call%s on the planner, %s@inv.edge%s on execute_plan, %s@inv.guard%s on the door\n' "$C_HEAD" "$C_R" "$C_FOCUS" "$C_R" "$C_FOCUS" "$C_R" "$C_FOCUS" "$C_R"
  printf '%s┃%s  Model:  a fake that we tell to misbehave, so no API key is needed\n' "$C_HEAD" "$C_R"
  printf '%s┃%s  Money:  a fake Stripe that records every refund, so we can see what moved\n' "$C_HEAD" "$C_R"
  printf '%s┗━━%s\n' "$C_HEAD" "$C_R"
  pause
  (
    cd "$ROOT/apps/support-agent"

    step "1/6" "The model refunds ${C_BAD}more than what's left${C_R}"
    say "It is asked once more, fresh. If it is still wrong, a person decides."
    if run_tests "over_refund_is_resampled or one_bad_sample or over_credit_is_escalated"; then rc=0; else rc=$?; fi
    expect 0 "$rc" "too much never reaches Stripe; an honest retry does"
    pause

    step "2/6" "The model picks ${C_BAD}another customer's order${C_R}"
    say "Handed to a person, and a plan checked for one customer cannot run for another."
    if run_tests "another_customer or cannot_run_for_another"; then rc=0; else rc=$?; fi
    expect 0 "$rc" "the wrong customer is never refunded"
    pause

    step "3/6" "The model sends ${C_BAD}invalid JSON or an unknown tool${C_R}, or says it can't"
    say "Retried with the parser error, at most twice. Then a person decides."
    if run_tests "d7 or d8"; then rc=0; else rc=$?; fi
    expect 0 "$rc" "a broken reply never becomes an action"
    pause

    step "4/6" "Someone runs a ${C_BAD}forged \"approved\" plan${C_R}"
    say "The edge does not trust the stamp. It checks the plan again."
    if run_tests "forged"; then rc=0; else rc=$?; fi
    expect 0 "$rc" "only the fair plan ran; the forged ones were refused"
    pause

    step "5/6" "The plan or the balance ${C_BAD}changes after the check${C_R}"
    say "The edge reads the live ledger again, just before paying."
    if run_tests "d10"; then rc=0; else rc=$?; fi
    expect 0 "$rc" "a stale or tampered plan is refused"
    pause

    step "6/6" "Other code calls ${C_BAD}refund() directly${C_R} with too much"
    say "The guard sits on the door itself, so it does not matter who calls."
    if run_tests "door_refuses"; then rc=0; else rc=$?; fi
    expect 0 "$rc" "the door refuses, whoever calls it"

    printf '\n   %s15 tests, 6 kinds of mistake: no bad action ever reached Stripe.%s\n' "$C_OK" "$C_R"
  )
}

demo_5_ablation() {
  banner "DEMO 5: Is each guard real? Switch it off and see (fact-pipeline)"
  printf '\n%s┏━━ THE APP: fact-pipeline (in this repo, apps/fact-pipeline) ━━%s\n' "$C_HEAD" "$C_R"
  printf '%s┃%s  What:   an AI reads documents and proposes facts\n' "$C_HEAD" "$C_R"
  printf '%s┃%s  Rules:  like %sK-1: no fact exists without evidence%s\n' "$C_HEAD" "$C_R" "$C_FOCUS" "$C_R"
  printf '%s┃%s  Idea:   switch one guard off, rerun its test. The test must now fail.\n' "$C_HEAD" "$C_R"
  printf '%s┃%s          If it does, the guard is %sload-bearing%s.\n' "$C_HEAD" "$C_R" "$C_OK" "$C_R"
  printf '%s┗━━%s\n' "$C_HEAD" "$C_R"
  pause
  (
    cd "$ROOT/apps/fact-pipeline"
    FP_PY="$PWD/.venv/bin/python"
    if [ ! -x "$FP_PY" ]; then say "(first run: setting up the app's environment via ci.sh)"; ./ci.sh >/dev/null; fi
    export PYTHONPATH=src

    step "1/2" "Switch each guard off, one at a time, and rerun its test"
    printf '   %s$ pytest --inv-ablate%s\n' "$C_CMD" "$C_R"
    set +e
    "$FP_PY" -m pytest -q --inv-ablate --inv-report-json build/inv-report.json -W ignore::DeprecationWarning 2>&1 \
      | grep -E 'K-[0-9]+:|caught|not checked|missed|[0-9]+ (passed|failed)' \
      | sed -E 's#caught +tests/#caught  #' | colour | sed 's/^/     /'
    rc=${PIPESTATUS[0]}
    set -e
    expect 0 "$rc" "every guard is load-bearing: its test noticed it was gone"
    pause

    step "2/2" "inv status: each rule's standing"
    say "A rule is ${C_OK}held${C_R} when its guard is wired, the scan passes, and ablation proved it."
    printf '   %s$ inv status --ablation build/inv-report.json%s\n' "$C_CMD" "$C_R"
    set +e
    "$INV" status invariants.yaml --import fact_pipeline.pipeline --ablation build/inv-report.json 2>&1 \
      | grep -E '^K-[0-9]+ ' | colour | sed 's/^/     /'
    rc=${PIPESTATUS[0]}
    set -e
    expect 0 "$rc" "proven, not just claimed"
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
