# -*- coding: utf-8 -*-
"""案件媒体端点：网页端要能取到正文里引用的图。

背景：文档正文里的图片写的是磁盘路径。Word / PDF 导出器直接读盘，所以导出件里
图是全的；浏览器读不了本机路径，网页端只剩图题没有图。这个端点补上那一段。

另一半同样重要：路径由请求方给，而请求方拿到的路径又来自模型生成的正文——
所以「只能读到本案件自己的图」这条边界必须有测试盯着，否则它就是一个
带鉴权的任意文件读取口。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_config

API = "/api/v1"

# 1x1 PNG（够 Pillow / 浏览器认，也够验证字节原样返回）
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


@pytest.fixture
def case_with_figure(client: TestClient):
    """建一个案件，并在它的输出目录里放一张附图。"""
    r = client.post(f"{API}/cases", json={"module": "paper2patent", "title": "媒体端点用例"})
    assert r.status_code in (200, 201), r.text
    case_id = r.json()["id"]

    work = get_config().outputs_dir / case_id / "p2p_work"
    work.mkdir(parents=True, exist_ok=True)
    png = work / "patent_图1.png"
    png.write_bytes(PNG_BYTES)
    return case_id, png


def test_absolute_path_is_served(client: TestClient, case_with_figure) -> None:
    """交底书插图写的就是绝对路径。"""
    case_id, png = case_with_figure
    r = client.get(f"{API}/cases/{case_id}/media", params={"path": str(png)})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/png")
    assert r.content == PNG_BYTES


def test_workdir_relative_path_is_served(client: TestClient, case_with_figure) -> None:
    """附图脚本回写的 png_path 常常只是相对工作目录的文件名。"""
    case_id, png = case_with_figure
    r = client.get(f"{API}/cases/{case_id}/media", params={"path": png.name})
    assert r.status_code == 200, r.text
    assert r.content == PNG_BYTES


def test_path_outside_the_case_is_refused(client: TestClient, case_with_figure) -> None:
    """越界一律 404 —— 这个端点不能变成带鉴权的任意文件读取口。"""
    case_id, _ = case_with_figure
    cfg = get_config()
    outsider = cfg.data_dir / "outsider.png"
    outsider.write_bytes(PNG_BYTES)

    r = client.get(f"{API}/cases/{case_id}/media", params={"path": str(outsider)})
    assert r.status_code == 404


def test_traversal_is_refused(client: TestClient, case_with_figure) -> None:
    """`..` 拼出来的路径同样要被包含判定挡住。"""
    case_id, _ = case_with_figure
    cfg = get_config()
    (cfg.data_dir / "outsider2.png").write_bytes(PNG_BYTES)

    r = client.get(
        f"{API}/cases/{case_id}/media", params={"path": "../../../outsider2.png"}
    )
    assert r.status_code == 404


def test_non_image_is_refused(client: TestClient, case_with_figure) -> None:
    """只放行图片：放宽到任意扩展名就等于把案件目录开成静态站点。"""
    case_id, png = case_with_figure
    leak = png.parent / "patent_content.json"
    leak.write_text('{"secret": 1}', encoding="utf-8")

    r = client.get(f"{API}/cases/{case_id}/media", params={"path": str(leak)})
    assert r.status_code == 404


def test_other_users_case_is_refused(other_client: TestClient, case_with_figure) -> None:
    """案件归属校验走的是与其它端点同一条 resolve_case_sync。"""
    case_id, png = case_with_figure
    r = other_client.get(f"{API}/cases/{case_id}/media", params={"path": str(png)})
    assert r.status_code in (403, 404)


def test_requires_login(raw_client: TestClient, case_with_figure) -> None:
    case_id, png = case_with_figure
    r = raw_client.get(f"{API}/cases/{case_id}/media", params={"path": str(png)})
    assert r.status_code in (401, 403)


def test_missing_file_is_404(client: TestClient, case_with_figure) -> None:
    case_id, png = case_with_figure
    r = client.get(f"{API}/cases/{case_id}/media", params={"path": str(png.parent / "无此图.png")})
    assert r.status_code == 404


def test_symlink_out_of_the_case_is_refused(client: TestClient, case_with_figure) -> None:
    """符号链接必须先解开再判包含，否则一条链接就能绕过整套边界。"""
    case_id, png = case_with_figure
    cfg = get_config()
    target = cfg.data_dir / "linked_outside.png"
    target.write_bytes(PNG_BYTES)
    link = png.parent / "escape.png"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("本环境不允许创建符号链接（Windows 需开发者模式或管理员）")

    r = client.get(f"{API}/cases/{case_id}/media", params={"path": str(link)})
    assert r.status_code == 404


def test_disclosure_upload_figure_is_served(client: TestClient) -> None:
    """交底书插图落在 uploads/{case_id}/，正文里写的是 as_posix() 的绝对路径。

    这正是「导出的 Word 有图、网页端没图」的那条路径，必须逐字按它的写法验。
    """
    r = client.post(f"{API}/cases", json={"module": "disclosure", "title": "插图用例"})
    assert r.status_code in (200, 201), r.text
    case_id = r.json()["id"]

    updir = get_config().uploads_dir / case_id
    updir.mkdir(parents=True, exist_ok=True)
    img = updir / "结构示意图.png"
    img.write_bytes(PNG_BYTES)

    # figure_plan.figure_markdown 写进正文的就是这个形式
    as_posix = Path(img).as_posix()
    assert "/" in as_posix

    r = client.get(f"{API}/cases/{case_id}/media", params={"path": as_posix})
    assert r.status_code == 200, r.text
    assert r.content == PNG_BYTES


def test_data_dir_relative_path_is_served(client: TestClient, case_with_figure) -> None:
    """`outputs/<case>/x.png` 就是 artifacts.stored_path 的入库形态。

    部署端在恢复演练里正是照这个形态传的，拿到 404 后差点当成缺陷报上来。
    少认一种写法，调用方就得去猜该传哪种——那种猜测迟早会以 bug 的形式回来。
    """
    case_id, png = case_with_figure
    rel = f"outputs/{case_id}/p2p_work/{png.name}"
    r = client.get(f"{API}/cases/{case_id}/media", params={"path": rel})
    assert r.status_code == 200, r.text
    assert r.content == PNG_BYTES


def test_another_cases_data_dir_relative_path_is_refused(
    client: TestClient, case_with_figure
) -> None:
    """认得更多不等于放得更松：别的案件的路径照样进不来。"""
    case_id, png = case_with_figure
    other = client.post(f"{API}/cases", json={"module": "disclosure", "title": "别人的案件"})
    other_id = other.json()["id"]
    outdir = get_config().outputs_dir / other_id
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "secret.png").write_bytes(PNG_BYTES)

    r = client.get(
        f"{API}/cases/{case_id}/media", params={"path": f"outputs/{other_id}/secret.png"}
    )
    assert r.status_code == 404, "跨案件取图必须被包含判定挡住"


def test_double_encoded_path_still_resolves(client: TestClient, case_with_figure) -> None:
    """多编码一层的路径也要认。

    这是生产上「右侧一张图都不显示」的真正成因：markdown 按 CommonMark 把链接目标
    做了 URL 规范化，`patent_图1.png` 到渲染器手里已经是 `patent_%E5%9B%BE1.png`；
    前端再编码一次，服务端解一次只得到那个字面名字 —— **文件名带汉字的图全 404**。
    附图文件名清一色是「patent_图N.png」，所以现象是一张都不显示。

    前端已经改成先解一次再编码；这里是服务端的兜底，任何调用方多编一层都不该让图消失。
    """
    case_id, png = case_with_figure
    from urllib.parse import quote

    once = quote(png.name)                    # markdown 规范化后的形态
    assert once != png.name, "用例前提：文件名含非 ASCII"

    r = client.get(f"{API}/cases/{case_id}/media", params={"path": once})
    assert r.status_code == 200, f"多编码一层的路径应当仍能取到：{once} → {r.text[:200]}"
    assert r.content == PNG_BYTES


def test_double_encoding_does_not_open_a_traversal_hole(
    client: TestClient, case_with_figure
) -> None:
    """认得更多不等于放得更松：编码过的 `../` 照样被包含判定挡住。"""
    case_id, _ = case_with_figure
    cfg = get_config()
    (cfg.data_dir / "encoded_outsider.png").write_bytes(PNG_BYTES)

    for probe in ("%2e%2e%2f%2e%2e%2fencoded_outsider.png",
                  "..%2f..%2f..%2fencoded_outsider.png"):
        r = client.get(f"{API}/cases/{case_id}/media", params={"path": probe})
        assert r.status_code == 404, f"{probe} 不该被放行"
