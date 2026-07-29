# Threat model

ConstraintLoop protects against accidental completion, stale evidence, weak
single-pass model judgment, and agents taking the easy path of editing their own
quality policy.

## Trusted inputs

- The committed `constraintloop.yml`, protected by normal repository review.
- The CI environment, secrets, and installed quality tools.
- A human invoking `constraintloop waive` locally with an explicit reason.

## Untrusted inputs

- Generated source code and tests.
- Agent tool calls and completion claims.
- Model evaluator prose, provider availability, and probabilistic output.
- Repository files included in a rubric bundle, which may contain prompt
  injection text.

## Controls

- Contract parsing is strict and rejects unknown fields, missing evaluators,
  invalid quorum, and dependency cycles.
- Command constraints use argv execution by default. Shell strings require the
  explicit `shell: true` opt-in.
- Command working directories and artifact paths may not escape the project.
- Evidence and deterministic waivers are bound to content digests and contract
  identity. Rubric constraints cannot be waived because their live bundle also
  depends on goals and prerequisite evidence.
- Required rubrics need repeated runs with explicit majority quorum.
- Evaluator output is parsed as a strict JSON schema. Errors fail closed for
  required rubrics.
- Optional native CLI evaluators run from an isolated temporary directory with
  edits, tools, project hooks, persistence, and unbounded turns disabled.
- Evaluation bundles are bounded and list omitted files.
- Agent hook writes to the contract and waiver commands observed by hooks are
  denied.
- CI ignores the local evidence cache and all waivers.

## Residual risks

Local hooks are not a sandbox. An agent with arbitrary filesystem or process
access may disable them, call tools outside the hooked surface, manipulate
dependencies, invoke the local waiver CLI outside the hooked surface, or forge
external artifacts. The local CLI cannot authenticate a human caller; human-only
waivers are a trust and workflow rule, while CI's waiver-free execution is the
hard enforcement boundary. Deterministic tests can encode an incorrect
specification, high coverage can still miss behavior, and several model calls
can share the same systematic blind spot.

Native CLI evaluators inherit local CLI authentication and provider quota.
CLI updates can change flags or output envelopes, and using the authoring agent
for review is not independent judgment. Keep them advisory until compatibility
and semantic corpus checks pass, and do not expose their authentication context
to untrusted CI.

For high-assurance use, protect the contract with code ownership, pin tool and
model versions, isolate CI, restrict secrets, retain evidence artifacts, require
human review for policy changes, and use independent security analysis.
