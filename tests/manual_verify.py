"""不依赖 pytest 的端到端行为验证：围栏、部分失败、检查点时序、重启对账。

跑法：PYTHONPATH=. python3 tests/manual_verify.py
退出码 0 表示全部断言通过。
"""

from __future__ import annotations

import shutil
import tempfile

from heatnet.config import default_config
from heatnet.errors import StorageError
from heatnet.eventbus import EventBus
from heatnet.store.command import CommandCheckpoint
from heatnet.store.jsonfile import JsonStore
from heatnet.store.state import DurableState
from heatnet.valve.fence import ARMED, TRIPPED, Fence, arm_fence, load_fence, trip_fence
from heatnet.valve.move import load_valve, move_valve
from heatnet.valve.queue import (
    OUTCOME_APPLIED,
    OUTCOME_BLOCKED,
    OUTCOME_HELD,
    OUTCOME_VOIDED,
    ValveCommandQueue,
    disposition_for,
    reconcile_queue,
    replay_queue,
)
from heatnet.valve.valve import Valve


def valve(name: str = "st01", position_pct: float = 0.0) -> Valve:
    return Valve(f"{name}.mix", default_config().valve, position_pct)


class _FailNTimes:
    """阀位文档写前 n 次失败，之后放行；检查点始终正常。"""

    def __init__(self, root: str, fail_state: int = 1) -> None:
        self._real = DurableState(JsonStore(root))
        self._fail_state = fail_state

    def load(self, component: str) -> dict:
        return self._real.load(component)

    def save(self, component: str, payload: object) -> str:
        if component.startswith("valve/") and self._fail_state > 0:
            self._fail_state -= 1
            raise StorageError("模拟阀位写盘失败", component=component)
        return self._real.save(component, payload)


class _CheckpointFailOnce:
    """检查点第一次写失败，之后放行。"""

    def __init__(self, real: CommandCheckpoint) -> None:
        self._real = real
        self._fail_left = 1

    def load_state(self, queue: str):
        return self._real.load_state(queue)

    def load(self, queue: str) -> int:
        return self._real.load(queue)

    def save(self, queue: str, applied: int, position_pct: float | None = None) -> str:
        if self._fail_left > 0:
            self._fail_left -= 1
            raise StorageError("模拟检查点写盘失败", component=f"command/{queue}")
        return self._real.save(queue, applied, position_pct)


def main() -> None:
    root = tempfile.mkdtemp(prefix="heatnet-verify-")
    try:
        store = JsonStore(root)
        durable = DurableState(store)
        checkpoint = CommandCheckpoint(store)
        bus = EventBus()
        events: list[str] = []
        bus.subscribe("*", lambda event: events.append(event.action))

        # 1. 正常推进：3 条指令全部 applied，检查点=3。
        v = valve()
        q = ValveCommandQueue("st01")
        f = Fence("st01")
        for target in (30.0, 55.0, 70.0):
            q.enqueue(target, "dispatch")
        report = replay_queue(v, durable, q, checkpoint, fence=f, bus=bus)
        assert report["applied"] == 3 and report["held"] == 0, report
        assert all(m["outcome"] == OUTCOME_APPLIED for m in report["moves"]), report
        assert v.position_pct() == 70.0 and checkpoint.load("st01") == 3
        state = checkpoint.load_state("st01")
        assert state.position_pct == 70.0, state

        # 2. 检查点是序号键，不是阀位键：两个 seq 可指向同一开度，游标不回退。
        q2 = ValveCommandQueue("st02")
        v2 = valve("st02")
        f2 = Fence("st02")
        q2.enqueue(40.0)
        q2.enqueue(40.0)  # 同阀位、不同序号
        r2 = replay_queue(v2, durable, q2, checkpoint, fence=f2)
        assert r2["applied"] == 2, r2
        assert checkpoint.load("st02") == 2

        # 3. 阀位文档写失败 => held：内存不变、检查点不推进、指令不丢；重试成功。
        flaky_root = tempfile.mkdtemp(prefix="flaky-")
        real_d3 = DurableState(JsonStore(flaky_root))
        v3 = valve("st03", position_pct=20.0)
        move_valve(v3, real_d3, 20.0)  # 预置 20%
        flaky = _FailNTimes(flaky_root, fail_state=1)
        q3 = ValveCommandQueue("st03")
        f3 = Fence("st03")
        cp3 = CommandCheckpoint(JsonStore(flaky_root))
        q3.enqueue(80.0)
        r3 = replay_queue(v3, flaky, q3, cp3, fence=f3, bus=bus)
        assert r3["stop"] == OUTCOME_HELD and r3["held"] == 1, r3
        assert v3.position_pct() == 20.0, v3.position_pct()  # 内存未改
        assert cp3.load("st03") == 0  # 检查点未推进
        assert v3.rejected() == 1
        # 原地重试同一条
        r3b = replay_queue(v3, flaky, q3, cp3, fence=f3)
        assert r3b["applied"] == 1 and r3b["stop"] is None, r3b
        assert v3.position_pct() == 80.0
        assert "valve_move_held" in events

        # 4. 部分失败按阀位作废：内存已在目标公差内 => voided，不重复动作、序号补齐。
        root4 = tempfile.mkdtemp(prefix="void-")
        d4 = DurableState(JsonStore(root4))
        cp4 = CommandCheckpoint(JsonStore(root4))
        v4 = valve("st04", position_pct=60.0)
        move_valve(v4, d4, 60.0)  # 盘上和内存都是 60，检查点仍为 0（模拟崩溃窗口）
        assert v4.moves() == 1
        q4 = ValveCommandQueue("st04")
        f4 = Fence("st04")
        q4.enqueue(60.0)
        q4.enqueue(70.0)
        r4 = replay_queue(v4, d4, q4, cp4, fence=f4, bus=bus)
        assert r4["voided"] == 1, r4
        assert r4["moves"][0]["outcome"] == OUTCOME_VOIDED
        assert v4.moves() == 2  # 第一条没重复动作，第二条才动作
        assert cp4.load("st04") == 2 and v4.position_pct() == 70.0
        assert "valve_move_voided" in events

        # 5. 检查点写失败 => held；下一轮按阀位决议成 voided 补齐，不重复动作。
        root5 = tempfile.mkdtemp(prefix="cpfail-")
        d5 = DurableState(JsonStore(root5))
        cp5 = _CheckpointFailOnce(CommandCheckpoint(JsonStore(root5)))
        v5 = valve("st05")
        q5 = ValveCommandQueue("st05")
        f5 = Fence("st05")
        q5.enqueue(50.0)
        r5 = replay_queue(v5, d5, q5, cp5, fence=f5, bus=bus)
        assert r5["stop"] == OUTCOME_HELD and r5["held"] == 1, r5
        assert v5.position_pct() == 50.0  # 阀位已落盘+改内存
        cp5_real = cp5._real  # noqa: SLF001
        assert cp5_real.load("st05") == 0  # 检查点未提交
        moves_before = v5.moves()
        r5b = replay_queue(v5, d5, q5, cp5_real, fence=f5, bus=bus)
        assert r5b["voided"] == 1 and r5b["applied"] == 1, r5b
        assert v5.moves() == moves_before  # 没有重复发动作
        assert cp5_real.load("st05") == 1

        # 6. 围栏触发 => blocked：不写盘、不改内存、检查点不推进；投用后从同序号继续。
        root6 = tempfile.mkdtemp(prefix="fence-")
        d6 = DurableState(JsonStore(root6))
        cp6 = CommandCheckpoint(JsonStore(root6))
        v6 = valve("st06")
        q6 = ValveCommandQueue("st06")
        f6 = Fence("st06")
        q6.enqueue(33.0)
        q6.enqueue(66.0)
        trip_fence(f6, d6, "overpressure")
        r6 = replay_queue(v6, d6, q6, cp6, fence=f6, bus=bus)
        assert r6["stop"] == OUTCOME_BLOCKED and r6["blocked"] == 1, r6
        assert v6.position_pct() == 0.0 and cp6.load("st06") == 0
        assert "valve_fenced" in events
        arm_fence(f6, d6, "shift-lead")
        r6b = replay_queue(v6, d6, q6, cp6, fence=f6, bus=bus)
        assert r6b["applied"] == 2 and r6b["stop"] is None, r6b
        assert v6.position_pct() == 66.0

        # 7. 围栏写盘失败不改内存，可重试。
        root7 = tempfile.mkdtemp(prefix="fencefail-")

        class _FenceFail:
            def load(self, component: str) -> dict:
                return {}

            def save(self, component: str, payload: object) -> str:
                raise StorageError("围栏写盘失败", component=component)

        f7 = Fence("st07")
        try:
            trip_fence(f7, _FenceFail(), "x")
            raise AssertionError("应当抛出 StorageError")
        except StorageError:
            pass
        assert f7.is_armed()  # 内存保持投用
        d7 = DurableState(JsonStore(root7))
        trip_fence(f7, d7, "retry-ok")
        assert f7.is_tripped()

        # 8. 围栏跨重启恢复：落盘的是 tripped，重启后仍是 tripped。
        f7b = Fence("st07")
        snap = load_fence(f7b, d7)
        assert snap.state == TRIPPED and snap.reason == "retry-ok"
        # 无文档时默认 armed
        assert load_fence(Fence("brand-new"), d7).state == ARMED

        # 9. 重启对账：阀位已到位、检查点落后 => reconcile 补齐序号且不动作。
        rec = reconcile_queue(v5, q5, cp5_real, bus=bus)
        assert rec["applied"] == 1 and rec["reconciled"] == 0, rec  # 已补齐过
        # 制造落后窗口再对账
        root9 = tempfile.mkdtemp(prefix="recon-")
        d9 = DurableState(JsonStore(root9))
        cp9 = CommandCheckpoint(JsonStore(root9))
        v9 = valve("st09")
        move_valve(v9, d9, 42.0)
        q9 = ValveCommandQueue("st09")
        q9.enqueue(42.0)
        rec9 = reconcile_queue(v9, q9, cp9, bus=bus)
        assert rec9["reconciled"] == 1 and cp9.load("st09") == 1, rec9
        assert v9.moves() == 1  # 对账不重复动作
        assert "checkpoint_reconciled" in events
        # 对不上 => mismatch 告警，不动状态
        root9b = tempfile.mkdtemp(prefix="reconm-")
        d9b = DurableState(JsonStore(root9b))
        cp9b = CommandCheckpoint(JsonStore(root9b))
        v9b = valve("st09")
        move_valve(v9b, d9b, 10.0)
        q9b = ValveCommandQueue("st09")
        q9b.enqueue(90.0)
        rec9b = reconcile_queue(v9b, q9b, cp9b, bus=bus)
        assert rec9b["mismatch"] is True and cp9b.load("st09") == 0, rec9b
        assert "checkpoint_mismatch" in events

        # 10. disposition_for：按阀位（含到位公差），而不是按盘上成败。
        limits = default_config().valve
        assert disposition_for(50.0, 50.0 + limits.settle_band_pct, limits) == "void"
        assert disposition_for(50.0, 50.0 + limits.settle_band_pct + 0.01, limits) == "retry"

        print("ALL_VERIFY_OK")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        for extra in ():
            shutil.rmtree(extra, ignore_errors=True)


if __name__ == "__main__":
    main()
