"""What happens with an accepted artifact (docs/architecture.md, section 6.2, step 5).

Content never writes into another domain. It places typed proposals in its outbox; each one is
checked by the boundary policy before it is queued, and the target domain decides later.
Skill candidates become an ActionRequest that the action gate holds for human approval,
because the material is tainted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jarvis_core.contracts.actions import ActionRequest, ApprovalTier
from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import BoundaryEvent, LifeTipProposed, TradingIdeaProposed
from jarvis_core.contracts.task_token import TaskTokenClaims
from jarvis_core.gate.action_gate import GateDecision, GateOutcome, decide
from jarvis_core.policy.flows import check_flow
from mod_content.reel.schemas import ReelKnowledgeArtifact, TargetModule

MIN_UTILITY_FOR_PROPOSAL = 0.4
MIN_UTILITY_FOR_SKILL = 0.8
PROPOSE_SKILL_TOOL = "propose_skill_candidate"


@dataclass(frozen=True)
class FollowUpPlan:
    proposals: tuple[BoundaryEvent, ...] = ()
    template_candidate: bool = False
    actions: tuple[tuple[ActionRequest, GateDecision], ...] = ()
    denied: tuple[str, ...] = ()
    held_for_review: bool = False
    notes: tuple[str, ...] = field(default=())

    @property
    def awaiting_approval(self) -> bool:
        return self.held_for_review or any(
            d.outcome in (GateOutcome.NEEDS_APPROVAL, GateOutcome.NEEDS_STEP_UP)
            for _, d in self.actions
        )


def plan_follow_ups(artifact: ReelKnowledgeArtifact, claims: TaskTokenClaims) -> FollowUpPlan:
    if artifact.needs_human_review:
        return FollowUpPlan(held_for_review=True, notes=("injection flags present",))
    if artifact.utility_score < MIN_UTILITY_FOR_PROPOSAL:
        return FollowUpPlan(notes=("utility below proposal threshold; stored only",))

    target = artifact.target_module
    if target is TargetModule.CONTENT:
        return FollowUpPlan(template_candidate=True)
    if target in (TargetModule.TRADING, TargetModule.LIFE_OS):
        event = _proposal(artifact, target)
        flow = check_flow(event, caller_domain=Domain.CONTENT)
        if not flow.allowed:
            return FollowUpPlan(denied=(flow.reason,))
        return FollowUpPlan(proposals=(event,))
    if target is TargetModule.SELF_EVOLUTION and artifact.utility_score >= MIN_UTILITY_FOR_SKILL:
        request = ActionRequest(
            task_id=artifact.task_id,
            domain=Domain.CONTENT,
            tool=PROPOSE_SKILL_TOOL,
            parameters={"reel_id": artifact.reel_id, "source_ref": artifact.source_ref},
            tier=ApprovalTier.B,
            taint=artifact.taint,
            rationale=f"skill candidate from reel {artifact.reel_id}",
        )
        return FollowUpPlan(actions=((request, decide(request, claims)),))
    return FollowUpPlan(notes=("no follow-up for this target",))


def _proposal(artifact: ReelKnowledgeArtifact, target: TargetModule) -> BoundaryEvent:
    if target is TargetModule.TRADING:
        return TradingIdeaProposed(
            origin_domain=Domain.CONTENT,
            target_domain=Domain.TRADING,
            classification=artifact.classification,
            taint=artifact.taint,
            summary=artifact.summary,
            source_ref=artifact.source_ref,
        )
    return LifeTipProposed(
        origin_domain=Domain.CONTENT,
        target_domain=Domain.PRIVATE,
        classification=artifact.classification,
        taint=artifact.taint,
        summary=artifact.summary,
        source_ref=artifact.source_ref,
    )
