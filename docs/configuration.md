# Configuration reference

ConstraintLoop reads the nearest supported YAML contract filename. The schema
is strict: unknown fields are errors.

The root fields are `version` (currently `1`), `settings`, `constraints`,
`evaluators`, and `loops`. Settings default to:

| Field | Default | Allowed |
| --- | ---: | --- |
| `max_auto_retries` | 2 | 0–20 |
| `concurrency` | 4 | 1–32 |
| `evidence_output_limit` | 65536 | 1024–1048576 bytes |
| `evaluation_bundle_limit` | 102400 | 4096–2097152 bytes |

Every constraint supports `description`, `enforcement` (`required` or
`advisory`), `phases` (`change`, `stop`, `ci`), `watch` globs, dependency IDs
in `needs`, `timeout_seconds`, and `enabled`. Dependencies must exist and the
graph must be acyclic.
Identifiers may contain letters, numbers, dots, underscores, and hyphens.
`watch` and `include` values must be nonempty project-relative POSIX globs.

Command constraints use `kind: command`, `command`, `cwd`, `shell`,
`success_codes`, and `pending_codes` (default `[75]`). Prefer an argv list. A
string command is rejected unless `shell: true` explicitly accepts shell
parsing. Success and pending codes may not overlap.

Metric constraints add `parser` and `threshold`. A parser has type `json` or
`regex`, reads `stdout`, `stderr`, or a project-contained `file`, and selects a
dotted JSON `path` or regex `pattern` and `group`. Threshold operators are
`gt`, `gte`, `lt`, `lte`, and `eq`.

Artifact constraints use `kind: artifact`, a project-contained `path`, format
`any`, `json`, or `junit`, and `non_empty`.

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
See `docs/convergence-loops.md` for the cycle protocol and stable exit codes.
