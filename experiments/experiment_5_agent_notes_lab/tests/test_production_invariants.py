from __future__ import annotations

import importlib.util
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ResilienceTests(unittest.TestCase):
    def test_half_open_allows_one_probe(self) -> None:
        from common.resilience import CircuitBreaker, CircuitOpenError, CircuitState

        now = [0.0]
        breaker = CircuitBreaker("test", failure_threshold=1, recovery_s=5, clock=lambda: now[0])
        breaker.before_call()
        breaker.record_failure(RuntimeError("down"))
        self.assertEqual(breaker.state, CircuitState.OPEN)
        with self.assertRaises(CircuitOpenError):
            breaker.before_call()
        now[0] = 6.0
        breaker.before_call()
        with self.assertRaises(CircuitOpenError):
            breaker.before_call()
        breaker.record_success()
        self.assertEqual(breaker.state, CircuitState.CLOSED)

    def test_retry_uses_transient_failures_only(self) -> None:
        from common.resilience import RetryPolicy, retry_call

        attempts = [0]

        def operation() -> str:
            attempts[0] += 1
            if attempts[0] < 3:
                raise TimeoutError("temporary")
            return "ok"

        self.assertEqual(retry_call(operation, policy=RetryPolicy(max_attempts=3, total_timeout_s=1, jitter_ratio=0), sleep=lambda _: None), "ok")
        self.assertEqual(attempts[0], 3)


class BoundaryTests(unittest.TestCase):
    def test_idempotency_is_first_write_wins(self) -> None:
        from common.storage import IdempotencyStore

        with tempfile.TemporaryDirectory() as directory:
            store = IdempotencyStore(Path(directory) / "ids.jsonl")
            self.assertEqual(store.put_if_absent("request-1", {"value": 1}), ({"value": 1}, True))
            self.assertEqual(store.put_if_absent("request-1", {"value": 2}), ({"value": 1}, False))
            reloaded = IdempotencyStore(Path(directory) / "ids.jsonl")
            self.assertEqual(reloaded.get("request-1"), {"value": 1})

    def test_idempotency_instances_reload_before_claim(self) -> None:
        from common.storage import IdempotencyStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ids.jsonl"
            first = IdempotencyStore(path)
            second = IdempotencyStore(path)
            self.assertEqual(first.begin("request-1", request_hash="hash"), ("claimed", None))
            self.assertEqual(second.begin("request-1", request_hash="hash"), ("in_progress", None))

    def test_write_action_requires_approval(self) -> None:
        from common.errors import AgentError, ErrorKind
        from common.security import ActionDescriptor, ApprovalToken, ToolPolicy

        policy = ToolPolicy([ActionDescriptor("book", read_only=False, destructive=True)])
        with self.assertRaises(AgentError) as caught:
            policy.authorize("book")
        self.assertEqual(caught.exception.kind, ErrorKind.APPROVAL_REQUIRED)
        token = ApprovalToken.issue("signing-secret", subject="agent", action="book", ttl_s=60)
        signed_policy = ToolPolicy([ActionDescriptor("book", read_only=False, destructive=True)], approval_secret="signing-secret")
        signed_policy.authorize("book", approval_token=token)
        self.assertFalse(ApprovalToken.verify(token, "signing-secret", subject="other-agent", action="book"))


class ModuleTests(unittest.TestCase):
    def test_all_production_modules_import(self) -> None:
        modules = [
            "p01_react_loop/production.py",
            "p02_function_calling/production.py",
            "p03_robustness/production.py",
            "p04_memory/production.py",
            "p05_multi_agent/production.py",
            "p06_mcp/production.py",
            "p07_evaluation/production.py",
            "p08_loop_graph/production.py",
            "p09_hallucination/production.py",
            "p10_a2a/production.py",
            "p11_openclaw/production.py",
            "p12_hermes_self_improvement/production.py",
            "p13_model_parameters/production.py",
            "p14_pi_harness/production.py",
            "p15_agent_scenario/production.py",
            "p16_agentic_boundary/production.py",
            "p17_framework_selection/production.py",
            "p18_online_ops/production.py",
            "p19_deepseek_harness/production.py",
        ]
        for index, relative in enumerate(modules):
            with self.subTest(relative=relative):
                load_module(f"production_{index}", relative)

    def test_session_tree_and_plugin_dependencies(self) -> None:
        session = load_module("session_prod", "p14_pi_harness/production.py")
        plugins = load_module("plugin_prod", "p19_deepseek_harness/production.py")
        with tempfile.TemporaryDirectory() as directory:
            tree = session.ProductionSessionTree(Path(directory) / "session.jsonl")
            tree.append(session.SessionEvent("root", None, "user", "hello"))
            tree.branch("root", "answer", "assistant", "world")
            self.assertEqual([item.event_id for item in tree.lineage("answer")], ["root", "answer"])
            reloaded_tree = session.ProductionSessionTree(Path(directory) / "session.jsonl")
            self.assertEqual([item.event_id for item in reloaded_tree.lineage("answer")], ["root", "answer"])
            with self.assertRaises(Exception):
                tree.append(session.SessionEvent("bad", "missing", "assistant", "x"))

            runtime = plugins.ProductionPluginRuntime(Path(directory) / "plugins.jsonl")
            runtime.load(plugins.PluginSpec("core", "1.0"))
            runtime.load(plugins.PluginSpec("agent", "1.0", ("core",), frozenset({"loop"})))
            with self.assertRaises(Exception):
                runtime.unload("core")
            reloaded_before_unload = plugins.ProductionPluginRuntime(Path(directory) / "plugins.jsonl")
            self.assertEqual(reloaded_before_unload.plugins["agent"].spec.dependencies, ("core",))
            self.assertEqual(reloaded_before_unload.plugins["agent"].spec.capabilities, frozenset({"loop"}))
            with self.assertRaises(Exception):
                reloaded_before_unload.unload("core")
            runtime.unload("agent")
            runtime.unload("core")

            reloaded_runtime = plugins.ProductionPluginRuntime(Path(directory) / "plugins.jsonl")
            self.assertEqual(reloaded_runtime.plugins, {})

    def test_learning_store_replays_approved_skill(self) -> None:
        learning = load_module("learning_prod", "p12_hermes_self_improvement/production.py")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "learning.jsonl"
            store = learning.GovernedLearningStore(path)
            proposal = learning.SkillProposal("skill-1", "标题", "步骤", ["evidence"], 0.9)
            store.propose(proposal)
            store.approve("skill-1", reviewer="reviewer")
            reloaded = learning.GovernedLearningStore(path)
            self.assertEqual(reloaded.skills["skill-1"].status, "approved")

    def test_learning_default_does_not_self_approve_and_sampling_state_is_explicit(self) -> None:
        learning = load_module("learning_no_auto_approve", "p12_hermes_self_improvement/production.py")
        sampling = load_module("sampling_state", "p13_model_parameters/production.py")
        with tempfile.TemporaryDirectory() as directory:
            store = learning.GovernedLearningStore(Path(directory) / "learning.jsonl")
            proposal = learning.ProductionReflectionLoop(store).learn("task", "outcome", skill_id="skill")
            self.assertIsNotNone(proposal)
            self.assertEqual(store.skills["skill"].status, "proposed")
        sampler = sampling.ProductionSampler(sampling.SamplingConfig(temperature=1, top_p=0.5, seed=3))
        first = sampler.choose([("high", 0.6), ("low", 0.4)])
        second = sampler.choose([("high", 0.6), ("low", 0.4)])
        self.assertIn(first, {"high", "low"})
        self.assertIn(second, {"high", "low"})

    def test_evaluation_fails_closed(self) -> None:
        evaluation = load_module("evaluation_prod", "p07_evaluation/production.py")
        evaluator = evaluation.ProductionEvaluator(lambda text: "wrong", lambda case, output: 0.0, min_pass_rate=1.0)
        report = evaluator.run([evaluation.EvalCase("case-1", "input", "expected")])
        self.assertFalse(report.gate_passed)
        self.assertEqual(report.pass_rate, 0.0)

    def test_react_and_function_calling_production_paths(self) -> None:
        from common.config import RuntimeConfig
        from common.llm import MockLLM, ToolCall
        from common.tools import ToolRegistry, make_calculator_tool

        registry = ToolRegistry("test")
        registry.register(make_calculator_tool())
        react = load_module("react_prod", "p01_react_loop/production.py")
        state = {"calls": 0}

        def policy(messages, tools):
            state["calls"] += 1
            return ToolCall("calculator", {"expression": "2+2"}) if state["calls"] == 1 else "4"

        result = react.ProductionReActRunner(MockLLM(policy), registry, config=RuntimeConfig(max_steps=3, max_tool_calls=3)).run("calculate")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.tool_calls, 1)

        caller = load_module("function_prod", "p02_function_calling/production.py")
        model = MockLLM(lambda messages, tools: ToolCall("calculator", {"expression": "3*3"}))
        with tempfile.TemporaryDirectory() as directory:
            outcome = caller.ProductionFunctionCaller(model, registry, config=RuntimeConfig(retry_attempts=1), idempotency_path=Path(directory) / "ids.jsonl").call(registry.schemas()[0], "calculate", idempotency_key="call-1")
        self.assertTrue(outcome.ok)
        self.assertIn("value: 9", outcome.content)

    def test_gateway_fails_over_and_grounder_abstains(self) -> None:
        from common.config import RuntimeConfig

        gateway = load_module("gateway_prod", "p03_robustness/production.py")

        class Down:
            def chat(self, messages, tools):
                raise TimeoutError("down")

        class Up:
            def chat(self, messages, tools):
                return "fallback-ok"

        result = gateway.ProductionModelGateway([("primary", Down()), ("backup", Up())], config=RuntimeConfig(retry_attempts=1)).chat([], [])
        self.assertEqual(result, "fallback-ok")
        grounder = load_module("grounder_prod", "p09_hallucination/production.py")
        answer = grounder.ProductionGrounder().answer("火星上有一座机场。", [grounder.Evidence("doc-1", "北京南站有机场大巴。")])
        self.assertFalse(answer.grounded)

    def test_mcp_timeout_does_not_reexecute_same_request(self) -> None:
        from common.config import RuntimeConfig
        from common.tools import ToolRegistry

        mcp = load_module("mcp_prod", "p06_mcp/production.py")
        calls = [0]
        started = threading.Event()

        def side_effect() -> str:
            started.set()
            time.sleep(0.06)
            calls[0] += 1
            return "done"

        registry = ToolRegistry("mcp-test")
        registry.add("write", "write", {"type": "object", "properties": {}}, side_effect, annotations={"read_only": False, "destructive": True, "open_world": True})
        with tempfile.TemporaryDirectory() as directory:
            host = mcp.ProductionMCPHost(registry, expected_token="secret", config=RuntimeConfig(request_timeout_s=0.01, retry_attempts=3, require_approval_for_writes=False), idempotency_path=Path(directory) / "ids.jsonl")
            request = mcp.RpcRequest("same-request", "tools/call", {"name": "write", "arguments": {}}, "secret")
            first = host.handle(request)
            self.assertFalse(first.ok)
            self.assertTrue(started.wait(0.1))
            second = host.handle(request)
            self.assertFalse(second.ok)
            self.assertEqual(second.error["code"], "request_in_progress")
            time.sleep(0.08)
            self.assertEqual(calls[0], 1)
            third = host.handle(request)
            self.assertTrue(third.ok)
            self.assertTrue(third.replayed)

    def test_mcp_retries_retryable_tool_result_inside_operation(self) -> None:
        from common.config import RuntimeConfig
        from common.tools import ToolFailure, ToolRegistry

        mcp = load_module("mcp_retry_prod", "p06_mcp/production.py")
        calls = [0]

        def flaky_read() -> str:
            calls[0] += 1
            if calls[0] == 1:
                raise ToolFailure("temporary", "temporary", retryable=True)
            return "ok"

        registry = ToolRegistry("mcp-retry-test")
        registry.add("read", "read", {"type": "object", "properties": {}}, flaky_read, annotations={"read_only": True})
        with tempfile.TemporaryDirectory() as directory:
            host = mcp.ProductionMCPHost(registry, expected_token="secret", config=RuntimeConfig(retry_attempts=2, retry_total_timeout_s=1), idempotency_path=Path(directory) / "ids.jsonl")
            response = host.handle(mcp.RpcRequest("retry-request", "tools/call", {"name": "read", "arguments": {}}, "secret"))
            self.assertTrue(response.ok)
            self.assertEqual(calls[0], 2)

    def test_openclaw_timeout_does_not_reexecute_same_request(self) -> None:
        from common.errors import AgentError
        from common.security import ActionDescriptor

        openclaw = load_module("openclaw_prod", "p11_openclaw/production.py")
        calls = [0]
        started = threading.Event()

        def side_effect() -> str:
            started.set()
            time.sleep(0.06)
            calls[0] += 1
            return "done"

        with tempfile.TemporaryDirectory() as directory:
            runtime = openclaw.ProductionOpenClawRuntime(directory, tool_timeout_s=0.01, approval_verifier=lambda action, token: token == "approved")
            runtime.register_tool("write", side_effect, ActionDescriptor("write", read_only=False, destructive=True, external_side_effect=True))
            with self.assertRaises(AgentError) as first_error:
                runtime.call_tool("session", "write", {}, request_id="same-request", approval_token="approved")
            self.assertEqual(first_error.exception.code, "timeout")
            self.assertTrue(started.wait(0.1))
            with self.assertRaises(AgentError) as caught:
                runtime.call_tool("session", "write", {}, request_id="same-request", approval_token="approved")
            self.assertEqual(caught.exception.code, "request_in_progress")
            time.sleep(0.08)
            self.assertEqual(calls[0], 1)
            self.assertEqual(runtime.call_tool("session", "write", {}, request_id="same-request", approval_token="approved"), "done")

    def test_memory_sampling_and_graph_recovery(self) -> None:
        memory = load_module("memory_prod", "p04_memory/production.py")
        sampling = load_module("sampling_prod", "p13_model_parameters/production.py")
        graph = load_module("graph_prod", "p08_loop_graph/production.py")
        with tempfile.TemporaryDirectory() as directory:
            record = memory.MemoryRecord("policy", "退票规则是开车前可退", importance=3)
            context = memory.ContextAssembler(max_tokens=100).assemble("退票规则", [record], [])
            self.assertEqual(context[-1]["content"], "退票规则")
            sampler = sampling.ProductionSampler(sampling.SamplingConfig(temperature=0, max_tokens=2))
            self.assertEqual(sampler.choose([("long", 0.2), ("best", 0.8)]), "be")
            nodes = [graph.Node("start", lambda ctx: "done"), graph.Node("done", lambda ctx: "done", terminal=True)]
            machine = graph.ProductionGraph(nodes, {"start": {"done"}, "done": set()}, checkpoint_path=Path(directory) / "graph.jsonl")
            machine.start("run-1", "start", {})
            restored = machine.restore("run-1")
            self.assertEqual(restored.state, "done")

    def test_supervisor_has_per_worker_deadline(self) -> None:
        from common.config import RuntimeConfig

        supervisor = load_module("supervisor_prod", "p05_multi_agent/production.py")
        with tempfile.TemporaryDirectory():
            result = supervisor.ProductionSupervisor(
                [supervisor.WorkerSpec("slow", lambda task: (time.sleep(0.05), "done")[1])],
                config=RuntimeConfig(request_timeout_s=0.01, retry_attempts=1),
            ).run("task")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.results[0].error["code"], "timeout")

    def test_a2a_client_verifies_bearer_token_before_submit(self) -> None:
        a2a = load_module("a2a_prod", "p10_a2a/production.py")
        sys.path.insert(0, str(ROOT / "p10_a2a"))
        try:
            class Card:
                security = [{"bearer": {}}]

            class Task:
                id = "remote-1"
                state = "submitted"

            class Agent:
                def __init__(self):
                    self.tokens = []

                def authenticate(self, token):
                    self.tokens.append(token)
                    return token == "valid"

                def card(self):
                    return Card()

                def send_task(self, message, idempotent=False):
                    return Task()

            class Directory:
                def __init__(self, agent):
                    self.agent = agent

                def resolve_by_id(self, agent_id):
                    return self.agent

            agent = Agent()
            with tempfile.TemporaryDirectory() as directory:
                invalid = a2a.ProductionA2AClient(Directory(agent), bearer_token="invalid", idempotency_path=Path(directory) / "invalid.jsonl")
                with self.assertRaises(Exception):
                    invalid.submit("agent-1", "task", message_id="m-1")
                valid = a2a.ProductionA2AClient(Directory(agent), bearer_token="valid", idempotency_path=Path(directory) / "valid.jsonl")
                self.assertEqual(valid.submit("agent-1", "task", message_id="m-2").task_id, "remote-1")
            self.assertEqual(agent.tokens, ["invalid", "valid"])
        finally:
            sys.path.remove(str(ROOT / "p10_a2a"))


if __name__ == "__main__":
    unittest.main()
