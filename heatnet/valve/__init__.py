"""混水阀与指令队列。

混水阀把一次网高温水和二次网回水按开度混合；每个换热站一只阀。阀门动作分三层：
:mod:`~heatnet.valve.move` 负责"先落盘、再改内存"的单次动作，
:mod:`~heatnet.valve.fence` 负责写盘部分失败的围栏（retry / aborted 两挂起态），
:mod:`~heatnet.valve.queue` 负责带序号的指令队列、按序号落盘的已应用检查点与
整条编排，重启后不回放已经执行过的指令、挂起指令也不丢。
"""

from __future__ import annotations

from .fence import (
    FENCE_ABORTED,
    FENCE_CLEAR,
    FENCE_RETRY,
    BlockedCommand,
    ValveFence,
)
from .move import ValveMove, load_valve, move_valve, position_already_persisted
from .queue import ValveCommand, ValveCommandQueue, replay_queue, resolve_queue
from .state import ValveSnapshot
from .valve import Valve

__all__ = [
    "BlockedCommand",
    "FENCE_ABORTED",
    "FENCE_CLEAR",
    "FENCE_RETRY",
    "Valve",
    "ValveCommand",
    "ValveCommandQueue",
    "ValveFence",
    "ValveMove",
    "ValveSnapshot",
    "load_valve",
    "move_valve",
    "position_already_persisted",
    "replay_queue",
    "resolve_queue",
]
