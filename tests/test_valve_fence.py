"""写盘失败围栏、按阀失败策略与检查点/队列/内存三处时序。

这些用例把需求里的硬约束钉死：

- 检查点按指令序号存，阀位文档与检查点文档各自独立；
- 阀位写盘失败不许改内存，指令挂进围栏（position 阶段），后续指令不得越过；
- 阀位已落盘但检查点写失败时按 checkpoint 阶段挂起，重启后幂等补检查点，
  阀门不重复动作、moves 不多计；
- 挂起指令随检查点落盘，跨重启能接上；
- retry 阀原地重试、abort 阀整条挂起等待显式裁决（retry 重试 / abort 作废）；
- 围栏状态经事件总线落审计，状态、阀位与审计口径一致。
"""

from __future__ import annotations

import dataclasses

import pytest

from heatnet.config import default_config
from heatnet.errors import StorageError
from heatnet.eventbus import WILDCARD, EventBus
from heatnet.store.command import (
    PHASE_CHECKPOINT,
    PHASE_POSITION,
    CommandCheckpoint,
)
from heatnet.store.jsonfile import JsonStore
from heatnet.store.state import DurableState
from heatnet.valve.fence import FENCE_ABORTED, FENCE_CLEAR, FENCE_RETRY, ValveFence
from heatnet.valve.move import load_valve
from heatnet.valve.queue import ValveCommandQueue, replay_queue, resolve_queue
from heatnet.valve.valve import Valve


class _FailOnceDurable:
    """第一次写 valve 文档失败，之后交给真实存储（模拟瞬时落盘失败）。"""

    def __init__(self, real: DurableState) -> None:
        self._real = real
        self.calls = 0

    def load(self, component: str) -> dict:
        return self._real.load(component)

    def save(self, component: str, payload: object) -> str:
        self.calls += 1
        if self.calls == 1:
            raise StorageError("模拟瞬时写盘失败", component="store")
        return self._real.save(component, payload)


class _FlakyCheckpoint(CommandCheckpoint):
    """前 N 次写检查点失败，之后恢复。"""

    def __init__(self, store: JsonStore, fail_writes: int = 1) -> None:
        super().__init__(store)
        self._left = fail_writes

    def save_state(self, queue: str, applied: int, blocked=None) -> str:
        if self._left > 0:
            self._left -= 1
            raise StorageError("模拟检查点写盘失败", component="store")
        return super().save_state(queue, applied, blocked)


def _limits(policy: str):
    return dataclasses.replace(default_config().valve, write_policy=policy)


def _setup(tmp_path, name="st01", policy="retry", position=10.0):
    store = JsonStore(str(tmp_path))
    durable = DurableState(store)
    checkpoint = CommandCheckpoint(store)
    valve = Valve(f"{name}.mix", _limits(policy), position)
    queue = ValveCommandQueue(name)
    fence = ValveFence()
    return store, durable, checkpoint, valve, queue, fence


def test_valve_carries_per_position_write_policy():
    assert Valve("a.mix", _limits("retry")).write_policy == "retry"
    assert Valve("b.mix", _limits("abort")).write_policy == "abort"
    # 默认策略是原地重试，且非法策略在装配期就被拦下。
    assert Valve("c.mix", default_config().valve).write_policy == "retry"
    with pytest.raises(ValueError):
        Valve("d.mix", _limits("bogus"))


def test_checkpoint_is_keyed_by_sequence_not_position(tmp_path):
    store = JsonStore(str(tmp_path))
    checkpoint = CommandCheckpoint(store)
    # 旧文档（只有 applied）照常读，blocked 视为空。
    checkpoint.save("st01", 3)
    assert checkpoint.load_state("st01").applied == 3
    assert checkpoint.load_state("st01").blocked is None
    # 同一阀位可以对应不同序号，序号才是队列进度。
    checkpoint.save_state("st01", 5, None)
    raw = store.read("command/st01")
    assert raw == {"applied": 5}


def test_position_write_failure_blocks_queue_and_keeps_memory(tmp_path):
    store, durable, checkpoint, valve, queue, fence = _setup(tmp_path)
    queue.enqueue(40.0)
    queue.enqueue(80.0)
    flaky = _FailOnceDurable(durable)

    report = replay_queue(valve, flaky, queue, checkpoint, fence, now=1.0)

    assert report["ok"] is False
    assert report["fence"] == FENCE_RETRY
    assert report["replayed"] == 0
    assert valve.position_pct() == pytest.approx(10.0)
    blocked = report["blocked"]
    assert blocked["seq"] == 1
    assert blocked["phase"] == PHASE_POSITION
    assert blocked["policy"] == "retry"
    assert blocked["attempts"] == 1
    # 挂起指令随检查点落盘：applied 不前移，blocked 留住目标开度。
    doc = store.read("command/st01")
    assert doc["applied"] == 0
    assert doc["blocked"]["seq"] == 1
    assert doc["blocked"]["target_pct"] == pytest.approx(40.0)
    # 第二条指令不得越过挂起指令，阀位文档一个字都没写成。
    assert not store.exists("state/valve/st01.mix")
    assert valve.rejected() == 1


def test_retry_policy_replays_blocked_command_in_place(tmp_path):
    store, durable, checkpoint, valve, queue, fence = _setup(tmp_path)
    queue.enqueue(40.0)
    queue.enqueue(80.0)
    flaky = _FailOnceDurable(durable)

    first = replay_queue(valve, flaky, queue, checkpoint, fence, now=1.0)
    assert first["fence"] == FENCE_RETRY

    second = replay_queue(valve, durable, queue, checkpoint, fence, now=2.0)
    assert second["ok"] is True
    assert second["retried"] == 1
    assert second["replayed"] == 2
    assert second["fence"] == FENCE_CLEAR
    assert valve.position_pct() == pytest.approx(80.0)
    assert checkpoint.load("st01") == 2
    assert "blocked" not in store.read("command/st01")
    assert valve.moves() == 2


def test_blocked_command_survives_restart_and_resumes(tmp_path):
    store = JsonStore(str(tmp_path))
    durable = DurableState(store)
    valve = Valve("st01.mix", _limits("retry"), 10.0)
    queue = ValveCommandQueue("st01")
    queue.enqueue(33.0)
    queue.enqueue(77.0)
    checkpoint = CommandCheckpoint(store)
    fence = ValveFence()
    replay_queue(valve, _FailOnceDurable(durable), queue, checkpoint, fence, now=1.0)
    assert fence.state("st01") == FENCE_RETRY

    # 重启：队列内存是空的，阀也回到出厂位置，只靠盘上两份文档恢复。
    reopened = CommandCheckpoint(JsonStore(str(tmp_path)))
    restarted_valve = Valve("st01.mix", _limits("retry"), 10.0)
    restarted_queue = ValveCommandQueue("st01")
    restarted_fence = ValveFence()
    report = replay_queue(
        restarted_valve, durable, restarted_queue, reopened, restarted_fence, now=2.0
    )

    assert report["ok"] is True
    assert report["retried"] == 1
    assert report["applied"] == 1
    assert restarted_valve.position_pct() == pytest.approx(33.0)
    assert reopened.load("st01") == 1
    # 挂起指令被塞回队列，调度重新下发的后续指令序号不会与它冲突。
    restarted_queue.enqueue(77.0)
    assert [command.seq for command in restarted_queue.commands()] == [1, 2]
    followup = replay_queue(
        restarted_valve, durable, restarted_queue, reopened, restarted_fence, now=3.0
    )
    assert followup["replayed"] == 1
    assert restarted_valve.position_pct() == pytest.approx(77.0)


def test_checkpoint_phase_failure_is_idempotent_across_restart(tmp_path):
    store = JsonStore(str(tmp_path))
    durable = DurableState(store)
    valve = Valve("st02.mix", _limits("retry"), 0.0)
    queue = ValveCommandQueue("st02")
    queue.enqueue(50.0)
    queue.enqueue(90.0)
    fence = ValveFence()
    flaky = _FlakyCheckpoint(store, fail_writes=1)

    first = replay_queue(valve, durable, queue, flaky, fence, now=1.0)
    # 阀位已落盘、内存已推进，只有检查点没写成。
    assert first["fence"] == FENCE_RETRY
    assert first["blocked"]["phase"] == PHASE_CHECKPOINT
    assert valve.position_pct() == pytest.approx(50.0)
    assert durable.load("valve/st02.mix")["position_pct"] == pytest.approx(50.0)

    # 重启恢复：阀位文档是 50、检查点是 0；补检查点时不许再动一次阀。
    reopened = CommandCheckpoint(JsonStore(str(tmp_path)))
    restored = Valve("st02.mix", _limits("retry"), 0.0)
    load_valve(restored, durable)
    assert restored.position_pct() == pytest.approx(50.0)
    assert restored.moves() == 1
    restarted_queue = ValveCommandQueue("st02")
    restarted_queue.enqueue(50.0)
    restarted_queue.enqueue(90.0)
    report = replay_queue(
        restored, durable, restarted_queue, reopened, ValveFence(), now=2.0
    )
    assert report["ok"] is True
    assert report["moves"][0]["resumed_from"] == PHASE_CHECKPOINT
    assert restored.position_pct() == pytest.approx(90.0)
    assert restored.moves() == 2
    assert reopened.load("st02") == 2


def test_abort_policy_holds_until_explicit_decision(tmp_path):
    store, durable, checkpoint, valve, queue, fence = _setup(tmp_path, name="st03", policy="abort")
    queue.enqueue(40.0)
    queue.enqueue(80.0)

    first = replay_queue(valve, _FailOnceDurable(durable), queue, checkpoint, fence, now=1.0)
    assert first["fence"] == FENCE_ABORTED
    assert first["blocked"]["policy"] == "abort"
    assert valve.position_pct() == pytest.approx(10.0)

    # 再推进也不会自动重试 abort 围栏。
    again = replay_queue(valve, durable, queue, checkpoint, fence, now=2.0)
    assert again["fence"] == FENCE_ABORTED
    assert again["replayed"] == 0

    # 显式重试：只处理挂起的那一条，后续指令仍按序号排队。
    decision = resolve_queue(valve, durable, queue, checkpoint, fence, "retry", now=3.0)
    assert decision["ok"] is True
    assert decision["fence"] == FENCE_CLEAR
    assert checkpoint.load("st03") == 1
    assert valve.position_pct() == pytest.approx(40.0)

    followup = replay_queue(valve, durable, queue, checkpoint, fence, now=4.0)
    assert followup["replayed"] == 1
    assert valve.position_pct() == pytest.approx(80.0)


def test_abort_decision_skips_command_without_moving_valve(tmp_path):
    store, durable, checkpoint, valve, queue, fence = _setup(tmp_path, name="st04", policy="abort")
    queue.enqueue(40.0)
    queue.enqueue(80.0)
    replay_queue(valve, _FailOnceDurable(durable), queue, checkpoint, fence, now=1.0)

    decision = resolve_queue(valve, durable, queue, checkpoint, fence, "abort", now=2.0)
    assert decision["ok"] is True
    assert checkpoint.load("st04") == 1
    assert store.read("command/st04") == {"applied": 1}
    # 作废只前移检查点、抬围栏，阀位保持旧值。
    assert valve.position_pct() == pytest.approx(10.0)

    followup = replay_queue(valve, durable, queue, checkpoint, fence, now=3.0)
    assert followup["applied"] == 2
    assert valve.position_pct() == pytest.approx(80.0)


def test_resolve_requires_a_blocked_command_and_valid_decision(tmp_path):
    store, durable, checkpoint, valve, queue, fence = _setup(tmp_path)
    with pytest.raises(ValueError):
        resolve_queue(valve, durable, queue, checkpoint, fence, "retry")
    queue.enqueue(40.0)
    replay_queue(valve, _FailOnceDurable(durable), queue, checkpoint, fence, now=1.0)
    with pytest.raises(ValueError):
        resolve_queue(valve, durable, queue, checkpoint, fence, "bogus")


def test_resume_requires_seq_and_position_to_both_match(tmp_path):
    """阀位相同但序号不同（绕回同值）时，不许把新指令误判成旧指令补检查点。"""

    from heatnet.valve.move import position_already_persisted

    store = JsonStore(str(tmp_path))
    durable = DurableState(store)
    valve = Valve("st07.mix", _limits("retry"), 50.0)
    # 盘上阀位 50 是 seq2 写成的；现在要执行 seq1（目标恰好也是 50）。
    durable.save("valve/st07.mix", {"name": "st07.mix", "position_pct": 50.0, "moves": 1, "seq": 2})
    assert position_already_persisted(durable, valve, 50.0, seq=1) is False
    assert position_already_persisted(durable, valve, 50.0, seq=2) is True
    assert position_already_persisted(durable, valve, 60.0, seq=2) is False
    # 旧文档没有 seq 字段时退化为只比阀位。
    durable.save("valve/st07.mix", {"name": "st07.mix", "position_pct": 50.0, "moves": 1})
    assert position_already_persisted(durable, valve, 50.0, seq=1) is True


def test_fence_transitions_are_published_to_audit(tmp_path):
    store, durable, checkpoint, valve, queue, fence = _setup(tmp_path, name="st05")
    bus = EventBus()
    events = []
    bus.subscribe(WILDCARD, events.append)

    queue.enqueue(40.0)
    replay_queue(valve, _FailOnceDurable(durable), queue, checkpoint, fence, bus, now=1.0)
    failed = [
        event
        for event in events
        if event.action == "valve_write_failed" and event.payload.get("queue") == "st05"
    ]
    assert len(failed) == 1
    assert failed[0].payload["fence"] == FENCE_RETRY
    assert failed[0].payload["phase"] == PHASE_POSITION
    assert failed[0].payload["seq"] == 1

    replay_queue(valve, durable, queue, checkpoint, fence, bus, now=2.0)
    applied = [
        event
        for event in events
        if event.action == "valve_command_applied" and event.payload.get("queue") == "st05"
    ]
    assert len(applied) == 1
    assert applied[0].payload["fence"] == FENCE_CLEAR
    assert applied[0].payload["position_pct"] == pytest.approx(40.0)

    # abort 裁决单独落一条审计，围栏与阀位口径与实际一致。
    store2, durable2, cp2, valve2, queue2, fence2 = _setup(tmp_path / "ab", name="st06", policy="abort")
    queue2.enqueue(50.0)
    replay_queue(valve2, _FailOnceDurable(durable2), queue2, cp2, fence2, bus, now=3.0)
    resolve_queue(valve2, durable2, queue2, cp2, fence2, "abort", bus=bus, now=4.0)
    aborted = [
        event
        for event in events
        if event.action == "valve_command_aborted" and event.payload.get("queue") == "st06"
    ]
    assert len(aborted) == 1
    assert aborted[0].payload["fence"] == FENCE_CLEAR
    assert aborted[0].payload["position_pct"] == pytest.approx(10.0)
