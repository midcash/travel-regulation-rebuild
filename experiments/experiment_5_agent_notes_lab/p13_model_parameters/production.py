"""Validated, reproducible sampling configuration."""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class SamplingConfig:
    temperature: float = 0.2
    top_p: float = 0.9
    max_tokens: int = 512
    seed: int | None = 7

    def validate(self) -> "SamplingConfig":
        if self.temperature < 0 or not 0 < self.top_p <= 1 or self.max_tokens < 1:
            raise ValueError("temperature/top_p/max_tokens 参数非法")
        return self


class ProductionSampler:
    def __init__(self, config: SamplingConfig | None = None):
        self.config = (config or SamplingConfig()).validate()
        self._rng = random.Random(self.config.seed)

    def choose(self, candidates: list[tuple[str, float]]) -> str:
        if not candidates:
            raise ValueError("candidates cannot be empty")
        normalized = [(text, max(0.0, float(prob))) for text, prob in candidates]
        if self.config.temperature == 0:
            return max(normalized, key=lambda item: item[1])[0][: self.config.max_tokens]
        tempered = [(text, prob ** (1.0 / self.config.temperature)) for text, prob in normalized]
        tempered.sort(key=lambda item: item[1], reverse=True)
        cumulative = 0.0
        top_p_candidates: list[tuple[str, float]] = []
        total_probability = sum(prob for _, prob in tempered) or 1.0
        for text, prob in tempered:
            top_p_candidates.append((text, prob))
            cumulative += prob / total_probability
            if cumulative >= self.config.top_p:
                break
        total = sum(prob for _, prob in top_p_candidates)
        if total <= 0:
            raise ValueError("candidate probabilities must contain a positive value")
        pick = self._rng.random() * total
        for text, prob in top_p_candidates:
            pick -= prob
            if pick <= 0:
                return text[: self.config.max_tokens]
        return top_p_candidates[-1][0][: self.config.max_tokens]
