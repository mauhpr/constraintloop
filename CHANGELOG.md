# Changelog

All notable changes are documented here. The project follows Semantic
Versioning, with the usual initial-development flexibility for `0.y.z`.

## [Unreleased]

## [0.2.0] - 2026-08-07

- Preserve selected monorepo project paths in generated hooks, support explicit
  persistent hook executables, and pin ephemeral `uvx` hook invocations.
- Add gitignored, strengthening-only local contract overlays while keeping CI
  authoritative, and automatically protect local state from Git tracking.
- Stream constraint start, heartbeat, retry, cache reuse, and completion status
  during long human-readable runs.
- Add bounded per-command transient retry policies for exit codes, startup
  failures, and timeouts with explicit total budgets.
- Add read-only `doctor --deep` diagnostics for executables, Python invocation,
  environment variables/files, empty watch globs, and state hygiene.
- Recommend isolated `uv tool` or `pipx` installation to avoid project dependency
  conflicts.

## [0.1.0] - 2026-07-29

- Added the initial evidence engine, strict contract schema, provider adapters,
  native agent hooks, contributor quality configuration, and CI baseline.
- Added deterministic OpenAI request-contract tests, refusal and incomplete
  response handling, safe call metadata, SDK compatibility checks, and a
  versioned opt-in semantic evaluation corpus.
- Added isolated native Codex and Claude Code command evaluators with
  hook-aware same-agent preference and strict structured output.
- Added bounded convergence loops with pending evidence, strict budgets, atomic
  journals, recoverable supervisor leases, stable cycle exit codes, native
  prompts, and shared Stop-hook attempt accounting.
- Declared the v0.1 compatibility boundary around CLI and versioned protocols;
  Python submodules remain internal during initial development.
- Added snapshot-bound advisory acknowledgments so Stop feedback must be
  addressed or explicitly explained before completion can continue.
- Added canonical package metadata, a build-once PyPI Trusted Publishing
  workflow, release invariants, CODEOWNERS, and dependency automation.
- Hardened project-bound and reversible hook setup, malformed hook/settings
  handling, exact-result deterministic waivers, complete-bundle rubric caching,
  rename disclosure, and evaluator-scoped secret loading and redaction.
- Added Contributor Covenant 2.1 with private conduct reporting instructions.
