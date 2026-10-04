"""阀门动作的落盘顺序。

先落盘、再改内存：写盘失败时内存开度保持不变，错误向上抛出，调用方据此把这一条
指令挂进围栏、原样重试；反过来先改内存再写盘，一旦写失败，内存里的开度就比盘
上的新，进程重启后阀门回退到旧位置，与调节结果对不上。

还有一种部分失败：阀位文档已写成、内存也推进了，但随后的检查点落盘失败。此时
内存与盘上阀位一致、只是队列进度没记上，重启后由队列层做幂等短路（见
:func:`position_already_persisted`），只补检查点，不再驱动一次阀门。阀位文档
同时记下写成它的指令序号，短路必须"序号 + 阀位"双匹配，避免两条不同指令恰好
目标开度相同（例如 50→60→50 中间漏执行）时把新指令误当成旧指令的补检查点。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import StorageError
from ..store.state import DurableState
from .valve import Valve


@dataclass(frozen=True)
class ValveMove:
    """一次阀门动作的结果。"""

    ok: bool
    name: str
    position_pct: float
    durable_pct: float
    persisted: bool

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "name": self.name,
            "position_pct": round(self.position_pct, 2),
            "durable_pct": round(self.durable_pct, 2),
            "persisted": self.persisted,
        }


def move_valve(
    valve: Valve,
    durable: DurableState,
    target_pct: float,
    seq: int | None = None,
) -> ValveMove:
    """把阀门开到目标开度：先落盘，落盘成功后再改内存。

    队列驱动时传入指令 ``seq``，阀位文档会记下"最后写成它的指令序号"，供检查点
    阶段部分失败后的幂等短路比对；队列外的直接动作（站内调节、启泵看守）不传。
    """

    clamped = max(0.0, min(100.0, float(target_pct)))
    payload = {
        "name": valve.name,
        "position_pct": round(clamped, 4),
        "moves": valve.moves() + 1,
    }
    if seq is not None:
        payload["seq"] = int(seq)
    try:
        durable.save(f"valve/{valve.name}", payload)
    except StorageError:
        valve.reject()
        raise
    position = valve.apply(clamped)
    return ValveMove(
        ok=True,
        name=valve.name,
        position_pct=position,
        durable_pct=clamped,
        persisted=True,
    )


def position_already_persisted(
    durable: DurableState,
    valve: Valve,
    target_pct: float,
    seq: int | None = None,
) -> bool:
    """盘上阀位是否已经是该指令的结果（检查点阶段部分失败后的幂等短路判定）。

    阀位文档已落盘、内存已推进，仅检查点没落盘时，重启后盘上阀位就是该指令的
    目标值。这时再 :func:`move_valve` 会让阀门多动作一次、``moves`` 多计一次；
    队列层据此只补写检查点。

    判定规则：传了 ``seq`` 时要求文档里的 ``seq`` 与阀位**同时**匹配（旧文档没有
    seq 字段时退化为只比阀位，兼容历史数据）；不传 seq 的直接动作只比阀位。
    """

    raw = durable.load(f"valve/{valve.name}")
    try:
        persisted_pct = float(raw["position_pct"])
    except (KeyError, TypeError, ValueError):
        return False
    clamped = max(0.0, min(100.0, float(target_pct)))
    if abs(persisted_pct - clamped) > 1e-9:
        return False
    if seq is None:
        return True
    persisted_seq = raw.get("seq")
    if persisted_seq is None:
        # 历史阀位文档不带序号：阀位一致即视为同一结果。
        return True
    try:
        return int(persisted_seq) == int(seq)
    except (TypeError, ValueError):
        return False


def load_valve(valve: Valve, durable: DurableState) -> float:
    """从落盘状态恢复阀门开度与动作计数，缺失时保持现值。

    恢复不算一次新动作，直接按盘上值置位（``Valve.restore``），否则每重启一次
    ``moves`` 就虚增一次，与阀位文档里记下的计数对不上。
    """

    raw = durable.load(f"valve/{valve.name}")
    if not raw:
        return valve.position_pct()
    try:
        position = float(raw.get("position_pct", valve.position_pct()))
    except (TypeError, ValueError):
        return valve.position_pct()
    try:
        moves = int(raw.get("moves", 0))
    except (TypeError, ValueError):
        moves = 0
    return valve.restore(position, moves)
