"""模块 B（论文转专利）端到端测试。

不依赖真实 LLM：把 `app.services.llm` 的 chat / chat_stream / structured 换成脚本化
FakeLLM（按 step_key 特征返回一篇小型「测试论文」对应的预置产物），其余环节**全部真跑**：
- 权项 lint 与 REPAIR：脚本首版权利要求故意含禁用词「优选」，走一次 REPAIR 修正；
- 忠实性审计：脚本给出一条 unsourced 断言，direct 模式转【待补充：…】并入 gaps；
- 质检补丁：脚本给出一条锚点补丁，服务端确定性应用（删掉评价性语言）；
- 附图：真跑 tools/generate_patent_drawings.py（零依赖脚本）产 SVG/PNG；
- DOCX：真跑 tools/generate_patent_docx.py，用 python-docx 打开校验五节标题与 word/media；
- PDF：真跑 Word COM / soffice / Pillow 兜底三级链，全失败则明确 skip 并记录原因。
"""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# 测试论文与脚本化产物
# ---------------------------------------------------------------------------

PAPER_MD = """# Graph-Guided Superpixel Network for Ultrasound Lesion Segmentation

## Abstract
We present a graph-guided framework for ultrasound lesion segmentation. The image is first
partitioned into superpixels, an adjacency graph is then built on the superpixel nodes, and a
graph convolutional network aggregates neighbourhood features to predict node-level lesion
probabilities, which are merged into the final lesion mask.

## 1. Introduction
Pixel-wise segmentation networks process every pixel independently and ignore the contextual
relation between neighbouring tissue regions, which makes speckle noise a dominant source of
false positives in ultrasound images.

## 2. Method
The pipeline contains four stages. First, the ultrasound image is partitioned into superpixels
with simple linear iterative clustering. Second, an adjacency graph is constructed by linking
spatially adjacent superpixels, using grey level and texture descriptors as node attributes.
Third, a graph convolutional network aggregates neighbourhood features layer by layer and
outputs node-level lesion probabilities. Finally, the node probabilities are merged into a
lesion segmentation mask.

## 3. Experiments
On an in-house ultrasound dataset the proposed method keeps the segmentation accuracy of the
pixel-wise baseline while reducing the inference latency.
"""

INVENTION_NAME = "一种基于图结构的超声图像病灶分割方法"

CLAIM_1 = (
    "1.一种基于图结构的超声图像病灶分割方法，其特征在于，包括："
    "对超声图像进行超像素划分，得到多个超像素节点；"
    "基于所述超像素节点的灰度特征与纹理特征构建邻接图；"
    "将所述邻接图输入图卷积网络，逐层聚合邻域特征，得到节点级病灶概率；"
    "根据所述节点级病灶概率归并所述超像素节点，生成病灶分割掩膜。"
)
CLAIM_2_BAD = "2.根据权利要求1所述的方法，其特征在于，所述超像素划分优选采用简单线性迭代聚类算法。"
CLAIM_2_FIXED = "2.根据权利要求1所述的方法，其特征在于，所述超像素划分采用简单线性迭代聚类算法。"
CLAIM_3 = (
    "3.根据权利要求1所述的方法，其特征在于，"
    "所述构建邻接图包括在空间相邻的两个超像素节点之间建立边，并将灰度特征与纹理特征作为节点属性。"
)

CLAIMS_MD = f"""## 权利要求书

{CLAIM_1}

{CLAIM_2_BAD}

{CLAIM_3}

```json
{{
  "invention_name": "{INVENTION_NAME}",
  "claims": [
    "{CLAIM_1}",
    "{CLAIM_2_BAD}",
    "{CLAIM_3}"
  ]
}}
```
"""

ABSTRACT_TEXT = (
    "本发明公开一种基于图结构的超声图像病灶分割方法，属于医学图像处理技术领域。"
    "该方法对超声图像进行超像素划分并构建超像素邻接图，"
    "通过图卷积网络逐层聚合邻域特征得到节点级病灶概率，再归并生成病灶分割掩膜。"
    "该方法在保持分割精度的同时降低了推理时延。"
    "关键词：超声图像；超像素；图卷积网络；病灶分割。"
)

DESCRIPTION_PART1 = """技术领域
本发明属于医学图像处理技术领域，具体涉及一种基于图结构的超声图像病灶分割方法。

背景技术
超声图像分割是指从超声图像中划分出病灶区域的图像处理过程。现有主流做法是采用逐像素分割网络，
对每个像素独立预测其类别。该做法存在的问题是忽略相邻组织区域之间的上下文关系；
问题成因在于逐像素预测缺乏区域级的关系建模手段；由此导致的后果是斑点噪声容易被误判为病灶，
分割结果的假阳性偏高，且逐像素计算带来冗余开销。

发明内容
本发明提供一种基于图结构的超声图像病灶分割方法，通过超像素划分、邻接图构建、
图卷积特征聚合与掩膜生成四个环节完成病灶分割。技术方案的细化在于：
以超像素节点替代像素作为处理单元，以空间相邻关系构建邻接图并以灰度特征与纹理特征作为节点属性，
由图卷积网络逐层聚合邻域特征输出节点级病灶概率，最终归并为病灶分割掩膜。
技术效果在于显式表达区域间上下文关系，降低斑点噪声导致的误判，并减少待处理单元数量。
"""

DESCRIPTION_PART2 = """附图说明
图1为本发明实施例提供的一种基于图结构的超声图像病灶分割方法的流程图。
图2为本发明实施例提供的一种超声图像病灶分割系统的结构示意图。

具体实施方式
下面结合附图对本发明实施例的技术方案进行清楚、完整的描述。

S101，对超声图像进行超像素划分。该步骤按照像素的灰度相似性与空间邻近性将超声图像划分为多个超像素节点，
解决了逐像素处理导致计算冗余的问题，通过简单线性迭代聚类算法完成划分，
达到了在保留病灶边界的前提下降低待处理单元数量的效果。

S102，构建超像素邻接图。该步骤以超像素节点为顶点、以空间相邻关系为边构建邻接图，
并将灰度特征与纹理特征作为节点属性，解决了现有方法忽略区域间上下文关系的问题，
达到了显式表达病灶区域与周边组织关系的效果。

S103，通过图卷积网络逐层聚合邻域特征。该步骤将邻接图输入图卷积网络，逐层聚合邻域节点特征并输出节点级病灶概率，
解决了斑点噪声超像素被误判为病灶的问题，达到了提升节点分类稳定性的效果。

S104，生成病灶分割掩膜。该步骤按照节点级病灶概率对超像素节点进行归并并输出病灶分割掩膜，
解决了节点级结果无法直接用于临床读片的问题，达到了提升分割效率的效果。本发明具有显著优点。

在一个具体实施例中，本实施例的方案在嵌入式超声设备上完成部署，实时输出分割结果。
"""

DRAWING_SPECS = {
    "drawings": [
        (
            "图1：一种基于图结构的超声图像病灶分割方法流程图，"
            "包含步骤S101，对超声图像进行超像素划分；S102，构建超像素邻接图；"
            "S103，图卷积网络逐层聚合邻域特征；S104，生成病灶分割掩膜。"
        ),
        (
            "图2：一种超声图像病灶分割系统结构示意图，"
            "包含超像素划分模块、邻接关系构建模块、特征聚合分类模块、掩膜生成模块。"
        ),
    ],
    "source_figures": [
        {
            "figure_no": 1,
            "source_label": "Figure 1",
            "caption": "Overview of the proposed pipeline",
            "page": 2,
            "notes": "论文原图给出四阶段处理流程与节点间连接关系",
        }
    ],
    "abstract_drawing": "（注：建议选取说明书附图中的图1作为摘要附图）",
}

# 忠实性审计：论文未记载「嵌入式超声设备部署」，落不了源
UNSOURCED_ASSERTION = "在嵌入式超声设备上完成部署"

FAITHFULNESS_AUDIT = {
    "verdict": "fail",
    "mappings": [
        {
            "location": "权利要求1",
            "assertion": "对超声图像进行超像素划分并构建邻接图",
            "source_quote": "the ultrasound image is partitioned into superpixels with simple linear iterative clustering",
            "status": "sourced",
        }
    ],
    "unsourced": [
        {
            "location": "具体实施方式步骤S104之后",
            "assertion": UNSOURCED_ASSERTION,
            "reason": "no_support",
            "suggested_action": "mark_placeholder",
            "suggested_text": "",
        }
    ],
}

# 质检：删掉评价性语言（锚点 + 逐字片段，服务端确定性应用）
QUALITY_AUDIT = {
    "verdict": "fail",
    "patches": [
        {
            "checklist_item": "Claims contain no uncertain words / no self-evaluation language.",
            "section": "description",
            "anchor_before": "达到了提升分割效率的效果。",
            "original_excerpt": "本发明具有显著优点。",
            "replacement": "",
            "reason": "说明书不得出现评价性语言。",
        }
    ],
    "unresolved": [],
}

INPUT_ASSESSMENT = {
    "completeness": {
        "title": True,
        "abstract": True,
        "method": True,
        "effects": True,
        "figures": False,
        "overall": "partial",
        "note": "方法与效果清楚，未提供可直接复用的原图材料。",
    },
    "patentability_clues": [
        "超像素划分 + 邻接图构建 + 图卷积聚合的处理流程（见 Method 第二节）",
    ],
    "fidelity_risks": ["论文未记载部署硬件环境，撰写时容易补齐"],
    "drawing_readiness": "partial",
    "gaps": [
        {
            "item": "论文未给出超像素数量的取值依据",
            "severity": "minor",
            "affects": "description",
            "suggested_question": "请补充超像素数量的取值范围与确定依据",
        }
    ],
}

EXTRACTION = {
    "contributions": [
        {"id": "C1", "title": "超像素图结构建模", "detail": "以超像素节点替代像素作为处理单元并构建邻接图"}
    ],
    "method_steps": [
        {"id": "S1", "label": "超像素划分", "detail": "将超声图像划分为多个超像素节点",
         "inputs": ["超声图像"], "outputs": ["超像素节点"]},
        {"id": "S2", "label": "邻接图构建", "detail": "以空间相邻关系构建邻接图",
         "inputs": ["超像素节点"], "outputs": ["邻接图"]},
        {"id": "S3", "label": "特征聚合", "detail": "图卷积网络逐层聚合邻域特征",
         "inputs": ["邻接图"], "outputs": ["节点级病灶概率"]},
        {"id": "S4", "label": "掩膜生成", "detail": "归并节点级概率生成病灶分割掩膜",
         "inputs": ["节点级病灶概率"], "outputs": ["病灶分割掩膜"]},
    ],
    "system_components": [
        {"id": "M1", "name": "超像素划分模块", "function": "划分超像素节点", "relations": ["向 M2 输出超像素节点"]},
        {"id": "M2", "name": "邻接关系构建模块", "function": "构建邻接图", "relations": ["向 M3 输出邻接图"]},
        {"id": "M3", "name": "特征聚合分类模块", "function": "聚合邻域特征并分类", "relations": ["向 M4 输出概率"]},
        {"id": "M4", "name": "掩膜生成模块", "function": "生成病灶分割掩膜", "relations": []},
    ],
    "distinctions": ["以超像素邻接图替代逐像素处理，显式建模区域间上下文关系"],
    "tech_problem": "逐像素分割网络忽略相邻组织区域的上下文关系，斑点噪声易被误判为病灶且计算冗余。",
    "tech_effects": ["降低斑点噪声导致的误判", "减少待处理单元数量"],
    "source_map": [
        {
            "claim_like": "以超像素邻接图替代逐像素处理",
            "source_quote": "an adjacency graph is then built on the superpixel nodes",
        }
    ],
}

FLASH_TEXT = """一、说明书摘要
本发明公开一种基于图结构的超声图像病灶分割方法，属于医学图像处理技术领域。

二、摘要附图
（注：建议选取说明书附图中的图1作为摘要附图）

三、权利要求书
1.一种基于图结构的超声图像病灶分割方法，其特征在于，包括：对超声图像进行超像素划分。

四、说明书
技术领域
本发明属于医学图像处理技术领域。

五、说明书附图
图1为一种基于图结构的超声图像病灶分割方法流程图。
"""


# ---------------------------------------------------------------------------
# 脚本化 FakeLLM
# ---------------------------------------------------------------------------

STREAM_SCRIPT: dict[str, str] = {
    "claims": CLAIMS_MD,
    "abstract": ABSTRACT_TEXT,
    "description.part1": DESCRIPTION_PART1,
    "description.part2": DESCRIPTION_PART2,
    "flash": FLASH_TEXT,
}

STRUCT_SCRIPT: dict[str, Any] = {
    "assess": INPUT_ASSESSMENT,
    "extract": EXTRACTION,
    "drawings": DRAWING_SPECS,
    "faithfulness": FAITHFULNESS_AUDIT,
    "quality": QUALITY_AUDIT,
    # 权项 lint 违规后的 REPAIR：去掉「优选」
    "claims.repair1": {
        "invention_name": INVENTION_NAME,
        "claims": [CLAIM_1, CLAIM_2_FIXED, CLAIM_3],
    },
    "claims.json": {
        "invention_name": INVENTION_NAME,
        "claims": [CLAIM_1, CLAIM_2_BAD, CLAIM_3],
    },
}


class FakeLLM:
    """按 step_key 特征回放预置产物；未脚本化的调用直接抛错，便于暴露流程变化。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @staticmethod
    def _tag(kwargs: dict[str, Any]) -> str:
        """step_key 形如 'draft.description.part1' → 取步骤后缀作脚本键。"""
        step_key = str(kwargs.get("step_key") or "")
        return step_key.split(".", 1)[1] if "." in step_key else step_key

    async def chat(self, messages, **kwargs) -> str:
        tag = self._tag(kwargs)
        self.calls.append(f"chat:{tag}")
        if tag in STREAM_SCRIPT:
            return STREAM_SCRIPT[tag]
        raise AssertionError(f"FakeLLM 未脚本化的 chat 调用：{tag}")

    async def chat_stream(self, messages, **kwargs):
        tag = self._tag(kwargs)
        self.calls.append(f"stream:{tag}")
        text = STREAM_SCRIPT.get(tag)
        if text is None:
            raise AssertionError(f"FakeLLM 未脚本化的流式调用：{tag}")
        for i in range(0, len(text), 48):   # 分片模拟逐 token 推送
            yield text[i : i + 48]

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
# 夹具与驱动工具
# ---------------------------------------------------------------------------



def _new_case(client: TestClient, title: str) -> str:
    resp = client.post("/api/v1/cases", json={"module": "paper2patent", "title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _upload_paper(client: TestClient, case_id: str) -> str:
    resp = client.post(
        f"/api/v1/cases/{case_id}/files",
        files={"files": ("test_paper.md", PAPER_MD.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()[0]["file"]["id"]


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
        "SELECT step_key, status, error FROM pipeline_runs WHERE case_id=? AND status='failed'",
        (case_id,),
    )
    return "；".join(f"{r['step_key']}: {r['error']}" for r in rows) or "无失败步骤记录"


def _artifacts(case_id: str, kind: str) -> list[dict[str, Any]]:
    from app.db import database as db

    rows = db.query_all(
        "SELECT * FROM artifacts WHERE case_id=? AND kind=? ORDER BY version ASC", (case_id, kind)
    )
    return [dict(r) for r in rows]


async def _drive(case_id: str, task: asyncio.Task, answers: dict[str, Any], on_gate=None) -> list[str]:
    """轮询门控并按 step_key 提交预置答复，直到流水线结束；返回遇到的门控顺序。"""
    from app.pipelines import engine

    seen: list[str] = []
    while not task.done():
        pending = engine.get_pending(case_id)
        if pending is not None:
            step_key = pending["step_key"]
            if step_key not in seen:
                seen.append(step_key)
                if on_gate is not None:
                    on_gate(pending)
            try:
                engine.submit_input(case_id, step_key, answers.get(step_key, {}))
            except engine.PipelineConflictError:
                pass                      # 已提交、正在处理中
        await asyncio.sleep(0.05)
    await task
    return seen


# ---------------------------------------------------------------------------
# 1. direct 模式全流水线
# ---------------------------------------------------------------------------

_direct: dict[str, Any] = {}


async def test_direct_pipeline_runs_to_build(client: TestClient, fake_llm: FakeLLM) -> None:
    """direct 模式跑完 input_check → … → build，产出内容契约、附图、DOCX。"""
    from app.pipelines import engine

    case_id = _new_case(client, "论文转专利 direct 测试")
    file_id = _upload_paper(client, case_id)

    task = engine.start(
        case_id, _steps(case_id), run_group="initial",
        start_payload={"file_id": file_id, "mode": "direct"},
    )
    await asyncio.wait_for(task, timeout=600)

    assert _case_row(case_id)["status"] == "completed", _failures(case_id)
    _direct["case_id"] = case_id
    _direct["state"] = _case_state(case_id)

    # 走过了 REPAIR 分支（首版权项含禁用词「优选」）
    assert "struct:claims.repair1" in fake_llm.calls
    assert _direct["state"]["lint_report"]["repair_attempts"] == 1
    assert _direct["state"]["lint_report"]["passed"] is True


def test_direct_content_artifact_valid(client: TestClient) -> None:
    """patent_content_json 交付物存在且通过 PatentContent 校验。"""
    from app.models.paper2patent import PatentContent

    case_id = _direct["case_id"]
    items = _artifacts(case_id, "patent_content_json")
    assert items, "未产出 patent_content_json 交付物"
    latest = items[-1]
    data = json.loads(Path(latest["stored_path"]).read_text(encoding="utf-8"))
    content = PatentContent.model_validate(data)          # 契约校验

    assert content.invention_name == INVENTION_NAME
    assert len(content.claims) == 3
    assert "优选" not in content.claims[1]                 # REPAIR 生效
    assert content.description.embodiments
    assert content.drawing_assets and content.image_model_prompts

    # 忠实性审计：unsourced 断言在 direct 模式就地转为【待补充：…】并入 gaps
    assert f"【待补充：{UNSOURCED_ASSERTION}。】" in content.description.embodiments
    assert any("【待补充" in g for g in content.gaps)
    # 质检补丁（评价性语言）被服务端确定性应用
    assert "本发明具有显著优点" not in content.description.embodiments

    # API 也能读到同一份内容
    got = client.get(f"/api/v1/cases/{case_id}/p2p/content")
    assert got.status_code == 200, got.text
    assert got.json()["content"]["invention_name"] == INVENTION_NAME


def test_direct_figures_generated() -> None:
    """SVG/PNG 由零依赖脚本真跑产出，且落成 figure_svg / figure_png 交付物。"""
    case_id = _direct["case_id"]
    svgs = _artifacts(case_id, "figure_svg")
    assert len(svgs) >= 2, "至少应产出两幅 SVG 附图"
    for item in svgs:
        text = Path(item["stored_path"]).read_text(encoding="utf-8")
        assert "<svg" in text
        assert "图1" not in text and "图2" not in text     # 图号/图题不得画进画布

    pngs = _artifacts(case_id, "figure_png")
    assert pngs, "未产出 PNG 兜底附图（Pillow 不可用？）"
    for item in pngs:
        assert Path(item["stored_path"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    validations = _direct["state"]["content"]["drawing_validation"]
    assert validations and all(v["passes"] for v in validations)


def test_direct_docx_generated() -> None:
    """DOCX 真实生成：python-docx 可打开、含五节标题、word/media 内有嵌图。"""
    from docx import Document

    case_id = _direct["case_id"]
    docx_items = _artifacts(case_id, "patent_docx")
    assert docx_items, "未产出 patent_docx 交付物"
    path = Path(docx_items[-1]["stored_path"])

    doc = Document(str(path))
    texts = [p.text.strip() for p in doc.paragraphs]
    for heading in ("一、说明书摘要", "二、摘要附图", "三、权利要求书", "四、说明书", "五、说明书附图"):
        assert any(t.startswith(heading) for t in texts), f"DOCX 缺少小节：{heading}"
    assert any(INVENTION_NAME in t for t in texts)

    with zipfile.ZipFile(path) as zf:
        media = [n for n in zf.namelist() if n.startswith("word/media/")]
    assert media, "DOCX 的 word/media/ 内没有嵌入的附图图片"


def test_direct_pdf_generated() -> None:
    """PDF 真实生成（Word COM → soffice → Pillow 图片版兜底）；全失败则 skip 并记录。"""
    case_id = _direct["case_id"]
    build = _direct["state"].get("build") or {}
    pdf_items = _artifacts(case_id, "patent_pdf")
    if not pdf_items:
        pytest.skip(f"PDF 全部引擎不可用，已记录：{build.get('pdf_error')}")
    data = Path(pdf_items[-1]["stored_path"]).read_bytes()
    assert data[:5] == b"%PDF-"
    assert build.get("pdf_engine") in ("word", "soffice", "pillow")


def test_direct_image_prompts_endpoint(client: TestClient) -> None:
    """GET /p2p/image-prompts：逐图精修提示词 + 原始 Prompt 模板。"""
    case_id = _direct["case_id"]
    resp = client.get(f"/api/v1/cases/{case_id}/p2p/image-prompts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["prompts"]) >= 2
    assert all(p["prompt"] for p in body["prompts"])
    assert "专利附图" in body["template"] or body["template"]


def test_put_content_lint_gate(client: TestClient) -> None:
    """PUT /p2p/content：权项书式违规 422；合法内容落新版本并回写 state。"""
    case_id = _direct["case_id"]
    content = client.get(f"/api/v1/cases/{case_id}/p2p/content").json()["content"]

    bad = json.loads(json.dumps(content))
    bad["claims"] = list(bad["claims"])
    bad["claims"][1] = "2.根据权利要求1所述的方法，其特征在于，所述划分采用聚类算法。多余的句号。"
    resp = client.put(f"/api/v1/cases/{case_id}/p2p/content", json={"content": bad})
    assert resp.status_code == 422
    assert "findings" in resp.json()["detail"]

    before = len(_artifacts(case_id, "patent_content_json"))
    good = json.loads(json.dumps(content))
    good["source_title"] = "Graph-Guided Superpixel Network"
    ok = client.put(f"/api/v1/cases/{case_id}/p2p/content", json={"content": good})
    assert ok.status_code == 200, ok.text
    assert len(_artifacts(case_id, "patent_content_json")) == before + 1
    assert _case_state(case_id)["content"]["source_title"] == "Graph-Guided Superpixel Network"


def test_rebuild_drawings_and_build_endpoints(client: TestClient) -> None:
    """POST /p2p/drawings 与 /p2p/build：同步复跑脚本链，产出新版本交付物。"""
    case_id = _direct["case_id"]

    resp = client.post(f"/api/v1/cases/{case_id}/p2p/drawings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["figures"]) >= 2 and body["degraded"] == []

    before = len(_artifacts(case_id, "patent_docx"))
    built = client.post(f"/api/v1/cases/{case_id}/p2p/build")
    assert built.status_code == 200, built.text
    assert len(_artifacts(case_id, "patent_docx")) == before + 1
    assert built.json()["docx"]["filename"].endswith(".docx")


def test_drawings_degrade_to_prompt_only(client: TestClient) -> None:
    """规格不足以成图时降级为 image_model_prompt-only，并把缺口写进 gaps。"""
    from app.api.p2p import _merge_state_sync

    case_id = _new_case(client, "论文转专利 附图降级测试")
    source = _artifacts(_direct["case_id"], "patent_content_json")[-1]
    content = json.loads(Path(source["stored_path"]).read_text(encoding="utf-8"))
    content["drawings"] = ["图1：一种示意图，其内容无法解析出步骤或模块。"]
    content.pop("drawing_assets", None)
    _merge_state_sync(case_id, {"content": content})

    resp = client.post(f"/api/v1/cases/{case_id}/p2p/drawings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["degraded"] == [1] and body["figures"] == []
    assert any("图1" in g and "【待补充" in g for g in body["gaps"])

    prompts = client.get(f"/api/v1/cases/{case_id}/p2p/image-prompts").json()["prompts"]
    assert prompts and prompts[0]["figure_no"] == 1
    assert "纯黑白线条图" in prompts[0]["prompt"]


# ---------------------------------------------------------------------------
# 2. human-in-loop 模式：门控挂起 → submit_input → 跑完
# ---------------------------------------------------------------------------


async def test_human_in_loop_gates(client: TestClient, fake_llm: FakeLLM) -> None:
    """hil 模式在 content_review 处 waiting_user 且 pending_interaction 可见，提交后跑完。"""
    from app.pipelines import engine

    case_id = _new_case(client, "论文转专利 hil 测试")
    file_id = _upload_paper(client, case_id)

    observed: dict[str, Any] = {}

    def on_gate(pending: dict[str, Any]) -> None:
        if pending["step_key"] != "content_review":
            return
        observed["case_status"] = _case_row(case_id)["status"]
        state = client.get(f"/api/v1/cases/{case_id}/pipeline/state").json()
        observed["pending"] = state["pending_interaction"]

    task = engine.start(
        case_id, _steps(case_id), run_group="initial",
        start_payload={"file_id": file_id, "mode": "human_in_loop"},
    )
    answers = {
        "input_check": {
            "supplements": [
                {"item": "论文未给出超像素数量的取值依据", "reply": ""}
            ],
            "note": "",
        },
        "rules_check": {"decisions": [{"action": "placeholder", "text": ""}]},
        "content_review": {"approved": True},
    }
    seen = await asyncio.wait_for(_drive(case_id, task, answers, on_gate=on_gate), timeout=600)

    assert _case_row(case_id)["status"] == "completed", _failures(case_id)
    assert "input_check" in seen and "rules_check" in seen and "content_review" in seen
    assert observed["case_status"] == "waiting_user"
    assert observed["pending"]["step_key"] == "content_review"
    assert observed["pending"]["kind"] == "review"

    state = _case_state(case_id)
    assert state["content"]["invention_name"] == INVENTION_NAME
    assert _artifacts(case_id, "patent_docx"), "hil 模式未产出 Word 交付物"


# ---------------------------------------------------------------------------
# 3. flash 快速档
# ---------------------------------------------------------------------------


async def test_flash_mode_single_call(client: TestClient, fake_llm: FakeLLM) -> None:
    """flash：单次流式调用产五部分纯文本 → md 交付物 → 后续步骤空跑。"""
    from app.pipelines import engine

    case_id = _new_case(client, "论文转专利 flash 测试")
    file_id = _upload_paper(client, case_id)

    task = engine.start(
        case_id, _steps(case_id), run_group="initial",
        start_payload={"file_id": file_id, "mode": "flash"},
    )
    await asyncio.wait_for(task, timeout=300)

    assert _case_row(case_id)["status"] == "completed", _failures(case_id)
    assert fake_llm.calls == ["stream:flash"], f"flash 应只发一次流式调用：{fake_llm.calls}"

    items = _artifacts(case_id, "patent_content_json")
    assert len(items) == 1 and items[0]["filename"].endswith(".md")
    text = Path(items[0]["stored_path"]).read_text(encoding="utf-8")
    assert "一、说明书摘要" in text and "五、说明书附图" in text
    assert not _artifacts(case_id, "patent_docx")     # 快速档不进文件管线


# ---------------------------------------------------------------------------
# 4. 单元级：prompt 装配、补丁定位、附图降级
# ---------------------------------------------------------------------------


def test_prompt_parts_assemble() -> None:
    """模块 B 用到的所有 prompt 资产都能装配（缺文件会抛 KeyError）。"""
    from app.pipelines import paper2patent as p2p
    from app.services import assembler

    for parts in (
        p2p.BASE_PARTS, p2p.FLASH_PARTS, p2p.ASSESS_PARTS, p2p.EXTRACTION_PARTS,
        p2p.CLAIMS_PARTS, p2p.ABSTRACT_PARTS, p2p.DESCRIPTION_PARTS, p2p.DRAWING_SPEC_PARTS,
    ):
        prompt = assembler.assemble(parts, runtime_ctx={"case_card": {"module": "paper2patent"}})
        assert len(prompt) > 200
        assert len(prompt.file_hashes) == len(parts)


def test_locate_exact_then_fuzzy() -> None:
    """补丁定位：精确匹配优先；失败时按空白归一化模糊匹配并还原原文区间。"""
    from app.services import faithfulness

    text = "第一句话。\n第二句 话，包含换行与空格。"
    assert faithfulness.locate(text, "第一句话。") == (0, 5)
    span = faithfulness.locate(text, "第二句话，包含换行与空格。")
    assert span is not None
    assert text[span[0] : span[1]] == "第二句 话，包含换行与空格。"
    assert faithfulness.locate(text, "不存在的片段") is None


def test_patch_apply_rate_floor() -> None:
    """补丁应用率 <80% 时 low_rate 置位（交人工，不静默吞掉）。"""
    from app.models.paper2patent import QualityPatch
    from app.services import faithfulness

    draft = {"abstract": "原始摘要正文。", "claims": [], "description": {}, "drawings": []}
    patches = [
        QualityPatch(section="abstract", original_excerpt="原始摘要", replacement="修订摘要"),
        QualityPatch(section="abstract", original_excerpt="根本不存在", replacement="X"),
    ]
    result = faithfulness.apply_patches(draft, patches)
    assert result["applied"] == 1 and result["low_rate"] is True
    assert result["draft"]["abstract"] == "修订摘要正文。"


def test_degrade_figure_to_prompt_only() -> None:
    """附图降级：移出 drawings/drawing_assets，补 image_model_prompt 与 gaps 文案。"""
    from app.services import drawings as drawings_service

    content = {
        "drawings": ["图1：流程图，包含步骤S101，甲；S102，乙。", "图2：结构示意图，包含A模块、B模块。"],
        "drawing_assets": [{"figure_no": 2, "spec": "图2：结构示意图，包含A模块、B模块。"}],
        "description": {"drawing_description": ["图1为流程图。", "图2为结构示意图。"]},
        "gaps": [],
    }
    gap = drawings_service.degrade_figure(content, 2, "画布校验未通过")
    assert "图2" in gap and gap in content["gaps"]
    assert len(content["drawings"]) == 1 and not content["drawing_assets"]
    assert content["description"]["drawing_description"] == ["图1为流程图。"]
    assert content["image_model_prompts"][0]["figure_no"] == 2
    assert "纯黑白线条图" in content["image_model_prompts"][0]["prompt"]
