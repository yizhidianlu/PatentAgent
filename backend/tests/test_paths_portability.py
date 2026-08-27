# -*- coding: utf-8 -*-
"""库要能跟着数据目录一起搬：路径不再绑死在某一台机器的某一个目录上。

此前 `files.stored_path` / `files.md_path` / `artifacts.stored_path` 存的是绝对路径。
把备份恢复到别的目录、别的机器之后：下载 404、预览 404、正文里的插图与说明书附图
全变裂图——**而且一声不吭**。数据库好好的、案件列表好好的、正文好好的，
只有点下去才发现打不开。灾备最怕的正是这种「看起来恢复成功了」。

所以这里验两件事：
1. 新写入的路径是相对形态（换目录即生效，不需要任何修补动作）；
2. 旧的绝对路径——包括**别的机器上的**绝对路径——仍然能落到当前数据目录。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ulid import ULID

from app.config import get_config
from app.db import database as db
from app.services import artifacts as artifacts_service
from app.services import paths as paths_service

API = "/api/v1"

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


# ---------------------------------------------------------------------------
# 存取形态
# ---------------------------------------------------------------------------


def test_to_stored_returns_a_relative_posix_path() -> None:
    """数据目录内 → 相对 POSIX 路径。用 POSIX 分隔符是为了跨 Windows/Linux 可搬。"""
    p = get_config().outputs_dir / "CASE" / "x.docx"
    stored = paths_service.to_stored(p)
    assert stored == "outputs/CASE/x.docx"
    assert not Path(stored).is_absolute()


def test_to_stored_keeps_paths_outside_the_data_dir(tmp_path: Path) -> None:
    """落在数据目录之外的路径原样保留（并已在实现里告警）——不能悄悄改错。"""
    outsider = tmp_path / "elsewhere.md"
    assert paths_service.to_stored(outsider) == str(outsider)


def test_resolve_accepts_relative_absolute_and_foreign_absolute() -> None:
    """三种形态都要认，其中第三种才是灾备的真正考题。"""
    cfg = get_config()
    case_dir = cfg.outputs_dir / "PORTCASE"
    case_dir.mkdir(parents=True, exist_ok=True)
    real = case_dir / "figure.png"
    real.write_bytes(PNG)

    assert paths_service.resolve("outputs/PORTCASE/figure.png") == real
    assert paths_service.resolve(str(real)) == real

    # 「别的机器上」的绝对路径：源机器的根目录完全不同，且用的是 Windows 分隔符
    foreign = r"D:\OldServer\PatentAgent\data\outputs\PORTCASE\figure.png"
    assert paths_service.resolve(foreign) == real, "换机恢复后必须能重新落到当前数据目录"

    foreign_posix = "/srv/patentagent/data/outputs/PORTCASE/figure.png"
    assert paths_service.resolve(foreign_posix) == real


def test_resolve_existing_returns_none_when_the_file_is_gone() -> None:
    assert paths_service.resolve_existing("outputs/PORTCASE/根本没有这个文件.png") is None
    assert paths_service.resolve_existing(None) is None
    assert paths_service.resolve_existing("") is None


# ---------------------------------------------------------------------------
# 入库形态
# ---------------------------------------------------------------------------


def test_uploaded_file_is_stored_relative(client: TestClient) -> None:
    r = client.post(f"{API}/cases", json={"module": "disclosure", "title": "路径可搬"})
    case_id = r.json()["id"]
    up = client.post(
        f"{API}/cases/{case_id}/files",
        files=[("files", ("材料.md", "# 正文\n内容".encode(), "text/markdown"))],
    )
    assert up.status_code == 201, up.text
    item = up.json()[0]["file"]

    assert not Path(item["stored_path"]).is_absolute(), item["stored_path"]
    assert item["stored_path"].startswith("uploads/")
    assert not Path(item["md_path"]).is_absolute()
    # 顺带：不再把服务端的绝对目录结构漏给客户端
    assert str(get_config().data_dir) not in json.dumps(item, ensure_ascii=False)


def test_artifact_is_stored_relative(client: TestClient) -> None:
    r = client.post(f"{API}/cases", json={"module": "disclosure", "title": "交付物路径"})
    case_id = r.json()["id"]
    art = artifacts_service.save_artifact_sync(case_id, "disclosure_md", "# 稿", "md", title="稿")
    assert not Path(art.stored_path).is_absolute()
    assert art.stored_path.startswith("outputs/")
    assert paths_service.resolve_existing(art.stored_path) is not None


# ---------------------------------------------------------------------------
# 旧数据：端点必须仍然可用
# ---------------------------------------------------------------------------


@pytest.fixture
def case_with_foreign_absolute_rows(client: TestClient):
    """造一份「从别的机器恢复过来」的库：文件真的在，但库里记的是源机器的路径。"""
    r = client.post(f"{API}/cases", json={"module": "disclosure", "title": "换机恢复"})
    case_id = r.json()["id"]
    cfg = get_config()

    updir = cfg.uploads_dir / case_id
    updir.mkdir(parents=True, exist_ok=True)
    (updir / "图纸.png").write_bytes(PNG)
    (updir / "材料.md").write_text("# 旧机器上的材料", encoding="utf-8")

    outdir = cfg.outputs_dir / case_id
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "定稿.md").write_text("# 定稿正文", encoding="utf-8")

    old_root = r"D:\OldServer\PatentAgent\data"
    file_id, artifact_id = str(ULID()), str(ULID())
    db.execute(
        "INSERT INTO files(id, case_id, kind, orig_name, mime, size, stored_path, md_path,"
        " meta_json, created_at) VALUES (?,?,'upload','材料.md','text/markdown',10,?,?,'{}',?)",
        (file_id, case_id,
         rf"{old_root}\uploads\{case_id}\材料.md",
         rf"{old_root}\uploads\{case_id}\材料.md",
         db.now_str()),
    )
    db.execute(
        "INSERT INTO artifacts(id, case_id, version, kind, filename, stored_path, created_at)"
        " VALUES (?,?,1,'disclosure_md','定稿.md',?,?)",
        (artifact_id, case_id, rf"{old_root}\outputs\{case_id}\定稿.md", db.now_str()),
    )
    return case_id, file_id, artifact_id


def test_download_works_after_a_restore_to_another_path(
    client: TestClient, case_with_foreign_absolute_rows
) -> None:
    """下载：旧库里的外机绝对路径不能变成一条 404。"""
    _, file_id, artifact_id = case_with_foreign_absolute_rows

    r = client.get(f"{API}/files/{file_id}/download")
    assert r.status_code == 200, r.text

    r = client.get(f"{API}/artifacts/{artifact_id}/content")
    assert r.status_code == 200, r.text
    assert "定稿正文" in r.json()["content"]


def test_media_works_after_a_restore_to_another_path(
    client: TestClient, case_with_foreign_absolute_rows
) -> None:
    """正文插图：这条最容易被忽略，因为它坏了也不报错，只是图没了。"""
    case_id, _, _ = case_with_foreign_absolute_rows
    foreign = rf"D:\OldServer\PatentAgent\data\uploads\{case_id}\图纸.png"
    r = client.get(f"{API}/cases/{case_id}/media", params={"path": foreign})
    assert r.status_code == 200, r.text
    assert r.content == PNG


def test_normalize_rewrites_legacy_rows_and_is_idempotent(
    client: TestClient, case_with_foreign_absolute_rows
) -> None:
    """启动时的一次性归一：旧行改成相对形态，再跑一次不再改动。"""
    _, file_id, artifact_id = case_with_foreign_absolute_rows

    first = db.transaction(paths_service.normalize_stored_paths)
    assert first["rewritten"] >= 3, first

    row = db.query_one("SELECT stored_path, md_path FROM files WHERE id=?", (file_id,))
    assert not Path(row["stored_path"]).is_absolute()
    assert row["stored_path"].startswith("uploads/")
    art = db.query_one("SELECT stored_path FROM artifacts WHERE id=?", (artifact_id,))
    assert art["stored_path"].startswith("outputs/")

    second = db.transaction(paths_service.normalize_stored_paths)
    assert second["rewritten"] == 0, "归一必须幂等，否则每次启动都在写库"


def test_normalize_leaves_unrelatable_paths_alone(client: TestClient) -> None:
    """落不到数据目录内的路径不动——硬改只会把「可疑」变成「错得理直气壮」。"""
    r = client.post(f"{API}/cases", json={"module": "disclosure", "title": "无法归一"})
    case_id = r.json()["id"]
    file_id = str(ULID())
    weird = r"E:\SomewhereElse\notes.md"
    db.execute(
        "INSERT INTO files(id, case_id, kind, orig_name, mime, size, stored_path, md_path,"
        " meta_json, created_at) VALUES (?,?,'upload','notes.md','text/markdown',1,?,NULL,'{}',?)",
        (file_id, case_id, weird, db.now_str()),
    )
    stats = db.transaction(paths_service.normalize_stored_paths)
    assert stats["left_alone"] >= 1
    row = db.query_one("SELECT stored_path FROM files WHERE id=?", (file_id,))
    assert row["stored_path"] == weird


def test_data_dir_wins_over_a_still_existing_foreign_absolute_path(tmp_path) -> None:
    """同机恢复到另一个目录时，源目录还在——不能去读源目录。

    这条是恢复演练真踩出来的：把备份恢复到**同一台机器的另一个目录**做验证，
    库里那条旧的绝对路径仍然指向一个真实存在的文件。若按「绝对路径存在就用它」，
    恢复出来的实例会安静地去读源目录——**验证全绿，实际上根本没在用恢复的数据**。
    等源目录一删、或者源目录本身是份过期副本，问题才会以最难查的形式冒出来。

    所以不变式是：当前数据目录里的东西优先。
    """
    cfg = get_config()
    case_dir = cfg.uploads_dir / "SAMEHOST"
    case_dir.mkdir(parents=True, exist_ok=True)
    mine = case_dir / "figure.png"
    mine.write_bytes(PNG)

    # 「源目录」：同机的另一个位置，同样的相对结构，内容不同
    old_root = tmp_path / "old_data"
    old = old_root / "uploads" / "SAMEHOST" / "figure.png"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"STALE-COPY")
    assert old.exists(), "本用例的前提就是这条旧绝对路径仍然有效"

    got = paths_service.resolve(str(old))
    assert got == mine, f"应当用当前数据目录内的那份，实际取了 {got}"
    assert got.read_bytes() == PNG, "读到的是源目录里的过期副本"


def test_media_serves_the_restored_copy_not_the_source(
    client: TestClient, tmp_path
) -> None:
    """同一条规则要贯穿到正文取图：网页端不能显示源目录里的旧图。"""
    r = client.post(f"{API}/cases", json={"module": "disclosure", "title": "同机恢复"})
    case_id = r.json()["id"]
    cfg = get_config()
    updir = cfg.uploads_dir / case_id
    updir.mkdir(parents=True, exist_ok=True)
    (updir / "图.png").write_bytes(PNG)

    old = tmp_path / "old_data" / "uploads" / case_id / "图.png"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"STALE-COPY")

    got = client.get(f"{API}/cases/{case_id}/media", params={"path": str(old)})
    assert got.status_code == 200, got.text
    assert got.content == PNG, "取到的是源目录里的过期副本"
