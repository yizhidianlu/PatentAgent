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


# ---------------------------------------------------------------------------
# 跨供应商分档：能力要给，但密钥边界必须在这里显式守住
# ---------------------------------------------------------------------------

FOREIGN = "https://another-vendor.example.com/v1"


def test_tier_can_point_at_another_provider(admin_client: TestClient, configured) -> None:
    """带上自己的地址与密钥时，这一档整套都换过去。"""
    r = admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model"},
            "deep": {
                "model": "vendor-b-thinking",
                "base_url": FOREIGN,
                "api_key": "sk-vendor-b-own-key-0001",
            },
            "default_tier": "deep",
        },
    )
    assert r.status_code == 200, r.text

    llm_service.set_active_tier("deep")
    cfg = llm_service.load_llm_settings()
    assert cfg.model == "vendor-b-thinking"
    assert cfg.base_url == FOREIGN
    assert cfg.api_key == "sk-vendor-b-own-key-0001", "应当用这一档自己的密钥"

    llm_service.set_active_tier("fast")
    fast = llm_service.load_llm_settings()
    assert fast.base_url == "https://api.example.com/v1", "没配地址的档位仍沿用主配置"
    assert fast.api_key == "sk-tier-test-000"


def test_changing_host_without_a_key_is_refused(admin_client: TestClient, configured) -> None:
    """**核心边界**：换 host 又不给密钥 = 把主密钥发给另一家。

    这与本项目此前修过的密钥外发是同一形态，只是换了个入口——
    每一处能独立设置 base_url 的地方，都是一处要重新论证「密钥去哪」的边界。
    """
    r = admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model"},
            "deep": {"model": "vendor-b", "base_url": FOREIGN},   # 没给 api_key
            "default_tier": "deep",
        },
    )
    assert r.status_code == 400, "换了 host 又不给密钥必须拒绝"
    assert "API Key" in r.json()["detail"]

    # 拒绝之后不能留下半套配置
    data = admin_client.get(f"{API}/settings/model-tiers").json()
    assert data["deep"]["base_url"] != FOREIGN, "被拒绝的配置不该落库"


def test_masked_key_cannot_be_used_to_move_a_tier_to_another_host(
    admin_client: TestClient, configured
) -> None:
    """掩码值是界面自己吐出来的，不能拿它当「我有密钥」。"""
    admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model"},
            "deep": {"model": "vendor-b", "base_url": FOREIGN, "api_key": "sk-real-b-0001"},
            "default_tier": "deep",
        },
    )
    masked = admin_client.get(f"{API}/settings/model-tiers").json()["deep"]["api_key"]
    assert masked and masked != "sk-real-b-0001"

    # 换到第三家，密钥框里放着上一家的掩码
    r = admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model"},
            "deep": {"model": "vendor-c", "base_url": "https://vendor-c.example/v1",
                     "api_key": masked},
            "default_tier": "deep",
        },
    )
    assert r.status_code == 400, "掩码回落的是上一家的密钥，不能带着它换到第三家"


def test_same_host_different_path_still_inherits_the_key(
    admin_client: TestClient, configured
) -> None:
    """同一供应商换版本号是常见操作，不该被误伤。"""
    r = admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model", "base_url": "https://api.example.com/v1beta"},
            "deep": {"model": "thinking-model"},
            "default_tier": "deep",
        },
    )
    assert r.status_code == 200, r.text
    llm_service.set_active_tier("fast")
    cfg = llm_service.load_llm_settings()
    assert cfg.base_url == "https://api.example.com/v1beta"
    assert cfg.api_key == "sk-tier-test-000", "同 host 应当仍回落主密钥"


def test_tier_key_is_masked_on_read(admin_client: TestClient, configured) -> None:
    admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model"},
            "deep": {"model": "vendor-b", "base_url": FOREIGN, "api_key": "sk-secret-b-9999"},
            "default_tier": "deep",
        },
    )
    body = admin_client.get(f"{API}/settings/model-tiers").text
    assert "sk-secret-b-9999" not in body, "读接口不能回明文密钥"


def test_empty_key_on_save_keeps_the_stored_one(admin_client: TestClient, configured) -> None:
    """只想改模型名时不必重填密钥。"""
    admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model"},
            "deep": {"model": "vendor-b", "base_url": FOREIGN, "api_key": "sk-keepme-0001"},
            "default_tier": "deep",
        },
    )
    r = admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model"},
            "deep": {"model": "vendor-b-v2", "base_url": FOREIGN},   # 不带 api_key
            "default_tier": "deep",
        },
    )
    assert r.status_code == 200, r.text
    llm_service.set_active_tier("deep")
    cfg = llm_service.load_llm_settings()
    assert cfg.model == "vendor-b-v2"
    assert cfg.api_key == "sk-keepme-0001", "留空应当沿用该档已存的密钥"


def test_effective_base_url_is_reported(admin_client: TestClient, configured) -> None:
    """界面要能一眼看出每一档实际打到哪儿。"""
    admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model"},
            "deep": {"model": "vendor-b", "base_url": FOREIGN, "api_key": "sk-b-1"},
            "default_tier": "deep",
        },
    )
    data = admin_client.get(f"{API}/settings/model-tiers").json()
    assert data["effective_base_url"]["fast"] == "https://api.example.com/v1"
    assert data["effective_base_url"]["deep"] == FOREIGN


def test_tier_test_endpoint_uses_the_saved_profile(
    admin_client: TestClient, configured, monkeypatch
) -> None:
    """试连要用已保存的那一档——配错了要在跑 40 分钟之前就知道。"""
    from app.models.settings import LlmTestResult
    from app.services import llm as llm_mod

    seen: dict = {}

    async def fake_test(override=None):
        seen["override"] = override
        return LlmTestResult(ok=True, model="x", latency_ms=1)

    monkeypatch.setattr(llm_mod, "test_llm", fake_test)
    r = admin_client.post(f"{API}/settings/model-tiers/deep/test")
    assert r.status_code == 200, r.text
    assert seen["override"]["model"] == "thinking-model"


def test_unknown_tier_test_is_404(admin_client: TestClient) -> None:
    assert admin_client.post(f"{API}/settings/model-tiers/turbo/test").status_code == 404


def test_tier_test_requires_admin(client: TestClient) -> None:
    assert client.post(f"{API}/settings/model-tiers/fast/test").status_code in (401, 403)


def test_clearing_a_tier_url_drops_that_tiers_key(admin_client: TestClient, configured) -> None:
    """把某档的地址清回主供应商时，上一家的密钥不该跟着走，也不该因此报错。

    「我把自定义地址清空了」意图很明确，不该要求用户再解释一遍；
    但那把属于上一家的密钥必须就地丢掉，不能被带到主供应商去。
    """
    admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model"},
            "deep": {"model": "vendor-b", "base_url": FOREIGN, "api_key": "sk-b-should-be-dropped"},
            "default_tier": "deep",
        },
    )
    r = admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model"},
            "deep": {"model": "thinking-model"},          # 地址清空、不给密钥
            "default_tier": "deep",
        },
    )
    assert r.status_code == 200, r.text

    llm_service.set_active_tier("deep")
    cfg = llm_service.load_llm_settings()
    assert cfg.base_url == "https://api.example.com/v1"
    assert cfg.api_key == "sk-tier-test-000", "应当回到主密钥"
    assert cfg.api_key != "sk-b-should-be-dropped", "上一家的密钥被带过来了"


def test_tier_key_never_appears_in_the_audit_log(admin_client: TestClient, configured) -> None:
    """审计留「改没改过」，不留密钥本身——审计的查看门槛比设置页低。"""
    import json

    from app.db import database as db

    secret = "sk-tier-audit-never-log-7777"
    admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model"},
            "deep": {"model": "vendor-b", "base_url": FOREIGN, "api_key": secret},
            "default_tier": "deep",
        },
    )
    blob = " ".join(str(dict(r)) for r in db.query_all("SELECT * FROM audit_log"))
    assert secret not in blob
    assert "tier-audit-never-log" not in blob

    rows = db.query_all(
        "SELECT * FROM audit_log WHERE action='settings_updated' AND target_id='model_tiers'"
        " ORDER BY rowid DESC LIMIT 1"
    )
    detail = json.loads(dict(rows[0])["detail_json"] or "{}")
    assert detail["deep_api_key_changed"] is True
    assert detail["deep_base_url"] == FOREIGN


def test_tier_test_reports_the_endpoint_it_actually_hit(
    admin_client: TestClient, configured
) -> None:
    """试连必须回出「实际打到哪儿」，不能只回 ok 和模型名。

    部署端在沙箱里踩到过这一点：他的假供应商对任何请求都回写死的模型名，
    只看返回值会以为配置指向了那一家——**返回值同时兼容「配置真被改了」
    和「你的桩就长这样」**。分档之后这条尤其要紧：两档指向不同供应商时，
    用户唯一能自查的就是这个地址。
    """
    admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model"},
            "deep": {"model": "vendor-b", "base_url": FOREIGN, "api_key": "sk-b-endpoint-1"},
            "default_tier": "deep",
        },
    )

    from app.models.settings import LlmTestResult
    from app.services import llm as llm_mod

    async def fake_test(override=None):
        # 桩故意回一个与配置无关的模型名——正是那个会误导人的形态
        cfg = llm_mod.load_llm_settings(override)
        return LlmTestResult(
            ok=True, model="whatever-the-stub-says", latency_ms=1,
            target_base_url=cfg.base_url,
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(llm_mod, "test_llm", fake_test)
        deep = admin_client.post(f"{API}/settings/model-tiers/deep/test").json()
        fast = admin_client.post(f"{API}/settings/model-tiers/fast/test").json()

    assert deep["target_base_url"] == FOREIGN, "深度档应当打到它自己的地址"
    assert fast["target_base_url"] == "https://api.example.com/v1", "快速档应当回落主地址"


# ---------------------------------------------------------------------------
# 思考模式：「快档」真正的开关
# ---------------------------------------------------------------------------


def test_thinking_params_are_omitted_by_default() -> None:
    """auto = 不发该参数：不改变任何既有模型的行为。"""
    from app.models.settings import LlmSettings

    assert llm_service._thinking_params(LlmSettings(model="m")) == {}


def test_thinking_disabled_is_sent_explicitly() -> None:
    """显式声明意图，而不是赌服务商默认。

    今天的教训：模型名里的 flash 是命名不是性能承诺——glm-5.3-flash 因思维链
    默认开满，在 8/9 个真实步骤上比旗舰更慢。快档要真的快，靠的是这个参数。
    """
    from app.models.settings import LlmSettings

    got = llm_service._thinking_params(
        LlmSettings(model="m", thinking="disabled", reasoning_effort="low")
    )
    # 必须包在 extra_body 里：SDK 对未知具名参数直接抛 TypeError（实测踩过），
    # 那是客户端拒收，服务端 400 的 quirk 自愈路径够不着它
    assert got == {"extra_body": {"thinking": {"type": "disabled"}, "reasoning_effort": "low"}}


def test_tier_overlay_carries_thinking(admin_client: TestClient, configured) -> None:
    """档位配置能把思考模式带到实际调用上。"""
    r = admin_client.put(
        f"{API}/settings/model-tiers",
        json={
            "fast": {"model": "quick-model", "thinking": "disabled", "reasoning_effort": "low"},
            "deep": {"model": "thinking-model"},
            "default_tier": "deep",
        },
    )
    assert r.status_code == 200, r.text

    llm_service.set_active_tier("fast")
    cfg = llm_service.load_llm_settings()
    assert cfg.thinking == "disabled" and cfg.reasoning_effort == "low"

    llm_service.set_active_tier("deep")
    deep = llm_service.load_llm_settings()
    assert deep.thinking == "auto", "没配的档位不该被快档的设置串味"


def test_endpoint_rejection_is_learned_not_fatal() -> None:
    """不支持关闭思考的服务商（如 glm-5.3）拒收时摘掉参数，而不是让调用失败。

    声明意图必须是安全的：不支持只回落到默认，不能变成一次硬失败。
    """
    model = "no-thinking-support"
    try:
        assert llm_service._learn_quirk(
            model, RuntimeError("400 invalid_request: parameter 'thinking' is not supported")
        ) is True
        assert llm_service.model_quirks(model).get("no_thinking_param") is True
        kw = llm_service._apply_quirks(
            model,
            {"model": model,
             "extra_body": {"thinking": {"type": "disabled"}, "reasoning_effort": "low"}},
        )
        assert "thinking" not in kw.get("extra_body", {}), "学到之后必须摘掉该参数"
    finally:
        llm_service._MODEL_QUIRKS.pop(model, None)
        llm_service._persist_quirks()
