"""实验四的六个用例。全部围绕"暂停恢复机制对不对"，不测业务逻辑。

命名刻意用"能抓到什么 bug"而不是"测什么功能"：
    1. 基本闭环          —— 结构可行性的下限
    2. 跨进程恢复        —— 内存里没被复用的对象，才是真正重建过的
    3. 同一次运行内多次暂停 —— 对话拼接、回合计数在多轮之间会不会错位
    4. 长间隔恢复的预算   —— 复用 perf_counter 老写法会在这里静默失效
    5. 幂等性            —— 网络重试不会塞两条同 tool_call_id 的回复
    6. 模型问不停        —— 配额跨暂停累计，而不是每次恢复发一份新的
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as C
from .loop import AgentHilLoop, HilError
from .provider import ScriptExhausted
from .snapshot import (HUMAN_TOOL, TERMINAL, WAITING, SnapshotError, check_snapshot,
                       ensure_ids, new_snapshot)
from .store import AgentHilStore

ROOT = Path(__file__).resolve().parent.parent.parent


class PauseResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.db_path = Path(self.temp_dir.name) / "exp4.sqlite3"
        self.store = AgentHilStore(self.db_path)
        self.loop = AgentHilLoop(self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    # ---------------- 助手 ----------------

    def run_start(self, script, **limits):
        return self.loop.start(C.SCENARIO, C.config(script),
                               C.limits_dict(**limits), C.REQUEST)

    def reply(self, result, response=None, response_id=None):
        return self.loop.respond(result["human_request"]["interaction_id"],
                                 response or {"action": "reply", "text": "可以"},
                                 response_id=response_id)

    def snapshot(self, run_id):
        return self.store.load_snapshot(run_id)

    def rewrite(self, run_id, mutate):
        """直接改快照。用来构造"暂停前已经用掉大半预算"这类前置状态。"""
        with self.store.transaction() as db:
            snapshot = self.store.load_snapshot(run_id, db)
            mutate(snapshot)
            self.store.save_snapshot(db, snapshot, "test_rewrite", {})

    def backdate(self, interaction, days=3):
        """把人工等待的开始时间往前挪，模拟"进程挂了很多天"。"""
        past = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.store.transaction() as db:
            db.execute("UPDATE human_interactions SET created_at=? WHERE interaction_id=?",
                       (past, interaction["interaction_id"]))
            db.execute("UPDATE runs SET created_at=?, updated_at=? WHERE run_id=?",
                       (past, past, interaction["run_id"]))

    def tool_replies(self, run_id):
        return [m["tool_call_id"] for m in self.snapshot(run_id)["messages"]
                if m["role"] == "tool" and m.get("tool_call_id")]

    # ---------------- 用例 1：单次暂停 + 恢复的基本闭环 ----------------

    def test_1_pause_and_resume_closes_the_loop(self):
        result = self.run_start(C.SCRIPTS["happy"])

        # 暂停点：assistant 的 request_human_input 已落库，对应的 tool 回复还没有。
        self.assertEqual(result["status"], WAITING)
        self.assertEqual(result["step"], 2)
        self.assertEqual(result["tool_calls"], 1)
        request = result["human_request"]
        self.assertEqual(request["tool_call_id"], "scripted:1")
        self.assertTrue(request["payload"]["question"])

        finished = self.reply(result)
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["verdict"]["status"], "planned")
        self.assertEqual(finished["verdict"]["evidence_ids"], ["train_early", "hotel_near"])

        snapshot = self.snapshot(result["run_id"])
        self.assertEqual(snapshot["step"], snapshot["run"]["model_calls"])
        # 三条工具回复：两次 get_price + 一次人工回复，没有多余的空洞。
        self.assertEqual(self.tool_replies(result["run_id"]),
                         ["scripted:0", "scripted:1", "scripted:2"])
        self.assertEqual(snapshot["pending_tool_call"], None)
        self.assertEqual(snapshot["run"]["status"], "planned")
        self.assertEqual(snapshot["run"]["observed_evidence_ids"],
                         ["hotel_near", "train_early"])

    # ---------------- 用例 2：真正跨进程恢复 ----------------

    def test_2_resume_across_real_processes(self):
        # 暂停点是另一个 Python 进程跑出来的，本进程只见过数据库文件。
        started = self.cli("start", "--script", "two-asks")
        run_id, first = started["run_id"], started["human_request"]["interaction_id"]

        # 冷启动：全新的 store 和 loop，不共用任何内存对象。
        fresh = AgentHilLoop(AgentHilStore(self.db_path))
        resumed = fresh.resume(run_id)
        self.assertEqual(resumed["status"], WAITING)
        self.assertEqual(resumed["human_request"]["interaction_id"], first)
        self.assertEqual(resumed["step"], 2)

        # 第二次暂停在第三个进程里再恢复一次。
        second = fresh.respond(first, {"action": "reply", "text": "早班"}, response_id="r-1")
        self.assertEqual(second["status"], WAITING)
        again = self.cli("resume", "--run", run_id)
        self.assertEqual(again["human_request"]["interaction_id"],
                         second["human_request"]["interaction_id"])
        self.assertEqual(again["human_requests"], 2)

        done = self.cli("respond", "--interaction", again["human_request"]["interaction_id"],
                        "--action", "reply", "--text", "接受")
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["model_calls"], 5)
        self.assertEqual(done["tool_calls"], 2)

    # ---------------- 用例 3：同一次运行内多次暂停 ----------------

    def test_3_multiple_pauses_in_one_run(self):
        first = self.run_start(C.SCRIPTS["two-asks"])
        self.assertEqual(first["human_requests"], 1)
        second = self.reply(first, {"action": "reply", "text": "早班"})
        self.assertEqual(second["status"], WAITING)
        self.assertEqual(second["human_requests"], 2)
        # 两次暂停之间有一次真实工具调用，工具计数必须跟着走。
        self.assertEqual(second["tool_calls"], 2)
        self.assertEqual(second["model_calls"], 4)

        done = self.reply(second, {"action": "reply", "text": "接受"})
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["human_requests"], 2)

        snapshot = self.snapshot(done["run_id"])
        self.assertEqual(snapshot["step"], 5)
        self.assertEqual(snapshot["step"], snapshot["run"]["model_calls"])
        # 回合数、工具数、人工环节都不重不漏。
        self.assertEqual([m["role"] for m in snapshot["messages"]].count("assistant"), 5)
        self.assertEqual(self.tool_replies(done["run_id"]),
                         ["scripted:0", "scripted:1", "scripted:2", "scripted:3"])
        kinds = [m["tool_name"] for m in snapshot["messages"] if m["role"] == "tool"]
        self.assertEqual(kinds.count(HUMAN_TOOL), 2)
        events = [e["type"] for e in self.store.list_events(done["run_id"])]
        self.assertEqual(events.count("run_waiting_for_human"), 2)
        self.assertEqual(events.count("human_responded"), 2)
        self.assertEqual(events.count("run_resumed"), 2)

    # ---------------- 用例 4：长间隔恢复的预算计算 ----------------

    def test_4_budget_survives_a_long_gap(self):
        result = self.run_start(C.SCRIPTS["happy"], human_ttl_minutes=60 * 24 * 30)
        self.rewrite(result["run_id"], lambda s: s.__setitem__("elapsed_s", 30.0))
        self.backdate(result["human_request"], days=7)

        resumed = AgentHilLoop(AgentHilStore(self.db_path)).resume(result["run_id"])
        # 等待 7 天不消耗活跃预算：恢复后已用时长仍是暂停前那 30 秒。
        self.assertEqual(resumed["status"], WAITING)
        self.assertEqual(resumed["elapsed_s"], 30.0)

        done = self.reply(resumed)
        self.assertEqual(done["status"], "completed")
        # 总活跃时长仍然远小于 timeout_s，说明没有把停机时间算进来，
        # 也没有在恢复时重新发一份 180 秒的预算。
        self.assertLess(done["elapsed_s"], 180.0)

    def test_4b_budget_is_not_refilled_on_resume(self):
        """老写法（perf_counter + timeout_s）真正的症状不是"算错"，而是
        **每次恢复都静默地发一份全新的预算**：模型可以用 180 秒 × 恢复次数。

        所以这条用例直接断言剩余预算的数值，不靠计时赛跑——脚本 provider 的
        调用只要微秒级，剩 0.1 秒照样能跑完，那种写法区分不出新旧实现。
        """
        result = self.run_start(C.SCRIPTS["happy"], human_ttl_minutes=60 * 24 * 30)
        self.rewrite(result["run_id"], lambda s: s.__setitem__("elapsed_s", 179.5))
        self.backdate(result["human_request"], days=7)

        loop = AgentHilLoop(AgentHilStore(self.db_path))
        resumed = loop.resume(result["run_id"])
        # 关键断言：恢复后只剩 0.5 秒，而不是一份新的 180 秒。
        self.assertEqual(resumed["remaining_s"], 0.5)

        # 而真正用完之后（elapsed_s 达到上限），恢复会立刻终止而不是再发一份。
        self.rewrite(result["run_id"], lambda s: s.__setitem__("elapsed_s", 180.0))
        exhausted = loop.respond(result["human_request"]["interaction_id"],
                                 {"action": "reply", "text": "可以"})
        self.assertEqual(exhausted["status"], "failed")
        self.assertEqual(exhausted["verdict"]["status"], "needs_review")
        snapshot = self.snapshot(result["run_id"])
        self.assertEqual(snapshot["run"]["termination"], "budget_exceeded")
        self.assertEqual(snapshot["run"]["budget_reason"], "deadline")

    # ---------------- 用例 5：恢复请求的幂等性 ----------------

    def test_5_response_is_idempotent(self):
        first = self.run_start(C.SCRIPTS["two-asks"])
        interaction_id = first["human_request"]["interaction_id"]

        once = self.loop.respond(interaction_id, {"action": "reply", "text": "早班"},
                                 response_id="same-id")
        self.assertEqual(once["status"], WAITING)
        before = self.tool_replies(once["run_id"])
        step = once["step"]

        # 同一 response_id 重复提交（模拟网络重试）：不推进，也不多塞一条回复。
        twice = self.loop.respond(interaction_id, {"action": "reply", "text": "早班"},
                                  response_id="same-id")
        self.assertEqual(twice["step"], step)
        self.assertEqual(twice["model_calls"], once["model_calls"])
        self.assertEqual(self.tool_replies(once["run_id"]), before)
        self.assertEqual(len(before), len(set(before)))

        # 换了 response_id 再提交同一个 interaction，必须被拒绝。
        with self.assertRaises(HilError):
            self.loop.respond(interaction_id, {"action": "reply", "text": "晚班"},
                              response_id="other-id")
        self.assertEqual(self.tool_replies(once["run_id"]), before)

    def test_5b_stale_interaction_is_rejected(self):
        first = self.run_start(C.SCRIPTS["two-asks"])
        second = self.reply(first, response_id="r-1")
        self.assertEqual(second["status"], WAITING)
        # 拿已经消费掉的那条 interaction 再提交，且换一个 response_id。
        with self.assertRaises(HilError):
            self.loop.respond(first["human_request"]["interaction_id"],
                              {"action": "reply"}, response_id="r-2")

    # ---------------- 用例 6：模型问不停 ----------------

    def test_6_budget_accumulates_across_pauses(self):
        result = self.run_start(C.SCRIPTS["ask-forever"], max_human_requests=3)
        seen = []
        while result["status"] == WAITING:
            seen.append(result["human_requests"])
            result = self.reply(result)

        # 三次之后就停：配额是跨暂停累计的，不是每次恢复重新发一份。
        self.assertEqual(seen, [1, 2, 3])
        self.assertEqual(result["status"], "failed")
        snapshot = self.snapshot(result["run_id"])
        self.assertEqual(snapshot["human_requests"], 3)
        self.assertEqual(snapshot["run"]["termination"], "budget_exceeded")
        self.assertEqual(snapshot["run"]["budget_reason"], "human_requests")
        # 会话历史里的回合数与计数器一致，没有因为反复暂停而算重。
        self.assertEqual(snapshot["step"], snapshot["run"]["model_calls"])
        self.assertEqual(snapshot["step"], 4)

    def test_6b_max_steps_also_accumulates(self):
        result = self.run_start(C.SCRIPTS["ask-forever"], max_steps=2, max_human_requests=10)
        result = self.reply(result)          # 第 2 个回合
        result = self.reply(result)          # 第 3 个回合会撞上 max_steps
        self.assertEqual(result["status"], "failed")
        snapshot = self.snapshot(result["run_id"])
        self.assertEqual(snapshot["run"]["budget_reason"], "steps")
        self.assertEqual(snapshot["step"], 2)

    # ---------------- 失败路径 ----------------

    def test_unobserved_evidence_is_blocked(self):
        result = self.run_start(C.SCRIPTS["unknown-evidence"])
        self.assertEqual(result["status"], "failed")
        snapshot = self.snapshot(result["run_id"])
        self.assertEqual(snapshot["run"]["termination"], "invalid_response")
        self.assertEqual(snapshot["run"]["guardrail_blocks"], 1)
        self.assertEqual(snapshot["verdict"]["status"], "needs_review")

    def test_script_exhaustion_terminates_the_run(self):
        """脚本用完 → provider 异常。用来说明"运行不会永远挂着"。"""
        result = self.run_start([C.price("train_early")])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.snapshot(result["run_id"])["run"]["termination"],
                         "provider_error")
        self.assertIsInstance(ScriptExhausted("x"), Exception)

    def test_waiting_run_cannot_be_advanced_without_a_reply(self):
        result = self.run_start(C.SCRIPTS["happy"])
        resumed = self.loop.resume(result["run_id"])
        self.assertEqual(resumed["status"], WAITING)
        self.assertEqual(resumed["step"], 2)          # 没有偷偷往后跑一个回合
        self.assertEqual(self.loop.resume(result["run_id"])["step"], 2)

    def test_expired_interaction_terminates_the_run(self):
        result = self.run_start(C.SCRIPTS["happy"], human_ttl_minutes=1)
        with self.store.transaction() as db:
            db.execute("UPDATE human_interactions SET expires_at=? WHERE run_id=?",
                       ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                        result["run_id"]))
        resumed = AgentHilLoop(AgentHilStore(self.db_path)).resume(result["run_id"])
        self.assertEqual(resumed["status"], "expired")
        self.assertIn(resumed["status"], TERMINAL)
        # 过期之后不能再用那条 interaction 推进。
        with self.assertRaises(HilError):
            self.loop.respond(result["human_request"]["interaction_id"],
                              {"action": "reply"}, response_id="too-late")

    # ---------------- 子进程助手 ----------------

    def cli(self, *args):
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", "-m",
             "experiments.experiment_4_agent_hil.real_run", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",      # 新增：强制以utf-8读取stdout/stderr
            errors="replace",      # 新增：遇到无法解码字节替换为�，不直接抛异常
            timeout=600
        )
        if completed.returncode != 0:
            self.fail(f"cli {' '.join(args)} failed: {completed.stderr[-2000:]}")
        return json.loads(completed.stdout)


class SnapshotInvariantTests(unittest.TestCase):
    """把"哪类 bug 会被哪条不变量拦住"固化下来。

    这些负例不是假想：用例 5 的"重复喂回"在最初的实现里就是漏检的——
    当时用 set 统计已回复的 tool_call_id，两条同 id 的回复会塌缩成一条，
    而重复喂回恰恰是幂等失效唯一的表现形式。
    """

    def setUp(self):
        self.limits = C.limits_dict()
        run = {"model_calls": 1, "tool_calls": 1, "input_tokens": 0, "output_tokens": 0,
               "guardrail_blocks": 0}
        self.snapshot = new_snapshot(run_id="r1", trace_id="t1", config={}, scenario={},
                                     limits=self.limits, prompt="p", request={}, run=run)
        self.snapshot["step"] = 1
        self.snapshot["messages"] += [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "function": {"name": "get_price", "arguments": {}}}]},
            {"role": "tool", "tool_name": "get_price", "tool_call_id": "c1", "content": "{}"}]
        check_snapshot(self.snapshot)          # 夹具自身必须是合法的

    def probe(self, mutate, expected):
        snapshot = copy.deepcopy(self.snapshot)
        mutate(snapshot)
        with self.assertRaises(SnapshotError) as caught:
            check_snapshot(snapshot)
        self.assertIn(expected, str(caught.exception))

    def test_duplicate_tool_reply_is_detected(self):
        self.probe(lambda s: s["messages"].append(
            {"role": "tool", "tool_name": "get_price", "tool_call_id": "c1", "content": "{}"}),
            "duplicate tool reply")

    def test_turn_count_drift_is_detected(self):
        self.probe(lambda s: s.__setitem__("step", 5), "step 5 != model_calls 1")

    def test_refilled_quota_is_detected(self):
        self.probe(lambda s: s.__setitem__("human_requests", 99), "already exceeded")

    def test_missing_tool_reply_is_detected(self):
        def add_orphan(s):
            s["messages"].append({"role": "assistant", "content": "", "tool_calls": [
                {"id": "c9", "function": {"name": "get_price", "arguments": {}}}]})
            s["step"], s["run"]["model_calls"] = 2, 2
        self.probe(add_orphan, "outside a terminal run")

    def test_waiting_without_a_pending_call_is_detected(self):
        self.probe(lambda s: s.__setitem__("status", WAITING),
                   "without a pending tool call")

    def test_waiting_on_a_non_human_tool_is_detected(self):
        def wrong_tool(s):
            s["messages"].append({"role": "assistant", "content": "", "tool_calls": [
                {"id": "c3", "function": {"name": "get_price", "arguments": {}}}]})
            s["step"], s["run"]["model_calls"] = 2, 2
            s["pending_tool_call"] = {"id": "c3", "name": "get_price", "arguments": {}}
            s["status"] = WAITING
        self.probe(wrong_tool, "waiting for human on tool get_price")

    def test_multiple_tool_calls_per_turn_is_detected(self):
        self.probe(lambda s: s["messages"].append({"role": "assistant", "content": "",
            "tool_calls": [{"id": "cx", "function": {"name": "a", "arguments": {}}},
                           {"id": "cy", "function": {"name": "b", "arguments": {}}}]}),
            "exactly one tool call")

    def test_unsynthesised_id_is_detected(self):
        def no_id(s):
            s["messages"].append({"role": "assistant", "content": "",
                                  "tool_calls": [{"function": {"name": "get_price",
                                                                "arguments": {}}}]})
            s["step"], s["run"]["model_calls"] = 2, 2
        self.probe(no_id, "without an id")

    def test_terminal_run_may_leave_the_finish_call_unanswered(self):
        # 正例：finish 本身不需要工具回复，而且终态不会再发给模型。
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["messages"].append({"role": "assistant", "content": "", "tool_calls": [
            {"id": "cf", "function": {"name": "finish", "arguments": {}}}]})
        snapshot["step"], snapshot["run"]["model_calls"] = 2, 2
        snapshot["status"] = "completed"
        check_snapshot(snapshot)

    def test_synthetic_ids_only_fill_missing_ones(self):
        message = ensure_ids({"role": "assistant", "tool_calls": [
            {"id": "real", "function": {"name": "a", "arguments": {}}},
            {"function": {"name": "b", "arguments": {}}}]}, 3)
        self.assertEqual([call["id"] for call in message["tool_calls"]], ["real", "synthetic:3"])


if __name__ == "__main__":
    unittest.main()
