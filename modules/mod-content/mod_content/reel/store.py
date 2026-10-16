"""Run records, the cost and audit report, and file-backed stores for a single workstation.

Production keeps run records in pg-content and the audit chain in pg-audit
(jarvis_core.audit.postgres). The JSONL stores here let Phase 2 run without those services
and enforce the same contracts: the audit file is append-only and re-verified on open.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.audit.chain import (
    GENESIS_HASH,
    AuditChainError,
    ChainedEntry,
    compute_hash,
    verify_chain,
)
from jarvis_core.orchestrator.state_machine import TaskState
from mod_content.reel.schemas import ReelKnowledgeArtifact

EXIT_CRITERION_REELS = 20


class StageMetric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    duration_ms: int = Field(ge=0)
    ok: bool


class ReelRunRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    task_id: UUID
    trace_id: str
    reel_id: str
    source_ref: str
    started_at: datetime
    finished_at: datetime
    state: TaskState
    iterations: int = Field(ge=0)
    error_class: str | None = None
    stages: tuple[StageMetric, ...] = ()
    model_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    cost_eur: float = Field(default=0.0, ge=0)
    models: tuple[str, ...] = ()
    audit_entries: int = Field(default=0, ge=0)
    artifact: ReelKnowledgeArtifact | None = None
    follow_ups: tuple[str, ...] = ()
    awaiting_approval: bool = False

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at).total_seconds() * 1000)


class ReelReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: int
    reels_processed: int
    done: int
    awaiting_approval: int
    escalated: int
    flagged_for_review: int
    cost_eur_total: float
    cost_eur_per_reel: float
    tokens_total: int
    model_calls_total: int
    avg_duration_s: float
    by_target: dict[str, int]
    audit_entries: int
    audit_chain_intact: bool | None
    exit_criterion_met: bool


def summarize(records: list[ReelRunRecord], audit_chain_intact: bool | None) -> ReelReport:
    processed = {
        r.reel_id
        for r in records
        if r.state in (TaskState.DONE, TaskState.AWAITING_ACTION) and r.artifact is not None
    }
    by_target: dict[str, int] = {}
    for r in records:
        if r.artifact is not None:
            key = r.artifact.target_module.value
            by_target[key] = by_target.get(key, 0) + 1
    cost = sum(r.cost_eur for r in records)
    return ReelReport(
        runs=len(records),
        reels_processed=len(processed),
        done=sum(r.state is TaskState.DONE for r in records),
        awaiting_approval=sum(r.state is TaskState.AWAITING_ACTION for r in records),
        escalated=sum(r.state is TaskState.ESCALATED for r in records),
        flagged_for_review=sum(
            r.artifact is not None and r.artifact.needs_human_review for r in records
        ),
        cost_eur_total=round(cost, 6),
        cost_eur_per_reel=round(cost / len(processed), 6) if processed else 0.0,
        tokens_total=sum(r.tokens for r in records),
        model_calls_total=sum(r.model_calls for r in records),
        avg_duration_s=round(sum(r.duration_ms for r in records) / len(records) / 1000, 1)
        if records
        else 0.0,
        by_target=by_target,
        audit_entries=sum(r.audit_entries for r in records),
        audit_chain_intact=audit_chain_intact,
        exit_criterion_met=len(processed) >= EXIT_CRITERION_REELS and audit_chain_intact is True,
    )


class JsonlReelStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, record: ReelRunRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")

    def load(self) -> list[ReelRunRecord]:
        if not self._path.exists():
            return []
        with self._path.open(encoding="utf-8") as fh:
            return [ReelRunRecord.model_validate_json(line) for line in fh if line.strip()]


class FileAuditStore:
    """Append-only JSONL audit chain for a workstation. Implements jarvis_core AuditStore."""

    def __init__(self, path: Path) -> None:
        self._path = path
        entries = self.entries()
        broken = verify_chain(entries)
        if broken is not None:
            raise AuditChainError(f"audit file is broken at entry {broken}")
        self._head = entries[-1].entry_hash if entries else GENESIS_HASH

    def head(self) -> str:
        return self._head

    def append(self, chained: ChainedEntry) -> None:
        if chained.prev_hash != self._head:
            raise AuditChainError("prev_hash does not match current head")
        if chained.entry_hash != compute_hash(chained.prev_hash, chained.payload):
            raise AuditChainError("entry_hash does not match payload")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"payload": chained.payload, "prev": chained.prev_hash, "hash": chained.entry_hash}
        )
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self._head = chained.entry_hash

    def entries(self) -> list[ChainedEntry]:
        if not self._path.exists():
            return []
        out: list[ChainedEntry] = []
        with self._path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    item = json.loads(line)
                    out.append(ChainedEntry(item["payload"], item["prev"], item["hash"]))
        return out
