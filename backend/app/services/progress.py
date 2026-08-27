# -*- coding: utf-8 -*-
"""长流程的实时反馈：现在在做什么、做到第几件、多久没动静了。

**要解决的问题。**
一次交底书跑下来 40 多分钟，其中「材料消化 8 分钟 + 撰写 26 分钟 + 规则校验 8 分钟」
落在三个一动不动的圆点上。用户看到的只有一个转圈：不知道是在跑还是卡死了，
更不知道卡在哪。

**三层反馈，缺一层就答不全那三个问题。**

- L1 步骤级：走到第几步了 —— 已有的 `step_status`；
- L2 步骤内：这一步分成几件事、做到第几件 —— `phase` + `index/total`；
- L3 证据：**最近一次真实变化**是什么、离现在多久 —— `detail` + `idle_ms`。

**L2 的分子必须是代码里真实存在的循环变量。**
不允许按时间估算、不允许「大概 60%」。假进度条比没有进度条更坏：它让人相信一个
不存在的剩余时间，等它停在 90% 不动时，用户失去的是对整个系统的信任。
所以 `index/total` 只能由「第 i 个文件 / 第 i 幅图 / 第 i 次调用」这类真实计数喂进来，
喂不出来的步骤就只报 L1+L3，不编 L2。

**「卡住」是按证据判定的，不是按耗时判定的。**
一次 26 分钟的撰写不是卡住，它一直在吐字；而一次 90 秒没有任何新证据的调用就很可疑。
所以判据是 `idle_ms`（距上一次真实变化多久），不是 `elapsed_ms`。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 心跳间隔：前端据此判断「还活着」。太密没有信息量，太疏又会让人以为断了。
BEAT_SEC = 5.0

# 多久没有新证据就算可疑。取 90 秒：正常的结构化调用（无流式）通常在 60 秒内返回，
# 长思维链模型有自己的 20 秒心跳会不断刷新证据，所以真的静默 90 秒基本都是异常。
STALL_AFTER_SEC = 90.0


@dataclass
class StepProgress:
    """一个正在执行的步骤的进度状态（进程内，随步骤结束丢弃）。"""

    step_key: str
    name_zh: str = ""
    phase: str = ""                     # L2：这一步内部当前在做的事
    index: int | None = None            # L2：真实循环变量，禁止估算
    total: int | None = None
    detail: str = ""                    # L3：最近一次证据
    waiting_for: str = ""               # 当前在等谁（模型 / 子进程 / 浏览器 / 用户）
    suspended: bool = False             # 门控挂起中：等的是用户，不该算「卡住」
    started_at: float = field(default_factory=time.monotonic)
    changed_at: float = field(default_factory=time.monotonic)

    def touch(self, detail: str = "") -> None:
        """记一次真实变化。detail 为空时只刷新时间戳（流式 token 走这条）。"""
        self.changed_at = time.monotonic()
        if detail:
            self.detail = detail

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        idle_ms = int((now - self.changed_at) * 1000)
        # 挂起等用户时不判卡住——那不是异常，是设计好的停顿
        stalled = (not self.suspended) and idle_ms >= STALL_AFTER_SEC * 1000
        data: dict[str, Any] = {
            "step_key": self.step_key,
            "name_zh": self.name_zh,
            "phase": self.phase,
            "detail": self.detail,
            "elapsed_ms": int((now - self.started_at) * 1000),
            "idle_ms": idle_ms,
            "stalled": stalled,
            "suspended": self.suspended,
        }
        if self.index is not None and self.total:
            data["index"] = self.index
            data["total"] = self.total
        if self.waiting_for:
            data["waiting_for"] = self.waiting_for
        if stalled:
            data["stall_hint"] = self._stall_hint(idle_ms)
        return data

    def _stall_hint(self, idle_ms: int) -> str:
        """把「卡住」说成人能据以行动的一句话，而不是一个转圈。"""
        secs = idle_ms // 1000
        target = self.waiting_for or "外部调用"
        return (
            f"已有 {secs} 秒没有收到{target}的任何新反馈。"
            "若持续无响应，可在下方取消本步骤后重试；重试不会丢失已完成的步骤。"
        )


# case_id → 当前步骤进度。步骤结束即清除；只在进程内，不入库。
_active: dict[str, StepProgress] = {}


def begin(case_id: str, step_key: str, name_zh: str = "") -> StepProgress:
    p = StepProgress(step_key=step_key, name_zh=name_zh)
    _active[case_id] = p
    return p


def end(case_id: str, step_key: str | None = None) -> None:
    """结束当前步骤的进度跟踪（step_key 给定时只清同一个步骤，避免误清后继步骤）。"""
    current = _active.get(case_id)
    if current is None:
        return
    if step_key is not None and current.step_key != step_key:
        return
    _active.pop(case_id, None)


def current(case_id: str) -> StepProgress | None:
    return _active.get(case_id)


def snapshot(case_id: str) -> dict[str, Any] | None:
    """当前进度快照（供 /pipeline/state 在刷新/重连后补上）。"""
    p = _active.get(case_id)
    return p.snapshot() if p is not None else None


def touch(case_id: str | None, detail: str = "") -> None:
    """记一次真实变化。

    给 services/llm 这类**不认识流水线上下文**的模块用：它只有 case_id，
    但它知道「刚刚确实发生了什么」（收到 token、退避重试、思维链心跳）。
    没有这条通路，一次 5 分钟的思考期会被误判成卡死。
    """
    if not case_id:
        return
    p = _active.get(case_id)
    if p is not None:
        p.touch(detail)
