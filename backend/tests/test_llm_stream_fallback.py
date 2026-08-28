# -*- coding: utf-8 -*-
"""非流式调用被掐 → 就地改走流式并记住：解开一个「学习需要成功，成功需要先学会」的死锁。

现场形态（glm-5.3-flash 配上当天真实发生）：思维链模型静默思考 ~110 秒，
非流式请求零字节流动，被中间设备当空闲连接掐掉 → 重试仍是非流式 → 再被掐 →
「模型接口连接中断（第 N/8 次）」循环十几分钟后失败。

解药（缓冲流式）在代码里早已存在，但通往它的判断 `_is_reasoning_model()` 要靠
一次成功响应来学习——而连接总在响应之前死掉。所以连接类故障本身必须被当作证据。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.db import database as db_mod
from app.services import llm

TEST_MODEL = "stall-prone-model"
OVERRIDE = {"base_url": "https://fake.example/v1", "api_key": "sk-fake", "model": TEST_MODEL}


class APIConnectionError(Exception):
    """按类名判定（llm._is_transient 认 type(exc).__name__）——与 openai SDK 同名。"""


@pytest.fixture(autouse=True)
def _clean_quirks():
    db_mod.init_db()
    llm._load_quirks_once()
    yield
    llm._MODEL_QUIRKS.pop(TEST_MODEL, None)
    llm._persist_quirks()


def _chunk(content: str | None = None, finish: str | None = None, usage=None):
    delta = SimpleNamespace(content=content, model_extra=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish)
    return SimpleNamespace(choices=[choice] if (content or finish) else [], usage=usage)


class FakeStream:
    """最小可用的流：两片正文 + 收尾帧 + usage 帧。"""

    def __init__(self, pieces: list[str]):
        self._chunks = (
            [_chunk(content=p) for p in pieces]
            + [_chunk(finish="stop"),
               _chunk(usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5))]
        )

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class FakeClient:
    """非流式一律被「掐」；流式正常。计数供断言「第二次直接走流式」。"""

    def __init__(self, pieces: list[str], nonstream_exc: Exception | None = None):
        self.nonstream_calls = 0
        self.stream_calls = 0
        self._pieces = pieces
        self._exc = nonstream_exc

    @property
    def chat(self):
        outer = self

        class _Completions:
            async def create(self, **kw):
                if kw.get("stream"):
                    outer.stream_calls += 1
                    return FakeStream(outer._pieces)
                outer.nonstream_calls += 1
                if outer._exc is not None:
                    raise outer._exc
                msg = SimpleNamespace(content="".join(outer._pieces))
                choice = SimpleNamespace(message=msg, finish_reason="stop")
                return SimpleNamespace(choices=[choice], usage=None)

        return SimpleNamespace(completions=_Completions())


@pytest.mark.anyio
async def test_first_transient_switches_to_streaming_and_remembers(monkeypatch) -> None:
    """被掐一次 → 本次就改走流式拿到结果，且结论持久化。"""
    fake = FakeClient(["专利", "交底书"], nonstream_exc=APIConnectionError("Connection error."))
    monkeypatch.setattr(llm, "_client", lambda *a, **k: fake)

    text = await llm.chat([{"role": "user", "content": "hi"}], override=OVERRIDE)

    assert text == "专利交底书", "改道后必须拿到完整正文，而不是把异常抛给用户"
    assert fake.nonstream_calls == 1, "非流式只该试一次——同样的连接故障重试不会有不同结果"
    assert fake.stream_calls == 1
    assert llm.model_quirks(TEST_MODEL).get("stream_chat") is True, "结论必须被记住"


@pytest.mark.anyio
async def test_second_call_skips_the_doomed_nonstream_attempt(monkeypatch) -> None:
    """学到之后，后续调用不再白付一次注定失败的非流式尝试。"""
    llm._MODEL_QUIRKS[TEST_MODEL] = {"stream_chat": True}
    fake = FakeClient(["好"], nonstream_exc=APIConnectionError("would fail"))
    monkeypatch.setattr(llm, "_client", lambda *a, **k: fake)

    text = await llm.chat([{"role": "user", "content": "hi"}], override=OVERRIDE)

    assert text == "好"
    assert fake.nonstream_calls == 0, "已标记 stream_chat 的模型不该再发非流式请求"


@pytest.mark.anyio
async def test_non_transient_error_does_not_learn_stream_chat(monkeypatch) -> None:
    """400 类错误与「连接被掐」无关：改走流式解决不了它，不许据此打标。"""
    fake = FakeClient(["x"], nonstream_exc=ValueError("Invalid request: bad schema"))
    monkeypatch.setattr(llm, "_client", lambda *a, **k: fake)

    with pytest.raises(ValueError):
        await llm.chat([{"role": "user", "content": "hi"}], override=OVERRIDE)
    assert "stream_chat" not in llm.model_quirks(TEST_MODEL)


@pytest.mark.anyio
async def test_transient_budget_bails_after_one_attempt(monkeypatch) -> None:
    """transient_budget=1：连接故障立即交还控制权，不做 8 次注定失败的退避。"""
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise APIConnectionError("Connection error.")

    with pytest.raises(APIConnectionError):
        await llm._with_rate_limit_retry(factory, what="test", transient_budget=1)
    assert calls["n"] == 1, "预算 1 意味着第一次失败就该 raise，而不是继续退避"


@pytest.mark.anyio
async def test_rate_limit_retries_are_not_affected_by_transient_budget(monkeypatch) -> None:
    """限流重试等的是配额窗口，永远值得等——小 transient 预算不得波及它。"""
    # llm.asyncio 就是全局 asyncio：patch 前先抓住真 sleep，否则替身会调到自己
    real_sleep = asyncio.sleep

    async def instant(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(llm.asyncio, "sleep", instant)
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("429 too many requests")
        return "ok"

    result = await llm._with_rate_limit_retry(factory, what="test", transient_budget=1)
    assert result == "ok"
    assert calls["n"] == 3
