# Releasing

## Before publishing

The project has not been published yet. Some metadata is deliberately left open, because none of
it is known yet and none of it should be invented. Settle each item below before the first upload
to PyPI, then delete this section.

- [ ] **Final package name.** `invariants-first` in `pyproject.toml` (`[project] name`) is a
      placeholder. Pick the distribution name, check that it is free on PyPI (and on TestPyPI), and
      update every place that names it: `pyproject.toml`, `src/invariants/__init__.py`
      (`_metadata.version(...)`), the README (the Quickstart install line and its placeholder
      note), `docs/cookbook.md`, both `apps/*/pyproject.toml` at the monorepo root (their dependency on the
      library), and `tests/test_package.py`. The import name stays `invariants`.
- [ ] **`[project.urls]`.** Add `Homepage`, `Source`, `Issues` and `Changelog` once the repository
      has its public URL. `tests/test_fix_round6.py` checks that no URL is invented before then;
      update that test in the same change.
- [ ] **README badges.** The badges are static shields that link to files in this repository; the
      CI badge says "GitHub Actions" whatever the last run did, so it must be swapped for a live one.
      Once the repository has its public URL, point the CI badge at the workflow's status badge
      (`.../actions/workflows/ci.yml/badge.svg`) and add a PyPI version badge.
- [ ] **SARIF tool URL.** `inv scan --format sarif` reports `informationUri` as a placeholder
      (`https://example.invalid/invariants`, in `src/invariants/scan.py`); set it to the project
      URL.
- [ ] **Authors and maintainers.** `authors` in `[project]` is the placeholder "The
      invariants-first authors". Replace it (and add `maintainers` if they differ) with the names
      and contact addresses the maintainers choose to publish. The `LICENSE` appendix's copyright line is
      the Apache template and needs no change, but a `NOTICE` file may name the copyright holder.
- [ ] **PyPI trusted publishing.** Create the project on PyPI (and TestPyPI), register this
      repository's release workflow as a trusted publisher, and add a release job to
      `.github/workflows/` that builds the sdist and wheel and uploads them with
      `pypa/gh-action-pypi-publish` using OpenID Connect (no API token stored in the repository).
- [ ] **Security contact.** Make sure private vulnerability reporting is turned on for the
      repository, since `SECURITY.md` points reporters to it. If the maintainers also want an email
      contact, add it to `SECURITY.md` and `CODE_OF_CONDUCT.md` (which today point to GitHub's
      private reporting and the repository's profile).
- [ ] **Changelog.** In `CHANGELOG.md`, rename `## [Unreleased]` to `## [0.1.0] - <release date>`,
  then add a new, empty `## [Unreleased]` heading above it for the next changes.

## Every release

1. Run the suite on every CEL backend and under `-O`, mypy, and both example apps' `ci.sh`:

   ```
   for b in subset cel-expr; do INV_CEL_BACKEND=$b python -m pytest tests -q; done
   python -O -m pytest tests -q
   python -m mypy
   ../apps/pool-service/ci.sh && ../apps/fact-pipeline/ci.sh
   ```

2. Bump `version` in `pyproject.toml` and date the release in `CHANGELOG.md`.
3. Build with `python -m build` and install the wheel into a fresh virtual environment; check that
   `inv --help` runs, `import invariants` works with only the core dependency (PyYAML), and the
   README Quickstart runs as shown.
4. Tag the release; the release workflow publishes it through trusted publishing.
