"""Hermes-inspired governed memory/skill improvement loop."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from common.errors import AgentError, ErrorKind
from common.storage import JsonlStore


@dataclass(frozen=True)
class SkillProposal:
    skill_id: str
    title: str
    instructions: str
    evidence: list[str]
    confidence: float
    status: str = "proposed"


class GovernedLearningStore:
    def __init__(self, path: str | Path = "workspace/learning-events.jsonl"):
        self.events = JsonlStore(path)
        self.skills: dict[str, SkillProposal] = {}
        self._replay()

    def _replay(self) -> None:
        for event in self.events.read():
            if event.get("type") == "skill.proposed":
                raw = event.get("skill", {})
                proposal = SkillProposal(
                    str(raw.get("skill_id", "")),
                    str(raw.get("title", "")),
                    str(raw.get("instructions", "")),
                    [str(item) for item in raw.get("evidence", [])],
                    float(raw.get("confidence", 0)),
                    str(raw.get("status", "proposed")),
                )
                if proposal.skill_id:
                    self.skills[proposal.skill_id] = proposal
            elif event.get("type") == "skill.approved" and event.get("skill_id") in self.skills:
                current = self.skills[event["skill_id"]]
                self.skills[event["skill_id"]] = SkillProposal(current.skill_id, current.title, current.instructions, current.evidence, current.confidence, "approved")

    def propose(self, proposal: SkillProposal) -> None:
        if not proposal.skill_id.strip() or not proposal.instructions.strip() or not proposal.evidence:
            raise AgentError("invalid_proposal", "技能提案必须包含 id、指令和证据", ErrorKind.VALIDATION)
        if not 0 <= proposal.confidence <= 1:
            raise AgentError("invalid_confidence", "confidence 必须在 [0,1]", ErrorKind.VALIDATION)
        self.skills[proposal.skill_id] = proposal
        self.events.append({"type": "skill.proposed", "ts": time.time(), "skill": vars(proposal)})

    def approve(self, skill_id: str, *, reviewer: str) -> SkillProposal:
        proposal = self.skills.get(skill_id)
        if proposal is None:
            raise AgentError("skill_not_found", "技能提案不存在", ErrorKind.NOT_FOUND)
        if not reviewer.strip():
            raise AgentError("reviewer_required", "技能发布必须有 reviewer", ErrorKind.AUTHORIZATION)
        approved = SkillProposal(proposal.skill_id, proposal.title, proposal.instructions, proposal.evidence, proposal.confidence, "approved")
        self.skills[skill_id] = approved
        self.events.append({"type": "skill.approved", "ts": time.time(), "skill_id": skill_id, "reviewer": reviewer})
        return approved


class ProductionReflectionLoop:
    def __init__(self, store: GovernedLearningStore, reviewer: Callable[[SkillProposal], str | None] | None = None):
        self.store = store
        # Learning may propose automatically; publishing must have an
        # explicitly injected reviewer.  There is no self-approval default.
        self.reviewer = reviewer

    def learn(self, task: str, outcome: str, *, skill_id: str) -> SkillProposal | None:
        if not task.strip() or not outcome.strip():
            return None
        proposal = SkillProposal(skill_id, f"由 {task[:24]} 提炼", f"处理相似任务时参考：{outcome[:500]}", [task[:500], outcome[:500]], 0.8)
        self.store.propose(proposal)
        reviewer_name = self.reviewer(proposal) if self.reviewer is not None else None
        if reviewer_name:
            return self.store.approve(skill_id, reviewer=reviewer_name)
        return proposal
