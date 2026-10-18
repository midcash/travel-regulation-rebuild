"""Pi-Agent 的极简 Harness：JSONL、append-only、parentId 会话树与压缩。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Entry:
    id: str
    parent_id: str | None
    type: str
    content: str = ""
    data: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parentId": self.parent_id,
            "type": self.type,
            "content": self.content,
            **(self.data or {}),
        }


class AppendOnlySession:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entries: dict[str, Entry] = {}
        self.leaf_id: str | None = None
        self._counter = 0

    def _new_id(self) -> str:
        self._counter += 1
        return f"e{self._counter:03d}"

    def append(self, type_: str, content: str = "", parent_id: str | None = None, **data: Any) -> Entry:
        parent_id = self.leaf_id if parent_id is None else parent_id
        entry = Entry(self._new_id(), parent_id, type_, content, data)
        self.entries[entry.id] = entry
        self.leaf_id = entry.id
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.as_dict(), ensure_ascii=False) + "\n")
        return entry

    def move_leaf(self, target_id: str) -> Entry:
        if target_id not in self.entries:
            raise KeyError(target_id)
        return self.append("position", target_id=target_id)

    def current_path(self, leaf_id: str | None = None) -> list[Entry]:
        current = leaf_id or self.leaf_id
        path: list[Entry] = []
        while current is not None:
            entry = self.entries[current]
            if entry.type == "position":
                # position 节点只是把“当前位置”指向另一个历史节点；
                # 它的 parent_id 是追加时的物理位置，不能当作上下文父节点。
                current = str(entry.data["target_id"])
                continue
            path.append(entry)
            current = entry.parent_id
        return list(reversed(path))

    def compact(self, summary: str, first_kept_entry_id: str) -> Entry:
        if first_kept_entry_id not in self.entries:
            raise KeyError(first_kept_entry_id)
        return self.append(
            "compaction",
            summary,
            firstKeptEntryId=first_kept_entry_id,
        )

    def model_context(self, leaf_id: str | None = None) -> list[str]:
        """把存储树翻译成模型看到的链；旧行仍在文件，只是被摘要替代。"""
        path = self.current_path(leaf_id)
        active = path
        for index, entry in enumerate(path):
            if entry.type == "compaction":
                first_kept = entry.data["firstKeptEntryId"]
                keep_index = next(i for i, item in enumerate(path) if item.id == first_kept)
                active = [Entry("summary", None, "summary", entry.content)] + path[keep_index:index] + path[index + 1 :]
        return [f"{entry.type}: {entry.content}" for entry in active if entry.type not in {"session", "position"}]


class PiAgent:
    def __init__(self, session: AppendOnlySession) -> None:
        self.session = session

    def user(self, text: str) -> Entry:
        return self.session.append("user", text)

    def assistant(self, text: str) -> Entry:
        return self.session.append("assistant", text)

    def tool(self, name: str, result: str) -> Entry:
        return self.session.append("tool", result, name=name)
