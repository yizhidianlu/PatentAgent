# -*- coding: utf-8 -*-
"""长流程的实时反馈：在跑 / 跑到哪 / 卡在哪，三个问题都要答得出。

一次交底书 40 多分钟，其中 42 分钟落在三个一动不动的圆点上。用户看到的只有一个
转圈——不知道是在跑还是死了，更不知道卡在哪。这组用例盯的就是这件事。

**最重要的一条：`index/total` 的分子必须是代码里真实存在的循环变量。**
不允许按时间估算。假进度条比没有进度条更坏：它让人相信一个不存在的剩余时间，
等它停在 90% 不动，用户失去的是对整个系统的信任。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.services import progress


@pytest.fixture(autouse=True)
def _clean():
    progress._active.clear()
    yield
    progress._active.clear()


def test_snapshot_is_none_without_a_running_step() -> None:
    assert progress.snapshot("nobody") is None


def test_counts_only_appear_when_both_are_given() -> None:
    """给不出真实分子的步骤就不显示 x/y —— 宁可少说，不可编。"""
    p = progress.begin("c1", "draft", "撰写")
    snap = p.snapshot()
    assert "index" not in snap and "total" not in snap

    p.index, p.total = 2, 5
    snap = p.snapshot()
    assert snap["index"] == 2 and snap["total"] == 5


def test_idle_not_elapsed_decides_stalled() -> None:
    """判据是「多久没有新证据」，不是「跑了多久」。

    一次 26 分钟的撰写不是卡住——它一直在吐字；
    而一次 90 秒没有任何反馈的调用就很可疑。两者用 elapsed 分不开。
    """
    p = progress.begin("c2", "draft")
    p.started_at = time.monotonic() - 1800      # 已经跑了半小时
    p.changed_at = time.monotonic() - 1         # 但一秒前刚有新内容
    snap = p.snapshot()
    assert snap["elapsed_ms"] > 1_700_000
    assert snap["stalled"] is False, "长耗时不等于卡住"

    p.changed_at = time.monotonic() - (progress.STALL_AFTER_SEC + 5)
    assert p.snapshot()["stalled"] is True


def test_stall_hint_says_who_we_are_waiting_for_and_what_to_do() -> None:
    """卡住提示必须能让人据以行动，而不只是「出错了」。"""
    p = progress.begin("c3", "drawings")
    p.waiting_for = "附图脚本"
    p.changed_at = time.monotonic() - 200
    snap = p.snapshot()
    hint = snap["stall_hint"]
    assert "附图脚本" in hint, "要说清在等谁"
    assert "秒" in hint, "要说清等了多久"
    assert "重试" in hint, "要给出可执行的下一步"


def test_waiting_for_user_is_not_stalled() -> None:
    """等用户确认是设计好的停顿，不是异常，不该报警。"""
    p = progress.begin("c4", "preview")
    p.suspended = True
    p.changed_at = time.monotonic() - 3600
    snap = p.snapshot()
    assert snap["suspended"] is True
    assert snap["stalled"] is False
    assert "stall_hint" not in snap


def test_touch_refreshes_the_evidence_clock() -> None:
    """流式 token / 退避重试 / 思维链心跳都算「确实在动」。"""
    p = progress.begin("c5", "draft")
    p.changed_at = time.monotonic() - 300
    assert p.snapshot()["stalled"] is True

    progress.touch("c5", "已输出 3,214 字")
    snap = p.snapshot()
    assert snap["stalled"] is False
    assert snap["detail"] == "已输出 3,214 字"


def test_touch_on_unknown_case_is_a_noop() -> None:
    """services/llm 不认识流水线上下文，可能对着一个没有步骤在跑的案件 touch。"""
    progress.touch("no-such-case", "x")
    progress.touch(None, "x")


def test_end_only_clears_its_own_step() -> None:
    """步骤结束时不能顺手清掉后继步骤的进度（重试/并发下会错乱）。"""
    progress.begin("c6", "step_a")
    progress.end("c6", "step_b")
    assert progress.current("c6") is not None
    progress.end("c6", "step_a")
    assert progress.current("c6") is None


# ---------------------------------------------------------------------------
# 引擎集成
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_engine_emits_heartbeats_for_a_slow_step(monkeypatch) -> None:
    """慢步骤期间必须持续有心跳——这是「在跑还是死了」唯一可靠的答案来源。"""
    from app.pipelines import engine

    beats: list[dict] = []

    async def fake_emit(case_id, event, data, **kwargs):
        if event == "step_progress":
            beats.append(data)
        return None

    monkeypatch.setattr(engine.hub, "emit", fake_emit)
    monkeypatch.setattr(progress, "BEAT_SEC", 0.05)

    progress.begin("hb", "slow_step", "慢步骤")
    task = asyncio.create_task(engine._progress_heartbeat("hb"))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(beats) >= 3, f"0.3 秒内应至少有 3 拍，实际 {len(beats)}"
    assert beats[-1]["step_key"] == "slow_step"
    assert beats[-1]["elapsed_ms"] >= beats[0]["elapsed_ms"]


@pytest.mark.anyio
async def test_heartbeat_stops_when_the_step_ends(monkeypatch) -> None:
    """步骤结束后不该再发心跳——否则界面会一直显示一个早就跑完的步骤。"""
    from app.pipelines import engine

    beats: list[dict] = []

    async def fake_emit(case_id, event, data, **kwargs):
        if event == "step_progress":
            beats.append(data)
        return None

    monkeypatch.setattr(engine.hub, "emit", fake_emit)
    monkeypatch.setattr(progress, "BEAT_SEC", 0.05)

    progress.begin("hb2", "s")
    task = asyncio.create_task(engine._progress_heartbeat("hb2"))
    await asyncio.sleep(0.15)
    progress.end("hb2", "s")
    count_at_end = len(beats)
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert count_at_end >= 1
    assert len(beats) == count_at_end, "进度记录清掉后不该再有心跳"


# ---------------------------------------------------------------------------
# 真实流水线：分子必须来自真实的循环变量
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_material_scan_counts_real_files() -> None:
    """材料消化的 x/y 里，y 就是待消化文件数，x 就是当前第几个。

    这条是整套进度机制的底线：分母不能是常量、分子不能按时间估。
    这里直接驱动 handler，用一个假 ctx 收集它上报了什么。
    """
    from app.pipelines import disclosure

    reported: list[dict] = []

    class FakeCtx:
        case_id = "prog-case"
        step_key = "material_scan"
        state: dict = {}
        start_payload: dict = {"file_ids": []}
        case: dict = {"id": "prog-case", "title": "T", "patent_type": "invention"}

        async def progress(self, phase="", *, index=None, total=None, detail="", waiting_for=""):
            reported.append(
                {"phase": phase, "index": index, "total": total, "detail": detail}
            )

        def tick(self, detail: str = "") -> None:
            pass

        async def emit(self, *a, **k):
            return None

    rows = [
        {"md_path": "a.md", "orig_name": "甲.docx"},
        {"md_path": "b.md", "orig_name": "乙.pdf"},
        {"md_path": "c.md", "orig_name": "丙.pptx"},
    ]

    ctx = FakeCtx()
    # 只跑到「上报进度」为止：真正的 LLM 调用不在本用例关心的范围内
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(disclosure.db, "arun", _fake_arun(rows))
        mp.setattr(disclosure, "_read_md_sync", lambda p: "正文内容")
        mp.setattr(disclosure, "_split_chunks", lambda text, **kw: [text])
        mp.setattr(disclosure, "_payload", lambda ctx: {"file_ids": []})

        async def boom(*a, **k):
            raise _StopAfterProgress

        mp.setattr(disclosure.assembler, "assemble", lambda *a, **k: "sys")
        mp.setattr(disclosure.build_service, "call_struct", boom)
        with pytest.raises(_StopAfterProgress):
            await disclosure.material_scan(ctx)

    assert reported, "材料消化必须上报进度"
    first = reported[0]
    assert first["total"] == len(rows), "分母必须是真实的待消化文件数"
    assert first["index"] == 1
    assert "甲.docx" in first["phase"], "阶段名要说清正在处理哪一份"


class _StopAfterProgress(Exception):
    """用例只关心进度上报，收到第一次 LLM 调用即停。"""


def _fake_arun(rows):
    async def _arun(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "_material_rows_sync":
            return rows
        return fn(*args, **kwargs)

    return _arun


# ---------------------------------------------------------------------------
# 停滞判定 × 在途探针：等待 ≠ 失联
# ---------------------------------------------------------------------------


def test_inflight_upstream_call_suppresses_stall(monkeypatch) -> None:
    """上游有在途调用时不判卡住——那是等待，不是失联。

    部署端从调用账里挖出的误报：思维链心跳间隔上限（曾 120s）大于停滞阈值（90s），
    每个心跳周期的尾部 30 秒界面都在对一个正常思考的调用喊「卡住」；
    思考 500 秒的一步被冤枉三次。更糟的分支：供应商不推思维链分片时心跳一次不发，
    停滞提示从 90 秒起常亮到正文开始。在途登记在 create 发出时就存在，
    **不依赖对方先说话**——停滞判定问它，不问心跳。
    """
    from app.services import progress

    p = progress.begin("stall-case", "extraction", "深读论文")
    p.changed_at = time.monotonic() - 500          # 静默 500 秒（纯思考期形态）

    monkeypatch.setattr(progress, "_inflight_probe", lambda case_id: 1)
    snap = p.snapshot()
    assert snap["stalled"] is False, "在途调用存在时不得判卡住"
    assert snap["upstream_inflight"] == 1
    assert snap["waiting_for"] == "模型响应"
    assert snap["idle_ms"] >= 490_000, "idle 要如实报，只是不配「可取消重试」那句话"

    monkeypatch.setattr(progress, "_inflight_probe", lambda case_id: 0)
    snap2 = p.snapshot()
    assert snap2["stalled"] is True, "在途归零后同样的静默必须立刻恢复可判"


def test_probe_failure_never_breaks_progress(monkeypatch) -> None:
    """探针坏了不能连累进度上报（宁可误报卡住，不能没有进度）。"""
    from app.services import progress

    def boom(case_id: str) -> int:
        raise RuntimeError("probe broken")

    monkeypatch.setattr(progress, "_inflight_probe", boom)
    p = progress.begin("probe-broken", "s")
    snap = p.snapshot()          # 不抛异常即通过
    assert "stalled" in snap


def test_heartbeat_interval_stays_below_stall_threshold() -> None:
    """不变量：心跳间隔上限 < 停滞阈值。

    两个常量在不同文件里各自演进，曾经交叉（120 > 90）——停滞检测检测到的
    是自己的心跳周期。这条测试让它们再也交叉不了。
    """
    from app.services import llm as llm_mod
    from app.services import progress

    assert llm_mod.REASONING_HEARTBEAT_MAX_SEC < progress.STALL_AFTER_SEC, (
        f"心跳间隔上限 {llm_mod.REASONING_HEARTBEAT_MAX_SEC}s 必须小于停滞阈值 "
        f"{progress.STALL_AFTER_SEC}s，否则长思考期会周期性误报「卡住」"
    )
