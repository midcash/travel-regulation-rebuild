"""模型服务故障的处理。对应笔记 17.3 节。

工具故障和模型故障的总体思路一样（受控恢复 -> 切换备用 -> 降级），
但模型故障多了一个**工具故障没有的判断**：

    模型接口返回成功，不代表模型在业务上正常。

所以这一节要同时看两个层面（笔记 17.3.5）：
  1. 技术可用性：错误率、延迟、限流       -> 决定要不要重试/切换
  2. 业务有效性：任务成功率、参数正确率、规则违反率、重复运行一致性
                                          -> 决定要不要停用该模型的高风险权限

还有一条最容易被忽略的（17.3.4）：
  **切换备用模型不等于保持原有权限。** 备用能力下降时，Agent 的自动化权限必须跟着下降。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from common.llm import LLM, LLMResponse, Message, ToolSchema
from resilience import Clock, VirtualClock


class Permission(str, Enum):
    """Agent 的操作权限。从高到低，降级就是往右走。"""

    AUTO_WRITE = "自动执行写操作"
    READ_ONLY = "只读查询"
    DRAFT_ONLY = "只生成草稿，人工确认后执行"
    HUMAN = "完全转人工"


PERMISSION_ORDER = [Permission.AUTO_WRITE, Permission.READ_ONLY, Permission.DRAFT_ONLY, Permission.HUMAN]

# 权限不是一条简单的全序：只读权限低于草稿权限（少了 draft），
# 但它俩都能读。所以用"能力集合"来判断某个动作能不能做。
PERMISSION_CAPS: dict[Permission, set[str]] = {
    Permission.AUTO_WRITE: {"read", "draft", "write"},
    Permission.READ_ONLY: {"read"},
    Permission.DRAFT_ONLY: {"read", "draft"},
    Permission.HUMAN: set(),
}


def downgrade(level: Permission, steps: int = 1) -> Permission:
    """沿严格程度阶梯往下走：自动写 -> 只读 -> 只出草稿 -> 转人工。"""
    index = PERMISSION_ORDER.index(level)
    return PERMISSION_ORDER[min(len(PERMISSION_ORDER) - 1, index + steps)]


@dataclass
class ModelSpec:
    """一个候选模型。备用模型必须是"上线前用真实业务任务验证过"的（笔记 17.3.3）。"""

    name: str
    tool_calling: bool = True
    complex_reasoning: bool = True
    rule_following: bool = True
    quality: float = 0.95  # 真实任务上的单次成功率，用来演示 pass^k
    verified_on: str = ""


@dataclass
class QualityMonitor:
    """笔记 17.3.2：不能只看可用率和响应时间。

    这里只放四个最能说明问题的业务指标。
    """

    task_success: list[bool] = field(default_factory=list)
    param_correct: list[bool] = field(default_factory=list)
    rule_violations: int = 0
    total: int = 0
    seen_outputs: dict[str, set[str]] = field(default_factory=dict)

    def observe(
        self,
        task_id: str,
        *,
        success: bool,
        params_ok: bool,
        violated_rule: bool = False,
        output: str = "",
    ) -> None:
        self.total += 1
        self.task_success.append(success)
        self.param_correct.append(params_ok)
        if violated_rule:
            self.rule_violations += 1
        if output:
            self.seen_outputs.setdefault(task_id, set()).add(output)

    @property
    def success_rate(self) -> float:
        return sum(self.task_success) / len(self.task_success) if self.task_success else 1.0

    @property
    def param_correct_rate(self) -> float:
        return sum(self.param_correct) / len(self.param_correct) if self.param_correct else 1.0

    @property
    def rule_violation_rate(self) -> float:
        return self.rule_violations / self.total if self.total else 0.0

    @property
    def consistency(self) -> float:
        """重复运行一致性：同一个任务多次跑，输出是否稳定。"""
        if not self.seen_outputs:
            return 1.0
        stable = sum(1 for outputs in self.seen_outputs.values() if len(outputs) == 1)
        return stable / len(self.seen_outputs)

    def verdict(self, *, baseline_success: float = 0.90, max_violation: float = 0.05) -> tuple[bool, str]:
        """接口可用但业务上是不是已经退化了。"""
        reasons: list[str] = []
        if self.success_rate < baseline_success:
            reasons.append(f"任务成功率 {self.success_rate:.0%} 低于基线 {baseline_success:.0%}")
        if self.rule_violation_rate > max_violation:
            reasons.append(f"规则违反率 {self.rule_violation_rate:.0%} 超过阈值 {max_violation:.0%}")
        if self.consistency < 0.8:
            reasons.append(f"重复运行一致性只有 {self.consistency:.0%}")
        if reasons:
            return True, "；".join(reasons)
        return False, "各项业务指标正常"

    def snapshot(self) -> dict[str, Any]:
        return {
            "任务成功率": f"{self.success_rate:.0%}",
            "参数正确率": f"{self.param_correct_rate:.0%}",
            "规则违反率": f"{self.rule_violation_rate:.0%}",
            "重复一致性": f"{self.consistency:.0%}",
            "样本数": self.total,
        }


def pass_k(single_run_success: float, k: int) -> float:
    """连续 k 次都成功的概率 = p^k。

    笔记 17.3.2 借 t-bench 的实验说明的就是这件事：
    单次成功率看着不错，但"同一任务连续多次都成功"的概率会快速下降。
    Agent 的可靠性不能只看单次平均成功率。
    """
    return single_run_success ** k


@dataclass
class GatewayResult:
    ok: bool
    action: str
    detail: str
    used_model: str = ""
    permission: Permission = Permission.AUTO_WRITE
    response: LLMResponse | None = None
    trace: list[str] = field(default_factory=list)

    def say(self, line: str) -> None:
        self.trace.append(line)


@dataclass
class ModelBudget:
    """笔记 17.3.1：模型调用一次同时消耗时间、Token 和费用，所以不能无限重试。"""

    max_calls: int = 3
    max_input_tokens: int = 20000
    max_output_tokens: int = 4000
    max_seconds: float = 30.0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    spent_seconds: float = 0.0

    def can_retry(self) -> bool:
        return (
            self.calls < self.max_calls
            and self.input_tokens < self.max_input_tokens
            and self.output_tokens < self.max_output_tokens
            and self.spent_seconds < self.max_seconds
        )

    def spend(self, response: LLMResponse | None, seconds: float) -> None:
        self.calls += 1
        self.spent_seconds += seconds
        if response is not None:
            self.input_tokens += response.input_tokens
            self.output_tokens += response.output_tokens

    def why_stopped(self) -> str:
        if self.calls >= self.max_calls:
            return f"调用次数用尽（{self.calls}/{self.max_calls}）"
        if self.spent_seconds >= self.max_seconds:
            return f"时间预算用尽（{self.spent_seconds:.1f}s/{self.max_seconds}s）"
        return "Token 预算用尽"


class ModelGateway:
    """模型网关：负责重试、切换备用模型、以及切换后的权限调整。"""

    def __init__(
        self,
        primary: LLM,
        backups: list[LLM] | None = None,
        specs: dict[str, ModelSpec] | None = None,
        monitor: QualityMonitor | None = None,
        retry_delays: list[float] | None = None,
        clock: Clock | None = None,
    ):
        self.primary = primary
        self.backups = backups or []
        self.specs = specs or {}
        self.monitor = monitor or QualityMonitor()
        self.retry_delays = retry_delays or [0.5, 1.0, 2.0]
        self.clock = clock or VirtualClock()
        self._rng = random.Random(20260911)

    def call(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
        *,
        required_capability: str = "tool_calling",
        needs_write: bool = True,
        allow_backup: bool = True,
    ) -> GatewayResult:
        result = GatewayResult(ok=False, action="", detail="")
        budget = ModelBudget(max_calls=len(self.retry_delays) + 1)

        # ---- 第 1 步：主模型 + 受控重试 ----
        attempt = 0
        while budget.can_retry():
            attempt += 1
            started = self.clock.now()
            try:
                response = self.primary.chat(messages, tools)
            except Exception as exc:  # noqa: BLE001 - 瞬时故障都收敛到这里
                budget.spend(None, self.clock.now() - started)
                result.say(f"[主模型] 第 {attempt} 次调用失败：{type(exc).__name__}: {exc}")
                if attempt > len(self.retry_delays):
                    break
                delay = self.retry_delays[min(attempt - 1, len(self.retry_delays) - 1)]
                delay *= 1 + self._rng.uniform(-0.3, 0.3)  # 随机抖动
                result.say(f"[主模型] 退避 {delay:.2f}s 后重试")
                self.clock.sleep(delay)
                continue

            budget.spend(response, self.clock.now() - started)
            result.say(f"[主模型] 第 {attempt} 次调用成功（接口层面）")
            result.ok = True
            result.action = "primary_ok"
            result.detail = "主模型返回成功"
            result.used_model = self.primary.name
            result.response = response
            result.permission = Permission.AUTO_WRITE
            return result

        result.say(f"[主模型] 停止重试：{budget.why_stopped()}")

        # ---- 第 2 步：切换预先验证过的备用模型（17.3.3）----
        if not allow_backup or not self.backups:
            result.action = "no_backup_available"
            result.detail = "主模型持续不可用，且没有备用模型"
            result.permission = Permission.HUMAN
            result.say("[网关] 没有备用模型 -> 保存状态并转人工")
            return result

        for index, backup in enumerate(self.backups, start=1):
            spec = self.specs.get(backup.name, ModelSpec(name=backup.name))
            result.say(f"[备用模型 {index}] 尝试 {backup.name}（验证记录：{spec.verified_on or '未记录'}）")

            if required_capability and not getattr(spec, required_capability, True):
                result.say(
                    f"[备用模型 {index}] 不具备 {required_capability} 能力，"
                    f"不能直接接管原任务"
                )
                continue

            try:
                response = backup.chat(messages, tools)
            except Exception as exc:  # noqa: BLE001
                result.say(f"[备用模型 {index}] 也失败了：{type(exc).__name__}: {exc}")
                continue

            # ---- 第 3 步：切换成功 != 保持原权限（17.3.4）----
            permission = Permission.AUTO_WRITE
            notes: list[str] = []
            if not spec.complex_reasoning and needs_write:
                permission = downgrade(permission, 1)
                notes.append("复杂推理能力不足")
            if not spec.rule_following and needs_write:
                permission = downgrade(permission, 1)
                notes.append("规则遵循能力不足")
            if not notes:
                notes.append("能力满足要求")

            result.ok = True
            result.action = "switched_to_backup"
            result.used_model = backup.name
            result.permission = permission
            result.response = response
            result.detail = f"已切换到备用模型 {backup.name}；" + "，".join(notes)
            result.say(f"[备用模型 {index}] 接管成功")
            result.say(f"[权限] {'，'.join(notes)} -> 权限调整为「{permission.value}」")
            if permission is not Permission.AUTO_WRITE:
                result.say("[权限] 笔记 17.3.4：启用备用模型不等于保持原有权限")
            return result

        result.action = "no_model_meets_standard"
        result.detail = "没有任何模型满足最低安全标准"
        result.permission = Permission.HUMAN
        result.say("[网关] 没有模型满足最低质量和安全标准 -> 保存任务状态，转人工")
        return result


def require_permission(permission: Permission, action: str, risk: str) -> tuple[bool, str]:
    """按当前权限判断某个动作能不能自动执行。risk 取 read / draft / write。"""
    caps = PERMISSION_CAPS[permission]
    if risk in caps:
        return True, f"权限「{permission.value}」覆盖 {risk}，允许{action}"
    return False, f"权限已降为「{permission.value}」（只有 {'/'.join(sorted(caps)) or '无'}），不允许自动{action}"
