"""阀门指令队列的检查点存储。

队列把"最后一条已提交指令的序号"单独落盘。重启后先读这个检查点，序号不大于
检查点的指令一律跳过——否则队列从零开始重放，已执行的指令会被再执行一次，阀门
回到调节前的位置。检查点写失败必须向上抛出，调用方据此把本条指令挂起、拒绝推进。

检查点**按指令序号存，不按阀位存**：序号单调唯一、每条指令一一对应，重启后能
精确定位"已执行到哪一条"；阀位会重复、会回跳，两个不同序号可以指向同一开度，
拿阀位当游标就无法区分该跳过谁。阀位只作为附带的对账字段 ``position_pct`` 一并
落盘，恢复时与状态文档、内存阀位三方核对，发现对不上就落告警。
"""

from __future__ import annotations

from dataclasses import dataclass

from .jsonfile import JsonStore


@dataclass(frozen=True)
class CheckpointState:
    """一份检查点：已提交序号 + 提交时阀位（对账用）。"""

    applied: int
    position_pct: float | None

    @classmethod
    def from_mapping(cls, raw: dict) -> "CheckpointState":
        try:
            applied = max(0, int(raw.get("applied", 0)))
        except (TypeError, ValueError):
            applied = 0
        position = raw.get("position_pct")
        try:
            position = None if position is None else float(position)
        except (TypeError, ValueError):
            position = None
        return cls(applied, position)


class CommandCheckpoint:
    """按队列名保存已应用序号。"""

    def __init__(self, store: JsonStore, prefix: str = "command") -> None:
        self._store = store
        self._prefix = prefix.strip("/")

    @property
    def store(self) -> JsonStore:
        return self._store

    def doc_id(self, queue: str) -> str:
        """返回某队列检查点文档的标识。"""

        return f"{self._prefix}/{queue}"

    def load(self, queue: str) -> int:
        """读取已应用序号，缺失或非法时返回 ``0``。"""

        return self.load_state(queue).applied

    def load_state(self, queue: str) -> CheckpointState:
        """读取完整检查点（序号 + 提交时阀位），缺失时返回零值。"""

        raw = self._store.read(self.doc_id(queue))
        if isinstance(raw, dict):
            return CheckpointState.from_mapping(raw)
        return CheckpointState(0, None)

    def save(self, queue: str, applied: int, position_pct: float | None = None) -> str:
        """保存已应用序号与当时阀位，返回落盘路径。"""

        if applied < 0:
            raise ValueError("已应用序号不能为负")
        payload: dict = {"applied": int(applied)}
        if position_pct is not None:
            payload["position_pct"] = round(float(position_pct), 4)
        return self._store.write(self.doc_id(queue), payload)

    def reset(self, queue: str) -> bool:
        """清除检查点，返回是否真的删掉了文件。"""

        return self._store.remove(self.doc_id(queue))
