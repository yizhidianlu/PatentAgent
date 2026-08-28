# -*- coding: utf-8 -*-
"""两档模型：快速 / 深度思考。

档位回答的是「这次要快还是要想清楚」，所以它只覆盖**用哪个模型、怎么生成**，
不覆盖服务地址与密钥——每多一个密钥输入框就多一个密钥外发的入口。

实现上的关键是 contextvar：流水线是一棵很深的调用树，把档位当参数传下去
只要漏掉一处，那次调用就会静悄悄用回默认模型。「大部分步骤用了快速档、
个别步骤没有」是最难发现的一类不一致，所以这里重点验「整棵树都跟着变」。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.services import llm as llm_service

API = "/api/v1"


@pytest.fixture(autouse=True)
def _reset_tier():
    llm_service.set_active_tier(None)
    yield
    llm_service.set_active_tier(None)


@pytest.fixture
def configured(admin_client: TestClient):
    """主配置 + 两档：快速档只换模型，深度档另外放宽出参预算。"""
    r = admin_client.put(
        f"{API}/settings/llm",
        json={"base_url": "https://api.example.com/v1", "api_key": "sk-tier-test-000",
              "model": "base-model", "temperature": 0.3,
              "max_output_tokens": 4096, "context_window": 131072},
    )
    assert r.status_code == 200, r.text
    r = admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model", "label": "快速"},
            "deep": {"model": "thinking-model", "max_output_tokens": 32768},
            "default_tier": "deep",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_tier_only_changes_model_and_generation_params(configured) -> None:
    """档位不碰 base_url 与 api_key —— 它不是「换一家供应商」。"""
    base = llm_service.load_llm_settings()
    assert base.model == "base-model"

    llm_service.set_active_tier("fast")
    fast = llm_service.load_llm_settings()
    assert fast.model == "quick-model"
    assert fast.base_url == base.base_url
    assert fast.api_key == base.api_key
    # 快速档没配预算 → 沿用主配置，而不是被清成默认值
    assert fast.max_output_tokens == base.max_output_tokens

    llm_service.set_active_tier("deep")
    deep = llm_service.load_llm_settings()
    assert deep.model == "thinking-model"
    assert deep.max_output_tokens == 32768
    assert deep.api_key == base.api_key


def test_unconfigured_tier_falls_back_to_the_base_model(admin_client: TestClient) -> None:
    """没配的档位不该把模型名清空——那会让每一次调用都失败。"""
    admin_client.put(
        f"{API}/settings/llm",
        json={"base_url": "https://api.example.com/v1", "api_key": "sk-x",
              "model": "only-model", "temperature": 0.3,
              "max_output_tokens": 4096, "context_window": 131072},
    )
    admin_client.put(f"{API}/settings/model-tiers", json={"fast": {}, "deep": {}})

    for tier in ("fast", "deep", None):
        llm_service.set_active_tier(tier)
        assert llm_service.load_llm_settings().model == "only-model"


def test_explicit_override_beats_the_active_tier(configured) -> None:
    """连接测试带着明确的目标模型，不该被上下文里残留的档位改掉。"""
    llm_service.set_active_tier("fast")
    cfg = llm_service.load_llm_settings({"model": "probe-model"})
    assert cfg.model == "probe-model"


def test_unknown_tier_is_treated_as_no_tier(configured) -> None:
    llm_service.set_active_tier("turbo")          # 不存在的档位
    assert llm_service.active_tier() is None
    assert llm_service.load_llm_settings().model == "base-model"


@pytest.mark.anyio
async def test_tier_propagates_into_nested_async_calls(configured) -> None:
    """整棵调用树都要跟着变——这正是不用「层层传参」的原因。"""
    llm_service.set_active_tier("fast")

    async def deep_in_the_call_tree() -> str:
        await asyncio.sleep(0)
        return llm_service.load_llm_settings().model

    async def middle() -> str:
        return await deep_in_the_call_tree()

    assert await middle() == "quick-model"


@pytest.mark.anyio
async def test_two_cases_can_run_on_different_tiers(configured) -> None:
    """并发的两个案件互不串档：contextvar 天然按任务隔离。"""
    seen: dict[str, str] = {}

    async def run(name: str, tier: str) -> None:
        llm_service.set_active_tier(tier)
        await asyncio.sleep(0.01)
        seen[name] = llm_service.load_llm_settings().model

    await asyncio.gather(run("a", "fast"), run("b", "deep"))
    assert seen == {"a": "quick-model", "b": "thinking-model"}


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


def test_get_reports_the_effective_model_per_tier(admin_client: TestClient, configured) -> None:
    """界面要显示「这一档实际会用哪个模型」，否则留空的档位看着像没生效。"""
    r = admin_client.get(f"{API}/settings/model-tiers")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["base_model"] == "base-model"
    assert data["effective"]["fast"] == "quick-model"
    assert data["effective"]["deep"] == "thinking-model"

    admin_client.put(f"{API}/settings/model-tiers", json={"fast": {}, "deep": {}})
    data = admin_client.get(f"{API}/settings/model-tiers").json()
    assert data["effective"]["fast"] == "base-model", "留空的档位要显示回落到的模型"


def test_writing_tiers_requires_admin(client: TestClient) -> None:
    r = client.put(f"{API}/settings/model-tiers", json={"fast": {"model": "x"}})
    assert r.status_code in (401, 403), "普通用户不该能改平台级模型配置"


def test_reading_tiers_is_open_to_users(client: TestClient) -> None:
    """普通用户要能读——聊天框上的档位开关得知道有哪些档、各是什么模型。"""
    assert client.get(f"{API}/settings/model-tiers").status_code == 200


def test_tier_change_is_audited(admin_client: TestClient) -> None:
    import json

    from app.db import database as db

    admin_client.put(
        f"{API}/settings/model-tiers",
        json={"fast": {"model": "audit-fast"}, "deep": {"model": "audit-deep"},
              "default_tier": "fast"},
    )
    rows = db.query_all(
        "SELECT * FROM audit_log WHERE action='settings_updated' AND target_id='model_tiers'"
        " ORDER BY rowid DESC LIMIT 1"
    )
    assert rows, "改平台级模型配置必须留痕"
    detail = json.loads(dict(rows[0])["detail_json"] or "{}")
    assert detail["fast_model"] == "audit-fast"
    assert detail["default_tier"] == "fast"


# ---------------------------------------------------------------------------
# 案件上的档位
# ---------------------------------------------------------------------------


def test_new_case_takes_the_default_tier(client: TestClient, admin_client, configured) -> None:
    r = client.post(f"{API}/cases", json={"module": "disclosure", "title": "默认档位"})
    case_id = r.json()["id"]
    detail = client.get(f"{API}/cases/{case_id}").json()
    assert detail["state"]["_model_tier"] == "deep"


def test_case_can_be_created_on_an_explicit_tier(client: TestClient, configured) -> None:
    """首页选好档位再开跑：建案时一次带上，不必再补一次 PATCH。"""
    r = client.post(
        f"{API}/cases",
        json={"module": "disclosure", "title": "指定档位", "model_tier": "fast"},
    )
    assert r.status_code in (200, 201), r.text
    detail = client.get(f"{API}/cases/{r.json()['id']}").json()
    assert detail["state"]["_model_tier"] == "fast"


def test_tier_can_be_switched_on_an_existing_case(client: TestClient, configured) -> None:
    r = client.post(f"{API}/cases", json={"module": "disclosure", "title": "中途换档"})
    case_id = r.json()["id"]
    assert client.patch(f"{API}/cases/{case_id}", json={"model_tier": "fast"}).status_code == 200
    detail = client.get(f"{API}/cases/{case_id}").json()
    assert detail["state"]["_model_tier"] == "fast"


def test_switching_tier_keeps_the_rest_of_the_working_memory(
    client: TestClient, configured
) -> None:
    """换档不能把流水线的工作内存冲掉——那等于把跑到一半的案件清零。"""
    from app.db import database as db

    r = client.post(f"{API}/cases", json={"module": "disclosure", "title": "保内存"})
    case_id = r.json()["id"]
    db.execute(
        "UPDATE cases SET state_json=? WHERE id=?",
        ('{"_model_tier": "deep", "_run_group": "initial", "intake": {"topic": "甲"}}', case_id),
    )
    assert client.patch(f"{API}/cases/{case_id}", json={"model_tier": "fast"}).status_code == 200
    state = client.get(f"{API}/cases/{case_id}").json()["state"]
    assert state["_model_tier"] == "fast"
    assert state["_run_group"] == "initial"
    assert state["intake"] == {"topic": "甲"}


def test_invalid_tier_is_rejected(client: TestClient) -> None:
    r = client.post(f"{API}/cases", json={"module": "disclosure", "model_tier": "turbo"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 引擎接线：档位真的到得了 LLM 调用
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_engine_applies_the_case_tier_to_every_step(client: TestClient, configured) -> None:
    """整条流水线都跑在案件选定的档位上。

    这是最容易断的一环：档位存在案件里、生效在 llm 里，中间隔着整个引擎。
    接线断了不会报错——只会安静地全程用回默认模型。
    """
    from app.pipelines.engine import StepDef, start

    r = client.post(
        f"{API}/cases",
        json={"module": "disclosure", "title": "档位接线", "model_tier": "fast"},
    )
    case_id = r.json()["id"]

    seen: list[str] = []

    async def record(ctx) -> dict:
        seen.append(llm_service.load_llm_settings().model)
        return {}

    steps = [
        StepDef(key="s1", name_zh="第一步", handler=record),
        StepDef(key="s2", name_zh="第二步", handler=record),
    ]
    await start(case_id, steps, run_group="initial")

    assert seen == ["quick-model", "quick-model"], f"两步都该跑在快速档，实际 {seen}"


@pytest.mark.anyio
async def test_switching_tier_takes_effect_on_the_next_run(
    client: TestClient, configured
) -> None:
    """换档影响的是**之后**的运行——界面上的提示语说的就是这件事。"""
    from app.pipelines.engine import StepDef, start

    r = client.post(
        f"{API}/cases", json={"module": "disclosure", "title": "换档生效", "model_tier": "fast"}
    )
    case_id = r.json()["id"]
    seen: list[str] = []

    async def record(ctx) -> dict:
        seen.append(llm_service.load_llm_settings().model)
        return {}

    await start(case_id, [StepDef(key="a", name_zh="A", handler=record)], run_group="initial")
    assert client.patch(f"{API}/cases/{case_id}", json={"model_tier": "deep"}).status_code == 200
    await start(case_id, [StepDef(key="b", name_zh="B", handler=record)], run_group="second")

    assert seen == ["quick-model", "thinking-model"]
