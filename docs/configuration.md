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
  exit_codes: [1, 125]
  retry_timeouts: false
  retry_start_errors: true
  delay_seconds: 2
  total_timeout_seconds: 90
```

No retries occur when `retry` is absent. A configured policy retries only the
listed exit codes and, when enabled, timeouts or process startup failures.
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

Metric constraints add `parser` and `threshold`. A parser has type `json` or
`regex`, reads `stdout`, `stderr`, or a project-contained `file`, and selects a
dotted JSON `path` or regex `pattern` and `group`. Threshold operators are
`gt`, `gte`, `lt`, `lte`, and `eq`.

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
project-relative JSON file. Each baseline entry records both the numeric value
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
