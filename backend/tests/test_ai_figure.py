# -*- coding: utf-8 -*-
"""AI 补图的失败边界。

出图是增强能力，不该有本事让主流程失败、也不该让它挂住——
这组用例锁住「任何异常都只降级」「每案有上限」「不留孤儿文件」三条。
"""
import struct
import zlib

import pytest

from app.services import drawings, llm as llm_service


def _png(width: int = 64, height: int = 48) -> bytes:
    """造一张最小的合法 PNG（只需 IHDR 能被解析）。"""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
    chunk += struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
    return sig + chunk + b"\x00" * 32


CONTENT = {
    "drawings": ["图1：系统结构框图，包括采集模块与计算模块。"],
    "drawing_assets": [],
}


@pytest.fixture(autouse=True)
def _enable_ai_figure(raw_client):
    """这组用例验的是「开着的时候怎么工作」。

    ai_figure 默认关闭（它按图像模型计费，不替用户做主），
    而技能开关现在真的会拦住调用——不显式打开，下面每条都会在第一行就返回 False。
    """
    from app.db import database as db
    from app.services import skills

    skills.set_enabled("ai_figure", True)
    yield
    db.execute("DELETE FROM settings WHERE key='skills'")


@pytest.fixture
def content():
    import copy

    return copy.deepcopy(CONTENT)


async def test_unavailable_is_silent(monkeypatch, content):
    """未启用图像生成时静默返回 False，不抛异常。"""
    async def fake(*a, **k):
        raise llm_service.ImageGenUnavailableError("未启用")

    monkeypatch.setattr(llm_service, "generate_image", fake)
    assert await drawings.try_ai_figure("case-x", content, 1) is False
    assert content["drawing_assets"] == []


async def test_api_error_does_not_propagate(monkeypatch, content):
    """端点报错只降级，异常不能冒到主流程。"""
    async def fake(*a, **k):
        raise RuntimeError("upstream 503")

    monkeypatch.setattr(llm_service, "generate_image", fake)
    assert await drawings.try_ai_figure("case-x", content, 1) is False


async def test_invalid_png_does_not_propagate(monkeypatch, content):
    """返回的不是 PNG 时同样只降级。"""
    async def fake(*a, **k):
        return b"this is not a png"

    monkeypatch.setattr(llm_service, "generate_image", fake)
    assert await drawings.try_ai_figure("case-x", content, 1) is False


async def test_disk_failure_does_not_propagate(monkeypatch, content):
    """落盘失败（磁盘满/权限）不能让整条流水线挂掉。"""
    async def fake(*a, **k):
        return _png()

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(llm_service, "generate_image", fake)
    monkeypatch.setattr(drawings.Path, "write_bytes", boom)
    assert await drawings.try_ai_figure("case-x", content, 1) is False


async def test_per_case_limit(monkeypatch, content):
    """每案有上限——整体失败分支会对每张图各调一次，没上限会串成几十分钟。"""
    calls = {"n": 0}

    async def fake(*a, **k):
        calls["n"] += 1
        return _png()

    monkeypatch.setattr(llm_service, "generate_image", fake)
    content["drawings"] = [f"图{i}：示意图。" for i in range(1, 9)]
    ok = [await drawings.try_ai_figure("case-limit", content, i) for i in range(1, 9)]

    assert calls["n"] == drawings.MAX_AI_FIGURES
    assert sum(ok) == drawings.MAX_AI_FIGURES
    assert ok[drawings.MAX_AI_FIGURES] is False


async def test_failed_attempt_counts_toward_limit(monkeypatch, content):
    """失败也计入配额，否则一直失败就会一直重试。"""
    calls = {"n": 0}

    async def fake(*a, **k):
        calls["n"] += 1
        raise RuntimeError("timeout")

    monkeypatch.setattr(llm_service, "generate_image", fake)
    content["drawings"] = [f"图{i}：示意图。" for i in range(1, 9)]
    for i in range(1, 9):
        await drawings.try_ai_figure("case-fail", content, i)
    assert calls["n"] == drawings.MAX_AI_FIGURES


def test_malformed_figure_no_does_not_crash():
    """已有 asset 里混入非数字 figure_no 时不该抛 ValueError。"""
    assert drawings._figure_no_of({"figure_no": "abc"}) == 0
    assert drawings._figure_no_of({"figure_no": None}) == 0
    assert drawings._figure_no_of({"figure_no": "3"}) == 3
    assert drawings._figure_no_of("not a dict") == 0


def test_svg_is_self_contained():
    """SVG 外壳内嵌 PNG：单独拿出去看也不该是裂图。"""
    svg = drawings._svg_wrapper(_png(120, 90), 120, 90)
    assert "data:image/png;base64," in svg
    assert 'width="120"' in svg and 'height="90"' in svg
    assert "xlink:href=\"figure" not in svg     # 不再引用外部相对路径
