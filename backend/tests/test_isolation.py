"""跨用户数据隔离穿透测试（M8 / auth-system.md §4，**本轮最关键的一份**）。

威胁：部署到公网后，用户 A 只要拿到用户 B 的 case_id / file_id / artifact_id
（截图、日志、URL 分享都可能泄露），就能把 B 的全部案件材料读走。

本文件的做法是「拿着 B 的真 id 去敲 A 的每一扇门」：
- 普通用户访问他人资源 → 一律 **404**（不是 403 —— 403 等于确认"这个 id 存在"，
  可用来枚举他人案件）；
- 管理员读他人案件 → 200，且必须留下 `cross_user_read` 审计；
- 管理员写他人案件 → 403（§4.2：可查看、不可篡改）；
- 普通用户访问 /admin/* 与平台级 /settings/* → 403。

新增按 case_id 取数的端点时，**必须在下面的用例表里补一行**。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.db import database as db
from app.services import artifacts as artifacts_service
from conftest import AuthedClient

API = "/api/v1"

#: 一个格式合法但必定不存在的 ULID（用于对比"不存在"与"不属于你"的响应一致性）
MISSING_ID = "01JZZZZZZZZZZZZZZZZZZZZZZZ"

#: 一份最小但合法的 PatentContent（PUT /p2p/content 的请求体校验先于属主校验，
#: 必须给合法体才能真正触达隔离层）
VALID_CONTENT = {
    "content": {
        "invention_name": "一种测试装置",
        "abstract": "本发明公开了一种测试装置。",
        "claims": ["一种测试装置，其特征在于，包括壳体与控制器。"],
        "description": {
            "technical_field": "本发明涉及测试领域。",
            "background": "现有技术存在不足。",
            "invention_content": "本发明提供一种测试装置。",
            "embodiments": "以下结合实施例说明。",
        },
    }
}


# ---------------------------------------------------------------------------
# 夹具：用户 A 的一整套资源
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def owned(client: AuthedClient) -> dict[str, str]:
    """用户 A 名下的案件 / 上传件 / 交付物 / 查新命中。"""
    case_id = client.post(
        f"{API}/cases", json={"module": "disclosure", "title": "隔离测试·A 的案件"}
    ).json()["id"]

    upload = client.post(
        f"{API}/cases/{case_id}/files",
        files=[("files", ("材料.md", b"# A private material\n\xe6\x9c\xba\xe5\xaf\x86", "text/markdown"))],
    )
    assert upload.status_code == 201, upload.text
    file_id = upload.json()[0]["file"]["id"]

    artifact = artifacts_service.save_artifact_sync(
        case_id, "disclosure_md", "# A 的定稿正文", "md", title="A 的定稿"
    )

    hit = client.post(
        f"{API}/cases/{case_id}/search/hits",
        json={"hits": [{"url": "https://epub.cnipa.gov.cn/patent/CN999999999A", "title": "在先文献"}]},
    )
    assert hit.status_code == 201, hit.text

    return {
        "case_id": case_id,
        "file_id": file_id,
        "artifact_id": artifact.id,
        "hit_id": hit.json()[0]["id"],
        "owner_id": client.user["id"],
    }


def _case_endpoints(case_id: str) -> list[tuple[str, str, dict[str, Any]]]:
    """所有按 case_id 取数 / 写入的端点（method, path, request kwargs）。"""
    base = f"{API}/cases/{case_id}"
    return [
        # 案件本体与会话
        ("GET", base, {}),
        ("PATCH", base, {"json": {"title": "被别人改名了"}}),
        ("DELETE", base, {}),
        ("GET", f"{base}/messages", {}),
        ("GET", f"{base}/events", {"params": {"last_event_id": "0"}}),
        # 文件
        ("POST", f"{base}/files",
         {"files": [("files", ("x.md", b"intruder", "text/markdown"))]}),
        # 交付物
        ("GET", f"{base}/artifacts", {}),
        # 正文插图（网页端 <img> 的取图口）
        ("GET", f"{base}/media", {"params": {"path": "figure_1.png"}}),
        # 流水线
        ("GET", f"{base}/pipeline/state", {}),
        ("POST", f"{base}/pipeline/start", {"json": {}}),
        ("POST", f"{base}/pipeline/input", {"json": {"step_key": "a1", "payload": {}}}),
        ("POST", f"{base}/pipeline/resume", {}),
        ("POST", f"{base}/pipeline/retry", {"json": {}}),
        ("POST", f"{base}/pipeline/cancel", {}),
        # 论文转专利
        ("GET", f"{base}/p2p/content", {}),
        ("PUT", f"{base}/p2p/content", {"json": VALID_CONTENT}),
        ("POST", f"{base}/p2p/drawings", {}),
        ("POST", f"{base}/p2p/build", {}),
        ("GET", f"{base}/p2p/image-prompts", {}),
        # 交底书
        ("POST", f"{base}/disclosure/iterate", {"json": {"mode": "auto", "instruction": ""}}),
        ("GET", f"{base}/disclosure/revisions", {}),
        # 专利解读
        ("GET", f"{base}/reader/report", {}),
        ("GET", f"{base}/reader/claim-tree", {}),
        # 查新
        ("POST", f"{base}/search/cnipa", {"json": {"terms": ["调度"]}}),
        ("GET", f"{base}/search/hits", {}),
        ("POST", f"{base}/search/hits",
         {"json": {"hits": [{"url": "https://epub.cnipa.gov.cn/patent/CN1A"}]}}),
        ("POST", f"{base}/search/skip", {"json": {"reason": "越权尝试"}}),
    ]


def _resource_endpoints(res: dict[str, str]) -> list[tuple[str, str, dict[str, Any]]]:
    """按 file_id / artifact_id / hit_id 取数的端点（隔离经其所属案件生效）。"""
    return [
        ("GET", f"{API}/files/{res['file_id']}/download", {}),
        ("GET", f"{API}/files/{res['file_id']}/content", {}),
        ("DELETE", f"{API}/files/{res['file_id']}", {}),
        ("GET", f"{API}/artifacts/{res['artifact_id']}/download", {}),
        ("GET", f"{API}/artifacts/{res['artifact_id']}/content", {}),
        ("POST", f"{API}/artifacts/{res['artifact_id']}/export", {"json": {"format": "docx"}}),
        ("PATCH", f"{API}/search/hits/{res['hit_id']}", {"json": {"selected": False}}),
    ]


# ---------------------------------------------------------------------------
# 用户 B 拿着 A 的真 id 敲门 —— 一律 404
# ---------------------------------------------------------------------------


def test_coverage_table_is_in_sync_with_openapi(raw_client) -> None:
    """守住纪律：新增按 case_id / 资源 id 取数的端点，必须补进上面的穿透用例表。

    否则新端点会悄悄绕过隔离层上线 —— 那正是本文件要防的事故。
    """
    spec = raw_client.get("/openapi.json").json()
    declared = {
        (method.upper(), path)
        for path, ops in spec["paths"].items()
        for method in ops
        if "{case_id}" in path
    }
    covered = {(m, p) for m, p, _ in _case_endpoints("{case_id}")}
    assert declared == covered, (
        f"未覆盖：{sorted(declared - covered)}；表里多余：{sorted(covered - declared)}"
    )

    resource_declared = {
        (method.upper(), path)
        for path, ops in spec["paths"].items()
        for method in ops
        if any(tok in path for tok in ("{file_id}", "{artifact_id}", "{hit_id}"))
    }
    resource_covered = {
        (m, p)
        for m, p, _ in _resource_endpoints(
            {"file_id": "{file_id}", "artifact_id": "{artifact_id}", "hit_id": "{hit_id}"}
        )
    }
    assert resource_declared == resource_covered, (
        f"未覆盖：{sorted(resource_declared - resource_covered)}；"
        f"表里多余：{sorted(resource_covered - resource_declared)}"
    )


def test_other_user_hits_every_case_endpoint_with_404(
    other_client: AuthedClient, owned: dict[str, str]
) -> None:
    failures = []
    for method, path, kwargs in _case_endpoints(owned["case_id"]):
        resp = other_client.request(method, path, **kwargs)
        if resp.status_code != 404:
            failures.append(f"{method} {path} → {resp.status_code} {resp.text[:160]}")
    assert not failures, "以下端点没有把越权访问拦成 404：\n" + "\n".join(failures)


def test_other_user_hits_every_resource_endpoint_with_404(
    other_client: AuthedClient, owned: dict[str, str]
) -> None:
    failures = []
    for method, path, kwargs in _resource_endpoints(owned):
        resp = other_client.request(method, path, **kwargs)
        if resp.status_code != 404:
            failures.append(f"{method} {path} → {resp.status_code} {resp.text[:160]}")
    assert not failures, "以下端点没有把越权访问拦成 404：\n" + "\n".join(failures)


def test_nonexistent_and_others_ids_are_indistinguishable(
    other_client: AuthedClient, owned: dict[str, str]
) -> None:
    """「不存在」与「存在但不属于你」必须返回同样的状态码，否则可用来枚举他人 id。"""
    theirs = other_client.get(f"{API}/cases/{owned['case_id']}")
    missing = other_client.get(f"{API}/cases/{MISSING_ID}")
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json()["detail"] == missing.json()["detail"]

    theirs_file = other_client.get(f"{API}/files/{owned['file_id']}/content")
    missing_file = other_client.get(f"{API}/files/{MISSING_ID}/content")
    assert theirs_file.status_code == missing_file.status_code == 404
    assert theirs_file.json()["detail"] == missing_file.json()["detail"]


def test_none_of_the_probing_actually_mutated_a_thing(
    client: AuthedClient, owned: dict[str, str]
) -> None:
    """穿透尝试跑完之后，A 的案件、文件、交付物一个都不能少、一个都不能变。"""
    detail = client.get(f"{API}/cases/{owned['case_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["title"] == "隔离测试·A 的案件"      # 没被 B 改名
    assert detail.json()["status"] != "archived"

    files = db.query_all("SELECT id FROM files WHERE case_id=?", (owned["case_id"],))
    assert [r["id"] for r in files] == [owned["file_id"]]      # 既没被删、也没多出 B 传的
    assert client.get(f"{API}/files/{owned['file_id']}/content").status_code == 200
    assert client.get(f"{API}/artifacts/{owned['artifact_id']}/content").status_code == 200
    hits = client.get(f"{API}/cases/{owned['case_id']}/search/hits").json()
    assert hits["hits"][0]["selected"] is True                 # B 的 PATCH 没生效


def test_case_list_never_leaks_other_users_cases(
    other_client: AuthedClient, owned: dict[str, str]
) -> None:
    page = other_client.get(f"{API}/cases", params={"limit": 200}).json()
    assert owned["case_id"] not in {c["id"] for c in page["items"]}
    # 关键词、模块过滤同样不能成为旁路
    for params in (
        {"q": "隔离测试"},
        {"module": "disclosure", "limit": 200},
        {"status": "draft", "limit": 200},
    ):
        listed = other_client.get(f"{API}/cases", params=params).json()
        assert owned["case_id"] not in {c["id"] for c in listed["items"]}


# ---------------------------------------------------------------------------
# 管理员：可读、留痕、不可写
# ---------------------------------------------------------------------------


def test_admin_can_read_others_case_and_it_is_audited(
    admin_client: AuthedClient, owned: dict[str, str]
) -> None:
    resp = admin_client.get(f"{API}/cases/{owned['case_id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "隔离测试·A 的案件"

    entries = admin_client.get(
        f"{API}/admin/audit", params={"action": "cross_user_read", "limit": 200}
    ).json()["items"]
    mine = [e for e in entries if e["target_id"] == owned["case_id"]]
    assert mine, "管理员跨用户读没有写审计 —— 无法追责就等于没有权限管控"
    entry = mine[0]
    assert entry["target_type"] == "case"
    assert entry["target_owner"] == owned["owner_id"]
    assert entry["actor_id"] and entry["actor_id"] != owned["owner_id"]
    assert entry["actor_name"] == admin_client.user["username"]

    # 管理员默认（scope=mine）只列自己的案件——避免用户一多就把侧栏淹没
    mine = admin_client.get(f"{API}/cases", params={"limit": 200}).json()
    assert owned["case_id"] not in {c["id"] for c in mine["items"]}

    # 显式 scope=all 才列出全平台案件（管理后台用）
    page = admin_client.get(f"{API}/cases", params={"limit": 200, "scope": "all"}).json()
    assert owned["case_id"] in {c["id"] for c in page["items"]}
    # 列表项带属主 id，便于管理后台区分归属（用户名只在详情接口返回）
    listed = next(c for c in page["items"] if c["id"] == owned["case_id"])
    assert listed["user_id"] == owned["owner_id"]


def test_normal_user_cannot_use_scope_all(
    client: AuthedClient, other_client: AuthedClient, owned: dict[str, str]
) -> None:
    """普通用户即便显式传 scope=all 也只能看到自己的案件（后端忽略该参数）。"""
    page = other_client.get(f"{API}/cases", params={"limit": 200, "scope": "all"}).json()
    assert owned["case_id"] not in {c["id"] for c in page["items"]}


def test_admin_cannot_modify_others_case(
    admin_client: AuthedClient, client: AuthedClient, owned: dict[str, str]
) -> None:
    """§4.2：管理员对他人案件是**只读**的，写操作一律 403。"""
    writes = [
        ("PATCH", f"{API}/cases/{owned['case_id']}", {"json": {"title": "管理员改的名"}}),
        ("POST", f"{API}/cases/{owned['case_id']}/files",
         {"files": [("files", ("admin.md", b"x", "text/markdown"))]}),
        ("POST", f"{API}/cases/{owned['case_id']}/pipeline/start", {"json": {}}),
        ("POST", f"{API}/cases/{owned['case_id']}/pipeline/cancel", {}),
        ("PUT", f"{API}/cases/{owned['case_id']}/p2p/content", {"json": VALID_CONTENT}),
        ("POST", f"{API}/cases/{owned['case_id']}/search/skip", {"json": {"reason": "x"}}),
        ("DELETE", f"{API}/files/{owned['file_id']}", {}),
        ("POST", f"{API}/artifacts/{owned['artifact_id']}/export", {"json": {"format": "docx"}}),
    ]
    failures = []
    for method, path, kwargs in writes:
        resp = admin_client.request(method, path, **kwargs)
        if resp.status_code != 403:
            failures.append(f"{method} {path} → {resp.status_code} {resp.text[:160]}")
    assert not failures, "管理员对他人案件的写操作没有被拦成 403：\n" + "\n".join(failures)

    # 确认真的没改动
    assert client.get(f"{API}/cases/{owned['case_id']}").json()["title"] == "隔离测试·A 的案件"


# ---------------------------------------------------------------------------
# 平台级设置：普通用户读写皆 403
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", f"{API}/settings/llm", None),
        ("PUT", f"{API}/settings/llm", {"model": "sneaky"}),
        ("POST", f"{API}/settings/llm/test", {}),
        ("GET", f"{API}/settings/embedding", None),
        ("PUT", f"{API}/settings/embedding", {"dim": 8}),
        ("POST", f"{API}/settings/embedding/reindex", {}),
        ("GET", f"{API}/settings/image-gen", None),
        ("PUT", f"{API}/settings/image-gen", {"model": "sneaky"}),
        ("PUT", f"{API}/settings/general", {}),
        ("POST", f"{API}/oa/library/rebuild", {}),
    ],
)
def test_normal_user_cannot_touch_platform_settings(
    client: AuthedClient, method: str, path: str, payload: dict | None
) -> None:
    kwargs = {} if payload is None else {"json": payload}
    resp = client.request(method, path, **kwargs)
    assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"
    assert "管理员" in resp.json()["detail"]


def test_normal_user_can_still_read_general_settings(client: AuthedClient) -> None:
    """外观等通用设置对全员可读（前端设置页的"外观"分区要用）。"""
    resp = client.get(f"{API}/settings/general")
    assert resp.status_code == 200, resp.text
    assert "language" in resp.json()


def test_platform_api_key_never_reaches_a_normal_user(
    admin_client: AuthedClient, client: AuthedClient
) -> None:
    """平台 API Key 只存服务端，任何接口都不得把明文回给普通用户。"""
    secret = "sk-platformsecret0123456789"
    assert admin_client.put(
        f"{API}/settings/llm",
        json={"base_url": "https://api.example.com/v1", "api_key": secret, "model": "m"},
    ).status_code == 200
    try:
        for path in (f"{API}/settings/llm", f"{API}/system/env", f"{API}/auth/me"):
            resp = client.get(path)
            assert secret not in resp.text, path
        # 掩码后的值管理员自己也只看到掩码
        assert admin_client.get(f"{API}/settings/llm").json()["api_key"] == "sk-***6789"
    finally:
        db.execute("DELETE FROM settings WHERE key='llm'")


# ---------------------------------------------------------------------------
# OA 案例库：属主隔离 + shared 全员可见
# ---------------------------------------------------------------------------


def test_oa_library_is_scoped_to_owner_and_shared_is_visible_to_all(
    client: AuthedClient, other_client: AuthedClient, admin_client: AuthedClient
) -> None:
    import json

    from ulid import ULID

    library_id = str(ULID())
    db.execute(
        "INSERT INTO oa_library(id, case_note_md, frontmatter_json, status, embedded,"
        " user_id, visibility, created_at, updated_at) VALUES (?,?,?,'draft',0,?,'private',?,?)",
        (
            library_id,
            "# A 的案例笔记",
            json.dumps({"title": "A 的私有案例", "tags": ["iso"]}, ensure_ascii=False),
            client.user["id"],
            db.now_str(),
            db.now_str(),
        ),
    )
    try:
        # 属主与管理员可见
        assert client.get(f"{API}/oa/library/{library_id}").status_code == 200
        assert admin_client.get(f"{API}/oa/library/{library_id}").status_code == 200
        # 他人不可见：详情 404、列表里也没有
        assert other_client.get(f"{API}/oa/library/{library_id}").status_code == 404
        listed = other_client.get(f"{API}/oa/library", params={"limit": 200}).json()
        assert library_id not in {i["id"] for i in listed["items"]}
        # 他人不能改、不能删
        assert other_client.put(
            f"{API}/oa/library/{library_id}", json={"note_md": "被篡改"}
        ).status_code == 404
        assert other_client.delete(f"{API}/oa/library/{library_id}").status_code == 404

        # 标为 shared 后对全员可见
        db.execute(
            "UPDATE oa_library SET visibility='shared' WHERE id=?", (library_id,)
        )
        assert other_client.get(f"{API}/oa/library/{library_id}").status_code == 200
        shared_list = other_client.get(f"{API}/oa/library", params={"limit": 200}).json()
        assert library_id in {i["id"] for i in shared_list["items"]}
    finally:
        db.execute("DELETE FROM oa_library WHERE id=?", (library_id,))


def test_oa_library_search_only_returns_visible_cases(
    client: AuthedClient, other_client: AuthedClient
) -> None:
    """检索是最容易漏的旁路：即便详情接口拦住了，检索命中也会把正文带出去。"""
    import json

    from ulid import ULID

    library_id = str(ULID())
    note = "# 隔离检索案例\n本案涉及独一无二的关键词：ZebraQuokkaIsolation。"
    db.execute(
        "INSERT INTO oa_library(id, case_note_md, frontmatter_json, status, embedded,"
        " user_id, visibility, created_at, updated_at)"
        " VALUES (?,?,?,'confirmed',0,?,'private',?,?)",
        (
            library_id,
            note,
            json.dumps({"title": "隔离检索案例"}, ensure_ascii=False),
            client.user["id"],
            db.now_str(),
            db.now_str(),
        ),
    )
    db.execute(
        "INSERT INTO oa_chunks(library_id, chunk_index, text) VALUES (?,0,?)",
        (library_id, note),
    )
    try:
        mine = client.get(
            f"{API}/oa/library/search", params={"q": "ZebraQuokkaIsolation", "k": 10}
        ).json()
        assert library_id in {h["library_id"] for h in mine["hits"]}

        theirs = other_client.get(
            f"{API}/oa/library/search", params={"q": "ZebraQuokkaIsolation", "k": 10}
        ).json()
        assert library_id not in {h["library_id"] for h in theirs["hits"]}
        assert "ZebraQuokkaIsolation" not in str(theirs["hits"])
    finally:
        db.execute("DELETE FROM oa_chunks WHERE library_id=?", (library_id,))
        db.execute("DELETE FROM oa_library WHERE id=?", (library_id,))
