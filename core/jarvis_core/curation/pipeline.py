"""The four curation stages.

1. collect: accept items from domain sources, refuse anything from another domain or labelled
   SECRET, skip items already collected.
2. consolidate: cluster exact and near duplicates and merge each cluster into one digest.
   Snapshots (items with a subject) are grouped by subject instead; the newest one wins.
3. prepare: pick the category, compare against the context already stored there and propose
   store, merge or discard with a confidence and its reasons.
4. decide: deterministic policy. Confident proposals are applied automatically; uncertain,
   flagged or over-classified ones wait for the owner's review.

No stage calls a model: LLMs may produce the raw items upstream, but what enters the context is
decided by code (CLAUDE.md, rule 5). Every stage writes hashes and ids to the audit chain.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import DataClassification
from jarvis_core.contracts.events import Taint
from jarvis_core.curation.models import (
    CategoryRule,
    CollectedItem,
    ContextEntry,
    CurationConfig,
    DecisionBrief,
    DecisionRecord,
    Digest,
    Outcome,
    Proposal,
    StageCounts,
)
from jarvis_core.curation.similarity import fingerprint, normalize, similarity, tokens, unique
from jarvis_core.curation.store import CurationStore, consumed_item_ids, open_reviews

MAX_FACTS = 30
MAX_TAGS = 20
MIN_DISCARD_CONFIDENCE = 0.6


class ReviewNotFound(Exception):
    """No open review with that id."""


@dataclass
class CurationPipeline:
    config: CurationConfig
    store: CurationStore
    audit: Callable[[AuditLogEntry], object]
    actor: str
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    # --- stage 1 -----------------------------------------------------------------------------

    def collect(self, items: Iterable[CollectedItem], trace_id: str) -> tuple[int, int]:
        """Returns (newly collected, rejected)."""
        known = {i.item_id for i in self.store.items()}
        fresh: list[CollectedItem] = []
        rejected = 0
        for item in items:
            if item.item_id in known:
                continue
            reason = self._refusal(item)
            decision = AuditDecision.DENY if reason else AuditDecision.ALLOW
            self._audit("collect", decision, trace_id, item.item_id, item.content_hash, reason)
            if reason:
                rejected += 1
                continue
            known.add(item.item_id)
            fresh.append(item)
        self.store.add_items(fresh)
        return len(fresh), rejected

    def _refusal(self, item: CollectedItem) -> str | None:
        if item.domain is not self.config.domain:
            return "foreign_domain"
        if item.classification is DataClassification.SECRET:
            return "secret_never_context"
        return None

    # --- stage 2 -----------------------------------------------------------------------------

    def consolidate(self, trace_id: str) -> list[Digest]:
        consumed = consumed_item_ids(self.store.decisions())
        pending = [i for i in self.store.items() if i.item_id not in consumed]
        digests = [self._digest(cluster) for cluster in self._clusters(pending)]
        for d in digests:
            self._audit(
                "consolidate",
                AuditDecision.ALLOW,
                trace_id,
                d.digest_id,
                d.content_hash,
                f"members:{len(d.member_ids)};dropped:{d.duplicates_dropped}",
            )
        return digests

    def _clusters(self, items: list[CollectedItem]) -> list[list[CollectedItem]]:
        snapshots: dict[str, list[CollectedItem]] = {}
        for item in items:
            if item.subject is not None:
                snapshots.setdefault(item.subject, []).append(item)
        loose = [i for i in items if i.subject is None]
        return [*self._similar(loose), *snapshots.values()]

    def _similar(self, items: list[CollectedItem]) -> list[list[CollectedItem]]:
        parent = list(range(len(items)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        toks = [tokens(_text(i.title, i.summary, i.facts)) for i in items]
        for a in range(len(items)):
            for b in range(a + 1, len(items)):
                same = (
                    items[a].content_hash == items[b].content_hash
                    or items[a].source_ref == items[b].source_ref
                    or similarity(toks[a], toks[b]) >= self.config.duplicate_similarity
                )
                if same:
                    parent[find(b)] = find(a)
        groups: dict[int, list[CollectedItem]] = {}
        for index, item in enumerate(items):
            groups.setdefault(find(index), []).append(item)
        return list(groups.values())

    def _digest(self, cluster: list[CollectedItem]) -> Digest:
        subject = cluster[0].subject
        if subject is None:
            ranked = sorted(cluster, key=lambda i: (-i.signal, i.collected_at, i.item_id))
        else:  # an older snapshot is superseded, not corroborating
            ranked = sorted(cluster, key=lambda i: (-i.collected_at.timestamp(), i.item_id))[:1]
        best = ranked[0]
        member_ids = tuple(sorted(i.item_id for i in cluster))
        facts = unique([f for i in ranked for f in i.facts], MAX_FACTS)
        return Digest(
            digest_id="dg:" + _short(*member_ids),
            domain=self.config.domain,
            classification=max((i.classification for i in cluster), key=lambda c: c.rank),
            taint=_taint(i.taint for i in cluster),
            member_ids=member_ids,
            duplicates_dropped=len(cluster) - 1,
            source_refs=unique([i.source_ref for i in ranked], 50),
            title=best.title,
            summary=best.summary,
            facts=facts,
            tags=unique([t for i in ranked for t in i.tags], MAX_TAGS),
            signal=best.signal,
            flags=unique([f for i in cluster for f in i.flags], 20),
            content_hash=fingerprint(best.summary, *facts),
            subject=subject,
        )

    # --- stage 3 -----------------------------------------------------------------------------

    def prepare(self, digest: Digest, trace_id: str) -> DecisionBrief:
        if digest.subject is not None:
            return self._brief(self._prepare_snapshot(digest), trace_id)
        rule = self._category(digest)
        here = tokens(_text(digest.title, digest.summary, digest.facts))
        nearest: ContextEntry | None = None
        nearest_sim = 0.0
        for entry in self.store.entries(rule.name):
            sim = similarity(here, tokens(_text(entry.title, entry.summary, entry.facts)))
            if sim > nearest_sim:
                nearest, nearest_sim = entry, sim
        known_facts = {normalize(f) for f in nearest.facts} if nearest else set()
        new_facts = tuple(f for f in digest.facts if normalize(f) not in known_facts)
        novelty = round(1.0 - nearest_sim, 4)
        corroboration = min(1.0, (digest.corroboration - 1) / 2)
        reasons: list[str] = [f"category {rule.name}", f"signal {digest.signal:.2f}"]
        if digest.corroboration > 1:
            reasons.append(f"seen in {digest.corroboration} sources")
        if digest.duplicates_dropped:
            reasons.append(f"{digest.duplicates_dropped} duplicate(s) merged")

        if digest.signal < rule.min_signal:
            proposal, confidence = Proposal.DISCARD, 1.0 - digest.signal
            reasons.append(f"signal below category minimum {rule.min_signal:.2f}")
        elif nearest is not None and nearest_sim >= self.config.known_similarity and not new_facts:
            proposal, confidence = Proposal.DISCARD, nearest_sim
            reasons.append("already in context with no new facts")
        elif nearest is not None and nearest_sim >= self.config.merge_similarity:
            proposal = Proposal.MERGE
            # Closeness to an entry that was already accepted lowers the risk of a merge.
            confidence = (
                0.6 * digest.signal + 0.25 * nearest_sim + 0.15 * min(1.0, len(new_facts) / 2)
            )
            reasons.append(f"extends an existing entry ({len(new_facts)} new fact(s))")
        else:
            proposal = Proposal.STORE
            confidence = 0.6 * digest.signal + 0.25 * novelty + 0.15 * corroboration
            reasons.append(f"novelty {novelty:.2f}")

        brief = DecisionBrief(
            brief_id="br:" + digest.digest_id.removeprefix("dg:"),
            digest=digest,
            category=rule.name,
            proposal=proposal,
            confidence=round(min(1.0, max(0.0, confidence)), 4),
            novelty=novelty,
            nearest_entry_id=nearest.entry_id if nearest else None,
            nearest_similarity=round(nearest_sim, 4),
            new_facts=new_facts,
            reasons=tuple(reasons),
        )
        return self._brief(brief, trace_id)

    def _prepare_snapshot(self, digest: Digest) -> DecisionBrief:
        """A snapshot replaces the entry with the same subject; its confidence is its signal."""
        rule = self._category(digest)
        current = next(
            (e for e in self.store.entries(rule.name) if e.subject == digest.subject), None
        )
        reasons = [f"category {rule.name}", f"snapshot of {digest.subject}"]
        if digest.duplicates_dropped:
            reasons.append(f"{digest.duplicates_dropped} older snapshot(s) superseded")
        if digest.signal < rule.min_signal:
            proposal, confidence = Proposal.DISCARD, 1.0 - digest.signal
            reasons.append(f"signal below category minimum {rule.min_signal:.2f}")
        elif current is not None and (current.summary, current.facts) == (
            digest.summary,
            digest.facts,
        ):
            proposal, confidence = Proposal.DISCARD, 1.0
            reasons.append("unchanged since the stored snapshot")
        elif current is not None:
            proposal, confidence = Proposal.MERGE, digest.signal
            reasons.append(f"replaces version {current.version}")
        else:
            proposal, confidence = Proposal.STORE, digest.signal
            reasons.append("first snapshot")
        return DecisionBrief(
            brief_id="br:" + digest.digest_id.removeprefix("dg:"),
            digest=digest,
            category=rule.name,
            proposal=proposal,
            confidence=round(min(1.0, max(0.0, confidence)), 4),
            novelty=0.0 if current else 1.0,
            nearest_entry_id=current.entry_id if current else None,
            nearest_similarity=1.0 if current else 0.0,
            new_facts=digest.facts,
            reasons=tuple(reasons),
        )

    def _brief(self, brief: DecisionBrief, trace_id: str) -> DecisionBrief:
        self._audit(
            "prepare",
            AuditDecision.ALLOW,
            trace_id,
            brief.brief_id,
            brief.digest.content_hash,
            f"{brief.proposal.value}:{brief.category}",
        )
        return brief

    def _category(self, digest: Digest) -> CategoryRule:
        tags = {normalize(t) for t in digest.tags}
        words = tokens(_text(digest.title, digest.summary, digest.facts))
        best: tuple[int, CategoryRule] | None = None
        for rule in self.config.categories:
            score = 2 * len(tags & {normalize(t) for t in rule.tags})
            score += sum(1 for k in rule.keywords if normalize(k) in words)
            if score > 0 and (best is None or score > best[0]):
                best = (score, rule)
        return best[1] if best else self.config.category(self.config.default_category)

    # --- stage 4 -----------------------------------------------------------------------------

    def decide(self, brief: DecisionBrief, trace_id: str) -> DecisionRecord:
        rule = self.config.category(brief.category)
        digest = brief.digest
        outcome: Outcome
        if digest.flags:
            outcome, reason = Outcome.REVIEW, "flagged: " + ", ".join(digest.flags)[:200]
        elif not rule.max_classification.at_least(digest.classification):
            outcome, reason = Outcome.REVIEW, f"classification {digest.classification.value}"
        elif brief.proposal is Proposal.DISCARD:
            if brief.confidence >= MIN_DISCARD_CONFIDENCE:
                outcome, reason = Outcome.DISCARDED, brief.reasons[-1]
            else:
                outcome, reason = Outcome.REVIEW, "discard is uncertain"
        elif brief.confidence >= rule.auto_store_at:
            outcome, reason = self._apply(brief), f"confidence {brief.confidence:.2f}"
        elif brief.confidence < rule.auto_discard_below:
            outcome, reason = Outcome.DISCARDED, f"confidence {brief.confidence:.2f} too low"
        else:
            outcome, reason = Outcome.REVIEW, f"confidence {brief.confidence:.2f} needs review"
        return self._record(brief, outcome, self.actor, reason, trace_id)

    def review(self, brief_id: str, accept: bool, by: str, note: str = "") -> DecisionRecord:
        """The owner's decision on an open review. Accepting stores or merges the digest."""
        brief = next(
            (b for b in open_reviews(self.store.decisions()) if b.brief_id == brief_id), None
        )
        if brief is None:
            raise ReviewNotFound(brief_id)
        trace_id = uuid4().hex
        outcome = self._apply(brief) if accept else Outcome.DISCARDED
        reason = (note or ("accepted by owner" if accept else "rejected by owner"))[:300]
        return self._record(brief, outcome, by, reason, trace_id, AuditAction.APPROVAL)

    def _apply(self, brief: DecisionBrief) -> Outcome:
        digest = brief.digest
        now = self.clock()
        existing = None
        if brief.proposal is Proposal.MERGE and brief.nearest_entry_id:
            existing = next(
                (
                    e
                    for e in self.store.entries(brief.category)
                    if e.entry_id == brief.nearest_entry_id
                ),
                None,
            )
        if existing is None:
            self.store.put_entry(
                ContextEntry(
                    entry_id=f"ctx:{brief.category}:{digest.content_hash[:16]}",
                    domain=digest.domain,
                    category=brief.category,
                    classification=digest.classification,
                    taint=digest.taint,
                    title=digest.title,
                    summary=digest.summary,
                    facts=digest.facts,
                    tags=digest.tags,
                    source_refs=digest.source_refs,
                    digest_ids=(digest.digest_id,),
                    signal=digest.signal,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    subject=digest.subject,
                )
            )
            return Outcome.STORED
        if digest.subject is not None:
            self.store.put_entry(_replaced(existing, digest, now))
            return Outcome.MERGED
        stronger = digest.signal > existing.signal
        self.store.put_entry(
            existing.model_copy(
                update={
                    "classification": max(
                        existing.classification, digest.classification, key=lambda c: c.rank
                    ),
                    "taint": _taint((existing.taint, digest.taint)),
                    "title": digest.title if stronger else existing.title,
                    "summary": digest.summary if stronger else existing.summary,
                    "facts": unique([*existing.facts, *digest.facts], MAX_FACTS),
                    "tags": unique([*existing.tags, *digest.tags], MAX_TAGS),
                    "source_refs": unique([*existing.source_refs, *digest.source_refs], 50),
                    "digest_ids": (*existing.digest_ids, digest.digest_id),
                    "signal": max(existing.signal, digest.signal),
                    "version": existing.version + 1,
                    "updated_at": now,
                }
            )
        )
        return Outcome.MERGED

    def _record(
        self,
        brief: DecisionBrief,
        outcome: Outcome,
        by: str,
        reason: str,
        trace_id: str,
        action: AuditAction = AuditAction.GATE_DECISION,
    ) -> DecisionRecord:
        entry_id = None
        if outcome in (Outcome.STORED, Outcome.MERGED):
            entry_id = (
                brief.nearest_entry_id
                if outcome is Outcome.MERGED
                else f"ctx:{brief.category}:{brief.digest.content_hash[:16]}"
            )
        record = DecisionRecord(
            brief=brief,
            outcome=outcome,
            decided_at=self.clock(),
            decided_by=by,
            entry_id=entry_id,
            reason=reason[:300],
        )
        self.store.add_decision(record)
        decision = {
            Outcome.STORED: AuditDecision.ALLOW,
            Outcome.MERGED: AuditDecision.ALLOW,
            Outcome.DISCARDED: AuditDecision.DENY,
            Outcome.REVIEW: AuditDecision.ESCALATE,
        }[outcome]
        self._audit(
            "decide",
            decision,
            trace_id,
            brief.brief_id,
            brief.digest.content_hash,
            f"{outcome.value}:{brief.category}",
            action=action,
            actor=by,
        )
        return record

    # --- all stages --------------------------------------------------------------------------

    def run(self, items: Iterable[CollectedItem]) -> StageCounts:
        trace_id = uuid4().hex
        collected, rejected = self.collect(items, trace_id)
        digests = self.consolidate(trace_id)
        outcomes: list[Outcome] = []
        # Sequential, so a later digest is compared with what an earlier one just stored.
        for digest in digests:
            outcomes.append(self.decide(self.prepare(digest, trace_id), trace_id).outcome)
        return StageCounts(
            collected=collected,
            rejected=rejected,
            digests=len(digests),
            duplicates_dropped=sum(d.duplicates_dropped for d in digests),
            briefs=len(digests),
            stored=outcomes.count(Outcome.STORED),
            merged=outcomes.count(Outcome.MERGED),
            discarded=outcomes.count(Outcome.DISCARDED),
            review=outcomes.count(Outcome.REVIEW),
        )

    def _audit(
        self,
        stage: str,
        decision: AuditDecision,
        trace_id: str,
        ref: str,
        content_hash: str,
        policy: str | None,
        *,
        action: AuditAction = AuditAction.STATE_TRANSITION,
        actor: str | None = None,
    ) -> None:
        self.audit(
            AuditLogEntry(
                trace_id=trace_id,
                domain=self.config.domain,
                actor=actor or self.actor,
                action=action,
                target=f"curation.{stage}",
                decision=decision,
                policy_id=policy[:100] if policy else None,
                content_hash=content_hash,
                content_ref=ref,
            )
        )


def _replaced(existing: ContextEntry, digest: Digest, now: datetime) -> ContextEntry:
    """The next version of a snapshot entry. Its label never drops below the stored one."""
    return existing.model_copy(
        update={
            "classification": max(
                existing.classification, digest.classification, key=lambda c: c.rank
            ),
            "taint": _taint((existing.taint, digest.taint)),
            "title": digest.title,
            "summary": digest.summary,
            "facts": digest.facts,
            "tags": digest.tags,
            "source_refs": digest.source_refs,
            "digest_ids": (*existing.digest_ids, digest.digest_id)[-20:],
            "signal": digest.signal,
            "version": existing.version + 1,
            "updated_at": now,
        }
    )


def _text(title: str, summary: str, facts: tuple[str, ...]) -> str:
    return " ".join((title, summary, *facts))


def _taint(taints: Iterable[Taint]) -> Taint:
    return (
        Taint.EXTERNAL_CONTENT if any(t is Taint.EXTERNAL_CONTENT for t in taints) else Taint.NONE
    )


def _short(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
