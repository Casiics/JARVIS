"""The domain side of `TaskAssigned`: run a predefined job and report back as metadata.

A domain registers one handler per job it owns and runs `JobServer.serve()` next to its other
services. For every assignment from core the server reports `running`, calls the handler and
reports the final state with the duration and, on failure, an error class. Handlers keep every
input and output inside the domain; only the outcome's state and error class go to core.

Assignments for jobs the domain has no handler for, or that name another domain, are escalated
with an error class instead of being dropped, so the deck shows them.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import UUID

from jarvis_core.boundary.mailbox import Mailbox, MailboxError
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import JOB_DOMAIN, Job, TaskAssigned, TaskMetadata
from jarvis_core.orchestrator.state_machine import TaskState


@dataclass(frozen=True)
class JobOutcome:
    """What a handler tells core: a final state and, if it failed, why (a slug, no content)."""

    state: TaskState = TaskState.DONE
    error_class: str | None = None

    @classmethod
    def failed(cls, error_class: str) -> JobOutcome:
        return cls(TaskState.ESCALATED, error_class)


JobHandler = Callable[[TaskAssigned], JobOutcome]
_FINAL = frozenset({TaskState.DONE, TaskState.ESCALATED, TaskState.AWAITING_ACTION})


class JobServer:
    def __init__(
        self,
        mailbox: Mailbox,
        handlers: Mapping[Job, JobHandler],
        *,
        log: Callable[[str], object] = print,
    ) -> None:
        if mailbox.me is Domain.CORE:
            raise ValueError("jobs run in a domain, not in core")
        foreign = [job for job in handlers if JOB_DOMAIN[job] is not mailbox.me]
        if foreign:
            raise ValueError(f"handlers for another domain's jobs: {foreign}")
        self._mailbox = mailbox
        self._handlers = dict(handlers)
        self._log = log

    def handle_waiting(self) -> int:
        """Run every assignment waiting in the mailbox, oldest first. Returns how many."""
        handled = 0
        for letter in self._mailbox.receive(Domain.CORE):
            if isinstance(letter.event, TaskAssigned):
                self.run(letter.event)
                handled += 1
            self._mailbox.ack(letter)
        return handled

    def serve(self, poll_seconds: float, *, once: bool = False) -> None:
        while True:
            self.handle_waiting()
            if once:
                return
            time.sleep(poll_seconds)

    def run(self, event: TaskAssigned) -> JobOutcome:
        started = time.monotonic()

        def report(state: TaskState, error_class: str | None = None) -> None:
            elapsed = int((time.monotonic() - started) * 1000)
            self._send(event.task_id, state, elapsed, error_class)

        handler = self._handlers.get(event.job)
        if event.target_domain is not self._mailbox.me or handler is None:
            outcome = JobOutcome.failed("job_not_available")
            report(outcome.state, outcome.error_class)
            return outcome
        report(TaskState.RUNNING)
        try:
            outcome = handler(event)
        except Exception as exc:  # the job failed; core learns that, not the details
            self._log(f"{event.task_id}: {event.job.value} failed ({type(exc).__name__})")
            outcome = JobOutcome.failed("internal_error")
        if outcome.state not in _FINAL:
            outcome = JobOutcome.failed("internal_error")
        report(outcome.state, outcome.error_class)
        self._log(f"{event.task_id}: {event.job.value} {outcome.state.value}")
        return outcome

    def _send(
        self, task_id: UUID, state: TaskState, duration_ms: int, error_class: str | None
    ) -> None:
        try:
            self._mailbox.send(
                TaskMetadata(
                    origin_domain=self._mailbox.me,
                    target_domain=Domain.CORE,
                    classification=DataClassification.INTERNAL,
                    task_id=task_id,
                    state=state.value,
                    duration_ms=duration_ms,
                    error_class=error_class,
                )
            )
        except (MailboxError, OSError) as exc:
            # Core's deadline still escalates the task if no report arrives.
            self._log(f"{task_id}: report failed ({type(exc).__name__})")
