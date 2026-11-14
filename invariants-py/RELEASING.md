# Releasing

## Before publishing

The project has not been published yet. Some metadata is deliberately left open, because none of
it is known yet and none of it should be invented. **Every unticked item below is a release
blocker**: do not upload to PyPI (or TestPyPI) until each one is settled by the project owner, then
delete this section. Three of them are placeholders in `pyproject.toml` today and must not ship as
they are:

| Field in `pyproject.toml` | Today                                   | Owner must supply                  |
|---------------------------|-----------------------------------------|------------------------------------|
| `[project] name`          | `invariants-first` (placeholder)        | the final distribution name        |
| `[project] authors`       | "The invariants-first authors"          | the names and contacts to publish  |
| `[project.urls]`          | absent (no URL is invented)             | Homepage, Source, Issues, Changelog |

CI's `twine check --strict` passes with these placeholders (it checks that the metadata and README
render, not that they are final), so a green build is not a sign that this list is done.

- [ ] **Final package name.** `invariants-first` in `pyproject.toml` (`[project] name`) is a
      placeholder. Pick the distribution name, check that it is free on PyPI (and on TestPyPI), and
      update every place that names it (`git grep -n invariants-first` lists them):
      `pyproject.toml`; `src/invariants/__init__.py` (`_metadata.version(...)`); the install hints in
      `src/invariants/cel/expr_python.py` and `src/invariants/integrations/anthropic.py`; the README
      (the Quickstart install line and its placeholder note); `docs/cookbook.md`, `docs/start.md`,
      `docs/models.md` and `docs/design-guide.md` (line 4, and its copy `Invariant_first.md` at the
      monorepo root, kept byte-identical); the monorepo root `README.md` and `makeInvariantSimple.md`;
      the three apps (`apps/pool-service`, `apps/fact-pipeline`, `apps/support-agent`: each
      `pyproject.toml`, their dependency on the library, and each `README.md`); and
      `tests/test_package.py` and `tests/test_integrations_anthropic.py`. The import name stays
      `invariants`.
- [ ] **`[project.urls]`.** Add `Homepage`, `Source`, `Issues` and `Changelog` once the repository
      has its public URL. `tests/test_fix_round6.py` checks that no URL is invented before then;
      update that test in the same change.
- [ ] **README badges.** The badges are static shields that link to files in this repository; the
      CI badge says "GitHub Actions" whatever the last run did, so it must be swapped for a live one.
      Once the repository has its public URL, point the CI badge at the workflow's status badge
      (`.../actions/workflows/ci.yml/badge.svg`) and add a PyPI version badge.
- [ ] **SARIF tool URL.** `inv scan --format sarif` reports `informationUri` as a placeholder
      (`https://example.invalid/invariants`, in `src/invariants/scan/sarif.py`); set it to the project
      URL.
- [ ] **Authors and maintainers.** `authors` in `[project]` is the placeholder "The
      invariants-first authors". Replace it (and add `maintainers` if they differ) with the names
      and contact addresses the maintainers choose to publish. The `LICENSE` appendix's copyright line is
      the Apache template and needs no change, but a `NOTICE` file may name the copyright holder.
- [ ] **PyPI trusted publishing.** Create the project on PyPI (and TestPyPI), register this
      repository's release workflow (`.github/workflows/release.yml`, environments `testpypi` and
      `pypi`) as a trusted publisher, and create those two GitHub environments (required reviewers
      on `pypi`). The workflow is a draft that runs only by hand; it builds the sdist and wheel and
      uploads them with `pypa/gh-action-pypi-publish` using OpenID Connect (no API token stored in
      the repository), and refuses to run while this section exists or the SARIF URL is the
      placeholder. Rehearse with target `testpypi` first.
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
   ../apps/pool-service/ci.sh && ../apps/fact-pipeline/ci.sh && ../apps/support-agent/ci.sh
   ```

2. Bump `version` in `pyproject.toml` and date the release in `CHANGELOG.md`.
3. Build both distributions and check their metadata (CI's `build` job runs the same steps):

   ```
   python -m pip install build twine
   python -m build                       # dist/*.whl and dist/*.tar.gz
   python -m twine check --strict dist/*
   ```

4. Install the wheel and, separately, the sdist (`pip install dist/*.tar.gz`) into fresh virtual
   environments. In each, check that `import invariants` works with only the core dependency
   (PyYAML), from a directory outside the checkout, and that `inv --help` and `inv-mcp --help` run;
   with the wheel, check that the README Quickstart runs as shown.
5. Tag the release and push the tag. The release workflow does not run on a tag: start it by hand
   (Actions > release > Run workflow), first with target `testpypi`, then with `pypi`. It uploads
   through trusted publishing.
