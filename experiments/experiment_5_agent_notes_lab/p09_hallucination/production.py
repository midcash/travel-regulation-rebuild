"""Grounded-answer boundary: evidence, citations and abstention."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Evidence:
    source_id: str
    text: str


@dataclass
class GroundedAnswer:
    answer: str
    grounded: bool
    citations: list[str]
    reason: str = ""


class ProductionGrounder:
    def __init__(self, *, min_overlap: float = 0.2):
        self.min_overlap = min_overlap

    def answer(self, draft: str, evidence: list[Evidence]) -> GroundedAnswer:
        if not draft.strip() or not evidence:
            return GroundedAnswer("目前没有足够依据回答该问题。", False, [], "missing_evidence")
        corpus = " ".join(item.text for item in evidence).lower()
        claims = [part.strip() for part in re.split(r"[。！？!?]", draft) if part.strip()]
        citations = [item.source_id for item in evidence if item.source_id]
        unsupported: list[str] = []
        for claim in claims:
            words = {word for word in re.findall(r"[\w\u4e00-\u9fff]+", claim.lower()) if len(word) > 1}
            overlap = len([word for word in words if word in corpus]) / max(1, len(words))
            if overlap < self.min_overlap:
                unsupported.append(claim)
        if unsupported:
            return GroundedAnswer("现有资料不足以支持完整结论，建议补充权威来源。", False, [], "unsupported_claim")
        suffix = " " + " ".join(f"[{item.source_id}]" for item in evidence if item.source_id)
        return GroundedAnswer(draft.strip() + suffix, True, citations, "grounded")
