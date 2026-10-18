"""本地商务差旅政策的类型化运行时模型。"""

from __future__ import annotations

from datetime import date
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .values import StableId

PolicyValue: TypeAlias = bool | int | str


class PolicyRule(BaseModel):
    """一条可追溯的政策规则。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    rule_id: StableId
    topic: str = Field(min_length=1, max_length=128)
    value: PolicyValue
    unit: str = Field(min_length=1, max_length=32)
    source_ref: str = Field(min_length=1, max_length=512)


class PolicySnapshot(BaseModel):
    """一次完整的本地政策读取结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    policy_id: StableId
    version: int = Field(ge=1)
    kind: str = Field(min_length=1, max_length=64)
    applicability: str = Field(min_length=1, max_length=64)
    effective_from: date
    effective_to: date | None = None
    rules: tuple[PolicyRule, ...] = Field(min_length=1)
    source_ref: str = Field(min_length=1, max_length=512)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("rules")
    @classmethod
    def validate_unique_rules(
        cls, values: tuple[PolicyRule, ...]
    ) -> tuple[PolicyRule, ...]:
        """规则编号必须唯一，避免同一快照产生歧义。"""
        rule_ids = tuple(rule.rule_id for rule in values)
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("policy rule IDs must be unique")
        return values

    @model_validator(mode="after")
    def validate_effective_range(self) -> PolicySnapshot:
        """有效期结束日期不得早于开始日期。"""
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError(
                "policy effective_to must not be earlier than effective_from"
            )
        return self

    def rule(self, rule_id: str) -> PolicyRule:
        """按稳定编号读取规则，不存在时显式失败。"""
        for rule in self.rules:
            if rule.rule_id == rule_id:
                return rule
        raise KeyError(f"unknown policy rule: {rule_id}")
