"""混水阀与指令队列。

混水阀把一次网高温水和二次网回水按开度混合；每个换热站一只阀。阀门动作分三层：
:mod:`~heatnet.valve.move` 负责"先落盘、再改内存"的单次动作，
:mod:`~heatnet.valve.fence` 负责联锁围栏的两态（投用/触发）与持久化，
:mod:`~heatnet.valve.queue` 负责带序号的指令队列、按阀位的部分失败决议与已应用
检查点，重启后不重放已经提交的指令，写盘部分失败的指令挂起原地重试。
"""

from __future__ import annotations

from .fence import (
    ARMED,
    TRIPPED,
    Fence,
    FenceSnapshot,
    arm_fence,
    load_fence,
    trip_fence,
)
from .move import ValveMove, load_valve, move_valve
from .queue import (
    DISPOSITION_RETRY,
    DISPOSITION_VOID,
    OUTCOME_APPLIED,
    OUTCOME_BLOCKED,
    OUTCOME_HELD,
    OUTCOME_VOIDED,
    ValveCommand,
    ValveCommandQueue,
    disposition_for,
    reconcile_queue,
    replay_queue,
)
from .state import ValveSnapshot
from .valve import Valve

__all__ = [
    "ARMED",
    "TRIPPED",
    "DISPOSITION_RETRY",
    "DISPOSITION_VOID",
    "OUTCOME_APPLIED",
    "OUTCOME_BLOCKED",
    "OUTCOME_HELD",
    "OUTCOME_VOIDED",
    "Fence",
    "FenceSnapshot",
    "Valve",
    "ValveCommand",
    "ValveCommandQueue",
    "ValveMove",
    "ValveSnapshot",
    "arm_fence",
    "disposition_for",
    "load_fence",
    "load_valve",
    "move_valve",
    "reconcile_queue",
    "replay_queue",
    "trip_fence",
]
