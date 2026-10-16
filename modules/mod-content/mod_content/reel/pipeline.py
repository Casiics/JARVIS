"""Reel pipeline: URL in, validated ReelKnowledgeArtifact out (docs/architecture.md, 6.2).

fetch (sandbox, egress) -> extract (sandbox, no network) -> transcribe (sandbox, GPU, no LLM
loaded) -> keyframe reading (local vision model) -> quarantined extraction (local model,
no tools, max 4 iterations) -> Tier 0b -> supervisor QA -> follow-ups via boundary policy.

Every step is authorised by the task token and audited; the audit holds hashes and counts.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import BoundaryEvent, Taint, TaskMetadata
from jarvis_core.contracts.reel_url import canonical_reel_url
from jarvis_core.contracts.task_token import TaskTokenClaims, TaskTokenError, verify_token
from jarvis_core.orchestrator.state_machine import Event, Task, TaskMachine, TaskState
from jarvis_core.policy.flows import check_flow
from jarvis_core.router.client import ModelClientError
from jarvis_core.router.gateway import GatewayResult, InferenceGateway
from jarvis_core.router.policy import RoutingDenied
from mod_content.reel.dispatch import PROPOSE_SKILL_TOOL, FollowUpPlan, plan_follow_ups
from mod_content.reel.gate import check_draft
from mod_content.reel.media import (
    MediaBundle,
    MediaRejected,
    load_extract,
    load_fetch,
    load_transcript,
)
from mod_content.reel.quarantine import QuarantinedWorker
from mod_content.reel.sandbox import (
    REEL_EGRESS_ALLOWLIST,
    SandboxMount,
    SandboxRunner,
    SandboxSpec,
)
from mod_content.reel.schemas import (
    ExtractionDraft,
    ReelKnowledgeArtifact,
    Transcript,
)
from mod_content.reel.store import ReelRunRecord, StageMetric

TOOL_FETCH = "sandbox.fetch"
TOOL_EXTRACT = "sandbox.extract"
TOOL_TRANSCRIBE = "sandbox.transcribe"
TOOL_VISION = "model.vision"
TOOL_EXTRACTION = "model.extract"
REEL_TOOLS = frozenset(
    {TOOL_FETCH, TOOL_EXTRACT, TOOL_TRANSCRIBE, TOOL_VISION, TOOL_EXTRACTION, PROPOSE_SKILL_TOOL}
)


class CountingSink:
    """Wraps the audit writer so a run knows how many entries it produced."""

    def __init__(self, sink: Callable[[AuditLogEntry], object]) -> None:
        self._sink = sink
        self.count = 0

    def __call__(self, entry: AuditLogEntry) -> None:
        self._sink(entry)
        self.count += 1


@dataclass(frozen=True)
class QaVerdict:
    accept: bool
    feedback: tuple[str, ...] = ()


class Supervisor(Protocol):
    def review(self, artifact: ReelKnowledgeArtifact) -> QaVerdict: ...


class RuleSupervisor:
    """Deterministic QA until the cloud supervisor client exists (content's supervisor is cloud).

    Checks the semantic minimum that Tier 0b cannot: enough substance for the claimed utility.
    """

    def review(self, artifact: ReelKnowledgeArtifact) -> QaVerdict:
        feedback: list[str] = []
        if len(artifact.summary) < 40:
            feedback.append("summary is too short to be useful; describe what the video teaches")
        if artifact.utility_score >= 0.8 and len(artifact.actionable_steps) < 2:
            feedback.append("utility_score >= 0.8 needs at least two concrete actionable_steps")
        return QaVerdict(not feedback, tuple(feedback))


@dataclass(frozen=True)
class SandboxImages:
    media: str = "localhost/jarvis-reel-media:0.1.0"
    whisper: str = "localhost/jarvis-reel-whisper:0.1.0"


@dataclass(frozen=True)
class WhisperConfig:
    models_dir: Path
    model: str = "large-v3"
    compute_type: str = "int8_float16"


class _StepFailed(Exception):
    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


@dataclass
class ReelPipeline:
    runner: SandboxRunner
    gateway: InferenceGateway
    audit: CountingSink
    token_key: bytes
    work_root: Path
    whisper: WhisperConfig
    images: SandboxImages = field(default_factory=SandboxImages)
    supervisor: Supervisor = field(default_factory=RuleSupervisor)
    outbox: Callable[[BoundaryEvent], object] = field(default=lambda _e: None)
    actor: str = "spiffe://jarvis.home/content/reel-pipeline"
    keep_media: bool = False
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def run(self, url: str, token: str) -> ReelRunRecord:
        source_ref = canonical_reel_url(url)
        reel_id = source_ref.rstrip("/").rsplit("/", 1)[-1]
        run_id = uuid4()
        trace_id = run_id.hex
        started = self.clock()
        audit_before = self.audit.count
        usage: list[GatewayResult] = []
        stages: list[StageMetric] = []
        machine = TaskMachine(self.audit, actor=self.actor)

        claims = self._claims(token, trace_id)
        task = Task(task_id=claims.task_id, domain=Domain.CONTENT, trace_id=trace_id)
        task = machine.apply(task, Event.START)
        work = self.work_root / str(run_id)
        artifact: ReelKnowledgeArtifact | None = None
        plan = FollowUpPlan()
        error_class: str | None = None

        try:
            bundle, transcript = self._media(claims, work, source_ref, trace_id, stages)
            self._use(claims, TOOL_VISION, trace_id)
            ocr, vision_usage = self._timed(
                stages,
                "vision",
                lambda: QuarantinedWorker(self.gateway).read_keyframes(
                    bundle.frames, task_id=claims.task_id, trace_id=trace_id
                ),
            )
            usage.extend(vision_usage)
            self._use(claims, TOOL_EXTRACTION, trace_id)
            task, artifact, plan = self._extract_loop(
                machine, task, claims, bundle, transcript, ocr, reel_id, source_ref, usage, stages
            )
        except _StepFailed as exc:
            error_class = exc.error_class
            task = machine.apply(task, Event.STEP_FAILED)
        finally:
            if not self.keep_media and work.exists():
                shutil.rmtree(work, ignore_errors=True)

        if artifact is not None and task.state is not TaskState.ESCALATED:
            self._emit_proposals(plan, trace_id, claims.task_id)
        if task.state is TaskState.ESCALATED and error_class is None:
            error_class = "max_iterations"
        self._report_metadata(task, started, error_class)

        return ReelRunRecord(
            run_id=run_id,
            task_id=claims.task_id,
            trace_id=trace_id,
            reel_id=reel_id,
            source_ref=source_ref,
            started_at=started,
            finished_at=self.clock(),
            state=task.state,
            iterations=task.iterations,
            error_class=error_class,
            stages=tuple(stages),
            model_calls=len(usage),
            tokens=sum(u.prompt_tokens + u.completion_tokens for u in usage),
            cost_eur=round(sum(u.cost_eur for u in usage), 6),
            models=tuple(sorted({u.route.model for u in usage})),
            audit_entries=self.audit.count - audit_before,
            artifact=artifact,
            follow_ups=_describe(plan),
            awaiting_approval=task.state is TaskState.AWAITING_ACTION,
        )

    # --- steps -------------------------------------------------------------------------------

    def _media(
        self,
        claims: TaskTokenClaims,
        work: Path,
        source_ref: str,
        trace_id: str,
        stages: list[StageMetric],
    ) -> tuple[MediaBundle, Transcript]:
        for step in ("fetch", "extract", "transcribe"):
            (work / step).mkdir(parents=True, exist_ok=True)
            # The container runs as an unprivileged user and must write its own output only.
            (work / step).chmod(0o777)

        self._use(claims, TOOL_FETCH, trace_id)
        self._sandbox(
            stages,
            SandboxSpec(
                step="fetch",
                image=self.images.media,
                command=("reel-media", "fetch", source_ref),
                mounts=(SandboxMount(source=work / "fetch", target="/out", read_only=False),),
                egress_allowlist=REEL_EGRESS_ALLOWLIST,
                timeout_s=180,
            ),
        )
        media_hash = self._check(lambda: load_fetch(work))
        self._audit_tool(claims, TOOL_FETCH, trace_id, AuditDecision.ALLOW, media_hash)

        self._use(claims, TOOL_EXTRACT, trace_id)
        self._sandbox(
            stages,
            SandboxSpec(
                step="extract",
                image=self.images.media,
                command=("reel-media", "extract"),
                mounts=(
                    SandboxMount(source=work / "fetch", target="/in"),
                    SandboxMount(source=work / "extract", target="/out", read_only=False),
                ),
            ),
        )
        bundle = self._check(lambda: load_extract(work, media_hash))

        self._use(claims, TOOL_TRANSCRIBE, trace_id)
        # One GPU: Faster-Whisper runs only while no LLM is loaded in Ollama.
        try:
            self.gateway.unload_local_models()
        except ModelClientError as exc:
            raise _StepFailed("model_unavailable") from exc
        self._sandbox(
            stages,
            SandboxSpec(
                step="transcribe",
                image=self.images.whisper,
                command=(
                    "reel-transcribe",
                    "--model",
                    self.whisper.model,
                    "--compute-type",
                    self.whisper.compute_type,
                ),
                mounts=(
                    SandboxMount(source=work / "extract", target="/in"),
                    SandboxMount(source=self.whisper.models_dir, target="/models"),
                    SandboxMount(source=work / "transcribe", target="/out", read_only=False),
                ),
                gpu=True,
                memory_mb=8192,
                timeout_s=600,
            ),
        )
        transcript = self._check(lambda: load_transcript(work))
        self._audit_tool(
            claims, TOOL_TRANSCRIBE, trace_id, AuditDecision.ALLOW, _sha(transcript.text)
        )
        return bundle, transcript

    def _extract_loop(
        self,
        machine: TaskMachine,
        task: Task,
        claims: TaskTokenClaims,
        bundle: MediaBundle,
        transcript: Transcript,
        ocr: tuple[str, ...],
        reel_id: str,
        source_ref: str,
        usage: list[GatewayResult],
        stages: list[StageMetric],
    ) -> tuple[Task, ReelKnowledgeArtifact | None, FollowUpPlan]:
        worker = QuarantinedWorker(self.gateway)
        feedback: tuple[str, ...] = ()
        while True:
            result = self._timed(
                stages,
                f"extract_{task.iterations}",
                partial(
                    worker.extract,
                    bundle.metadata,
                    transcript.text,
                    ocr,
                    feedback=feedback,
                    task_id=claims.task_id,
                    trace_id=task.trace_id,
                ),
            )
            usage.append(result)
            task = machine.apply(task, Event.WORKER_FINISHED)
            report = check_draft(result.text)
            if report.draft is None or not report.passed:
                task = machine.apply(task, Event.GATE_FAILED)
                if task.state is TaskState.ESCALATED:
                    return task, None, FollowUpPlan()
                feedback = report.errors
                continue
            task = machine.apply(task, Event.GATE_PASSED)
            artifact = _artifact(
                report.draft,
                report.injection_flags,
                reel_id,
                source_ref,
                claims.task_id,
                bundle.media_hash,
                transcript,
                self.clock(),
            )
            verdict = self.supervisor.review(artifact)
            if not verdict.accept:
                task = machine.apply(task, Event.QA_REJECTED)
                if task.state is TaskState.ESCALATED:
                    return task, None, FollowUpPlan()
                feedback = verdict.feedback
                continue
            plan = plan_follow_ups(artifact, claims)
            event = Event.QA_ACCEPTED_WITH_ACTION if plan.awaiting_approval else Event.QA_ACCEPTED
            return machine.apply(task, event), artifact, plan

    # --- helpers -----------------------------------------------------------------------------

    def _claims(self, token: str, trace_id: str) -> TaskTokenClaims:
        try:
            claims = verify_token(self.token_key, token)
        except TaskTokenError as exc:
            self._audit_raw(None, trace_id, "task_token", AuditDecision.DENY, str(exc))
            raise
        if claims.domain is not Domain.CONTENT:
            self._audit_raw(claims.task_id, trace_id, "task_token", AuditDecision.DENY, "domain")
            raise TaskTokenError("task token is not for the content domain")
        return claims

    def _use(self, claims: TaskTokenClaims, tool: str, trace_id: str) -> None:
        if tool not in claims.allowed_tools:
            self._audit_tool(claims, tool, trace_id, AuditDecision.DENY, None)
            raise _StepFailed("tool_not_allowed")

    def _sandbox(self, stages: list[StageMetric], spec: SandboxSpec) -> None:
        result = self.runner.run(spec)
        stages.append(StageMetric(name=spec.step, duration_ms=result.duration_ms, ok=result.ok))
        if not result.ok:
            raise _StepFailed(f"{spec.step}_{'timeout' if result.timed_out else 'failed'}")

    def _timed[T](self, stages: list[StageMetric], name: str, fn: Callable[[], T]) -> T:
        start = self.clock()
        ok = False
        try:
            value = fn()
            ok = True
            return value
        except RoutingDenied as exc:
            raise _StepFailed("routing_denied") from exc
        except ModelClientError as exc:
            raise _StepFailed("model_unavailable") from exc
        finally:
            ms = int((self.clock() - start).total_seconds() * 1000)
            stages.append(StageMetric(name=name, duration_ms=max(ms, 0), ok=ok))

    @staticmethod
    def _check[T](fn: Callable[[], T]) -> T:
        try:
            return fn()
        except MediaRejected as exc:
            raise _StepFailed("media_rejected") from exc

    def _audit_tool(
        self,
        claims: TaskTokenClaims,
        tool: str,
        trace_id: str,
        decision: AuditDecision,
        content_hash: str | None,
    ) -> None:
        self.audit(
            AuditLogEntry(
                trace_id=trace_id,
                task_id=claims.task_id,
                domain=Domain.CONTENT,
                actor=self.actor,
                action=AuditAction.TOOL_CALL,
                target=tool,
                decision=decision,
                taint=Taint.EXTERNAL_CONTENT,
                content_hash=content_hash,
            )
        )

    def _audit_raw(
        self, task_id: UUID | None, trace_id: str, target: str, decision: AuditDecision, why: str
    ) -> None:
        self.audit(
            AuditLogEntry(
                trace_id=trace_id,
                task_id=task_id,
                domain=Domain.CONTENT,
                actor=self.actor,
                action=AuditAction.TOOL_CALL,
                target=target,
                decision=decision,
                policy_id=why[:100],
            )
        )

    def _emit_proposals(self, plan: FollowUpPlan, trace_id: str, task_id: UUID) -> None:
        for event in plan.proposals:
            self.audit(
                AuditLogEntry(
                    trace_id=trace_id,
                    task_id=task_id,
                    domain=Domain.CONTENT,
                    actor=self.actor,
                    action=AuditAction.BOUNDARY_EVENT,
                    target=f"{event.origin_domain}->{event.target_domain}",
                    decision=AuditDecision.ALLOW,
                    taint=event.taint,
                    content_ref=f"event:{event.event_id}",
                )
            )
            self.outbox(event)
        for request, decision in plan.actions:
            self.audit(
                AuditLogEntry(
                    trace_id=trace_id,
                    task_id=task_id,
                    domain=Domain.CONTENT,
                    actor=self.actor,
                    action=AuditAction.GATE_DECISION,
                    target=request.tool,
                    decision=AuditDecision.ESCALATE,
                    policy_id=decision.outcome.value,
                    taint=request.taint,
                    content_ref=f"action:{request.request_id}",
                )
            )

    def _report_metadata(self, task: Task, started: datetime, error_class: str | None) -> None:
        event = TaskMetadata(
            origin_domain=Domain.CONTENT,
            target_domain=Domain.CORE,
            classification=DataClassification.INTERNAL,
            task_id=task.task_id,
            state=task.state.value,
            duration_ms=max(int((self.clock() - started).total_seconds() * 1000), 0),
            error_class=error_class,
        )
        if check_flow(event, caller_domain=Domain.CONTENT).allowed:
            self.outbox(event)


def _artifact(
    draft: ExtractionDraft,
    flags: tuple[str, ...],
    reel_id: str,
    source_ref: str,
    task_id: UUID,
    media_hash: str,
    transcript: Transcript,
    now: datetime,
) -> ReelKnowledgeArtifact:
    return ReelKnowledgeArtifact(
        reel_id=reel_id,
        source_ref=source_ref,
        task_id=task_id,
        created_at=now,
        summary=draft.summary,
        extracted_tools=draft.extracted_tools,
        actionable_steps=draft.actionable_steps,
        target_module=draft.target_module,
        utility_score=draft.utility_score,
        injection_flags=flags,
        media_hash=media_hash,
        transcript_hash=_sha(transcript.text),
    )


def _describe(plan: FollowUpPlan) -> tuple[str, ...]:
    items = [f"proposal:{type(e).__name__}" for e in plan.proposals]
    if plan.template_candidate:
        items.append("content_template_candidate")
    items.extend(f"action:{r.tool}:{d.outcome.value}" for r, d in plan.actions)
    items.extend(f"denied:{reason}" for reason in plan.denied)
    if plan.held_for_review:
        items.append("held_for_human_review")
    return tuple(items)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
