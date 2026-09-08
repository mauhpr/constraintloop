# Release readiness

This is the current release checklist, updated September 8, 2026 for v0.5.2.
The authoritative publication procedure is [RELEASE.md](../RELEASE.md).
Historical v0.1 milestones are complete; they are not current release gates.

## Required gates

- A focused release PR includes one version bump, lockfile, changelog,
  generated schema, implementation, regression tests, and migration notes.
- Formatting, lint, strict typing, and the full deterministic test suite pass.
- Enforced coverage remains at least **95% statements and 90% branches**.
- Hosted compatibility checks pass on Python 3.11–3.14, Ubuntu and macOS.
  Windows remains unsupported.
- Lowest-supported and latest-compatible OpenAI/Anthropic SDK checks pass.
- Both wheel and sdist are built and installed into clean environments. The
  installed CLI must exercise deliberate failures and session challenge
  discovery/verification, not only print its version.
- The source-distribution content policy excludes local state and secrets.
- All required PR checks pass before merge. A single GitHub Release targets
  the merged main commit; GitHub OIDC publishes that version with attestations.
- Verify publication and a fresh public-index install afterward. Never replace
  an existing version, publish locally, or add long-lived registry tokens.

## v0.5.2 environment recovery and retry evidence

Regression tests cover missing-executable recovery with unchanged watched inputs,
legacy cache entries, blocked dependents, explicit environment waivers, cache
refresh, and the absence of cache writes during uncached and CI runs. Command,
metric, and ratchet attempts retain output, exit codes, errors, and timing across
success, failure, pending results, parser errors, and retry exhaustion. Timeout
and output-overflow diagnostics remain bounded and redacted. Review cache and
waiver identities include attempt evidence while ignoring volatile timing.

Upgrade the runtime used by hooks, then use `constraintloop run --refresh` when
fresh persisted evidence is needed. Historical attempts cannot be reconstructed.
The existing retry exit-code default remains `[1]`; examples now set an explicit
policy that avoids retrying assertion failures.

## v0.5.1 worktree isolation

Worktree regressions cover linked and detached checkouts, shared cache directories,
branch switches at the same commit, HEAD changes with unchanged watched files,
session goals and retry budgets, local exceptions, loop journals and leases,
checkout changes during evaluation and supervision, nested-project hooks,
legacy-state isolation, and challenge-only loops. See [worktrees](worktrees.md)
for setup and migration instructions. Container resource isolation remains the
consuming project's responsibility.

## v0.5 review closure

The [pre-release review](pre-release-review-2026-09-06.md) records ten reproduced
bugs and their resolution. Tests cover filename/content boundary collisions,
recursive hook budgets, malformed evidence, cross-phase waiver policy,
dependency root causes, completed/unfinished task lifecycles, delayed external
state, invalid numbers, redaction, and observed baseline weakening.

Additional checks cover phase-compatible dependencies, CI overlay isolation,
subprocess output overflow and cleanup, lease renewal/loss, and all three
native hook protocols. Coverage is a floor, not proof of correctness.

## Native-agent compatibility

| Adapter | Protocol fixtures | Local preflight observed September 6 | Interactive live session |
| --- | --- | --- | --- |
| Codex | Stop continuation, fail-closed termination, saved challenges | CLI 0.153.4, authenticated | Not exercised by this release validation |
| Claude Code | Stop continuation, fail-closed termination, saved challenges | CLI 2.1.252, unauthenticated | Not exercised; authentication required |
| Gemini CLI | AfterAgent deny/retry, explicit stop, saved challenges | No CLI preflight recorded | Not exercised by this release validation |

These versions are observations, not minimum-version guarantees. Fixture and
installed-CLI tests do not prove compatibility with a running native host.
Repeat the relevant setup and interactive smoke test when upgrading a host.

Optional live rubric preflight/canary commands are documented in
[native CLI evaluators](native-cli-evaluators.md). They consume provider quota;
no paid model call is required by PR CI or by the session challenge gate.

An optional live session smoke should configure a disposable project with
three challenges, request a tiny implementation, observe Stop/AfterAgent
continuation, submit discovery and verification, change watched source, check
stale-evidence rejection, and exercise budget exhaustion. Record host version,
authentication readiness, and outcomes. Do not use a real project as the
failure fixture.

## Scenario quality, separate from schema validity

Session challenge submissions establish a traceable self-review, not an
independent verdict. Evaluate representative domain-specific sessions for:

- distinct failure mechanisms rather than paraphrases or relabeled scenarios;
- assumptions grounded in actual domain rules and source references;
- concrete triggers and observable expected behavior;
- verification that exercises the stated failure, not merely a passing test ID;
- evidence-backed rejection of inapplicable cases and explicit unresolved risks;
- retained original scenarios and fresh re-verification after repairs.

The runtime enforces counts, references, IDs, phases, snapshots, evidence
verdicts, and budgets. It cannot deterministically establish semantic diversity,
test adequacy, or the truth of a self-authored explanation. Use human review or
a separately configured evaluator where independent assurance is required.
