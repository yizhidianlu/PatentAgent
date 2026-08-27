"""设置页后端契约测试：api_key 掩码/保留语义 + 连接测试的请求体临时覆盖。

要点（backend-architecture.md §2 + §3.4）：
- GET/PUT 响应中的 api_key 一律掩码（sk-*** + 尾 4 位），明文永不回显；
- PUT 省略 / 空串 / 回传掩码 三种情况都表示"不修改已存 key"；
- POST /settings/{llm,embedding}/test 支持请求体临时覆盖（不落库），
  未填字段回落已存配置，回传的掩码 api_key 同样回落已存 key。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import settings as settings_api
from app.services import llm as llm_service

API = "/api/v1"
LLM_SECRET = "sk-llmrealkey0123456789abcd"
EMB_SECRET = "sk-embrealkey0123456789wxyz"
IMG_SECRET = "sk-imgrealkey0123456789efgh"


@pytest.fixture(scope="module")
def client(admin_client):
    """本模块全部是**平台级**设置（llm/embedding/image_gen/general），仅管理员可读写。"""
    return admin_client


@pytest.fixture
def stored_llm(client: TestClient) -> dict[str, Any]:
    """先存一份明文 LLM 配置作为基线。"""
    resp = client.put(
        f"{API}/settings/llm",
        json={
            "base_url": "https://api.example.com/v1",
            "api_key": LLM_SECRET,
            "model": "baseline-model",
            "temperature": 0.3,
            "max_output_tokens": 2048,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 掩码
# ---------------------------------------------------------------------------

def test_is_masked_key_helper() -> None:
    from app.models.settings import mask_api_key

    assert settings_api.is_masked_key(mask_api_key(LLM_SECRET)) is True
    assert settings_api.is_masked_key("sk-***abcd") is True
    assert settings_api.is_masked_key(LLM_SECRET) is False
    assert settings_api.is_masked_key("") is False
    assert settings_api.is_masked_key(None) is False


def test_get_and_put_mask_api_key(client: TestClient, stored_llm: dict[str, Any]) -> None:
    """PUT 响应与 GET 响应都只回掩码：sk-*** + 尾 4 位。"""
    assert stored_llm["api_key"] == "sk-***abcd"
    assert LLM_SECRET not in str(stored_llm)

    got = client.get(f"{API}/settings/llm")
    assert got.status_code == 200
    body = got.json()
    assert body["api_key"] == "sk-***abcd"
    assert body["model"] == "baseline-model"
    assert body["base_url"] == "https://api.example.com/v1"
    assert body["temperature"] == 0.3
    assert LLM_SECRET not in got.text

    # 明文确实落库了（掩码只发生在出站方向）
    assert llm_service.load_llm_settings().api_key == LLM_SECRET


# ---------------------------------------------------------------------------
# PUT 的 "不修改 api_key" 三种写法
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("label", "patch"),
    [
        ("省略字段", {}),
        ("空字符串", {"api_key": ""}),
        ("回传掩码", {"api_key": "sk-***abcd"}),
    ],
)
def test_put_keeps_stored_api_key(
    client: TestClient, stored_llm: dict[str, Any], label: str, patch: dict[str, Any]
) -> None:
    """省略 / 空串 / 掩码回传都必须保留已存明文 key，且其它字段照常更新。"""
    payload: dict[str, Any] = {
        "base_url": "https://api.example.com/v2",
        "model": f"model-{label}",
        **patch,
    }
    resp = client.put(f"{API}/settings/llm", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["api_key"] == "sk-***abcd"

    got = client.get(f"{API}/settings/llm").json()
    assert got["model"] == f"model-{label}"
    assert got["base_url"] == "https://api.example.com/v2"
    assert got["api_key"] == "sk-***abcd"
    # 关键：DB 里仍是原明文，没有被掩码/空串覆盖
    assert llm_service.load_llm_settings().api_key == LLM_SECRET


def test_put_replaces_api_key_when_new_plaintext_given(
    client: TestClient, stored_llm: dict[str, Any]
) -> None:
    """填了新的明文 key 则如实替换。"""
    resp = client.put(
        f"{API}/settings/llm",
        json={"base_url": "https://api.example.com/v1", "api_key": "sk-newkey000011112222",
              "model": "baseline-model"},
    )
    assert resp.status_code == 200
    assert resp.json()["api_key"] == "sk-***2222"
    assert llm_service.load_llm_settings().api_key == "sk-newkey000011112222"


def test_embedding_put_keeps_masked_key_and_flags_rebuild(client: TestClient) -> None:
    """Embedding 同一套语义；且 dim 变化时 need_rebuild=true。"""
    first = client.put(
        f"{API}/settings/embedding",
        json={"base_url": "https://emb.example.com/v1", "api_key": EMB_SECRET,
              "model": "emb-model", "dim": 1024},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["settings"]["api_key"] == "sk-***wxyz"
    assert body["need_rebuild"] is False          # 首次配置（原本无 model）不算变更
    assert EMB_SECRET not in first.text

    # 回传掩码 + 改 dim → key 保留、need_rebuild=true
    second = client.put(
        f"{API}/settings/embedding",
        json={"base_url": "https://emb.example.com/v1", "api_key": "sk-***wxyz",
              "model": "emb-model", "dim": 1536},
    )
    assert second.status_code == 200, second.text
    assert second.json()["need_rebuild"] is True
    assert second.json()["settings"]["dim"] == 1536
    assert llm_service.load_embedding_settings().api_key == EMB_SECRET

    got = client.get(f"{API}/settings/embedding").json()
    assert got["api_key"] == "sk-***wxyz" and got["dim"] == 1536


# ---------------------------------------------------------------------------
# 连接测试端点：请求体临时覆盖
# ---------------------------------------------------------------------------

def test_llm_test_uses_body_override(
    client: TestClient, stored_llm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """请求体里的临时配置应原样传给 llm 服务层，且不落库。"""
    captured: list[Any] = []

    async def fake_test_llm(override: dict[str, Any] | None = None):
        captured.append(override)
        from app.models.settings import LlmTestResult

        cfg = llm_service.load_llm_settings(override)
        return LlmTestResult(ok=True, model=cfg.model, latency_ms=1)

    monkeypatch.setattr(llm_service, "test_llm", fake_test_llm)

    resp = client.post(
        f"{API}/settings/llm/test",
        json={"base_url": "https://tmp.example.com/v1", "api_key": "sk-tmpkey999988887777",
              "model": "tmp-model", "temperature": 0.9},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "ok": True, "model": "tmp-model", "latency_ms": 1, "error": None,
        # 探测走真实 /v1/models，FakeLLM 场景下拿不到规格 → None
        "capability": None,
    }
    assert captured[-1] == {
        "base_url": "https://tmp.example.com/v1",
        "api_key": "sk-tmpkey999988887777",
        "model": "tmp-model",
        "temperature": 0.9,
    }
    # 临时覆盖绝不落库
    assert llm_service.load_llm_settings().model == "baseline-model"
    assert llm_service.load_llm_settings().api_key == LLM_SECRET


def test_llm_test_partial_override_falls_back_to_stored(
    client: TestClient, stored_llm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """只填 model 时，base_url/api_key 回落已存配置；掩码 api_key 不得覆盖真 key。"""
    seen: list[Any] = []

    async def fake_test_llm(override: dict[str, Any] | None = None):
        from app.models.settings import LlmTestResult

        cfg = llm_service.load_llm_settings(override)
        seen.append(cfg)
        return LlmTestResult(ok=True, model=cfg.model, latency_ms=2)

    monkeypatch.setattr(llm_service, "test_llm", fake_test_llm)

    assert client.post(f"{API}/settings/llm/test", json={"model": "probe-model"}).status_code == 200
    cfg = seen[-1]
    assert cfg.model == "probe-model"                      # 临时覆盖生效
    assert cfg.base_url == "https://api.example.com/v1"    # 未填字段回落
    assert cfg.api_key == LLM_SECRET

    # 前端把 GET 到的掩码原样回传 → 必须回落已存明文 key，而不是拿掩码去请求
    assert client.post(
        f"{API}/settings/llm/test", json={"model": "probe-model", "api_key": "sk-***abcd"}
    ).status_code == 200
    assert seen[-1].api_key == LLM_SECRET


def test_llm_test_without_body_uses_stored(
    client: TestClient, stored_llm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """不带请求体 → override=None，用已存配置。"""
    captured: list[Any] = []

    async def fake_test_llm(override: dict[str, Any] | None = None):
        from app.models.settings import LlmTestResult

        captured.append(override)
        return LlmTestResult(ok=True, model=llm_service.load_llm_settings(override).model)

    monkeypatch.setattr(llm_service, "test_llm", fake_test_llm)

    resp = client.post(f"{API}/settings/llm/test")
    assert resp.status_code == 200, resp.text
    assert captured == [None]
    assert resp.json()["model"] == "baseline-model"


def test_llm_test_reports_not_configured(client: TestClient) -> None:
    """未配置 model 时不发网络请求，直接返回 ok=false + 中文提示。"""
    client.put(f"{API}/settings/llm", json={"base_url": "", "api_key": "", "model": ""})
    resp = client.post(f"{API}/settings/llm/test", json={"model": ""})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "模型" in body["error"]


def test_embedding_test_uses_body_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Embedding 连接测试同样支持临时覆盖，并剔除掩码 api_key。"""
    seen: list[Any] = []

    async def fake_test_embedding(override: dict[str, Any] | None = None):
        from app.models.settings import EmbeddingTestResult

        seen.append(override)
        cfg = llm_service.load_embedding_settings(override)
        return EmbeddingTestResult(ok=True, model=cfg.model, dim=cfg.dim, latency_ms=3)

    monkeypatch.setattr(llm_service, "test_embedding", fake_test_embedding)

    resp = client.post(
        f"{API}/settings/embedding/test",
        json={"model": "tmp-emb", "api_key": "sk-***wxyz"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == "tmp-emb"
    assert seen[-1] == {"model": "tmp-emb"}                # 掩码 key 已剔除


def test_embedding_test_reports_not_configured(client: TestClient) -> None:
    client.put(f"{API}/settings/embedding", json={"base_url": "", "api_key": "", "model": "",
                                                  "dim": 1024})
    resp = client.post(f"{API}/settings/embedding/test", json={"model": ""})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    assert "Embedding" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Embedding：启用开关 / provider / 重建索引
# ---------------------------------------------------------------------------

def test_embedding_enabled_and_provider_roundtrip(client: TestClient) -> None:
    """新增的 enabled/provider 字段可往返；provider 越界 → 422。"""
    resp = client.put(
        f"{API}/settings/embedding",
        json={"enabled": True, "provider": "zhipu", "base_url": "https://open.bigmodel.cn/api/paas/v4",
              "api_key": EMB_SECRET, "model": "embedding-3", "dim": 2048},
    )
    assert resp.status_code == 200, resp.text
    got = client.get(f"{API}/settings/embedding").json()
    assert got["enabled"] is True
    assert got["provider"] == "zhipu"
    assert got["api_key"] == "sk-***wxyz"

    bad = client.put(
        f"{API}/settings/embedding",
        json={"enabled": True, "provider": "不存在的厂商", "base_url": "", "api_key": "",
              "model": "embedding-3", "dim": 2048},
    )
    assert bad.status_code == 422


def test_embedding_reindex(client: TestClient) -> None:
    """重建索引端点返回排队条数与 sqlite-vec 可用性，且不 5xx。"""
    resp = client.post(f"{API}/settings/embedding/reindex")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["queued"] >= 0
    assert isinstance(body["vec_available"], bool)
    assert body["message"]
    assert body["dim"] == client.get(f"{API}/settings/embedding").json()["dim"]


# ---------------------------------------------------------------------------
# 图像生成（image_gen）
# ---------------------------------------------------------------------------

def test_image_gen_crud_masks_and_keeps_key(client: TestClient) -> None:
    """image_gen 与 llm/embedding 同一套语义：掩码回显 + 空/掩码回传不改 key。"""
    empty = client.get(f"{API}/settings/image-gen")
    assert empty.status_code == 200, empty.text
    assert empty.json() == {
        "enabled": False, "provider": "custom", "base_url": "", "api_key": "",
        "model": "", "size": "1024x1024",
    }

    saved = client.put(
        f"{API}/settings/image-gen",
        json={"enabled": True, "provider": "zhipu", "base_url": "https://img.example.com/v1",
              "api_key": IMG_SECRET, "model": "cogview-3", "size": "512x512"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["api_key"] == "sk-***efgh"
    assert IMG_SECRET not in saved.text

    got = client.get(f"{API}/settings/image-gen").json()
    assert got["enabled"] is True and got["provider"] == "zhipu"
    assert got["model"] == "cogview-3" and got["size"] == "512x512"
    assert got["api_key"] == "sk-***efgh"
    assert llm_service.load_image_gen_settings().api_key == IMG_SECRET

    # 回传掩码 + 改模型 → key 保留
    again = client.put(
        f"{API}/settings/image-gen",
        json={"enabled": True, "provider": "zhipu", "base_url": "https://img.example.com/v1",
              "api_key": "sk-***efgh", "model": "cogview-4", "size": "512x512"},
    )
    assert again.status_code == 200, again.text
    assert client.get(f"{API}/settings/image-gen").json()["model"] == "cogview-4"
    assert llm_service.load_image_gen_settings().api_key == IMG_SECRET

    # 省略 api_key 亦不修改
    client.put(
        f"{API}/settings/image-gen",
        json={"enabled": False, "provider": "custom", "base_url": "https://img.example.com/v1",
              "model": "cogview-4", "size": "1024x1024"},
    )
    assert llm_service.load_image_gen_settings().api_key == IMG_SECRET
    assert client.get(f"{API}/settings/image-gen").json()["enabled"] is False


def test_image_gen_test_uses_body_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """测试出图支持表单未保存值的临时覆盖；掩码 key 回落已存明文，prompt 不污染配置。"""
    seen: list[Any] = []

    async def fake_test_image_gen(override: dict[str, Any] | None = None):
        from app.models.settings import ImageGenTestResult

        seen.append(override)
        cfg = llm_service.load_image_gen_settings(override)
        return ImageGenTestResult(ok=True, model=cfg.model, latency_ms=5,
                                  image_data_url="data:image/png;base64,AAAA")

    monkeypatch.setattr(llm_service, "test_image_gen", fake_test_image_gen)

    resp = client.post(
        f"{API}/settings/image-gen/test",
        json={"model": "tmp-image-model", "size": "256x256", "api_key": "sk-***efgh",
              "prompt": "黑白线条示意图"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["model"] == "tmp-image-model"
    assert body["image_data_url"].startswith("data:image/png;base64,")
    # 掩码 api_key 已剔除，prompt 原样透传给服务层
    assert seen[-1] == {"model": "tmp-image-model", "size": "256x256", "prompt": "黑白线条示意图"}
    # 临时覆盖不落库
    assert client.get(f"{API}/settings/image-gen").json()["model"] == "cogview-4"

    # prompt 不得被并入配置对象
    cfg = llm_service.load_image_gen_settings(seen[-1])
    assert cfg.size == "256x256" and not hasattr(cfg, "prompt")
    assert cfg.api_key == IMG_SECRET


def test_image_gen_test_reports_not_configured(client: TestClient) -> None:
    """未配置模型时不发网络请求，返回 ok=false + 中文提示。"""
    client.put(
        f"{API}/settings/image-gen",
        json={"enabled": False, "provider": "custom", "base_url": "", "api_key": "", "model": "",
              "size": "1024x1024"},
    )
    resp = client.post(f"{API}/settings/image-gen/test", json={"model": ""})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    assert "图像" in resp.json()["error"]


# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------

def test_general_settings_roundtrip(client: TestClient) -> None:
    resp = client.put(
        f"{API}/settings/general",
        json={"output_dir": "", "browser_channel": "msedge", "pdf_engine": "soffice",
              "language": "zh"},
    )
    assert resp.status_code == 200, resp.text
    got = client.get(f"{API}/settings/general").json()
    assert got["browser_channel"] == "msedge"
    assert got["pdf_engine"] == "soffice"

    # 枚举外的值 → 422
    bad = client.put(
        f"{API}/settings/general",
        json={"output_dir": "", "browser_channel": "firefox", "pdf_engine": "auto",
              "language": "zh"},
    )
    assert bad.status_code == 422
