# Pre-release review — September 6, 2026

Status: all ten findings below are fixed in the v0.5.0 release changes. The
original reproduction descriptions are preserved as the audit record; they
describe the pre-fix worktree, not current behavior.

Regression coverage is in
[test_review_regressions.py](../tests/test_review_regressions.py),
[test_process.py](../tests/test_process.py), and
[test_challenges.py](../tests/test_challenges.py).
The fixes also include dependency phase validation, bounded subprocess output,
lease heartbeats, CI overlay isolation, and refreshed release documentation.
See [completion policy](completion-policy.md) for behavior and migration notes,
and [release readiness](release-readiness.md) for the native-host test matrix
and the separate scenario-quality assessment checklist.

The review covered deterministic commands, metrics, artifacts, ratchets,
dependencies, evidence caching, waivers, overlays, native evaluator isolation,
hooks, and convergence-loop state and budgets. Thirteen isolated reproduction
checks confirmed the ten findings below on the current working tree. These
checks intentionally assert the faulty behavior; they are diagnostic evidence,
not passing regression tests for the intended behavior.

The session challenge feature was implemented separately in this worktree for
Claude Code, Codex, and Gemini CLI. It remains opt-in through `loops.NAME.challenge`.
Its tests cover discovery, verification, source freshness, replay rejection,
repairs, follow-up rounds, restart recovery, and all three hook protocols.
All existing-feature findings below are now closed by implementation and
regression tests. No live model call is required by the new session gate.

## P1 — fix before release

### 1. Ambiguous file hashing can reuse a pass for a deleted artifact

Location: [digest.py](../src/constraintloop/digest.py#L81),
[engine.py](../src/constraintloop/engine.py#L222).

The digest concatenates each filename and its contents without lengths or
separators. A file named `a` containing `bc` hashes identically to a file named
`ab` containing `c`, under the same contract and watch patterns. A required
artifact at `a` passes, then still returns cached PASS after `a` is renamed to
`ab` and its contents changed to `c`. An uncached run correctly fails.

Fix: hash an unambiguous, versioned sequence of file paths and content digests
(or length-prefixed fields). Invalidate old cached evidence and waivers when
the digest format changes. Add rename/content-boundary property tests.

### 2. Ordinary recursive completion hooks bypass required failures

Location: [hooks.py](../src/constraintloop/hooks.py#L104).

For a contract without the new challenge gate, the first completion event blocks
a failing required command. The next event with `stop_hook_active: true`
returns `{}` even when the failure is unchanged. This reproduces for Claude,
Codex, and Gemini. The behavior predates this change: the new challenge feature
uses bounded continuations, but the legacy guard remains for other contracts.

Fix: apply journal-backed bounded continuation handling to ordinary gates too.
At exhaustion, stop explicitly for human intervention; do not silently allow
completion without evaluating the required evidence.

### 3. Malformed evidence can crash a hook without returning a blocking decision

Location: [runners.py](../src/constraintloop/runners.py#L367),
[runners.py](../src/constraintloop/runners.py#L493),
[hooks.py](../src/constraintloop/hooks.py#L165).

Malformed JUnit XML raises `xml.etree.ElementTree.ParseError`, which the artifact
runner does not catch. A JSON metric whose selected value is `null` raises an
uncaught `TypeError` at `float(value)`. Invoking the hook CLI in either case exits
1 with empty stdout instead of structured blocking JSON. This can become a
non-blocking hook warning: Gemini documents nonzero exit codes other than 2 as
non-fatal. See its [hook exit-code rules](https://geminicli.com/docs/hooks/reference/#global-hook-mechanics).

Fix: normalize malformed artifact/metric data into explicit results, validate
numeric types, and put a final exception boundary around completion-hook
evaluation that returns the adapter's explicit stop response. Test exceptions
through the actual hook CLI, including the exit code and JSON output.

### 4. Push loops honor waivers that the regular push command rejects

Location: [loops.py](../src/constraintloop/loops.py#L200),
[cli.py](../src/constraintloop/cli.py#L166).

`run --phase push` disables local waivers. A loop configured with `phase: push`
uses `allow_waivers = phase != ci`, so the same failing evidence with a local
waiver produces `passed` through `cycle`. Completion authority changes depending
on which entry point is used.

Fix: centralize phase policy and disable waivers for both push and CI across
every execution path. Add a cross-entry-point policy matrix.

## P2 — reliability and policy gaps

### 5. A failed prerequisite prevents the loop from offering a repair

Location: [engine.py](../src/constraintloop/engine.py#L142),
[loops.py](../src/constraintloop/loops.py#L232).

With `tests.needs: [syntax]`, a syntax failure produces `syntax=fail` and
`tests=error`. The loop treats every required ERROR as unreliable evaluation and
returns `error`, rather than requesting repair of the syntax failure. This also
affects challenge-enabled loops because prerequisites run before discovery.

Fix: distinguish dependencies blocked by a repairable failure from evaluation
errors, and return the root constraints that actually require repair. Preserve
blocking behavior when an advisory prerequisite prevents a required dependent
gate from running.

### 6. Ordinary loop budgets leak across completed tasks

Location: [loops.py](../src/constraintloop/loops.py#L154),
[loops.py](../src/constraintloop/loops.py#L275).

A normal completion loop passes at time 100 with a 600-second duration budget.
A new task changes its source and fails at time 800; the loop immediately
returns `budget_exhausted` with zero repairs. Its journal was never reset after
the previous task completed. The new challenge path resets completed runs on
changed inputs/goals, but ordinary loops still retain the prior task's budget.

Fix: define an explicit run/task lifecycle for every loop. Starting a new task
after completion should receive a new budget; restarting an unfinished task
must preserve its existing limits.

### 7. Hook-driven monitoring can retain PENDING after external work finishes

Location: [hooks.py](../src/constraintloop/hooks.py#L165),
[engine.py](../src/constraintloop/engine.py#L223).

An external-status command first returns 75, then 0 without a source change.
Repeated completion hooks reuse cached PENDING because they construct the
record without `refresh_pending=True`. A direct cycle after the polling interval
correctly refreshes it and passes. The hook supplies its cached record to the
cycle, bypassing that cycle's refresh behavior.

Fix: share the evaluation path between hooks and cycles, including pending
refresh and interval rules. Test delayed external state independently of source
changes.

### 8. Non-finite metric values can satisfy required thresholds

Location: [runners.py](../src/constraintloop/runners.py#L493),
[models.py](../src/constraintloop/models.py#L138).

A metric value of the JSON string `"Infinity"` becomes positive infinity and
passes `gte: 95`. Measurements, thresholds, and stored baselines do not require
finite numeric values. Invalid measurements can therefore produce apparently
successful evidence.

Fix: reject non-finite values and booleans before comparison and baseline
updates. Decide explicitly whether numeric strings are supported. Apply the
same validation to parsed measurements and persisted baselines.

### 9. Deterministic command output is retained without secret redaction

Location: [runners.py](../src/constraintloop/runners.py#L509),
[engine.py](../src/constraintloop/engine.py#L287).

A test command printing a fake `password=...` retains the marker unchanged in
`output_tail` and `.constraintloop/state/evidence.json`. The runner truncates
output but does not redact it before persistence or hook summaries. Evaluator
bundle redaction does not protect these local evidence and feedback paths.
Only a synthetic marker was used to reproduce this; no real secret was read.

Fix: define one redaction boundary before retained evidence and user/agent
feedback. Apply it consistently to command logs, parser errors, and structured
artifact fields. Document the limits of pattern-based redaction.

### 10. Observed agent commands can weaken ratchet baselines

Location: [hooks.py](../src/constraintloop/hooks.py#L409),
[cli.py](../src/constraintloop/cli.py#L493).

The pre-tool hook permits `constraintloop baseline update --all --allow-regression`.
It blocks the waiver command but does not recognize baseline weakening as a
quality-policy mutation. The reproduction checked the permission response only;
it did not modify any real baseline.

Fix: treat observed baseline weakening like an observed waiver or contract
edit. Preserve the ability to tighten a baseline when authorized. This is a
consistency fix for the existing guard, not a claim that local hooks can
authenticate a human or sandbox a hostile agent.

## Other improvement opportunities (original review)

Concrete runtime and documentation items are implemented for v0.5.0. Native
interactive smoke runs remain explicitly optional and unclaimed, and semantic
scenario quality remains a separate assessment rather than a deterministic
schema guarantee; both now have documented validation procedures.

- Centralize completion policy across `run`, `cycle`, hooks, and CI. Document
  which commands check constraints and which decide task completion; include
  advisory dispositions, overlays, waivers, and pending evidence in the matrix.
- Validate dependency phase compatibility. The schema currently permits a
  dependent gate in a phase where its prerequisite will not run.
- Bound subprocess output while collecting it. `run_bounded` currently uses
  `communicate()` to collect everything before retained-output truncation, so
  the configured output limit is not a memory bound.
- Renew supervisor leases during long evaluations. The lease lifetime is based
  on the polling interval, while individual configured checks can run much
  longer. The journal lock serializes cycles, but lease ownership can still
  expire while a supervisor is evaluating.
- Add property/fuzz tests for digest framing, malformed evidence, phase/dependency
  combinations, and lifecycle sequences. High line coverage did not detect the
  reproductions above.
- Maintain a tested native-agent version matrix with optional live smoke runs.
  Local preflight found Codex CLI 0.153.4 ready and Claude Code 2.1.252 installed
  but unauthenticated. Hook protocol fixtures passed for all three agents;
  interactive sessions and external model evaluations were not exercised.
- Refresh release documentation: `docs/release-readiness.md` still describes a
  v0.1 baseline and old coverage targets, while package metadata is at 0.4.1.
- For the new challenge feature, evaluate scenario quality separately from
  submission validity. Distinct strings and perspective labels do not establish
  distinct failure mechanisms; test citations do not prove that a test exercises
  the stated scenario. This remains explicit self-review, with evidence and
  traceability rather than independent judgment.

## Validation record (before fixes)

- Repository test suite: 262 passed on macOS / Python 3.12.
- Statement coverage: 95.63%; branch coverage: 91.10%. Both enforced floors pass.
- Formatting, lint, and strict type checks passed.
- The generated contract schema matches the authoritative models.
- Offline source-distribution and wheel builds passed. The source-distribution
  content policy passed, and the wheel contains the new challenge module.
- Thirteen temporary diagnostic checks reproduced the ten existing issues.
- No live model requests, remote writes, package publication, or releases were
  performed for this review.

## Post-fix validation

- 337 tests passed locally on macOS / Python 3.12, before the final artifact
  smoke additions; statement coverage 95.86%, branch coverage 91.47%.
- Formatting, lint, strict typing, and schema checks passed.
- The release PR and publishing workflow provide the final hosted matrix,
  artifact-install, and publication records.
