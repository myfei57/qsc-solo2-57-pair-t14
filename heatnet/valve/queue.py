"""阀门指令队列、已应用检查点与一次动作的完整时序。

一条指令从入队到算"执行完"，严格按下面的顺序走，任何一步写盘失败都不会留下
"内存比盘新"的半截状态：

1. **围栏检查**：围栏触发（``tripped``）时，本条与后面所有指令停在围栏前——
   不发动作、不写状态文档、不推进检查点，结果记 ``blocked``；重新投用后从同一
   序号继续。
2. **读内存阀位做部分失败决议**：本条序号大于检查点，说明上一轮没落完。按阀位
    定，不看盘上写没写：
   - 内存阀位已经在目标到位公差内 → ``voided``：动作本体作废，不重复发动作，
     只把检查点补到该序号（序号本身不能丢，后面的指令才能继续）；
   - 内存阀位不在目标 → ``retry``：同序号原地重试第 3 步。
3. **先落盘状态文档，再改内存**（见 :mod:`~heatnet.valve.move`）：写盘失败时
   内存阀位不变，结果记 ``held``，队列立即停住，本条既不算执行完也不丢，下一次
   推进重新走第 2 步决议。
4. **再落盘检查点**（序号 + 当时阀位）：检查点写失败同样记 ``held`` 停住；此时
   状态文档与内存已经一致，恢复时第 2 步会把它决议成 ``voided`` 补齐序号。
5. **最后发审计**：只有 1–4 全部走完才发布成功事件；异常结果（``blocked`` /
   ``held`` / ``voided`` / 对账不符）发 warning/alarm。

检查点**只按指令序号存**，阀位仅作对账字段，见
:class:`~heatnet.store.command.CommandCheckpoint` 的说明。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ValveLimits
from ..errors import StorageError
from ..eventbus import Event, EventBus
from ..store.command import CheckpointState, CommandCheckpoint
from ..store.state import DurableState
from .fence import Fence
from .move import move_valve
from .valve import Valve

#: 已在目标到位公差内：动作本体作废，只补序号。
DISPOSITION_VOID = "void"
#: 不在目标：同序号原地重发动作。
DISPOSITION_RETRY = "retry"

#: 正常执行完（落盘状态 + 改内存 + 落盘检查点）。
OUTCOME_APPLIED = "applied"
#: 部分失败，阀已在到位公差内，动作作废但序号已补齐。
OUTCOME_VOIDED = "voided"
#: 部分失败，阀不在目标，写盘没成功，指令挂起，等待重试。
OUTCOME_HELD = "held"
#: 围栏触发，指令停在围栏前，等待重新投用。
OUTCOME_BLOCKED = "blocked"


@dataclass(frozen=True)
class ValveCommand:
    """一条阀门指令。"""

    seq: int
    target_pct: float
    source: str

    def as_dict(self) -> dict:
        return {"seq": self.seq, "target_pct": round(self.target_pct, 2), "source": self.source}


class ValveCommandQueue:
    """按序号排列的阀门指令队列。"""

    def __init__(self, name: str) -> None:
        self._name = name
        self._commands: list[ValveCommand] = []
        self._next_seq = 1

    @property
    def name(self) -> str:
        return self._name

    def enqueue(self, target_pct: float, source: str = "dispatch") -> ValveCommand:
        """入队一条指令，返回带序号的指令。"""

        command = ValveCommand(self._next_seq, float(target_pct), str(source))
        self._commands.append(command)
        self._next_seq += 1
        return command

    def commands(self) -> list[ValveCommand]:
        """返回全部指令（按序号升序）。"""

        return list(self._commands)

    def pending(self, applied: int) -> list[ValveCommand]:
        """返回序号大于检查点的指令。"""

        return [command for command in self._commands if command.seq > int(applied)]

    def last_seq(self) -> int:
        """返回队尾序号。"""

        return self._commands[-1].seq if self._commands else 0


def disposition_for(position_pct: float, target_pct: float, limits: ValveLimits) -> str:
    """按阀位定部分失败决议：已到位则作废，否则原地重试。"""

    if limits.settled(position_pct, target_pct):
        return DISPOSITION_VOID
    return DISPOSITION_RETRY


def _publish(bus: EventBus | None, name: str, action: str, severity: str, message: str, **payload: object) -> None:
    if bus is None:
        return
    bus.publish(
        Event.of(
            "valve",
            f"valve.{name}",
            action,
            asset=name,
            severity=severity,
            message=message,
            **payload,
        )
    )


def replay_queue(
    valve: Valve,
    durable: DurableState,
    queue: ValveCommandQueue,
    checkpoint: CommandCheckpoint,
    fence: Fence | None = None,
    bus: EventBus | None = None,
) -> dict:
    """按检查点推进队列，逐条走完整时序，返回本次推进摘要。

    遇到 ``held``（写盘部分失败）或 ``blocked``（围栏触发）立即停住，后面的指令
    本轮不再推进；下一次调用从同一条序号重新决议。
    """

    state = checkpoint.load_state(queue.name)
    applied = state.applied
    pending = queue.pending(applied)
    moves: list[dict] = []
    held = 0
    blocked = 0
    voided = 0
    stop: str | None = None

    for command in pending:
        # 1. 围栏检查：停在围栏前，什么都不写。
        if fence is not None and fence.is_tripped():
            blocked += 1
            stop = OUTCOME_BLOCKED
            _publish(
                bus,
                queue.name,
                "valve_fenced",
                "alarm",
                f"围栏触发，指令 #{command.seq} 停在围栏前：{fence.reason()}",
                seq=command.seq,
                target_pct=round(command.target_pct, 2),
                fence=fence.snapshot().as_dict(),
            )
            break

        # 2. 按内存阀位做部分失败决议。
        disposition = disposition_for(valve.position_pct(), command.target_pct, valve.limits)
        entry: dict = {**command.as_dict()}

        if disposition == DISPOSITION_VOID:
            # 阀已经在目标上：动作本体作废，只补检查点序号。
            try:
                checkpoint.save(queue.name, command.seq, valve.position_pct())
            except StorageError as error:
                held += 1
                stop = OUTCOME_HELD
                entry.update(
                    {"ok": False, "outcome": OUTCOME_HELD, "reason": "checkpoint", "error": error.message}
                )
                moves.append(entry)
                _publish(
                    bus,
                    queue.name,
                    "valve_move_held",
                    "warning",
                    f"作废指令 #{command.seq} 补检查点失败，挂起重试：{error.message}",
                    seq=command.seq,
                    target_pct=round(command.target_pct, 2),
                    position_pct=round(valve.position_pct(), 2),
                )
                break
            applied = command.seq
            voided += 1
            entry.update(
                {
                    "ok": True,
                    "outcome": OUTCOME_VOIDED,
                    "position_pct": round(valve.position_pct(), 2),
                    "durable_pct": round(valve.position_pct(), 2),
                    "persisted": True,
                }
            )
            moves.append(entry)
            _publish(
                bus,
                queue.name,
                "valve_move_voided",
                "warning",
                f"指令 #{command.seq} 对应的阀位已到位，动作作废，仅补齐序号",
                seq=command.seq,
                target_pct=round(command.target_pct, 2),
                position_pct=round(valve.position_pct(), 2),
            )
            continue

        # 3. 先落盘状态文档、再改内存；写盘失败 => held，原地停住。
        try:
            move = move_valve(valve, durable, command.target_pct)
        except StorageError as error:
            held += 1
            stop = OUTCOME_HELD
            entry.update(
                {
                    "ok": False,
                    "outcome": OUTCOME_HELD,
                    "position_pct": round(valve.position_pct(), 2),
                    "durable_pct": None,
                    "persisted": False,
                    "reason": "state",
                    "error": error.message,
                }
            )
            moves.append(entry)
            _publish(
                bus,
                queue.name,
                "valve_move_held",
                "warning",
                f"指令 #{command.seq} 落盘失败，挂起原地重试：{error.message}",
                seq=command.seq,
                target_pct=round(command.target_pct, 2),
                position_pct=round(valve.position_pct(), 2),
            )
            break

        # 4. 再落盘检查点；失败 => held（状态文档已与内存一致，恢复后决议作废）。
        try:
            checkpoint.save(queue.name, command.seq, move.position_pct)
        except StorageError as error:
            held += 1
            stop = OUTCOME_HELD
            entry.update(
                {
                    **move.as_dict(),
                    "outcome": OUTCOME_HELD,
                    "reason": "checkpoint",
                    "error": error.message,
                }
            )
            moves.append(entry)
            _publish(
                bus,
                queue.name,
                "valve_move_held",
                "warning",
                f"指令 #{command.seq} 阀位已落盘但检查点失败，挂起等待对账：{error.message}",
                seq=command.seq,
                target_pct=round(command.target_pct, 2),
                position_pct=round(move.position_pct, 2),
            )
            break

        # 5. 全部落盘完成：本条算执行完。
        applied = command.seq
        entry.update({**move.as_dict(), "outcome": OUTCOME_APPLIED})
        moves.append(entry)

    return {
        "queue": queue.name,
        "applied": applied,
        "last_seq": queue.last_seq(),
        "skipped": len(queue.commands()) - len(pending),
        "replayed": len(moves),
        "held": held,
        "blocked": blocked,
        "voided": voided,
        "stop": stop,
        "fence": fence.snapshot().as_dict() if fence is not None else None,
        "moves": moves,
    }


def reconcile_queue(
    valve: Valve,
    queue: ValveCommandQueue,
    checkpoint: CommandCheckpoint,
    bus: EventBus | None = None,
) -> dict:
    """重启后对账：检查点、状态文档（已加载进内存）与队列三方核对。

    检查点序号落在某条指令之前、而内存阀位已经在该指令目标上时，说明上次死在
    "阀位已落盘、检查点未完成"的窗口里——此时不再重复动作，直接把检查点补齐。
    阀位与待执行指令目标对不上时只落告警、不动任何状态，等下一次推进按
    :func:`replay_queue` 的决议处理。
    """

    state: CheckpointState = checkpoint.load_state(queue.name)
    reconciled = 0
    mismatch = False
    next_command: ValveCommand | None = next(
        (command for command in queue.commands() if command.seq > state.applied), None
    )

    if next_command is not None and valve.limits.settled(
        valve.position_pct(), next_command.target_pct
    ):
        checkpoint.save(queue.name, next_command.seq, valve.position_pct())
        reconciled = 1
        _publish(
            bus,
            queue.name,
            "checkpoint_reconciled",
            "warning",
            f"重启对账：阀位已在指令 #{next_command.seq} 目标上，补齐检查点、不重复动作",
            seq=next_command.seq,
            position_pct=round(valve.position_pct(), 2),
            target_pct=round(next_command.target_pct, 2),
        )
    elif next_command is not None:
        mismatch = True
        _publish(
            bus,
            queue.name,
            "checkpoint_mismatch",
            "alarm",
            (
                f"重启对账不符：检查点停在 #{state.applied}，内存阀位 "
                f"{valve.position_pct():.2f}% 不在下一指令 #{next_command.seq} 目标 "
                f"{next_command.target_pct:.2f}% 上，等待重试"
            ),
            applied=state.applied,
            position_pct=round(valve.position_pct(), 2),
            target_pct=round(next_command.target_pct, 2),
        )

    return {
        "queue": queue.name,
        "applied": checkpoint.load(queue.name),
        "reconciled": reconciled,
        "mismatch": mismatch,
        "position_pct": round(valve.position_pct(), 2),
        "checkpoint_position_pct": state.position_pct,
    }
