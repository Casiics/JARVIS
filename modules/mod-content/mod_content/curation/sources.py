"""Stage 1 sources of the content domain.

Reads reel results only through the reel pipeline's run records (runs.jsonl); it never touches
reel internals or media. Each successful run becomes one CollectedItem. Re-runs of the same reel
are collected too and folded together in stage 2.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from jarvis_core.contracts.classification import Domain
from jarvis_core.curation.models import CollectedItem
from jarvis_core.curation.similarity import fingerprint
from jarvis_core.orchestrator.state_machine import TaskState
from mod_content.reel.store import JsonlReelStore, ReelRunRecord

USABLE_STATES = frozenset({TaskState.DONE, TaskState.AWAITING_ACTION})


def reel_items(runs_file: Path) -> Iterator[CollectedItem]:
    for record in JsonlReelStore(runs_file).load():
        item = reel_item(record)
        if item is not None:
            yield item


def reel_item(record: ReelRunRecord) -> CollectedItem | None:
    artifact = record.artifact
    if artifact is None or record.state not in USABLE_STATES:
        return None
    facts = (*artifact.extracted_tools, *artifact.actionable_steps)
    return CollectedItem(
        item_id=f"reel:{record.run_id.hex}",
        domain=Domain.CONTENT,
        classification=artifact.classification,
        taint=artifact.taint,
        source_ref=artifact.source_ref,
        collected_at=record.finished_at,
        title=_title(artifact.summary),
        summary=artifact.summary,
        facts=facts,
        tags=(artifact.target_module.value, *(t.casefold() for t in artifact.extracted_tools)),
        signal=artifact.utility_score,
        flags=artifact.injection_flags,
        content_hash=fingerprint(artifact.summary, *facts),
    )


def _title(summary: str) -> str:
    first = summary.strip().split(". ")[0].rstrip(".")
    return first if len(first) <= 120 else first[:117].rstrip() + "..."
