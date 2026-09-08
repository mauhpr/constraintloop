"""Strict public models for contracts, results, and evaluator payloads."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from constraintloop._numbers import finite_number
from constraintloop.redaction import redact_text, redact_value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Verdict(StrEnum):
    PASS = "pass"
    PENDING = "pending"
    FAIL = "fail"
    ERROR = "error"
    SKIPPED = "skipped"
    UNCERTAIN = "uncertain"
    WAIVED = "waived"


class Enforcement(StrEnum):
    REQUIRED = "required"
    ADVISORY = "advisory"


class Phase(StrEnum):
    CHANGE = "change"
    STOP = "stop"
    PUSH = "push"
    CI = "ci"

    @property
    def allows_local_waivers(self) -> bool:
        return self not in {Phase.PUSH, Phase.CI}


class FailureCategory(StrEnum):
    CONSTRAINT = "constraint"
    ENVIRONMENT = "environment"


class ContractSettings(StrictModel):
    max_auto_retries: int = Field(default=2, ge=0, le=20)
    concurrency: int = Field(default=4, ge=1, le=32)
    evidence_output_limit: int = Field(default=65_536, ge=1_024, le=1_048_576)
    hook_output_limit: int = Field(default=4_096, ge=512, le=32_768)
    evaluation_bundle_limit: int = Field(default=102_400, ge=4_096, le=2_097_152)
    progress_interval_seconds: float = Field(default=15.0, ge=0.1, le=300)


class CommandRetryPolicy(StrictModel):
    max_attempts: int = Field(default=2, ge=2, le=10)
    exit_codes: list[int] = Field(default_factory=lambda: [1])
    retry_timeouts: bool = False
    retry_start_errors: bool = True
    delay_seconds: float = Field(default=1.0, ge=0, le=300)
    total_timeout_seconds: float | None = Field(default=None, gt=0, le=7_200)


class BaseConstraint(StrictModel):
    description: str | None = None
    enforcement: Enforcement = Enforcement.REQUIRED
    phases: list[Phase] = Field(default_factory=lambda: [Phase.STOP, Phase.CI])
    watch: list[str] = Field(default_factory=lambda: ["**/*"])
    needs: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=300.0, gt=0, le=7_200)
    enabled: bool = True

    @field_validator("watch")
    @classmethod
    def validate_watch(cls, patterns: list[str]) -> list[str]:
        return _relative_patterns(patterns, "watch")


class CommandConstraint(BaseConstraint):
    kind: Literal["command"]
    command: list[str] | str
    shell: bool = False
    cwd: str = "."
    success_codes: list[int] = Field(default_factory=lambda: [0])
    pending_codes: list[int] = Field(default_factory=lambda: [75])
    retry: CommandRetryPolicy | None = None

    @model_validator(mode="after")
    def validate_command(self) -> CommandConstraint:
        if isinstance(self.command, str) and not self.shell:
            raise ValueError("string commands require shell: true; prefer argv lists")
        if isinstance(self.command, list) and not self.command:
            raise ValueError("command argv cannot be empty")
        if set(self.success_codes) & set(self.pending_codes):
            raise ValueError("success_codes and pending_codes must not overlap")
        if self.retry and set(self.retry.exit_codes) & (
            set(self.success_codes) | set(self.pending_codes)
        ):
            raise ValueError("retry exit_codes must not overlap success_codes or pending_codes")
        if (
            self.retry
            and self.retry.retry_timeouts
            and (
                self.retry.total_timeout_seconds is None
                or self.retry.total_timeout_seconds <= self.timeout_seconds
            )
        ):
            raise ValueError(
                "timeout retries require retry.total_timeout_seconds greater than timeout_seconds"
            )
        return self


class MetricParser(StrictModel):
    type: Literal["json", "regex"]
    path: str | None = None
    pattern: str | None = None
    group: int | str = 1
    source: Literal["stdout", "stderr", "file"] = "stdout"
    file: str | None = None

    @model_validator(mode="after")
    def validate_parser(self) -> MetricParser:
        if self.type == "json" and not self.path:
            raise ValueError("json metric parsers require path")
        if self.type == "regex" and not self.pattern:
            raise ValueError("regex metric parsers require pattern")
        if self.source == "file" and not self.file:
            raise ValueError("file metric parsers require file")
        return self


class MetricThreshold(StrictModel):
    operator: Literal["gt", "gte", "lt", "lte", "eq"]
    value: float = Field(allow_inf_nan=False)

    @field_validator("value", mode="before")
    @classmethod
    def validate_value(cls, value: object) -> float:
        return finite_number(value)


class MetricConstraint(BaseConstraint):
    kind: Literal["metric"]
    command: list[str] | str
    shell: bool = False
    cwd: str = "."
    success_codes: list[int] = Field(default_factory=lambda: [0])
    pending_codes: list[int] = Field(default_factory=lambda: [75])
    retry: CommandRetryPolicy | None = None
    parser: MetricParser
    threshold: MetricThreshold

    @model_validator(mode="after")
    def validate_command(self) -> MetricConstraint:
        if isinstance(self.command, str) and not self.shell:
            raise ValueError("string commands require shell: true; prefer argv lists")
        if isinstance(self.command, list) and not self.command:
            raise ValueError("command argv cannot be empty")
        if set(self.success_codes) & set(self.pending_codes):
            raise ValueError("success_codes and pending_codes must not overlap")
        if self.retry and set(self.retry.exit_codes) & (
            set(self.success_codes) | set(self.pending_codes)
        ):
            raise ValueError("retry exit_codes must not overlap success_codes or pending_codes")
        if (
            self.retry
            and self.retry.retry_timeouts
            and (
                self.retry.total_timeout_seconds is None
                or self.retry.total_timeout_seconds <= self.timeout_seconds
            )
        ):
            raise ValueError(
                "timeout retries require retry.total_timeout_seconds greater than timeout_seconds"
            )
        return self


class RatchetConstraint(BaseConstraint):
    kind: Literal["ratchet"]
    command: list[str] | str
    shell: bool = False
    cwd: str = "."
    success_codes: list[int] = Field(default_factory=lambda: [0])
    pending_codes: list[int] = Field(default_factory=lambda: [75])
    retry: CommandRetryPolicy | None = None
    parser: MetricParser
    mode: Literal["must_not_increase", "must_not_decrease"] = "must_not_increase"
    baseline_file: str = "constraintloop-baselines.json"

    @field_validator("baseline_file")
    @classmethod
    def validate_baseline_file(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
            raise ValueError("baseline_file must be a project-relative POSIX path")
        return value

    @model_validator(mode="after")
    def validate_command(self) -> RatchetConstraint:
        if isinstance(self.command, str) and not self.shell:
            raise ValueError("string commands require shell: true; prefer argv lists")
        if isinstance(self.command, list) and not self.command:
            raise ValueError("command argv cannot be empty")
        if set(self.success_codes) & set(self.pending_codes):
            raise ValueError("success_codes and pending_codes must not overlap")
        if self.retry and set(self.retry.exit_codes) & (
            set(self.success_codes) | set(self.pending_codes)
        ):
            raise ValueError("retry exit_codes must not overlap success_codes or pending_codes")
        if (
            self.retry
            and self.retry.retry_timeouts
            and (
                self.retry.total_timeout_seconds is None
                or self.retry.total_timeout_seconds <= self.timeout_seconds
            )
        ):
            raise ValueError(
                "timeout retries require retry.total_timeout_seconds greater than timeout_seconds"
            )
        return self


class ArtifactConstraint(BaseConstraint):
    kind: Literal["artifact"]
    path: str
    format: Literal["any", "json", "junit"] = "any"
    non_empty: bool = True
    evidence: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_evidence(self) -> ArtifactConstraint:
        if self.evidence and self.format != "json":
            raise ValueError("structured artifact evidence requires format: json")
        if any(not name or not path for name, path in self.evidence.items()):
            raise ValueError("artifact evidence names and JSON paths must not be empty")
        return self


class RubricConstraint(BaseConstraint):
    kind: Literal["rubric"]
    evaluator: str
    rubric: str
    include: list[str] = Field(default_factory=lambda: ["**/*"])
    runs: int = Field(default=1, ge=1, le=9)
    pass_quorum: int | None = Field(default=None, ge=1, le=9)

    @field_validator("include")
    @classmethod
    def validate_include(cls, patterns: list[str]) -> list[str]:
        return _relative_patterns(patterns, "include")

    @model_validator(mode="after")
    def validate_quorum(self) -> RubricConstraint:
        if self.enforcement == Enforcement.REQUIRED:
            if self.runs < 2:
                raise ValueError("required rubrics need at least two runs")
            if self.pass_quorum is None or self.pass_quorum <= self.runs // 2:
                raise ValueError("required rubrics need an explicit majority pass_quorum")
            if self.pass_quorum > self.runs:
                raise ValueError("pass_quorum cannot exceed runs")
        elif self.pass_quorum is not None and self.pass_quorum > self.runs:
            raise ValueError("pass_quorum cannot exceed runs")
        return self


ConstraintSpec = Annotated[
    CommandConstraint
    | MetricConstraint
    | RatchetConstraint
    | ArtifactConstraint
    | RubricConstraint,
    Field(discriminator="kind"),
]


class OpenAIEvaluatorConfig(StrictModel):
    type: Literal["openai"]
    model: str
    api_key_env: str = "OPENAI_API_KEY"
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_attempts: int = Field(default=2, ge=1, le=5)
    max_output_tokens: int = Field(default=2_000, ge=256, le=16_384)
    reasoning_effort: Literal["minimal", "low", "medium", "high"] = "minimal"


class AnthropicEvaluatorConfig(StrictModel):
    type: Literal["anthropic"]
    model: str
    api_key_env: str = "ANTHROPIC_API_KEY"
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_attempts: int = Field(default=2, ge=1, le=5)
    max_output_tokens: int = Field(default=2_048, ge=256, le=16_384)


class CommandEvaluatorConfig(StrictModel):
    type: Literal["command"]
    command: list[str] | str
    shell: bool = False
    timeout_seconds: float = Field(default=60, gt=0, le=600)

    @model_validator(mode="after")
    def validate_command(self) -> CommandEvaluatorConfig:
        if isinstance(self.command, str) and not self.shell:
            raise ValueError("string commands require shell: true")
        if isinstance(self.command, list) and not self.command:
            raise ValueError("command argv cannot be empty")
        return self


EvaluatorConfig = Annotated[
    OpenAIEvaluatorConfig | AnthropicEvaluatorConfig | CommandEvaluatorConfig,
    Field(discriminator="type"),
]


class ChallengeConfig(StrictModel):
    """Discovery and verification performed by the current coding session."""

    count: int = Field(default=10, ge=1, le=100)
    max_rounds: int = Field(default=2, ge=1, le=10)
    max_continuations: int = Field(default=8, ge=1, le=100)
    watch: list[str] = Field(default_factory=lambda: ["**/*"])
    domain_context: list[str] = Field(default_factory=list)

    @field_validator("watch", "domain_context")
    @classmethod
    def validate_patterns(cls, patterns: list[str], info: Any) -> list[str]:
        if info.field_name == "domain_context" and not patterns:
            return patterns
        return _relative_patterns(patterns, info.field_name)


class LoopConfig(StrictModel):
    phase: Phase
    interval_seconds: float = Field(gt=0, le=86_400)
    max_repair_attempts: int = Field(ge=1, le=100)
    max_unchanged_repairs: int = Field(ge=1, le=100)
    max_duration_seconds: float = Field(gt=0, le=2_592_000)
    on_pass: Literal["stop"] = "stop"
    on_failure: Literal["repair"] = "repair"
    on_pending: Literal["wait"] = "wait"
    on_budget_exhausted: Literal["human_required"] = "human_required"
    challenge: ChallengeConfig | None = None

    @model_validator(mode="after")
    def validate_challenge_phase(self) -> LoopConfig:
        if self.challenge is not None and self.phase != Phase.STOP:
            raise ValueError("session challenge gates require phase: stop")
        return self


class Contract(StrictModel):
    version: Literal[1] = 1
    settings: ContractSettings = Field(default_factory=ContractSettings)
    constraints: dict[str, ConstraintSpec]
    evaluators: dict[str, EvaluatorConfig] = Field(default_factory=dict)
    loops: dict[str, LoopConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_graph_and_evaluators(self) -> Contract:
        for name in (*self.constraints, *self.evaluators, *self.loops):
            if not name or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
                for character in name
            ):
                raise ValueError(f"invalid identifier {name!r}")
        stop_loops = [name for name, loop in self.loops.items() if loop.phase == Phase.STOP]
        if len(stop_loops) > 1:
            raise ValueError(
                "at most one stop-phase loop is supported; found " + ", ".join(sorted(stop_loops))
            )
        known = set(self.constraints)
        for constraint_id, spec in self.constraints.items():
            unknown = set(spec.needs) - known
            if unknown:
                raise ValueError(
                    f"{constraint_id} depends on unknown constraints: {sorted(unknown)}"
                )
            for dependency in spec.needs:
                prerequisite = self.constraints[dependency]
                if spec.enabled and (
                    not prerequisite.enabled or not set(spec.phases).issubset(prerequisite.phases)
                ):
                    raise ValueError(
                        f"{constraint_id} requires {dependency} to be enabled "
                        "in every dependent phase"
                    )
            if isinstance(spec, RubricConstraint) and spec.evaluator not in self.evaluators:
                raise ValueError(f"{constraint_id} references unknown evaluator {spec.evaluator!r}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError(f"constraint dependency cycle includes {node!r}")
            if node in visited:
                return
            visiting.add(node)
            for dependency in self.constraints[node].needs:
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for constraint_id in self.constraints:
            visit(constraint_id)
        for loop_id, loop in self.loops.items():
            if not any(
                spec.enabled and loop.phase in spec.phases for spec in self.constraints.values()
            ):
                raise ValueError(
                    f"{loop_id} references phase {loop.phase.value!r} with no enabled constraints"
                )
        return self


def _relative_patterns(patterns: list[str], field: str) -> list[str]:
    if not patterns:
        raise ValueError(f"{field} must contain at least one pattern")
    for pattern in patterns:
        path = PurePosixPath(pattern)
        if not pattern or path.is_absolute() or ".." in path.parts or "\\" in pattern:
            raise ValueError(f"{field} patterns must be project-relative POSIX globs: {pattern!r}")
    return patterns


class Finding(StrictModel):
    message: str
    file_path: str | None = None
    line: int | None = Field(default=None, ge=1)
    suggestion: str | None = None


class EvaluatorCallMetadata(StrictModel):
    provider: str
    model: str
    response_id: str | None = None
    status: str
    attempts: int = Field(ge=1)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cli_version: str | None = None
    cost_usd: float | None = Field(default=None, ge=0)
    duration_ms: float = Field(ge=0)


class CommandAttempt(StrictModel):
    attempt: int = Field(ge=1)
    started_at: str
    duration_ms: float = Field(ge=0)
    exit_code: int | None = None
    output_tail: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def scrub_evidence(self) -> CommandAttempt:
        self.output_tail = redact_text(self.output_tail) if self.output_tail else None
        self.error = redact_text(self.error) if self.error else None
        return self


class ConstraintResult(StrictModel):
    constraint_id: str
    kind: str
    verdict: Verdict
    enforcement: Enforcement
    input_digest: str
    message: str
    duration_ms: float = Field(default=0, ge=0)
    exit_code: int | None = None
    value: float | None = None
    baseline: float | None = None
    delta: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    blocked_by: list[str] = Field(default_factory=list)
    evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    failure_category: FailureCategory | None = None
    output_tail: str | None = None
    attempts: list[CommandAttempt] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    evaluator_calls: list[EvaluatorCallMetadata] = Field(default_factory=list)
    cached: bool = False

    @model_validator(mode="after")
    def scrub_evidence(self) -> ConstraintResult:
        self.message = redact_text(self.message)
        self.output_tail = redact_text(self.output_tail) if self.output_tail else None
        self.details = redact_value(self.details)
        self.attempts = [
            CommandAttempt.model_validate(attempt.model_dump()) for attempt in self.attempts
        ]
        self.findings = [
            Finding.model_validate(redact_value(finding.model_dump())) for finding in self.findings
        ]
        return self

    @property
    def blocks(self) -> bool:
        return self.enforcement == Enforcement.REQUIRED and self.verdict not in {
            Verdict.PASS,
            Verdict.SKIPPED,
            Verdict.WAIVED,
        }


class EvidenceRecord(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    project_root: str
    contract_digest: str
    phase: Phase
    started_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    results: list[ConstraintResult]

    @property
    def passed(self) -> bool:
        return not any(result.blocks for result in self.results)


ChallengeText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)
]


class Challenge(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
    perspective: ChallengeText
    assumption: ChallengeText
    scenario: ChallengeText
    expected_behavior: ChallengeText
    verification_plan: ChallengeText
    source_refs: list[str] = Field(min_length=1, max_length=50)


class ChallengeResolution(StrictModel):
    challenge_id: str
    outcome: Literal["verified", "defect", "rejected", "unresolved"]
    evidence: ChallengeText
    constraint_ids: list[str] = Field(default_factory=list, max_length=50)
    source_refs: list[str] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_verified_evidence(self) -> ChallengeResolution:
        if self.outcome == "verified" and not self.constraint_ids:
            raise ValueError("verified challenges require deterministic constraint_ids")
        return self


class ChallengeSubmission(StrictModel):
    request_id: str
    input_snapshot: str


class DiscoverySubmission(ChallengeSubmission):
    kind: Literal["discovery"]
    domain_summary: ChallengeText
    domain_sources: list[str] = Field(min_length=1, max_length=50)
    challenges: list[Challenge] = Field(min_length=1, max_length=100)


class VerificationSubmission(ChallengeSubmission):
    kind: Literal["verification"]
    resolutions: list[ChallengeResolution] = Field(min_length=1, max_length=1000)


class RecordedResolution(ChallengeResolution):
    input_snapshot: str


class ChallengeLedger(StrictModel):
    request_id: str
    input_snapshot: str
    round: int = Field(default=1, ge=1)
    discovery_pending: bool = True
    followup_needed: bool = False
    domain_briefs: list[dict[str, Any]] = Field(default_factory=list)
    challenges: list[Challenge] = Field(default_factory=list)
    resolutions: dict[str, RecordedResolution] = Field(default_factory=dict)


class LoopState(StrEnum):
    PASSED = "passed"
    REPAIR = "repair"
    WAITING = "waiting"
    HUMAN_REQUIRED = "human_required"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ERROR = "error"
    CHALLENGE = "challenge"
    VERIFY = "verify"


class LoopJournal(StrictModel):
    schema_version: Literal[1] = 1
    loop: str
    contract_digest: str
    started_at: float
    updated_at: float
    observation: int = Field(default=0, ge=0)
    repair_attempt: int = Field(default=0, ge=0)
    unchanged_repairs: int = Field(default=0, ge=0)
    prior_state: LoopState | None = None
    prior_snapshot: str | None = None
    input_snapshot: str | None = None
    last_result: dict[str, Any] | None = None
    last_evidence: EvidenceRecord | None = None
    goal: str | None = None
    challenge: ChallengeLedger | None = None
    challenge_continuations: int = Field(default=0, ge=0)


class CycleResult(StrictModel):
    schema_version: Literal[1] = 1
    loop: str
    state: LoopState
    snapshot: str
    observation: int = Field(ge=1)
    repair_attempt: int = Field(ge=0)
    next_action: str
    wake_after_seconds: float = Field(ge=0)
    blocking_constraints: list[str] = Field(default_factory=list)
    blocking_challenges: list[str] = Field(default_factory=list)


class EvaluationBundle(StrictModel):
    schema_version: Literal[1] = 1
    constraint_id: str
    rubric: str
    goal: str | None = None
    diff: str
    deterministic_results: list[dict[str, Any]]
    files: dict[str, str]
    omitted_files: list[str] = Field(default_factory=list)


class EvaluatorVerdict(StrictModel):
    verdict: Literal["pass", "fail", "uncertain"]
    score: float | None = Field(default=None, ge=0, le=1)
    rationale: str
    findings: list[Finding] = Field(default_factory=list)


class Evaluator(Protocol):
    def evaluate(self, bundle: EvaluationBundle) -> EvaluatorVerdict:
        """Return a structured evaluator verdict."""
