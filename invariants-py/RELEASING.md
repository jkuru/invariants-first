# Releasing

The project is **four distributions**, released together at one version and in this order, so that
each one's dependencies are on the index before it is:

| Order | Distribution (dummy name) | Import | Source | Depends on |
|---|---|---|---|---|
| 1 | `invariants-cel` | `invariants_cel` | `packages/invariants-cel/` | nothing (extra `[cel]`: cel-expr-python, protobuf) |
| 2 | `invariants-scan` | `invariants_scan` | `packages/invariants-scan/` | nothing |
| 3 | `invariants-schema-diff` | `invariants_schema_diff` | `packages/invariants-schema-diff/` | `invariants-cel==<version>` |
| 4 | `invariants-first` | `invariants` | `invariants-py/` | PyYAML and the three, each `==<version>` (its `[cel]` extra is `invariants-cel[cel]`) |

Paths below are relative to `invariants-py/` unless they start with `packages/`.

## Before publishing

The project has not been published yet. Its publishing metadata holds **dummy values** on
`example.com`, a domain reserved for examples, so nothing real is invented. **Every unticked item
below is a release blocker**: do not upload to PyPI (or TestPyPI) until each one is settled by the
project owner, then delete this section. The dummies must not ship:

| Field                                      | Today (dummy)                                       | Owner must supply                   |
|--------------------------------------------|-----------------------------------------------------|-------------------------------------|
| `[project] name` (four `pyproject.toml`)   | `invariants-first`, `invariants-cel`, `invariants-scan`, `invariants-schema-diff` | the final distribution names |
| `[project.urls]` (all four)                | Homepage, Source, Issues, Changelog on `example.com` | the repository's public URLs        |
| SARIF `informationUri` (`packages/invariants-scan/src/invariants_scan/sarif.py`) | `https://example.com/invariants-first` | the project URL |

CI's `twine check --strict` passes with these placeholders (it checks that the metadata and README
render, not that they are final), so a green build is not a sign that this list is done.

This section and the dummy values go together, and `tests/test_fix_round6.py`
(`test_publishing_checklist_and_placeholders_go_together`) flips with them: while this section
exists, `pyproject.toml` and the SARIF URL hold the `example.com` dummies; once it is deleted, no
`example.com` value may remain. Replace the dummies and delete this section in the same change, and
the suite stays green; do one without the other and it fails. The release workflow refuses to run
while this section exists or any `example.com` value remains in `pyproject.toml` or the SARIF URL.

- [ ] **Final package names.** `invariants-first` in `pyproject.toml` (`[project] name`) is a
      placeholder, and so are the three libraries' names (`invariants-cel`, `invariants-scan`,
      `invariants-schema-diff` in `packages/*/pyproject.toml`, and the pins on them in
      `pyproject.toml`'s `dependencies` and `cel` extra, `packages/invariants-schema-diff/pyproject.toml`,
      `.github/workflows/ci.yml` and `release.yml`, `tests/test_fix_round27_split2.py`,
      `tests/test_mcp.py` and `tests/test_layering_packages.py`). Pick the distribution names, check that
      they are free on PyPI (and on TestPyPI), and update every place that names them
      (`git grep -n invariants-first` and `git grep -nE 'invariants-(cel|scan|schema-diff)'` list them):
      `pyproject.toml`; `src/invariants/__init__.py` (`_metadata.version(...)`); the install hints in
      `packages/invariants-cel/src/invariants_cel/expr_python.py` and
      `src/invariants/integrations/anthropic.py`; the README
      (the Quickstart install line and its placeholder note); `docs/cookbook.md`, `docs/start.md`,
      `docs/models.md` and `docs/design-guide.md` (line 4, and its copy `Invariant_first.md` at the
      monorepo root, kept byte-identical); the monorepo root `README.md` and `makeInvariantSimple.md`;
      the three apps (`apps/pool-service`, `apps/fact-pipeline`, `apps/support-agent`: each
      `pyproject.toml`, their dependency on the library, and each `README.md`); and
      `tests/test_package.py` and `tests/test_integrations_anthropic.py`. The import names stay
      `invariants`, `invariants_cel`, `invariants_scan` and `invariants_schema_diff`.
- [ ] **`[project.urls]`.** Replace the dummy `Homepage`, `Source`, `Issues` and `Changelog`
      (`example.com`) with the repository's public URLs, in all four `pyproject.toml`.
- [ ] **README badges.** The badges are static shields that link to files in this repository; the
      CI badge says "GitHub Actions" whatever the last run did, so it must be swapped for a live one.
      Once the repository has its public URL, point the CI badge at the workflow's status badge
      (`.../actions/workflows/ci.yml/badge.svg`) and add a PyPI version badge.
- [ ] **SARIF tool URL.** `inv scan --format sarif` reports `informationUri` as the dummy
      `https://example.com/invariants-first` (in `packages/invariants-scan/src/invariants_scan/sarif.py`); set it to the
      project URL.
- [x] **Authors.** Settled by the owner: `authors` in `[project]` (all four `pyproject.toml`) is
      `[{name = "Claude"}, {name = "Gemini"}, {name = "Jerry"}]`, names only, with no email address
      (Jerry built it). Add `maintainers` only if the owner names them. The `LICENSE` appendix's
      copyright line is the Apache template and needs no change, but a `NOTICE` file may name the
      copyright holder.
- [ ] **PyPI trusted publishing.** Create the four projects on PyPI (and TestPyPI), register this
      repository's release workflow (`.github/workflows/release.yml`, environments `testpypi` and
      `pypi`) as a trusted publisher, and create those two GitHub environments (required reviewers
      on `pypi`). The workflow is a draft that runs only by hand; it builds the sdist and wheel and
      uploads them with `pypa/gh-action-pypi-publish` using OpenID Connect (no API token stored in
      the repository), and refuses to run while this section exists or any `example.com` dummy
      remains. Rehearse with target `testpypi` first.
- [ ] **Security contact.** Make sure private vulnerability reporting is turned on for the
      repository, since `SECURITY.md` points reporters to it. If the maintainers also want an email
      contact, add it to `SECURITY.md` and `CODE_OF_CONDUCT.md` (which today point to GitHub's
      private reporting and the repository's profile).
- [ ] **Changelog.** In `CHANGELOG.md`, rename `## [Unreleased]` to `## [0.1.0] - <release date>`,
  then add a new, empty `## [Unreleased]` heading above it for the next changes.

## Every release

1. Run the suite on every CEL backend and under `-O`, mypy, and the three example apps' `ci.sh`:

   ```
   for b in subset cel-expr; do INV_CEL_BACKEND=$b python -m pytest tests -q; done
   python -O -m pytest tests -q
   python -m mypy
   for lib in invariants-cel invariants-scan invariants-schema-diff; do
     (cd ../packages/$lib && python -m pytest tests -q && python -m mypy)
   done
   ../apps/pool-service/ci.sh && ../apps/fact-pipeline/ci.sh && ../apps/support-agent/ci.sh
   ```

2. Bump `version` in all four `pyproject.toml` (they share one version) and the `==` pins on the
   libraries in `pyproject.toml` and `packages/invariants-schema-diff/pyproject.toml`
   (`tests/test_fix_round27_split2.py` checks they agree), and date the release in `CHANGELOG.md`.
3. Build the four distributions and check their metadata (CI's `build` job runs the same steps):

   ```
   python -m pip install build twine
   for lib in invariants-cel invariants-scan invariants-schema-diff; do
     python -m build --outdir dist ../packages/$lib
   done
   python -m build --outdir dist .       # dist/*.whl and dist/*.tar.gz, four of each
   python -m twine check --strict dist/*
   ```

4. Install invariants-first's wheel and, separately, its sdist into fresh virtual environments,
   with the libraries taken from the wheels just built (`pip install --find-links dist
   dist/invariants_first-*.whl`). In each, check that `import invariants` works with only the core
   dependencies (PyYAML and the three libraries), from a directory outside the checkout, and that
   `inv --help`, `inv-mcp --help` and `inv-scan --help` run; with the wheel, check that the README
   Quickstart runs as shown, and each library's README example in a venv that holds only it.
5. Tag the release and push the tag. The release workflow does not run on a tag: start it by hand
   (Actions > release > Run workflow), first with target `testpypi`, then with `pypi`. It uploads
   through trusted publishing, the libraries first (cel, scan, schema-diff), then invariants-first.
