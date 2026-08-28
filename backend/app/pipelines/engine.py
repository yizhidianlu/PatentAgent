"""通用流水线状态机引擎（M3 地基）。

设计（backend-architecture.md §4 + prompt-porting-spec.md §0 / §3.7）：
- StepDef{key, name_zh, handler, gate, retryable}；handler 为一段式 async 函数
  handler(ctx) → StepResult|dict|None；
- 门控步骤用 **ctx.await_user(interaction) 协程挂起模式**：emit interaction_required
  → 状态置 waiting_user → await asyncio.Event → 用户输入经 submit_input() 注入后
  返回 payload —— 使 prepare/consume 两段逻辑写成一段自然代码；
- run_pipeline：逐步执行；每步 INSERT pipeline_runs(running) + emit step_status；
  成功 → done + output 合并进 cases.state_json；异常 → failed + emit error{retryable}
  （LLM 网络错的自动重试在 services/llm 层，引擎不重复）；结束 emit pipeline_done，
  case 状态 completed/failed；
- 已 done 的步骤（同 run_group）自动跳过 → resume/retry 都等于「从第一个非 done
  步骤重建再启动」，waiting_user 的门控步骤重跑时会自然重发 interaction_required；
- 任务管理：模块级 dict[case_id] → asyncio.Task；cancel = task.cancel() + 状态回写
  cancelled（case 回 draft，可重新 start/resume）；
- startup 恢复钩子 recover_interrupted()：running 的 runs → failed('interrupted')，
  running 的 cases → failed。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from ulid import ULID

from ..db import database as db
from ..services import assembler, llm, progress
from ..services.sse import hub

logger = logging.getLogger(__name__)


class PipelineConflictError(RuntimeError):
    """流水线状态冲突（已在运行 / 未在等待输入 / step_key 不匹配等）；API 层映射 409。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class InteractionRequest:
    """门控步骤的人机交互请求（→ SSE interaction_required 事件）。"""

    kind: str                       # 'form' | 'confirm' | 'select' | 'review' | 前端扩展 kind
    schema: Any = None              # 表单/选择项 JSON Schema（前端渲染依据）
    prompt: str = ""                # 提示文案
    default: Any = None             # 默认值


@dataclass
class StepResult:
    """步骤执行结果；output 合并进 cases.state_json（工作内存）。"""

    output: dict[str, Any] = field(default_factory=dict)


StepHandler = Callable[["Ctx"], Awaitable["StepResult | dict[str, Any] | None"]]


@dataclass
class StepDef:
    """流水线步骤定义。"""

    key: str
    name_zh: str
    handler: StepHandler
    gate: str | None = None         # None | 'form' | 'confirm' | 'select' | 'review'
    retryable: bool = True


@dataclass
class _Pending:
    """挂起中的人机交互（内存态；重启即失，resume 时由步骤重跑重建）。"""

    step_key: str
    run_id: str
    interaction: InteractionRequest
    event: asyncio.Event = field(default_factory=asyncio.Event)
    payload: Any = None


# ---------------------------------------------------------------------------
# Ctx：步骤处理器上下文
# ---------------------------------------------------------------------------

class Ctx:
    """步骤 handler 的运行上下文。

    暴露：
    - case（案件行 dict 快照，reload_case() 刷新）与 state（工作内存 dict，步骤
      成功后由引擎持久化到 cases.state_json）；
    - emit(event, data, persist=True)：转 services/sse.hub 且默认落 messages；
    - llm / assets(assembler) / db：服务层模块直通；
    - run_group / step_key / attempt / gate / start_payload；
    - chat_delta / doc_delta / chat_done / doc_done 便捷流式方法；
    - await_user(interaction)：门控挂起，返回用户 payload。
    """

    def __init__(
        self,
        case_id: str,
        run_group: str,
        state: dict[str, Any],
        start_payload: Any,
        case_row: dict[str, Any],
    ) -> None:
        self.case_id = case_id
        self.run_group = run_group
        self.state = state
        self.start_payload = start_payload
        self.case: dict[str, Any] = case_row
        self.step_key: str | None = None
        self.attempt: int = 1
        self.gate: str | None = None
        # 服务层直通
        self.llm = llm
        self.assets = assembler
        self.db = db
        # 引擎每步注入的挂起回调
        self._await_user_cb: Callable[[InteractionRequest], Awaitable[Any]] | None = None

    @property
    def user_id(self) -> str:
        """案件属主 id（M8）。

        流水线跑在后台 asyncio 任务里，拿不到 request；属主写在 cases.user_id 上，
        步骤需要按用户计量/隔离时从这里取（LLM 用量已由 services/llm 自动归账）。
        """
        return str(self.case.get("user_id") or "")

    async def reload_case(self) -> dict[str, Any]:
        """从 DB 重读案件行（title/patent_type 等可能被其它接口修改）。"""
        row = await db.aquery_one("SELECT * FROM cases WHERE id=?", (self.case_id,))
        if row is not None:
            self.case = dict(row)
        return self.case

    async def emit(self, event: str, data: Any, *, persist: bool = True) -> int | None:
        """发 SSE 事件（默认持久化到 messages，作为重放源）。"""
        return await hub.emit(self.case_id, event, data, persist=persist, step_key=self.step_key)

    async def chat_delta(self, text: str) -> None:
        """会话叙述通道流式增量（高频，不落库；llm_done 时机由调用方掌握）。"""
        # 每个 token 都是「还活着」的实证：刷新进度的最后变化时间，
        # 否则一次 20 分钟的正常撰写会被心跳判成卡死
        progress.touch(self.case_id)
        await self.emit(
            "llm_delta", {"step_key": self.step_key, "channel": "chat", "text": text}, persist=False
        )

    async def doc_delta(self, doc_id: str, text: str) -> None:
        """文档正文通道流式增量（高频，不落库）。"""
        progress.touch(self.case_id)
        await self.emit(
            "llm_delta",
            {"step_key": self.step_key, "channel": "doc", "doc_id": doc_id, "text": text},
            persist=False,
        )

    async def progress(
        self,
        phase: str = "",
        *,
        index: int | None = None,
        total: int | None = None,
        detail: str = "",
        waiting_for: str = "",
    ) -> None:
        """报告步骤内进度并立即推给前端。

        `index/total` **只接受代码里真实存在的循环变量**（第 i 个文件、第 i 幅图、
        第 i 次调用）。不要按时间估算百分比：假进度条停在 90% 不动时，
        用户失去的是对整个系统的信任。给不出真实分子的步骤就别传这两个参数。

        `waiting_for` 写「当前在等谁」（模型 / 附图脚本 / 国知局 / Word），
        它是卡住提示里唯一能让用户据以行动的信息。
        """
        p = progress.current(self.case_id)
        if p is None:
            return
        if phase:
            p.phase = phase
        if index is not None:
            p.index = index
        if total is not None:
            p.total = total
        if waiting_for:
            p.waiting_for = waiting_for
        p.touch(detail)
        await self.emit("step_progress", p.snapshot(), persist=False)

    def tick(self, detail: str = "") -> None:
        """廉价地记一次「有进展」（不发事件，等心跳带出去）。"""
        progress.touch(self.case_id, detail)

    async def chat_done(self) -> None:
        """会话通道流式结束（落库，供重放判段落边界）。"""
        await self.emit("llm_done", {"step_key": self.step_key, "channel": "chat"})

    async def doc_done(self, doc_id: str) -> None:
        """文档通道流式结束。"""
        await self.emit("llm_done", {"step_key": self.step_key, "channel": "doc", "doc_id": doc_id})

    async def await_user(
        self,
        interaction: InteractionRequest | None = None,
        *,
        kind: str | None = None,
        schema: Any = None,
        prompt: str = "",
        default: Any = None,
    ) -> Any:
        """门控挂起：emit interaction_required → waiting_user → 等待用户输入 → 返回 payload。

        既可传现成 InteractionRequest，也可用关键字参数就地构造；kind 缺省取
        StepDef.gate（再缺省 'form'）。
        """
        if interaction is None:
            interaction = InteractionRequest(
                kind=kind or self.gate or "form", schema=schema, prompt=prompt, default=default
            )
        if self._await_user_cb is None:  # 理论上只在引擎外直接调用 handler 时发生
            raise RuntimeError("await_user 只能在流水线引擎调度的步骤内使用")
        return await self._await_user_cb(interaction)


# ---------------------------------------------------------------------------
# 模块级任务/挂起管理
# ---------------------------------------------------------------------------

_tasks: dict[str, asyncio.Task] = {}
_pending: dict[str, _Pending] = {}


def is_running(case_id: str) -> bool:
    """该案件是否有存活的流水线任务。"""
    task = _tasks.get(case_id)
    return task is not None and not task.done()


def get_pending(case_id: str) -> dict[str, Any] | None:
    """当前挂起的人机交互（→ state 端点 pending_interaction）。"""
    pend = _pending.get(case_id)
    if pend is None:
        return None
    return {
        "step_key": pend.step_key,
        "kind": pend.interaction.kind,
        "schema": pend.interaction.schema,
        "prompt": pend.interaction.prompt,
        "default": pend.interaction.default,
    }


def submit_input(case_id: str, step_key: str, payload: Any) -> None:
    """注入用户输入：校验 waiting_user 且 step_key 匹配后唤醒挂起的步骤协程。"""
    pend = _pending.get(case_id)
    if pend is None:
        raise PipelineConflictError("当前没有等待用户输入的步骤")
    if pend.step_key != step_key:
        raise PipelineConflictError(f"当前等待输入的步骤是 {pend.step_key}，而非 {step_key}")
    if pend.event.is_set():
        raise PipelineConflictError("该步骤的输入已提交，正在处理中")
    pend.payload = payload
    pend.event.set()


def start(
    case_id: str,
    steps: list[StepDef],
    run_group: str = "initial",
    start_payload: Any = None,
) -> asyncio.Task:
    """启动（或续跑）流水线后台任务；已在运行时抛 PipelineConflictError。

    已 done 的步骤（同 run_group）自动跳过，因此 resume/retry 也复用本入口。
    """
    if is_running(case_id):
        raise PipelineConflictError("该案件的流水线正在运行中")
    task = asyncio.create_task(
        run_pipeline(case_id, steps, run_group, start_payload), name=f"pipeline:{case_id}"
    )
    _tasks[case_id] = task
    return task


def cancel(case_id: str) -> None:
    """取消运行中的流水线（状态回写 cancelled，case 回 draft）。"""
    task = _tasks.get(case_id)
    if task is None or task.done():
        raise PipelineConflictError("该案件的流水线未在运行")
    task.cancel()


def recover_interrupted() -> dict[str, int]:
    """startup 恢复钩子：把上次进程死亡时遗留的 running 状态标记为 failed。

    - pipeline_runs.status='running' → failed('interrupted')；
    - cases.status='running' → failed；
    - waiting_user 的 run/case 保持原状（resume 重跑该步骤时会重发 interaction_required）。
    同步函数（startup 时经 anyio.to_thread 调用；测试可直接调）。
    """
    now = db.now_str()
    runs_failed = db.execute(
        "UPDATE pipeline_runs SET status='failed', error='interrupted', finished_at=? "
        "WHERE status='running'",
        (now,),
    )
    cases_failed = db.execute(
        "UPDATE cases SET status='failed', updated_at=? WHERE status='running'", (now,)
    )
    if runs_failed or cases_failed:
        logger.warning("启动恢复：中断的运行已标记 failed（runs=%s, cases=%s）", runs_failed, cases_failed)
    return {"runs_failed": runs_failed, "cases_failed": cases_failed}


def resolve_run_group(case_row: Mapping[str, Any]) -> str:
    """从案件行 state_json 读上次的 run_group（缺省 'initial'）。"""
    try:
        state = json.loads(case_row["state_json"] or "{}")
    except (KeyError, TypeError, json.JSONDecodeError):
        state = {}
    return state.get("_run_group") or "initial"


# ---------------------------------------------------------------------------
# 同步 DB helper（经 db.arun 进线程池；取消路径直接同步调用）
# ---------------------------------------------------------------------------

def _insert_run(run_id: str, case_id: str, run_group: str, step_key: str, attempt: int) -> None:
    db.execute(
        """
        INSERT INTO pipeline_runs(id, case_id, run_group, step_key, attempt, status, started_at)
        VALUES (?,?,?,?,?,'running',?)
        """,
        (run_id, case_id, run_group, step_key, attempt, db.now_str()),
    )


def _update_run(run_id: str, fields: dict[str, Any]) -> None:
    sets, params = [], []
    for k, v in fields.items():
        sets.append(f"{k}=?")
        params.append(v)
    if fields.get("status") in ("done", "failed", "cancelled", "skipped"):
        sets.append("finished_at=?")
        params.append(db.now_str())
    db.execute(f"UPDATE pipeline_runs SET {', '.join(sets)} WHERE id=?", (*params, run_id))


def _save_case(case_id: str, patch: dict[str, Any]) -> None:
    """按 patch 更新 cases 行（status / current_step / state 任意组合）。"""
    sets, params = ["updated_at=?"], [db.now_str()]
    if "status" in patch:
        sets.append("status=?")
        params.append(patch["status"])
    if "current_step" in patch:
        sets.append("current_step=?")
        params.append(patch["current_step"])
    if "state" in patch:
        sets.append("state_json=?")
        params.append(json.dumps(patch["state"], ensure_ascii=False))
    db.execute(f"UPDATE cases SET {', '.join(sets)} WHERE id=?", (*params, case_id))


def _latest_run(case_id: str, run_group: str, step_key: str):
    return db.query_one(
        "SELECT * FROM pipeline_runs WHERE case_id=? AND run_group=? AND step_key=? "
        "ORDER BY attempt DESC LIMIT 1",
        (case_id, run_group, step_key),
    )


def _cancel_active_runs(case_id: str, run_group: str) -> None:
    db.execute(
        "UPDATE pipeline_runs SET status='cancelled', finished_at=? "
        "WHERE case_id=? AND run_group=? AND status IN ('running','waiting_user')",
        (db.now_str(), case_id, run_group),
    )


# ---------------------------------------------------------------------------
# 流水线主循环
# ---------------------------------------------------------------------------

async def run_pipeline(
    case_id: str,
    steps: list[StepDef],
    run_group: str,
    start_payload: Any = None,
) -> None:
    """逐步执行流水线（asyncio 后台任务体；请经 start() 启动）。"""
    try:
        row = await db.aquery_one("SELECT * FROM cases WHERE id=?", (case_id,))
        if row is None:
            logger.error("run_pipeline：案件不存在 %s", case_id)
            return
        try:
            state: dict[str, Any] = json.loads(row["state_json"] or "{}")
        except json.JSONDecodeError:
            state = {}
        if start_payload is not None:
            state["_start_payload"] = start_payload
        state["_run_group"] = run_group
        # 档位在任务开头设一次，整棵调用树自动继承（见 services/llm 的说明）。
        # 放在这里而不是各步骤里：漏一处就会出现「个别步骤悄悄用了别的模型」，
        # 那是最难发现的一类不一致。
        llm.set_active_tier(state.get("_model_tier"))
        # 步骤元信息存 case（state 端点无需内存任务即可拼装步骤表）
        state["_steps"] = [
            {"key": s.key, "name_zh": s.name_zh, "gate": s.gate, "retryable": s.retryable}
            for s in steps
        ]
        await db.arun(_save_case, case_id, {"status": "running", "state": state})

        ctx = Ctx(case_id, run_group, state, state.get("_start_payload"), dict(row))
        failed = False
        for step in steps:
            latest = await db.arun(_latest_run, case_id, run_group, step.key)
            if latest is not None and latest["status"] == "done":
                continue  # resume/retry：已完成步骤跳过（state 已含其产物）
            attempt = (int(latest["attempt"]) + 1) if latest is not None else 1
            if not await _run_step(ctx, step, attempt):
                failed = True
                break

        if failed:
            await db.arun(_save_case, case_id, {"status": "failed"})
            await hub.emit(case_id, "pipeline_done", {"run_group": run_group, "status": "failed"})
        else:
            await db.arun(_save_case, case_id, {"status": "completed", "current_step": None})
            await hub.emit(case_id, "pipeline_done", {"run_group": run_group, "status": "done"})
    except asyncio.CancelledError:
        # 取消路径：同步回写（避免再次被取消打断），事件用独立任务补发
        _cancel_active_runs(case_id, run_group)
        _save_case(case_id, {"status": "draft"})
        try:
            asyncio.get_running_loop().create_task(
                hub.emit(case_id, "pipeline_done", {"run_group": run_group, "status": "cancelled"})
            )
        except RuntimeError:  # 事件循环已关闭
            pass
        raise
    except Exception as exc:  # noqa: BLE001 —— 引擎级兜底：绝不静默收场
        # 走到这里说明异常逃出了 _run_step 的保护范围（如 handler 返回了不支持的类型、
        # 或 DB/emit 本身失败）。必须把残留的 running 行收尾并补发终止事件，
        # 否则前端的步骤会永远停在 running 转圈、也等不到 pipeline_done。
        logger.exception("run_pipeline 引擎级异常 case=%s", case_id)
        msg = f"{type(exc).__name__}: {exc}"
        _save_case(case_id, {"status": "failed"})
        stuck = db.query_all(
            "SELECT step_key FROM pipeline_runs WHERE case_id=? AND run_group=? AND status='running'",
            (case_id, run_group),
        )
        db.execute(
            "UPDATE pipeline_runs SET status='failed', error=?, finished_at=? "
            "WHERE case_id=? AND run_group=? AND status='running'",
            (msg[:2000], db.now_str(), case_id, run_group),
        )
        error_data: dict[str, Any] = {"msg": msg, "message": msg, "retryable": False}
        if stuck:
            error_data["step_key"] = stuck[0]["step_key"]
        try:
            await hub.emit(case_id, "error", error_data, step_key=error_data.get("step_key"))
            await hub.emit(case_id, "pipeline_done", {"run_group": run_group, "status": "failed"})
        except Exception:  # noqa: BLE001 —— 收尾事件失败不再向上抛
            logger.exception("引擎级异常的收尾事件发送失败 case=%s", case_id)
    finally:
        _pending.pop(case_id, None)
        if _tasks.get(case_id) is asyncio.current_task():
            _tasks.pop(case_id, None)


async def _progress_heartbeat(case_id: str) -> None:
    """每 BEAT_SEC 推一次当前步骤的进度快照。

    这是「在跑还是卡死」唯一可靠的答案来源：只要心跳还在来，前端就知道进程活着；
    快照里的 idle_ms 又能区分「活着且在出活」与「活着但等了很久没反应」。
    没有它，一个 26 分钟的正常步骤和一个真的死掉的步骤，在界面上长得一模一样。
    """
    try:
        while True:
            await asyncio.sleep(progress.BEAT_SEC)
            snap = progress.snapshot(case_id)
            if snap is None:
                continue
            try:
                await hub.emit(
                    case_id, "step_progress", snap, persist=False, step_key=snap["step_key"]
                )
            except Exception:  # noqa: BLE001 —— 心跳发不出去不能拖垮步骤本身
                logger.debug("进度心跳发送失败 case=%s", case_id, exc_info=True)
    except asyncio.CancelledError:
        raise


async def _run_step(ctx: Ctx, step: StepDef, attempt: int) -> bool:
    """执行单个步骤（一次 attempt = 一行 pipeline_runs）；返回是否成功。"""
    case_id = ctx.case_id
    run_id = str(ULID())
    ctx.step_key, ctx.attempt, ctx.gate = step.key, attempt, step.gate
    progress.begin(case_id, step.key, step.name_zh)

    await db.arun(_insert_run, run_id, case_id, ctx.run_group, step.key, attempt)
    await db.arun(_save_case, case_id, {"status": "running", "current_step": step.key})
    await hub.emit(
        case_id,
        "step_status",
        {"step_key": step.key, "status": "running", "name_zh": step.name_zh, "attempt": attempt},
        step_key=step.key,
    )

    async def _await_user(interaction: InteractionRequest) -> Any:
        pend = _Pending(step_key=step.key, run_id=run_id, interaction=interaction)
        _pending[case_id] = pend
        # 等用户不是卡住，是设计好的停顿——心跳照发，但不报「无响应」
        if (p := progress.current(case_id)) is not None:
            p.suspended = True
            p.waiting_for = "你的确认"
            p.touch("等待人工确认")
        await db.arun(_update_run, run_id, {"status": "waiting_user"})
        await db.arun(_save_case, case_id, {"status": "waiting_user"})
        await hub.emit(
            case_id,
            "step_status",
            {"step_key": step.key, "status": "waiting_user", "attempt": attempt},
            step_key=step.key,
        )
        await hub.emit(
            case_id,
            "interaction_required",
            {
                "step_key": step.key,
                "kind": interaction.kind,
                "schema": interaction.schema,
                "prompt": interaction.prompt,
                "default": interaction.default,
            },
            step_key=step.key,
        )
        try:
            await pend.event.wait()
        finally:
            if _pending.get(case_id) is pend:
                _pending.pop(case_id, None)
        await db.arun(
            _update_run,
            run_id,
            {"status": "running", "user_input_json": json.dumps(pend.payload, ensure_ascii=False)},
        )
        await db.arun(_save_case, case_id, {"status": "running"})
        if (p := progress.current(case_id)) is not None:
            p.suspended = False
            p.waiting_for = ""
            p.touch("已收到确认，继续执行")
        await hub.emit(
            case_id,
            "step_status",
            {"step_key": step.key, "status": "running", "attempt": attempt},
            step_key=step.key,
        )
        return pend.payload

    ctx._await_user_cb = _await_user
    # 心跳在这里才建：上面那几步一旦抛异常就不会走到 finally，
    # 提前建会漏下一个仍在给已结束案件发心跳的任务
    beat = asyncio.create_task(_progress_heartbeat(case_id))
    try:
        result = await step.handler(ctx)
    except asyncio.CancelledError:
        raise  # 由 run_pipeline 统一回写 cancelled
    except Exception as exc:  # noqa: BLE001 —— 步骤失败不炸引擎
        msg = f"{type(exc).__name__}: {exc}"
        logger.exception("步骤失败 case=%s step=%s attempt=%s", case_id, step.key, attempt)
        await db.arun(_update_run, run_id, {"status": "failed", "error": msg[:2000]})
        await hub.emit(
            case_id,
            "step_status",
            {"step_key": step.key, "status": "failed", "attempt": attempt, "error": msg},
            step_key=step.key,
        )
        # 契约字段为 msg；附带 message 兼容前端既有 ErrorEvent 类型
        await hub.emit(
            case_id,
            "error",
            {"step_key": step.key, "msg": msg, "message": msg, "retryable": step.retryable},
            step_key=step.key,
        )
        return False
    finally:
        ctx._await_user_cb = None
        beat.cancel()
        progress.end(case_id, step.key)

    if isinstance(result, StepResult):
        output = result.output
    elif isinstance(result, dict):
        output = result
    elif result is None:
        output = {}
    else:
        raise TypeError(f"步骤 {step.key} 返回了不支持的类型 {type(result).__name__}")

    ctx.state.update(output)
    await db.arun(_save_case, case_id, {"state": ctx.state})
    await db.arun(
        _update_run, run_id, {"status": "done", "output_json": json.dumps(output, ensure_ascii=False)}
    )
    await hub.emit(
        case_id,
        "step_status",
        {"step_key": step.key, "status": "done", "attempt": attempt},
        step_key=step.key,
    )
    return True
