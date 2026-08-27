"""M2 导出管线测试：mermaid 渲染、md→docx（OMML）、docx→pdf（Word COM）、交付物版本化。

- mermaid：本机 Chrome 可用则断言 PNG magic bytes，否则 skip；
- md→docx：真实子进程调 md_to_docx.py，python-docx 打开验证段落数；
- docx→pdf：Word COM 真实转一次，COM 失败则 skip；
- 版本化：同名两次落盘版本递增、文件名不同（时间戳秒级 +1 防覆盖）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 含 LaTeX 公式（行内 + 块级）与 mermaid 围栏的样例 md
SAMPLE_MD = """# 测试交底书

**案件名称**：一种批量作业调度装置

## 技术方案

行内公式 $E = mc^2$，块级公式：

$$
\\bar{x} = \\frac{1}{n} \\sum_{i=1}^{n} x_i
$$

```mermaid
flowchart TD
    S1[接收作业] --> S2[打分排序]
    S2 --> S3[派发执行]
```

以上为测试内容。
"""

# 跨测试共享（docx→pdf 依赖 md→docx 的产物）
_shared: dict = {}



@pytest.fixture(scope="module")
def case_id(client: TestClient) -> str:
    resp = client.post("/api/v1/cases", json={"module": "disclosure", "title": "M2 导出测试案件"})
    assert resp.status_code == 201
    return resp.json()["id"]


def test_render_mermaid(client: TestClient) -> None:
    """POST /render/mermaid：成功则 PNG magic bytes；浏览器不可用则 skip。"""
    resp = client.post(
        "/api/v1/render/mermaid",
        json={"code": "flowchart TD\n    A[开始] --> B[结束]\n"},
    )
    if resp.status_code == 422:
        pytest.skip(f"mermaid 渲染不可用（本机浏览器/Playwright 问题）：{resp.json()}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"   # PNG magic bytes
    _shared["mermaid_ok"] = True


def test_render_mermaid_bad_code(client: TestClient) -> None:
    """语法错误的 mermaid → 422 且 detail 带错误详情。"""
    resp = client.post(
        "/api/v1/render/mermaid",
        json={"code": "this is definitely !!! not mermaid ((("},
    )
    assert resp.status_code == 422
    assert "渲染失败" in resp.json()["detail"]


def test_export_md_to_docx(client: TestClient, case_id: str) -> None:
    """md 交付物 → docx：python-docx 可打开且段落数 > 0、含正文关键词。"""
    from app.services import artifacts as artifacts_service

    art = artifacts_service.save_artifact_sync(
        case_id, "disclosure_md", SAMPLE_MD, "md", iteration_type="initial",
    )
    assert art.version == 1
    assert art.filename.startswith("一种批量作业调度装置_")   # 案件名取自正文「**案件名称**：」行

    resp = client.post(f"/api/v1/artifacts/{art.id}/export", json={"format": "docx"})
    assert resp.status_code == 200, resp.text
    exported = resp.json()
    assert exported["kind"] == "disclosure_docx"
    assert exported["version"] == 1
    assert exported["iteration_type"] == "export"
    assert exported["source_artifact_id"] == art.id
    assert exported["filename"].startswith("一种批量作业调度装置_")

    from docx import Document

    doc = Document(exported["stored_path"])
    texts = [p.text for p in doc.paragraphs]
    assert len([t for t in texts if t.strip()]) > 0
    assert any("技术方案" in t for t in texts)
    _shared["docx_artifact"] = exported

    # 交付物列表应能按 kind 过滤到两个版本序列
    listed = client.get(f"/api/v1/cases/{case_id}/artifacts", params={"kind": "disclosure_docx"})
    assert listed.status_code == 200
    assert any(a["id"] == exported["id"] for a in listed.json())


def test_export_docx_to_pdf(client: TestClient) -> None:
    """docx 交付物 → pdf：Word COM 真实转换；COM/引擎失败则 skip。"""
    docx_art = _shared.get("docx_artifact")
    if not docx_art:
        pytest.skip("前置 md→docx 未产出 docx 交付物")

    resp = client.post(f"/api/v1/artifacts/{docx_art['id']}/export", json={"format": "pdf"})
    if resp.status_code != 200:
        pytest.skip(f"docx→pdf 引擎不可用（Word COM/soffice）：{resp.text}")
    exported = resp.json()
    assert exported["kind"] == "disclosure_pdf"
    pdf_bytes = Path(exported["stored_path"]).read_bytes()
    assert pdf_bytes[:5] == b"%PDF-"                 # PDF magic bytes
    assert exported["source_artifact_id"] == docx_art["id"]

    # download 接口可取回同样内容
    dl = client.get(f"/api/v1/artifacts/{exported['id']}/download")
    assert dl.status_code == 200
    assert dl.content[:5] == b"%PDF-"


def test_pdf_probe() -> None:
    """probe() 报告引擎可用性结构完整。"""
    from app.services import export_pdf

    report = export_pdf.probe()
    assert "word" in report and "soffice" in report and "order" in report
    assert isinstance(report["order"], list)


def test_artifact_versioning(case_id: str) -> None:
    """同名两次落盘：版本递增、文件名不同（同秒冲突时间戳 +1s）。"""
    from app.services import artifacts as artifacts_service

    a1 = artifacts_service.save_artifact_sync(case_id, "revision_log_md", "第一次内容", "md", title="同名版本测试")
    a2 = artifacts_service.save_artifact_sync(case_id, "revision_log_md", "第二次内容", "md", title="同名版本测试")
    assert a2.version == a1.version + 1
    assert a1.filename != a2.filename                # 禁覆盖：文件名必不同
    assert Path(a1.stored_path).is_file() and Path(a2.stored_path).is_file()
    assert Path(a1.stored_path).read_text(encoding="utf-8") == "第一次内容"
    assert Path(a2.stored_path).read_text(encoding="utf-8") == "第二次内容"


def test_artifact_content_endpoint(client: TestClient, case_id: str) -> None:
    """content 接口：文本类全文预览；不支持导出的 kind → 422。"""
    from app.services import artifacts as artifacts_service

    art = artifacts_service.save_artifact_sync(case_id, "search_report_json", '{"hits": []}', "json")
    resp = client.get(f"/api/v1/artifacts/{art.id}/content")
    assert resp.status_code == 200
    assert resp.json()["content"] == '{"hits": []}'

    # search_report_json 没有对应 docx kind → 422
    bad = client.post(f"/api/v1/artifacts/{art.id}/export", json={"format": "docx"})
    assert bad.status_code == 422


def test_normalize_case_name() -> None:
    """案件名规范化：正文行优先、占位符回落 title、非法字符剔除、≤80 字符。"""
    from app.services.artifacts import normalize_case_name

    # 正文「**案件名称**：」行优先
    assert normalize_case_name("**案件名称**：一种测试装置\n其余内容", "别名") == "一种测试装置"
    # 占位符（XXX）回落 title
    assert normalize_case_name("**案件名称**：XXX", "备用标题") == "备用标题"
    # 非法字符剔除
    assert normalize_case_name(None, 'A/B\\C:D*E?F"G<H>I|J') == "ABCDEFGHIJ"
    # 全无 → 未命名案件
    assert normalize_case_name(None, None) == "未命名案件"
    # 截断 ≤80
    assert len(normalize_case_name(None, "长" * 200)) == 80
