"""长期记忆：工作空间 Markdown 文件 + 按需检索。

对应笔记第 5 节：

    会话结束后短期记忆会丢失，所以我们通常也会让 Agent 支持长期记忆。
    数据通常存在智能体运行环境之外，比如数据库、知识图谱或向量数据库中。

    还是以 OpenClaw 为例，它的长期记忆就是工作空间里的 Markdown 文件——
    MEMORY.md 存策展过的持久知识和偏好，memory/YYYY-MM-DD.md 按日期存追加式日志。
    Agent 需要回忆历史时，通过 memory_search 工具按需检索相关片段再拉进上下文，
    而不是把全部记忆都塞进去，这就是典型的 RAG 式长期记忆方案。

两个设计点值得单独说：

  1. **两类文件，两种写法。**
     MEMORY.md 是"策展"的：内容少、经过整理、长期有效（偏好、规范、教训）。
     memory/YYYY-MM-DD.md 是"追加"的：只写不改，按时间累积（今天发生了什么）。
     混在一起写，要么日志把策展内容淹了，要么策展时把日志改乱了。

  2. **按需检索，不是全量注入。**
     这是 RAG 式记忆和"把记忆全塞进 prompt"的分界。main.py 里有 token 数对比。

文件真实落在 p04_memory/workspace/ 下，跑完可以直接打开看。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from retrieval import Document, HybridRetriever, SearchHit

# 记忆类型（笔记 5.4.1）：情景记忆 / 语义记忆 / 程序记忆
EPISODIC = "情景记忆"  # 什么时候发生了什么
SEMANTIC = "语义记忆"  # 事实和知识
PROCEDURAL = "程序记忆"  # 怎么做一件事、有什么规矩


@dataclass
class MemoryRecord:
    text: str
    kind: str = SEMANTIC
    tags: tuple[str, ...] = ()
    created_at: str = ""

    def render(self) -> str:
        stamp = f"（{self.created_at}）" if self.created_at else ""
        tag_text = f" #{' #'.join(self.tags)}" if self.tags else ""
        return f"- [{self.kind}] {self.text}{stamp}{tag_text}"


class LongTermMemory:
    """工作空间里的 Markdown 记忆文件 + 混合检索索引。"""

    CURATED_FILE = "MEMORY.md"
    LOG_DIR = "memory"

    def __init__(self, workspace: Path, today: str = "2026-09-11"):
        self.workspace = Path(workspace)
        self.today = today
        self.retriever = HybridRetriever(vector_weight=0.7, bm25_weight=0.3)
        self._documents: list[Document] = []
        self.workspace.mkdir(parents=True, exist_ok=True)

    # -- 路径 -------------------------------------------------------------

    @property
    def curated_path(self) -> Path:
        return self.workspace / self.CURATED_FILE

    @property
    def log_path(self) -> Path:
        return self.workspace / self.LOG_DIR / f"{self.today}.md"

    # -- 写入 -------------------------------------------------------------

    def remember(self, record: MemoryRecord) -> Path:
        """策展内容写 MEMORY.md，其余按日期追加到日志。"""
        if record.kind in {SEMANTIC, PROCEDURAL} and "curated" in record.tags:
            path = self.curated_path
            self._ensure_header(path, "# 长期记忆（策展）\n\n整理过的持久知识和偏好。只写经过确认、长期有效的内容。\n")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(record.render() + "\n")
            return path

        path = self.log_path
        self._ensure_header(path, f"# {self.today} 追加日志\n\n只追加、不修改。记录当天发生了什么。\n")
        stamp = record.created_at or dt.datetime.now().strftime("%H:%M")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"- [{record.kind}] {stamp} {record.text}\n")
        return path

    @staticmethod
    def _ensure_header(path: Path, header: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(header + "\n", encoding="utf-8")

    def reset(self) -> None:
        """清空工作空间。demo 要可重复运行，所以每轮从干净状态开始。"""
        if self.curated_path.exists():
            self.curated_path.unlink()
        if self.log_path.exists():
            self.log_path.unlink()

    # -- 读取与索引 --------------------------------------------------------

    def load_documents(self) -> list[Document]:
        """把 Markdown 文件切成可检索的片段。

        真实系统里这一步会按标题/段落切块（chunking）；这里按行切，
        因为我们的记忆本来就是一行一条。
        """
        documents: list[Document] = []
        for path in [self.curated_path, self.log_path]:
            if not path.exists():
                continue
            source = path.name if path.parent == self.workspace else f"{path.parent.name}/{path.name}"
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                text = line.strip()
                # 只索引真正的记忆条目（"- " 开头的行）。
                # 标题和说明文字不是记忆，索引进去只会变成检索噪声。
                if not text.startswith("-"):
                    continue
                text = re.sub(r"^-\s*", "", text)
                documents.append(
                    Document(doc_id=f"{source}:{line_no}", text=text, source=source)
                )
        self._documents = documents
        self.retriever.index(documents)
        return documents

    def search(self, query: str, top_k: int = 3) -> list[SearchHit]:
        if not self._documents:
            self.load_documents()
        return self.retriever.search(query, top_k=top_k)

    def all_text(self) -> str:
        """把全部记忆拼成一段文本——用来和"按需检索"做 token 对比。"""
        return "\n".join(doc.text for doc in self._documents)

    # -- 给 Agent 用的工具 -------------------------------------------------

    def as_tool(self):
        """包装成 memory_search 工具，对应笔记里 OpenClaw 的 memory_search。"""

        def handler(query: str, top_k: int = 3) -> dict:
            hits = self.search(query, top_k=top_k)
            if not hits:
                from common.tools import ToolFailure

                raise ToolFailure(
                    "NO_MEMORY_HIT",
                    f"长期记忆里没有和 {query!r} 相关的内容，可以直接问用户",
                    retryable=False,
                )
            return {
                "query": query,
                "count": len(hits),
                "results": [{"source": hit.source, "text": hit.text} for hit in hits],
            }

        from common.tools import Tool

        return Tool(
            name="memory_search",
            description=(
                "在长期记忆里检索。需要回忆用户偏好、之前定过的规矩、"
                "或历史任务细节时调用。不确定记忆里有什么就先搜一次。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                    "top_k": {
                        "type": "integer",
                        "description": "返回条数",
                        "default": 3,
                        "minimum": 1,
                        "maximum": 5,
                    },
                },
                "required": ["query"],
            },
            handler=handler,
            annotations={"read_only": True, "idempotent": True},
        )
