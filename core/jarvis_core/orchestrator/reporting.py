"""Stage-level progress from a domain to core, as metadata only.

A domain plugs `MetadataReporter.sink` into its `SupervisorWorkerLoop` as the telemetry sink.
Every time a task's state changes (worker, gate, supervisor, final), one `TaskMetadata` goes
through the domain's outgoing mailbox to core, where the orchestrator moves the task and the
deck shows the stage live. Repeated events in the same state are not re-sent, and nothing but
state, duration and error class leaves the domain.
"""

from __future__ import annotations

import threading
from uuid import UUID

from jarvis_core.boundary.mailbox import Mailbox, MailboxError
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import TaskMetadata
from jarvis_core.contracts.telemetry import TelemetryEvent


class MetadataReporter:
    def __init__(self, mailbox: Mailbox) -> None:
        if mailbox.me is Domain.CORE:
            raise ValueError("core does not report to itself")
        self._mailbox = mailbox
        self._last: dict[UUID, str] = {}
        self._lock = threading.Lock()

    def sink(self, event: TelemetryEvent) -> None:
        with self._lock:
            if self._last.get(event.task_id) == event.state.value:
                return
            self._last[event.task_id] = event.state.value
        try:
            self._mailbox.send(
                TaskMetadata(
                    origin_domain=self._mailbox.me,
                    target_domain=Domain.CORE,
                    classification=DataClassification.INTERNAL,
                    task_id=event.task_id,
                    state=event.state.value,
                    duration_ms=event.duration_ms,
                    error_class=event.error_class,
                )
            )
        except (MailboxError, OSError):
            # Progress reports are best effort; the final report and core's deadline still hold.
            with self._lock:
                self._last.pop(event.task_id, None)

    def forget(self, task_id: UUID) -> None:
        with self._lock:
            self._last.pop(task_id, None)
