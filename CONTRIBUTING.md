# Contributing

By participating, you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Report conduct concerns privately using
the contact method documented there.

ConstraintLoop accepts focused changes that strengthen evidence, state,
budgeting, locks, and stopping. It is not a general agent runtime and v0.1 must
not launch provider CLIs for repair turns or offer unbounded repair loops.
Opt-in native CLI evaluators are limited to isolated, tool-disabled reviews.

Use Python 3.11 or newer and install the development environment:

```bash
uv sync --extra dev --extra openai
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest --cov=constraintloop --cov-branch
uv build
```

Tests must be deterministic and must not require provider credentials. Mock
provider success, refusal, malformed output, timeout, and retry exhaustion.
Live-provider checks are opt-in and never required for pull requests.

Hook installation must be reversible and preserve user configuration.
`constraintloop uninstall` removes only ConstraintLoop-owned entries; changes
to setup or uninstall behavior require merge, idempotency, and preservation
tests.

Failure behavior is tested with isolated temporary projects and fake evaluator
commands. Add scenarios through `tests/failure_lab.py`; do not add a public
failure-simulation flag or require a live provider merely to exercise an error
path.

Pull requests should explain the behavior and failure cases, add tests, update
user-facing documentation, and avoid unrelated formatting. Changes to the
contract schema, hooks, package metadata, or release workflows need especially
careful review. Do not commit local evidence, waivers, or secrets.

Releases are prepared through a focused release pull request and published only
through GitHub Trusted Publishing. See `RELEASE.md`. Contributors and agents
must not run local package upload commands or add long-lived registry tokens.

The supported v0.1 platforms are macOS and Linux. Windows is not supported
until hook command generation and clean-wheel tests are implemented there.
