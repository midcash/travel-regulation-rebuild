"""M1 阶段的领域值对象和稳定 ID 类型。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

StableId: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
        strip_whitespace=True,
    ),
]
RequestId: TypeAlias = StableId
TripId: TypeAlias = StableId
SessionId: TypeAlias = StableId
TraceId: TypeAlias = StableId
ConstraintId: TypeAlias = StableId
EvidenceId: TypeAlias = StableId
CandidateId: TypeAlias = StableId
PlanId: TypeAlias = StableId
PlanItemId: TypeAlias = StableId
IssueId: TypeAlias = StableId
CheckpointId: TypeAlias = StableId


class Money(BaseModel):
    """使用 Decimal 表示的非负金额。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    amount: Decimal = Field(ge=Decimal("0"))
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("amount", mode="before")
    @classmethod
    def reject_float_amount(cls, value: object) -> object:
        """拒绝 float，避免金额在进入领域层前发生二进制精度损失。"""
        if isinstance(value, float):
            raise ValueError("amount must be Decimal, int, or decimal string")
        return value

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        """将 ISO 4217 货币代码规范化为大写。"""
        normalized = value.upper()
        if not normalized.isalpha():
            raise ValueError("currency must contain only letters")
        return normalized


class DateRange(BaseModel):
    """包含起止日期的闭区间。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date
    end: date

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        """校验结束日期不得早于开始日期。"""
        if self.end < self.start:
            raise ValueError("end must not be earlier than start")
        return self

    @property
    def days(self) -> int:
        """返回包含首尾日期的天数。"""
        return (self.end - self.start).days + 1


class GeoPoint(BaseModel):
    """高德 GCJ-02 坐标；不自动转换 GPS/WGS84 坐标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    crs: Literal["GCJ-02"] = "GCJ-02"
