"""原型 17：Agent 框架共同内核、使用方式与选型。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from framework_selection import FRAMEWORKS, ProjectBrief, common_kernel, select  # noqa: E402


def main() -> None:
    trace.section("原型 17：Agent 开发框架与选型", "第 1 章：共同内核、六种开发方式、硬约束到场景的四步筛选")
    trace.note("框架只是工具；真正需要掌握的是模型、工具、Loop、状态、记忆、权限、评测和可靠性的共同内核。")
    trace.kv("共同内核", common_kernel())

    trace.section("六种框架使用方式")
    methods = {}
    for framework in FRAMEWORKS:
        methods.setdefault(framework.method, []).append(framework.name)
    trace.summary_table([{"开发形状": key, "代表框架": "、".join(value)} for key, value in methods.items()], ["开发形状", "代表框架"])

    trace.section("四步选型演示")
    cases = [
        ProjectBrief("Python", "复杂状态、暂停恢复"),
        ProjectBrief("Python", "企业文档 RAG"),
        ProjectBrief("Java", "多 Agent、国产模型", model="国产模型"),
        ProjectBrief(".NET", "企业内部助手", cloud="Azure"),
        ProjectBrief("Python", "营销应用", low_code=True),
    ]
    for brief in cases:
        candidates = select(brief)
        trace.summary_table(
            [{"项目": brief.scenario, "团队": brief.team, "候选": framework.name, "理由": reason} for framework, reason in candidates],
            ["项目", "团队", "候选", "理由"],
        )
    trace.note("选型顺序：先团队/云/私有化等硬约束，再问项目最难的问题，最后看模型偏好和交付方式。")


if __name__ == "__main__":
    main()
