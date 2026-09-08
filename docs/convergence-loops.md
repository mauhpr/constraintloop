# Bounded convergence loops

Bounded convergence loops are part of the ConstraintLoop v0.1 release scope.
They connect completion evidence to repeated agent work and delayed external
state without turning ConstraintLoop into another general-purpose agent
runtime.

## Product boundary

ConstraintLoop owns the state machine, evidence, budgets, locking, and stop
decision. Claude Code, Codex, or Gemini CLI may perform a repair after
ConstraintLoop requests one. A native scheduler may wake the workflow.

Neither the agent nor the scheduler decides that work is complete. Only a fresh
contract evaluation can do that.

The first release will not launch provider CLIs for repair turns, manage
provider credentials, or implement a general autonomous task queue. An
explicitly configured command evaluator may invoke one isolated, read-only
Codex or Claude Code review and return the existing evaluator protocol. The
loop engine itself remains provider-neutral.

## Loop types

### Completion

Runs during an active coding session. A failed required gate produces one
focused repair instruction. Passing evidence stops the loop once any configured
session challenge gate is also satisfied.

### Monitor

Polls delayed deterministic evidence such as CI, deployments, integration
environments, or review state. Unchanged pending evidence waits without calling
a model.

### Maintenance

Starts an independent contract run on a recurring schedule, usually through
Claude/Codex scheduled tasks or CI. Examples include mutation testing,
dependency audits, and coverage regression checks.

## Contract shape

The v0.1 schema is:

```yaml
loops:
  completion:
    phase: stop
    interval_seconds: 10
    max_repair_attempts: 3
    max_unchanged_repairs: 2
    max_duration_seconds: 1200
    on_pass: stop
    on_failure: repair
    on_pending: wait
    on_budget_exhausted: human_required

  ci_watch:
    phase: ci
    interval_seconds: 120
    max_repair_attempts: 2
    max_unchanged_repairs: 1
    max_duration_seconds: 2700
    on_pass: stop
    on_failure: repair
    on_pending: wait
    on_budget_exhausted: human_required
```

All fields are explicit and schema-validated. There is no unbounded mode.

Commands that monitor asynchronous state can declare pending exit codes. The
default convention reserves exit code `75` for a temporary pending result:

```yaml
constraints:
  deployment:
    kind: command
    command: [scripts/deployment-status]
    success_codes: [0]
    pending_codes: [75]
    phases: [stop, ci]
```

Pending is neither pass nor failure. It never authorizes completion and never
consumes a repair attempt.

## Cycle protocol

`constraintloop cycle NAME --json` executes exactly one transition. It does not
sleep and does not launch an agent.

```json
{
  "schema_version": 1,
  "loop": "ci_watch",
  "state": "repair",
  "snapshot": "sha256:...",
  "observation": 4,
  "repair_attempt": 1,
  "next_action": "Repair the failing required constraints, then run one cycle.",
  "wake_after_seconds": 0,
  "blocking_constraints": ["integration_tests"]
}
```

States are:

- `passed`: fresh required evidence passes; stop.
- `repair`: new repairable failure; allow one focused agent turn.
- `waiting`: evidence is pending or unchanged without an attempted repair.
- `human_required`: unchanged repairs or policy require a human decision.
- `budget_exhausted`: attempts or elapsed time reached a hard limit.
- `error`: ConstraintLoop could not evaluate the contract reliably.
- `challenge`: the current coding session must generate domain-grounded scenarios.
- `verify`: the current coding session must investigate recorded scenarios and
  submit evidence for unresolved or stale outcomes.

Exit codes will remain stable for automation:

| Code | State |
| ---: | --- |
| 0 | passed |
| 10 | repair |
| 11 | waiting |
| 12 | human required |
| 13 | budget exhausted |
| 14 | engine error |
| 15 | challenge discovery |
| 16 | challenge verification |

## Supervisor

`constraintloop supervise NAME` repeatedly calls the same state machine while
holding a project lease. It polls deterministic constraints locally and emits
JSON Lines when state changes.

The supervisor does not launch coding agents. On `repair`, `challenge`, or
`verify`, it exits with code 10, 15, or 16 so the native session or another
explicit controller can perform the requested work. Optional native rubric evaluators are
single-shot, tool-disabled reviews and cannot perform a repair transition.

The supervisor:

- use a single-writer lease in the active state directory's `loops/` subdirectory
  (`doctor` reports the directory; Git checkouts separate state by worktree and branch);
- recover leases after a bounded TTL;
- handle cancellation signals;
- never count polling observations as repair attempts;
- avoid rerunning unchanged expensive evidence before its configured interval;
- journal transitions atomically;
- redact secrets and cap retained output;
- stop at every attempt and duration budget.

Supervision stops if the Git checkout changes while it is running. Start a new
supervisor in the intended worktree so it reloads that checkout's contract.

## Native integration

`constraintloop loop-prompt NAME --adapter claude|codex|gemini` prints a durable
prompt that tells the native agent to:

1. run one cycle;
2. follow only the returned `next_action`;
3. make at most one repair per `repair` transition;
4. make no edits while `waiting`;
5. stop on `passed`, `human_required`, `budget_exhausted`, or `error`;
6. never edit the contract or create a waiver.
7. perform `challenge` and `verify` work in the current session, read the saved
   request with `challenge show`, and submit evidence with `challenge submit`.

The generated prompt can be copied into a Claude Code, Codex, or Gemini CLI session. It
is provider-neutral except for adapter-specific command framing. ConstraintLoop
does not create account-level scheduled tasks or launch either provider CLI.

## Session challenge gates

An optional `challenge` block on a Stop-phase loop requires the current coding
session to explore failure scenarios before completion. The session supplies
its existing context, reasoning, and tools. ConstraintLoop does not call an
evaluator or launch another agent for this gate. Separately configured rubric
constraints still run normally.

```yaml
loops:
  completion:
    phase: stop
    interval_seconds: 10
    max_repair_attempts: 3
    max_unchanged_repairs: 2
    max_duration_seconds: 1200
    challenge:
      count: 10
      max_rounds: 2
      max_continuations: 8
      watch: ["src/**/*.py", "tests/**/*.py", "docs/**/*.md", README.md]
      domain_context: [README.md, "docs/**/*.md"]
```

Install the existing hooks with `constraintloop setup --adapter all`, or use
`loop-prompt` for a specific adapter. The same state machine serves every agent:

| Agent | Completion event | Continue current session | Budget exhausted |
| --- | --- | --- | --- |
| Claude Code | `Stop` | `decision: block` with reason | `continue: false` |
| Codex | `Stop` | `decision: block` with reason | `continue: false` |
| Gemini CLI | `AfterAgent` | `decision: deny` with reason | `continue: false` |

These responses follow the official [Claude Code hooks](https://code.claude.com/docs/en/hooks#stop),
[Codex hooks](https://learn.chatgpt.com/docs/hooks#stop), and
[Gemini CLI hooks](https://geminicli.com/docs/hooks/reference/#afteragent) protocols.
Challenge-enabled loops recheck recursive completion events against their
persisted budgets. Other loops retain the legacy recursive-hook guard.
Generated continuation feedback does not replace the saved user goal.

Run `constraintloop cycle completion --json`. After required checks pass, the
cycle returns `challenge`. `constraintloop challenge show completion` returns
the goal, context paths, current request ID and input snapshot, saved scenarios,
and the exact JSON submission schema. The session reads the relevant domain
sources and produces a discovery submission:

```json
{
  "kind": "discovery",
  "request_id": "COPY_FROM_CHALLENGE_SHOW",
  "input_snapshot": "COPY_FROM_CHALLENGE_SHOW",
  "domain_summary": "Pending evidence must never authorize completion.",
  "domain_sources": ["docs/convergence-loops.md"],
  "challenges": [
    {
      "id": "r1-c1",
      "perspective": "recovery",
      "assumption": "Pending state survives process restarts.",
      "scenario": "Restart the supervisor while its evidence is pending.",
      "expected_behavior": "The restarted supervisor waits and does not pass.",
      "verification_plan": "Run the restart regression in the tests gate.",
      "source_refs": ["docs/convergence-loops.md"]
    }
  ]
}
```

The example contains one scenario; submit exactly `count` scenarios per round.
At least three distinct perspective labels are required, or `count` when it is
smaller. IDs and normalized scenario text must be unique across every round.
Source references must name existing files covered by challenge `watch` or
`domain_context`; references are project-relative paths, without line suffixes.
The prompt requests distinct failure mechanisms and rejects invented domain
requirements. Structural checks cannot establish semantic diversity or creativity.

Save the submission under the gitignored `.constraintloop/state/` directory, then run:

```bash
constraintloop challenge submit completion --file .constraintloop/state/discovery.json
constraintloop cycle completion --json
constraintloop challenge show completion
```

The cycle now returns `verify`. The session investigates the scenarios using
existing tests, new regression tests, and other domain-appropriate evidence.
Submit a verification document using the current request ID and input snapshot:

```json
{
  "kind": "verification",
  "request_id": "COPY_FROM_CHALLENGE_SHOW",
  "input_snapshot": "COPY_FROM_CHALLENGE_SHOW",
  "resolutions": [
    {
      "challenge_id": "r1-c1",
      "outcome": "verified",
      "evidence": "The restart regression asserts that pending state remains blocking.",
      "constraint_ids": ["tests"],
      "source_refs": ["tests/test_loops.py"]
    }
  ]
}
```

Verification can be submitted in batches. `verified` requires at least one
enabled deterministic Stop constraint ID; the cycle checks that every cited
gate passes. A waiver, skipped result, rubric verdict, or the session's claim
alone does not satisfy this check. `defect` requests a focused repair;
`unresolved` continues verification; `rejected` requires a written explanation
and watched source references. Rejections are recorded self-review judgments,
not independently proven dispositions. The session must explain why the cited
checks or source evidence actually address the scenario.

Accepted discovery plans cannot be replaced through the submission protocol.
Every accepted submission rotates the request token to reject stale replays.
Changes to the goal, contract, constraint inputs, or watched domain/source files
invalidate prior verification. After edits, run one cycle to refresh the
snapshot before submitting evidence. A source change during evaluation prevents
completion until a fresh evaluation. Keep generated reports outside the watched
set; `.constraintloop/` is excluded automatically.

After repairs or input changes, resolving the current scenarios starts a fresh
discovery round if `max_rounds` allows it. Earlier scenarios remain recorded.
The round cap limits further exploration; it never excuses unresolved scenarios.
An unchanged successful round can pass immediately. A new goal or changed inputs
after completion starts a new challenge run.

Discovery and verification do not consume repair attempts. Each hook-requested
continuation for challenge, verification, or repair consumes the separate
`max_continuations` budget. Reading requests, submitting results, and polling
cycles do not consume that budget. The time budget bounds the entire loop;
repair and unchanged-repair budgets still apply. Journals and budgets survive
session restarts, and startup/compaction hooks point back to the saved work.
Native runtimes may impose their own continuation caps; the generated loop
prompt lets the session perform several steps before another completion event.

This gate applies to completion loops and their hooks. `constraintloop run`
evaluates ordinary constraints; use `cycle` to evaluate the full completion
decision. Challenge configuration on a non-Stop loop is rejected. CI continues
to execute committed constraints without trusting local challenge judgments.
This is evidence-backed self-review, with the author's potential blind spots;
it does not provide independent model review or exhaustive correctness proof.

## Snapshot and accounting rules

The snapshot includes the contract digest plus each applicable constraint ID,
input digest, verdict, and normalized findings. A repair attempt is consumed
only when:

1. the prior transition returned `repair`; and
2. a later cycle observes the result of that agent turn.

Repeated polling of the same pending snapshot increments observations but not
repair attempts. A changed source or external-evidence snapshot resets the
unchanged-repair counter, but not the total duration budget.

Required non-deterministic rubrics retain their existing quorum rules. Repeating
a rubric until it happens to pass is forbidden; all configured runs belong to
one evaluation and one snapshot.

Advisory feedback is actionable but does not require a passing verdict. A Stop
transition remains blocked until the agent either changes the evidence and a
fresh review passes, or records an explicit explanation with `constraintloop
acknowledge`. The explanation is bound to the exact input, verdict, rationale,
and findings; changed feedback requires a new disposition. Acknowledgment never
changes the verdict and is not a waiver.

## First-release acceptance criteria

- The schema rejects unknown loop fields, missing referenced phases, zero
  budgets, and unbounded configurations.
- `cycle` produces stable structured output and exit codes for every state.
- Pending evidence cannot pass the contract or consume repairs.
- Identical polling does not invoke an evaluator or consume repair attempts.
- Changed evidence invalidates the prior transition.
- Attempt and time budgets survive process restarts.
- A second supervisor cannot acquire an active project lease.
- A stale lease is safely recoverable.
- SIGINT/SIGTERM release the lease and preserve the journal.
- Claude and Codex prompt fixtures follow the same provider-neutral protocol.
- Stop hooks and cycles share one attempt ledger rather than double-counting.
- CI continues to ignore local waivers and cached evidence.
- The ConstraintLoop repository dogfoods a bounded completion-loop
  configuration before v0.1 is tagged.

## Implementation sequence

1. Add `PENDING`, `pending_codes`, and strict loop models.
2. Extract the current Stop retry accounting into a shared loop state machine.
3. Implement atomic journals, leases, snapshots, and budget accounting.
4. Add `cycle` with stable JSON and exit codes.
5. Add the deterministic `supervise` process.
6. Generate Claude and Codex native prompts.
7. Connect Stop hooks to the same completion loop.
8. Add integration tests for restart, locking, pending, cancellation, and
   unchanged evidence.
9. Enable and exercise the completion loop in this repository.
