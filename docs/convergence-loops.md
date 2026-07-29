# Bounded convergence loops

Bounded convergence loops are part of the ConstraintLoop v0.1 release scope.
They connect completion evidence to repeated agent work and delayed external
state without turning ConstraintLoop into another general-purpose agent
runtime.

## Product boundary

ConstraintLoop owns the state machine, evidence, budgets, locking, and stop
decision. Claude Code, Codex, or another agent may perform a repair after
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
focused repair instruction. Passing evidence stops the loop immediately.

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

Exit codes will remain stable for automation:

| Code | State |
| ---: | --- |
| 0 | passed |
| 10 | repair |
| 11 | waiting |
| 12 | human required |
| 13 | budget exhausted |
| 14 | engine error |

## Supervisor

`constraintloop supervise NAME` repeatedly calls the same state machine while
holding a project lease. It polls deterministic constraints locally and emits
JSON Lines when state changes.

The v0.1 supervisor does not launch Claude or Codex for repairs. On `repair`, it
exits with code 10 so a native agent loop, CI workflow, or another explicit
controller can perform the repair. Optional native rubric evaluators are
single-shot, tool-disabled reviews and cannot perform a repair transition.

The supervisor:

- use a single-writer lease under `.constraintloop/state/loops/`;
- recover leases after a bounded TTL;
- handle cancellation signals;
- never count polling observations as repair attempts;
- avoid rerunning unchanged expensive evidence before its configured interval;
- journal transitions atomically;
- redact secrets and cap retained output;
- stop at every attempt and duration budget.

## Native integration

`constraintloop loop-prompt NAME --adapter claude|codex` will print a durable
prompt that tells the native agent to:

1. run one cycle;
2. follow only the returned `next_action`;
3. make at most one repair per `repair` transition;
4. make no edits while `waiting`;
5. stop on `passed`, `human_required`, `budget_exhausted`, or `error`;
6. never edit the contract or create a waiver.

For Claude Code, `constraintloop setup-loop NAME --adapter claude` may also
write `.claude/loop.md`. The user can start it with `/loop` or `/loop 2m`.

For Codex, the generated prompt can be used in a same-chat scheduled task or a
project scheduled task. ConstraintLoop will not attempt to create account-level
scheduled tasks itself.

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
