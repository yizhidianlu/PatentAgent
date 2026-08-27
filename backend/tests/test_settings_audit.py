# -*- coding: utf-8 -*-
"""平台级设置的变更必须留痕，且留痕里不能有密钥。

这四个端点改的是全平台共用的模型服务与 API Key，此前一条审计都不写——
谁在什么时候把 base_url 指到了别处、把图像生成打开了，事后无从查证，
而同一张 audit_log 里连一次普通登录都记着。

另一半同样重要：审计日志的查看门槛比设置页低（管理员都能翻历史），
把密钥写进去等于给它开第二个出口。所以只留「是否被改过」这个布尔。
"""
import json

from fastapi.testclient import TestClient

from app.db import database as db

API = "/api/v1"
SECRET = "sk-audittest-do-not-leak-9876543210"


def _audit_rows(action: str = "settings_updated") -> list[dict]:
    # 按 rowid 排序而非 created_at：后者精度只到秒，同一秒内写入的多条
    # 分不出先后，取「最新一条」会随机拿到上一个用例留下的行。
    rows = db.query_all(
        "SELECT * FROM audit_log WHERE action=? ORDER BY rowid DESC LIMIT 20", (action,)
    )
    return [dict(r) for r in rows]


def test_llm_update_is_audited(admin_client: TestClient) -> None:
    r = admin_client.put(
        f"{API}/settings/llm",
        json={"base_url": "https://audit.example.com/v1", "api_key": SECRET,
              "model": "audit-model", "temperature": 0.3,
              "max_output_tokens": 4096, "context_window": 131072},
    )
    assert r.status_code == 200, r.text

    rows = [x for x in _audit_rows() if x.get("target_id") == "llm"]
    assert rows, "保存 LLM 配置必须留痕"
    detail = json.loads(rows[0]["detail_json"] or "{}")
    assert detail.get("model") == "audit-model"
    assert detail.get("api_key_changed") is True


def test_audit_never_contains_the_key(admin_client: TestClient) -> None:
    """整张审计表里都不该出现密钥原文。"""
    admin_client.put(
        f"{API}/settings/llm",
        json={"base_url": "https://audit.example.com/v1", "api_key": SECRET,
              "model": "audit-model", "temperature": 0.3,
              "max_output_tokens": 4096, "context_window": 131072},
    )
    everything = db.query_all("SELECT * FROM audit_log")
    blob = " ".join(str(dict(r)) for r in everything)
    assert SECRET not in blob, "审计日志里绝不能出现 API Key 原文"
    assert "audittest" not in blob, "密钥片段也不行"


def test_unchanged_key_is_not_reported_as_changed(admin_client: TestClient) -> None:
    """只改模型名、不动密钥时，不该记成「密钥被改过」。"""
    base = {"base_url": "https://audit.example.com/v1", "api_key": SECRET,
            "model": "m1", "temperature": 0.3,
            "max_output_tokens": 4096, "context_window": 131072}
    admin_client.put(f"{API}/settings/llm", json=base)

    # 第二次不带 api_key —— 按契约表示「不修改」
    second = dict(base, model="m2")
    second.pop("api_key")
    r = admin_client.put(f"{API}/settings/llm", json=second)
    assert r.status_code == 200, r.text

    rows = [x for x in _audit_rows() if x.get("target_id") == "llm"]
    detail = json.loads(rows[0]["detail_json"] or "{}")
    assert detail.get("model") == "m2"
    assert detail.get("api_key_changed") is False


def test_image_gen_update_is_audited(admin_client: TestClient) -> None:
    r = admin_client.put(
        f"{API}/settings/image-gen",
        json={"enabled": True, "provider": "custom",
              "base_url": "https://img.example.com/v1", "api_key": SECRET,
              "model": "img-model", "size": "1024x1024"},
    )
    assert r.status_code == 200, r.text
    rows = [x for x in _audit_rows() if x.get("target_id") == "image_gen"]
    assert rows, "保存图像生成配置必须留痕"
    detail = json.loads(rows[0]["detail_json"] or "{}")
    assert detail.get("enabled") is True
    assert detail.get("model") == "img-model"
    assert "api_key" not in detail


def test_settings_write_still_requires_admin(client: TestClient) -> None:
    """把 dependencies= 改成注入之后，权限不能被改松。"""
    r = client.put(
        f"{API}/settings/llm",
        json={"base_url": "https://x/v1", "api_key": "k", "model": "m",
              "temperature": 0.3, "max_output_tokens": 4096, "context_window": 131072},
    )
    assert r.status_code in (401, 403), "普通用户不该能改平台级配置"
