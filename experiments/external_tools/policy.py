"""从受控 Markdown 文件加载本地商务差旅政策。"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from .policy_models import PolicyRule, PolicySnapshot

_REQUIRED_METADATA = (
    "policy_id",
    "version",
    "kind",
    "applicability",
    "effective_from",
    "effective_to",
)
_TABLE_HEADER = ("rule_id", "topic", "value", "unit", "source_ref")


class PolicyDocumentError(ValueError):
    """政策文件缺失或不符合受控格式。"""


class PolicySnapshotLoader:
    """只解析当前 MVP 约定的元数据和规则表。"""

    def load(self, path: Path) -> PolicySnapshot:
        """加载一个本地 Markdown 政策文件。"""
        if not isinstance(path, Path):
            raise TypeError("policy path must be a Path")
        if not path.is_file():
            raise PolicyDocumentError(f"policy document not found: {path}")
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise PolicyDocumentError(
                f"policy document cannot be read: {path}"
            ) from exc

        try:
            metadata, rows = _parse_document(text)
            return PolicySnapshot(
                policy_id=metadata["policy_id"],
                version=_parse_int(metadata["version"], "version"),
                kind=metadata["kind"],
                applicability=metadata["applicability"],
                effective_from=_parse_date(
                    metadata["effective_from"], "effective_from"
                ),
                effective_to=_parse_effective_to(metadata["effective_to"]),
                rules=tuple(_parse_rule(row) for row in rows),
                source_ref=path.as_posix(),
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        except PolicyDocumentError:
            raise
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise PolicyDocumentError(f"invalid policy document: {path}") from exc


def _parse_document(text: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    lines = text.splitlines()
    try:
        heading_index = next(
            index for index, line in enumerate(lines) if line.strip() == "## 规则"
        )
    except StopIteration as exc:
        raise PolicyDocumentError("policy rules are required") from exc

    metadata: dict[str, str] = {}
    for line in lines[:heading_index]:
        stripped = line.strip()
        if not stripped:
            continue
        key, separator, value = stripped.partition(":")
        if not separator or not key.strip() or not value.strip():
            raise PolicyDocumentError("invalid policy metadata")
        metadata[key.strip()] = value.strip()
    missing = tuple(key for key in _REQUIRED_METADATA if not metadata.get(key))
    if missing:
        raise PolicyDocumentError(f"policy metadata missing: {', '.join(missing)}")

    table_lines = [line.strip() for line in lines[heading_index + 1 :] if line.strip()]
    if len(table_lines) < 3:
        raise PolicyDocumentError("policy rules are required")
    header = _split_table_row(table_lines[0])
    if header != _TABLE_HEADER or not _is_separator(table_lines[1]):
        raise PolicyDocumentError("invalid policy rules table")
    rows = [_row_from_values(_split_table_row(line)) for line in table_lines[2:]]
    if not rows:
        raise PolicyDocumentError("policy rules are required")
    return metadata, rows


def _split_table_row(line: str) -> tuple[str, ...]:
    if not line.startswith("|") or not line.endswith("|"):
        raise PolicyDocumentError("invalid policy table row")
    return tuple(value.strip() for value in line[1:-1].split("|"))


def _is_separator(line: str) -> bool:
    values = _split_table_row(line)
    return len(values) == len(_TABLE_HEADER) and all(
        re.fullmatch(r":?-+:?", value) for value in values
    )


def _row_from_values(values: tuple[str, ...]) -> dict[str, str]:
    if len(values) != len(_TABLE_HEADER) or any(not value for value in values):
        raise PolicyDocumentError("invalid policy rule row")
    return dict(zip(_TABLE_HEADER, values, strict=True))


def _parse_rule(row: dict[str, str]) -> PolicyRule:
    return PolicyRule(
        rule_id=row["rule_id"],
        topic=row["topic"],
        value=_parse_value(row["value"]),
        unit=row["unit"],
        source_ref=row["source_ref"],
    )


def _parse_value(value: str) -> bool | int | str:
    lowered = value.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _parse_int(value: str, field_name: str) -> int:
    if not re.fullmatch(r"\d+", value):
        raise PolicyDocumentError(f"policy {field_name} must be an integer")
    return int(value)


def _parse_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PolicyDocumentError(f"policy {field_name} must be an ISO date") from exc


def _parse_effective_to(value: str) -> date | None:
    if value.casefold() == "open":
        return None
    return _parse_date(value, "effective_to")
