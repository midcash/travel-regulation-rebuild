"""信用卡欺诈检测 Agent：规则分流 + 有界 ReAct + 并行 Worker + 确定性决策。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Transaction:
    id: str
    user_id: str
    amount: float
    country: str
    home_country: str
    merchant_risk: float
    device_new: bool
    velocity_1h: int
    network_flagged: bool = False
    whitelisted: bool = False
    blacklisted: bool = False


@dataclass(frozen=True)
class Evidence:
    dimension: str
    risk: float
    confidence: float
    summary: str


@dataclass(frozen=True)
class Decision:
    transaction_id: str
    route: str
    risk_score: float
    critic_score: float
    action: str
    evidence: tuple[Evidence, ...] = ()
    rationale: str = ""


Worker = Callable[[Transaction], Evidence]


def geo_speed(tx: Transaction) -> Evidence:
    risk = 0.88 if tx.country != tx.home_country else 0.08
    return Evidence("地理/速度", risk, 0.86, "交易国家与常用国家不一致" if risk > 0.5 else "地理位置符合画像")


def behavior_shift(tx: Transaction) -> Evidence:
    risk = min(1.0, 0.12 * tx.velocity_1h + (0.35 if tx.device_new else 0.0))
    return Evidence("行为偏移", risk, 0.78, f"一小时内 {tx.velocity_1h} 笔，设备新={tx.device_new}")


def merchant(tx: Transaction) -> Evidence:
    return Evidence("商户风险", tx.merchant_risk, 0.92, f"商户风险系数={tx.merchant_risk:.2f}")


def device_network(tx: Transaction) -> Evidence:
    risk = 0.9 if tx.network_flagged else (0.65 if tx.device_new else 0.1)
    return Evidence("设备与关联网络", risk, 0.73, "网络命中黑名单" if tx.network_flagged else "设备新但网络未命中")


class Critic:
    """独立复核：只看综合证据，不替代融合层做拍脑袋决策。"""

    def review(self, score: float, evidence: tuple[Evidence, ...]) -> tuple[float, str]:
        high = sum(item.risk >= 0.7 for item in evidence)
        low_confidence = sum(item.confidence < 0.8 for item in evidence)
        if score >= 0.75 and high <= 1:
            return min(score, 0.62), "高分主要来自单一信号，降低误报风险"
        if score >= 0.5 and low_confidence >= 2:
            return min(score, 0.58), "证据置信度偏低，建议加验而非直接拒绝"
        return score, "证据链相互支持"


class FeedbackStore:
    """异步真实标签回流：更新画像、模式库和阈值，而不阻塞实时判定。"""

    def __init__(self) -> None:
        self.profile: dict[str, int] = {}
        self.patterns: list[str] = []
        self.reject_threshold = 0.80

    def apply_label(self, tx: Transaction, label: str, decision: Decision) -> str:
        self.profile[tx.user_id] = self.profile.get(tx.user_id, 0) + (1 if label == "fraud" else 0)
        if label == "fraud" and decision.action == "放行":
            self.reject_threshold = max(0.65, self.reject_threshold - 0.05)
            self.patterns.append(f"{tx.country}/{tx.merchant_risk:.1f}/new_device")
            return "漏报：强化账户画像、沉淀模式、收紧拦截线"
        if label == "legit" and decision.action == "拒绝":
            self.reject_threshold = min(0.90, self.reject_threshold + 0.02)
            return "误报：提高拦截线，保留加验缓冲"
        return "标签与动作一致：更新统计，不调整阈值"


class FraudOrchestrator:
    def __init__(self, feedback: FeedbackStore | None = None) -> None:
        self.workers: dict[str, Worker] = {
            "地理/速度": geo_speed,
            "行为偏移": behavior_shift,
            "商户风险": merchant,
            "设备与关联网络": device_network,
        }
        self.critic = Critic()
        self.feedback = feedback or FeedbackStore()

    def _fuse(self, evidence: tuple[Evidence, ...]) -> float:
        weights = {"地理/速度": 0.30, "行为偏移": 0.25, "商户风险": 0.20, "设备与关联网络": 0.25}
        numerator = sum(item.risk * item.confidence * weights[item.dimension] for item in evidence)
        denominator = sum(item.confidence * weights[item.dimension] for item in evidence)
        return numerator / denominator if denominator else 0.0

    def classify(self, tx: Transaction) -> Decision:
        if tx.whitelisted:
            return Decision(tx.id, "秒级放行", 0.0, 0.0, "放行", rationale="白名单在 LLM 之前结束")
        if tx.blacklisted:
            return Decision(tx.id, "秒级拦截", 1.0, 1.0, "拒绝", rationale="黑名单硬规则在 LLM 之前结束")

        # 四个维度相互独立，真实系统可并行；这里保留线程池让数据流可见。
        with ThreadPoolExecutor(max_workers=len(self.workers)) as pool:
            futures = [pool.submit(worker, tx) for worker in self.workers.values()]
            evidence = tuple(sorted((future.result() for future in futures), key=lambda item: item.dimension))
        score = self._fuse(evidence)  # 公式层做融合，不让 LLM 直接拍最终分数
        critic_score, critic_note = self.critic.review(score, evidence)
        disagreement = abs(score - critic_score) >= 0.12
        if tx.amount >= 5000 or disagreement:
            action = "转人工"
            rationale = "高额或 Critic 与融合层分歧，进入人工兜底"
        elif critic_score >= self.feedback.reject_threshold:
            action = "拒绝"
            rationale = "高风险且证据链充分"
        elif critic_score >= 0.50:
            action = "加验证"
            rationale = "灰区交易用 OTP/3DS 等 step-up 缓冲"
        else:
            action = "放行"
            rationale = "风险低于业务阈值"
        return Decision(tx.id, "灰区 Agent", score, critic_score, action, evidence, f"{rationale}；{critic_note}")
