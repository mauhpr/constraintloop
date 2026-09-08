# Completion policy and v0.5 migration

## Entry points

| Entry point | Local overlays | Local waivers | Pending refresh | Task completion |
| --- | --- | --- | --- | --- |
| `run --phase change/stop` | Strengthening only | Deterministic gates only | Use `--refresh` to save a fresh observation | Constraint report only |
| `run --phase push` | Strengthening only | Never | Use `--refresh` | Constraint report only |
| `ci` / `run --phase ci` | Never | Never | Always uncached | Independent CI evidence |
| `cycle` / `supervise` | Except CI phase | Except push/CI | Refresh after loop interval | Persisted bounded transition; includes configured challenge work |
| Stop / AfterAgent hooks | Strengthening only | Deterministic gates only | Shared cycle interval, or refresh each event without a loop | Required evidence, configured challenges, and advisory dispositions |

The engine enforces waiver and CI-cache policy regardless of caller defaults.
`run` does not finish a session challenge gate. Use `cycle` and the native Stop
hook for completion. CI verifies committed deterministic/rubric constraints;
it does not trust a local challenge journal or start an interactive session.

Failed prerequisites produce blocked dependents with `blocked_by` IDs, not
spurious evaluation errors. A cycle's `blocking_constraints` names the root
causes, including advisory prerequisites needed by required gates. Missing
tools, parser failures, and uncertain evaluations remain blocking errors.

## Task lifecycle

After a loop passes, a changed watched input or task goal starts a new run with
a fresh budget. Restarting an unfinished run, changing its code, or changing
its goal does not reset its limits. Unchanged completed evidence stays passed
even after the old time budget elapses. One project loop has one journal;
use separate worktrees for independent concurrent tasks.

Recursive Stop hooks are completion boundaries too. Ordinary loops use their
repair and duration budgets. Challenge loops also bound session continuations.
Without a loop, `max_auto_retries` bounds repair and advisory-disposition
continuations across changing evidence until completion succeeds. Exhaustion
returns `continue: false`, including Gemini, rather than silently allowing
completion or asking the agent to retry indefinitely.

`supervise` renews its lease during checks and waits, and checks ownership
before yielding a transition. It does not launch an agent or perform repairs.

## Evidence and redaction

The input-digest format is versioned and length-framed: filename/content
boundaries, unreadable files, and missing baselines cannot alias ordinary
content. Upgrading invalidates old evidence and snapshot-bound waivers.

Known sensitive environment values of at least eight characters are scrubbed,
as are credential assignments such as `password=...`, `api_key=...`, and
`access_token=...`. Structured sensitive keys are scrubbed recursively.
Scrubbing applies to evidence messages, findings, structured artifact fields,
retained command output, cached reads, and native-hook feedback. Command output
is scrubbed before tail truncation so truncation cannot sever the credential
label from its value.

This is best-effort redaction, not a data-loss-prevention boundary. Unknown,
encoded, split, or unlabeled secrets may escape detection. Raw command output
exists transiently in memory for parsing, bounded to 8 MiB per subprocess.
Pre-existing files from older releases are not retroactively erased. Avoid
printing credentials and restrict access to local state. Repository artifacts
and challenge submissions remain author-controlled data; do not put secrets
in them. No new remote model API is used by the session challenge gate.

## Upgrade checklist

1. Upgrade ConstraintLoop to 0.5.0 and re-run `constraintloop setup --adapter all`
   in projects whose hooks pin an ephemeral package version.
2. Re-run checks to replace stale cached evidence. Revisit any intentional
   local waiver against the new exact evidence; CI and push cannot use it.
3. Ensure prerequisites are enabled in every dependent phase. Correct invalid
   numeric baselines; finite numeric strings are still accepted.
4. Keep noisy tool output below 8 MiB or write detailed reports to artifacts
   and emit a compact command summary.
5. Enable `loops.NAME.challenge` explicitly where domain-driven self-review is
   desired. Omitted challenge configuration preserves ordinary gating.
