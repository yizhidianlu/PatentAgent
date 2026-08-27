"""模块 D（审查意见答复）端到端测试 + OA 案例库 API 测试。

不依赖真实 LLM：把 `app.services.llm` 的 chat_stream / structured 换成脚本化 FakeLLM
（按 step_key 尾缀回放预置产物），其余环节**全部真跑**：

- **通知书 PDF fixture 现造**：pymupdf 生成一份含两条审查意见（专利法第22条第3款
  创造性 + 专利法第26条第4款 得不到说明书支持）与对比文件 D1（CN102222222A）的
  通知书，经 `services/convert` 真实 PDF→md 后进入流水线；
- **R16 防条款幻觉**：首版结构化故意给出非法 statute（「专利法22条3款」）与非法
  defect_type（「creativity」），服务端枚举校验捕获 → REPAIR 一次修好；
- **三处强制人审**：oa_issues / oa_retrieve / oa_strategy 依次挂起且 payload 可回传；
- **检索方式明示**：retrieve 门控 payload 必带 `retrieval_mode`（本用例未配 embedding
  → keyword），且逐 issue 记录放宽过的过滤条件；
- **修改后权项过 lint**：amend_claims 首版修改稿含禁用词「优选」且一项权利要求里出现
  两个句号（error 级），触发一次 REPAIR 后修好；
- **跨条一致性**：确定性检查 + 一次 AUDIT 复核；冲突场景另有专项用例；
- **交付**：真跑 md→docx 子进程管线，python-docx 打开校验；
- **归档回案例库**：产 `draft` 状态案例笔记（人审后才进检索库）。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from conftest import disk_path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# 一、fixture：审查意见通知书 PDF（pymupdf 现造）与本申请权利要求书
# ---------------------------------------------------------------------------

APPLICATION_NO = "CN202410123456.7"
INVENTION_NAME = "一种基于图结构的超声图像病灶分割方法"

NOTICE_TEXT = f"""国家知识产权局
审查意见通知书（第一次）

申请号：{APPLICATION_NO}
申请人：引途医疗科技有限公司
发明名称：{INVENTION_NAME}
答复期限：自本通知书发文日起四个月内答复（2026-10-20 前）

一、关于专利法第22条第3款（创造性）
权利要求1相对于对比文件1不具备创造性。对比文件1（CN102222222A，以下称D1）公开了
一种超声图像分割方法，并公开了对图像进行区域划分并逐区域判定病灶的步骤。权利要求1
与D1的区别特征在于构建超像素邻接图并采用图卷积网络逐层聚合邻域特征，上述区别特征
属于本领域的惯用技术手段，故权利要求1不具备突出的实质性特点和显著的进步。

二、关于专利法第26条第4款（得不到说明书支持）
权利要求2限定所述超像素划分采用简单线性迭代聚类算法，但说明书中未给出该算法迭代
次数的对应记载，所属技术领域的技术人员无法从说明书公开的内容中得到或概括得出该
技术方案，故权利要求2得不到说明书的支持。

对比文件清单：
对比文件1：CN102222222A，公开日2011-01-01。
"""

CLAIM_1 = (
    "1. 一种基于图结构的超声图像病灶分割方法，其特征在于，包括："
    "对超声图像进行超像素划分，得到多个超像素节点；"
    "基于所述超像素节点的灰度特征与纹理特征构建邻接图；"
    "将所述邻接图输入图卷积网络，逐层聚合邻域特征，得到节点级病灶概率；"
    "根据所述节点级病灶概率归并所述超像素节点，生成病灶分割掩膜。"
)
CLAIM_2 = "2. 根据权利要求1所述的方法，其特征在于，所述超像素划分采用简单线性迭代聚类算法。"
CLAIM_3 = "3. 根据权利要求1所述的方法，其特征在于，所述邻接图的节点属性包括灰度特征与纹理特征。"

CLAIMS_TEXT = f"""权利要求书

{CLAIM_1}

{CLAIM_2}

{CLAIM_3}
"""

# 修改后权利要求2：首版含禁用词「优选」且出现两个句号（period 为 error 级 → 触发 REPAIR）
CLAIM_2_BAD = (
    "2. 根据权利要求1所述的方法，其特征在于，所述超像素划分优选采用简单线性迭代聚类算法。"
    "所述算法的迭代次数为10次。"
)
CLAIM_2_FIXED = (
    "2. 根据权利要求1所述的方法，其特征在于，所述超像素划分采用简单线性迭代聚类算法，"
    "且所述算法的迭代次数为10至20次。"
)


def build_notice_pdf() -> bytes:
    """用 pymupdf 现造一份审查意见通知书 PDF（内置 china-s 字体，无需外部字体文件）。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    left = page.insert_textbox(
        pymupdf.Rect(45, 45, 550, 790), NOTICE_TEXT, fontname="china-s", fontsize=9.5,
    )
    assert left >= 0, f"通知书正文未能排入单页（剩余高度 {left}）"
    payload: bytes = doc.tobytes()
    doc.close()
    return payload


# ---------------------------------------------------------------------------
# 二、FakeLLM 脚本
# ---------------------------------------------------------------------------

D1 = {"ref_label": "对比文件1", "pub_no": "CN102222222A", "used_against": [1]}

ISSUE_1_DIGEST = (
    "权利要求1相对于D1的区别在于构建超像素邻接图并用图卷积网络聚合邻域特征，"
    "审查员认为该区别属于本领域惯用技术手段，故不具备创造性。"
)
ISSUE_2_DIGEST = (
    "审查员认为权利要求2限定的简单线性迭代聚类算法缺少迭代次数的说明书记载，"
    "本领域技术人员无法概括得出该技术方案，故得不到说明书支持。"
)


def _notice(statute_1: str, defect_1: str) -> dict[str, Any]:
    """通知书结构化脚本（statute_1/defect_1 用于制造首版的非法枚举值）。"""
    return {
        "notice_kind": "office_action",
        "notice_no": 1,
        "application_no": APPLICATION_NO,
        "patent_type": "invention",
        "deadline": "2026-10-20",
        "issues": [
            {
                "id": "issue-1",
                "statute": statute_1,
                "defect_type": defect_1,
                "claims_involved": [1],
                "examiner_argument_digest": ISSUE_1_DIGEST,
                "cited_documents": [D1],
            },
            {
                "id": "issue-2",
                "statute": "专利法第26条第4款",
                "defect_type": "support",
                "claims_involved": [2],
                "examiner_argument_digest": ISSUE_2_DIGEST,
                "cited_documents": [],
            },
        ],
    }


#: 首版：statute 未照抄原文（缺「第…条」）+ defect_type 不在枚举内 → 服务端校验必须捕获
NOTICE_BAD = _notice("专利法22条3款", "creativity")
NOTICE_OK = _notice("专利法第22条第3款", "inventiveness")

STRATEGY_I1 = {
    "issue_id": "I1",
    "recommended": "argue_only",
    "options": [
        {
            "strategy": "argue_only",
            "rationale": "D1未公开超像素邻接图与图卷积聚合，区别特征带来降低斑点噪声误判的技术效果，可直接陈述创造性。",
            "risk": "",
            "amendment_sketch": "",
        },
        {
            "strategy": "amend_claims",
            "rationale": "如审查员坚持惯用手段的认定，可将说明书记载的节点属性补入权利要求1。",
            "risk": "补入特征均有说明书原文记载，超范围（专利法第33条）风险低。",
            "amendment_sketch": "将灰度特征与纹理特征作为节点属性补入权利要求1。",
        },
    ],
}

#: I2 首版：修改类策略缺 risk（服务端 lint_issue_strategy 必须捕获）→ REPAIR
STRATEGY_I2_BAD = {
    "issue_id": "I2",
    "recommended": "amend_claims",
    "options": [
        {
            "strategy": "amend_claims",
            "rationale": "将说明书已记载的迭代次数范围补入权利要求2，消除支持缺陷。",
            "risk": "",
            "amendment_sketch": "在权利要求2中补入迭代次数10至20次。",
        }
    ],
}
STRATEGY_I2_OK = json.loads(json.dumps(STRATEGY_I2_BAD))
STRATEGY_I2_OK["options"][0]["risk"] = (
    "迭代次数10至20次在说明书具体实施方式中有原文记载，不构成超范围修改（专利法第33条）。"
)

DRAFT_I1 = """### 一、关于专利法第22条第3款（创造性）

【审查意见理解】审查员认为权利要求1相对于D1的区别特征属于本领域惯用技术手段。

【答复意见】D1公开的是逐区域判定病灶的分割方法，并未公开以超像素节点构建邻接图、
再由图卷积网络逐层聚合邻域特征的技术手段。本申请说明书记载，该区别特征使相邻组织
区域之间的上下文关系被显式建模，从而降低斑点噪声导致的误判。上述效果并非本领域
惯用手段的常规效果，权利要求1具备突出的实质性特点和显著的进步。

【结论】权利要求1符合专利法第22条第3款的规定，请求审查员重新考虑。
"""

DRAFT_I2_BAD = f"""### 二、关于专利法第26条第4款（得不到说明书支持）

【审查意见理解】审查员认为权利要求2缺少迭代次数的说明书记载。

【答复意见】申请人将说明书具体实施方式中记载的迭代次数范围补入权利要求2。

（权利要求1、3维持原文，本次未作修改。）

【修改对照】

| 权项 | 修改前 | 修改后 |
|------|--------|--------|
| 2 | 采用简单线性迭代聚类算法 | 采用简单线性迭代聚类算法，且迭代次数为10至20次 |

【修改后权利要求（全文）】

{CLAIM_2_BAD}
"""

DRAFT_I2_FIXED = DRAFT_I2_BAD.replace(CLAIM_2_BAD, CLAIM_2_FIXED)

CONSISTENCY_AUDIT = {
    "verdict": "pass",
    "findings": [
        {
            "severity": "info",
            "message": "两条答复的事实认定一致：均以超像素邻接图为区别特征。",
            "issue_ids": ["I1", "I2"],
            "claim_no": None,
        }
    ],
    "notes": "未发现跨条冲突。",
}

CASE_NOTE_MD = """## 通知书要点
审查员依据专利法第22条第3款指出权利要求1相对于对比文件1不具备创造性，
认为区别特征属于本领域惯用技术手段；另依据专利法第26条第4款指出权利要求2
得不到说明书支持。

## 答复策略
第一条以意见陈述为主，围绕区别特征的技术效果论述创造性；第二条修改权利要求，
补入说明书已记载的迭代次数范围。

## 结果
本案答复草稿已生成，结论待审查员进一步通知。
"""

INGEST_DRAFT = {
    "frontmatter": {
        "case_id": "oa-archived-case",
        "title": "超声图像病灶分割案创造性与支持问题答复",
        "patent_type": "invention",
        "statutes": ["专利法第22条第3款", "专利法第26条第4款"],
        "defect_types": ["inventiveness", "support"],
        "domain": "医学影像",
        "notice_kind": "office_action",
        "outcome": "pending",
        "strategy": ["argue_only", "amend_claims"],
        "tags": ["超声", "图卷积"],
        "redacted": True,
    },
    "note_md": CASE_NOTE_MD,
    "redactions": [
        {"original_hint": "申请人名称", "replaced_with": "某医疗器械企业", "reason": "企业主体脱敏"},
        {"original_hint": "申请号", "replaced_with": "CN2024XXXXXXX", "reason": "案件识别信息脱敏"},
    ],
    "inferred_fields": ["domain"],
}

STRUCT_SCRIPT: dict[str, Any] = {
    "notice": NOTICE_BAD,
    "notice.repair1": NOTICE_OK,
    "strategy.I1": STRATEGY_I1,
    "strategy.I2": STRATEGY_I2_BAD,
    "strategy.I2.repair1": STRATEGY_I2_OK,
    "consistency": CONSISTENCY_AUDIT,
    "ingest": INGEST_DRAFT,
}

STREAM_SCRIPT: dict[str, str] = {
    "draft.I1": DRAFT_I1,
    "draft.I2": DRAFT_I2_BAD,
    "draft.I2.repair1": DRAFT_I2_FIXED,
}


class FakeLLM:
    """按 step_key 尾缀回放预置产物；未脚本化的调用直接抛错，便于暴露流程变化。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @staticmethod
    def _tag(kwargs: dict[str, Any]) -> str:
        """step_key 形如 'draft.draft.I2.repair1' → 去掉步骤名前缀作脚本键。"""
        step_key = str(kwargs.get("step_key") or "")
        return step_key.split(".", 1)[1] if "." in step_key else step_key

    async def chat(self, messages, **kwargs) -> str:  # pragma: no cover —— 本模块不该走非流式 GEN
        raise AssertionError(f"FakeLLM 未脚本化的 chat 调用：{self._tag(kwargs)}")

    async def chat_stream(self, messages, **kwargs):
        tag = self._tag(kwargs)
        self.calls.append(f"stream:{tag}")
        text = STREAM_SCRIPT.get(tag)
        if text is None:
            raise AssertionError(f"FakeLLM 未脚本化的流式调用：{tag}")
        for i in range(0, len(text), 64):        # 分片模拟逐 token 推送
            yield text[i : i + 64]

    async def structured(self, messages, model_cls, **kwargs):
        tag = self._tag(kwargs)
        self.calls.append(f"struct:{tag}")
        data = STRUCT_SCRIPT.get(tag)
        if data is None:
            raise AssertionError(f"FakeLLM 未脚本化的结构化调用：{tag}（模型 {model_cls.__name__}）")
        return model_cls.model_validate(data)


@pytest.fixture()
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """把 services/llm 的三个入口换成脚本化实现。"""
    from app.services import llm

    fake = FakeLLM()
    monkeypatch.setattr(llm, "chat", fake.chat)
    monkeypatch.setattr(llm, "chat_stream", fake.chat_stream)
    monkeypatch.setattr(llm, "structured", fake.structured)
    return fake


# ---------------------------------------------------------------------------
# 三、夹具与驱动工具
# ---------------------------------------------------------------------------



@pytest.fixture(scope="module", autouse=True)
def clean_library(client: TestClient):
    """本模块用例前后清空案例库，并保证 embedding 处于未配置态（检索走 keyword）。"""
    from app.db import database as db

    original = db.get_setting_json("embedding")
    _wipe_library()
    db.set_setting_json("embedding", {"enabled": False, "model": "", "dim": 1024})
    yield
    _wipe_library()
    if original is None:
        db.execute("DELETE FROM settings WHERE key='embedding'")
    else:
        db.set_setting_json("embedding", original)


def _wipe_library() -> None:
    from app.db import database as db
    from app.services import vector

    if vector.vec_available():
        db.execute(f"DROP TABLE IF EXISTS {vector.VEC_TABLE}")
    db.execute(f"DELETE FROM {vector.BLOB_TABLE}")
    db.execute("DELETE FROM oa_chunks")
    db.execute("DELETE FROM oa_library")
    db.execute("DELETE FROM oa_vec_meta")


def _new_case(client: TestClient, title: str) -> str:
    resp = client.post("/api/v1/cases", json={"module": "oa", "title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _upload(client: TestClient, case_id: str, name: str, payload: bytes, mime: str) -> dict[str, Any]:
    resp = client.post(f"/api/v1/cases/{case_id}/files", files={"files": (name, payload, mime)})
    assert resp.status_code == 201, resp.text
    return resp.json()[0]


def _steps(case_id: str):
    from app.db import database as db
    from app.pipelines import registry

    row = db.query_one("SELECT * FROM cases WHERE id=?", (case_id,))
    return registry.build_steps(dict(row))


def _case_row(case_id: str) -> dict[str, Any]:
    from app.db import database as db

    return dict(db.query_one("SELECT * FROM cases WHERE id=?", (case_id,)))


def _case_state(case_id: str) -> dict[str, Any]:
    return json.loads(_case_row(case_id)["state_json"] or "{}")


def _failures(case_id: str) -> str:
    from app.db import database as db

    rows = db.query_all(
        "SELECT step_key, error FROM pipeline_runs WHERE case_id=? AND status='failed'", (case_id,)
    )
    return "；".join(f"{r['step_key']}: {r['error']}" for r in rows) or "无失败步骤记录"


def _artifacts(case_id: str, kind: str) -> list[dict[str, Any]]:
    from app.db import database as db

    rows = db.query_all(
        "SELECT * FROM artifacts WHERE case_id=? AND kind=? ORDER BY version ASC", (case_id, kind)
    )
    return [dict(r) for r in rows]


async def _drive(
    case_id: str, task: asyncio.Task, answers: dict[str, Any], gates: dict[str, Any]
) -> list[str]:
    """轮询门控、记录 payload 并按 step_key 提交预置答复；返回门控出现顺序。"""
    from app.pipelines import engine

    order: list[str] = []
    while not task.done():
        pending = engine.get_pending(case_id)
        if pending is not None:
            step_key = pending["step_key"]
            if step_key not in order:
                order.append(step_key)
                gates[step_key] = pending
            try:
                engine.submit_input(case_id, step_key, answers.get(step_key, {}))
            except engine.PipelineConflictError:
                pass                          # 已提交、正在处理中
        await asyncio.sleep(0.05)
    await task
    return order


# ---------------------------------------------------------------------------
# 四、五步流水线端到端
# ---------------------------------------------------------------------------

_oa: dict[str, Any] = {}


async def test_oa_pipeline_runs_end_to_end(client: TestClient, fake_llm: FakeLLM) -> None:
    """notice_extract → retrieve → strategy → draft → review_deliver 全程跑通。"""
    from app.pipelines import engine
    from app.services import oa_library

    # 案例库里先放一条已确认的历史案例（供 retrieve 命中）
    seeded, _notes = await oa_library.create(
        {
            "case_id": "seed-inventiveness",
            "title": "创造性答复参考案例",
            "patent_type": "invention",
            "statutes": ["专利法第22条第3款"],
            "defect_types": ["inventiveness"],
            "domain": "医学影像",
            "notice_kind": "office_action",
            "outcome": "amended_then_granted",
            "strategy": ["argue_only"],
            "tags": ["超声"],
        },
        CASE_NOTE_MD,
        status="confirmed",
        user_id=client.user["id"],          # M8：案例库按属主隔离，种子案例归当前用户
    )
    _oa["seeded_id"] = seeded.id
    assert seeded.chunk_count >= 1, "已确认案例应完成切块（未配 embedding 时仍可关键词检索）"

    case_id = _new_case(client, "OA 答复端到端测试")
    notice = _upload(client, case_id, "审查意见通知书.pdf", build_notice_pdf(), "application/pdf")
    _upload(client, case_id, "权利要求书.md", CLAIMS_TEXT.encode("utf-8"), "text/markdown")

    # PDF→md 真跑：抽取文本须含条款号与对比文件
    assert notice["convert_error"] is None, notice
    notice_md = disk_path(notice["file"]["md_path"]).read_text(encoding="utf-8")
    assert "专利法第22条第3款" in notice_md
    assert "专利法第26条第4款" in notice_md
    assert "CN102222222A" in notice_md and "对比文件1" in notice_md

    answers = {
        "notice_extract": {
            "issues": [
                {"id": "I1", "note": "人工核对：法条与通知书原文逐字一致"},
                {"id": "I2"},
            ]
        },
        "retrieve": {
            "selections": [
                {"issue_id": "I1", "library_ids": [seeded.id]},
                {"issue_id": "I2", "library_ids": []},
            ]
        },
        "strategy": {
            "choices": [
                {"issue_id": "I1", "strategy": "argue_only", "note": "维持推荐策略"},
                {"issue_id": "I2", "strategy": "amend_claims", "note": "补入迭代次数10至20次"},
            ]
        },
        "review_deliver": {"action": "confirm", "archive": True, "note": "代理人已复核，仅作内部草稿"},
    }
    gates: dict[str, Any] = {}

    task = engine.start(
        case_id, _steps(case_id), run_group="initial",
        start_payload={"file_id": notice["file"]["id"]},
    )
    order = await asyncio.wait_for(_drive(case_id, task, answers, gates), timeout=900)

    assert _case_row(case_id)["status"] == "completed", _failures(case_id)
    _oa.update(
        {
            "case_id": case_id, "state": _case_state(case_id),
            "gates": gates, "order": order, "calls": list(fake_llm.calls),
        }
    )

    # 三个门控依次挂起（+ 终稿 confirm）
    assert order == ["notice_extract", "retrieve", "strategy", "review_deliver"]
    assert [gates[k]["kind"] for k in order] == ["oa_issues", "oa_retrieve", "oa_strategy", "confirm"]

    # 每步都只跑了一次（REPAIR 在步骤内部完成，不占 attempt）
    from app.db import database as db

    runs = db.query_all(
        "SELECT step_key, status, attempt FROM pipeline_runs WHERE case_id=?", (case_id,)
    )
    assert sorted(r["step_key"] for r in runs) == sorted(
        ["notice_extract", "retrieve", "strategy", "draft", "review_deliver"]
    )
    assert all(r["status"] == "done" and int(r["attempt"]) == 1 for r in runs)


def test_notice_enum_gate_triggers_repair() -> None:
    """非法 statute / defect_type 被服务端枚举校验捕获 → REPAIR 一次后通过。"""
    state = _oa["state"]

    # 首版 STRUCT → 服务端枚举校验不过 → 带错误清单 REPAIR 一次
    assert _oa["calls"][:2] == ["struct:notice", "struct:notice.repair1"]
    assert "struct:notice.repair2" not in _oa["calls"]                 # REPAIR ≤2 且一次即修好
    assert state["notice_lint"]["repairs"] == 1
    assert state["notice_lint"]["unresolved"] == []
    assert state["notice_lint"]["after_review"] == []

    notice = state["notice"]
    assert [i["statute"] for i in notice["issues"]] == ["专利法第22条第3款", "专利法第26条第4款"]
    assert [i["defect_type"] for i in notice["issues"]] == ["inventiveness", "support"]
    assert [i["id"] for i in notice["issues"]] == ["I1", "I2"]        # issue-1 → I1 归一化
    assert notice["issues"][0]["cited_documents"][0]["code"] == "D1"
    assert notice["issues"][0]["cited_documents"][0]["identifier"] == "CN102222222A"
    # P1 的人工备注已合并回结构化结果
    assert "逐字一致" in notice["issues"][0]["note"]


def test_notice_gate_payload_and_case_title() -> None:
    """P1 门控 payload 含枚举选项、校验报告与自动识别的权利要求书原文；案件标题已回填。"""
    gate = _oa["gates"]["notice_extract"]
    default = gate["default"]

    assert gate["kind"] == "oa_issues"
    assert "防条款幻觉" in gate["prompt"]
    assert default["repairs"] == 1 and default["lint_problems"] == []
    assert {o["value"] for o in default["defect_type_options"]} >= {"inventiveness", "support"}
    # 未上传的权利要求书由 guess_claims_text 从申请文件里挑出
    assert "超像素划分" in default["claims_text"]
    assert set(_oa["state"]["base_claims"]) == {"1", "2", "3"}

    assert _case_row(_oa["case_id"])["title"] == f"第1次审查意见通知书 答复（{APPLICATION_NO}）"


def test_retrieve_exposes_retrieval_mode() -> None:
    """检索方式必须透出到门控 payload 与工作内存（本用例未配 embedding → keyword）。"""
    default = _oa["gates"]["retrieve"]["default"]
    retrieval = _oa["state"]["retrieval"]

    assert default["retrieval_mode"] == "keyword"
    assert "关键词匹配" in _oa["gates"]["retrieve"]["prompt"]
    assert retrieval["retrieval_mode"] == "keyword"
    assert retrieval["modes"] == ["keyword", "keyword"]

    by_issue = {x["issue_id"]: x for x in retrieval["issues"]}
    assert set(by_issue) == {"I1", "I2"}
    assert all(x["retrieval_mode"] == "keyword" for x in by_issue.values())
    # I1 命中并选用了人工勾选的参考案例
    assert _oa["seeded_id"] in {h["library_id"] for h in by_issue["I1"]["hits"]}
    assert by_issue["I1"]["selected"] == [_oa["seeded_id"]]
    # I2 的严格过滤无命中 → 逐级放宽并如实记录
    assert by_issue["I2"]["relaxed"], by_issue["I2"]
    assert by_issue["I2"]["selected"] == []


def test_strategy_lint_triggers_repair_and_choices_recorded() -> None:
    """修改类策略缺 risk 被捕获 → REPAIR；用户逐条勾选结果进工作内存。"""
    state = _oa["state"]
    reports = {r["issue_id"]: r for r in state["strategy_reports"]}

    assert reports["I1"]["repairs"] == 0
    assert reports["I2"]["repairs"] == 1 and reports["I2"]["unresolved"] == []
    assert "struct:strategy.I2.repair1" in _oa["calls"]
    assert "struct:strategy.I1.repair1" not in _oa["calls"]

    choices = {c["issue_id"]: c for c in state["strategy_choices"]}
    assert choices["I1"]["strategy"] == "argue_only" and choices["I1"]["from_recommended"] is True
    assert choices["I2"]["strategy"] == "amend_claims"
    assert "专利法第33条" in choices["I2"]["risk"]           # 修改类策略必须带超范围风险评估
    assert choices["I2"]["note"] == "补入迭代次数10至20次"

    gate = _oa["gates"]["strategy"]
    assert gate["kind"] == "oa_strategy"
    assert {s["issue_id"] for s in gate["default"]["strategies"]} == {"I1", "I2"}


def test_amended_claims_pass_claims_lint_after_repair() -> None:
    """amend_claims 修改稿的 error 级违规触发一次 REPAIR，修复后 lint 清零。"""
    drafts = {d["issue_id"]: d for d in _oa["state"]["drafts"]}

    assert "stream:draft.I2" in _oa["calls"] and "stream:draft.I2.repair1" in _oa["calls"]
    assert "stream:draft.I1.repair1" not in _oa["calls"]      # 仅意见陈述无修改稿，不触发 lint

    assert drafts["I1"]["strategy"] == "argue_only"
    assert drafts["I1"]["amended_claims"] == {}
    assert drafts["I1"]["cases_used"] == ["seed-inventiveness"]   # 引用了勾选的历史案例

    lint = drafts["I2"]["claims_lint"]
    assert lint["repairs"] == 1
    assert lint["errors"] == []
    assert lint["claims"] == [2]
    assert drafts["I2"]["amended_claims"] == {"2": CLAIM_2_FIXED}
    assert "优选" not in drafts["I2"]["markdown"]


def test_cross_issue_consistency_and_deliverables() -> None:
    """跨条一致性（确定性 + AUDIT）通过；md/docx 双交付物真实产出。"""
    from docx import Document

    state = _oa["state"]
    consistency = state["consistency"]

    assert consistency["code_findings"] == []
    assert consistency["audit"]["verdict"] == "pass"
    assert consistency["unresolved"] == []
    assert state["merged_claims"] == {"2": CLAIM_2_FIXED}
    assert state["review"] == {"action": "confirm", "confirmed": True, "note": "代理人已复核，仅作内部草稿"}

    document = state["response_markdown"]
    assert document.startswith("# 意见陈述书（草稿·须人工复核）")
    assert f"**申请号**：{APPLICATION_NO}" in document
    assert "第1次审查意见通知书" in document
    assert "| 第1条（I1） | 专利法第22条第3款 | 创造性 | 1 | 仅意见陈述 |" in document
    assert "| 第2条（I2） | 专利法第26条第4款 | 得不到说明书支持 | 2 | 修改权利要求 |" in document
    assert "## 三、权利要求书修改替换页" in document
    assert CLAIM_2_FIXED in document and CLAIM_1 in document and CLAIM_3 in document
    assert "待人工确认事项" not in document                     # 本案无遗留问题

    md_items = _artifacts(_oa["case_id"], "oa_response_md")
    docx_items = _artifacts(_oa["case_id"], "oa_response_docx")
    assert len(md_items) == 1 and len(docx_items) == 1, state["deliver"]["files"]
    assert disk_path(md_items[0]["stored_path"]).read_text(encoding="utf-8") == document
    assert docx_items[0]["source_artifact_id"] == md_items[0]["id"]

    doc = Document(disk_path(docx_items[0]["stored_path"]))
    texts = [p.text for p in doc.paragraphs]
    assert any("意见陈述书" in t for t in texts)
    # 「2. …」在 Word 里被有序列表的自动编号接管，段落文本只剩权项正文
    assert any(CLAIM_2_FIXED.split(". ", 1)[1] in t for t in texts)
    assert any("D1" in t or "对比文件" in t for t in texts)


def test_archive_back_to_library_creates_draft_note(client: TestClient) -> None:
    """「归档回案例库」产 draft 状态案例笔记：不切块、不进检索库，等待人审。"""
    archive = _oa["state"]["deliver"]["archive"]

    assert archive and archive.get("library_id"), archive
    assert archive["status"] == "draft"
    assert archive["case_id"] == "oa-archived-case"
    assert len(archive["redactions"]) == 2

    detail = client.get(f"/api/v1/oa/library/{archive['library_id']}").json()
    assert detail["status"] == "draft"
    assert detail["embedded"] is False
    assert detail["chunks"] == []                       # 人审前不进检索库
    assert detail["defect_types"] == ["inventiveness", "support"]
    assert [r["original_hint"] for r in detail["redactions"]] == ["申请人名称", "申请号"]
    assert detail["note_full_md"].startswith("---")     # frontmatter + 正文
    _oa["archived_id"] = archive["library_id"]


# ---------------------------------------------------------------------------
# 五、确定性部件的专项用例
# ---------------------------------------------------------------------------


def test_lint_amended_claims_error_vs_warning() -> None:
    """禁用词是 warning（只标记不阻断），句号越界是 error（触发 REPAIR）。"""
    from app.pipelines import oa as oa_pipeline

    base = oa_pipeline.parse_claims(CLAIMS_TEXT)
    errors, findings = oa_pipeline.lint_amended_claims(base, {2: CLAIM_2_BAD})

    rules = {(f["claim_no"], f["rule"], f["severity"]) for f in findings}
    assert (2, "forbidden_word", "warning") in rules      # 「优选」只标记
    assert (2, "period", "error") in rules                # 一项权利要求两个句号
    assert [f["rule"] for f in errors] == ["period"]      # 只有 error 级进 REPAIR 清单

    ok_errors, _ = oa_pipeline.lint_amended_claims(base, {2: CLAIM_2_FIXED})
    assert ok_errors == []


def test_parse_and_extract_amended_claims() -> None:
    """权项解析与「修改后权利要求（全文）」块抽取。"""
    from app.pipelines import oa as oa_pipeline

    base = oa_pipeline.parse_claims(CLAIMS_TEXT)
    assert set(base) == {1, 2, 3}
    assert base[1] == CLAIM_1 and base[3] == CLAIM_3

    assert oa_pipeline.extract_amended_claims(DRAFT_I2_FIXED) == {2: CLAIM_2_FIXED}
    assert oa_pipeline.extract_amended_claims(DRAFT_I1) == {}      # 仅意见陈述无修改块
    assert oa_pipeline.merged_claim_list(base, {2: CLAIM_2_FIXED}) == [
        CLAIM_1, CLAIM_2_FIXED, CLAIM_3
    ]


def test_check_cross_issue_detects_conflicts() -> None:
    """跨条一致性：同一权项多条修改冲突 + 合并后编号不连续 + 合并后权项 lint。"""
    from app.pipelines import oa as oa_pipeline

    base = oa_pipeline.parse_claims(CLAIMS_TEXT)
    drafts = [
        {"issue_id": "I1", "amended_claims": {"2": CLAIM_2_FIXED}},
        {"issue_id": "I2", "amended_claims": {"2": CLAIM_2_BAD}},
    ]
    findings, merged = oa_pipeline.check_cross_issue(drafts, base)

    messages = "\n".join(f["message"] for f in findings)
    assert "互不相同的修改文本" in messages
    assert any(f["issue_ids"] == ["I1", "I2"] for f in findings)
    assert "合并后权项校验" in messages                     # 冲突稿的 period 错误一并报出
    assert merged == {2: CLAIM_2_BAD}                      # 以最后一条为准，交人工合并

    # 删项导致编号不连续
    gap_findings, _ = oa_pipeline.check_cross_issue(
        [{"issue_id": "I1", "amended_claims": {"5": "5. 根据权利要求1所述的方法，其特征在于，A。"}}],
        base,
    )
    assert any("编号不连续" in f["message"] for f in gap_findings)


def test_human_gate_prompt_is_verbatim_from_guardrails() -> None:
    """终稿人审话术逐字取自 guardrails.md 的人审闸门代码块。"""
    from app.pipelines import oa as oa_pipeline

    prompt = oa_pipeline.human_gate_prompt()
    assert "以下为审查答复【草稿】" in prompt
    assert "请回复：确认仅作内部草稿 / 需要修改策略 / 暂停" in prompt
    assert prompt in oa_pipeline.assets_loader.get_text("prompts/oa/guardrails")


def test_issue_filters_and_query_build() -> None:
    """逐 issue 的检索式与元数据预过滤条件构造（扩充缺陷类型带上归并后的基准值）。"""
    from app.pipelines import oa as oa_pipeline

    issue = {
        "defect_type": "added_matter", "statute": "专利法第33条",
        "examiner_argument_digest": "修改超出原说明书记载范围",
    }
    assert oa_pipeline._issue_query(issue).startswith("修改超范围 专利法第33条")
    assert oa_pipeline._issue_filters(issue, "invention") == {
        "defect_types": ["added_matter", "other"],
        "statutes": ["专利法第33条"],
        "patent_type": "invention",
    }
    # 通知书未写明法条时不拿「未明示」当过滤条件
    assert "statutes" not in oa_pipeline._issue_filters(
        {"defect_type": "clarity", "statute": "未明示"}, None
    )


# ---------------------------------------------------------------------------
# 六、案例库 API
# ---------------------------------------------------------------------------


def test_oa_module_registered_in_pipeline_api(client: TestClient) -> None:
    """注册表已挂 'oa'：新建 oa 案件即可经通用流水线 API 看到五步（无需先 start）。"""
    case_id = _new_case(client, "OA 注册检查")
    state = client.get(f"/api/v1/cases/{case_id}/pipeline/state")

    assert state.status_code == 200, state.text
    body = state.json()
    assert [s["key"] for s in body["steps"]] == [
        "notice_extract", "retrieve", "strategy", "draft", "review_deliver"
    ]
    assert body["steps"][0]["name_zh"] == "通知书结构化"
    assert all(s["status"] == "pending" for s in body["steps"])
    assert body["pending_interaction"] is None


def test_library_list_filters(client: TestClient) -> None:
    """列表：status / defect_type / tag / q 过滤 + 分页；非法 status → 422。"""
    all_items = client.get("/api/v1/oa/library").json()
    assert all_items["total"] >= 2                    # 种子案例 + 归档草稿
    assert {i["id"] for i in all_items["items"]} >= {_oa["seeded_id"], _oa["archived_id"]}

    confirmed = client.get("/api/v1/oa/library", params={"status": "confirmed"}).json()
    assert [i["id"] for i in confirmed["items"]] == [_oa["seeded_id"]]

    drafts = client.get("/api/v1/oa/library", params={"status": "draft"}).json()
    assert _oa["archived_id"] in {i["id"] for i in drafts["items"]}

    by_defect = client.get("/api/v1/oa/library", params={"defect_type": "support"}).json()
    assert [i["id"] for i in by_defect["items"]] == [_oa["archived_id"]]

    by_tag = client.get("/api/v1/oa/library", params={"tag": "图卷积"}).json()
    assert [i["id"] for i in by_tag["items"]] == [_oa["archived_id"]]

    by_q = client.get("/api/v1/oa/library", params={"q": "创造性答复参考"}).json()
    assert [i["id"] for i in by_q["items"]] == [_oa["seeded_id"]]

    paged = client.get("/api/v1/oa/library", params={"limit": 1, "offset": 0}).json()
    assert len(paged["items"]) == 1 and paged["total"] == all_items["total"]

    assert client.get("/api/v1/oa/library", params={"status": "bogus"}).status_code == 422
    assert client.get(f"/api/v1/oa/library/{'X' * 26}").status_code == 404


def test_library_search_endpoint_exposes_retrieval_mode(client: TestClient) -> None:
    """GET /oa/library/search 响应必带 retrieval_mode，并支持元数据预过滤。"""
    resp = client.get(
        "/api/v1/oa/library/search",
        params={"q": "创造性 专利法第22条第3款", "k": 3, "defect_type": "inventiveness"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retrieval_mode"] == "keyword"          # 未配 embedding → 明示降级
    assert "Embedding" in body["message"]
    assert [h["library_id"] for h in body["hits"]] == [_oa["seeded_id"]]
    assert body["filters"] == {"defect_types": ["inventiveness"]}
    assert body["hits"][0]["score"] > 0

    # 过滤到空集不报错，仍明示检索方式
    empty = client.get(
        "/api/v1/oa/library/search", params={"q": "创造性", "patent_type": "design"}
    ).json()
    assert empty["retrieval_mode"] in ("vector", "brute", "keyword")
    assert empty["hits"] == []

    assert client.get("/api/v1/oa/library/search", params={"q": "x", "k": 0}).status_code == 422
    assert client.get("/api/v1/oa/library/search", params={"q": "x", "k": 999}).status_code == 422


def test_library_ingest_multipart_and_confirm(client: TestClient, fake_llm: FakeLLM) -> None:
    """multipart 导入 → draft；PUT status=confirmed 触发切块 + 入检索库；DELETE 清理。"""
    resp = client.post(
        "/api/v1/oa/library/ingest",
        files={"files": ("历史案例.md", CASE_NOTE_MD.encode("utf-8"), "text/markdown")},
        data={"notes": "手工导入的历史案例", "outcome": "granted", "case_id": "api-ingest-case"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    library_id = body["item"]["id"]

    assert body["item"]["status"] == "draft"            # 默认落草稿，等待人审
    assert body["item"]["case_id"] == "api-ingest-case"
    assert body["item"]["outcome"] == "granted"         # 请求体的结案结果覆盖模型推断
    assert body["item"]["chunks"] == [] and body["item"]["embedded"] is False
    assert len(body["redactions"]) == 2
    assert body["materials"] == ["历史案例.md"]
    assert body["item"]["frontmatter"].get("_redactions") is None   # 平台元数据不外泄

    detail = client.get(f"/api/v1/oa/library/{library_id}")
    assert detail.status_code == 200 and detail.json()["note_md"] == CASE_NOTE_MD

    confirmed = client.put(f"/api/v1/oa/library/{library_id}", json={"status": "confirmed"})
    assert confirmed.status_code == 200, confirmed.text
    payload = confirmed.json()
    assert payload["reindexed"] is True
    assert payload["item"]["status"] == "confirmed"
    assert payload["item"]["chunk_count"] >= 1 and payload["item"]["chunks"]
    assert payload["item"]["embedded"] is False        # 未配 embedding → 只切块，关键词可检索

    hit = client.get(
        "/api/v1/oa/library/search", params={"q": "迭代次数 说明书支持", "k": 5}
    ).json()
    assert library_id in {h["library_id"] for h in hit["hits"]}

    # 人审改回 draft → 退出检索库
    back = client.put(f"/api/v1/oa/library/{library_id}", json={"status": "draft"}).json()
    assert back["item"]["chunk_count"] == 0
    assert library_id not in {
        h["library_id"]
        for h in client.get("/api/v1/oa/library/search", params={"q": "迭代次数"}).json()["hits"]
    }

    assert client.put(f"/api/v1/oa/library/{library_id}", json={}).status_code == 422
    assert client.delete(f"/api/v1/oa/library/{library_id}").json() == {"ok": True}
    assert client.get(f"/api/v1/oa/library/{library_id}").status_code == 404
    assert client.delete(f"/api/v1/oa/library/{library_id}").status_code == 404


def test_library_ingest_by_file_id(client: TestClient, fake_llm: FakeLLM) -> None:
    """JSON {file_id} 复用 files 表里已上传并转换过的文件。"""
    case_id = _new_case(client, "案例库导入测试")
    uploaded = _upload(client, case_id, "历史通知书.pdf", build_notice_pdf(), "application/pdf")

    resp = client.post(
        "/api/v1/oa/library/ingest",
        json={"file_id": uploaded["file"]["id"], "notes": "由已上传件导入", "status": "draft"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["item"]["status"] == "draft"
    assert body["materials"] == ["历史通知书.pdf"]
    assert body["inferred_fields"] == ["domain"]
    client.delete(f"/api/v1/oa/library/{body['item']['id']}")

    assert client.post("/api/v1/oa/library/ingest", json={"file_id": "nope"}).status_code == 404
    assert client.post("/api/v1/oa/library/ingest", json={}).status_code == 422


def test_library_stats_and_rebuild(client: TestClient, admin_client: TestClient) -> None:
    """索引状态 + 全量重建（同步 wait=true 与后台任务 + 轮询两种形态）。"""
    stats = client.get("/api/v1/oa/library/stats").json()
    assert stats["embedding_configured"] is False
    assert stats["retrieval_mode"] == "keyword"
    assert stats["confirmed"] >= 1 and stats["chunks"] >= 1

    synced = admin_client.post("/api/v1/oa/library/rebuild", params={"wait": True})
    assert synced.status_code == 202, synced.text
    report = synced.json()
    assert report["ok"] is True
    assert report["mode"] == "keyword"                 # 未配 embedding → 只重新切块
    assert report["queued"] >= 1
    assert "关键词检索" in report["message"]

    queued = admin_client.post("/api/v1/oa/library/rebuild")
    assert queued.status_code == 202
    assert "后台" in queued.json()["message"]

    for _ in range(100):                                # 轮询后台任务直至结束
        status = admin_client.get("/api/v1/oa/library/rebuild/status").json()
        if not status["running"]:
            break
        time.sleep(0.05)
    assert status["running"] is False, status
    assert status["error"] == ""
    assert status["result"] and status["result"]["ok"] is True
    assert status["finished_at"]
