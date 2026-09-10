"""Extract a decision-sized policy snapshot from the copied regulation Markdown."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


class RegulationPolicyError(ValueError):
    """The source regulation is missing or cannot be parsed safely."""


ARTICLE_PATTERN = re.compile(
    r"^\*\*(第[^*]+条)\*\*\s*(.*?)(?=^\*\*第[^*]+条\*\*|\Z)",
    re.MULTILINE | re.DOTALL,
)
DATE_PATTERN = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")

KEY_ARTICLES = (
    ("transport_grade", "城市间交通工具等级", "第七条"),
    ("transport_choice", "公共交通工具选择原则", "第八条"),
    ("flight_purchase", "公务机票购买和附加费", "第九条"),
    ("lodging_limit", "住宿费限额", "第十二条"),
    ("meal_subsidy", "伙食补助费", "第十五条"),
    ("misc_subsidy", "公杂费", "第十七条"),
    ("reimbursement_scope", "报销标准和住宿天数", "第二十条"),
    ("receipt_condition", "住宿发票与补助", "第二十一条"),
    ("same_day_trip", "当天往返", "第二十四条"),
    ("required_documents", "报销凭证和办理期限", "第二十五条"),
    ("approval_required", "审批与超标准支出", "第二十六条"),
    ("long_train_supplement", "连续乘火车超过十二小时", "第三十条"),
    ("subsidy_exclusions", "不得享受伙食和公杂补助的情形", "第三十六条"),
    ("maximum_caps", "各项标准为最高限额", "第四十二条"),
)


def load_regulation_policy(path: Path) -> dict[str, Any]:
    """Read the requested raw regulation and return a compact, traceable snapshot."""
    if not isinstance(path, Path) or not path.is_file():
        raise RegulationPolicyError(f"policy document not found: {path}")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise RegulationPolicyError(f"policy document cannot be read: {path}") from exc

    title = _first_heading(text)
    articles = _parse_articles(text)
    if not title or not articles:
        raise RegulationPolicyError("policy document has no title or numbered articles")
    article_map = {item["article"]: item for item in articles}
    issued_on = _date_from(_metadata_value(text, "发文日期"))
    effective_from = _date_from(_effective_text(text))
    source_ref = path.resolve().as_posix()
    key_rules = []
    for rule_id, topic, article in KEY_ARTICLES:
        item = article_map.get(article)
        if item is None:
            continue
        key_rules.append(
            {
                "rule_id": rule_id,
                "topic": topic,
                "article": article,
                "value": item["text"],
                "unit": "原文条款",
                "source_ref": f"{source_ref}#{article}",
            }
        )
    applicability = article_map.get("第二条", {}).get("text")
    return {
        "policy_id": "zhejiang-government-travel-regulation-2017",
        "title": title,
        "document_no": _metadata_value(text, "文件"),
        "issuer": _metadata_value(text, "发文单位"),
        "issued_on": issued_on,
        "effective_from": effective_from,
        "effective_to": None,
        "applicability": applicability,
        "source_ref": source_ref,
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "article_count": len(articles),
        "rules": key_rules,
        "article_index": [
            {"article": item["article"], "source_ref": f"{source_ref}#{item['article']}"}
            for item in articles
        ],
    }


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None


def _metadata_value(text: str, label: str) -> str | None:
    match = re.search(rf"^>\s*{re.escape(label)}：?\s*(.+?)\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _effective_text(text: str) -> str | None:
    match = re.search(r"本规定自\s*([0-9]{4}\s*年\s*[0-9]{1,2}\s*月\s*[0-9]{1,2}\s*日)起施行", text)
    return match.group(1) if match else None


def _date_from(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.replace("‐", "-").replace("‑", "-").replace("‒", "-").replace("–", "-").replace("—", "-").replace("−", "-")
    iso = re.search(r"\d{4}-\d{1,2}-\d{1,2}", normalized)
    if iso:
        year, month, day = (int(part) for part in iso.group().split("-"))
        return f"{year:04d}-{month:02d}-{day:02d}"
    match = DATE_PATTERN.search(normalized)
    if not match:
        return value
    year, month, day = (int(part) for part in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def _parse_articles(text: str) -> list[dict[str, str]]:
    articles = []
    for match in ARTICLE_PATTERN.finditer(text):
        article = match.group(1).strip()
        body = re.sub(r"(?m)^#{2,}\s+.*(?:\n|$)", "", match.group(2))
        body = re.sub(r"\n{3,}", "\n\n", body.strip())
        if body:
            articles.append({"article": article, "text": body})
    return articles
