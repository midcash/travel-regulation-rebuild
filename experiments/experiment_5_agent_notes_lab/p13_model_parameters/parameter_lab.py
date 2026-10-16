"""大模型采样参数的无依赖、可复现原型。"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class GenerationConfig:
    temperature: float = 0.7
    top_p: float = 1.0
    max_tokens: int = 12
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0


class TinyTokenModel:
    """用一组固定 logits 模拟模型的下一 token 概率分布。"""

    TOKENS = ("稳定", "准确", "快速", "灵活", "可靠", "探索", "结论", "证据")
    LOGITS = (3.4, 3.1, 2.8, 2.3, 2.0, 1.6, 1.2, 0.8)

    @staticmethod
    def _softmax(logits: list[float], temperature: float) -> list[float]:
        temperature = max(0.01, temperature)
        scaled = [value / temperature for value in logits]
        pivot = max(scaled)
        exps = [math.exp(value - pivot) for value in scaled]
        total = sum(exps)
        return [value / total for value in exps]

    def distribution(self, previous: list[str], config: GenerationConfig) -> tuple[list[str], list[float]]:
        counts = {token: previous.count(token) for token in self.TOKENS}
        logits = [
            base
            - (config.presence_penalty if counts[token] else 0.0)
            - config.frequency_penalty * counts[token]
            for token, base in zip(self.TOKENS, self.LOGITS)
        ]
        probabilities = self._softmax(logits, config.temperature)
        pairs = sorted(zip(self.TOKENS, probabilities), key=lambda pair: pair[1], reverse=True)
        kept: list[tuple[str, float]] = []
        cumulative = 0.0
        for token, probability in pairs:
            kept.append((token, probability))
            cumulative += probability
            if cumulative >= min(1.0, max(0.01, config.top_p)):
                break
        normalizer = sum(probability for _, probability in kept)
        return [token for token, _ in kept], [probability / normalizer for _, probability in kept]

    def generate(self, seed: int, config: GenerationConfig) -> list[str]:
        rng = random.Random(seed)
        output: list[str] = []
        for _ in range(config.max_tokens):
            tokens, probabilities = self.distribution(output, config)
            output.append(rng.choices(tokens, weights=probabilities, k=1)[0])
        return output


def repetition_rate(tokens: list[str]) -> float:
    return 1 - len(set(tokens)) / len(tokens) if tokens else 0.0
