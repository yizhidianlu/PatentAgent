"""LLM 客户端封装（OpenAI 兼容）。

能力（backend-architecture.md §1.1 services/llm.py + §6 风险 3）：
- 配置从 DB settings 表（key='llm'）读取，可用 override 临时覆盖（测试端点用）；
- chat()：非流式，返回完整文本；
- chat_stream()：async 生成器逐 token 产出（SSE 直推用）；
- structured()：json_object 能力位 + 提示词双保险 → 提取首个 JSON 块 → pydantic 校验 → 带错重试 1 次；
- 每次调用记 llm_calls 表（token 用量、耗时、状态）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import base64
import time
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError
from ulid import ULID

from ..db import database as db
from ..models.settings import (
    EmbeddingSettings,
    EmbeddingTestResult,
    ImageGenSettings,
    ImageGenTestResult,
    LlmSettings,
    LlmTestResult,
    ModelCapability,
    load_tolerant,
)
from .sse import hub
from . import progress

logger = logging.getLogger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)

# 默认请求超时（秒）。推理型模型「先想几十秒再开口」，非流式调用在服务端出结果前
# 一个字节都不会回来 —— 按 120s 卡会把正常的长思考请求判死，故单列一档更宽的超时。
DEFAULT_TIMEOUT = 300
REASONING_TIMEOUT = 900
TEST_TIMEOUT = 30
# 出图比对话慢一到两个数量级：文生图从提交到返回常见 20-120 秒，4K 或排队时更久。
# 拿 TEST_TIMEOUT(30s) 去测图像模型，结果是「配置明明正确却永远超时」——
# 而超时的报错长得跟网络不通一样，看不出真正原因。
IMAGE_TIMEOUT = 180


class LlmNotConfiguredError(RuntimeError):
    """LLM 尚未配置（缺 model 或 api_key/base_url）。"""


class LlmEmptyOutputError(RuntimeError):
    """模型返回了空正文（推理型模型最常见：token 预算被思维链吃光）。"""


async def _notify(
    case_id: str | None,
    message: str,
    *,
    persist: bool = True,
    step_key: str | None = None,
    kind: str | None = None,
) -> None:
    """向案件事件流补一条 log（无 case_id 时跳过；发送失败绝不影响主流程）。

    没有它，限流退避与长思维链期间前端只有一个转圈的步骤卡，用户无从判断系统是在
    等配额、在思考、还是已经卡死。kind='progress' 标记「同一件事的滚动进度」，
    前端可就地刷新同一张卡片而不是每条新增一张。
    """
    if not case_id:
        return
    # 限流退避、长思维链心跳都是「确实在动」的证据：刷新进度的最后变化时间，
    # 否则一次 5 分钟的思考期会被流水线心跳判成卡死
    progress.touch(case_id, message if kind == "progress" else "")
    data: dict[str, Any] = {"message": message}
    if kind:
        data["kind"] = kind
    try:
        await hub.emit(case_id, "log", data, persist=persist, step_key=step_key)
    except Exception:  # noqa: BLE001 —— 进度提示失败不能拖垮真正的调用
        logger.debug("进度事件发送失败 case=%s", case_id, exc_info=True)


def load_llm_settings(override: dict[str, Any] | None = None) -> LlmSettings:
    """从 DB 读 LLM 配置并合并临时覆盖（override 中的 None/空 api_key 不生效）。"""
    stored = db.get_setting_json("llm") or {}
    cfg = load_tolerant(LlmSettings, stored)
    if override:
        patch = {k: v for k, v in override.items() if v is not None}
        if not patch.get("api_key"):
            patch.pop("api_key", None)
        cfg = cfg.model_copy(update=patch)
    return cfg


def load_embedding_settings(override: dict[str, Any] | None = None) -> EmbeddingSettings:
    """从 DB 读 Embedding 配置并合并临时覆盖。"""
    stored = db.get_setting_json("embedding") or {}
    cfg = load_tolerant(EmbeddingSettings, stored)
    if override:
        patch = {k: v for k, v in override.items() if v is not None}
        if not patch.get("api_key"):
            patch.pop("api_key", None)
        cfg = cfg.model_copy(update=patch)
    return cfg


def load_image_gen_settings(override: dict[str, Any] | None = None) -> ImageGenSettings:
    """从 DB 读图像生成配置并合并临时覆盖。"""
    stored = db.get_setting_json("image_gen") or {}
    cfg = load_tolerant(ImageGenSettings, stored)
    if override:
        patch = {k: v for k, v in override.items() if v is not None}
        if not patch.get("api_key"):
            patch.pop("api_key", None)
        known = set(ImageGenSettings.model_fields)  # 丢掉 prompt 等非配置字段
        cfg = cfg.model_copy(update={k: v for k, v in patch.items() if k in known})
    return cfg


def _client(base_url: str, api_key: str, timeout: float = DEFAULT_TIMEOUT) -> AsyncOpenAI:
    """构造 OpenAI 兼容客户端；base_url 为空则用官方默认。"""
    return AsyncOpenAI(
        base_url=base_url or None,
        api_key=api_key or "EMPTY",   # 部分本地端点不校验 key，但 SDK 要求非空
        timeout=timeout,
        max_retries=0,                # 重试策略由上层控制
    )


# ---------------------------------------------------------------------------
# 模型参数兼容层（quirks）
#
# 不同厂商对 chat.completions 的参数支持并不一致，最常见的三类差异：
#   1. 推理型模型只接受 temperature=1（kimi-k3、OpenAI o1/o3/gpt-5 系列）；
#   2. 要求用 max_completion_tokens 取代 max_tokens（同上）；
#   3. 不支持 response_format=json_object。
#   4. 推理型模型的思维链与正文**共享** max_tokens 预算，按「正文预算」发请求会
#      让思维链把额度吃光、正文为空（finish_reason='length'）。
# 与其维护一张易过时的模型白名单，不如从真实报错/真实响应里**学**：首次撞墙后解析
# 错误消息、剥离/改名对应参数并立刻重试，学到的结论按模型缓存，后续调用直接绕开。
# ---------------------------------------------------------------------------

_MODEL_QUIRKS: dict[str, dict[str, Any]] = {}

# 推理型模型的思维链预留额度：实测 kimi-k3 写 200 字正文要先想 1100+ tokens，
# 长文档任务的思维链能到上万 tokens，故按「正文预算 + 保留额」下发。
REASONING_RESERVE_TOKENS = 32768
REASONING_MAX_BUDGET = 98304

# 学到的 quirks 落 settings 表：进程重启后不必再拿一次真实请求去撞 400 换结论
_QUIRKS_SETTING_KEY = "llm_quirks"
_quirks_loaded = False


def _load_quirks_once() -> None:
    """从 settings 表补齐已持久化的 quirks（只做一次，失败静默）。"""
    global _quirks_loaded
    if _quirks_loaded:
        return
    _quirks_loaded = True
    try:
        stored = db.get_setting_json(_QUIRKS_SETTING_KEY) or {}
    except Exception:  # noqa: BLE001 —— DB 未就绪时按空表处理
        logger.debug("读取 %s 失败", _QUIRKS_SETTING_KEY, exc_info=True)
        return
    for model, quirks in stored.items():
        if isinstance(quirks, dict):
            merged = dict(quirks)
            merged.update(_MODEL_QUIRKS.get(model) or {})
            _MODEL_QUIRKS[model] = merged


def _persist_quirks() -> None:
    """把当前 quirks 写回 settings（失败不影响主流程）。"""
    try:
        db.set_setting_json(_QUIRKS_SETTING_KEY, _MODEL_QUIRKS)
    except Exception:  # noqa: BLE001
        logger.debug("持久化 %s 失败", _QUIRKS_SETTING_KEY, exc_info=True)


def reasoning_budget(model: str, want: int | None) -> int:
    """推理型模型的实际 max_tokens：正文预算 + 思维链保留额（有下限与上限）。"""
    quirks = _MODEL_QUIRKS.get(model) or {}
    floor = int(quirks.get("reasoning_budget") or 0)
    base = int(want or 0) + REASONING_RESERVE_TOKENS
    return min(max(base, floor, REASONING_RESERVE_TOKENS), REASONING_MAX_BUDGET)


def _apply_quirks(model: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """按已学到的 quirks 调整请求参数。"""
    _load_quirks_once()
    quirks = _MODEL_QUIRKS.get(model)
    if not quirks:
        return kwargs
    if quirks.get("no_temperature"):
        kwargs.pop("temperature", None)
    if quirks.get("reasoning") and kwargs.get("max_tokens") is not None:
        kwargs["max_tokens"] = reasoning_budget(model, kwargs["max_tokens"])
    alias = quirks.get("max_tokens_alias")
    if alias:
        value = kwargs.pop("max_tokens", None)
        if value is not None:
            kwargs[alias] = value
    if quirks.get("no_response_format"):
        kwargs.pop("response_format", None)
    if quirks.get("no_stream_options"):
        kwargs.pop("stream_options", None)
    return kwargs


def _is_reasoning_model(model: str) -> bool:
    """该模型是否已被判定为推理型（会吐思维链）。"""
    _load_quirks_once()
    return bool((_MODEL_QUIRKS.get(model) or {}).get("reasoning"))


def _call_timeout(model: str) -> float:
    """推理型模型用更宽的超时（思维链动辄几十秒到几分钟）。"""
    return REASONING_TIMEOUT if _is_reasoning_model(model) else DEFAULT_TIMEOUT


def _note_reasoning(model: str) -> bool:
    """标记该模型为推理型；返回 True 表示这是**新**结论（值得按新预算重试一次）。"""
    _load_quirks_once()
    quirks = _MODEL_QUIRKS.setdefault(model, {})
    if quirks.get("reasoning"):
        return False
    quirks["reasoning"] = True
    logger.info("模型 %s 判定为推理型（响应含思维链），后续调用按 %s tokens 追加预算",
                model, REASONING_RESERVE_TOKENS)
    _persist_quirks()
    return True


def _escalate_reasoning_budget(model: str) -> bool:
    """思维链仍把预算吃光时把预算下限拉到上限档；返回 True 表示确实抬高了。"""
    quirks = _MODEL_QUIRKS.setdefault(model, {})
    if int(quirks.get("reasoning_budget") or 0) >= REASONING_MAX_BUDGET:
        return False
    quirks["reasoning"] = True
    quirks["reasoning_budget"] = REASONING_MAX_BUDGET
    logger.warning("模型 %s 思维链仍超预算，max_tokens 上调至 %s", model, REASONING_MAX_BUDGET)
    _persist_quirks()
    return True


def _reasoning_text(delta: Any) -> str:
    """从流式 delta 里取思维链增量（各家字段名不一，OpenAI SDK 归到 model_extra）。"""
    for attr in ("reasoning_content", "reasoning"):
        value = getattr(delta, attr, None)
        if isinstance(value, str) and value:
            return value
    extra = getattr(delta, "model_extra", None) or {}
    for key in ("reasoning_content", "reasoning"):
        value = extra.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _used_reasoning(resp: Any) -> bool:
    """非流式响应是否用了思维链（usage.reasoning_tokens 或 message.reasoning_content）。"""
    details = getattr(getattr(resp, "usage", None), "completion_tokens_details", None)
    if int(getattr(details, "reasoning_tokens", 0) or 0) > 0:
        return True
    try:
        message = resp.choices[0].message
    except (AttributeError, IndexError):
        return False
    return bool(_reasoning_text(message))


def _learn_quirk(model: str, exc: Exception) -> bool:
    """从报错里学习模型 quirk；返回 True 表示学到新结论、值得重试一次。"""
    message = str(exc).lower()
    # 仅对参数类错误（400）学习；鉴权/额度/网络错误重试无意义
    if "400" not in message and "invalid_request" not in message and "unsupported" not in message:
        return False
    quirks = _MODEL_QUIRKS.setdefault(model, {})
    learned = False
    if "temperature" in message and not quirks.get("no_temperature"):
        quirks["no_temperature"] = True
        learned = True
    if "max_completion_tokens" in message and not quirks.get("max_tokens_alias"):
        quirks["max_tokens_alias"] = "max_completion_tokens"
        learned = True
    elif "max_tokens" in message and "unsupported" in message and not quirks.get("max_tokens_alias"):
        quirks["max_tokens_alias"] = "max_completion_tokens"
        learned = True
    if "response_format" in message and not quirks.get("no_response_format"):
        quirks["no_response_format"] = True
        learned = True
    if "stream_options" in message and not quirks.get("no_stream_options"):
        quirks["no_stream_options"] = True
        learned = True
    if learned:
        logger.info("模型 %s 参数兼容性已调整：%s", model, quirks)
        _persist_quirks()
    return learned


def model_quirks(model: str) -> dict[str, Any]:
    """供设置页/自检展示当前已探测到的模型兼容性调整。"""
    return dict(_MODEL_QUIRKS.get(model) or {})


# ---------------------------------------------------------------------------
# 限流退避
#
# 低档位账户的 RPM 往往只有个位数（实测 Moonshot 免费档 RPM=3），而一条流水线
# 每步都有多次调用，不退避必然大面积 429 失败。这里统一做指数退避重试，并优先
# 采纳服务端给出的 Retry-After / "try again after N seconds" 提示。
# ---------------------------------------------------------------------------

# 实测除了账户 RPM 之外，还会连续撞上服务端的 engine_overloaded（与本方配额无关的
# 全局过载），一次能持续好几分钟。整条流水线本就要跑十几分钟，与其让某一步炸掉、
# 逼用户从头再来，不如把耐心放宽到 ~4 分钟。
RATE_LIMIT_MAX_RETRY = 8
RATE_LIMIT_BASE_DELAY = 3.0
RATE_LIMIT_MAX_DELAY = 60.0   # 低配额账户（如 RPM=3）需要跨过整个分钟窗口

# 流**开始输出正文之前**被掐断时可以整条重开（不会重复内容）的最大次数。
# 推理型模型一条流要活几分钟，中途被服务端断开实测并不罕见。
STREAM_RESTART_MAX = 3

# 服务端「我放弃了」型的收尾原因：连接没断、也没报错，但 finish_reason 表明这次
# 生成是被服务端中止的，正文一个字都没有（实测 Moonshot 在长思维链期间过载会回
# finish_reason='engine_overloaded'）。这类结果不能当成模型的正常输出，按断流重开。
ABORT_FINISH_REASONS = frozenset(
    {"engine_overloaded", "server_busy", "overloaded", "network_error", "error", "abort", "aborted"}
)

_RETRY_AFTER_RE = re.compile(r"try again after\s+([0-9]+(?:\.[0-9]+)?)\s*second", re.IGNORECASE)


def _is_rate_limited(exc: Exception) -> bool:
    """判定是否为限流/过载类可重试错误。"""
    if getattr(exc, "status_code", None) == 429:
        return True
    message = str(exc).lower()
    return any(
        token in message
        for token in ("429", "rate_limit", "rate limit", "too many requests",
                      "engine_overloaded", "overloaded", "server_busy")
    )


def _is_transient(exc: Exception) -> bool:
    """连接类瞬时故障：推理型模型一次调用要几分钟，中途被掐断并不罕见。"""
    name = type(exc).__name__
    if name in ("APIConnectionError", "APITimeoutError", "InternalServerError",
                "ConnectError", "ReadTimeout", "ReadError", "RemoteProtocolError"):
        return True
    if getattr(exc, "status_code", None) in (500, 502, 503, 504):
        return True
    message = str(exc).lower()
    return any(
        token in message
        for token in ("connection error", "connection reset", "connection aborted",
                      "timed out", "timeout", "temporarily unavailable",
                      "bad gateway", "service unavailable")
    )


def _is_retryable(exc: Exception) -> bool:
    """限流 + 连接瞬时故障统一按可重试处理。"""
    return _is_rate_limited(exc) or _is_transient(exc)


def _retry_delay(exc: Exception, attempt: int) -> float:
    """本次退避秒数 = max(服务端提示, 指数退避)，上限 RATE_LIMIT_MAX_DELAY。

    服务端给的 "try again after 1 seconds" 常是乐观值：RPM=3 的配额实际要等满
    一个分钟窗口，照它等只会立刻再撞一次。故取两者较大值，逐次拉长。
    """
    backoff = RATE_LIMIT_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
    hinted = 0.0
    match = _RETRY_AFTER_RE.search(str(exc))
    if match:
        try:
            hinted = float(match.group(1)) + 0.5
        except ValueError:
            hinted = 0.0
    header = getattr(getattr(exc, "response", None), "headers", None)
    if header:
        raw = header.get("retry-after") or header.get("Retry-After")
        if raw:
            try:
                hinted = max(hinted, float(raw) + 0.5)
            except (TypeError, ValueError):
                pass
    return min(max(backoff, hinted), RATE_LIMIT_MAX_DELAY)


async def _with_rate_limit_retry(
    factory: Callable[[], Awaitable[Any]],
    *,
    what: str,
    case_id: str | None = None,
    step_key: str | None = None,
) -> Any:
    """执行请求；遇限流则退避重试，其余异常原样抛出。

    退避期间向案件事件流发一条 log —— 否则前端只有一个转圈的步骤卡，用户看到的是
    「点了没反应」，而实际上系统正在按服务端要求等待配额窗口。
    """
    last: Exception | None = None
    for attempt in range(RATE_LIMIT_MAX_RETRY):
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001
            if not _is_retryable(exc):
                raise
            last = exc
            if attempt == RATE_LIMIT_MAX_RETRY - 1:
                break
            limited = _is_rate_limited(exc)
            delay = _retry_delay(exc, attempt)
            reason = "触发限流" if limited else "连接中断"
            logger.warning(
                "%s %s（第 %d/%d 次），%.1fs 后重试：%s",
                what, reason, attempt + 1, RATE_LIMIT_MAX_RETRY, delay, str(exc)[:160],
            )
            await _notify(
                case_id,
                f"模型接口{'触发限流' if limited else '连接中断'}"
                f"（第 {attempt + 1}/{RATE_LIMIT_MAX_RETRY} 次），"
                f"将在 {delay:.0f} 秒后自动重试，请稍候……",
                step_key=step_key,
            )
            await asyncio.sleep(delay)
    assert last is not None
    raise last


def _case_owner_sync(case_id: str | None) -> str:
    """案件属主（LLM 用量要计到发起人头上；流水线跑在后台任务里，拿不到 request）。"""
    if not case_id:
        return ""
    try:
        row = db.query_one("SELECT user_id FROM cases WHERE id=?", (case_id,))
    except Exception:  # noqa: BLE001
        return ""
    return str(row["user_id"]) if row and row["user_id"] else ""


def _record_call(
    *,
    case_id: str | None,
    step_key: str | None,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    duration_ms: int,
    status: str,
    error: str | None = None,
    user_id: str | None = None,
) -> None:
    """向 llm_calls 表记一笔调用账（失败也记），并把用量累加到用户配额上。

    `user_id` 缺省时回落到案件属主 —— 流水线在后台 asyncio 任务里跑，拿不到
    request，但案件行上有属主，据此归账最稳（少一处需要手工传参的遗漏面）。
    """
    try:
        db.execute(
            """
            INSERT INTO llm_calls(id, case_id, step_key, model, prompt_tokens, completion_tokens,
                                  duration_ms, status, error, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (str(ULID()), case_id, step_key, model, prompt_tokens, completion_tokens,
             duration_ms, status, error, db.now_str()),
        )
    except Exception:  # noqa: BLE001 —— 记账失败不影响主流程
        logger.exception("llm_calls 记账失败")

    try:
        owner = (user_id or "").strip() or _case_owner_sync(case_id)
        if owner:
            from . import auth as auth_service

            auth_service.bump_usage(
                owner, calls=1, tokens=int(prompt_tokens or 0) + int(completion_tokens or 0)
            )
    except Exception:  # noqa: BLE001 —— 配额计数失败同样不影响主流程
        logger.exception("LLM 用量计入用户失败：case=%s", case_id)


# 长思维链期间的「还活着」心跳间隔（秒）：首拍 20s，之后逐次翻倍至 120s 封顶。
# 推理型模型的正文可能几分钟后才开始吐，全程无反馈用户会以为程序卡死。
REASONING_HEARTBEAT_SEC = 20.0
REASONING_HEARTBEAT_MAX_SEC = 120.0


def _empty_output_hint(model: str, finish: Any) -> str:
    """空正文的可执行错误文案（直接呈现给用户，需说清怎么办）。"""
    if finish == "length":
        return (
            f"模型 {model} 未产出任何正文：输出长度已达上限（finish_reason=length），"
            f"额度被思维链占满。请到设置页把「最大输出 tokens」调大（推理型模型建议 ≥ "
            f"{REASONING_RESERVE_TOKENS}），或改用非推理型模型。"
        )
    if str(finish or "").lower() in ABORT_FINISH_REASONS:
        return (
            f"模型服务端持续过载：{model} 连续 {STREAM_RESTART_MAX} 次在生成中途被中止"
            f"（finish_reason={finish}），一个字正文都没产出。这不是本地问题，"
            "请过几分钟点「重试」，或在设置页换一个模型。"
        )
    return (
        f"模型 {model} 返回了空正文（finish_reason={finish}）。"
        "请检查模型是否可用、提示词是否触发了内容过滤，或改用其它模型重试。"
    )


def _require_configured(cfg: LlmSettings) -> None:
    """校验必要配置；缺失抛 LlmNotConfiguredError。"""
    if not cfg.model:
        raise LlmNotConfiguredError("尚未配置 LLM 模型，请先在设置页填写 model")
    if not cfg.api_key and not cfg.base_url:
        raise LlmNotConfiguredError("尚未配置 API Key 或 base_url，请先在设置页完成配置")


async def chat(
    messages: list[dict[str, str]],
    *,
    case_id: str | None = None,
    step_key: str | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    override: dict[str, Any] | None = None,
    response_format: dict | None = None,
    user_id: str | None = None,
) -> str:
    """非流式 chat：返回完整回复文本；调用记账进 llm_calls 并计入用户配额。

    推理型模型例外：改走流式再缓冲成整段。原因是这类模型一次调用要思考几分钟，
    非流式请求期间连接上一个字节都不流动，中间网络设备会把这种空闲连接掐掉
    （实测报 APIConnectionError）；流式下思维链分片持续到达，连接始终活跃。
    """
    cfg = load_llm_settings(override)
    _require_configured(cfg)
    if _is_reasoning_model(cfg.model):
        parts: list[str] = []
        async for piece in chat_stream(
            messages,
            case_id=case_id, step_key=step_key,
            temperature=temperature, max_output_tokens=max_output_tokens,
            override=override, response_format=response_format, user_id=user_id,
        ):
            parts.append(piece)
        return "".join(parts)   # 记账已在 chat_stream 内完成，此处不重复
    client = _client(cfg.base_url, cfg.api_key, _call_timeout(cfg.model))
    started = time.perf_counter()
    try:
        def _build() -> dict[str, Any]:
            kw: dict[str, Any] = dict(
                model=cfg.model,
                messages=messages,
                temperature=cfg.temperature if temperature is None else temperature,
                max_tokens=cfg.max_output_tokens if max_output_tokens is None else max_output_tokens,
            )
            if response_format is not None:
                kw["response_format"] = response_format
            return _apply_quirks(cfg.model, kw)

        async def _send() -> Any:
            return await client.chat.completions.create(**_build())

        async def _send_learning() -> Any:
            try:
                return await _with_rate_limit_retry(
                    _send, what=f"chat({cfg.model})", case_id=case_id, step_key=step_key
                )
            except Exception as exc:  # noqa: BLE001 —— 仅在学到参数 quirk 时重试一次
                if not _learn_quirk(cfg.model, exc):
                    raise
                return await _with_rate_limit_retry(
                    _send, what=f"chat({cfg.model})", case_id=case_id, step_key=step_key
                )

        # 推理型模型的思维链与正文共享预算：正文空 + finish_reason=length 时
        # 加大预算再来一次（而不是把空串当成功回给上层）
        for budget_attempt in range(2):
            resp = await _send_learning()
            choice = resp.choices[0] if getattr(resp, "choices", None) else None
            content = (getattr(getattr(choice, "message", None), "content", None) or "")
            finish = getattr(choice, "finish_reason", None)
            if content.strip():
                break
            grew = False
            if _used_reasoning(resp):
                grew = _note_reasoning(cfg.model)
                if not grew and finish == "length":
                    grew = _escalate_reasoning_budget(cfg.model)
            if not (grew and budget_attempt == 0):
                break
            logger.warning("chat(%s) 正文为空（finish=%s），加大 token 预算后重试", cfg.model, finish)
            await _notify(
                case_id, "模型的思维链占满了本次输出额度，正在加大额度重试……", step_key=step_key
            )

        if not content.strip():
            raise LlmEmptyOutputError(_empty_output_hint(cfg.model, finish))
        duration = int((time.perf_counter() - started) * 1000)
        usage = getattr(resp, "usage", None)
        _record_call(
            case_id=case_id, step_key=step_key, model=cfg.model,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            duration_ms=duration, status="ok", user_id=user_id,
        )
        return content
    except Exception as exc:
        duration = int((time.perf_counter() - started) * 1000)
        _record_call(
            case_id=case_id, step_key=step_key, model=cfg.model,
            prompt_tokens=None, completion_tokens=None,
            duration_ms=duration, status="error", error=str(exc)[:2000],
            user_id=user_id,
        )
        raise


async def chat_stream(
    messages: list[dict[str, str]],
    *,
    case_id: str | None = None,
    step_key: str | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    override: dict[str, Any] | None = None,
    response_format: dict | None = None,
    user_id: str | None = None,
) -> AsyncIterator[str]:
    """流式 chat：async 生成器逐 token 产出增量文本；结束（或异常）时记账。"""
    cfg = load_llm_settings(override)
    _require_configured(cfg)
    client = _client(cfg.base_url, cfg.api_key, _call_timeout(cfg.model))
    started = time.perf_counter()
    status, error = "ok", None
    prompt_tokens = completion_tokens = None
    try:
        def _build() -> dict[str, Any]:
            kw: dict[str, Any] = dict(
                model=cfg.model,
                messages=messages,
                temperature=cfg.temperature if temperature is None else temperature,
                max_tokens=cfg.max_output_tokens if max_output_tokens is None else max_output_tokens,
                stream=True,
                # 流式默认不回 usage；显式索要，否则推理型模型（走本函数缓冲的 chat()）
                # 的 token 用量全记 None，用户配额只能按「次数」计，统计不出真实消耗
                stream_options={"include_usage": True},
            )
            if response_format is not None:
                kw["response_format"] = response_format
            return _apply_quirks(cfg.model, kw)

        async def _open_stream() -> Any:
            return await client.chat.completions.create(**_build())

        async def _open_learning() -> Any:
            try:
                return await _with_rate_limit_retry(
                    _open_stream, what=f"chat_stream({cfg.model})",
                    case_id=case_id, step_key=step_key,
                )
            except Exception as exc:  # noqa: BLE001 —— 仅在学到参数 quirk 时重试一次
                if not _learn_quirk(cfg.model, exc):
                    raise
                return await _with_rate_limit_retry(
                    _open_stream, what=f"chat_stream({cfg.model})",
                    case_id=case_id, step_key=step_key,
                )

        # 只要**还没吐出过正文**，重开一条流永远是安全的（不会重复内容）。两类故障
        # 都靠这个不变量兜底：① 思维链吃光预算导致正文为空 → 加大预算重来；
        # ② 长思考期间流被中途掐断 → 原样重来。一旦已经 yield 过正文就不能重开
        # （无法续传），只能把异常上抛，由用户决定是否重试该步。
        budget_grown = 0
        restarts = 0
        while True:
            stream = await _open_learning()
            got_content = False
            reasoning_chars = 0
            finish = None
            beat_at = time.perf_counter()
            beat_gap = REASONING_HEARTBEAT_SEC
            try:
                async for chunk in stream:
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:   # include_usage 的收尾帧（choices 为空）
                        prompt_tokens = getattr(usage, "prompt_tokens", None)
                        completion_tokens = getattr(usage, "completion_tokens", None)
                    if not getattr(chunk, "choices", None):
                        continue
                    choice = chunk.choices[0]
                    if getattr(choice, "finish_reason", None):
                        finish = choice.finish_reason
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    if getattr(delta, "content", None):
                        got_content = True
                        yield delta.content
                        continue
                    thought = _reasoning_text(delta)
                    if thought:
                        reasoning_chars += len(thought)
                        now = time.perf_counter()
                        if not got_content and now - beat_at >= beat_gap:
                            beat_at = now
                            # 间隔逐次拉长：开头及时报活，长思考时不刷屏
                            beat_gap = min(beat_gap * 2, REASONING_HEARTBEAT_MAX_SEC)
                            await _notify(
                                case_id,
                                f"模型正在推理（已思考 {int(now - started)} 秒 / 约 "
                                f"{reasoning_chars} 字），正文尚未开始输出……",
                                persist=False, step_key=step_key, kind="progress",
                            )
            except Exception as exc:  # noqa: BLE001 —— 流中途断开
                if got_content or restarts >= STREAM_RESTART_MAX or not _is_retryable(exc):
                    raise
                restarts += 1
                delay = _retry_delay(exc, restarts - 1)
                logger.warning(
                    "chat_stream(%s) 输出流中断（第 %d/%d 次，思维链 %s 字），%.1fs 后重开：%s",
                    cfg.model, restarts, STREAM_RESTART_MAX, reasoning_chars, delay, str(exc)[:160],
                )
                await _notify(
                    case_id,
                    f"模型输出流中断（第 {restarts}/{STREAM_RESTART_MAX} 次），"
                    f"将在 {delay:.0f} 秒后重新生成，请稍候……",
                    step_key=step_key,
                )
                await asyncio.sleep(delay)
                continue
            if got_content:
                return
            # 服务端中止（而非模型正常写完）→ 与断流同等对待，整条重开
            if str(finish or "").lower() in ABORT_FINISH_REASONS and restarts < STREAM_RESTART_MAX:
                restarts += 1
                delay = _retry_delay(RuntimeError(str(finish)), restarts - 1)
                logger.warning(
                    "chat_stream(%s) 被服务端中止（finish=%s，思维链 %s 字），%.1fs 后重新生成",
                    cfg.model, finish, reasoning_chars, delay,
                )
                await _notify(
                    case_id,
                    f"模型服务端过载并中止了本次生成（第 {restarts}/{STREAM_RESTART_MAX} 次），"
                    f"将在 {delay:.0f} 秒后重新生成，请稍候……",
                    step_key=step_key,
                )
                await asyncio.sleep(delay)
                continue
            grew = False
            if reasoning_chars:
                grew = _note_reasoning(cfg.model)
                if not grew and finish == "length":
                    grew = _escalate_reasoning_budget(cfg.model)
            if not grew or budget_grown >= 1:
                raise LlmEmptyOutputError(_empty_output_hint(cfg.model, finish))
            budget_grown += 1
            logger.warning(
                "chat_stream(%s) 正文为空（finish=%s，思维链 %s 字），加大 token 预算后重试",
                cfg.model, finish, reasoning_chars,
            )
            await _notify(
                case_id, "模型的思维链占满了本次输出额度，正在加大额度重试……", step_key=step_key
            )
    except Exception as exc:
        status, error = "error", str(exc)[:2000]
        raise
    finally:
        duration = int((time.perf_counter() - started) * 1000)
        # token 数来自 stream_options.include_usage 的收尾帧；端点不支持时仍为 None
        _record_call(
            case_id=case_id, step_key=step_key, model=cfg.model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            duration_ms=duration, status=status, error=error, user_id=user_id,
        )


# ---------------------------------------------------------------------------
# 结构化输出
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)


def extract_first_json(text: str) -> Any:
    """提取文本中首个合法 JSON 值：优先 ```json 围栏块，其次首个平衡的 {...} / [...]。"""
    for match in _JSON_FENCE_RE.finditer(text):
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(text[i:])
                return value
            except json.JSONDecodeError:
                continue
    raise json.JSONDecodeError("未在模型输出中找到合法 JSON", text[:200], 0)


async def structured(
    messages: list[dict[str, str]],
    model_cls: type[TModel],
    *,
    case_id: str | None = None,
    step_key: str | None = None,
    temperature: float = 0.1,
    max_output_tokens: int | None = None,
    override: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> TModel:
    """结构化输出调用。

    策略（§6 风险 3）：json_object 能力位 + 提示词双保险 → 提取首个 JSON 块 →
    pydantic 校验 → 失败带 ValidationError 反馈重试 1 次。
    """
    cfg = load_llm_settings(override)
    _require_configured(cfg)
    schema = json.dumps(model_cls.model_json_schema(), ensure_ascii=False)
    guard = (
        "你必须只输出一个 JSON 对象（可用 ```json 围栏包裹），不得输出任何解释性文字。"
        f"输出必须符合以下 JSON Schema：\n{schema}"
    )
    work = list(messages) + [{"role": "system", "content": guard}]
    response_format = {"type": "json_object"} if cfg.supports_json_mode else None

    last_error: Exception | None = None
    for attempt in range(2):  # 首次 + 带错重试 1 次
        try:
            text = await chat(
                work,
                case_id=case_id, step_key=step_key,
                temperature=temperature, max_output_tokens=max_output_tokens,
                override=override, response_format=response_format, user_id=user_id,
            )
        except LlmEmptyOutputError:
            raise  # 空正文与 response_format 无关，去掉它重发只会白烧一次额度
        except Exception as exc:  # noqa: BLE001
            # 限流/连接类故障与 response_format 无关（chat() 内部已按退避重试过），
            # 再借"降级"的名义重发一遍只会把日志写歪、并把重试预算悄悄翻倍
            if _is_retryable(exc):
                raise
            # 部分兼容端点不接受 response_format：去掉后立刻重发一次（不占用纠错重试机会）
            if response_format is not None:
                logger.warning("response_format=json_object 被端点拒绝，降级为纯提示词约束：%s", exc)
                response_format = None
                text = await chat(
                    work,
                    case_id=case_id, step_key=step_key,
                    temperature=temperature, max_output_tokens=max_output_tokens,
                    override=override, user_id=user_id,
                )
            else:
                raise
        try:
            data = extract_first_json(text)
            return model_cls.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            if attempt == 0:
                # 带错误反馈重试一次
                work = work + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": (
                        "上面的输出未通过 JSON 校验，错误如下：\n"
                        f"{exc}\n请修正后重新只输出一个符合 Schema 的 JSON 对象。"
                    )},
                ]
    raise ValueError(f"结构化输出两次均未通过校验：{last_error}")


# ---------------------------------------------------------------------------
# 连接测试（设置页用）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 模型能力探测
#
# OpenAI 兼容服务商多在 /v1/models 暴露上下文窗口，但字段名各家不一
# （context_length / context_window / max_context_length …），逐一兼容。
# 输出上限鲜有明示，探测不到时按上下文给一个保守推荐值并标记 estimated。
# ---------------------------------------------------------------------------

_CONTEXT_KEYS = (
    "context_length", "context_window", "max_context_length",
    "max_context_tokens", "max_input_tokens", "context_size",
)
_OUTPUT_KEYS = (
    "max_output_tokens", "max_completion_tokens", "max_tokens",
    "max_output", "completion_token_limit",
)


def _pick_int(payload: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    """从模型描述里取第一个合法正整数字段（含 top_provider 等常见嵌套）。"""
    scopes: list[dict[str, Any]] = [payload]
    for nested in ("top_provider", "architecture", "limits", "capabilities"):
        v = payload.get(nested)
        if isinstance(v, dict):
            scopes.append(v)
    for scope in scopes:
        for k in keys:
            v = scope.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
            if isinstance(v, str) and v.isdigit() and int(v) > 0:
                return int(v)
    return None


def recommend_output_tokens(context_length: int, *, reasoning: bool) -> int:
    """服务商未明示输出上限时的推荐值。

    取上下文的 1/8 并封顶：留足输入余量，又不至于让长文生成频繁被截断。
    推理模型的思维链同样计入输出，故给更高的下限。
    """
    guess = max(4096, context_length // 8)
    cap = 65536 if reasoning else 32768
    return int(min(guess, cap))


async def probe_model_capability(override: dict[str, Any] | None = None) -> ModelCapability | None:
    """查询服务商 /v1/models，提取当前模型的上下文与输出上限。

    任何失败都返回 None —— 这只是锦上添花的能力，绝不能拖垮连接测试。
    """
    cfg = load_llm_settings(override)
    if not cfg.model:
        return None
    base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT) as client:
            resp = await client.get(
                f"{base}/models",
                headers={"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {},
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("模型能力探测跳过（%s）：%s", cfg.model, str(exc)[:120])
        return None

    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return None
    entry = next((m for m in items if isinstance(m, dict) and m.get("id") == cfg.model), None)
    if entry is None:
        return None

    ctx = _pick_int(entry, _CONTEXT_KEYS)
    out = _pick_int(entry, _OUTPUT_KEYS)
    reasoning = bool(entry.get("supports_reasoning") or entry.get("reasoning"))
    estimated = False
    if out is None and ctx:
        out = recommend_output_tokens(ctx, reasoning=reasoning)
        estimated = True
    if ctx is None and out is None:
        return None
    return ModelCapability(
        context_length=ctx,
        max_output_tokens=out,
        supports_reasoning=reasoning or None,
        estimated=estimated,
    )


async def test_llm(override: dict[str, Any] | None = None) -> LlmTestResult:
    """用当前（或请求体临时）配置发一次最小 chat，返回 {ok, model, latency_ms, error}。"""
    try:
        cfg = load_llm_settings(override)
        _require_configured(cfg)
    except LlmNotConfiguredError as exc:
        return LlmTestResult(ok=False, error=str(exc))
    client = _client(cfg.base_url, cfg.api_key, timeout=TEST_TIMEOUT)
    started = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=cfg.model,
            messages=[{"role": "user", "content": "你好"}],
            max_tokens=1,
        )
        latency = int((time.perf_counter() - started) * 1000)
        model_name = getattr(resp, "model", None) or cfg.model
        _record_call(case_id=None, step_key="settings.llm_test", model=cfg.model,
                     prompt_tokens=None, completion_tokens=None,
                     duration_ms=latency, status="ok")
        cap = await probe_model_capability(override)
        return LlmTestResult(ok=True, model=model_name, latency_ms=latency, capability=cap)
    except Exception as exc:  # noqa: BLE001
        latency = int((time.perf_counter() - started) * 1000)
        _record_call(case_id=None, step_key="settings.llm_test", model=cfg.model,
                     prompt_tokens=None, completion_tokens=None,
                     duration_ms=latency, status="error", error=str(exc)[:2000])
        return LlmTestResult(ok=False, model=cfg.model, latency_ms=latency, error=str(exc))


DEFAULT_IMAGE_TEST_PROMPT = "专利附图风格：黑白线条示意图，白底，无阴影无文字，简单的方框与箭头"


class ImageGenUnavailableError(RuntimeError):
    """图像生成未启用或未配置——调用方据此走「只给提示词」的降级路径。"""


async def generate_image(
    prompt: str,
    *,
    case_id: str | None = None,
    step_key: str | None = None,
) -> bytes:
    """按提示词出一张图，返回 PNG 字节。

    与 test_image_gen 的区别：这是生产路径，出图会进交付物，所以
      * 未启用/未配置时抛 ImageGenUnavailableError，由调用方决定如何降级；
      * 端点只给 URL 时会把图拉回来——交付物不能依赖一个随时会失效的外链。
    """
    cfg = load_image_gen_settings()
    if not cfg.enabled:
        raise ImageGenUnavailableError("图像生成未启用")
    if not cfg.model:
        raise ImageGenUnavailableError("尚未配置图像生成模型")

    client = _client(cfg.base_url, cfg.api_key, timeout=IMAGE_TIMEOUT)
    started = time.perf_counter()
    resp = await client.images.generate(
        model=cfg.model, prompt=prompt, n=1, size=cfg.size or "1024x1024",
    )
    latency = int((time.perf_counter() - started) * 1000)

    item = resp.data[0] if getattr(resp, "data", None) else None
    if not item:
        raise RuntimeError("图像端点未返回任何数据")

    b64 = getattr(item, "b64_json", None)
    data: bytes
    if b64:
        data = base64.b64decode(b64)
    else:
        url = getattr(item, "url", None)
        if not url:
            raise RuntimeError("图像端点既未返回 url 也未返回 b64_json")
        # 落地保存，不留外链：交付物要能长期打开
        async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT) as http:
            r = await http.get(url)
            r.raise_for_status()
            data = r.content

    if not data:
        raise RuntimeError("图像端点返回了空内容")
    await _notify(case_id, f"出图完成（{cfg.model}，{latency}ms，{len(data) // 1024} KB）", step_key=step_key)
    return data



async def test_image_gen(override: dict[str, Any] | None = None) -> ImageGenTestResult:
    """用当前（或临时）配置发一次最小 images 请求，返回可预览的缩略图或错误。

    出图会真实消耗额度，因此仅在设置页「测试出图」显式触发。
    """
    prompt = (override or {}).get("prompt") or DEFAULT_IMAGE_TEST_PROMPT
    cfg = load_image_gen_settings(override)
    if not cfg.model:
        return ImageGenTestResult(ok=False, error="尚未配置图像生成模型")
    client = _client(cfg.base_url, cfg.api_key, timeout=IMAGE_TIMEOUT)
    started = time.perf_counter()
    try:
        resp = await client.images.generate(
            model=cfg.model, prompt=prompt, n=1, size=cfg.size or "1024x1024",
        )
        latency = int((time.perf_counter() - started) * 1000)
        item = resp.data[0] if getattr(resp, "data", None) else None
        b64 = getattr(item, "b64_json", None) if item else None
        url = getattr(item, "url", None) if item else None
        if not b64 and not url:
            return ImageGenTestResult(
                ok=False, model=cfg.model, latency_ms=latency,
                error="端点未返回图片数据（既无 url 也无 b64_json）",
            )
        return ImageGenTestResult(
            ok=True, model=cfg.model, latency_ms=latency, image_url=url,
            image_data_url=f"data:image/png;base64,{b64}" if b64 else None,
        )
    except Exception as exc:  # noqa: BLE001
        latency = int((time.perf_counter() - started) * 1000)
        msg = str(exc)
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            msg = (
                f"出图超时（已等待 {IMAGE_TIMEOUT} 秒）。该模型可能排队较久或不支持当前尺寸；"
                "可换一个更快的图像模型、把尺寸调小后重试。"
            )
        return ImageGenTestResult(ok=False, model=cfg.model, latency_ms=latency, error=msg)


async def test_embedding(override: dict[str, Any] | None = None) -> EmbeddingTestResult:
    """用当前（或临时）配置 embed("测试")，返回 {ok, dim, latency_ms, error}。"""
    cfg = load_embedding_settings(override)
    if not cfg.model:
        return EmbeddingTestResult(ok=False, error="尚未配置 Embedding 模型")
    client = _client(cfg.base_url, cfg.api_key, timeout=TEST_TIMEOUT)
    started = time.perf_counter()
    try:
        resp = await client.embeddings.create(model=cfg.model, input="测试")
        latency = int((time.perf_counter() - started) * 1000)
        dim = len(resp.data[0].embedding) if resp.data else None
        return EmbeddingTestResult(ok=True, model=cfg.model, dim=dim, latency_ms=latency)
    except Exception as exc:  # noqa: BLE001
        latency = int((time.perf_counter() - started) * 1000)
        return EmbeddingTestResult(ok=False, model=cfg.model, latency_ms=latency, error=str(exc))
