"""实验四主体：可暂停、可跨进程恢复的 LLM agent loop。

与 agent.py:65 那个 for 循环的唯一结构差异：
把"一个模型回合"抽成 _turn()，每次调用在一处提交快照后返回；
_drive() 负责循环，直到 WAIT 或终态。进程可以在 WAIT 处退出，
恢复时从快照重建，而不是从 step=0 重来。

事务纪律（兑现 exp3 README 第 50 行自己写下的取舍）：
    短事务写快照 ──> 无事务调模型 / 执行工具 ──> 短事务写结果
绝不在持有数据库写锁的情况下等网络或等人工。

时间纪律：
    elapsed_s 只累加"模型调用 + 工具执行"的实际耗时；
    等待人工、进程停机都不计入——它们由 interaction.expires_at 单独约束。
    run["duration_ms"] 因此报的是活跃时长，不是墙上时间（agent.py:180 报的是后者，
    跨进程没有意义）。

崩溃语义（本实验明确不解决的）：
    工具执行是 at-least-once。进程死在"工具已执行、结果未落库"之间时，
    恢复会重做这次调用。demo 工具对外部世界无副作用，重放安全；
    真实副作用（下单/支付）的 exactly-once 不在本实验范围，见 README 取舍一节。
"""
from __future__ import annotations

from datetime import datetime, timezone
import time
import uuid

from . import provider as provider_module
from . import tools as tools_module
from .snapshot import (
    FINISH_TOOL, HUMAN_TOOL, RUNNING, TERMINAL, WAITING,
    ensure_ids, human_tool_message, new_snapshot, public, remaining_s, tool_message,
)
from .store import utcnow
from .verdict import handoff, validate_finish


class HilError(Exception):
    pass


class BudgetExceeded(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


CONTINUE, WAIT, DONE = "continue", "wait", "done"


def make_run(*, trace_id, scenario, config, limits):
    """对应 agent.py:42-50 的 run 字典。跨进程要能原样恢复，所以整体进快照。"""
    return {
        "trace_id": trace_id, "started_at": utcnow(), "scenario": scenario.get("id"),
        "provider": config["provider"], "model": config.get("model"),
        "tool_mode": config.get("tool_mode", "demo"),
        "status": RUNNING, "termination": None,
        "limits": dict(limits),
        "input_tokens": 0, "output_tokens": 0, "cache_hit_tokens": 0,
        "usage_complete": True, "cost_usd": 0 if config["provider"] == "scripted" else None,
        "model_calls": 0, "tool_calls": 0, "guardrail_blocks": 0,
        "observed_evidence_ids": [],
    }


class AgentHilLoop:
    """三个入口（start / respond / resume）都收敛到 _drive()，与 runtime.py 同构。"""

    def __init__(self, store, provider_factory=provider_module.build_provider,
                 tools_factory=tools_module.DemoTools):
        self.store = store
        self.provider_factory = provider_factory
        self.tools_factory = tools_factory

    # ================= 入口一：新建并跑到第一个暂停点或终态 =================

    def start(self, scenario, config, limits, request):
        run_id, trace_id = uuid.uuid4().hex, uuid.uuid4().hex
        limits, config = dict(limits), dict(config)
        run = make_run(trace_id=trace_id, scenario=scenario, config=config, limits=limits)
        snapshot = new_snapshot(run_id=run_id, trace_id=trace_id, config=config,
                                scenario=scenario, limits=limits,
                                prompt=tools_module.PROMPT, request=request, run=run)
        snapshot["toolbox_state"] = tools_module.new_state(scenario)
        with self.store.transaction() as db:
            self.store.create_run(db, snapshot)
        return self._drive(snapshot)

    # ================= 入口二：人工回复（幂等） =================

    def respond(self, interaction_id, response, response_id=None, expected_version=None):
        response_id = response_id or uuid.uuid4().hex
        interaction = self.store.get_interaction(interaction_id)

        # 幂等：同一 response_id 重复提交直接返回当前状态，不重复推进（runtime.py:124-150）。
        if interaction["status"] == "consumed":
            if interaction["response_id"] == response_id:
                return self.resume(interaction["run_id"])
            raise HilError("interaction already consumed")
        if interaction["status"] != "pending":
            raise HilError(f"interaction is {interaction['status']}")
        if expected_version is not None and interaction["state_version"] != expected_version:
            raise HilError("stale state version")
        expired = self._expire_if_due(interaction)
        if expired is not None:
            return public(expired)

        # ---- 关键事务：消费 + 喂回 + 复位必须原子提交 ----
        # 只做其中一部分，就会出现"消费了但没喂回"（模型永远等不到结果），
        # 或"喂回了两遍"（两条同 tool_call_id 的 tool 消息，API 直接报错）。
        with self.store.transaction() as db:
            snapshot = self.store.load_snapshot(interaction["run_id"], db)
            if snapshot["status"] != WAITING:
                raise HilError(f"run is {snapshot['status']}")
            pending = snapshot["pending_tool_call"]
            if pending is None or pending["id"] != interaction["tool_call_id"]:
                raise HilError("interaction does not match the pending tool call")
            self.store.consume_interaction(db, interaction_id, response_id, response)
            snapshot["messages"].append(human_tool_message(pending, response))
            snapshot["pending_tool_call"] = None
            snapshot["status"] = RUNNING
            snapshot["stage"] = "model_turn"
            # 事件名不能也叫 human_responded：那个事实由 consume_interaction 记，
            # 同一事务里记两条同名事件会让事件流对不上"到底发生了什么"。
            self.store.save_snapshot(db, snapshot, "run_resumed",
                                     {"interaction_id": interaction_id})

        # 事务外继续推进。elapsed_s 不变——等待人工不消耗活跃预算。
        return self._drive(snapshot)

    # ================= 入口三：冷启动 / 崩溃恢复 =================

    def resume(self, run_id):
        snapshot = self.store.load_snapshot(run_id)   # decode() 内含不变量自检
        if snapshot["status"] in TERMINAL:
            return public(snapshot)
        if snapshot["status"] == WAITING:
            interaction = self.store.latest_interaction(run_id)
            if interaction is None or interaction["status"] != "pending":
                # 消费和喂回是同一个事务，所以这里不该出现别的状态。
                # 真出现了，说明原子性被破坏了，要让它响。
                raise HilError("waiting run has no pending interaction")
            expired = self._expire_if_due(interaction)
            if expired is not None:
                return public(expired)
            return public(snapshot, human_request=interaction)
        # RUNNING：上一轮进程死在回合中途。_drive 会按 pending_tool_call 决定
        # 是"重做分派"还是"开新回合"——不能无脑开新回合。
        return self._drive(snapshot)

    # ================= 循环本体 =================

    def _drive(self, snapshot):
        """循环 _turn/_dispatch，直到进入 WAIT 或终态。

        pending_tool_call≠None 而 status 仍是 RUNNING，只有一种成因：
        上一回合的模型响应已落库、分派没做完。这时必须重做分派而不是调模型，
        否则历史里会出现第二个孤儿 tool_call，下一条不变量校验就会炸。
        """
        try:
            while snapshot["status"] == RUNNING:
                outcome = (self._dispatch(snapshot)
                           if snapshot["pending_tool_call"] is not None
                           else self._turn(snapshot))
                if outcome is not CONTINUE:
                    break
        except BudgetExceeded as exc:
            return self._terminate(snapshot, "budget_exceeded", reason=exc.reason)
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            return self._terminate(snapshot, "invalid_response", error_type=type(exc).__name__)
        except Exception as exc:
            # 不落库异常内容：上游消息可能含密钥或 URL（agent.py:170 同款约束）。
            return self._terminate(snapshot, "provider_error", error_type=type(exc).__name__,
                                   http_status=getattr(exc, "http_status", None))
        if snapshot["status"] == WAITING:
            # 调用方拿到 human_request 才算真的"暂停在这里"，和 runtime.py 的 _wait 一致。
            return public(snapshot, human_request=self.store.latest_interaction(snapshot["run_id"]))
        return public(snapshot)

    def _turn(self, snapshot):
        """一个模型回合。返回 CONTINUE / WAIT / DONE。"""
        limits = snapshot["limits"]

        # ---- 预算检查：全部基于持久化状态，没有一个用 perf_counter 的绝对点 ----
        if remaining_s(snapshot) <= 0:
            raise BudgetExceeded("deadline")
        if snapshot["run"]["input_tokens"] + snapshot["run"]["output_tokens"] >= limits["max_tokens"]:
            raise BudgetExceeded("tokens")
        if snapshot["step"] >= limits["max_steps"]:
            raise BudgetExceeded("steps")

        # ---- 段 1（无事务）：调模型 ----
        # provider 每回合现造：快照里只存 config，不存实例（见 provider.py 的说明）。
        started = time.perf_counter()
        response = self.provider_factory(snapshot["config"]).chat(
            snapshot["messages"], self._schemas(snapshot),
            min(limits["model_timeout_s"], remaining_s(snapshot)))
        snapshot["elapsed_s"] += time.perf_counter() - started

        message = response["message"]
        calls = message.get("tool_calls") or []
        if len(calls) != 1:
            raise ValueError("Exactly one tool call per step required")
        name, args = calls[0]["function"]["name"], calls[0]["function"]["arguments"]
        if not isinstance(name, str) or name not in self._tool_names(snapshot):
            snapshot["run"]["guardrail_blocks"] += 1
            raise ValueError("Unknown tool")

        # ---- 段 2（短事务）：记账 + 追加 assistant 消息 + 记下尚未答复的调用 ----
        with self.store.transaction() as db:
            snapshot["run"]["model_calls"] += 1
            snapshot["step"] += 1
            self._count_usage(snapshot, response)
            # 落库前补齐 id：这一步之后，历史里每条 tool_call 都能和它的回复对账，
            # 重复喂回、漏喂回才都是可检测的。
            snapshot["messages"].append(ensure_ids(message, snapshot["step"]))
            snapshot["pending_tool_call"] = {"id": message["tool_calls"][0]["id"],
                                             "name": name, "arguments": args}
            self.store.save_snapshot(db, snapshot, "model_turn", {"tool": name})

        return self._dispatch(snapshot)

    def _dispatch(self, snapshot):
        """按模型选中的工具名分流。这里就是 runtime.py 里 if-else 硬编码 stage 的位置，
        现在变成了"模型自己选的 name 决定走哪条路"——本实验的命题本身，只有这三行。"""
        pending = snapshot["pending_tool_call"]
        if pending["name"] == FINISH_TOOL:
            return self._finish(snapshot)
        if pending["name"] == HUMAN_TOOL:
            return self._pause(snapshot)
        return self._execute(snapshot)

    # ---------------- 分支 A：普通工具 ----------------

    def _execute(self, snapshot):
        if snapshot["toolbox_state"]["calls"] >= snapshot["limits"]["max_tool_calls"]:
            raise BudgetExceeded("tool_calls")
        pending = snapshot["pending_tool_call"]
        # ---- 段 1（无事务）：执行工具 ----
        # toolbox 持有的是 snapshot["toolbox_state"] 这个裸 dict 的引用，
        # 所以 execute 里的计数/证据/故障写入会直接反映到快照上，下一次提交就落库。
        toolbox = self.tools_factory(snapshot["config"], snapshot["scenario"],
                                     snapshot["toolbox_state"])
        started = time.perf_counter()
        result = toolbox.execute(pending["name"], pending["arguments"])
        snapshot["elapsed_s"] += time.perf_counter() - started
        # ---- 段 2（短事务）：结果 + 回复消息 + 清除待答复 ----
        with self.store.transaction() as db:
            key = result.get("call_id", pending["name"])
            snapshot["tool_results"][key] = result
            snapshot["messages"].append(tool_message(pending, result))
            snapshot["pending_tool_call"] = None
            snapshot["run"]["tool_calls"] = snapshot["toolbox_state"]["calls"]
            self.store.save_snapshot(db, snapshot, "tool_result",
                                     {"tool": pending["name"], "key": key,
                                      "status": result.get("status", "ok")})
        return CONTINUE

    # ---------------- 分支 B：请求人工输入（本实验新增的暂停点） ----------------

    def _pause(self, snapshot):
        """★ 模型自主选择停下来问人。这里不做任何业务判断，
        "该不该问"完全由模型在 _turn 里决定——这正是与 runtime.py 固定检查点的区别。"""
        if snapshot["human_requests"] >= snapshot["limits"]["max_human_requests"]:
            raise BudgetExceeded("human_requests")
        pending = snapshot["pending_tool_call"]
        arguments = pending["arguments"] if isinstance(pending["arguments"], dict) else {}
        # ---- 单个短事务：计数 + 人工请求 + 状态 + 快照一次提交 ----
        with self.store.transaction() as db:
            snapshot["human_requests"] += 1
            snapshot["status"] = WAITING
            snapshot["stage"] = "await_human"
            interaction = self.store.create_interaction(
                db, snapshot["run_id"], kind=arguments.get("kind", "input"),
                state_version=snapshot["step"], tool_call_id=pending["id"],
                payload={"question": arguments.get("question", ""),
                         "options": arguments.get("options", [])},
                ttl_minutes=snapshot["limits"]["human_ttl_minutes"])
            self.store.save_snapshot(db, snapshot, "run_waiting_for_human",
                                     {"interaction_id": interaction["interaction_id"],
                                      "kind": interaction["kind"]})
        return WAIT

    # ---------------- 分支 C：结束 ----------------

    def _finish(self, snapshot):
        # 校验是纯函数，放在事务外：它抛 ValueError 时不该连累已经记账的那些回合。
        # （agent.py:105-110 是在 try 里做的，那里只保存最终 run，没有这个问题。）
        try:
            verdict = validate_finish(snapshot["pending_tool_call"]["arguments"],
                                      snapshot["toolbox_state"]["seen"])
        except ValueError:
            snapshot["run"]["guardrail_blocks"] += 1
            raise
        with self.store.transaction() as db:
            snapshot["verdict"] = verdict
            snapshot["pending_tool_call"] = None
            snapshot["status"] = "completed"
            snapshot["stage"] = "done"
            self._finish_bookkeeping(snapshot)
            self.store.save_snapshot(db, snapshot, "run_completed",
                                     {"verdict": verdict["status"]})
        return DONE

    # ================= 收尾 =================

    def _terminate(self, snapshot, termination, reason=None, error_type=None, http_status=None):
        """与 agent.py:162-175 对齐：预算/无效响应/供应商异常都收敛成
        verdict=handoff()（业务上是 needs_review）。

        运行生命周期记 failed，而业务结论记在 verdict["status"] 里——这两个概念
        在 agent.py:176 被合成一个字段了，这里拆开，见 README 的说明。
        """
        run = snapshot["run"]
        run["termination"] = termination
        if reason is not None:
            run["budget_reason"] = reason
        if error_type is not None:
            run["error_type"] = error_type
        if http_status is not None:
            run["http_status"] = http_status
        with self.store.transaction() as db:
            snapshot["verdict"] = handoff()
            snapshot["status"] = "failed"
            snapshot["stage"] = "done"
            # pending 一并清掉：历史里会留下一个未答复的 tool_call，
            # 终态下这是允许的（见 snapshot.check_messages）。
            snapshot["pending_tool_call"] = None
            self._finish_bookkeeping(snapshot)
            self.store.save_snapshot(db, snapshot, "run_terminated",
                                     {"termination": termination, "reason": reason})
        return public(snapshot)

    def _finish_bookkeeping(self, snapshot):
        """把只在结束时才算得出的派生字段补进 run（agent.py:176-186 同款）。"""
        run = snapshot["run"]
        run["status"] = snapshot["verdict"]["status"]
        run["verdict"] = snapshot["verdict"]
        run["observed_evidence_ids"] = sorted(snapshot["toolbox_state"]["seen"])
        run["observed_tool_names"] = sorted(snapshot["tool_results"])
        run["duration_ms"] = round(snapshot["elapsed_s"] * 1000, 3)
        run["finished_at"] = utcnow()

    def _count_usage(self, snapshot, response):
        """照抄 agent.py:76-88，唯一改动是写进 snapshot["run"]。
        缺字段时必须置 usage_complete=False，否则 token 预算会静默失效。"""
        run = snapshot["run"]
        for key in ("input_tokens", "output_tokens"):
            count = response.get(key)
            if type(count) is int and count >= 0:
                run[key] += count
            else:
                run["usage_complete"] = False
        if run["cost_usd"] is not None and snapshot["config"]["provider"] != "baseline":
            cost = response.get("cost_usd")
            run["cost_usd"] = run["cost_usd"] + cost if cost is not None else None
        if type(response.get("cache_hit_tokens")) is int:
            run["cache_hit_tokens"] += response["cache_hit_tokens"]

    def _schemas(self, snapshot):
        return tools_module.SCHEMAS

    def _tool_names(self, snapshot):
        return set(tools_module.TOOL_NAMES)

    def _expire_if_due(self, interaction):
        """到点则把 interaction 与 run 一起置为 expired（runtime.py:254-263 同款）。

        返回已置为 expired 的那份快照；没到点返回 None。返回快照而不是 bool，
        是为了让调用方不必再读一次库——读到的会是被这次过期改写过的那份。

        pending_tool_call 保留不动：运行已经终态，它只用来标明"当时在问什么"。
        """
        if interaction["status"] != "pending":
            return None
        if datetime.now(timezone.utc) < datetime.fromisoformat(interaction["expires_at"]):
            return None
        with self.store.transaction() as db:
            snapshot = self.store.load_snapshot(interaction["run_id"], db)
            self.store.expire_interaction(db, interaction["interaction_id"])
            snapshot["status"] = "expired"
            snapshot["stage"] = "done"
            snapshot["verdict"] = handoff()
            self._finish_bookkeeping(snapshot)
            self.store.save_snapshot(db, snapshot, "run_expired", {})
        return snapshot
