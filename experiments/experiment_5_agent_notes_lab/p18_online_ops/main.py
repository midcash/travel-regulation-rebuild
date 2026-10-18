"""原型 18：Agent 上线后的监控、反馈、归因和修复。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from ops_monitor import Attribution, Monitor, make_window  # noqa: E402


def main() -> None:
    trace.section("原型 18：Agent 项目上线问题", "第 18 章：监控业务指标、收集反馈、归因与修复")
    baseline_traces = make_window("baseline", 9, 700)
    baseline = Monitor.measure(baseline_traces)
    monitor = Monitor(baseline)
    trace.kv("基线", baseline)

    windows = [
        ("上线第 1 天", baseline_traces),
        ("上线第 7 天（退化）", make_window("degraded", 6, 1500, "tool", safety=1)),
        ("回滚后", make_window("recovered", 9, 760)),
    ]
    for name, traces in windows:
        metrics = Monitor.measure(traces)
        alerts = monitor.check(metrics)
        trace.section(name)
        trace.summary_table(
            [{"通过率": f"{metrics.pass_rate:.1%}", "正反馈": f"{metrics.positive_feedback_rate:.1%}", "P95": f"{metrics.p95_latency}ms", "成本": f"${metrics.avg_cost:.4f}", "安全违规": metrics.safety_violations, "告警": len(alerts)}],
            ["通过率", "正反馈", "P95", "成本", "安全违规", "告警"],
        )
        for alert in alerts:
            trace.bullet(f"{alert.metric}: {alert.detail}")
        if alerts:
            owner, actions = Attribution().diagnose(traces, alerts)
            trace.kv("归因候选", owner)
            trace.kv("修复动作", actions)
        else:
            trace.note("相对基线没有触发发布/线上告警阈值。")

    trace.section("线上指标边界")
    trace.note("可用率/HTTP 200 只说明服务还活着；通过率、用户反馈、P95、成本和安全违规才说明业务还能不能用。")
    trace.note("每条 trace 都要保留模型版本、Prompt 版本、工具阶段、输入输出摘要和最终反馈，才能做归因而不是猜。")


if __name__ == "__main__":
    main()
