"""Loop 工程的五块积木 + 一个记事本。对应笔记 13.1。

笔记 13.1.3 点题：**一套完整的循环 = 五个积木 + 一个记事本。**

    积木一  触发器    AI 什么时候开始干活
    积木二  worktree  独立工作目录，改砸了就删掉分支，不碰主分支
    积木三  Skill     项目说明书（团队约定、构建命令、踩过的坑）
    积木四  连接器    连数据库 / 内部系统 / 第三方服务（MCP）
    积木五  子 Agent  写审分离，一个写一个挑刺，可以用不同模型
    记事本  状态文件  跨会话的公共记忆

而笔记 13.1.2 有一句话是整章的分水岭：

    cron 跑的是一段固定脚本，第一步、第二步、第三步，永远一样。
    loop 跑的是一个 agent：它看当前状态，自己选下一步该干啥，
    干完检查结果，再决定要不要继续。它会重试、会回滚、会在搞不定的时候停下来。
    一句话，cron 是在执行脚本，loop 是在模仿一个工程师。

这个原型把这句话做成**可测量的**：同样的触发器、同样的工具、同样的轮数预算，
跑一段固定脚本 vs 跑一个会看状态的 loop，看谁能走出去。

笔记还有一句必须落进代码的话（13.1.4）：

    每个正经的 loop 都得配一个「心跳」机制——最大迭代次数、
    「无进展」检测、每天能花的 token/美元上限。
    预算就是架构的一部分。

所以这个文件里有三道硬刹车，它们各挡一种死法。
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any


# ==========================================================================
# 积木一：触发器
# ==========================================================================


@dataclass
class Trigger:
    """什么时候开始干活。

    笔记 13.1.2：「触发器可以是一个 PR 被打开、一次 CI 失败、一条 Slack 消息，
    或者你手动敲一句。」

    单独拎出来当一个积木，是因为它回答的是一个很容易被忽略的问题：
    **这段代码由谁唤醒？** 答案不同，整个 loop 的形态就不同——
    响应 CI 失败的 loop 可以很激进（错了重来就是），
    每天定时扫一遍收件箱的 loop 必须很保守（没人盯着）。
    """

    kind: str  # cron / ci_failure / pr_opened / manual
    detail: str = ""
    interval_minutes: int = 0

    def describe(self) -> str:
        if self.kind == "cron":
            return f"定时任务：每 {self.interval_minutes} 分钟醒一次"
        labels = {"ci_failure": "CI 失败", "pr_opened": "PR 被打开", "manual": "手动敲一句"}
        return f"{labels.get(self.kind, self.kind)}：{self.detail}"


# ==========================================================================
# 积木二：worktree
# ==========================================================================


@dataclass
class Workspace:
    """独立工作目录（worktree）。

    笔记：「很多人用 AI 写代码最怕它乱改文件、把好好的项目改崩。
    worktree 就是给每个 agent 一个独立分支，所有改动都在自己的沙盒里，不碰主分支。
    改砸了就删掉分支，主代码干干净净。相当于给每个员工一个独立工位，
    最后审核通过才合并回主线。」

    所以它在代码里要能回答两个问题：改了多少（diff_lines），
    以及**失败了怎么收拾**（discard）。后一个才是它存在的理由。
    """

    branch: str
    base_branch: str = "main"
    files: dict[str, str] = field(default_factory=dict)
    applied_fixes: list[str] = field(default_factory=list)
    merged: bool = False
    discarded: bool = False

    def apply(self, fix_id: str, patch: str) -> None:
        self.applied_fixes.append(fix_id)
        self.files["src/agent.py"] = self.files.get("src/agent.py", "") + patch

    def diff_lines(self) -> int:
        return sum(len(text.splitlines()) for text in self.files.values())

    def merge(self) -> str:
        self.merged = True
        return f"已合并回 {self.base_branch}"

    def discard(self) -> str:
        """改砸了就删掉分支——这是 worktree 存在的全部意义。"""
        self.discarded = True
        self.applied_fixes.clear()
        self.files.clear()
        return f"已删除分支 {self.branch}，{self.base_branch} 干干净净"

    def describe(self) -> str:
        if self.merged:
            state = "已合并"
        elif self.discarded:
            state = "已丢弃"
        else:
            state = "进行中"
        return f"{self.branch}（{state}，改动 {self.diff_lines()} 行）"


# ==========================================================================
# 积木三：Skill
# ==========================================================================


@dataclass
class Skill:
    """项目说明书。这个积木决定了 loop 能不能跑出复利。

    笔记 13.1.3：「Skill 就是把这些写进文件——构建命令是什么、代码规范是什么、
    哪些写法绝对禁止、上次出过什么事故——AI 每次启动先读一遍，就知道这个项目的规矩。
    说白了就是一本员工手册，谁来都先翻一遍，不用从头培训。」

    笔记还专门强调了不写 skill 的代价：
    「一个没有可复用 skill 的 loop，每跑一次都在『从零重新认识你的项目』，
    纯纯烧 token 重学你早就知道的东西；而一个 skill 写得好的 loop 会开始产生复利。」

    所以它在代码里的作用很具体：**装 loop 自己推不出来的知识**。
    通用做法能修的问题不需要手册；需要手册的，是那些「光看代码看不出来」的团队约定。
    """

    name: str
    rules: list[str] = field(default_factory=list)
    pitfalls: list[str] = field(default_factory=list)
    build_commands: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"# {self.name}", "", "## 团队约定"]
        lines += [f"- {rule}" for rule in self.rules]
        lines += ["", "## 踩过的坑"]
        lines += [f"- {pit}" for pit in self.pitfalls]
        lines += ["", "## 构建命令"]
        lines += [f"- {cmd}" for cmd in self.build_commands]
        return "\n".join(lines)

    def token_cost(self) -> int:
        """读一次手册要花多少 token。

        笔记说手册要「写得尽量密、又尽量小，别把上下文塞满」——
        这个函数就是那句话的量化：手册越大，每一轮都更贵。
        """
        return len(self.render()) // 2


# ==========================================================================
# 积木四：连接器
# ==========================================================================


@dataclass
class Connector:
    """连接器：让 loop 跳出单个文件夹。

    笔记：「过去 AI 只能看你当前打开的文件，有了连接器，它能连数据库、
    连公司内部系统、连第三方服务，自己去查生产环境数据、读 CRM 客户记录、拉监控告警。
    AI 就从一个『只能处理本地文件的工具』，变成了『能接入你整个工作流的节点』。」

    这里用一个内部规范库来体现：loop 发现本地上下文不够时，
    **主动去外部查**，而不是硬猜。
    """

    name: str
    registry: Any  # common.tools.ToolRegistry

    def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        return self.registry.call(tool_name, arguments)


# ==========================================================================
# 积木五：子 Agent（写审分离）
# ==========================================================================


@dataclass
class SubAgent:
    """子 Agent。笔记强调的不是「多几个 agent」，而是**写审必须分离**。

    「成熟的 loop 里至少有两个 agent，一个负责写、一个负责挑刺，
    甚至可以用不同的模型（比如一个写、另一个审）。指令不同、视角不同、
    模型都不同，才能真审出问题。道理很朴素——自己写的东西自己很难看出毛病，
    必须有第二双眼睛。」

    注意 model 这个字段在两个 SubAgent 上是**故意不同**的。
    第 13.2.4 节会说为什么：同一个模型 + 同一份上下文，
    多个 agent 只会「以工业化规模彼此赞同」。详见 graph.py 的演示 6。
    """

    name: str
    role: str  # writer / reviewer
    model: str
    instruction: str

    def describe(self) -> str:
        label = "写" if self.role == "writer" else "审"
        return f"{self.name}（{label}，模型 {self.model}）"


# ==========================================================================
# 记事本：状态文件
# ==========================================================================


class StateFile:
    """跨会话的公共记忆。笔记说这是「最容易被忽略，但它才是 loop 能长期跑下去的关键」。

    「现在绝大多数 AI 工作流每天都从零开始：昨天确认过的事实今天再查一遍，
    上周否掉的标题风格这周又冒出来，某个来源一直不靠谱但它下次还引用。
    为什么？因为记忆只活在单次对话里，对话一关就没了。」

    所以这里**真的往磁盘写一个文件**。你可以在 PyCharm 里打开它看长什么样，
    也可以在两次运行之间手动改它，观察 loop 的行为怎么变——
    这比读十遍笔记都直观。

    用 Markdown 而不是 JSON，是因为笔记说状态文件「可以是文档、表格、看板」，
    而且真实系统里它常常是给人看的：人能读、能改、能审查。
    代价是解析要自己写，就下面这十几行。
    """

    SECTIONS = {
        "已确认的事实": "facts",
        "踩过的坑": "pitfalls",
        "上次没解决的问题": "open_questions",
    }

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.writes = 0

    # -- 读 -----------------------------------------------------------------

    def load(self) -> dict[str, Any]:
        memory: dict[str, Any] = {
            "facts": [],
            "pitfalls": [],
            "open_questions": [],
            "runs": 0,
            "total_iterations": 0,
            "total_spent": 0.0,
        }
        if not self.path.exists():
            return memory

        current = ""
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("## "):
                title = line[3:].strip()
                current = self.SECTIONS.get(title, "" if title != "计数" else "counter")
                continue
            if not line.startswith("- "):
                continue
            item = line[2:].strip()
            if current in self.SECTIONS.values():
                memory[current].append(item)
            elif current == "counter" and ":" in item:
                key, _, value = item.partition(":")
                key, value = key.strip(), value.strip()
                if key == "运行次数":
                    memory["runs"] = int(value)
                elif key == "累计轮次":
                    memory["total_iterations"] = int(value)
                elif key == "累计花费":
                    memory["total_spent"] = float(value.lstrip("$"))
        return memory

    # -- 写 -----------------------------------------------------------------

    def save(self, memory: dict[str, Any]) -> None:
        self.writes += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.render(memory), encoding="utf-8")

    def render(self, memory: dict[str, Any]) -> str:
        lines = [
            "# Loop 状态文件",
            "",
            "> 这份文件是 loop 的公共记忆。每一轮结束写、每一轮开始读。",
            "> 它活在聊天框之外——所以对话关了、进程重启了，它还在。",
            "",
        ]
        for title, key in self.SECTIONS.items():
            lines.append(f"## {title}")
            items = memory.get(key) or []
            lines += [f"- {item}" for item in items] or ["- （空）"]
            lines.append("")
        lines += [
            "## 计数",
            f"- 运行次数: {memory.get('runs', 0)}",
            f"- 累计轮次: {memory.get('total_iterations', 0)}",
            f"- 累计花费: ${memory.get('total_spent', 0.0):.2f}",
            "",
        ]
        return "\n".join(lines)


# ==========================================================================
# 工作区里的问题，以及「可验证的目标」
# ==========================================================================


@dataclass
class Defect:
    """工作区里的一个问题。

    strategy 决定了 loop 能不能自己修掉它——这是整个原型里最重要的一个字段：

        generic       通用做法就能修（跑个 formatter 之类）
        skill_guided  **必须先读项目手册才知道该怎么修**（团队约定）
        needs_human   机器修不了，需要人做决定

    真实项目里这三类的比例，直接决定了这个 loop 值不值得做。
    """

    id: str
    check: str  # lint / test / review
    detail: str
    strategy: str
    fixed: bool = False
    fixed_by: str = ""


@dataclass
class CheckResult:
    name: str
    label: str
    passed: bool
    detail: str


class Verifier:
    """可验证的目标。笔记对它的措辞很重：

    「一定得有某种检查，否则你建的不是 loop，而是『一台非常自信的烧钱炉』。」

    所以这个类只干一件事：跑一遍检查，给出一个**不依赖 agent 自述**的结论。
    笔记 13.2.4 还说了一句相关的：「还得有比『agent 说自己做完了』更强的验证：
    跑测试、做类型检查、让评审 agent 去比对 diff 和规格。」

    第三项检查（review）是打分制而不是通过制，因为笔记里 RossMike 的规矩是
    「低于 4 分的代码，绝不上生产」——门槛可以调，但必须有。
    """

    def __init__(self, defects: list[Defect], review_threshold: float = 4.0):
        self.defects = defects
        self.review_threshold = review_threshold

    def run(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        for name, label in (("lint", "代码风格检查"), ("test", "测试套件")):
            broken = [d for d in self.defects if d.check == name and not d.fixed]
            results.append(
                CheckResult(
                    name,
                    label,
                    not broken,
                    "通过" if not broken else f"失败：{len(broken)} 个问题（{broken[0].detail}）",
                )
            )
        # review 是打分制：每个未修的问题扣 1 分
        unresolved = [d for d in self.defects if d.check == "review" and not d.fixed]
        score = max(0.0, 5.0 - len(unresolved))
        results.append(
            CheckResult(
                "review",
                "评审打分",
                score >= self.review_threshold,
                f"{score:.0f}/5（门槛 {self.review_threshold:.0f} 分）",
            )
        )
        return results

    @staticmethod
    def all_passed(checks: list[CheckResult]) -> bool:
        return all(check.passed for check in checks)

    @staticmethod
    def score(checks: list[CheckResult]) -> float:
        """把检查结果压成一个 0~1 的分数，给「无进展检测」用。"""
        if not checks:
            return 0.0
        return sum(1 for check in checks if check.passed) / len(checks)


# ==========================================================================
# 运行状态与动作
# ==========================================================================


@dataclass
class Action:
    kind: str  # read_skill / patch / escalate / run_script / check
    reason: str
    defect: str = ""


@dataclass
class RunState:
    """一次 loop 运行的全部可变状态。

    断点打在这里最划算：一个对象看完，就知道 loop 现在走到哪了、为什么走不下去。
    """

    task: str
    iteration: int = 0
    skill_read: bool = False
    checks: list[CheckResult] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    spent_usd: float = 0.0
    last_score: float = -1.0
    stagnant: int = 0
    escalated: str = ""
    tokens: int = 0
    #: 从状态文件里读出来的「踩过的坑」。执行者看得到它，所以能跳过试错。
    known_pitfalls: list[str] = field(default_factory=list)

    def failing(self, defects: list[Defect]) -> list[Defect]:
        return [d for d in defects if not d.fixed]

    @property
    def score(self) -> float:
        return Verifier.score(self.checks)

    def record(self, action: Action, observation: str) -> None:
        self.history.append(
            {
                "轮次": self.iteration,
                "检查": self.check_summary(),
                "决定": self.action_summary(action),
                "结果": observation,
                "分数": f"{self.score:.0%}",
                # 完整文本留给调试器，表格里放不下
                "detail": {
                    "observation": self.observation(),
                    "reason": action.reason,
                    "result": observation,
                },
            }
        )

    def observation(self) -> str:
        if not self.checks:
            return "（还没跑过检查）"
        return "；".join(f"{c.label}:{c.detail}" for c in self.checks)

    def check_summary(self) -> str:
        """压成表格里放得下的一行。"""
        if not self.checks:
            return "（未跑）"
        parts = []
        for check in self.checks:
            if check.name == "review":
                score = check.detail.split("/")[0]
                parts.append(f"review {score}/5")
            else:
                parts.append(f"{check.name}{'✓' if check.passed else '✗'}")
        return " ".join(parts)

    @staticmethod
    def action_summary(action: Action) -> str:
        labels = {
            "read_skill": "读项目手册",
            "patch": "修",
            "escalate": "转人工",
            "run_script": "跑固定脚本",
            "check": "复查",
        }
        base = labels.get(action.kind, action.kind)
        return f"{base} {action.defect}" if action.defect else base


# ==========================================================================
# 心跳：三道硬刹车
# ==========================================================================


@dataclass
class Heartbeat:
    """心跳。笔记 13.1.4——「预算就是架构的一部分」。

    「每个正经的 loop 都得配一个『心跳』机制——最大迭代次数、
    『无进展』检测、每天能花的 token/美元上限。」

    三道刹车各挡一种死法：

        budget          挡烧钱：不管有没有产出都在烧 token
        no_progress     挡原地打转：分数一动不动，再跑一万轮也一样
        max_iterations  挡「永远差一点点」：看着快成了，就是不成

    顺序也是设计的一部分：预算排第一，因为它是唯一一道**跟任务进展无关**的刹车。
    任务可能一直有进展，但钱烧完了就是烧完了。
    """

    max_iterations: int = 10
    no_progress_limit: int = 2
    budget_usd: float = 1.0
    cost_per_iteration: float = 0.06
    tokens_per_iteration: int = 1200

    def spend(self, state: RunState) -> None:
        state.spent_usd += self.cost_per_iteration
        state.tokens += self.tokens_per_iteration

    def check(self, state: RunState) -> str:
        """返回停车原因；空字符串表示可以继续。

        预算这一条问的是「**再跑一轮**会不会超」，不是「已经超了没」。
        差别就在一次迭代的钱上：事后算账的写法会先把钱花掉，再告诉你超了。
        这是「预算就是架构的一部分」在代码里最具体的一处体现。
        """
        if state.spent_usd + self.cost_per_iteration > self.budget_usd:
            return f"预算不够再跑一轮（已花 ${state.spent_usd:.2f}）"
        if state.stagnant >= self.no_progress_limit:
            return f"无进展：连续 {state.stagnant} 轮卡住"
        if state.iteration >= self.max_iterations:
            return f"到达最大迭代次数 {self.max_iterations}"
        return ""


# ==========================================================================
# 两个执行者：一个会看状态，一个不会
# ==========================================================================


def pitfall_id(entry: str) -> str:
    """状态文件里的坑写成「defect-id：说明」，取出前面的 id。

    这点小事值得单独一个函数，是因为它踩过一次：
    最早写成 `defect.id in state.known_pitfalls`，而列表里存的是
    「lint-import-order：通用做法修不掉它……」整句，于是永远匹配不上，
    「读状态文件」那一组省下的轮数就成了 0。
    状态文件是**给人读的**文本，所以读取方必须知道怎么解析它——
    这是用 Markdown 当存储格式要付的代价。
    """
    return entry.split("：", 1)[0].strip()


class Worker:
    """loop 里的执行者。它不是脚本——它**看状态决定下一步**。

    这是整个原型最关键的一个类。跟下面 CronRunner 对比着读：
    同样的触发器、同样的工具、同样的轮数预算，差别只有一处——
    它每一轮先看「上一轮发生了什么」，再决定这一轮干什么。

    笔记里那个 `/goal` 的描述说的就是这个：
    「一个长期运行的任务，不用你一遍遍催，harness 自己会一路干到目标达成为止。」
    """

    def __init__(self, name: str, skill: Skill, connector: Connector | None = None):
        self.name = name
        self.skill = skill
        self.connector = connector
        self.skill_lookups = 0

    def decide(self, state: RunState, defects: list[Defect]) -> Action:
        failing = state.failing(defects)
        known_ids = {pitfall_id(entry) for entry in state.known_pitfalls}

        # 情况零：状态文件里记过这个坑——不用再试错，直接按上次记下的修法处理。
        # 这就是笔记说的「它能接着上次的进度往下走，而不是永远重启」。
        for defect in failing:
            if defect.id in known_ids:
                return Action(
                    "patch",
                    f"状态文件里记过这个坑，直接按记下的修法处理，省掉试错",
                    defect=defect.id,
                )

        # 情况一：有必须先读手册才知道怎么修的问题
        if any(d.strategy == "skill_guided" for d in failing) and not state.skill_read:
            return Action(
                "read_skill",
                "通用做法修不掉这个问题，得先知道团队约定是什么——去读项目手册",
            )

        # 情况二：能自己修的先修，一次修一个，修完再看
        for defect in failing:
            if defect.strategy in ("generic", "skill_guided"):
                return Action("patch", f"修 {defect.id}：{defect.detail}", defect=defect.id)

        # 情况三：剩下的都是机器修不了的，停下来交给人
        if any(d.strategy == "needs_human" for d in failing):
            target = next(d for d in failing if d.strategy == "needs_human")
            return Action("escalate", f"{target.id} 不是机器能定的，停下来交给人", defect=target.id)

        return Action("check", "看起来都好了，再跑一遍检查确认")


class CronRunner:
    """cron 版的「执行者」：每一轮跑同一段固定脚本。

    第一步、第二步、第三步，永远一样（笔记 13.1.2 的原话）。

    它没有做错什么。它只是**不会去看上一轮发生了什么**——
    上一轮 lint 报的那个错，这一轮的它一无所知。
    所以它每一轮都从头开始，在同一个地方撞同一堵墙。
    """

    SCRIPT = ["跑 formatter", "跑通用修复", "跑测试"]

    def __init__(self, name: str = "cron"):
        self.name = name

    def decide(self, state: RunState, defects: list[Defect]) -> Action:
        return Action("run_script", "执行固定脚本：" + " → ".join(self.SCRIPT))


# ==========================================================================
# Loop 本体
# ==========================================================================


@dataclass
class LoopReport:
    name: str
    task: str
    state: RunState
    brake: str
    passed: bool
    outcome: str

    def describe(self) -> str:
        if self.passed:
            return f"目标达成（{self.state.iteration} 轮，${self.state.spent_usd:.2f}）"
        return f"停下：{self.brake}"


class Loop:
    """把五块积木和一个记事本拼起来。

    每一轮的形状固定，就是笔记 13.1.2 描述的那个循环：

        读记事本 → 看当前状态 → 决定下一步 → 执行 → 检查 → 写记事本 → 问心跳

    注意「决定下一步」这一步是真的在执行者的代码里做的（Worker.decide），
    不是一个 if-else 常量。loop 和 cron 的全部差别就在这一行。
    """

    def __init__(
        self,
        name: str,
        trigger: Trigger,
        workspace: Workspace,
        skill: Skill,
        verifier: Verifier,
        defects: list[Defect],
        heartbeat: Heartbeat,
        state_file: StateFile,
        worker: Worker | CronRunner,
        subagents: list[SubAgent] | None = None,
        connectors: list[Connector] | None = None,
    ):
        self.name = name
        self.trigger = trigger
        self.workspace = workspace
        self.skill = skill
        self.verifier = verifier
        self.defects = defects
        self.heartbeat = heartbeat
        self.state_file = state_file
        self.worker = worker
        self.subagents = subagents or []
        self.connectors = connectors or []

    # -- 一块积木一个方法，方便单独打断点看 ---------------------------------

    def read_state_file(self) -> dict[str, Any]:
        """记事本：**每次启动第一件事就是读它**（这是笔记的原话）。"""
        return self.state_file.load()

    def read_skill(self, state: RunState) -> str:
        """积木三：读项目手册。真实系统里这一步是往上下文里塞一份文件。"""
        state.skill_read = True
        state.tokens += self.skill.token_cost()
        if state.skill_read and isinstance(self.worker, Worker):
            self.worker.skill_lookups += 1
        return self.skill.render()

    def use_connector(self, defect: Defect) -> str:
        """积木四：本地上下文不够时，主动去外部查。"""
        for connector in self.connectors:
            result = connector.call("search_knowledge", {"query": defect.id})
            if result.ok:
                return result.content.replace("\n", " ")
        return "（连接器没查到）"

    def patch(self, defect: Defect, state: RunState) -> str:
        """积木二：所有改动都发生在 worktree 里，不碰主分支。"""
        if defect.strategy == "needs_human":
            # 重要：修不了就说修不了，**不能假装修好了**。
            # 笔记 17.3 那条「绝不伪造成功」在这里同样成立——
            # 一个会假装成功的 loop，比一个会停下来的 loop 危险得多。
            return "改了一版，还是失败"

        if defect.strategy == "generic":
            reviewer = "通用修复"
        else:
            # 读手册之后再修，才修得对。真实系统里这一步就是把 skill 里的
            # 那条规定塞进上下文，让模型照着改。
            reviewer = "按团队约定"
            state.tokens += 400
        defect.fixed = True
        defect.fixed_by = self.worker.name
        self.workspace.apply(defect.id, f"\n# 修复 {defect.id}（{reviewer}）\n")
        return f"{reviewer}修好"

    # -- 主循环 -------------------------------------------------------------

    def run(self, task: str, use_memory: bool = True) -> LoopReport:
        memory = self.read_state_file() if use_memory else {
            "facts": [], "pitfalls": [], "open_questions": [], "runs": 0,
            "total_iterations": 0, "total_spent": 0.0,
        }

        # 状态文件里记着「踩过的坑」——loop 一上来就避开它们，
        # 而不是自己再撞一遍。这就是笔记说的「能接着上次的进度往下走，而不是永远重启」。
        state = RunState(task=task, known_pitfalls=list(memory.get("pitfalls") or []))

        brake = ""
        while True:
            # 每轮开始**先问心跳**：还有预算吗？还在进展吗？轮次还没用完吧？
            # 放在花钱之前问，而不是事后算账——这是「预算就是架构的一部分」的兑现方式。
            brake = self.heartbeat.check(state)
            if brake:
                break

            state.iteration += 1
            self.heartbeat.spend(state)

            action = self.worker.decide(state, self.defects)

            # ---- 执行 ----
            if action.kind == "read_skill":
                self.read_skill(state)
                observation = f"读完手册（{len(self.skill.rules)} 条约定）"
            elif action.kind == "patch":
                defect = next(d for d in self.defects if d.id == action.defect)
                known_ids = {pitfall_id(entry) for entry in state.known_pitfalls}
                if defect.id in known_ids and defect.strategy == "skill_guided":
                    # 状态文件里记过这个坑：上次已经确认通用做法修不掉它，
                    # 这次直接按约定修，省掉「先试错、失败、再读手册」那一整轮
                    defect.fixed = True
                    defect.fixed_by = self.worker.name
                    self.workspace.apply(defect.id, f"\n# 修复 {defect.id}（凭状态文件里记下的坑）\n")
                    observation = "凭状态文件里的坑直接修好"
                else:
                    observation = self.patch(defect, state)
                    if defect.strategy == "skill_guided" and defect.fixed:
                        point = f"{defect.id}：通用做法修不掉它，必须先读项目手册按团队约定来"
                        if point not in memory.setdefault("pitfalls", []):
                            memory["pitfalls"].append(point)
            elif action.kind == "escalate":
                defect = next(d for d in self.defects if d.id == action.defect)
                state.escalated = defect.detail
                questions = memory.setdefault("open_questions", [])
                if defect.detail not in questions:
                    questions.append(defect.detail)
                observation = "已转人工，等回复"
                brake = f"转人工（{defect.id}）"
                state.checks = self.verifier.run()
                state.record(action, observation)
                break
            elif action.kind == "run_script":
                # cron 的固定脚本：只会做通用修复，碰都不碰需要手册的那些
                fixed_now = []
                for defect in self.defects:
                    if not defect.fixed and defect.strategy == "generic":
                        defect.fixed = True
                        defect.fixed_by = self.worker.name
                        self.workspace.apply(defect.id, f"\n# 通用修复 {defect.id}\n")
                        fixed_now.append(defect.id)
                observation = f"通用修复处理了 {len(fixed_now)} 个"
            else:
                observation = "复查一遍"

            # ---- 检查（可验证的目标）----
            state.checks = self.verifier.run()

            # ---- 无进展检测：分数和上一轮一样就计一次 -----
            current = state.score
            if abs(current - state.last_score) < 1e-9:
                state.stagnant += 1
            else:
                state.stagnant = 0
                state.last_score = current

            state.record(action, observation)

            # ---- 记事本：每轮结束写一次 ----
            memory["total_iterations"] = memory.get("total_iterations", 0) + 1
            memory["total_spent"] = round(memory.get("total_spent", 0.0) + self.heartbeat.cost_per_iteration, 4)
            self.state_file.save(memory)

            # ---- 目标达成没有 ----
            if self.verifier.all_passed(state.checks):
                memory["runs"] = memory.get("runs", 0) + 1
                memory.setdefault("facts", [])
                fact = f"{task}：{state.iteration} 轮完成，全部检查通过"
                if fact not in memory["facts"]:
                    memory["facts"].append(fact)
                self.state_file.save(memory)
                if not self.workspace.discarded:
                    self.workspace.merge()
                return LoopReport(self.name, task, state, "", True, "目标达成")

        # ---- 停下来，把结论写进记事本 ----
        memory["runs"] = memory.get("runs", 0) + 1
        memory.setdefault("facts", [])
        memory["facts"].append(f"{task}：第 {state.iteration} 轮停下（{brake}）")
        self.state_file.save(memory)

        if state.escalated:
            outcome = "已转人工"
        elif any(not d.fixed for d in self.defects):
            outcome = "未完成"
        else:
            outcome = "完成"
        return LoopReport(self.name, task, state, brake, False, outcome)
