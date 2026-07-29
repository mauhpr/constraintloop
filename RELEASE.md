# Release process

ConstraintLoop publishes from GitHub Actions with PyPI Trusted Publishing.
Never publish from a local machine and never store a PyPI token in GitHub.

## One-time repository setup

1. Create the public repository as `mauhpr/constraintloop` with `main` as the
   default branch.
2. Push an initial human-reviewed commit and let every CI job pass.
3. Create GitHub environments named `testpypi` and `pypi`. Require maintainer
   approval for `pypi`; restrict both environments to the default branch or
   protected release tags.
4. Enable branch protection for `main`:
   - require pull requests and conversation resolution;
   - require every CI job;
   - block force pushes and branch deletion;
   - require CODEOWNERS review for policy and release files.
5. Enable dependency alerts, Dependabot, secret scanning with push protection,
   private vulnerability reporting, and code scanning.

## Trusted publisher setup

Create pending publishers before the first upload. A pending publisher does not
reserve the package name until the first successful publication.

Production PyPI:

- PyPI project name: `constraintloop`
- GitHub owner: `mauhpr`
- Repository: `constraintloop`
- Workflow: `publish.yml`
- Environment: `pypi`

TestPyPI:

- Project name: `constraintloop`
- GitHub owner: `mauhpr`
- Repository: `constraintloop`
- Workflow: `test-publish.yml`
- Environment: `testpypi`

## Release checklist

1. Prepare a release pull request from `release/vX.Y.Z-description`.
2. Update the version in `pyproject.toml` and
   `src/constraintloop/__init__.py`, then update `uv.lock` and move relevant
   changelog entries from Unreleased into the new version.
3. Run:

   ```bash
   uv lock --check
   uv run ruff format --check .
   uv run ruff check .
   uv run mypy
   uv run pytest --cov=constraintloop --cov-branch --cov-report=json:coverage.json
   uv run python scripts/check_coverage.py coverage.json
   uv build
   uv run python scripts/check_sdist_contents.py dist/*.tar.gz
   ```

4. Merge only after the complete GitHub CI matrix passes.
5. For the first release, run the `Publish to TestPyPI` workflow and verify a
   clean installation before production.
6. Create a GitHub Release targeting the final `main` commit with tag
   `vX.Y.Z`. Publishing the release triggers `publish.yml`.
7. Confirm the workflow uploaded the single build artifact through OIDC and
   that PyPI displays attestations for both distributions.
8. Verify externally:

   ```bash
   uv tool install --upgrade --reinstall constraintloop
   constraintloop --version
   ```

9. Confirm the GitHub release, PyPI metadata, changelog links, and installation
   instructions all point to the published version.

## Release invariants

- Build once, transfer the immutable artifact between jobs, and publish that
  exact artifact.
- Only the publishing job receives `id-token: write`.
- Do not use `uv publish`, Twine upload, or a local API token.
- Do not reuse, move, or replace a published tag or version.
- If a release is wrong, publish a new patch version; yank only when leaving the
  release installable would materially harm users.
- Release notes describe user-visible behavior, compatibility changes, security
  impact, and migrations.
