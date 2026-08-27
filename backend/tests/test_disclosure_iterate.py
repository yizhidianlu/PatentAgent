"""模块 A · 迭代流水线端到端测试（M4）。

不依赖真实 LLM 与真实网络：复用 `test_disclosure.py` 的脚本化 `FakeLLM`、cnipa 测试桩
与案件语料，先把主流水线跑到 deliver，再连跑三轮迭代：

1. **merge**  —— 上传一份新材料 → `POST /disclosure/iterate {mode:'auto'}`（IT0 判为 merge）
   → 断言 v2 md/docx 生成、文件名时间戳不同、v1 文件仍在磁盘（禁覆盖）、
   chat 通道出现「## 合并摘要（留档）」、受影响章节（第六章）变化而其余章节**逐字未变**；
2. **correct** —— 指出一处术语错误 → 断言 v3 生成、全文该术语被替换且替换计数 > 0、
   chat 通道出现「## 纠正摘要（留档）」；
3. **question** —— IT0 判为 question → 只答疑，不产生新版本。

另覆盖：`revision_log_md` 交付物累计两轮记录、`GET /disclosure/revisions` 契约、
定稿分章切分 / 章节定位 / 补丁分章应用 / run_group 递增等确定性单元。

流水线一律经 REST 驱动（`/pipeline/start`、`/disclosure/iterate`、`/pipeline/input`），
后台任务跑在 TestClient 的事件循环里，测试侧只做同步轮询 —— 避免跨事件循环唤醒。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from conftest import disk_path
from typing import Any

import pytest
import test_disclosure as td
from fastapi.testclient import TestClient

API = "/api/v1"


# ---------------------------------------------------------------------------
# 迭代轮的脚本化产物
# ---------------------------------------------------------------------------

NEW_MATERIAL_NAME = "gray_release.md"
NEW_MATERIAL_MD = """# 批量作业调度平台 · 灰度放量补充说明

## 1. 背景
一期上线后需要按节点集合分批放量，避免一次性切换到全量节点带来的风险。

## 2. 补充设计
- 先只对灰度节点集合采集负载画像并计算匹配分；
- 灰度阶段采用更保守的最小派发间隔；
- 执行反馈连续三轮无异常后再把节点集合扩大到全量。
"""

ITER_DIGEST: dict[str, Any] = {
    "file": NEW_MATERIAL_NAME,
    "priority": 2,
    "summary_zh": "补充说明灰度放量：先在灰度节点集合上执行批量作业调度，再按执行反馈扩大到全量节点。",
    "tech_points": [
        "按节点集合分批放量",
        "灰度阶段采用更保守的最小派发间隔",
        "执行反馈连续三轮无异常后扩容到全量",
    ],
    "components": ["灰度节点集合"],
    "figures_mentioned": [],
    "sensitive_hits": [],
    "type_signals": {"invention": 0.9, "utility_model": 0.1, "design": 0.0},
}

INCREMENTS: dict[str, Any] = {
    "increments": [
        {
            "target_chapter": "六",
            "summary": "补一个灰度放量的实施例：先在灰度节点集合上跑 S1 至 S4，执行反馈连续三轮无异常后扩到全量。",
            "new_terms": ["灰度节点集合"],
            "needs_search": False,
        }
    ],
    "figure_plan_updated": False,
    "note": "只影响第六章实施例，未改变技术实质，无需补充检索。",
}

EXTRA_EMBODIMENT = """**实施例三：灰度放量下的批量作业调度**

本实施例在 S1 至 S4 的主路径上叠加灰度放量：负载画像采集模块先只对灰度节点集合采集负载画像，
批量作业调度决策模块在 S2 仅对灰度节点集合计算匹配分，限频控制模块在 S3 按更保守的最小派发间隔
裁决，S4 派发成功且执行反馈连续三轮无异常后再把节点集合扩大到全量。该实施例说明本方案可以在不
改动流程结构的前提下按节点集合分批放量。

"""

MERGE_CH6 = td.CH6.replace("### 技术效果", EXTRA_EMBODIMENT + "### 技术效果")

MERGE_SUMMARY_BODY = """本轮只改动了第六章「其它」，在原有两个实施例之外补入「灰度放量下的批量作业调度」实施例。
改动原因是用户补充了按节点集合分批放量的材料，需要在实施例层面体现该实施方式。
本轮未改动第五章的保护点，也未改动第一章的检索结论，因此不影响保护范围与查新论述。
已按 8.1 复核标题实词与实施例步骤号的贯穿，8.2 涉及的公式与符号本轮未动，8.3 脱敏与文末清洁均通过。
本轮未做术语族替换，全篇叫法保持一致。"""

OLD_TERM = "排队时延抖动"
NEW_TERM = "排队等待时延波动"

CORRECTIONS: dict[str, Any] = {
    "corrections": [
        {
            "category": "术语与可读性",
            "quote": OLD_TERM,
            "fix_intent": f"统一改为「{NEW_TERM}」，与 3.1 的场景定义对齐",
            "target_chapter": "全文",
        }
    ],
    "note": "仅表述问题，不涉及技术事实。",
}

TERM_PLAN: dict[str, Any] = {
    "replacements": [
        {"old": OLD_TERM, "new": NEW_TERM, "reason": "与 3.1 场景定义中的叫法对齐"}
    ],
    "note": "",
}

CORRECT_SUMMARY_BODY = f"""本轮把全篇的旧叫法统一改为「{NEW_TERM}」，涉及 1.1 局限性、1.2 缺点、第二章技术问题与第四章优点等位置。
依据是用户指出旧叫法与 3.1 场景定义中的说法不一致、不够贴合领域表述。
本次仅调整表述，未改动技术方案，也未影响第五章保护点与第一章的检索结论。
术语族替换已整篇对齐，旧叫法未在正文中残留。"""

QUESTION_ANSWER = """第五章的四条保护点分别对应 3.4 中的 S1—S4：采集负载画像、计算匹配分、
双条件限频裁决与派发后回写执行反馈。其中「双条件」指最小派发间隔与画像变化幅度，
两者同时满足才放行本次派发，具体取值见 3.5 的关键技术参数。本次仅作答疑，未改动定稿。"""


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------



@pytest.fixture()
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> td.FakeLLM:
    from app.services import llm

    fake = td.FakeLLM()
    monkeypatch.setattr(llm, "chat", fake.chat)
    monkeypatch.setattr(llm, "chat_stream", fake.chat_stream)
    monkeypatch.setattr(llm, "structured", fake.structured)
    return fake


class EventTap:
    """把 SSE hub 的 emit 旁路录一份，用于断言 chat / doc 通道的真实输出。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services.sse import hub

        original = hub.emit

        async def emit(case_id: str, event: str, data: Any, **kwargs: Any):
            self.events.append({"case_id": case_id, "event": event, "data": data})
            return await original(case_id, event, data, **kwargs)

        monkeypatch.setattr(hub, "emit", emit)

    def reset(self) -> None:
        self.events.clear()

    def channel_text(self, channel: str = "chat") -> str:
        parts: list[str] = []
        for item in self.events:
            if item["event"] != "llm_delta":
                continue
            data = item["data"]
            if isinstance(data, dict) and data.get("channel") == channel:
                parts.append(str(data.get("text") or ""))
        return "".join(parts)

    def names(self) -> list[str]:
        return [e["event"] for e in self.events]


@pytest.fixture()
def tap(monkeypatch: pytest.MonkeyPatch) -> EventTap:
    recorder = EventTap()
    recorder.install(monkeypatch)
    return recorder


# ---------------------------------------------------------------------------
# REST 驱动工具
# ---------------------------------------------------------------------------


def _new_case(client: TestClient, title: str, module: str = "disclosure") -> str:
    resp = client.post(f"{API}/cases", json={"module": module, "title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _upload(client: TestClient, case_id: str, name: str, text: str) -> str:
    resp = client.post(
        f"{API}/cases/{case_id}/files",
        files={"files": (name, text.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()[0]["file"]["id"]


def _pipeline_state(client: TestClient, case_id: str) -> dict[str, Any]:
    resp = client.get(f"{API}/cases/{case_id}/pipeline/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _case(client: TestClient, case_id: str) -> dict[str, Any]:
    resp = client.get(f"{API}/cases/{case_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _drive(
    client: TestClient,
    case_id: str,
    answers: dict[str, Any],
    *,
    run_group: str,
    timeout: float = 900.0,
) -> tuple[list[str], str]:
    """轮询门控并按 step_key 提交预置答复，直到本轮 run_group 跑完。

    返回 `(遇到的门控顺序, 案件终态)`。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:  # 先等本轮 run_group 生效（避免读到上一轮的终态）
        if _pipeline_state(client, case_id)["run_group"] == run_group:
            break
        time.sleep(0.05)
    else:  # pragma: no cover
        raise AssertionError(f"等待 run_group={run_group} 生效超时")

    seen: list[str] = []
    while time.monotonic() < deadline:
        state = _pipeline_state(client, case_id)
        pending = state.get("pending_interaction")
        if pending is not None:
            step_key = pending["step_key"]
            if step_key not in seen:
                seen.append(step_key)
            client.post(
                f"{API}/cases/{case_id}/pipeline/input",
                json={"step_key": step_key, "payload": answers.get(step_key, {})},
            )
            continue
        status = _case(client, case_id)["status"]
        if status in ("completed", "failed"):
            return seen, status
        time.sleep(0.05)
    raise AssertionError(f"流水线 {run_group} 未在 {timeout}s 内结束；最后状态={_failures(case_id)}")


def _failures(case_id: str) -> str:
    from app.db import database as db

    rows = db.query_all(
        "SELECT step_key, status, error FROM pipeline_runs WHERE case_id=? AND status='failed'",
        (case_id,),
    )
    return "；".join(f"{r['step_key']}: {r['error']}" for r in rows) or "无失败步骤记录"


def _state(case_id: str) -> dict[str, Any]:
    from app.db import database as db

    row = db.query_one("SELECT state_json FROM cases WHERE id=?", (case_id,))
    return json.loads(row["state_json"] or "{}")


def _artifacts(case_id: str, kind: str) -> list[dict[str, Any]]:
    from app.db import database as db

    rows = db.query_all(
        "SELECT * FROM artifacts WHERE case_id=? AND kind=? ORDER BY version ASC", (case_id, kind)
    )
    return [dict(r) for r in rows]


def _iterate(
    client: TestClient, case_id: str, *, mode: str = "auto", instruction: str = "", file_ids=()
) -> dict[str, Any]:
    resp = client.post(
        f"{API}/cases/{case_id}/disclosure/iterate",
        json={"mode": mode, "instruction": instruction, "file_ids": list(file_ids)},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 主用例：主流水线 → merge → correct → question
# ---------------------------------------------------------------------------

_run: dict[str, Any] = {}


def test_iterations_end_to_end(
    client: TestClient, fake_llm: td.FakeLLM, tap: EventTap, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次主流水线 + 三轮迭代（合并 / 纠正 / 答疑）全跑通。"""
    from app.pipelines import disclosure_iterate as iterate_pipeline

    td._install_search(monkeypatch)
    case_id = _new_case(client, "交底书迭代测试")
    file_id = _upload(client, case_id, "material.md", td.MATERIAL_MD)

    # ---------- 0. 主流水线跑到 deliver ----------
    started = client.post(
        f"{API}/cases/{case_id}/pipeline/start",
        json={"file_ids": [file_id], "instruction": "批量作业调度", "patent_type": "invention"},
    )
    assert started.status_code == 202, started.text
    _, status = _drive(client, case_id, td.ANSWERS, run_group="initial")
    assert status == "completed", _failures(case_id)

    base_state = _state(case_id)
    _run["case_id"] = case_id
    _run["v1_markdown"] = base_state["final_markdown"]
    _run["v1_chapters"] = iterate_pipeline.split_document(base_state["final_markdown"])[1]
    _run["contact"] = base_state.get("contact")

    # 未定稿的案件不允许迭代（另建一个空案件验证 409）
    empty = _new_case(client, "尚未成文的交底书")
    blocked = client.post(
        f"{API}/cases/{empty}/disclosure/iterate", json={"mode": "auto", "instruction": "改一下"}
    )
    assert blocked.status_code == 409 and "尚未产出交底书定稿" in blocked.json()["detail"]

    # ---------- 1. merge 轮 ----------
    new_file = _upload(client, case_id, NEW_MATERIAL_NAME, NEW_MATERIAL_MD)
    fake_llm.struct_script.update(
        {
            "intent": {"intent": "merge", "rationale": "用户上传了新材料并要求补进实施例", "points": ["灰度放量"]},
            "iter.digest.0": ITER_DIGEST,
            "increments": INCREMENTS,
            "audit": td.SELF_CHECK_PASS,      # 迭代轮的自检：首轮补丁已落地，本轮无新补丁
            "claim_bias": td.CLAIM_BIAS_FIXED,
        }
    )
    fake_llm.stream_script.update(
        {"merge.g7": MERGE_CH6, "merge_summary": MERGE_SUMMARY_BODY}
    )

    tap.reset()
    merge_resp = _iterate(
        client,
        case_id,
        mode="auto",
        instruction="补一份灰度放量的说明，请把它并进第六章的实施例里。",
        file_ids=[new_file],
    )
    assert merge_resp == {"run_group": "iteration-1", "mode": "auto", "round": 1}
    seen, status = _drive(
        client, case_id, {"iterate_deliver": {"skip": True}}, run_group="iteration-1"
    )
    assert status == "completed", _failures(case_id)
    assert seen == ["iterate_deliver"], f"合并轮的门控不符预期：{seen}"

    _run["merge_chat"] = tap.channel_text("chat")
    _run["merge_events"] = tap.names()
    _run["merge_state"] = _state(case_id)
    _run["v2_markdown"] = _run["merge_state"]["final_markdown"]
    _run["v2_chapters"] = iterate_pipeline.split_document(_run["v2_markdown"])[1]

    # ---------- 2. correct 轮 ----------
    fake_llm.struct_script.update(
        {
            "intent": {"intent": "correct", "rationale": "用户指出某个叫法与 3.1 场景定义不一致"},
            "corrections": CORRECTIONS,
            "correct.terms1": TERM_PLAN,
        }
    )
    fake_llm.stream_script["correct_summary"] = CORRECT_SUMMARY_BODY

    tap.reset()
    correct_resp = _iterate(
        client,
        case_id,
        mode="auto",
        instruction=f"「{OLD_TERM}」这个说法不对，全篇统一改成「{NEW_TERM}」。",
    )
    assert correct_resp["run_group"] == "iteration-2" and correct_resp["round"] == 2
    seen, status = _drive(client, case_id, {}, run_group="iteration-2")
    assert status == "completed", _failures(case_id)
    assert seen == [], f"纠正轮不应出现门控：{seen}"

    _run["correct_chat"] = tap.channel_text("chat")
    _run["correct_state"] = _state(case_id)
    _run["v3_markdown"] = _run["correct_state"]["final_markdown"]

    # ---------- 3. question 轮 ----------
    fake_llm.struct_script["intent"] = {"intent": "question", "rationale": "用户只是要求解释"}
    fake_llm.stream_script["answer"] = QUESTION_ANSWER

    tap.reset()
    question_resp = _iterate(
        client, case_id, mode="auto", instruction="第五章的四条保护点分别对应哪几个步骤？"
    )
    assert question_resp["run_group"] == "iteration-3"
    seen, status = _drive(client, case_id, {}, run_group="iteration-3")
    assert status == "completed", _failures(case_id)
    assert seen == [], f"答疑轮不应出现门控：{seen}"

    _run["question_chat"] = tap.channel_text("chat")
    _run["question_state"] = _state(case_id)
    _run["revisions"] = client.get(f"{API}/cases/{case_id}/disclosure/revisions").json()
    _run["calls"] = list(fake_llm.calls)


# ---------------------------------------------------------------------------
# merge 轮断言
# ---------------------------------------------------------------------------


def test_merge_produces_new_version_without_overwriting() -> None:
    """合并轮产出 v2 md/docx；文件名时间戳不同且 v1 文件仍在磁盘（只增不改）。"""
    case_id = _run["case_id"]
    mds = _artifacts(case_id, "disclosure_md")
    assert len(mds) >= 2
    v1, v2 = mds[0], mds[1]
    assert (v1["version"], v2["version"]) == (1, 2)
    assert v1["iteration_type"] == "initial" and v2["iteration_type"] == "merge"
    assert v2["run_group"] == "iteration-1"
    assert v2["source_artifact_id"] == v1["id"], "新版本应链回上一版"

    assert v1["filename"] != v2["filename"], "迭代必须落新时间戳文件名"
    assert disk_path(v1["stored_path"]).is_file(), "旧稿被覆盖/删除了"
    assert disk_path(v2["stored_path"]).is_file()
    assert disk_path(v1["stored_path"]).read_text(encoding="utf-8") == _run["v1_markdown"]
    assert disk_path(v2["stored_path"]).read_text(encoding="utf-8") == _run["v2_markdown"]
    assert _run["v2_markdown"] != _run["v1_markdown"]

    docxs = _artifacts(case_id, "disclosure_docx")
    assert len(docxs) >= 2 and docxs[1]["version"] == 2
    assert docxs[0]["filename"] != docxs[1]["filename"]
    assert disk_path(docxs[0]["stored_path"]).is_file() and disk_path(docxs[1]["stored_path"]).is_file()


def test_merge_touches_only_affected_chapter() -> None:
    """只有受影响章节（第六章）内容变化，其余章节逐字未变。"""
    v1 = _run["v1_chapters"]
    v2 = _run["v2_chapters"]
    assert set(v1) == set(v2) == {"g1", "g2", "g3", "g5", "g6", "g7"}
    assert v1["g7"] != v2["g7"], "第六章应被合并改写"
    assert "实施例三" in v2["g7"] and "灰度节点集合" in v2["g7"]
    for key in ("g1", "g2", "g3", "g5", "g6"):
        assert v1[key] == v2[key], f"未受影响的章节 {key} 被改动了"

    state = _run["merge_state"]
    iteration = state["iteration"]
    assert iteration["intent"] == "merge"
    assert iteration["affected"] == ["g7"]
    assert iteration["merge"]["increments"][0]["chapter_key"] == "g7"
    assert iteration["merge"]["digested"][0]["file"] == NEW_MATERIAL_NAME
    assert iteration["merge"]["search"]["triggered"] is False
    assert iteration["merge"]["formula"]["rechecked"] is True
    assert iteration["merge"]["formula"]["ok"] is True


def test_merge_summary_forced_into_chat_channel() -> None:
    """「## 合并摘要（留档）」强制出现在 chat 通道，并留档进 artifacts.summary。"""
    chat = _run["merge_chat"]
    assert "## 合并摘要（留档）" in chat, "合并摘要未推到 chat 通道"
    assert "实施例" in chat
    assert "## 纠正摘要（留档）" not in chat

    summary = _run["merge_state"]["iteration"]["summary_md"]
    assert summary.startswith("## 合并摘要（留档）")
    assert summary.count("## 合并摘要（留档）") == 1

    v2 = _artifacts(_run["case_id"], "disclosure_md")[1]
    assert v2["summary"] and "## 合并摘要（留档）" in v2["summary"]

    # 交付事件齐备：新版本 + 文档版本广播
    assert "artifact_created" in _run["merge_events"]
    assert "doc_version" in _run["merge_events"]


def test_merge_claim_bias_guidance() -> None:
    """合并稿交付后再附「权利要求偏向点」引导；用户跳过时不入正文。"""
    bias = _run["merge_state"]["iteration"]["deliver"]["claim_bias"]
    assert bias["dropped"] == 0 and len(bias["groups"]) == 1
    text = _run["v2_markdown"]
    for option in ("option_a", "option_b"):
        assert bias["groups"][0][option]["basis_quote"] in text
    assert bias["selection"]["skipped"] is True
    assert bias["groups"][0]["axis"] not in text


# ---------------------------------------------------------------------------
# correct 轮断言
# ---------------------------------------------------------------------------


def test_correct_replaces_term_family() -> None:
    """纠正轮：v3 生成，全文旧叫法被替换且替换计数 > 0。"""
    case_id = _run["case_id"]
    mds = _artifacts(case_id, "disclosure_md")
    assert len(mds) >= 3
    v3 = mds[2]
    assert v3["version"] == 3 and v3["iteration_type"] == "correction"
    assert v3["run_group"] == "iteration-2"
    assert disk_path(v3["stored_path"]).is_file()
    assert mds[1]["filename"] != v3["filename"]
    assert disk_path(mds[1]["stored_path"]).is_file(), "纠正轮覆盖了合并轮的文件"

    text = _run["v3_markdown"]
    assert OLD_TERM not in text, "旧叫法仍残留在正文中"
    assert NEW_TERM in text
    assert _run["v2_markdown"].count(OLD_TERM) > 0     # 上一版确实存在该叫法

    correct = _run["correct_state"]["iteration"]["correct"]
    replaced = correct["term_replacements"]
    assert len(replaced) == 1
    assert replaced[0]["from"] == OLD_TERM and replaced[0]["to"] == NEW_TERM
    assert replaced[0]["count"] > 0, "术语替换计数应大于 0"
    assert replaced[0]["count"] == _run["v2_markdown"].count(OLD_TERM)
    assert correct["corrections"][0]["category"] == "术语与可读性"
    assert correct["patches"][0]["kind"] == "term_family"
    assert correct["touched"], "应记录被改动的章节"


def test_correct_summary_forced_into_chat_channel() -> None:
    """「## 纠正摘要（留档）」强制出现在 chat 通道。"""
    chat = _run["correct_chat"]
    assert "## 纠正摘要（留档）" in chat
    assert "## 合并摘要（留档）" not in chat
    summary = _run["correct_state"]["iteration"]["summary_md"]
    assert summary.startswith("## 纠正摘要（留档）")
    v3 = _artifacts(_run["case_id"], "disclosure_md")[2]
    assert "## 纠正摘要（留档）" in (v3["summary"] or "")


def test_correct_chapters_stay_in_sync() -> None:
    """纠正后分章视图与全文一致（后续轮次可以继续在分章上改写）。"""
    from app.pipelines import disclosure_iterate as iterate_pipeline

    state = _run["correct_state"]
    chapters = state["chapters"]
    split = iterate_pipeline.split_document(state["final_markdown"])[1]
    assert set(split) == set(chapters)
    for key, text in split.items():
        assert text == chapters[key].strip(), f"章节 {key} 与全文不同步"


# ---------------------------------------------------------------------------
# question 轮断言
# ---------------------------------------------------------------------------


def test_question_round_does_not_produce_new_version() -> None:
    """答疑轮只在 chat 通道回答，不产生新版本、不改稿。"""
    case_id = _run["case_id"]
    assert len(_artifacts(case_id, "disclosure_md")) == 3, "答疑轮不应产生新版本"
    assert len(_artifacts(case_id, "disclosure_docx")) == 3

    iteration = _run["question_state"]["iteration"]
    assert iteration["intent"] == "question"
    assert iteration["status"] == "answered"
    assert "answer" in iteration and iteration["answer"].strip()
    assert "deliver" not in iteration

    assert _run["question_state"]["final_markdown"] == _run["v3_markdown"], "答疑轮改动了定稿"
    assert "保护点" in _run["question_chat"]
    assert "留档）" not in _run["question_chat"]


# ---------------------------------------------------------------------------
# 修订记录
# ---------------------------------------------------------------------------


def test_iteration_binary_artifacts() -> None:
    """每轮迭代同步产出 PNG 附图与（引擎可用时）PDF，且文件名互不相同。"""
    case_id = _run["case_id"]
    pngs = _artifacts(case_id, "mermaid_png")
    if pngs:
        assert len(pngs) == 6, "初稿 + 两轮迭代各 2 幅（3.2 框图 + 3.4 流程）"
        assert [p["iteration_type"] for p in pngs] == [
            "initial", "initial", "merge", "merge", "correction", "correction"
        ]
        for item in pngs:
            assert disk_path(item["stored_path"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    pdfs = _artifacts(case_id, "disclosure_pdf")
    files = _run["merge_state"]["iteration"]["deliver"]["files"]
    if not pdfs:
        pytest.skip(f"docx→pdf 引擎不可用：{files.get('pdf_error')}")
    assert len(pdfs) == 3
    assert len({p["filename"] for p in pdfs}) == 3
    for item in pdfs:
        assert disk_path(item["stored_path"]).read_bytes()[:5] == b"%PDF-"
    assert files.get("pdf_engine") in ("word", "soffice")


def test_revision_log_artifact_contains_two_rounds() -> None:
    """`revision_log_md` 每轮重写新版本，最新一版累计含初稿 + 两轮迭代。"""
    case_id = _run["case_id"]
    logs = _artifacts(case_id, "revision_log_md")
    assert len(logs) == 2, "合并轮与纠正轮各应导出一版修订对话记录"
    assert logs[0]["filename"] != logs[1]["filename"]
    assert logs[0]["filename"].startswith("交底书修订对话记录_")

    latest = disk_path(logs[1]["stored_path"]).read_text(encoding="utf-8")
    assert latest.startswith("# 交底书修订对话记录")
    assert f"**案件名称**：{td.CASE_TITLE}" in latest
    assert "**累计轮次**：3" in latest
    assert "初稿" in latest and "合并迭代" in latest and "纠正迭代" in latest
    assert "合并摘要（留档）" in latest and "纠正摘要（留档）" in latest
    assert "## 合并摘要（留档）" not in latest      # 摘录以引用块呈现，标题记号已剥离
    assert "第 2 轮 · 合并迭代" in latest and "第 3 轮 · 纠正迭代" in latest
    # 五要素齐备
    for element in ("**记录时间**", "**类型**", "**用户说明摘要**", "**交付文件**", "**摘要摘录**"):
        assert element in latest, f"修订记录缺要素：{element}"

    first = disk_path(logs[0]["stored_path"]).read_text(encoding="utf-8")
    assert "**累计轮次**：2" in first, "第一次导出应只有初稿 + 合并轮"


def test_revisions_endpoint_contract() -> None:
    """`GET /cases/{id}/disclosure/revisions` 返回五要素列表 + 最近一版记录文件。"""
    data = _run["revisions"]
    assert data["case_id"] == _run["case_id"]
    assert data["count"] == 3
    kinds = [r["iteration_type"] for r in data["records"]]
    assert kinds == ["initial", "merge", "correction"]
    labels = [r["type_label"] for r in data["records"]]
    assert labels == ["初稿", "合并迭代", "纠正迭代"]

    merge_record = data["records"][1]
    assert merge_record["recorded_at"]
    assert merge_record["instruction"].startswith("补一份灰度放量的说明")
    assert "## 合并摘要（留档）" in merge_record["summary"]
    assert merge_record["run_group"] == "iteration-1"
    assert merge_record["intent"] == "merge"
    assert any(f.endswith(".md") for f in merge_record["files"])
    assert any(f.endswith(".docx") for f in merge_record["files"])

    assert data["log_artifact_id"] and data["log_filename"].startswith("交底书修订对话记录_")


def test_iteration_llm_call_trace() -> None:
    """迭代各阶段的 LLM 调用位齐备（意图路由 / 增量 / 章节重写 / 自检 / 摘要 / 偏向点）。"""
    calls = _run["calls"]
    for expected in (
        "struct:intent",
        "struct:iter.digest.0",
        "struct:increments",
        "stream:merge.g7",
        "stream:merge_summary",
        "struct:claim_bias",
        "struct:corrections",
        "struct:correct.terms1",
        "stream:correct_summary",
        "stream:answer",
    ):
        assert expected in calls, f"缺少迭代调用：{expected}"
    assert calls.count("struct:intent") == 3, "三轮迭代各走一次意图路由"


# ---------------------------------------------------------------------------
# API 契约（错误路径）
# ---------------------------------------------------------------------------


def test_iterate_api_error_paths(client: TestClient) -> None:
    """404 / 400 / 409 三类错误路径。"""
    missing = client.post(
        f"{API}/cases/no-such-case/disclosure/iterate", json={"mode": "auto", "instruction": ""}
    )
    assert missing.status_code == 404
    assert client.get(f"{API}/cases/no-such-case/disclosure/revisions").status_code == 404

    other = _new_case(client, "论文转专利案件", module="paper2patent")
    wrong = client.post(f"{API}/cases/{other}/disclosure/iterate", json={"mode": "auto"})
    assert wrong.status_code == 400 and "不是交底书案件" in wrong.json()["detail"]


def test_iterate_rejects_unsupported_patent_type(client: TestClient) -> None:
    """实用新型 / 外观在 M5 前不支持迭代（400 友好错误，而非 500）。"""
    from app.db import database as db
    from app.pipelines import disclosure_iterate as iterate_pipeline
    from app.pipelines.disclosure import PatentTypeNotSupportedError

    with pytest.raises(PatentTypeNotSupportedError):
        iterate_pipeline.build_iteration_steps({"patent_type": "utility_model"})

    case_id = _new_case(client, "实用新型迭代")
    db.execute("UPDATE cases SET patent_type='utility_model' WHERE id=?", (case_id,))
    # 造一条已交付记录，让类型校验成为唯一失败原因
    from app.services import artifacts as artifacts_service

    artifacts_service.save_artifact_sync(
        case_id, "disclosure_md", "# 技术交底书\n\n正文", "md", title="实用新型迭代",
        run_group="initial", iteration_type="initial",
    )
    resp = client.post(f"{API}/cases/{case_id}/disclosure/iterate", json={"mode": "auto"})
    assert resp.status_code == 400 and "实用新型" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 确定性单元
# ---------------------------------------------------------------------------


def test_split_document_roundtrip() -> None:
    """定稿切分：六章齐全、文头不丢、拼回与原文等价。"""
    from app.pipelines import disclosure_iterate as iterate_pipeline
    from app.services import disclosure_build as build_service

    text = _run["v1_markdown"]
    header, chapters = iterate_pipeline.split_document(text)
    assert header.lstrip().startswith("# 技术交底书")
    assert "## 注意事项" in header
    assert list(chapters) == ["g1", "g2", "g3", "g5", "g6", "g7"]
    assert chapters["g1"].startswith("## 一、")
    assert chapters["g2"].startswith("## 三、")
    assert chapters["g3"].startswith("### 3.4")
    assert chapters["g5"].startswith("#### 3.4.1")
    assert chapters["g6"].startswith("## 四、")
    assert chapters["g7"].startswith("## 六、")

    rebuilt = build_service.assemble_document(
        chapters, case_title=td.CASE_TITLE, contact=_run["contact"]
    )
    assert rebuilt == text, "切分后原样拼回应与定稿逐字一致"

    assert iterate_pipeline.split_document("") == ("", {})
    assert iterate_pipeline.split_document("没有任何章节锚点的文本")[1] == {}


def test_chapter_key_mapping() -> None:
    """增量/纠正点的 target_chapter → 章节 key。"""
    from app.pipelines.disclosure_iterate import chapter_key

    assert chapter_key("1.1") == "g1"
    assert chapter_key("第一章") == "g1"
    assert chapter_key("二") == "g1"
    assert chapter_key("3.2 系统框图") == "g2"
    assert chapter_key("三") == "g2"
    assert chapter_key("3.4") == "g3"
    assert chapter_key("3.4.1") == "g5"          # 小数点更长的写法优先
    assert chapter_key("3.5") == "g5"
    assert chapter_key("第五章") == "g6"
    assert chapter_key("六") == "g7"
    assert chapter_key("实施例") == "g7"
    assert chapter_key("g3") == "g3"
    assert chapter_key("") is None
    assert chapter_key("与本案无关的位置") is None


def test_normalize_correction_category() -> None:
    """纠正点五分类归一化（未知分类落到「事实与技术」）。"""
    from app.pipelines.disclosure_iterate import CORRECTION_CATEGORIES, normalize_category

    for name in CORRECTION_CATEGORIES:
        assert normalize_category(name) == name
    assert normalize_category("术语") == "术语与可读性"
    assert normalize_category("公式体例") == "符号与公式体例"
    assert normalize_category("查新") == "查新与区别"
    assert normalize_category("附图") == "保护点与表述与附图"
    assert normalize_category("说不清楚") == "事实与技术"


def test_apply_patches_to_chapters() -> None:
    """补丁按所属章节分组后逐章应用；定位不到的补丁进 unresolved。"""
    from app.pipelines.disclosure_iterate import apply_patches_to_chapters

    chapters = {"g1": "第一章讲的是甲方案。", "g6": "第五章保护点是乙机制。"}
    result = apply_patches_to_chapters(
        chapters,
        [
            {"checklist_item": "事实", "original_excerpt": "甲方案", "replacement": "甲改进方案"},
            {"checklist_item": "保护点", "original_excerpt": "乙机制", "replacement": "乙判定机制"},
            {"checklist_item": "找不到", "original_excerpt": "丙方案", "replacement": "丁方案"},
        ],
    )
    assert chapters["g1"] == "第一章讲的是甲改进方案。"
    assert chapters["g6"] == "第五章保护点是乙判定机制。"
    assert result["applied"] == 2 and result["total"] == 3 and result["dropped"] == 1
    assert sorted(result["chapters"]) == ["g1", "g6"]
    assert any("丙方案" in u for u in result["unresolved"])


def test_apply_term_plan_counts() -> None:
    """术语族替换按章执行并回报替换计数。"""
    from app.pipelines.disclosure_iterate import TermReplacePlan, apply_term_plan

    chapters = {"g1": "水位偏高时暂停派发。", "g2": "水位由采集模块给出。", "g6": "与水位无关。"}
    replaced, touched = apply_term_plan(
        chapters, TermReplacePlan.model_validate({"replacements": [{"old": "水位", "new": "负载画像"}]})
    )
    assert replaced[0] == {"from": "水位", "to": "负载画像", "count": 3, "reason": ""}
    assert touched == ["g1", "g2", "g6"]
    assert all("水位" not in t for t in chapters.values())

    # 空 / 同名替换被忽略
    empty, none_touched = apply_term_plan(
        chapters, TermReplacePlan.model_validate({"replacements": [{"old": "x", "new": "x"}, {"old": "", "new": "y"}]})
    )
    assert empty == [] and none_touched == []


def test_next_run_group_increments() -> None:
    """run_group 按已有迭代轮次递增。"""
    from app.pipelines.disclosure_iterate import iteration_round, next_run_group_sync

    assert next_run_group_sync(_run["case_id"]) == "iteration-4"
    assert next_run_group_sync("case-without-any-run") == "iteration-1"
    assert iteration_round("iteration-7") == 7
    assert iteration_round("initial") == 1


def test_revision_log_markdown_rendering() -> None:
    """修订记录渲染：无记录时给出明确说明；有记录时五要素齐备。"""
    from app.services import revision_log

    assert "尚无交付记录" in revision_log.render_markdown("某案件", [])
    text = revision_log.render_markdown(
        "一种示例方法及系统",
        [
            revision_log.RevisionRecord(
                recorded_at="2026-08-25 14:32:00",
                iteration_type="merge",
                instruction="补一份材料",
                files=["a.md", "a.docx"],
                summary="## 合并摘要（留档）\n改了第六章。",
                run_group="iteration-1",
                intent="merge",
            )
        ],
    )
    assert "第 1 轮 · 合并迭代" in text
    assert "- **记录时间**：2026-08-25 14:32:00" in text
    assert "  - a.docx" in text
    assert "  > 改了第六章。" in text


class _StubCtx:
    """`_supplementary_search` 只用到 case_id / step_key / state / emit。"""

    def __init__(self, state: dict[str, Any]) -> None:
        self.case_id = "stub-case"
        self.step_key = "iterate_rewrite"
        self.state = state
        self.logs: list[tuple[str, Any]] = []

    async def emit(self, event: str, data: Any, **kwargs: Any) -> None:
        self.logs.append((event, data))


def _log_text(ctx: _StubCtx) -> str:
    return "\n".join(str(d.get("message", "")) for _e, d in ctx.logs if isinstance(d, dict))


async def test_supplementary_search_failure_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """needs_search 触发的补充查新失败时只记日志、不阻断迭代，1.1 不动。"""
    from app.models.search import SearchResult
    from app.pipelines import disclosure_iterate as iterate_pipeline
    from app.services import cnipa

    async def _noop(stage: str, msg: str) -> None:
        return None

    async def failing_search(case_id, terms, patent_type="invention", **kwargs):
        return SearchResult(
            status="failed", hits=[], error="浏览器不可用（测试桩）",
            terms=list(terms), patent_type=patent_type,
        )

    monkeypatch.setattr(cnipa, "hub_progress", lambda *a, **k: _noop)
    monkeypatch.setattr(cnipa, "search", failing_search)

    ctx = _StubCtx({"prior_art": {"terms": ["批量作业调度", "负载画像"], "type_param": "invention"}})
    report = await iterate_pipeline._supplementary_search(ctx, ["灰度节点集合"])

    assert report["status"] == "failed" and report["added"] == 0
    assert report["terms"][0] == "灰度节点集合"          # 新词优先，其后接原检索词
    assert "补充查新未取得可用结果" in _log_text(ctx)


async def test_supplementary_search_skips_when_terms_too_few(monkeypatch: pytest.MonkeyPatch) -> None:
    """检索单位不足 2 个时直接跳过补充查新（不发起爬虫）。"""
    from app.pipelines import disclosure_iterate as iterate_pipeline
    from app.services import cnipa

    async def boom(*args, **kwargs):  # pragma: no cover —— 不应被调用
        raise AssertionError("检索单位不足时不应发起检索")

    monkeypatch.setattr(cnipa, "search", boom)
    ctx = _StubCtx({"prior_art": {"terms": []}})
    report = await iterate_pipeline._supplementary_search(ctx, [])

    assert report["added"] == 0 and "不足 2 个" in report["error"]
    assert "不足 2 个" in _log_text(ctx)


def test_registry_dispatches_to_iteration_steps_when_unfinished() -> None:
    """未跑完的迭代轮经 resume/retry 重建时给出迭代五步，而非主流水线八步。"""
    import json as _json

    from app.api.disclosure import build_disclosure_steps
    from app.db import database as db
    from app.pipelines import disclosure_iterate as iterate_pipeline
    from app.pipelines import registry

    case_id = _run["case_id"]
    row = dict(db.query_one("SELECT * FROM cases WHERE id=?", (case_id,)))
    assert _json.loads(row["state_json"])["_run_group"] == "iteration-3"

    # 三轮迭代都已跑完 → 仍回落主流水线八步（新的 /pipeline/start 不受影响）
    assert next(s.key for s in registry.build_steps(row)) == "intake"

    # 造一条 failed 的迭代步骤 → 重建时应给迭代五步
    db.execute(
        "INSERT INTO pipeline_runs(id, case_id, run_group, step_key, attempt, status, started_at) "
        "VALUES (?,?,?,?,?,'failed',?)",
        ("run-iter-fail", case_id, "iteration-3", "iterate_rewrite", 9, db.now_str()),
    )
    try:
        steps = build_disclosure_steps(row)
        assert [s.key for s in steps] == list(iterate_pipeline.ITERATION_STEP_KEYS)
    finally:
        db.execute("DELETE FROM pipeline_runs WHERE id='run-iter-fail'")
    assert next(s.key for s in build_disclosure_steps(row)) == "intake"


def test_iteration_prompt_parts_assemble() -> None:
    """迭代用到的 prompt 资产都能装配（缺文件会抛 KeyError）。"""
    from app.pipelines import disclosure_iterate as iterate_pipeline
    from app.services import assembler

    for parts in (
        iterate_pipeline.INTENT_PARTS,
        iterate_pipeline.MERGE_PARTS,
        iterate_pipeline.CORRECT_PARTS,
        iterate_pipeline.ANSWER_PARTS,
    ):
        prompt = assembler.assemble(parts, runtime_ctx={"case_card": {"module": "disclosure"}})
        assert len(prompt) > 200
        assert len(prompt.file_hashes) == len(parts)
