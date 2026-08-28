# -*- coding: utf-8 -*-
"""配额类 429 快败说人话；结构化解析不被回显/包装骗成空壳。

两条都来自 glm-5.3 上线当天的真实现场：

1. 深度档反复「触发限流（第 N/8 次）」倒计时——若那其实是订阅额度耗尽，
   8 次退避是让用户看着倒计时白等三四分钟，最后拿到一句语焉不详的失败；
   而提示里连服务端原话都没有，没人知道该充值、该等窗口，还是该换档。
2. A5 预览卡五项全「暂无」——PreviewSummary 全字段可选，**任何 dict 都能
   「验证通过」成一个全默认值空壳**；模型回显 schema 或用包装对象包住真数据时，
   「取第一个 JSON」就把空壳送进了流水线，界面不报错，数据静默归零。
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.services import llm

ZHIPU_QUOTA = (
    "Error code: 429 - {'error': {'code': '1113', "
    "'message': '余额不足或无可用资源包,请充值。'}}"
)
ZHIPU_CONCURRENCY = (
    'Error code: 429 - {"error": {"code": "1302", '
    '"message": "您当前使用该API的并发数过高,请降低并发或联系客服增加限额"}}'
)


# ---------------------------------------------------------------------------
# 配额 vs 限流：处置完全不同，必须分开
# ---------------------------------------------------------------------------


def test_quota_exhaustion_is_distinguished_from_rate_limits() -> None:
    assert llm._is_quota_exhausted(RuntimeError(ZHIPU_QUOTA)) is True
    assert llm._is_quota_exhausted(RuntimeError(ZHIPU_CONCURRENCY)) is False, (
        "并发超限等一等就能过，误判成配额耗尽会把可恢复的等待变成误报失败"
    )
    assert llm._is_quota_exhausted(
        RuntimeError("You exceeded your current quota, please check your plan and billing")
    ) is True


def test_server_message_is_extracted_for_humans() -> None:
    """用户和维护者要拿到同一句服务端原话，才谈得上去查。"""
    assert llm._server_message(RuntimeError(ZHIPU_QUOTA)) == "余额不足或无可用资源包,请充值。"
    assert "并发数过高" in llm._server_message(RuntimeError(ZHIPU_CONCURRENCY))


@pytest.mark.anyio
async def test_quota_error_fails_fast_instead_of_eight_backoffs() -> None:
    """配额耗尽重试无意义：第一次就该以 LlmQuotaError 上抛，且带服务端原话。"""
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise RuntimeError(ZHIPU_QUOTA)

    with pytest.raises(llm.LlmQuotaError) as info:
        await llm._with_rate_limit_retry(factory, what="test", model="glm-5.3")
    assert calls["n"] == 1, "配额类错误不该进退避循环"
    assert "余额不足" in str(info.value)
    assert "glm-5.3" in str(info.value), "两档不同模型时必须说清是哪一档在受限"


# ---------------------------------------------------------------------------
# 候选校验：全可选模型 + 回显/包装 = 静默空壳，必须挡住
# ---------------------------------------------------------------------------


class AllOptional(BaseModel):
    """按 PreviewSummary 的形态建模：全字段可选 → 任何 dict 都能验证通过。"""

    model_config = ConfigDict(extra="ignore")

    case_title: str = ""
    tech_problems: list[str] = []


GOOD = '{"case_title": "一种检测方法", "tech_problems": ["误检率高"]}'


def test_schema_echo_is_skipped() -> None:
    """模型把我们发去的 JSON Schema 回显在前面：第一个 JSON 是 schema，不是数据。"""
    text = (
        '这是您要的结构：\n```json\n{"type": "object", "properties": '
        '{"case_title": {"type": "string"}}, "required": []}\n```\n'
        f"实际内容：\n```json\n{GOOD}\n```"
    )
    got = llm.validate_from_candidates(text, AllOptional)
    assert got.case_title == "一种检测方法", "取到的是 schema 空壳而不是真数据"


def test_wrapper_object_is_unwrapped() -> None:
    """模型用一层无关键名包住真数据（{"结构化摘要": {...}}）。"""
    text = '{"结构化摘要": ' + GOOD + "}"
    got = llm.validate_from_candidates(text, AllOptional)
    assert got.tech_problems == ["误检率高"]


def test_zero_field_overlap_never_produces_an_empty_shell() -> None:
    """一个目标字段都对不上的 dict，宁可报错触发带错重试，也不能静默变全空。"""
    with pytest.raises(Exception):
        llm.validate_from_candidates('{"foo": 1, "bar": "x"}', AllOptional)


def test_plain_good_json_still_works() -> None:
    got = llm.validate_from_candidates(GOOD, AllOptional)
    assert got.case_title == "一种检测方法"


def test_invalid_then_valid_candidate_picks_the_valid_one() -> None:
    class Strict(BaseModel):
        n: int

    text = '{"n": "不是数字"} 然后是 {"n": 7}'
    assert llm.validate_from_candidates(text, Strict).n == 7


def test_all_invalid_raises_validation_error_for_feedback_retry() -> None:
    class Strict(BaseModel):
        n: int

    with pytest.raises(ValidationError):
        llm.validate_from_candidates('{"n": "甲"}', Strict)
