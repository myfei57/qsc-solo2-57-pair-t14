"""阀门指令队列的检查点存储。

队列把"最后一条已应用指令的序号"单独落盘——**检查点按指令序号存，不按阀位
存**：阀位文档只是物理结果，同一个开度可能由不同序号的指令写成，阀位无法表达
"队列执行到哪一条"，更无法表达"卡在第 N 条"；序号单调、可比较，重启后序号不
大于检查点的指令一律跳过，天然支持去重。

检查点文档形状为 ``{"applied": 序号, "blocked": 挂起指令或缺失}``：写盘部分
失败的那一条指令（围栏挂起）随检查点一起落盘，所以"检查点、围栏、队列"看到的
是同一份进度——重启后挂起指令既不会被当成已执行（序号仍大于 applied），也不
会丢（blocked 里留着它的目标开度与挂起阶段）。旧文档只有 ``applied`` 字段时
照常读取，blocked 视为空。检查点写失败必须向上抛出，调用方据此拒绝本次推进。
"""

from __future__ import annotations

from dataclasses import dataclass

from .jsonfile import JsonStore

# 挂起阶段：阀位落盘失败，或阀位已落盘但检查点落盘失败。
PHASE_POSITION = "position"
PHASE_CHECKPOINT = "checkpoint"
PHASES = (PHASE_POSITION, PHASE_CHECKPOINT)

# 按阀位定死的两种写盘失败策略。
POLICY_RETRY = "retry"
POLICY_ABORT = "abort"
POLICIES = (POLICY_RETRY, POLICY_ABORT)


@dataclass(frozen=True)
class BlockedCommand:
    """一条挂起在围栏上的指令（部分失败，未完成、未丢失）。

    结构放在 store 层：挂起记录要随检查点文档落盘，持久化层不能反向依赖阀门
    编排层（否则形成循环导入）；阀门围栏只持有它并做状态转换。
    """

    seq: int
    target_pct: float
    source: str
    phase: str
    policy: str
    attempts: int = 1
    ts: float = 0.0

    def as_dict(self) -> dict:
        return {
            "seq": int(self.seq),
            "target_pct": round(float(self.target_pct), 4),
            "source": self.source,
            "phase": self.phase,
            "policy": self.policy,
            "attempts": int(self.attempts),
            "ts": round(float(self.ts), 3),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "BlockedCommand":
        """从检查点文档还原挂起指令，缺字段时用安全默认值。"""

        phase = str(raw.get("phase", PHASE_POSITION))
        if phase not in PHASES:
            phase = PHASE_POSITION
        policy = str(raw.get("policy", POLICY_RETRY))
        if policy not in POLICIES:
            policy = POLICY_RETRY
        return cls(
            seq=int(raw.get("seq", 0)),
            target_pct=float(raw.get("target_pct", 0.0)),
            source=str(raw.get("source", "unknown")),
            phase=phase,
            policy=policy,
            attempts=max(1, int(raw.get("attempts", 1))),
            ts=float(raw.get("ts", 0.0)),
        )


@dataclass(frozen=True)
class CheckpointState:
    """一份检查点文档的完整内容。"""

    applied: int
    blocked: BlockedCommand | None


class CommandCheckpoint:
    """按队列名保存已应用序号与挂起指令。"""

    def __init__(self, store: JsonStore, prefix: str = "command") -> None:
        self._store = store
        self._prefix = prefix.strip("/")

    @property
    def store(self) -> JsonStore:
        return self._store

    def doc_id(self, queue: str) -> str:
        """返回某队列检查点文档的标识。"""

        return f"{self._prefix}/{queue}"

    def load_state(self, queue: str) -> CheckpointState:
        """读取完整检查点（已应用序号 + 挂起指令）。"""

        raw = self._store.read(self.doc_id(queue))
        if not isinstance(raw, dict):
            return CheckpointState(applied=0, blocked=None)
        try:
            applied = max(0, int(raw.get("applied", 0)))
        except (TypeError, ValueError):
            applied = 0
        blocked_raw = raw.get("blocked")
        blocked = BlockedCommand.from_dict(blocked_raw) if isinstance(blocked_raw, dict) else None
        if blocked is not None and blocked.seq <= applied:
            # 盘上数据自相矛盾（已应用序号越过了挂起指令）时，以序号为准，
            # 挂起记录作废——不能让一条已完成的指令永远挡住队列。
            blocked = None
        return CheckpointState(applied=applied, blocked=blocked)

    def load(self, queue: str) -> int:
        """读取已应用序号，缺失或非法时返回 ``0``。"""

        return self.load_state(queue).applied

    def save_state(self, queue: str, applied: int, blocked: BlockedCommand | None = None) -> str:
        """保存已应用序号与挂起指令，返回落盘路径。"""

        if applied < 0:
            raise ValueError("已应用序号不能为负")
        if blocked is not None and blocked.seq <= applied:
            raise ValueError("挂起指令序号必须大于已应用序号")
        payload: dict = {"applied": int(applied)}
        if blocked is not None:
            payload["blocked"] = blocked.as_dict()
        return self._store.write(self.doc_id(queue), payload)

    def save(self, queue: str, applied: int) -> str:
        """只保存已应用序号，保留盘上已有的挂起指令。"""

        blocked = self.load_state(queue).blocked
        return self.save_state(queue, applied, blocked)

    def reset(self, queue: str) -> bool:
        """清除检查点，返回是否真的删掉了文件。"""

        return self._store.remove(self.doc_id(queue))
