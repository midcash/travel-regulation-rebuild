"""原型 07：Agent 评测。对应笔记第 3 章（笔记里最长的一章）。

评测这件事的难点不在写断言，在于**评测什么**。这个原型把笔记 3.3 的四个层次
都落成了可运行的代码：

    组件评测      单步的工具选择、参数构造对不对（3.3.3）
    轨迹评测      调用的工具序列对不对，有没有多余步骤（3.3.4）
    结果评测      最终答案对不对
    安全与合规    注入防御、高风险确认、越权动作（3.5.3）

然后走一遍完整的发布流程：
    EDD 评测集 -> 四层评测 -> 发布闸门 -> 上线后监控

运行方式（PyCharm 直接右键 Run / Debug）：
    python experiments/experiment_5_agent_notes_lab/p07_evaluation/main.py

建议打断点的位置：
  - evaluation.py 的 `safety_violation()`：安全判定的**唯一实现**，三类评测共用
  - evaluation.py 的 `_trace_passed()`：一条轨迹算不算过的判据
  - evaluation.py 的 `ReleaseGate.evaluate()`：闸门是怎么卡人的
  - evaluation.py 的 `GateCheck.__post_init__()`：硬指标为什么不能被降级成"只告警"
  - evaluation.py 的 `EvalReport.pass_pow_k`：Agent 可靠性为什么要看连续 k 次
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import trace  # noqa: E402
from dataset import (  # noqa: E402
    TASKS,
    RunTrace,
    Task,
    make_baseline,
    make_candidate_fast,
    make_candidate_fixed,
    make_candidate_unsafe,
)
from evaluation import (  # noqa: E402
    ComponentEvaluator,
    EvalReport,
    Judge,
    MonitorWindow,
    OnlineMonitor,
    ReleaseGate,
    TrajectoryEvaluator,
    group_by_category,
    run_evaluation,
)

RUNS_PER_TASK = 5


def find_trace(
    report: EvalReport, task_id: str, predicate
) -> tuple[Task, RunTrace] | None:
    """在报告里找一条满足条件的轨迹。

    demo 需要的是「某个版本在某个样本上出的那种错」，
    而这种轨迹只在**特定配置**下才存在（基线版本根本不违规）。
    找不到就返回 None —— 让 demo 明说"这轮没出现"，而不是静默少打一行。
    """
    for result in report.results:
        if result.task.id != task_id:
            continue
        for run_trace in result.traces:
            if predicate(run_trace):
                return result.task, run_trace
    return None


# ==========================================================================
# 演示 1：EDD 评测集长什么样
# ==========================================================================


def demo_1_dataset() -> None:
    trace.section(
        "演示 1：评测集 —— 从 TDD 到 EDD",
        "笔记 3.1.1：Agent 输出是概率性的，所以评测集要同时定义「期望结果」和「期望轨迹」",
    )
    trace.kv("样本数", len(TASKS))
    trace.kv("每条跑几次", RUNS_PER_TASK)

    print()
    trace.summary_table(
        [
            {
                "id": task.id,
                "类别": task.category,
                "风险": task.risk,
                "需要确认": "是" if task.requires_confirmation else "",
                "期望工具": " -> ".join(task.expected_tools) or "（不调工具）",
                "期望答案含": "/".join(task.answer_keywords),
            }
            for task in TASKS
        ],
        ["id", "类别", "风险", "需要确认", "期望工具", "期望答案含"],
    )
    print()
    trace.note("注意三件传统测试里没有的东西：")
    trace.note("  1. **期望轨迹**：不只断言答案，还断言调了哪些工具、什么顺序。")
    trace.note("     Agent 可能瞎猫碰上死耗子 —— 工具调错、最后答案碰巧对。")
    trace.note("     只评测结果的话，这种问题永远发现不了。")
    trace.note("  2. **风险等级**：高风险样本要单独统计，不能混进平均值。")
    trace.note("  3. **注入攻击样本**（t08）：期望 Agent **拒绝执行**，而不是照做。")


# ==========================================================================
# 演示 2：四层评测
# ==========================================================================


def demo_2_four_layers(
    report: EvalReport, candidate_fast: EvalReport, candidate_unsafe: EvalReport
) -> None:
    trace.section(
        "演示 2：四层评测 —— 光看一个总分是定位不到问题的",
        "笔记 3.3.3 / 3.3.4：组件评测定位到「哪一步坏了」，系统评测看「整体行不行」",
    )

    print()
    trace.section("第一层：组件评测（单步能力）")
    for name, value in ComponentEvaluator.evaluate(report.results).items():
        trace.kv(name, f"{value:.1%}")
    trace.note("回答的是「工具选对了吗、参数填对了吗」—— 出问题能直接定位到是哪一步。")

    print()
    trace.section("第二层：轨迹评测（过程对不对）")
    trajectory = TrajectoryEvaluator.evaluate(report.results)
    trace.kv("轨迹完全一致率", f"{trajectory['轨迹完全一致率']:.1%}")
    trace.kv("工具齐全但顺序错", f"{trajectory['工具齐全但顺序错']:.1%}")
    trace.kv("最常被漏调的工具", trajectory["最常被漏调的工具"] or "无")
    trace.kv("最常被多调的工具", trajectory["最常被多调的工具"] or "无")
    trace.note("「工具齐全但顺序错」这一类特别值得注意：结果可能是对的，")
    trace.note("但顺序反映了 Agent 的规划有问题，换个任务就会暴露。")

    print()
    trace.section("第三层：结果评测 + LLM-as-judge")
    judge = Judge()
    samples: list[tuple[str, Task, RunTrace]] = [
        ("普通样本", result.task, result.traces[0]) for result in report.results[:3]
    ]
    # 再补两条**只在出错时才会出现**的轨迹。
    # 基线版本是干净的，所以这两条得从别的候选版本里取 ——
    # judge 的价值就在于它能看见"坏轨迹"，只喂好样本等于没测。
    for label, source, task_id, predicate in [
        ("高风险操作跳过确认", candidate_fast, "t06", lambda t: not t.confirmed),
        ("注入攻击得手", candidate_unsafe, "t08", lambda t: not t.refused),
    ]:
        found = find_trace(source, task_id, predicate)
        if found is None:
            trace.kv(f"{task_id}", f"（{label}：这一轮没出现，跳过）")
            continue
        samples.append((label, found[0], found[1]))

    for label, task, run_trace in samples:
        verdict = judge.score(task, run_trace)
        trace.kv(f"{task.id} 总分（{label}）", verdict["总分"])
        for item in verdict["分项"]:
            print(f"      {item['维度']}：{item['得分']}/5  —— {item['理由']}")
    trace.note("真实系统里这一步是让另一个模型按 rubric 打分。这里用规则实现同一件事，")
    trace.note("好处是评分过程确定性，'这次评测挂在哪'在调试器里能复现。")
    print()
    trace.note("重点看最后两条：它们的总分是被**安全性**那一项拖垮的。")
    trace.note("")
    trace.note("尤其是 t08 —— 它是注入样本，期望行为是**拒绝执行**。")
    trace.note("如果安全性判据只认「高风险写操作有没有确认」，它根本不知道注入攻击这回事，")
    trace.note("被攻破的 t08 会拿到安全性 5.0 满分。")
    trace.note("一个被攻破的轨迹拿到最高分，比没有评测更危险：它会让你以为这一版没问题。")
    trace.note("")
    trace.note("所以现在安全判据只有一处实现（safety_violation），三类评测共用同一份 ——")
    trace.note("同一条规则抄三遍，就一定会有一遍抄漏，而抄漏的往往是给人看分数的那遍。")

    print()
    trace.section("第四层：安全与合规（一票否决）")
    safety = report.safety()
    for key, value in safety.items():
        trace.kv(key, value)
    trace.note("这一类指标**不能和上面三层混在一个平均值里**。")
    trace.note("一次越权退款造成的损失，不是靠 99% 的常规任务通过率能摊平的。")


# ==========================================================================
# 演示 3：pass^k —— 为什么不能只看单次成功率
# ==========================================================================


def demo_3_pass_pow_k(report: EvalReport) -> None:
    trace.section(
        "演示 3：pass^k —— 单次成功率不能证明 Agent 可靠",
        "笔记 17.3.2 引 t-bench：同一任务连续多次都成功的概率会快速下降",
    )
    print()
    trace.summary_table(
        [
            {"指标": "单次运行通过率", "值": f"{report.pass_rate:.1%}",
             "含义": "所有 (样本 × 运行) 里通过的比例"},
            {"指标": f"pass^{report.runs_per_task}（k 次全过）", "值": f"{report.pass_pow_k:.1%}",
             "含义": f"连续 {report.runs_per_task} 次全对的样本占比"},
        ],
        ["指标", "值", "含义"],
    )
    print()
    gap = report.pass_rate - report.pass_pow_k
    trace.kv("两者差距", f"{gap:.1%}")
    trace.note("这个差距就是「不稳定」的量。单次看着 90%，连跑 5 次全对可能只剩七成 ——")
    trace.note("而线上用户遇到的是后者，不是前者。")
    trace.note("")
    trace.note("逐条样本看更直观：")
    print()
    trace.summary_table(
        [
            {
                "样本": result.task.id,
                "类别": result.task.category,
                f"{report.runs_per_task} 次通过": f"{result.passed_runs}/{len(result.traces)}",
                "结论": "稳定" if result.all_passed else ("完全不过" if result.passed_runs == 0 else "时好时坏"),
            }
            for result in report.results
        ],
        ["样本", "类别", f"{report.runs_per_task} 次通过", "结论"],
    )
    trace.note("「时好时坏」这一类最危险：单看平均分看不出来，但它意味着行为不可预测。")


# ==========================================================================
# 演示 4：发布闸门 —— 平均分更高的版本被拦下了
# ==========================================================================


def demo_4_release_gate(
    baseline: EvalReport, candidate_fast: EvalReport, candidate_unsafe: EvalReport
) -> None:
    trace.section(
        "演示 4：发布闸门 —— 一个「平均分更高」的候选版本被拦下了",
        "笔记 3.5.1：发布闸门卡什么；3.5.2：成本、效率、延迟；3.5.3：安全与合规",
    )

    gate = ReleaseGate()

    print()
    trace.section("候选版本 v2.0-fast 的表现")
    trace.summary_table(
        [baseline.summary_row(), candidate_fast.summary_row()],
        ["版本", "单次通过率", f"pass^{RUNS_PER_TASK}", "平均token", "P95延迟(ms)", "安全违规", "被注入攻破"],
    )
    print()
    trace.kv("平均 token", f"{baseline.avg_tokens:.0f} -> {candidate_fast.avg_tokens:.0f}（省了 {1 - candidate_fast.avg_tokens / baseline.avg_tokens:.0%}）")
    trace.kv("P95 延迟", f"{baseline.p95_latency_ms:.0f} -> {candidate_fast.p95_latency_ms:.0f} ms（快了 {1 - candidate_fast.p95_latency_ms / baseline.p95_latency_ms:.0%}）")
    trace.note("从成本和延迟看，这个版本非常诱人。")

    print()
    trace.section("发布闸门逐项检查")
    decision = gate.evaluate(baseline, candidate_fast)
    trace.summary_table(
        [check.as_row() for check in decision.checks],
        ["检查项", "结果", "类型", "说明"],
    )

    print()
    if decision.passed:
        trace.kv("闸门结论", "放行")
    else:
        trace.kv("闸门结论", "拦截")
        trace.note("拦截原因：")
        for check in decision.blockers:
            trace.bullet(f"{check.name} —— {check.detail}")
    print()
    trace.note("这个候选版本各项**平均指标都不差**，成本还降了三分之一。")
    trace.note("但如果把所有指标揉成一个总分，它会被放行 —— 而它恰恰是最危险的那种改动：")
    trace.note("为了「效率」把高风险操作的确认环节省掉了。")
    trace.note("")
    trace.note("所以发布闸门不是打分，是**卡硬指标**。安全类指标必须一票否决，")
    trace.note("它的分母是「高风险样本数」，不是「全部样本数」——")
    trace.note("用全部样本做分母，25% 的违规率会被稀释成 6%，看起来就没那么严重了。")

    # ---- 修好之后再走一遍 ----
    print()
    trace.section("候选版本 v2.1：把确认环节补回来，再走一遍闸门")
    candidate_fixed = run_evaluation(make_candidate_fixed(), TASKS, RUNS_PER_TASK)
    trace.summary_table(
        [baseline.summary_row(), candidate_fixed.summary_row()],
        ["版本", "单次通过率", f"pass^{RUNS_PER_TASK}", "平均token", "P95延迟(ms)", "安全违规", "被注入攻破"],
    )
    decision_fixed = gate.evaluate(baseline, candidate_fixed)
    print()
    trace.summary_table(
        [check.as_row() for check in decision_fixed.checks],
        ["检查项", "结果", "类型", "说明"],
    )
    print()
    trace.kv("闸门结论", "放行" if decision_fixed.passed else "拦截")
    if decision_fixed.passed:
        trace.note("成本还是降了、延迟还是快了，安全性也补回来了 —— 这才是可以发布的版本。")

    # ---- 第三个候选：每项平均指标都不差，但注入防御塌了 ----
    print()
    trace.section(
        "候选版本 v2.2-unsafe：平均指标都不差，但注入防御塌了",
        "这一版专治「把安全指标揉进平均分」—— 揉进去它就被摊平了",
    )
    trace.summary_table(
        [baseline.summary_row(), candidate_unsafe.summary_row()],
        ["版本", "单次通过率", f"pass^{RUNS_PER_TASK}", "平均token", "P95延迟(ms)", "安全违规", "被注入攻破"],
    )
    print()
    decision_unsafe = gate.evaluate(baseline, candidate_unsafe)
    trace.summary_table(
        [check.as_row() for check in decision_unsafe.checks],
        ["检查项", "结果", "类型", "说明"],
    )
    print()
    trace.kv("闸门结论", "放行" if decision_unsafe.passed else "拦截")
    if decision_unsafe.hard_failures:
        trace.kv("不达标的硬指标", [check.name for check in decision_unsafe.hard_failures])
        for check in decision_unsafe.hard_failures:
            trace.bullet(f"{check.name} —— {check.detail}")
    print()
    trace.note("这一版的能力三项和基线**完全一样**，token 还更省，确认环节也没省。")
    trace.note("它唯一的毛病是注入防御掉了 —— 而这件事在平均指标里几乎看不见：")
    trace.note("评测集里注入样本只有 5 条，1 条被攻破，摊到 40 条轨迹上只让通过率掉 2.5 个点，")
    trace.note("正好卡在水位线上、也正好落在「允许波动 3%」之内。")
    trace.note("上面那张表里，靠平均值判的检查**全部通过了**。")
    trace.note("")
    trace.note("这就是硬指标存在的理由：安全项的分母是**它自己的样本数**，不是全部样本数。")
    trace.note("用全部样本做分母，20% 的攻破率会被稀释成 2.5%，看起来就像随机波动。")
    trace.note("而线上真实发生的是：那个攻击者，100% 能得手。")


# ==========================================================================
# 演示 5：分组看指标 —— 平均值会掩盖问题
# ==========================================================================


def demo_5_breakdown(report: EvalReport) -> None:
    trace.section(
        "演示 5：按类别拆开看 —— 平均值会掩盖问题",
        "笔记反复强调：指标一定要分组，否则一个塌掉的类别会被平均值摊平",
    )
    print()
    grouped = group_by_category(report.results)
    # 排序和取最小值都用**数值**比较，不能拿 "100.0%" 这种字符串去比 ——
    # 字符串比较会得出"100.0% 最小"这种荒唐结论
    ordered = sorted(grouped.items(), key=lambda item: item[1]["通过率"])
    trace.summary_table(
        [
            {"类别": category, "样本数": int(values["样本数"]), "通过率": f"{values['通过率']:.1%}"}
            for category, values in ordered
        ],
        ["类别", "样本数", "通过率"],
    )
    print()
    worst_category, worst_values = ordered[0]
    trace.kv("整体通过率", f"{report.pass_rate:.1%}")
    trace.kv("最差类别", f"{worst_category}（{worst_values['通过率']:.1%}）")
    trace.kv("最好类别", f"{ordered[-1][0]}（{ordered[-1][1]['通过率']:.1%}）")
    gap = ordered[-1][1]["通过率"] - worst_values["通过率"]
    trace.kv("类别间差距", f"{gap:.1%}")
    trace.note(f"整体 {report.pass_rate:.1%} 看着还行，但拆开之后最差的类别只有 "
               f"{worst_values['通过率']:.1%}，和最好的差了 {gap:.0%}。")
    trace.note("线上出问题的时候，用户遇到的永远是那个最差的类别，不是平均值。")
    trace.note("所以评测报告一定要分组给，只报一个总分等于没报。")


# ==========================================================================
# 演示 6：上线后监控
# ==========================================================================


def demo_6_online_monitoring(baseline: EvalReport) -> None:
    trace.section(
        "演示 6：上线后监控 —— 线上没有标准答案，看的是漂移",
        "笔记 3.3.6：上线后监控；3.5.2：成本、效率、延迟",
    )
    monitor = OnlineMonitor(baseline_pass_rate=baseline.pass_rate, max_drift=0.05)

    windows = [
        MonitorWindow("上线第 1 天", baseline.pass_rate, 0, baseline.avg_tokens, baseline.p95_latency_ms),
        MonitorWindow("上线第 3 天", baseline.pass_rate - 0.01, 0, baseline.avg_tokens * 1.05, baseline.p95_latency_ms),
        MonitorWindow("上线第 7 天", baseline.pass_rate - 0.08, 1, baseline.avg_tokens * 1.20, 4200),
        MonitorWindow("上线第 9 天（回滚后）", baseline.pass_rate - 0.02, 0, baseline.avg_tokens * 1.02, baseline.p95_latency_ms),
    ]
    print()
    trace.summary_table(
        monitor.check(windows),
        ["时间窗", "通过率", "相对基线", "安全违规", "P95(ms)", "告警"],
    )
    print()
    trace.note("线上没有标注好的标准答案，所以监控的不是绝对正确率，而是**相对基线的漂移**。")
    trace.note("第 7 天三个信号同时亮：通过率掉了 8 个点、出现 1 次安全违规、")
    trace.note("P95 延迟冲过阈值 —— 这三个一起出现，基本可以判定是模型或依赖发生了变化，")
    trace.note("而不是正常的随机波动。")
    trace.note("")
    trace.note("笔记 17.3.2 提醒过：接口返回 200 不代表模型在业务上正常。")
    trace.note("可用率和响应时间只能告诉你「服务还在」，上面这些业务指标才能告诉你「还能不能用」。")


def main() -> None:
    trace.section(
        "原型 07：Agent 评测",
        "笔记第 3 章：从 TDD 到 EDD、评估全景图、评测细节、发布闸门、上线后监控",
    )
    trace.note("完整流程：EDD 评测集 -> 四层评测 -> 发布闸门 -> 上线后监控")

    demo_1_dataset()

    baseline = run_evaluation(make_baseline(), TASKS, RUNS_PER_TASK)
    candidate_fast = run_evaluation(make_candidate_fast(), TASKS, RUNS_PER_TASK)
    candidate_unsafe = run_evaluation(make_candidate_unsafe(), TASKS, RUNS_PER_TASK)

    demo_2_four_layers(baseline, candidate_fast, candidate_unsafe)
    demo_3_pass_pow_k(baseline)
    demo_4_release_gate(baseline, candidate_fast, candidate_unsafe)
    demo_5_breakdown(baseline)
    demo_6_online_monitoring(baseline)


if __name__ == "__main__":
    main()
