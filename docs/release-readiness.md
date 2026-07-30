# Open-source release readiness

This document defines what ConstraintLoop should complete before its first
public v0.1 release. It separates mandatory release gates from useful later
improvements so open-source process does not eclipse product validation.

The assessment was last updated on July 29, 2026.

## Current baseline

ConstraintLoop currently has:

- an MIT license and modern `pyproject.toml` package;
- a buildable source distribution and universal wheel;
- 139 passing tests with 90.86% statement and 82.70% branch coverage;
- strict contract validation and deterministic, rubric, cache, waiver, and hook
  tests;
- Claude Code, Codex, and Gemini hook configurations;
- a real, passing OpenAI structured-output evaluation using `gpt-5-nano`;
- a threat model and bounded-convergence-loop implementation with pending
  evidence, strict budgets, atomic journals, leases, cycle/supervisor commands,
  native prompts, and shared Stop-hook accounting;
- a self-contract that passes its deterministic gates.

The public GitHub repository, protected `main` branch, cross-platform CI,
community-health files, production `pypi` environment, and pending Trusted
Publisher are configured. The first production publication and external
installation verification remain.

The PyPI JSON endpoint for `constraintloop` returned 404 during this assessment,
so the name appeared unoccupied. A name is not reserved until the project is
actually registered and published.

## v0.1 release gates

### 1. Establish an authoritative repository

- Create the initial reviewed commit rather than publishing an unreviewed
  generated tree.
- Create the public GitHub repository and record its canonical URL in
  `pyproject.toml`.
- Add project URLs for source, documentation, issues, changelog, and security.
- Choose the default branch and protect it after CI exists.
- Require pull requests and passing checks for changes to the contract, hooks,
  release workflow, and package metadata.
- Add `CODEOWNERS` for these policy-sensitive files when there is more than one
  maintainer.

### 2. Make supported environments truthful

Test every environment the metadata claims to support:

- Python 3.11, 3.12, 3.13, and 3.14;
- Ubuntu and macOS;
- Windows, or explicitly document Windows as unsupported in v0.1.

Windows is currently the largest portability risk. Hook commands and generated
virtual-environment paths use POSIX shell and `.venv/bin`. Either implement
Windows-specific commands and tests or narrow the support statement.

The v0.1 decision is Linux and macOS support. Windows is explicitly unsupported
until locking, process control, and hook command generation have Windows-native
implementations and CI coverage.

The CI matrix must install the built wheel in a clean environment and invoke the
console entry point. Testing only the editable source tree is insufficient.

### 3. Increase test depth

Set the v0.1 target to at least 90% statement coverage and 80% branch coverage.
Coverage is a floor, not proof of correctness.

Required suites:

- unit tests for every command, metric operator/parser, and artifact format;
- dependency-DAG ordering and failed-dependency behavior;
- cache freshness for edits, deletes, renames, symlinks, and contract changes;
- waiver binding, corruption, and CI bypass resistance;
- secret parsing, permissions, redaction, and agent-write denial;
- provider fixtures for pass, fail, uncertain, refusal, malformed output,
  timeout, rate limit, empty output, and retry exhaustion;
- golden input/output fixtures for every native hook event and adapter;
- setup merge/idempotency tests with pre-existing user hook configuration;
- subprocess tests against an installed wheel;
- concurrent evidence writes and atomic-state recovery;
- path traversal, symlink escape, shell opt-in, oversized output, and hostile
  repository-content tests;
- property-based or fuzz tests for YAML contracts and evaluator JSON;
- interruption tests for timeouts and process termination;
- loop tests listed in `docs/convergence-loops.md`;
- one opt-in live-provider smoke test that is never required for forked pull
  requests.

Add mutation testing after the deterministic suite reaches the coverage target.
Use surviving mutations to improve behavioral assertions, not merely to raise a
score.

### 4. Add deterministic development quality gates

The repository should enforce:

- formatting and linting;
- static type checking;
- tests and branch coverage;
- package build;
- wheel installation smoke test;
- documentation link/config-example validation;
- secret scanning;
- dependency vulnerability review.

These gates should run without API keys. The OpenAI dogfood rubric remains
advisory and should not make ordinary contributor CI depend on a paid service.

### 5. Complete user and contributor documentation

Add:

- `CONTRIBUTING.md` with environment setup, commands, test expectations,
  architecture boundaries, and pull-request process;
- `SECURITY.md` with supported versions, private disclosure channel, response
  expectations, and scope;
- `CODE_OF_CONDUCT.md` only after choosing a real enforcement contact;
- `SUPPORT.md` separating usage questions, bugs, feature requests, and security
  reports;
- `GOVERNANCE.md` describing maintainers, decision making, releases, and policy
  changes;
- `CHANGELOG.md` and an explicit semantic-versioning policy;
- issue forms and a pull-request template;
- a configuration reference covering every strict schema field and default;
- installation/uninstallation and hook-trust instructions per agent;
- Windows support or limitation documentation;
- evaluator data-flow and privacy documentation explaining exactly which goal,
  diff, files, findings, and metadata leave the machine;
- provider cost controls, failure modes, and key-rotation instructions;
- a compatibility table for tested Claude, Codex, and Gemini versions;
- architecture and extension guides for adding constraints and evaluators;
- migration guidance for contract schema changes.

Examples must be parsed as part of CI so documentation cannot drift from the
strict model.

### 6. Finish the promised first-release loop

The README already places bounded convergence loops in v0.1 scope. Before
release, either implement the acceptance criteria in
`docs/convergence-loops.md` or move the feature to a clearly labeled roadmap.

The required implementation boundary remains:

- ConstraintLoop owns state, evidence, locks, budgets, and stopping;
- native agents perform at most one repair per transition;
- unchanged polling never invokes a model;
- no provider CLI launching for repair turns in v0.1; opt-in native rubric
  evaluators must remain isolated, read-only, bounded, and advisory by default;
- no unbounded mode.

Do not advertise commands that do not exist.

### 7. Secure dependency and contribution workflows

For a public GitHub repository, enable:

- dependency graph and Dependabot alerts;
- Dependabot security and version updates for Python and GitHub Actions;
- secret scanning and push protection;
- code scanning or an equivalent static security analysis;
- least-privilege `GITHUB_TOKEN` permissions;
- immutable action pins for sensitive release/security workflows;
- branch protection after required checks are stable.

Run OpenSSF Scorecard after the repository is public and fix high-risk findings.
Begin the OpenSSF Baseline/Best Practices self-assessment; earning a badge is
useful but not a v0.1 blocker.

### 8. Build a trusted release process

- Build both sdist and wheel in CI.
- Inspect the contents and install each artifact in a clean environment.
- Install both locally built artifacts in clean environments before publishing.
- Publish to PyPI using Trusted Publishing rather than a long-lived PyPI token.
- Attach PyPI publish attestations/provenance.
- Generate release notes from the changelog.
- Tag releases and never replace the contents of an existing version.
- Document rollback/yank and security-release procedures.
- Keep provider credentials out of build and release jobs.

PyPA recommends Trusted Publishing for supported CI platforms, and PyPI can
attach attestations binding release files to the publishing workflow.

## Public API and compatibility

The README now declares the supported v0.1 surfaces:

- the CLI and its exit codes;
- `constraintloop.yml` schema and defaults;
- evaluator command JSON protocol;
- hook adapter output protocol;
- Python submodules are explicitly internal and are not package-root exports;
- evidence and cycle JSON schemas.

Version these surfaces deliberately. Under semantic versioning, `0.y.z` is
initial development and may change, but users still need migration notes.
Schema-bearing JSON should always include `schema_version`.

## Security-specific requirements

The existing threat model is a useful start. Before release, add tests and
documentation for:

- hooks as guardrails rather than a security boundary;
- command execution and explicit shell opt-in;
- repository prompt injection into rubric bundles;
- source-code disclosure to remote evaluators;
- secret exclusion and output redaction;
- malicious symlinks and file replacement races;
- cache/waiver/state tampering;
- CI isolation from local waivers and evidence;
- compromised evaluator output and provider outages;
- denial-of-wallet through repeated or oversized evaluations;
- dependency and release-workflow compromise.

Add a machine-readable `security-insights.yml` later if external consumers need
standardized security posture metadata.

## Suggested execution order

### Milestone A: trustworthy contributor baseline

1. Create initial commit and GitHub repository.
2. Add CI matrix, lint, typing, coverage, wheel smoke test, and docs validation.
3. Raise coverage to the v0.1 target.
4. Add community and security health files.
5. Complete configuration/privacy/troubleshooting documentation.

### Milestone B: bounded loop implementation

Implemented locally: pending verdicts, strict loop models, shared attempt
accounting, atomic journals, recoverable leases, `cycle`, deterministic
`supervise`, native prompt generation, and loop tests. Remaining before the
release tag:

1. Add a bounded completion loop to the repository's protected configuration
   through a human-reviewed change.
2. Dogfood repair, unchanged-repair, and budget-exhaustion transitions.
3. Confirm cancellation and installed-wheel behavior on the supported platform
   matrix.

### Milestone C: public release

1. Run security and compatibility audits.
2. Confirm clean wheel and sdist installation jobs pass.
3. Configure Trusted Publishing and attestations.
4. Publish v0.1.0 and verify a clean external installation.
5. Enable Scorecard and begin OpenSSF baseline self-assessment.

## Source standards

This plan is based on:

- [PyPA packaging flow](https://packaging.python.org/en/latest/flow/) and
  [tool recommendations](https://packaging.python.org/en/latest/guides/tool-recommendations/);
- [PyPI digital attestations](https://docs.pypi.org/attestations/);
- [GitHub community health files](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file)
  and [community profile](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories);
- [GitHub public-repository security recommendations](https://docs.github.com/en/enterprise-cloud@latest/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-security-and-analysis-settings-for-your-repository);
- [OpenSSF Scorecard](https://www.scorecard.dev/),
  [Best Practices criteria](https://www.bestpractices.dev/en/criteria), and
  [OSPS Baseline](https://baseline.openssf.org/);
- [Semantic Versioning 2.0.0](https://semver.org/);
- [REUSE Specification](https://reuse.software/spec/) for optional per-file
  licensing compliance.
