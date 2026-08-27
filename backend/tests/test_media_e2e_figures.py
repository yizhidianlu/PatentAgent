# -*- coding: utf-8 -*-
"""端到端：论文转专利的附图，从附图脚本一路走到网页端能取到的 URL。

这条链此前是断的：附图脚本把 PNG 落进 `outputs/{case_id}/p2p_work/`，
把**相对工作目录的文件名**写进 `drawing_assets[].png_path`；DOCX 生成器按盘读，
所以 Word / PDF 里图是全的。而网页端的文档面板是从 `patent_content_json` 装配的，
既没有出图、就算出了图浏览器也读不了本机路径——「只有导出的 Word 和 PDF 有图」。

所以这里不造假路径：真的跑一遍附图脚本，拿它真实回写的 png_path 去请求媒体端点。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_config
from app.services import artifacts as artifacts_service

API = "/api/v1"

_TOOLS = Path(__file__).resolve().parents[1] / "app" / "tools"

# 附图脚本要求规格里的步骤/模块足够明确，否则会按设计拒绝成图
_DRAWINGS = [
    "图1为本发明方法的流程示意图；S101，采集喉镜图像帧；S102，提取多尺度特征；"
    "S103，融合特征并精炼；S104，输出检测结果。",
    "图2为本发明系统的结构框图，包含采集模块、骨干网络模块、颈部融合模块、检测头模块。",
]


@pytest.fixture
def case_with_real_figures(client: TestClient):
    """建案件 → 在它的 p2p_work 里真跑一次附图脚本 → 落一版 patent_content_json。"""
    r = client.post(f"{API}/cases", json={"module": "paper2patent", "title": "附图端到端"})
    assert r.status_code in (200, 201), r.text
    case_id = r.json()["id"]

    work = get_config().outputs_dir / case_id / "p2p_work"
    work.mkdir(parents=True, exist_ok=True)
    content = {
        "invention_name": "一种气管插管解剖结构检测方法",
        "abstract": "摘要占位。",
        "claims": ["1. 一种方法，其特征在于……"],
        "description": {"technical_field": "本发明属于医学图像处理领域。"},
        "drawings": list(_DRAWINGS),
    }
    json_path = work / "patent_content.json"
    json_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(_TOOLS / "generate_patent_drawings.py"), str(json_path),
         "--output-dir", str(work), "--prefix", "patent", "--update-json"],
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        pytest.skip(f"附图脚本未能在本环境运行：{(proc.stderr or '')[-300:]}")

    updated = json.loads(json_path.read_text(encoding="utf-8-sig"))
    assert updated.get("drawing_assets"), "附图脚本没有回写 drawing_assets"

    artifact = artifacts_service.save_artifact_sync(
        case_id, "patent_content_json",
        json.dumps(updated, ensure_ascii=False), "json", title="附图端到端",
    )
    return case_id, artifact.id, updated


def test_figure_paths_from_the_artifact_are_fetchable(client: TestClient, case_with_real_figures) -> None:
    """网页端拿到的正是这份 JSON —— 里面的每个 png_path 都必须能取到图。"""
    case_id, artifact_id, _ = case_with_real_figures

    # 前端文档面板的数据源
    r = client.get(f"{API}/artifacts/{artifact_id}/content")
    assert r.status_code == 200, r.text
    content = json.loads(r.json()["content"])

    assets = content["drawing_assets"]
    assert len(assets) >= 2, "样例规格应当出两幅图"

    for asset in assets:
        # docHydration.figuresMarkdown 的取值次序：png 优先，回落 svg
        path = str(asset.get("png_path") or asset.get("svg_path") or "").strip()
        assert path, f"图{asset.get('figure_no')} 没有任何图片路径"
        # 脚本回写的是相对工作目录的文件名——这正是最容易被忽略的一种形态
        assert not Path(path).is_absolute()

        got = client.get(f"{API}/cases/{case_id}/media", params={"path": path})
        assert got.status_code == 200, f"图{asset.get('figure_no')} 取不到：{got.text[:200]}"
        assert got.headers["content-type"].startswith("image/")
        assert len(got.content) > 0


def test_svg_figures_are_also_fetchable(client: TestClient, case_with_real_figures) -> None:
    """SVG 是矢量源，用户可能要放大看；它同样在白名单里。"""
    case_id, _, content = case_with_real_figures
    for asset in content["drawing_assets"]:
        svg = str(asset.get("svg_path") or "").strip()
        if not svg:
            continue
        r = client.get(f"{API}/cases/{case_id}/media", params={"path": svg})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("image/svg")


def test_work_json_is_not_reachable_through_the_image_endpoint(
    client: TestClient, case_with_real_figures
) -> None:
    """同一目录里躺着 patent_content.json，媒体端点不能顺手把它一并放出去。"""
    case_id, _, _ = case_with_real_figures
    r = client.get(f"{API}/cases/{case_id}/media", params={"path": "patent_content.json"})
    assert r.status_code == 404
