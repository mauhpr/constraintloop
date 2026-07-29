# Provider data flow and privacy

Deterministic command, metric, and artifact constraints run locally. Rubric
constraints may send repository information to the configured remote provider.

An evaluation bundle contains the constraint ID and rubric, the captured user
goal when available, the current Git diff (including readable untracked files),
prior deterministic result metadata and bounded output, files matched by the
rubric's `include` globs, and a list of files omitted because of the size limit.
Repository content is untrusted and may contain prompt injection.

The size limit is fixed before evaluation. One quarter is reserved for the diff
and the remainder for matched files. Within the configured globs, selection
prioritizes paths related to the goal and rubric, changed files, implementation
source, and recent modifications. This improves review relevance without
expanding which files are eligible or increasing the disclosure budget.

Before enabling a remote evaluator:

1. Minimize `include` and `watch` patterns.
2. Confirm the provider's retention, training, residency, and access policies.
3. Exclude credentials, customer data, proprietary files, and generated
   artifacts containing secrets.
4. Keep remote rubrics advisory until reliability and cost are measured.

Local credentials come from the named process environment variable or the
gitignored local secrets file. Process variables take precedence. The file is
parsed as data, never executed as shell. Rotate a key immediately if it appears
in evidence, logs, a diff, or provider output.

`evaluation_bundle_limit`, evaluator `max_attempts`, rubric `runs`, provider
timeouts, and OpenAI `max_output_tokens` bound cost. Provider absence, timeout,
refusal, malformed output, or retry exhaustion becomes an uncertain result;
required rubrics fail closed.

OpenAI evidence metadata is limited to provider and model identity, response
ID, status, attempt count, token counts, and latency. It does not duplicate the
credential or evaluation bundle.

Hooks deny ordinary agent tool requests that name the local secrets file, but
hooks are not a sandbox. CI isolation, reviewed globs, provider access controls,
and credential rotation remain the security boundary.
