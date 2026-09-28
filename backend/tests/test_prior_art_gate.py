"""查新门控（disclosure.prior_art_search）的交互契约测试。

钉住三件曾经出过事或险些出事的事：

1. **前后端字段对不上**：前端卡片提交 `{hit_ids, manual:[{pub_number,…}], skipped}`，后端只认
   `{action, terms, hits, reason}` / `selected_ids`——取不到 action 就落到 skip，
   手工补录的在先文献被静默丢弃；取消勾选被忽略、照样写进 1.1。
2. **重试必然再败**：预算型失败时原样重试同一组词、同一个预算，结果不可能不同；
   零命中的词原样重试也不可能不同。
3. **1.1 只能写实际检索过的词**：部分完成时，检索说明里不许出现没检索过的词。

检索本身用测试桩（按调用次序返回预置的 SearchResult），命中行经 add_manual_hits 真实落库。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.models.search import SearchResult
from app.pipelines import disclosure
from app.services import cnipa

API = "/api/v1"


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_fail_gate_answer_accepts_frontend_shape():
    """前端卡片的提交形状要能被认出来，而不是一律当成「跳过」。"""
    manual = disclosure._fail_gate_answer(
        {
            "hit_ids": [],
            "manual": [{"pub_number": "CN1A", "title": "手工一", "url": "https://x/CN1A", "abstract": "摘"}],
            "skipped": False,
        }
    )
    assert manual["action"] == "manual"
    assert manual["hits"][0] == {
        "url": "https://x/CN1A", "pub_no": "CN1A", "title": "手工一", "applicant": None, "abstract": "摘",
    }
    assert disclosure._fail_gate_answer({"skipped": True})["action"] == "skip"
    assert disclosure._fail_gate_answer({"action": "retry", "terms": [" 甲 ", ""]}) == {
        "action": "retry", "terms": ["甲"], "hits": [], "reason": "",
    }
    # 后端契约原样通过
    assert disclosure._fail_gate_answer({"action": "skip", "reason": "先出稿"})["reason"] == "先出稿"
    # 什么都没有：保守按跳过处理（不编造、不瞎重试）
    assert disclosure._fail_gate_answer(None)["action"] == "skip"


def test_manual_items_counted_once_across_keys():
    """同一条在 hits 与 manual 下各出现一次：只算一条（否则「缺链接 N 条」会翻倍）。"""
    one = {"url": "", "pub_number": "CN1A", "title": "缺链接"}
    items = disclosure._manual_items({"hits": [one], "manual": [one]})
    assert len(items) == 1


def test_selected_ids_accepts_frontend_alias():
    assert disclosure._selected_ids({"selected_ids": ["a"]}) == {"a"}
    assert disclosure._selected_ids({"hit_ids": ["b", 3]}) == {"b", "3"}
    assert disclosure._selected_ids({}) is None


@pytest.mark.parametrize(
    ("kind", "must", "must_not"),
    [
        ("budget", ["时间预算", "国知局本身是通的", "尚未检索的 2 个词"], ["WAF", "网络不可达"]),
        ("blocked", ["访问验证未通过", "稍后重试可能恢复"], []),
        ("empty", ["零命中", "原词重试结果不会不同"], ["超时"]),
    ],
)
def test_fail_prompt_tells_the_truth_per_kind(kind: str, must: list[str], must_not: list[str]):
    res = SearchResult(status="failed", error="具体原因", failure_kind=kind)
    prompt = disclosure._search_fail_prompt(res, ["乙", "丙"])
    for text in must:
        assert text in prompt, (kind, text)
    for text in must_not:
        assert text not in prompt, (kind, text)
    assert "跳过查新" in prompt


# ---------------------------------------------------------------------------
# 门控全流程（最小上下文）
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, case_id: str, answers: list[Any]) -> None:
        self.case_id = case_id
        self.step_key = "prior_art_search"
        self.state: dict[str, Any] = {}
        self._answers = list(answers)
        self.requests: list[Any] = []
        self.logs: list[str] = []

    async def emit(self, event: str, data: Any, **_kw: Any) -> None:
        if isinstance(data, dict) and data.get("message"):
            self.logs.append(str(data["message"]))

    async def await_user(self, req: Any) -> Any:
        self.requests.append(req)
        assert self._answers, f"门控多问了一次：{req.prompt}"
        return self._answers.pop(0)


def _hit(n: int) -> dict[str, Any]:
    return {"url": f"http://epub.cnipa.gov.cn/patent/CN10000{n}A", "pub_no": f"CN10000{n}A", "title": f"命中{n}"}


@pytest.fixture
def gate(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """装好桩：检索词固定、检索按脚本返回、消化改写直接取命中。返回 (make_ctx, search_calls, script)。"""
    calls: list[list[str]] = []
    script: list[Any] = []

    async def fake_terms(_ctx: Any):
        return ["甲", "乙", "丙"], "invention", {"repairs": 0}

    async def fake_digest(_ctx: Any, hits: Any):
        return [{"url": h.url, "pub_number": h.pub_no, "title": h.title} for h in hits]

    async def fake_search(case_id: str, terms: Any, patent_type: str = "invention", **_kw: Any):
        norm = cnipa.normalize_terms(terms)
        calls.append(norm)
        step = script.pop(0)
        return await step(case_id, norm, patent_type)

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(disclosure, "_search_terms", fake_terms)
    monkeypatch.setattr(disclosure, "_digest_hits", fake_digest)
    monkeypatch.setattr(disclosure.skills_service, "is_user_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(cnipa, "search", fake_search)
    monkeypatch.setattr(cnipa, "hub_progress", lambda *_a, **_k: _noop)

    def make_ctx(title: str, answers: list[Any]) -> _Ctx:
        resp = client.post(f"{API}/cases", json={"module": "disclosure", "title": title})
        assert resp.status_code == 201, resp.text
        return _Ctx(resp.json()["id"], answers)

    return make_ctx, calls, script


def _fails(kind: str, searched: list[str], pending: list[str], error: str = "原因"):
    async def step(_case_id: str, norm: list[str], ptype: str) -> SearchResult:
        return SearchResult(
            status="failed", error=error, terms=norm, patent_type=ptype,
            searched_terms=searched, skipped_terms=pending, failure_kind=kind,
        )

    return step


def _succeeds(hits: list[dict[str, Any]], searched: list[str], pending: list[str] = ()):
    async def step(case_id: str, norm: list[str], ptype: str) -> SearchResult:
        rows = await cnipa.add_manual_hits(case_id, hits, note="测试桩命中")
        return SearchResult(
            status="done", hits=rows, terms=norm, patent_type=ptype,
            searched_terms=searched, skipped_terms=list(pending),
        )

    return step


async def test_budget_failure_retry_only_searches_pending_terms(gate):
    make_ctx, calls, script = gate
    script += [
        _fails("budget", searched=["甲"], pending=["乙", "丙"], error="已检索的 1 个词均无命中"),
        _succeeds([_hit(1), _hit(2)], searched=["乙", "丙"]),
    ]
    ctx = make_ctx("门控-预算重试", [{"action": "retry"}, {"hit_ids": None}])
    # 第二次门控（勾选）答案稍后按真实命中 id 填
    async def await_user(req: Any) -> Any:
        ctx.requests.append(req)
        if len(ctx.requests) == 1:
            return {"action": "retry"}
        ids = [h["id"] for h in req.default["hits"]]
        return {"hit_ids": ids[:1], "manual": [], "skipped": False}

    ctx.await_user = await_user  # type: ignore[method-assign]
    out = await disclosure.prior_art_search(ctx)

    fail_req = ctx.requests[0]
    assert fail_req.default["failed"] is True               # 前端据此进入失败态
    assert fail_req.default["failure_kind"] == "budget"
    assert fail_req.default["terms"] == ["乙", "丙"]          # 预填的是还没检索的词
    assert "尚未检索的 2 个词" in fail_req.prompt
    assert calls == [["甲", "乙", "丙"], ["乙", "丙"]]        # 重试只补这两个
    pa = out["prior_art"]
    assert pa["terms"] == ["甲", "乙", "丙"]                  # 三个词都真检索过
    assert pa["unsearched_terms"] == []
    assert pa["selected_count"] == 1                          # 取消勾选生效（hit_ids 被认出来）


async def test_user_edited_retry_terms_replace_the_plan(gate):
    """用户在重试时改了词：被换掉、从没检索过的词不该再被报成「未检索」。"""
    make_ctx, calls, script = gate
    script += [
        _fails("budget", searched=["甲"], pending=["乙", "丙"]),
        _succeeds([_hit(21)], searched=["丁"]),
    ]
    ctx = make_ctx("门控-改词重试", [{"action": "retry", "terms": ["丁"]}, {}])
    out = await disclosure.prior_art_search(ctx)

    assert calls == [["甲", "乙", "丙"], ["丁"]]
    pa = out["prior_art"]
    assert pa["terms"] == ["甲", "丁"]
    assert pa["unsearched_terms"] == []
    assert not any("部分完成" in m for m in ctx.logs)


async def test_futile_retry_is_not_rerun(gate):
    """已检索且零命中的词，原词重试不会有不同结果：不再为它们发请求，而是告诉用户。"""
    make_ctx, calls, script = gate
    script += [_fails("empty", searched=["甲", "乙", "丙"], pending=[], error="全部 3 个词检索完成，均无命中")]
    ctx = make_ctx(
        "门控-无效重试",
        [{"action": "retry", "terms": ["甲"]}, {"action": "skip", "reason": "先出稿"}],
    )
    out = await disclosure.prior_art_search(ctx)

    assert calls == [["甲", "乙", "丙"]]                      # 没有第二次检索
    assert any("原词重试结果不会不同" in m for m in ctx.logs)
    assert len(ctx.requests) == 2                             # 门控重新问了一次
    assert out["prior_art"]["skipped"] is True
    assert out["prior_art"]["skip_reason"] == "先出稿"


async def test_manual_entries_from_card_are_kept_not_skipped(gate):
    """被拦截后在卡片上手工补录：必须纳入，而不是被当成「跳过」丢掉。缺链接的那条如实说明。"""
    make_ctx, _calls, script = gate
    script += [_fails("blocked", searched=[], pending=["甲", "乙", "丙"], error="首页未出现检索框")]
    ctx = make_ctx(
        "门控-手工补录",
        [
            {
                "hit_ids": ["manual-1"],
                "manual": [
                    {"pub_number": "CN9A", "title": "手工文献", "url": "https://patents.google.com/patent/CN9A"},
                    {"pub_number": "CN8A", "title": "没有链接"},
                ],
                "skipped": False,
            },
            {},   # 随后的勾选卡片：沿用默认全选
        ],
    )
    out = await disclosure.prior_art_search(ctx)

    pa = out["prior_art"]
    assert pa["skipped"] is False and pa["manual"] is True
    assert pa["searched"] is True and pa["selected_count"] == 1
    assert out["prior_art_notes"][0]["url"] == "https://patents.google.com/patent/CN9A"
    assert any("缺少来源链接" in m for m in ctx.logs)


async def test_partial_success_reports_only_searched_terms(gate):
    """部分完成且自动补检索也没做成：1.1 的检索词只能是检索过的；勾选卡片上手工追加的也要纳入。"""
    make_ctx, calls, script = gate
    script += [
        _succeeds([_hit(3), _hit(4)], searched=["甲", "乙"], pending=["丙"]),
        _fails("budget", searched=[], pending=["丙"]),         # 自动补检索这一次也没做成
    ]
    ctx = make_ctx("门控-部分完成", [])

    async def await_user(req: Any) -> Any:
        ctx.requests.append(req)
        first = req.default["hits"][0]["id"]
        return {
            "hit_ids": [first],
            "manual": [{"pub_number": "CN7A", "title": "追加", "url": "https://x/CN7A"}],
            "skipped": False,
        }

    ctx.await_user = await_user  # type: ignore[method-assign]
    out = await disclosure.prior_art_search(ctx)

    assert calls == [["甲", "乙", "丙"], ["丙"]]               # 自动补检索只补没做完的
    pa = out["prior_art"]
    assert pa["terms"] == ["甲", "乙"]                        # 丙 没检索过，不许出现在检索说明里
    assert pa["unsearched_terms"] == ["丙"]
    assert pa["planned_terms"] == ["甲", "乙", "丙"]
    assert any("部分完成" in m and "丙" in m for m in ctx.logs)
    urls = {n["url"] for n in out["prior_art_notes"]}
    assert "https://x/CN7A" in urls                           # 追加的纳入了
    assert len(urls) == 2                                     # 取消勾选的那条没纳入


async def test_partial_success_follow_up_fills_the_gap(gate):
    """部分完成时换新会话自动补检索一次：补上的命中与原命中合并，1.1 的检索词完整。"""
    make_ctx, calls, script = gate
    script += [
        _succeeds([_hit(11), _hit(12)], searched=["甲", "乙"], pending=["丙"]),
        _succeeds([_hit(12), _hit(13)], searched=["丙"]),     # 与第一轮有一条重复
    ]
    ctx = make_ctx("门控-自动补检索", [{}])
    out = await disclosure.prior_art_search(ctx)

    assert calls == [["甲", "乙", "丙"], ["丙"]]
    assert any("自动补检索" in m for m in ctx.logs)
    assert not any("部分完成" in m for m in ctx.logs)          # 补完了，就不再说部分完成
    pa = out["prior_art"]
    assert pa["terms"] == ["甲", "乙", "丙"]
    assert pa["unsearched_terms"] == []
    assert sorted(n["url"] for n in out["prior_art_notes"]) == sorted(
        _hit(n)["url"] for n in (11, 12, 13)
    )


async def test_skip_on_selection_card_includes_nothing(gate):
    """勾选卡片上点「跳过」：一条都不纳入（早先会被当成全选，与用户意图正相反）。"""
    make_ctx, _calls, script = gate
    script += [_succeeds([_hit(5), _hit(6)], searched=["甲", "乙", "丙"])]
    ctx = make_ctx("门控-勾选跳过", [{"skipped": True}])
    out = await disclosure.prior_art_search(ctx)
    assert out["prior_art_notes"] == []
    assert out["prior_art"]["selected_count"] == 0


async def test_backend_contract_answers_still_work(gate):
    """旧的后端契约回填（selected_ids / action）照旧生效。"""
    make_ctx, _calls, script = gate
    script += [_succeeds([_hit(7), _hit(8)], searched=["甲", "乙", "丙"])]
    ctx = make_ctx("门控-旧契约", [])

    async def await_user(req: Any) -> Any:
        ctx.requests.append(req)
        return {"selected_ids": [req.default["hits"][1]["id"]]}

    ctx.await_user = await_user  # type: ignore[method-assign]
    out = await disclosure.prior_art_search(ctx)
    assert [n["url"] for n in out["prior_art_notes"]] == [_hit(8)["url"]]


# ---------------------------------------------------------------------------
# 迭代期补充查新
# ---------------------------------------------------------------------------


async def test_supplementary_search_reports_searched_terms(monkeypatch: pytest.MonkeyPatch):
    """补充查新要带回实际检索过的词：新文献是用这些词查到的，1.1 的检索说明得写上。"""
    from app.pipelines import disclosure_iterate as iterate_pipeline

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    async def partial_search(case_id, terms, patent_type="invention", **_kw):
        return SearchResult(
            status="done", hits=[], terms=list(terms), patent_type=patent_type,
            searched_terms=["灰度节点集合"], skipped_terms=["批量作业调度"],
        )

    monkeypatch.setattr(cnipa, "hub_progress", lambda *_a, **_k: _noop)
    monkeypatch.setattr(cnipa, "search", partial_search)

    class Ctx:
        def __init__(self) -> None:
            self.case_id = "stub-case"
            self.step_key = "iterate_rewrite"
            self.state = {"prior_art": {"terms": ["批量作业调度", "负载画像"], "type_param": "invention"}}
            self.logs: list[Any] = []

        async def emit(self, event: str, data: Any, **_kw: Any) -> None:
            self.logs.append(data)

    report = await iterate_pipeline._supplementary_search(Ctx(), ["灰度节点集合"])
    assert report["searched_terms"] == ["灰度节点集合"]
    assert report["unsearched_terms"] == ["批量作业调度"]
