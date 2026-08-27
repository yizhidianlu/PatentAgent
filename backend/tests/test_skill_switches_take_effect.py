# -*- coding: utf-8 -*-
"""技能开关必须真的改变行为，而不只是改数据库。

这组用例存在的理由是一次真实的疏忽：技能库页面做好了、开关能点、
审计日志也如实记下 skill_toggled，但 `is_enabled()` 全仓库零调用——
用户把「国知局联网查新」关掉、界面回复「已停用」，然后流水线照样带着
本案的技术要点去访问国知局。**用户据此做的保密判断，在系统明确回复
「已生效」之后落空。** 对专利交底材料，这比功能不生效严重得多。
"""
import pytest

from app.db import database as db
from app.services import drawings, skills


@pytest.fixture(autouse=True)
def _db(raw_client):
    return raw_client


@pytest.fixture
def clean_switches():
    db.execute("DELETE FROM settings WHERE key='skills'")
    yield
    db.execute("DELETE FROM settings WHERE key='skills'")


def test_disabled_skill_reports_disabled(clean_switches):
    skills.set_enabled("cnipa_search", False)
    assert skills.is_enabled("cnipa_search") is False


def test_enabled_but_unmet_requirement_is_not_enabled(clean_switches, monkeypatch):
    """开着但前置条件不满足 → 不生效。

    两个条件缺一不可：用户开了却没配模型，跑起来也是错的，
    这时应当走降级路径而不是报错。
    """
    skills.set_enabled("cnipa_search", True)
    monkeypatch.setattr(skills, "_has_chrome", lambda: False)
    # requirements 里持有的是函数对象，得换掉 probe 本身
    defn = skills.get_definition("cnipa_search")
    patched = tuple(
        skills.RequirementDef(key=r.key, label=r.label, probe=(lambda: False),
                              hint=r.hint, settings_path=r.settings_path)
        if r.key == "chrome" else r
        for r in defn.requirements
    )
    object.__setattr__(defn, "requirements", patched)
    try:
        assert skills.is_enabled("cnipa_search") is False
    finally:
        object.__setattr__(defn, "requirements", defn.requirements)


def test_non_toggleable_skill_cannot_be_disabled(clean_switches):
    """流程必需的技能不给关——关掉等于允许产出不合规文书。"""
    with pytest.raises(ValueError):
        skills.set_enabled("claims_lint", False)
    assert skills.is_enabled("claims_lint") is True


def test_unknown_skill_key_rejected(clean_switches):
    with pytest.raises(KeyError):
        skills.set_enabled("no_such_skill", True)


async def test_ai_figure_off_means_no_image_call(clean_switches, monkeypatch):
    """关掉附图智能补绘之后，绝不能再调用图像模型。

    它按图像模型计费——只改数据库而照样调用，等于在用户明确关闭之后继续扣额度。
    """
    from app.services import llm as llm_service

    called = {"n": 0}

    async def spy(*a, **k):
        called["n"] += 1
        return b"\x89PNG\r\n\x1a\n" + b"0" * 64

    monkeypatch.setattr(llm_service, "generate_image", spy)
    skills.set_enabled("ai_figure", False)

    content = {"drawings": ["图1：结构框图。"], "drawing_assets": []}
    got = await drawings.try_ai_figure("case-switch", content, 1)

    assert got is False
    assert called["n"] == 0, "技能关闭时不该发生任何图像模型调用"


def test_cnipa_switch_is_consulted_by_pipeline():
    """联网查新的开关必须被流水线读到——而不只是存在于设置里。

    直接断言源码里存在这次查询：这条的价值全在「关掉之后确实不发出去」，
    而一旦有人重构掉这行判断，界面上的开关会再次变成摆设且无人察觉。
    """
    import inspect

    from app.pipelines import disclosure

    src = inspect.getsource(disclosure.prior_art_search)
    assert "cnipa_search" in src, "查新步骤必须查询自己的技能开关"
    # 查的是 is_user_enabled 而非 is_enabled：用户明确关掉才跳过；
    # 前置条件不满足时要走既有的三选项兜底（重试/手工录入/跳过），
    # 那比直接跳过有用得多，也能如实写进 1.1。
    assert "is_user_enabled" in src
