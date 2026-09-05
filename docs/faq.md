# FAQ and troubleshooting

## Concepts

### Is ConstraintLoop another coding agent or workflow runtime?

No. It does not plan product work or implement repairs. It owns the contract,
evidence, cache identity, budgets, leases, and completion decision. Claude,
Codex, or another agent may perform one repair when a bounded transition asks
for it.

### Why not just ask the agent to run tests before finishing?

A prompt does not prove which checks ran, which files they covered, or whether
their result is still current. ConstraintLoop binds evidence to the contract
definition and watched file bytes, then reruns authoritative CI without local
caches or waivers.

### Why do constraints run after some actions and again at session end?

They should have different purposes:

| Phase | Use |
| --- | --- |
| `change` | Fast feedback after edits |
| `stop` | Complete local evidence before the agent finishes |
| `push` | Opt-in heavyweight local evidence before Git push |
| `ci` | Independent, uncached, waiver-free verification |

If full tests run after every edit, remove `change` from that constraint. Keep
fast unit tests in `stop`; put full integration suites in `push` and `ci`.

### What is the difference between a failure, uncertainty, and pending state?

- `fail`: reliable evidence says the constraint was not satisfied.
- `uncertain`: an evaluator could not reach a reliable verdict.
- `pending`: external or delayed evidence is not ready yet.

None authorizes completion for a required constraint.

### Why are rubric constraints advisory by default?

Model judgments can vary, refuse, time out, or be influenced by repository
content. Advisory reviews still require the agent to address the finding or
record a snapshot-bound disposition, but deterministic CI remains the safer
release authority.

## Providers and agents

### Can I use Codex or Claude Code instead of calling an API?

Yes. Configure the native command evaluator described in
[native-cli-evaluators.md](native-cli-evaluators.md). The auto adapter prefers a
supported available native CLI. The evaluation is isolated and read-only; it
cannot repair the finding itself.

### How do I know an OpenAI or Anthropic call returned the expected shape?

Provider responses are parsed into the strict `EvaluatorVerdict` model.
Malformed output, empty parsed output, refusal, timeout, and exhausted retries
become errors or `uncertain` evidence rather than passes. SDK compatibility and
semantic fixtures run in CI without live credentials.

### What repository data leaves the machine?

Only files selected by the rubric `include` globs, plus bounded goal, diff,
constraint, and evidence metadata. Secret paths are filtered from both diffs
and full-file evidence. Review the complete disclosure model in
[provider-privacy.md](provider-privacy.md).

### Will repeated polling spend model tokens?

Unchanged pending evidence does not invoke a model. Rubric runs are bounded by
their configured run count, output cap, timeout, and attempts. Use
`constraintloop debug ID` to inspect availability without making a provider
call.

## Evidence and caching

### Why does a result say `cached`?

The contract definition and every matched `watch` input have the same digest as
the prior evidence. Use `--no-cache` for a deliberate local rerun. CI always
reruns without local evidence.

### Why did an unrelated edit invalidate a result?

The constraint probably has a broad `watch`, such as `"**/*"`. Narrow it to the
files that can affect the result while including configuration, lockfiles, and
scripts that genuinely change the check.

### Why did my acknowledgment stop working?

An acknowledgment is bound to the exact evaluator input, verdict, rationale,
and findings. Any relevant change or fresh different feedback requires a new
disposition.

### Can an agent waive a required failure?

Hooks deny observed agent waiver commands, but local process identity cannot
prove a caller is human. A local deterministic waiver therefore requires an
explicit reason and exact fresh snapshot, while CI ignores all waivers.
Rubric verdicts cannot be waived.

## Failure testing

### How should I test failure scenarios?

Use deterministic fakes and isolated temporary projects:

| Scenario | Test method |
| --- | --- |
| Command failure | Fake command exits with a known nonzero code |
| Pending evidence | Fake command exits `75` |
| Timeout | Fake command exceeds a very small test timeout |
| Malformed evaluator | Command writes invalid or extra JSON |
| Provider refusal/rate limit | Mock the SDK response or exception |
| Corrupt state | Write truncated or schema-invalid state in a temporary cache |
| Concurrent writer | Start two supervisors against one temporary project |
| Secret disclosure | Put sentinel credentials under denied/secret paths and assert absence |

Do not trigger paid provider failures merely to test error handling. The
repository’s failure laboratory and fixtures cover these paths deterministically.

## Hooks and setup

### Are hooks a security sandbox?

No. A privileged process can bypass or alter local hooks. Treat hooks as fast
policy automation. Protect the committed contract and use independent CI as the
trusted completion boundary.

### Will setup overwrite existing hooks?

No. `constraintloop setup` merges owned hook entries and preserves unrelated
configuration. In a monorepo, hooks retain the exact directory selected with
`--project` instead of falling back to the Git root. `constraintloop uninstall`
removes only owned entries and writes a gitignored local tombstone. Restoring an
old committed settings file therefore cannot silently reactivate an explicitly
uninstalled hook; running setup again clears the tombstone. Claude hooks are
installed into `.claude/settings.local.json`, not the shareable
`.claude/settings.json`. Review and trust newly installed Codex project hooks
through `/hooks`.

### Why did a Stop gate not run while background work was active?

ConstraintLoop evaluates completion only for the main agent at an idle turn
boundary. It ignores subagent hook events and defers Stop/AfterAgent while the
hook payload reports background tasks or scheduled wakeups. Run
`constraintloop run --phase stop` explicitly when you want an immediate check.

### How do I keep integration tests off the frequent Stop gate?

Assign them to `phases: [push, ci]`. Run them explicitly with
`constraintloop run --phase push`, or install the local pre-push integration with
`constraintloop setup --adapter all --pre-push`. Existing non-ConstraintLoop Git
hooks are never overwritten.

### Why does a hook say `Missing option --project`?

Regenerate the adapter configuration:

```bash
constraintloop setup --adapter all --project .
```

Generated commands include the resolved project argument. If the error remains,
inspect the relevant `.claude`, `.codex`, or `.gemini` settings file for a stale
manually copied command.

### Why does a hook reject documented contract keys as extra inputs?

The hook is running an older ConstraintLoop executable than the contract expects.
Upgrade that installation, then regenerate the hook commands so they resolve to the
same release:

```bash
constraintloop --version
constraintloop setup --adapter all --project .
```

Current releases include their version and this recovery action when strict schema
validation encounters unknown keys. Recursive Claude Stop-hook calls are allowed to
finish after delivering the error once, so an invalid or version-skewed contract does
not create a repeated Stop-hook loop.

## Loops

### Can I configure an unlimited repair loop?

No. Repair attempts, unchanged repairs, and elapsed duration must all be
bounded. Zero or unbounded budgets are rejected by the schema.

### Why did `supervise` exit with code 10?

Code 10 means `repair`: ConstraintLoop has produced one focused next action and
stopped so a native agent can perform at most one repair. Run another cycle only
after that turn.

### What if a supervisor crashes?

Transitions are journaled atomically. A recoverable lease prevents concurrent
writers and can be reclaimed after its bounded TTL. Cancellation preserves the
journal and releases the lease.

## Compatibility and release

### Which platforms are supported in v0.2?

Linux and macOS on Python 3.11 through 3.14. Windows is not supported until
hook commands, locking, process control, and installed-wheel behavior have
Windows-native implementations and CI coverage.

### Why did local checks pass while CI failed?

Local environments can contain undeclared extras, cached files, or tools from
earlier work. Reproduce the workflow’s exact `uv sync` command, then compare
Python and optional dependency versions. Installed-wheel jobs catch editable
source assumptions that unit tests can miss.

### Where should I report problems?

- Usage questions: [SUPPORT.md](../SUPPORT.md)
- Bugs and features: GitHub issue forms
- Vulnerabilities: [SECURITY.md](../SECURITY.md)
- Conduct concerns: [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md)
