# Configuration reference

ConstraintLoop reads the nearest supported YAML contract filename. The schema
is strict: unknown fields are errors.

The root fields are `version` (currently `1`), `settings`, `constraints`,
`evaluators`, and `loops`.

The generated JSON Schema is published at
[`schema/constraintloop.schema.json`](../schema/constraintloop.schema.json).
Editors that support YAML language-server directives can enable validation and
autocomplete with:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/mauhpr/constraintloop/main/schema/constraintloop.schema.json
```

Settings default to:

| Field | Default | Allowed |
| --- | ---: | --- |
| `max_auto_retries` | 2 | 0–20 |
| `concurrency` | 4 | 1–32 |
| `evidence_output_limit` | 65536 | 1024–1048576 bytes |
| `hook_output_limit` | 4096 | 512–32768 bytes |
| `evaluation_bundle_limit` | 102400 | 4096–2097152 bytes |
| `progress_interval_seconds` | 15 | 0.1–300 seconds |

If `constraintloop.local.yml` or `constraintloop.local.yaml` exists beside the
repository contract, it is loaded as a local overlay. Mapping values merge
recursively. Overlays may add constraints and tighten an existing constraint's
enforcement, phase set, dependency set, or timeout. They cannot disable or
replace committed gates, commands, evaluators, loops, watched inputs, or reduce
evidence limits. Only one overlay filename may exist. The merged contract is
validated normally and its digest invalidates evidence when local policy
changes. `init` and `setup` add both overlay names to `.gitignore`. The
authoritative `constraintloop ci` command ignores local overlays and always
evaluates the committed repository contract.

Every constraint supports `description`, `enforcement` (`required` or
`advisory`), `phases` (`change`, `stop`, `push`, `ci`), `watch` globs, dependency IDs
in `needs`, `timeout_seconds`, and `enabled`. Dependencies must exist and the
graph must be acyclic.
Every enabled dependent must have its prerequisites enabled in all of its
phases. A failed prerequisite blocks its dependents without running them;
cycles report the root cause for repair, even if that prerequisite is advisory.
Genuine evaluation errors still require human inspection.
Identifiers may contain letters, numbers, dots, underscores, and hyphens.
`watch` and `include` values must be nonempty project-relative POSIX globs.

Command constraints use `kind: command`, `command`, `cwd`, `shell`,
`success_codes`, and `pending_codes` (default `[75]`). Prefer an argv list. A
string command is rejected unless `shell: true` explicitly accepts shell
parsing. Success and pending codes may not overlap.

Command, metric, and ratchet constraints may declare an optional transient
retry policy:

```yaml
retry:
  max_attempts: 3
  exit_codes: [125]  # A wrapper-defined infrastructure failure code.
  retry_timeouts: false
  retry_start_errors: true
  delay_seconds: 2
  total_timeout_seconds: 90
```

No retries occur when `retry` is absent. A configured policy retries only the
listed exit codes and, when enabled, timeouts or process startup failures.
For compatibility, omitting `exit_codes` from a retry policy still defaults to
`[1]`. Set it explicitly: `exit_codes: []` retries no completed command failures,
while a wrapper can reserve a distinct exit code for retryable infrastructure
failures. Do not retry assertion failures; exit code 1 commonly includes them.
Every attempt is capped by the constraint's normal timeout, which defaults to
300 seconds and is always finite. On POSIX, a timeout terminates the entire
spawned process group, including descendants that inherited the command's output
pipes. Retry and periodic
running status lines are emitted during human-readable runs; `--json` remains a
single machine-readable document. `timeout_seconds` bounds each attempt and,
unless overridden, the complete retry sequence including delays. Timeout
retries require an explicit `total_timeout_seconds` greater than the per-attempt
timeout. This keeps the total bound visible while leaving enough budget for a
second attempt. Command and command-evaluator processes run from the selected
project root by default, and ConstraintLoop prepends that root to `PYTHONPATH`.
Collection of combined stdout/stderr has a separate hard 8 MiB limit per
process. Exceeding it terminates the process group and produces an error;
truncated output is never treated as a complete metric or evaluator response.
`evidence_output_limit` controls the smaller, redacted tail retained afterward.
Command, metric, and ratchet results retain an ordered `attempts` list in JSON
and cached evidence, also shown by `constraintloop debug ID`.
Each attempt includes its number, UTC start time, duration,
exit code (null if the command did not complete), redacted output tail, and any
execution error. The output limit applies separately to each attempt (at most
10); timeouts and output-limit errors retain the output collected before
termination. Top-level output and exit code continue to describe the final
execution; earlier attempts are never fed into metric parsing.

Metric constraints add `parser` and `threshold`. A parser has type `json` or
`regex`, reads `stdout`, `stderr`, or a project-contained `file`, and selects a
dotted JSON `path` or regex `pattern` and `group`. Threshold operators are
`gt`, `gte`, `lt`, `lte`, and `eq`.
Measurements, thresholds, and baselines must be finite numbers. Finite numeric
strings remain supported; booleans, nulls, containers, NaN, and infinities are
rejected. `--allow-regression` never overrides numeric validation.

Ratchet constraints use `kind: ratchet` with the same command and parser fields
as a metric. Their default `mode: must_not_increase` compares the current value
to the committed `constraintloop-baselines.json`; `must_not_decrease` supports
monotonic growth metrics. Initialize or strengthen baselines explicitly:

```bash
constraintloop baseline update database_consumers
constraintloop baseline update --all
```

Updates that would weaken an existing baseline are rejected. Use
`--allow-regression` only for a reviewed, intentional reset, then commit the
baseline artifact with the contract. `baseline_file` can select another
project-relative JSON file. Observed agent commands using `--allow-regression`
and direct edits to baseline files are denied by pre-tool hooks; ask the human
to perform intentional policy changes outside the hooked session. Strengthening
through the ordinary baseline-update command remains allowed. Each baseline
entry records both the numeric value
and the SHA-256 digest of the parsed evidence source, replacing the separate
count-and-hash bookkeeping commonly used for migration inventories.

Artifact constraints use `kind: artifact`, a project-contained `path`, format
`any`, `json`, or `junit`, and `non_empty`. JSON artifacts can expose selected
dotted paths as structured evidence so summaries and `status` show meaningful
counts instead of the entire report:

```yaml
report:
  kind: artifact
  path: reports/consumer-inventory.json
  format: json
  evidence:
    consumers: counts.consumers
    change: counts.change
```

Rubric constraints use `kind: rubric`, an evaluator ID, a written `rubric`,
`include` globs, `runs`, and `pass_quorum`. Required rubrics need at least two
runs and an explicit majority quorum.

Evaluators are:

- `command`: `command`, `shell`, and `timeout_seconds`;
- `openai`: `model`, `api_key_env`, `timeout_seconds`, `max_attempts`,
  `max_output_tokens`, and `reasoning_effort`;
- `anthropic`: `model`, `api_key_env`, `timeout_seconds`, `max_attempts`, and
  `max_output_tokens`.

See `examples/constraintloop.full.yml` for a parseable full example. CI ignores
local waivers. Each loop declares a phase, positive polling interval, repair and
unchanged-repair budgets, a duration budget, and fixed pass/failure/pending/
exhaustion actions. Unknown fields and zero or unbounded budgets are rejected.
At most one Stop-phase loop is allowed. Ready deterministic constraints execute
concurrently up to `settings.concurrency`; rubric evaluators remain serialized
and result ordering follows the contract.
`constraintloop explain --phase stop` reports why every constraint is eligible
or skipped, the files matched by its watch globs, changed watch paths, cache
state, and dependency chains without executing any gate. Human-readable final
summaries label concrete policy failures as `constraint` and startup,
prerequisite, or evaluation errors as `environment`; the same
`failure_category` is retained in JSON evidence.
Hook responses use `hook_output_limit` to retain failing test names and the first
useful traceback line without injecting the complete test log. The unabridged
retained tail remains available with `constraintloop debug CONSTRAINT`.
See `docs/convergence-loops.md` for the cycle protocol and stable exit codes.

Stop-phase loops optionally accept `challenge` for discovery and verification
performed in the active Claude Code, Codex, or Gemini CLI session. Defaults are
`count: 10` (1–100), `max_rounds: 2` (1–10), `max_continuations: 8` (1–100),
`watch: ["**/*"]`, and `domain_context: []`. Context and watch entries are
project-relative globs. The existing loop duration and repair budgets also
apply; challenge work uses no model evaluator configuration. Omit `challenge`
to retain the ordinary completion behavior. See
[session challenge gates](convergence-loops.md#session-challenge-gates) for
submission examples, evidence requirements, and adapter behavior.

See [completion policy](completion-policy.md) for the cross-entry-point matrix,
task lifecycle, redaction policy, and upgrade notes.
