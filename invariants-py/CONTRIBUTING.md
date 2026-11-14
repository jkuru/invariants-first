# Contributing

Thank you for helping. This project is small and young, so issues and pull requests are both welcome.

## Ground rules

- Use decorators only. The library never rewrites user code (no AST rewriting) and never calls `eval` or `exec` on rule text or source code. A test checks this.
- Every behaviour you add needs a pytest test.
- Keep docstrings plain and short, and add type hints to the public API.
- Test fixture apps live in `tests/fixtures/`: the rule-file apps `jobs`, `triage` and `pool`, the
  FastAPI and pydantic apps, and the two CLI projects `cli/clean` and `cli/bypass`. The library must
  never import them. A test that must import an app with guards should run it in a subprocess or a
  scoped fixture, so its guards do not leak into other tests.
- The standalone example projects live in `apps/` at the root of the monorepo, next to this
  library. They use only the public API, have their own `pyproject.toml`, tests and `ci.sh`, and are
  not part of the library's test suite. A change that affects them must keep all three
  `apps/*/ci.sh` (pool-service, fact-pipeline, support-agent) green. The GitHub Action
  (`action.yml`), the pre-commit hook (`.pre-commit-hooks.yaml`) and CI (`.github/`) also live at
  the monorepo root.
- Documentation: the README is the front page; reference detail belongs in `docs/`. Commands and
  outputs shown in the README and `docs/cookbook.md` are real; re-run them when behaviour changes.

## Setup

```
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev,cel]'
python -m pytest tests -q
```

Run the suite on each CEL backend before you send a change to CEL behaviour, and mypy and the
examples before any change:

```
for b in subset cel-expr; do INV_CEL_BACKEND=$b python -m pytest tests -q; done
python -O -m pytest tests -q        # asserts must not carry behaviour
python -m mypy
PYTHON=$(command -v python) ../apps/pool-service/ci.sh
PYTHON=$(command -v python) ../apps/fact-pipeline/ci.sh
PYTHON=$(command -v python) ../apps/support-agent/ci.sh
```

## CEL conformance cases

The cases are YAML files under `tests/conformance/cel/`. A new case must pass on every backend. A known disagreement goes in `expected_failures.yaml` with a reason. Those entries run as strict xfails, so fixing the case means removing its entry.

## Pull requests

- Keep each change focused, and describe the behaviour it changes.
- Add an entry under `## [Unreleased]` in `CHANGELOG.md` (the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format).
- By contributing, you agree that your work is licensed under Apache-2.0.

Maintainers: the release steps, and the metadata to settle before the first upload (package name,
`project.urls`, authors, PyPI trusted publishing), are in [RELEASING.md](RELEASING.md).

Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
