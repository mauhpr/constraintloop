# Native Codex and Claude Code evaluators

ConstraintLoop can delegate an advisory rubric to an authenticated local Codex
or Claude Code installation without directly configuring a provider SDK key.
This still consumes the selected product's account quota and sends the
evaluation bundle to its provider.

Three command-protocol entry points are installed:

- `constraintloop-native-evaluator` selects an available CLI automatically;
- `constraintloop-codex-evaluator` requires Codex;
- `constraintloop-claude-evaluator` requires Claude Code.

Use the command evaluator shown in `examples/native-evaluator.yml`. During a
hook evaluation, `--adapter auto` prefers the agent that initiated the hook:
Codex prefers Codex and Claude prefers Claude Code. Outside a hook, auto mode
uses the first installed supported CLI, preferring Codex when both are
available. Set `CONSTRAINTLOOP_CALLER_ADAPTER=codex` or `claude` for an explicit
one-process preference, or use a provider-specific entry point.

## Isolation

The wrappers create a temporary working directory and provide the complete
bounded evaluation bundle over stdin. They do not expose the repository as the
working directory.

Codex runs in ephemeral, read-only, non-interactive mode with project and user
rules ignored, agentic shell/code/browser/app tools explicitly disabled, a
minimal authentication/runtime environment, and a strict output schema. Claude Code runs in safe,
non-persistent print mode with built-in tools and slash commands disabled, an
isolated empty MCP configuration, one maximum turn, and a provider-compatible
structured output schema. Safe mode preserves the user's normal local Claude
authentication; the wrapper never reads or forwards the credential.

Both wrappers:

- impose a subprocess timeout;
- remove unrelated parent-process environment variables and credentials;
- validate the final object again with ConstraintLoop's strict model;
- return nonzero for missing authentication, timeout, malformed output,
  unavailable CLI, or schema failure;
- allow the command evaluator to convert failures into an uncertain verdict;
- never authorize repository edits or launch an agent repair turn.

Run `constraintloop-native-evaluator --doctor --adapter claude` for a no-cost
binary and authentication preflight. After that passes, run
`constraintloop-native-evaluator --canary --adapter claude` for one live,
unambiguous structured-output check. The canary and ordinary Claude evaluations
default to a `$0.25` maximum; change it with `--max-budget-usd`. Pass
`--model FULL_MODEL_ID` to pin a model and `--timeout-seconds N` to change the
wrapper limit. Keep the surrounding rubric advisory until its semantic corpus
has measured false-pass and false-fail rates.

## Trust boundary

Using the same product that authored a patch preserves the user's existing
authentication and model preference, but it is not independent review. Shared
blind spots can produce shared false passes. Required policy should combine
native review with deterministic gates or use a separately configured provider.

The CLI process inherits the invoking user's authentication context. Treat its
saved authentication data like a credential, pin and test supported CLI
versions, and do not enable native evaluators on untrusted fork CI. For public
CI, use the provider's supported action or an isolated API integration with
least-privilege secrets.
