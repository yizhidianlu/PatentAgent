# -*- coding: utf-8 -*-
"""search_progress 事件的字段契约。

前端 sessionStore 的分支第一句是 `if (!d?.message) return`——字段名对不上时
事件被**静默丢弃**，没有报错、没有日志，界面上就是什么都不出现。
国知局检索的滚动进度曾经整条链路都是哑的，而 oa 那条带了 message 所以正常，
两处不一致更难被发现。这组用例把字段名钉死。
"""
import asyncio

import pytest

from app.services import cnipa
from app.services.sse import hub


@pytest.fixture
def captured(monkeypatch):
    """截获 hub.emit 的调用参数。"""
    calls: list[dict] = []

    async def fake_emit(case_id, event, data=None, *, step_key=None, persist=True, **kw):
        calls.append(
            {"case_id": case_id, "event": event, "data": data,
             "step_key": step_key, "persist": persist}
        )

    monkeypatch.setattr(hub, "emit", fake_emit)
    return calls


def test_progress_uses_message_field(captured):
    """必须是 message —— 前端只认这个名字。"""
    cb = cnipa.hub_progress("case-1", step_key="prior_art_search")
    asyncio.run(cb("crawl", "正在检索第 2/8 组检索式"))

    assert len(captured) == 1
    data = captured[0]["data"]
    assert "message" in data, "字段名必须是 message，否则前端静默丢弃"
    assert data["message"] == "正在检索第 2/8 组检索式"


def test_progress_is_not_persisted(captured):
    """滚动进度不落库：UI 是同一行原地更新，落库会让重放时铺开几十条。"""
    cb = cnipa.hub_progress("case-1")
    asyncio.run(cb("crawl", "第 1 组"))
    assert captured[0]["persist"] is False


def test_progress_keeps_stage_as_phase(captured):
    """阶段信息保留在 phase 里——前端类型定义有这个可选字段。"""
    cb = cnipa.hub_progress("case-1")
    asyncio.run(cb("parse", "正在解析命中条目"))
    assert captured[0]["data"].get("phase") == "parse"


def test_event_name_matches_frontend(captured):
    cb = cnipa.hub_progress("case-1")
    asyncio.run(cb("crawl", "x"))
    assert captured[0]["event"] == "search_progress"
