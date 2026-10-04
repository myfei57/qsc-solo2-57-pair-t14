"""阀门指令队列、已应用检查点与写盘失败围栏。

指令带单调序号入队；推进时只执行序号大于检查点的指令。**检查点按指令序号存**
（见 :mod:`~heatnet.store.command` 的定死说明）。

每执行一条指令，三处时序不能乱：

1. 阀位文档先落盘；写盘失败内存开度保持旧值，指令挂进围栏（phase=position），
   队列停住——这一条既不算执行完（检查点不前移），也不丢（挂起指令连同目标开度
   写进检查点文档）。
2. 阀位落盘成功后才改内存开度。
3. 内存推进后才落检查点；检查点写失败时阀位与内存已经一致，指令挂进围栏
   （phase=checkpoint），下次推进幂等补写检查点，不会再驱动一次阀门。

挂起指令怎么处理按阀位定：``retry`` 阀原地重试（下次推进自动重放挂起的那一条，
后续指令始终不得越过它）；``abort`` 阀整条动作挂起，必须经
:func:`resolve_queue` 显式裁决——重试（retry）或作废（abort，检查点越过该序号
但阀位保持不变）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..errors import StorageError
from ..eventbus import Event, EventBus
from ..store.command import (
    PHASE_CHECKPOINT,
    PHASE_POSITION,
    BlockedCommand,
    CommandCheckpoint,
)
from ..store.state import DurableState
from .fence import ValveFence
from .move import move_valve, position_already_persisted
from .valve import Valve


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

    def has(self, seq: int) -> bool:
        """队列里是否已有某序号的指令。"""

        return any(command.seq == int(seq) for command in self._commands)

    def restore_command(self, seq: int, target_pct: float, source: str) -> ValveCommand:
        """把盘上挂起的指令塞回队列（重启恢复专用）。

        已存在同序号指令时原样返回；缺失时按序号插回有序位置，并把队尾序号补到
        它之后，保证后续入队不会复用挂起指令的序号。
        """

        for command in self._commands:
            if command.seq == int(seq):
                return command
        command = ValveCommand(int(seq), float(target_pct), str(source))
        self._commands.append(command)
        self._commands.sort(key=lambda item: item.seq)
        self._next_seq = max(self._next_seq, command.seq + 1)
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


def _publish(bus: EventBus | None, action: str, severity: str, message: str, **fields: object) -> None:
    """经事件总线落审计；未装配总线（单元测试直调）时跳过。"""

    if bus is None:
        return
    asset = str(fields.get("queue", ""))
    bus.publish(
        Event.of(
            "valve",
            "valve",
            action,
            asset=asset,
            severity=severity,
            message=message,
            **fields,
        )
    )


def _apply_one(
    valve: Valve,
    durable: DurableState,
    checkpoint: CommandCheckpoint,
    fence: ValveFence,
    queue_name: str,
    seq: int,
    target_pct: float,
    source: str,
    applied: int,
    bus: EventBus | None,
    now: float,
    *,
    attempts: int,
) -> tuple[bool, int, dict | None]:
    """执行一条指令的完整时序，返回 ``(是否完成, 新的已应用序号, 动作明细)``。

    任何部分失败都不向上抛（阀位写盘且围栏也落不住的情况除外，那时盘上进度无法
    保证，只能抛给调用方），改为落下围栏并返回未完成，由下次推进或显式裁决接上。
    """

    # 阶段一：阀位落盘 + 内存推进。盘上阀位已是该指令结果时（checkpoint 阶段部分
    # 失败后的重启重放）幂等短路，只补检查点，不再驱动阀门；序号与阀位必须双匹配。
    if position_already_persisted(durable, valve, target_pct, seq):
        detail = {
            "seq": seq,
            "target_pct": round(float(target_pct), 2),
            "source": source,
            "resumed_from": PHASE_CHECKPOINT,
        }
    else:
        try:
            move = move_valve(valve, durable, target_pct, seq)
        except StorageError:
            blocked = BlockedCommand(
                seq=seq,
                target_pct=target_pct,
                source=source,
                phase=PHASE_POSITION,
                policy=valve.write_policy,
                attempts=attempts,
                ts=now,
            )
            if fence.is_blocked(queue_name):
                fence.retry_again(queue_name)
                blocked = fence.blocked(queue_name)  # type: ignore[assignment]
            else:
                fence.plant(queue_name, blocked)
            # 挂起指令必须随检查点落盘：这一步也写不住，进度在盘上无从表达。
            checkpoint.save_state(queue_name, applied, blocked)
            _publish(
                bus,
                "valve_write_failed",
                "warning",
                f"{queue_name} 第 {seq} 条指令阀位落盘失败，围栏={fence.state(queue_name)}",
                queue=queue_name,
                seq=seq,
                target_pct=round(float(target_pct), 2),
                policy=valve.write_policy,
                phase=PHASE_POSITION,
                attempts=blocked.attempts,
                fence=fence.state(queue_name),
            )
            return False, applied, None
        detail = {
            "seq": seq,
            "target_pct": round(move.position_pct, 2),
            "source": source,
            **move.as_dict(),
        }

    # 阶段二：检查点落盘。失败时阀位与内存已一致，围栏按 checkpoint 阶段挂起；
    # 若围栏原本挂的是 position 阶段（这一轮重试时阀位补写成功、检查点才失败），
    # 挂起阶段随之推进，重试次数沿用。挂起状态只留在内存（同一份检查点文档本来就
    # 写不住），重启后靠"盘上阀位已是目标值"幂等自愈。
    try:
        checkpoint.save_state(queue_name, seq, None)
    except StorageError:
        blocked = BlockedCommand(
            seq=seq,
            target_pct=target_pct,
            source=source,
            phase=PHASE_CHECKPOINT,
            policy="retry",
            attempts=attempts,
            ts=now,
        )
        if fence.is_blocked(queue_name):
            fence.resume_at(
                queue_name,
                BlockedCommand(
                    seq=seq,
                    target_pct=target_pct,
                    source=source,
                    phase=PHASE_CHECKPOINT,
                    policy="retry",
                    attempts=attempts + 1,
                    ts=now,
                ),
            )
        else:
            fence.plant(queue_name, blocked)
        _publish(
            bus,
            "valve_checkpoint_failed",
            "warning",
            f"{queue_name} 第 {seq} 条指令阀位已落盘，检查点落盘失败，围栏=retry",
            queue=queue_name,
            seq=seq,
            target_pct=round(float(target_pct), 2),
            phase=PHASE_CHECKPOINT,
            attempts=fence.blocked(queue_name).attempts,  # type: ignore[union-attr]
            fence="retry",
        )
        return False, applied, detail

    fence.release(queue_name)
    _publish(
        bus,
        "valve_command_applied",
        "notice",
        f"{queue_name} 第 {seq} 条阀位指令执行完成",
        queue=queue_name,
        seq=seq,
        target_pct=round(float(target_pct), 2),
        position_pct=round(valve.position_pct(), 2),
        source=source,
        resumed=detail.get("resumed_from") == PHASE_CHECKPOINT,
        fence="clear",
    )
    return True, seq, detail


def _report(
    queue: ValveCommandQueue,
    applied: int,
    pending_before: list[ValveCommand],
    moves: list[dict],
    fence: ValveFence,
    *,
    retried: int,
) -> dict:
    blocked = fence.blocked(queue.name)
    # skipped = 检查点之前的旧指令；replayed = 本次真正完成的；其余是仍未执行
    # （被围栏挡住或本周期没排到）的待执行指令。
    skipped = len(queue.commands()) - len(pending_before)
    return {
        "queue": queue.name,
        "applied": applied,
        "last_seq": queue.last_seq(),
        "skipped": skipped,
        "replayed": len(moves),
        "retried": retried,
        "moves": moves,
        "fence": fence.state(queue.name),
        "blocked": None if blocked is None else blocked.as_dict(),
        "ok": blocked is None,
    }


def replay_queue(
    valve: Valve,
    durable: DurableState,
    queue: ValveCommandQueue,
    checkpoint: CommandCheckpoint,
    fence: ValveFence | None = None,
    bus: EventBus | None = None,
    now: float | None = None,
) -> dict:
    """按检查点推进队列，返回本次推进摘要。

    先按盘上检查点恢复围栏镜像并把挂起指令塞回队列；围栏是 retry 时先原地重试
    挂起的那一条，是 abort 时直接停下等显式裁决；之后才按序号推进后续指令，任一
    条部分失败立即停住（后面的指令本周期不执行）。
    """

    fence = fence if fence is not None else ValveFence()
    moment = time.time() if now is None else float(now)

    state = checkpoint.load_state(queue.name)
    applied = state.applied
    fence.recover(queue.name, state.blocked)
    if state.blocked is not None and not queue.has(state.blocked.seq):
        queue.restore_command(
            state.blocked.seq, state.blocked.target_pct, state.blocked.source
        )

    pending_before = queue.pending(applied)
    pending = list(pending_before)
    moves: list[dict] = []
    retried = 0

    blocked = fence.blocked(queue.name)
    if blocked is not None:
        if blocked.policy == "abort":
            # 整条动作挂起，不自动重试，后续指令一律不越过。
            return _report(queue, applied, pending_before, moves, fence, retried=0)
        retried += 1
        done, applied, detail = _apply_one(
            valve,
            durable,
            checkpoint,
            fence,
            queue.name,
            blocked.seq,
            blocked.target_pct,
            blocked.source,
            applied,
            bus,
            moment,
            attempts=blocked.attempts,
        )
        if not done:
            return _report(queue, applied, pending_before, moves, fence, retried=retried)
        if detail is not None:
            moves.append(detail)
        pending = queue.pending(applied)

    for command in pending:
        if fence.is_blocked(queue.name):
            break
        done, applied, detail = _apply_one(
            valve,
            durable,
            checkpoint,
            fence,
            queue.name,
            command.seq,
            command.target_pct,
            command.source,
            applied,
            bus,
            moment,
            attempts=1,
        )
        if not done:
            break
        if detail is not None:
            moves.append(detail)

    return _report(queue, applied, pending_before, moves, fence, retried=retried)


def resolve_queue(
    valve: Valve,
    durable: DurableState,
    queue: ValveCommandQueue,
    checkpoint: CommandCheckpoint,
    fence: ValveFence,
    decision: str,
    bus: EventBus | None = None,
    now: float | None = None,
) -> dict:
    """显式裁决围栏上的挂起指令：``retry`` 原地重试，``abort`` 整条作废。

    作废只前移检查点、抬起围栏，阀位保持挂起前的旧值不动；后续指令仍按序号排队，
    由下一次 :func:`replay_queue` 推进。
    """

    moment = time.time() if now is None else float(now)
    blocked = fence.blocked(queue.name)
    if blocked is None:
        raise ValueError(f"队列 {queue.name} 没有挂起指令，无需裁决")
    if decision not in ("retry", "abort"):
        raise ValueError("裁决必须是 retry 或 abort")

    if decision == "abort":
        checkpoint.save_state(queue.name, blocked.seq, None)
        fence.release(queue.name)
        _publish(
            bus,
            "valve_command_aborted",
            "warning",
            f"{queue.name} 第 {blocked.seq} 条指令经裁决整条作废",
            queue=queue.name,
            seq=blocked.seq,
            target_pct=round(blocked.target_pct, 2),
            position_pct=round(valve.position_pct(), 2),
            fence="clear",
        )
        return {
            "queue": queue.name,
            "decision": "abort",
            "applied": blocked.seq,
            "seq": blocked.seq,
            "fence": "clear",
            "blocked": None,
            "ok": True,
        }

    done, applied, detail = _apply_one(
        valve,
        durable,
        checkpoint,
        fence,
        queue.name,
        blocked.seq,
        blocked.target_pct,
        blocked.source,
        checkpoint.load(queue.name),
        bus,
        moment,
        attempts=blocked.attempts,
    )
    return {
        "queue": queue.name,
        "decision": "retry",
        "applied": applied,
        "seq": blocked.seq,
        "fence": fence.state(queue.name),
        "blocked": None if fence.blocked(queue.name) is None else fence.blocked(queue.name).as_dict(),
        "move": detail,
        "ok": done,
    }
