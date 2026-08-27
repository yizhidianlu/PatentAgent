"""流水线注册表：module → build_steps(case) 工厂。

- 各模块（disclosure / paper2patent / reader / oa）的流水线由第二棒在各自模块文件
  内调用 register() 注册；
- API 层 start/resume/retry 用 build_steps(case_row) 按案件的 module 构建步骤；
- 内置一个 'demo' 测试流水线（三步：普通步 + form 门控步 + 可失败步），供引擎
  测试与前端联调用。测试可 register("reader", build_demo_steps) 之类把 demo 挂到
  真实模块名下（cases.module 有 CHECK 约束，建不出 module='demo' 的案件）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .engine import Ctx, InteractionRequest, StepDef

logger = logging.getLogger(__name__)

# build_steps 工厂：入参为案件行 dict（含 module/patent_type/state_json 等），返回步骤列表
StepsBuilder = Callable[[dict[str, Any]], list[StepDef]]

_builders: dict[str, StepsBuilder] = {}


def register(key: str, builder: StepsBuilder) -> None:
    """注册（或覆盖）某模块的步骤工厂。"""
    if key in _builders:
        logger.info("流水线注册表：覆盖已注册的 %r", key)
    _builders[key] = builder


def get_builder(key: str) -> StepsBuilder | None:
    """取某模块的步骤工厂；未注册返回 None。"""
    return _builders.get(key)


def registered_modules() -> list[str]:
    """已注册的模块 key 列表。"""
    return sorted(_builders)


def build_steps(case: dict[str, Any]) -> list[StepDef]:
    """按案件 module 构建步骤列表；未注册抛 KeyError。"""
    key = case.get("module") or ""
    builder = _builders.get(key)
    if builder is None:
        raise KeyError(f"模块 {key!r} 的流水线尚未注册（已注册：{registered_modules()}）")
    return builder(case)


# ---------------------------------------------------------------------------
# demo 测试流水线（引擎地基验收用）
# ---------------------------------------------------------------------------

def build_demo_steps(case: dict[str, Any]) -> list[StepDef]:
    """三步演示流水线：prepare（普通）→ gate_form（form 门控）→ flaky（可失败）。

    start_payload 约定：{"fail_attempts": N} —— flaky 步骤前 N 次 attempt 抛异常
    （N=0 一次通过；N=1 首跑失败、retry 续跑成功）。
    """

    async def prepare(ctx: Ctx) -> dict[str, Any]:
        await ctx.chat_delta("正在准备演示流水线…")
        await ctx.chat_done()
        payload = ctx.start_payload if isinstance(ctx.start_payload, dict) else {}
        return {"prepared": True, "fail_attempts": int(payload.get("fail_attempts", 0))}

    async def gate_form(ctx: Ctx) -> dict[str, Any]:
        answer = await ctx.await_user(
            InteractionRequest(
                kind="form",
                schema={
                    "type": "object",
                    "properties": {"answer": {"type": "integer", "title": "答案"}},
                    "required": ["answer"],
                },
                prompt="请填写演示表单",
                default={"answer": 0},
            )
        )
        return {"form": answer}

    async def flaky(ctx: Ctx) -> dict[str, Any]:
        fail_attempts = int(ctx.state.get("fail_attempts", 0))
        if ctx.attempt <= fail_attempts:
            raise RuntimeError(f"演示失败（第 {ctx.attempt} 次尝试）")
        return {"flaky_ok": True, "flaky_attempt": ctx.attempt}

    return [
        StepDef(key="prepare", name_zh="准备", handler=prepare),
        StepDef(key="gate_form", name_zh="表单门控", handler=gate_form, gate="form"),
        StepDef(key="flaky", name_zh="可失败步", handler=flaky),
    ]


register("demo", build_demo_steps)


# ---------------------------------------------------------------------------
# 各模块流水线注册（模块文件本身不反向依赖注册表，避免循环导入）
# ---------------------------------------------------------------------------

from . import disclosure as _disclosure  # 置于 register 定义之后，避免循环导入
from . import paper2patent as _paper2patent
from . import reader as _reader

register("disclosure", _disclosure.build_steps)
register("paper2patent", _paper2patent.build_steps)
register("reader", _reader.build_steps)

from . import oa as _oa  # 同上：置于 register 定义之后，避免循环导入

register("oa", _oa.build_steps)
