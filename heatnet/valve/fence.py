"""阀门写盘失败围栏。

围栏表达"某只阀的指令队列当前能不能继续推进"，有三种状态：

- ``clear``：没有挂起的部分失败，队列正常推进；
- ``retry``：上一条指令写盘部分失败，指令既没执行完也没丢，必须原地重试，
  检查点停在失败序号之前，后续指令一律不得越过它；
- ``aborted``：阀位写盘失败且该阀按 ``abort`` 策略处置，整条动作挂起等待显式
  裁决（重试或作废）。

挂起指令分两个阶段，对应三处时序中不同的落盘点：

- ``position``：阀位文档都没写成，内存开度保持旧值，重试就是完整重放这一条；
- ``checkpoint``：阀位文档已写成、内存已推进，但检查点没落盘；重启后阀位文档
  是新值、检查点是旧序号，重试只能幂等补写检查点，不能再驱动一次阀门。

挂起指令的数据结构（:class:`~heatnet.store.command.BlockedCommand`）放在 store
层并随检查点文档落盘；围栏本身只管内存镜像与状态转换是否合法，镜像的落盘由
队列编排层负责，和检查点写进同一份文档，保证"围栏、检查点、队列"看到的是同一
个进度。
"""

from __future__ import annotations

from ..store.command import (
    PHASE_CHECKPOINT,
    PHASE_POSITION,
    PHASES,
    POLICY_ABORT,
    POLICY_RETRY,
    BlockedCommand,
)

FENCE_CLEAR = "clear"
FENCE_RETRY = "retry"
FENCE_ABORTED = "aborted"
FENCE_STATES = (FENCE_CLEAR, FENCE_RETRY, FENCE_ABORTED)

__all__ = [
    "BlockedCommand",
    "FENCE_ABORTED",
    "FENCE_CLEAR",
    "FENCE_RETRY",
    "FENCE_STATES",
    "PHASE_CHECKPOINT",
    "PHASE_POSITION",
    "PHASES",
    "POLICY_ABORT",
    "POLICY_RETRY",
]


class ValveFence:
    """按队列名维护的写盘失败围栏（内存镜像）。"""

    def __init__(self) -> None:
        self._blocked: dict[str, BlockedCommand] = {}

    def state(self, queue: str) -> str:
        """返回某队列的围栏状态。"""

        blocked = self._blocked.get(queue)
        if blocked is None:
            return FENCE_CLEAR
        return FENCE_ABORTED if blocked.policy == POLICY_ABORT else FENCE_RETRY

    def blocked(self, queue: str) -> BlockedCommand | None:
        """返回挂起指令，没有挂起时返回 ``None``。"""

        return self._blocked.get(queue)

    def is_blocked(self, queue: str) -> bool:
        """队列是否被围栏挡住（此时不得推进后续指令）。"""

        return queue in self._blocked

    def plant(self, queue: str, blocked: BlockedCommand) -> BlockedCommand:
        """落下围栏。只允许从 clear 落下；已挂起用 :meth:`retry_again` 改记。"""

        if queue in self._blocked:
            raise ValueError(f"队列 {queue} 围栏已挂起，不能重复落下")
        if blocked.phase not in PHASES:
            raise ValueError("挂起阶段必须是 position 或 checkpoint")
        if blocked.policy not in (POLICY_RETRY, POLICY_ABORT):
            raise ValueError("写盘失败策略必须是 retry 或 abort")
        self._blocked[queue] = blocked
        return blocked

    def retry_again(self, queue: str) -> BlockedCommand:
        """挂起指令又重试了一次仍失败，尝试次数加一，状态不变。"""

        blocked = self._blocked.get(queue)
        if blocked is None:
            raise ValueError(f"队列 {queue} 围栏未挂起")
        self._blocked[queue] = BlockedCommand(
            seq=blocked.seq,
            target_pct=blocked.target_pct,
            source=blocked.source,
            phase=blocked.phase,
            policy=blocked.policy,
            attempts=blocked.attempts + 1,
            ts=blocked.ts,
        )
        return self._blocked[queue]

    def resume_at(self, queue: str, blocked: BlockedCommand) -> BlockedCommand:
        """把挂起指令推进到下一阶段（阀位补写成功、只剩检查点没补）。"""

        current = self._blocked.get(queue)
        if current is None or current.seq != blocked.seq:
            raise ValueError("只能推进当前挂起的同一条指令")
        self._blocked[queue] = blocked
        return blocked

    def release(self, queue: str) -> None:
        """抬起围栏（指令重试成功或显式作废后）。"""

        self._blocked.pop(queue, None)

    def recover(self, queue: str, blocked: BlockedCommand | None) -> None:
        """按盘上检查点恢复围栏镜像（重启专用）。"""

        if blocked is None:
            self._blocked.pop(queue, None)
        else:
            self._blocked[queue] = blocked

    def snapshot(self) -> dict[str, dict]:
        """返回全部挂起指令的字典形式，供控制台展示与落盘对照。"""

        return {queue: blocked.as_dict() for queue, blocked in self._blocked.items()}
