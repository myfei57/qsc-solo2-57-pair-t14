"""阀门围栏（联锁开关）。

围栏只有两态：``armed``（已投用，允许动作）与 ``tripped``（已触发，拒绝一切
阀门指令）。围栏触发后，指令队列里的指令一律停在围栏前——不发动作、不写状态
文档、不推进检查点；重新投用后从同一条序号继续，不会漏掉也不会重放。

围栏状态本身也是一份"当前态"文档（``fence/<名称>``），遵守和阀门动作同一条
铁律：**先落盘，落盘成功后才改内存**。这样进程重启之后，围栏状态、阀位与检查
点三处仍然对得上；文档缺失时按"已投用"处理，新装的阀不会被一道不存在的围栏
挡住。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import StorageError
from ..store.state import DurableState

ARMED = "armed"
TRIPPED = "tripped"
STATES = (ARMED, TRIPPED)


@dataclass(frozen=True)
class FenceSnapshot:
    """围栏某一时刻的状态。"""

    name: str
    state: str
    reason: str

    def as_dict(self) -> dict:
        return {"name": self.name, "state": self.state, "tripped": self.state == TRIPPED, "reason": self.reason}


class Fence:
    """一只阀的围栏；本体只记内存状态，不知道持久化的存在。"""

    def __init__(self, name: str, state: str = ARMED, reason: str = "") -> None:
        if state not in STATES:
            raise ValueError(f"非法围栏状态: {state!r}")
        self._name = name
        self._state = state
        self._reason = str(reason)

    @property
    def name(self) -> str:
        return self._name

    def state(self) -> str:
        """返回 ``armed`` / ``tripped``。"""

        return self._state

    def reason(self) -> str:
        """返回最近一次状态变更的原因。"""

        return self._reason

    def is_armed(self) -> bool:
        """是否已投用（允许阀门动作）。"""

        return self._state == ARMED

    def is_tripped(self) -> bool:
        """是否已触发（联锁拒绝阀门动作）。"""

        return self._state == TRIPPED

    def snapshot(self) -> FenceSnapshot:
        """返回围栏状态快照。"""

        return FenceSnapshot(self._name, self._state, self._reason)

    def _set(self, state: str, reason: str = "") -> None:
        """只改内存（供本模块的落盘包装函数在写盘成功后调用）。"""

        if state not in STATES:
            raise ValueError(f"非法围栏状态: {state!r}")
        self._state = state
        self._reason = str(reason)


def _doc_id(name: str) -> str:
    return f"fence/{name}"


def trip_fence(fence: Fence, durable: DurableState, reason: str) -> FenceSnapshot:
    """触发围栏：先落盘，成功后才改内存。

    已经触发时原样返回（幂等，不重复写盘）；写盘失败时内存状态保持 ``armed``，
    调用方可原样重试。
    """

    if fence.is_tripped():
        return fence.snapshot()
    payload = {"state": TRIPPED, "reason": str(reason)}
    try:
        durable.save(_doc_id(fence.name), payload)
    except StorageError:
        raise
    fence._set(TRIPPED, reason)
    return fence.snapshot()


def arm_fence(fence: Fence, durable: DurableState, reason: str = "reset") -> FenceSnapshot:
    """重新投用围栏：先落盘，成功后才改内存。"""

    if fence.is_armed():
        return fence.snapshot()
    payload = {"state": ARMED, "reason": str(reason)}
    try:
        durable.save(_doc_id(fence.name), payload)
    except StorageError:
        raise
    fence._set(ARMED, reason)
    return fence.snapshot()


def load_fence(fence: Fence, durable: DurableState) -> FenceSnapshot:
    """从落盘状态恢复围栏；文档缺失或内容非法时保持"已投用"。"""

    raw = durable.load(_doc_id(fence.name))
    state = str(raw.get("state", ARMED)) if isinstance(raw, dict) else ARMED
    if state not in STATES:
        state = ARMED
    reason = str(raw.get("reason", "")) if isinstance(raw, dict) else ""
    fence._set(state, reason)
    return fence.snapshot()
