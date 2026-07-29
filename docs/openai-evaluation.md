# OpenAI evaluator verification

ConstraintLoop uses the Responses API `parse` helper with a strict Pydantic
verdict schema. Schema conformance is necessary but does not prove that a
verdict is correct, so verification is layered.

Ordinary tests replace the OpenAI module with a deterministic fake and assert
the exact model, instructions, bundle, token limit, reasoning effort, timeout,
and Pydantic response type sent by the adapter. They cover successful parsing,
refusal, incomplete output, transient errors, retry exhaustion, metadata
capture, and fail-closed engine behavior. They never need a credential.

CI also installs both the lowest supported OpenAI SDK and the newest compatible
release, then checks that `responses.parse` exposes every parameter the adapter
uses. The supported SDK range is `>=2.0,<3`.

## Semantic corpus

The versioned corpus at `tests/fixtures/openai_eval_corpus_v1.yml` contains a
known-good patch and known-bad security-boundary regressions. Known-bad cases
may never receive a passing verdict, must receive a failing majority, and must
produce required concrete finding terms.

Run the live canary only when intentionally spending provider tokens:

```bash
uv sync --extra dev --extra openai
uv run python scripts/openai_eval_canary.py \
  --model EXACT_MODEL_ID_OR_SNAPSHOT \
  --runs 3
```

The command reads the normal configured provider credential, prints one
machine-readable summary per public corpus case, and returns nonzero for
provider errors, false passes, false failures, missing findings, or insufficient
quorum. It does not print the credential or private repository bundles.

Use an exact model snapshot when the provider offers one. Alias changes can
alter evaluator behavior without a source change. Corpus results should be
recorded before changing the model, prompt, schema, SDK compatibility range, or
quorum policy.

Evidence records retain only safe operational metadata: provider, returned
model identity, response ID, status, attempts, token counts, and latency.
Credentials and request content are not duplicated into metadata.

OpenAI documents that Structured Outputs enforce schema adherence while
refusals and incomplete responses still require explicit application handling:
https://developers.openai.com/api/docs/guides/structured-outputs
