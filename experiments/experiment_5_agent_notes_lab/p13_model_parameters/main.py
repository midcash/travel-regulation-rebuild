"""原型 13：Temperature / Top-p / Penalty 参数对输出的影响。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from parameter_lab import GenerationConfig, TinyTokenModel, repetition_rate  # noqa: E402


def main() -> None:
    trace.section("原型 13：大模型参数设置", "第 7 章：temperature / top_p / max_tokens / penalties")
    model = TinyTokenModel()
    configs = [
        ("分析型低温", GenerationConfig(0.1, 1.0, 10, 0.1, 0.1)),
        ("普通对话", GenerationConfig(0.7, 0.90, 10, 0.1, 0.1)),
        ("创意写作", GenerationConfig(1.2, 0.95, 10, 0.0, 0.0)),
    ]
    rows = []
    for name, config in configs:
        tokens = model.generate(7, config)
        rows.append({
            "配置": name,
            "temperature": config.temperature,
            "top_p": config.top_p,
            "输出": " ".join(tokens),
            "重复率": f"{repetition_rate(tokens):.0%}",
        })
    trace.summary_table(rows, ["配置", "temperature", "top_p", "输出", "重复率"])
    trace.note("temperature 越低，概率分布越尖，输出更稳定；越高，候选 token 更均匀，变化更大。")
    trace.note("top_p 是先截取累计概率达到 p 的候选集合；它和 temperature 通常不要同时大幅调整。")

    trace.section("presence / frequency penalty")
    for label, config in [
        ("无惩罚", GenerationConfig(0.7, 1.0, 12)),
        ("轻惩罚", GenerationConfig(0.7, 1.0, 12, 0.4, 0.4)),
    ]:
        tokens = model.generate(11, config)
        trace.kv(label, f"{' '.join(tokens)} | 重复率={repetition_rate(tokens):.0%}")
    trace.note("分析型 Agent 的常见思路：temperature 0.1~0.3，top_p 保持默认，惩罚项只设很低，避免破坏领域词复用。")


if __name__ == "__main__":
    main()
