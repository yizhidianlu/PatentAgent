"""后端 API 契约与边界回归测试（3 号 tester）。

覆盖 openapi.json 里全部 60 个操作的错误路径与边界，重点锁死本轮修掉的四个缺陷：

| # | 端点 | 缺陷 | 现在的契约 |
|---|---|---|---|
| 1 | `PUT /settings/llm`、`PUT /settings/embedding` | temperature / context_window / max_output_tokens / dim 越界照单全收，等到跑流水线时才在**每一次**调用上炸 | 越界 → 422；库里已有的历史越界值读取时回退默认值，GET 不再 500 |
| 2 | `POST /cases/{id}/disclosure/iterate` | `file_ids` 不校验归属，传别的案件（或不存在）的 id 会**静默回落到本案全部上传件**当新材料重跑 | 不属于本案 / 不存在 → 404 |
| 3 | `GET /cases/{id}/p2p/content` | 交付物 JSON 损坏 → `json.JSONDecodeError` 逃逸成 500 | → 422 并指明是哪一版坏了 |
| 4 | `GET /cases/{id}/p2p/content` | 工作内存里的半成品 content → `ValidationError` 逃逸成 500 | → 422 并列出缺失字段 |

**本文件绝不触发真实 LLM / Embedding / 图像调用**：`_no_llm` 是 autouse 保险丝，
把 `services/llm._client` 换成会直接抛异常的桩，任何漏网的出网调用都会当场失败而
不是偷偷打到用户的 Moonshot 配额上（实测 RPM=3）。所有涉及 LLM 的端点只测在真正
发请求**之前**就该返回的 4xx。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

from conftest import disk_path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import get_config
from app.db import database as db
from app.pipelines import registry
from app.services import artifacts as artifacts_service

API = "/api/v1"
#: 一个格式合法但必定不存在的 ULID
MISSING_ID = "01JZZZZZZZZZZZZZZZZZZZZZZZ"
#: 把 demo 三步流水线临时挂到这个真实模块名下（cases.module 有 CHECK 约束）
DEMO_MODULE = "reader"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------



@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """保险丝：任何真实出网的 LLM/Embedding/图像调用都当场炸掉。

    用户账户 RPM=3 且被别的测试占用；本文件的用例全部只走「发请求之前就该拦下」
    的路径，真打出去说明有回归。
    """
    from app.services import llm as llm_service

    def _boom(*_args: Any, **_kwargs: Any):
        raise AssertionError("test_api_edge 不允许发起任何真实 LLM 调用")

    monkeypatch.setattr(llm_service, "_client", _boom)


@pytest.fixture(scope="module", autouse=True)
def _demo_pipeline():
    """demo 三步流水线临时顶替 reader 模块，用完还原注册表。"""
    previous = registry.get_builder(DEMO_MODULE)
    registry.register(DEMO_MODULE, registry.build_demo_steps)
    yield
    if previous is None:  # pragma: no cover —— reader 恒已注册
        registry._builders.pop(DEMO_MODULE, None)
    else:
        registry.register(DEMO_MODULE, previous)


@pytest.fixture
def restore_settings():
    """保存/还原 settings 表，避免污染同一 DB 上的其它测试模块。"""
    keys = ("llm", "embedding", "image_gen", "general")
    saved = {k: db.get_setting_json(k) for k in keys}
    yield
    for key, value in saved.items():
        if value is None:
            db.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            db.set_setting_json(key, value)


def _new_case(client: TestClient, title: str, module: str = "disclosure") -> str:
    resp = client.post(f"{API}/cases", json={"module": module, "title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _upload(client: TestClient, case_id: str, name: str, payload: bytes,
            mime: str = "text/markdown") -> dict[str, Any]:
    resp = client.post(f"{API}/cases/{case_id}/files", files=[("files", (name, payload, mime))])
    assert resp.status_code == 201, resp.text
    return resp.json()[0]


def _wait_state(client: TestClient, case_id: str, pred: Callable[[dict], bool],
                timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.get(f"{API}/cases/{case_id}/pipeline/state").json()
        if pred(last):
            return last
        time.sleep(0.03)
    raise AssertionError(f"等待流水线状态超时；最后一次 state={last}")


# ===========================================================================
# 缺陷 1：设置端点的数值范围校验
# ===========================================================================


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", 2.5),        # 超上界
        ("temperature", -0.1),       # 负温度
        ("temperature", 999),
        ("context_window", 0),       # 预算器会算出负预算
        ("context_window", -1),
        ("max_output_tokens", 0),    # max_tokens=0 → 端点每次都 400
        ("max_output_tokens", -5),
    ],
)
def test_llm_settings_reject_out_of_range(
    admin_client: TestClient, restore_settings: None, field: str, value: float
) -> None:
    """越界的能力位必须在保存时就 422，而不是等跑流水线时每次调用都失败。"""
    resp = admin_client.put(f"{API}/settings/llm", json={"model": "m", field: value})
    assert resp.status_code == 422, resp.text
    assert any(err["loc"][-1] == field for err in resp.json()["detail"])


@pytest.mark.parametrize("value", [0, -1, -1024])
def test_embedding_settings_reject_non_positive_dim(
    admin_client: TestClient, restore_settings: None, value: int
) -> None:
    """dim<=0 会让 vec0 虚拟表建表直接失败，必须拦在保存这一步。"""
    resp = admin_client.put(f"{API}/settings/embedding", json={"dim": value})
    assert resp.status_code == 422, resp.text
    assert any(err["loc"][-1] == "dim" for err in resp.json()["detail"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", 0.0),
        ("temperature", 1.0),
        ("temperature", 2.0),        # 上界含端点
        ("context_window", 1),
        ("max_output_tokens", 1),
    ],
)
def test_llm_settings_accept_boundary_values(
    admin_client: TestClient, restore_settings: None, field: str, value: float
) -> None:
    """边界内的值不得误伤（尤其 temperature=1，推理型模型只吃这个值）。"""
    resp = admin_client.put(f"{API}/settings/llm", json={"model": "m", field: value})
    assert resp.status_code == 200, resp.text
    assert resp.json()[field] == value


def test_embedding_settings_accept_positive_dim(admin_client: TestClient, restore_settings: None) -> None:
    resp = admin_client.put(f"{API}/settings/embedding", json={"dim": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["dim"] == 1


def test_llm_test_endpoint_validates_before_calling(
    admin_client: TestClient, restore_settings: None
) -> None:
    """连接测试端点的 temperature 同样受限，且 422 发生在建客户端之前。

    `_no_llm` 已把 `_client` 换成会抛的桩：这里能拿到 422 就证明请求根本没发出去。
    """
    resp = admin_client.post(f"{API}/settings/llm/test", json={"temperature": 99})
    assert resp.status_code == 422, resp.text
    assert any(err["loc"][-1] == "temperature" for err in resp.json()["detail"])


def test_legacy_out_of_range_settings_still_readable(
    admin_client: TestClient, restore_settings: None
) -> None:
    """范围校验是后加的：库里躺着的历史越界值必须回退默认值，读取绝不 500。

    否则加完校验反而把老用户的设置页整个打不开（GET /settings/llm → 500），
    连带 load_llm_settings() 一挂，四个模块的流水线全线不可用。
    """
    db.set_setting_json(
        "llm",
        {
            "model": "legacy-model",
            "api_key": "sk-legacyrealkey1234",
            "temperature": 42,          # 越界
            "context_window": 0,        # 越界
            "max_output_tokens": -1,    # 越界
            "base_url": "https://api.example.com/v1",
        },
    )

    resp = admin_client.get(f"{API}/settings/llm")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 合法字段原样保留
    assert body["model"] == "legacy-model"
    assert body["base_url"] == "https://api.example.com/v1"
    # 越界字段回退默认值
    assert body["temperature"] == 0.2
    assert body["context_window"] == 131072
    assert body["max_output_tokens"] == 4096
    # key 仍在（且仍是掩码，明文永不回显）
    assert body["api_key"] == "sk-***1234"
    assert "sk-legacyrealkey1234" not in resp.text

    # 服务层同一口径，不抛异常
    from app.services import llm as llm_service

    cfg = llm_service.load_llm_settings()
    assert cfg.model == "legacy-model"
    assert cfg.temperature == 0.2 and cfg.context_window == 131072


def test_masked_key_roundtrip_is_not_stored_as_key(
    admin_client: TestClient, restore_settings: None
) -> None:
    """前端把 GET 到的掩码原样 PUT 回来是常态，不能把 `sk-***1234` 当成真 key 存进去。"""
    secret = "sk-roundtriprealkey987654"
    admin_client.put(f"{API}/settings/llm", json={"model": "m1", "api_key": secret})
    masked = admin_client.get(f"{API}/settings/llm").json()["api_key"]

    resp = admin_client.put(f"{API}/settings/llm", json={"model": "m2", "api_key": masked})
    assert resp.status_code == 200, resp.text
    assert admin_client.get(f"{API}/settings/llm").json()["api_key"] == masked
    # 明文确实还在库里（没被掩码顶掉）
    assert db.get_setting_json("llm")["api_key"] == secret


# ===========================================================================
# 缺陷 2：disclosure/iterate 的 file_ids 归属校验（越权 + 静默串料）
# ===========================================================================


@pytest.fixture
def delivered_case(client: TestClient) -> str:
    """一个「已交付定稿」的交底书案件（可进入迭代）。"""
    case_id = _new_case(client, "已交付的交底书案件")
    artifacts_service.save_artifact_sync(case_id, "disclosure_md", "# 定稿正文", "md", title="定稿")
    return case_id


def test_iterate_rejects_file_id_from_another_case(
    client: TestClient, delivered_case: str
) -> None:
    """A 案件的 file_id 用在 B 案件必须 404。

    不拦的话 `_material_rows_sync` 过滤不到任何行，会**静默回落到 B 案件的全部
    上传件**——用户以为只并入了新材料，实际把全部旧材料又当新材料重跑一遍。
    """
    other = _new_case(client, "另一个案件")
    foreign = _upload(client, other, "别人的材料.md", "# 不属于本案".encode())["file"]["id"]

    resp = client.post(
        f"{API}/cases/{delivered_case}/disclosure/iterate",
        json={"mode": "merge", "instruction": "并入", "file_ids": [foreign]},
    )
    assert resp.status_code == 404, resp.text
    assert foreign in resp.json()["detail"]
    # 没有任何流水线被启动
    assert client.get(f"{API}/cases/{delivered_case}/pipeline/state").json()["run_group"] == "initial"


def test_iterate_rejects_unknown_file_id(client: TestClient, delivered_case: str) -> None:
    resp = client.post(
        f"{API}/cases/{delivered_case}/disclosure/iterate",
        json={"mode": "merge", "file_ids": [MISSING_ID]},
    )
    assert resp.status_code == 404, resp.text
    assert MISSING_ID in resp.json()["detail"]


def test_iterate_rejects_mixed_valid_and_foreign_file_ids(
    client: TestClient, delivered_case: str
) -> None:
    """一半合法一半越权也必须整体拒绝（不能只挑合法的悄悄跑）。"""
    mine = _upload(client, delivered_case, "本案材料.md", "# 本案".encode())["file"]["id"]
    other = _new_case(client, "又一个案件")
    foreign = _upload(client, other, "外来.md", "# 外来".encode())["file"]["id"]

    resp = client.post(
        f"{API}/cases/{delivered_case}/disclosure/iterate",
        json={"mode": "merge", "file_ids": [mine, foreign]},
    )
    assert resp.status_code == 404, resp.text
    assert foreign in resp.json()["detail"] and mine not in resp.json()["detail"]


def test_iterate_file_id_check_runs_before_delivery_check(client: TestClient) -> None:
    """本案自己的 file_id 通过归属校验后，才落到「尚未交付」的 409。"""
    case_id = _new_case(client, "未交付的交底书案件")
    mine = _upload(client, case_id, "材料.md", "# 本案材料".encode())["file"]["id"]

    resp = client.post(
        f"{API}/cases/{case_id}/disclosure/iterate",
        json={"mode": "merge", "file_ids": [mine]},
    )
    assert resp.status_code == 409, resp.text
    assert "尚未产出交底书定稿" in resp.json()["detail"]


def test_iterate_non_disclosure_case_returns_400(client: TestClient) -> None:
    case_id = _new_case(client, "解读案件", module=DEMO_MODULE)
    resp = client.post(f"{API}/cases/{case_id}/disclosure/iterate", json={"mode": "auto"})
    assert resp.status_code == 400
    assert "不是交底书案件" in resp.json()["detail"]


def test_iterate_rejects_invalid_mode(client: TestClient, delivered_case: str) -> None:
    resp = client.post(f"{API}/cases/{delivered_case}/disclosure/iterate", json={"mode": "nope"})
    assert resp.status_code == 422


# ===========================================================================
# 缺陷 3 / 4：p2p/content 的两条 500 路径
# ===========================================================================


def _p2p_content() -> dict[str, Any]:
    """一份合法的 PatentContent（满足 pydantic 必填约束）。"""
    return {
        "invention_name": "一种医疗导管装置",
        "abstract": "一种可弯曲的医疗导管装置。",
        "claims": ["1. 一种医疗导管装置，其特征在于，包括导管本体。"],
        "description": {
            "technical_field": "本发明涉及医疗器械领域。",
            "background": "现有技术存在不足。",
            "invention_content": "本发明提供一种改进方案。",
            "drawing_description": ["图1为结构示意图。"],
            "embodiments": "下面结合附图说明具体实施方式。",
        },
    }


def _set_state(case_id: str, state: dict[str, Any]) -> None:
    db.execute(
        "UPDATE cases SET state_json=? WHERE id=?",
        (json.dumps(state, ensure_ascii=False), case_id),
    )


def test_p2p_content_corrupt_artifact_returns_422(client: TestClient) -> None:
    """落盘的 patent_content_json 被改坏/截断时报 422，而不是 500。"""
    case_id = _new_case(client, "坏契约", module="paper2patent")
    artifacts_service.save_artifact_sync(
        case_id, "patent_content_json", "{ 这不是合法 JSON", "json", title="坏契约"
    )

    resp = client.get(f"{API}/cases/{case_id}/p2p/content")
    assert resp.status_code == 422, resp.text
    assert "损坏" in resp.json()["detail"]


def test_p2p_content_non_object_artifact_returns_422(client: TestClient) -> None:
    """合法 JSON 但不是对象（比如写成了数组）同样 422。"""
    case_id = _new_case(client, "数组契约", module="paper2patent")
    artifacts_service.save_artifact_sync(
        case_id, "patent_content_json", "[1, 2, 3]", "json", title="数组契约"
    )

    resp = client.get(f"{API}/cases/{case_id}/p2p/content")
    assert resp.status_code == 422, resp.text
    assert "JSON 对象" in resp.json()["detail"]


def test_p2p_content_incomplete_state_returns_422(client: TestClient) -> None:
    """工作内存里的半成品 content（流水线中途取消/失败留下的）→ 422 并列出缺字段。"""
    case_id = _new_case(client, "半成品", module="paper2patent")
    _set_state(case_id, {"content": {"invention_name": "半成品", "abstract": "x", "claims": ["1. x"]}})

    resp = client.get(f"{API}/cases/{case_id}/p2p/content")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "description" in detail["invalid_fields"]


def test_p2p_content_happy_path_and_versioning(client: TestClient) -> None:
    """合法 content：GET 200；PUT 每次落一个新版本（只增不改）。"""
    case_id = _new_case(client, "正常契约", module="paper2patent")
    _set_state(case_id, {"content": _p2p_content()})

    got = client.get(f"{API}/cases/{case_id}/p2p/content")
    assert got.status_code == 200, got.text
    assert got.json()["content"]["invention_name"] == "一种医疗导管装置"

    first = client.put(f"{API}/cases/{case_id}/p2p/content", json={"content": _p2p_content()})
    second = client.put(f"{API}/cases/{case_id}/p2p/content", json={"content": _p2p_content()})
    assert first.status_code == second.status_code == 200, second.text
    assert second.json()["version"] == first.json()["version"] + 1


def test_p2p_put_content_rejects_empty_claims(client: TestClient) -> None:
    case_id = _new_case(client, "空权项", module="paper2patent")
    resp = client.put(
        f"{API}/cases/{case_id}/p2p/content",
        json={"content": dict(_p2p_content(), claims=[])},
    )
    assert resp.status_code == 422, resp.text


def test_p2p_endpoints_404_before_content_exists(client: TestClient) -> None:
    case_id = _new_case(client, "空案件", module="paper2patent")
    for method, path in [
        ("get", "content"),
        ("get", "image-prompts"),
        ("post", "build"),
        ("post", "drawings"),
    ]:
        resp = getattr(client, method)(f"{API}/cases/{case_id}/p2p/{path}")
        assert resp.status_code == 404, f"{path} → {resp.status_code} {resp.text[:160]}"


def test_p2p_endpoints_reject_wrong_module(client: TestClient) -> None:
    """交底书案件访问 p2p 端点 → 400（模块错配，不是 404 也不是 500）。"""
    case_id = _new_case(client, "交底书案件")
    assert client.get(f"{API}/cases/{case_id}/p2p/content").status_code == 400
    assert client.get(f"{API}/cases/{case_id}/reader/report").status_code == 400


# ===========================================================================
# 不存在的 id：一律 404，绝不 500
# ===========================================================================


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", f"/cases/{MISSING_ID}", {}),
        ("patch", f"/cases/{MISSING_ID}", {"json": {"title": "x"}}),
        ("delete", f"/cases/{MISSING_ID}", {}),
        ("get", f"/cases/{MISSING_ID}/messages", {}),
        ("get", f"/cases/{MISSING_ID}/artifacts", {}),
        ("post", f"/cases/{MISSING_ID}/files",
         {"files": [("files", ("a.txt", b"hi", "text/plain"))]}),
        ("get", f"/cases/{MISSING_ID}/events", {}),
        ("post", f"/cases/{MISSING_ID}/pipeline/start", {"json": {}}),
        ("get", f"/cases/{MISSING_ID}/pipeline/state", {}),
        ("post", f"/cases/{MISSING_ID}/pipeline/input",
         {"json": {"step_key": "x", "payload": {}}}),
        ("post", f"/cases/{MISSING_ID}/pipeline/resume", {}),
        ("post", f"/cases/{MISSING_ID}/pipeline/retry", {}),
        ("post", f"/cases/{MISSING_ID}/pipeline/cancel", {}),
        ("post", f"/cases/{MISSING_ID}/disclosure/iterate", {"json": {"mode": "auto"}}),
        ("get", f"/cases/{MISSING_ID}/disclosure/revisions", {}),
        ("get", f"/cases/{MISSING_ID}/reader/report", {}),
        ("get", f"/cases/{MISSING_ID}/reader/claim-tree", {}),
        ("get", f"/cases/{MISSING_ID}/p2p/content", {}),
        ("post", f"/cases/{MISSING_ID}/p2p/drawings", {}),
        ("post", f"/cases/{MISSING_ID}/p2p/build", {}),
        ("get", f"/cases/{MISSING_ID}/p2p/image-prompts", {}),
        ("get", f"/cases/{MISSING_ID}/search/hits", {}),
        ("post", f"/cases/{MISSING_ID}/search/hits",
         {"json": {"hits": [{"url": "https://example.com/1"}]}}),
        ("post", f"/cases/{MISSING_ID}/search/skip", {"json": {"reason": "x"}}),
        ("get", f"/files/{MISSING_ID}/download", {}),
        ("get", f"/files/{MISSING_ID}/content", {}),
        ("delete", f"/files/{MISSING_ID}", {}),
        ("get", f"/artifacts/{MISSING_ID}/download", {}),
        ("get", f"/artifacts/{MISSING_ID}/content", {}),
        ("post", f"/artifacts/{MISSING_ID}/export", {"json": {"format": "docx"}}),
        ("patch", f"/search/hits/{MISSING_ID}", {"json": {"selected": True}}),
        ("get", f"/oa/library/{MISSING_ID}", {}),
        ("put", f"/oa/library/{MISSING_ID}", {"json": {"status": "confirmed"}}),
        ("delete", f"/oa/library/{MISSING_ID}", {}),
        ("post", "/oa/library/ingest", {"json": {"file_id": MISSING_ID}}),
    ],
)
def test_unknown_ids_return_404(
    client: TestClient, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    resp = getattr(client, method)(API + path, **kwargs)
    assert resp.status_code == 404, f"{method.upper()} {path} → {resp.status_code} {resp.text[:200]}"


def test_missing_case_is_404_even_for_pipeline_state(client: TestClient) -> None:
    """流水线状态端点会读注册表构步骤表，也不能因为案件不存在而 500。"""
    resp = client.get(f"{API}/cases/{MISSING_ID}/pipeline/state")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


# ===========================================================================
# 文件端点边界
# ===========================================================================


@pytest.mark.parametrize(
    ("filename", "expect_basename"),
    [
        ("../../../etc/passwd", "passwd"),
        ("..\\..\\..\\Windows\\System32\\evil.dll", "evil.dll"),
        ("....//....//escape.txt", "escape.txt"),
        ("/absolute/path/file.txt", "file.txt"),
        ("C:\\Windows\\win.ini", "win.ini"),
    ],
)
def test_upload_filename_path_traversal_is_contained(
    client: TestClient, filename: str, expect_basename: str
) -> None:
    """带路径分隔符的文件名只取 basename，落盘必须仍在 uploads/{case_id}/ 之内。"""
    case_id = _new_case(client, "路径穿越")
    item = _upload(client, case_id, filename, b"payload", mime="text/plain")

    assert item["file"]["orig_name"] == expect_basename
    # 库里存的是相对 DATA_DIR 的路径（换机可搬），落实成绝对路径走的是生产同一条通路
    from app.services import paths as paths_service

    raw = item["file"]["stored_path"]
    assert not Path(raw).is_absolute(), f"stored_path 不该再是绝对路径：{raw}"
    stored = paths_service.resolve(raw).resolve()
    case_dir = (get_config().uploads_dir / case_id).resolve()
    assert stored.is_relative_to(case_dir), f"{stored} 逃出了 {case_dir}"
    assert stored.read_bytes() == b"payload"


@pytest.mark.parametrize("filename", ["...", "..", "///", "<>:|?*"])
def test_upload_degenerate_filename_falls_back_to_unnamed(
    client: TestClient, filename: str
) -> None:
    """清洗后为空的文件名回落 `unnamed`，不得建出空名文件或抛异常。"""
    case_id = _new_case(client, "怪文件名")
    item = _upload(client, case_id, filename, b"x", mime="text/plain")
    assert item["file"]["orig_name"] == "unnamed"
    assert disk_path(item["file"]["stored_path"]).is_file()


def test_upload_strips_windows_illegal_chars(client: TestClient) -> None:
    """Windows 非法字符被剔除，但正常部分保留（不得整体丢名）。"""
    case_id = _new_case(client, "非法字符")
    item = _upload(client, case_id, "报告<v2>:草稿|终稿?.md", b"# x")
    assert item["file"]["orig_name"] == "报告v2草稿终稿.md"
    assert disk_path(item["file"]["stored_path"]).is_file()


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("../../../etc/passwd", "passwd"),
        ("..\\..\\evil.dll", "evil.dll"),
        ("\r\n\t", "unnamed"),           # 控制字符（httpx 会转义，故在单元层直测）
        ('a"b<c>d.txt', "abcd.txt"),
        ("", "unnamed"),
        ("   .  ", "unnamed"),
    ],
)
def test_sanitize_filename_unit(raw: str, expect: str) -> None:
    """落盘前的文件名清洗（路径分隔符 / 控制字符 / 非法字符 / 空名）。"""
    from app.services.convert import sanitize_filename

    assert sanitize_filename(raw) == expect


def test_upload_chinese_and_special_filename(client: TestClient) -> None:
    case_id = _new_case(client, "中文名")
    item = _upload(client, case_id, "交底书（第 2 版）—— 王医生.md", "# 中文正文".encode())
    assert item["file"]["orig_name"] == "交底书（第 2 版）—— 王医生.md"
    assert disk_path(item["file"]["stored_path"]).is_file()

    got = client.get(f"{API}/files/{item['file']['id']}/content")
    assert got.status_code == 200
    assert got.json()["content"] == "# 中文正文"


def test_upload_empty_file_is_accepted_without_crash(client: TestClient) -> None:
    """空文件不该 500；落盘成功、size=0。"""
    case_id = _new_case(client, "空文件")
    item = _upload(client, case_id, "empty.txt", b"", mime="text/plain")
    assert item["file"]["size"] == 0
    assert disk_path(item["file"]["stored_path"]).is_file()


@pytest.mark.parametrize(
    ("filename", "mime"),
    [("mal.exe", "application/octet-stream"), ("bundle.zip", "application/zip")],
)
def test_upload_unsupported_type_stores_original_without_text(
    client: TestClient, filename: str, mime: str
) -> None:
    """不支持转换的类型只存原件：download 可用、content 404（而非 500）。"""
    case_id = _new_case(client, "不支持类型")
    item = _upload(client, case_id, filename, b"\x00\x01\x02binary", mime=mime)

    assert item["file"]["md_path"] is None
    file_id = item["file"]["id"]
    assert client.get(f"{API}/files/{file_id}/download").status_code == 200
    assert client.get(f"{API}/files/{file_id}/content").status_code == 404


def test_upload_broken_pdf_records_convert_error_not_500(client: TestClient) -> None:
    """伪装成 PDF 的垃圾字节：整个请求仍 201，失败信息落在 convert_error。"""
    case_id = _new_case(client, "坏 PDF")
    item = _upload(client, case_id, "broken.pdf", b"definitely not a pdf", mime="application/pdf")
    assert item["convert_error"], item


def test_upload_partial_failure_does_not_fail_whole_request(client: TestClient) -> None:
    """一次传三个、中间那个转换失败，其余两个照常入库。"""
    case_id = _new_case(client, "部分失败")
    resp = client.post(
        f"{API}/cases/{case_id}/files",
        files=[
            ("files", ("ok1.md", b"# ok1", "text/markdown")),
            ("files", ("bad.pdf", b"not a pdf", "application/pdf")),
            ("files", ("ok2.txt", b"plain text", "text/plain")),
        ],
    )
    assert resp.status_code == 201, resp.text
    items = resp.json()
    assert len(items) == 3
    assert items[0]["convert_error"] is None and items[2]["convert_error"] is None
    assert items[1]["convert_error"]


def test_upload_same_name_twice_never_overwrites(client: TestClient) -> None:
    case_id = _new_case(client, "重名")
    first = _upload(client, case_id, "dup.md", b"# first")
    second = _upload(client, case_id, "dup.md", b"# second")

    p1, p2 = disk_path(first["file"]["stored_path"]), disk_path(second["file"]["stored_path"])
    assert p1 != p2
    assert p1.read_bytes() == b"# first" and p2.read_bytes() == b"# second"


def test_upload_without_files_returns_422(client: TestClient) -> None:
    case_id = _new_case(client, "无文件")
    assert client.post(f"{API}/cases/{case_id}/files").status_code == 422


def test_large_upload_is_accepted(client: TestClient) -> None:
    """8MB 上传不得被截断或 5xx（真实交底书附件可以很大）。"""
    case_id = _new_case(client, "大文件")
    payload = b"y" * (8 * 1024 * 1024)
    item = _upload(client, case_id, "big.txt", payload, mime="text/plain")
    assert item["file"]["size"] == len(payload)


def test_download_and_content_404_when_disk_file_gone(client: TestClient) -> None:
    """DB 里有记录但磁盘文件被外部删掉 → 404（带可读原因），不是 500。"""
    case_id = _new_case(client, "磁盘丢失")
    item = _upload(client, case_id, "gone.md", b"# gone")
    file_id = item["file"]["id"]

    disk_path(item["file"]["md_path"]).unlink()
    assert client.get(f"{API}/files/{file_id}/content").status_code == 404
    assert client.get(f"{API}/files/{file_id}/download").status_code == 404


def test_delete_file_removes_disk_artifacts(client: TestClient) -> None:
    case_id = _new_case(client, "删文件")
    item = _upload(client, case_id, "del.md", b"# del")
    stored = disk_path(item["file"]["stored_path"])
    assert stored.is_file()

    assert client.delete(f"{API}/files/{item['file']['id']}").status_code == 200
    assert not stored.exists()
    assert client.delete(f"{API}/files/{item['file']['id']}").status_code == 404


def test_delete_file_refuses_paths_outside_uploads(client: TestClient, tmp_path: Path) -> None:
    """脏数据/被篡改的 stored_path 不得让删除接口变成任意文件删除。"""
    case_id = _new_case(client, "越界删除")
    outside = tmp_path / "DO_NOT_DELETE.txt"
    outside.write_text("keep me", encoding="utf-8")

    from ulid import ULID

    file_id = str(ULID())
    db.execute(
        "INSERT INTO files(id, case_id, kind, orig_name, mime, size, stored_path, md_path,"
        " meta_json, created_at) VALUES (?,?,'upload','x.txt','text/plain',7,?,NULL,'{}',?)",
        (file_id, case_id, str(outside), db.now_str()),
    )

    assert client.delete(f"{API}/files/{file_id}").status_code == 200
    assert outside.exists(), "uploads/ 之外的文件被误删了"
    assert db.query_one("SELECT id FROM files WHERE id=?", (file_id,)) is None


# ===========================================================================
# 交付物：版本只增不改 / 导出错误码
# ===========================================================================


def test_artifact_versions_are_strictly_increasing_and_never_overwrite(
    client: TestClient,
) -> None:
    case_id = _new_case(client, "版本序列")
    saved = [
        artifacts_service.save_artifact_sync(
            case_id, "disclosure_md", f"# 第 {i} 版", "md", title="版本序列"
        )
        for i in range(5)
    ]

    assert [a.version for a in saved] == [1, 2, 3, 4, 5]
    assert len({a.filename for a in saved}) == 5
    for i, a in enumerate(saved):
        assert disk_path(a.stored_path).read_text(encoding="utf-8") == f"# 第 {i} 版"

    listed = client.get(f"{API}/cases/{case_id}/artifacts").json()
    assert [a["version"] for a in listed] == [5, 4, 3, 2, 1]


def test_concurrent_artifact_writes_keep_versions_unique(client: TestClient) -> None:
    """并发落同一 case+kind 的交付物：版本分配与插入在同一事务内，不得跳号/撞号。"""
    case_id = _new_case(client, "并发交付物")
    errors: list[str] = []
    lock = threading.Lock()

    def save(i: int) -> None:
        try:
            artifacts_service.save_artifact_sync(
                case_id, "disclosure_md", f"# 并发 {i}", "md", title="并发"
            )
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=save, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    rows = db.query_all(
        "SELECT version, stored_path FROM artifacts WHERE case_id=? ORDER BY version", (case_id,)
    )
    assert [r["version"] for r in rows] == list(range(1, 11))
    assert len({r["stored_path"] for r in rows}) == 10   # 文件互不覆盖


def test_case_detail_lists_only_latest_version_per_kind(client: TestClient) -> None:
    case_id = _new_case(client, "最新版")
    for i in range(3):
        artifacts_service.save_artifact_sync(case_id, "disclosure_md", f"# {i}", "md", title="X")
    for _ in range(2):
        artifacts_service.save_artifact_sync(case_id, "search_report_json", "{}", "json", title="X")

    latest = client.get(f"{API}/cases/{case_id}").json()["artifacts_latest"]
    assert {a["kind"]: a["version"] for a in latest} == {
        "disclosure_md": 3,
        "search_report_json": 2,
    }


def test_artifact_content_rejects_binary_with_415(client: TestClient) -> None:
    case_id = _new_case(client, "二进制交付物")
    art = artifacts_service.save_artifact_sync(
        case_id, "disclosure_docx", b"PK\x03\x04binary", "docx", title="二进制"
    )
    resp = client.get(f"{API}/artifacts/{art.id}/content")
    assert resp.status_code == 415, resp.text


@pytest.mark.parametrize("fmt", ["docx", "pdf"])
def test_artifact_export_rejects_unsupported_kind(client: TestClient, fmt: str) -> None:
    case_id = _new_case(client, "不可导出")
    art = artifacts_service.save_artifact_sync(
        case_id, "search_report_json", "{}", "json", title="检索报告"
    )
    resp = client.post(f"{API}/artifacts/{art.id}/export", json={"format": fmt})
    assert resp.status_code == 422, resp.text
    assert "不支持导出" in resp.json()["detail"]


def test_artifact_export_rejects_invalid_format(client: TestClient) -> None:
    case_id = _new_case(client, "非法格式")
    art = artifacts_service.save_artifact_sync(case_id, "disclosure_md", "# x", "md", title="X")
    assert client.post(f"{API}/artifacts/{art.id}/export", json={"format": "txt"}).status_code == 422
    assert client.post(f"{API}/artifacts/{art.id}/export", json={}).status_code == 422


def test_artifact_export_404_when_source_file_missing(client: TestClient) -> None:
    case_id = _new_case(client, "源文件丢失")
    art = artifacts_service.save_artifact_sync(case_id, "disclosure_md", "# x", "md", title="X")
    disk_path(art.stored_path).unlink()

    assert client.post(f"{API}/artifacts/{art.id}/export", json={"format": "docx"}).status_code == 404
    assert client.get(f"{API}/artifacts/{art.id}/download").status_code == 404
    assert client.get(f"{API}/artifacts/{art.id}/content").status_code == 404


def test_artifacts_filter_by_unknown_kind_returns_empty_list(client: TestClient) -> None:
    case_id = _new_case(client, "未知 kind")
    resp = client.get(f"{API}/cases/{case_id}/artifacts", params={"kind": "no_such_kind"})
    assert resp.status_code == 200 and resp.json() == []


# ===========================================================================
# 流水线状态机组合
# ===========================================================================


def test_pipeline_control_on_idle_case(client: TestClient) -> None:
    """未启动的案件：cancel/retry/input 一律 409，resume 等价 start。"""
    case_id = _new_case(client, "空闲案件", module=DEMO_MODULE)

    assert client.post(f"{API}/cases/{case_id}/pipeline/cancel").status_code == 409
    assert client.post(f"{API}/cases/{case_id}/pipeline/retry").status_code == 409
    assert client.post(
        f"{API}/cases/{case_id}/pipeline/input", json={"step_key": "gate_form", "payload": {}}
    ).status_code == 409

    started = client.post(f"{API}/cases/{case_id}/pipeline/resume")
    assert started.status_code == 202, started.text
    _wait_state(client, case_id, lambda s: s["pending_interaction"] is not None)
    client.post(f"{API}/cases/{case_id}/pipeline/cancel")


def test_duplicate_input_while_waiting_user_returns_409(client: TestClient) -> None:
    """门控只能被回答一次；重复提交必须 409，不能把同一步唤醒两遍。"""
    case_id = _new_case(client, "重复回答", module=DEMO_MODULE)
    assert client.post(f"{API}/cases/{case_id}/pipeline/start", json={}).status_code == 202
    _wait_state(client, case_id, lambda s: s["pending_interaction"] is not None)

    first = client.post(
        f"{API}/cases/{case_id}/pipeline/input",
        json={"step_key": "gate_form", "payload": {"answer": 1}},
    )
    second = client.post(
        f"{API}/cases/{case_id}/pipeline/input",
        json={"step_key": "gate_form", "payload": {"answer": 2}},
    )
    assert first.status_code == 202, first.text
    assert second.status_code == 409, second.text

    _wait_state(client, case_id, lambda s: all(x["status"] == "done" for x in s["steps"]))
    # 生效的是第一次提交的答案
    assert client.get(f"{API}/cases/{case_id}").json()["state"]["form"] == {"answer": 1}


def test_concurrent_start_admits_exactly_one(client: TestClient) -> None:
    """5 个线程同时 start 同一案件：只能有一个 202，其余 409。"""
    case_id = _new_case(client, "并发启动", module=DEMO_MODULE)
    codes: list[int] = []
    lock = threading.Lock()

    def fire() -> None:
        resp = client.post(f"{API}/cases/{case_id}/pipeline/start", json={})
        with lock:
            codes.append(resp.status_code)

    threads = [threading.Thread(target=fire) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert codes.count(202) == 1, codes
    assert codes.count(409) == 4, codes
    client.post(f"{API}/cases/{case_id}/pipeline/cancel")


def test_retry_and_resume_on_completed_case(client: TestClient) -> None:
    """跑完的案件：retry 任何步骤都 409；resume 合法（全 done → 直接再次 completed）。"""
    case_id = _new_case(client, "已完成", module=DEMO_MODULE)
    assert client.post(f"{API}/cases/{case_id}/pipeline/start", json={}).status_code == 202
    _wait_state(client, case_id, lambda s: s["pending_interaction"] is not None)
    client.post(
        f"{API}/cases/{case_id}/pipeline/input",
        json={"step_key": "gate_form", "payload": {"answer": 7}},
    )
    _wait_state(client, case_id, lambda s: all(x["status"] == "done" for x in s["steps"]))

    assert client.post(f"{API}/cases/{case_id}/pipeline/retry").status_code == 409
    for step in ("prepare", "gate_form", "flaky", "no-such-step"):
        resp = client.post(f"{API}/cases/{case_id}/pipeline/retry", json={"step_key": step})
        assert resp.status_code == 409, f"{step} → {resp.status_code}"

    resumed = client.post(f"{API}/cases/{case_id}/pipeline/resume")
    assert resumed.status_code == 202, resumed.text
    _wait_state(client, case_id, lambda s: all(x["status"] == "done" for x in s["steps"]))


def test_pipeline_input_requires_step_key(client: TestClient) -> None:
    case_id = _new_case(client, "缺字段", module=DEMO_MODULE)
    assert client.post(
        f"{API}/cases/{case_id}/pipeline/input", json={"payload": {}}
    ).status_code == 422


def test_corrupt_state_json_does_not_break_read_endpoints(client: TestClient) -> None:
    """state_json 被写坏时，读接口必须降级返回而不是 500。"""
    case_id = _new_case(client, "坏 state", module=DEMO_MODULE)
    db.execute("UPDATE cases SET state_json=? WHERE id=?", ("{ 这不是 json", case_id))

    assert client.get(f"{API}/cases/{case_id}").status_code == 200
    assert client.get(f"{API}/cases/{case_id}/pipeline/state").status_code == 200
    assert client.get(f"{API}/cases/{case_id}/reader/report").status_code == 404


# ===========================================================================
# 案件 CRUD / 级联清理
# ===========================================================================


@pytest.mark.parametrize("status", ["running", "waiting_user", "completed", "failed", "nope"])
def test_patch_case_rejects_non_user_settable_status(client: TestClient, status: str) -> None:
    """只有 draft / archived 可由用户改；运行态由引擎独占。"""
    case_id = _new_case(client, "状态枚举")
    assert client.patch(f"{API}/cases/{case_id}", json={"status": status}).status_code == 422


def test_patch_case_roundtrip(client: TestClient) -> None:
    case_id = _new_case(client, "改名")
    resp = client.patch(
        f"{API}/cases/{case_id}",
        json={
            "title": "新标题",
            "status": "archived",
            "patent_type": "utility_model",
            "contact": {"name": "王医生", "phone": "13800138000", "email": "w@example.com"},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "新标题"
    assert body["status"] == "archived"
    assert body["patent_type"] == "utility_model"
    assert body["contact"]["name"] == "王医生"

    # 空 body / 全空白标题都不得清空既有值
    assert client.patch(f"{API}/cases/{case_id}", json={}).status_code == 200
    assert client.patch(f"{API}/cases/{case_id}", json={"title": "   "}).json()["title"] == "新标题"


def test_delete_case_cascades_to_children(client: TestClient) -> None:
    """删案件必须级联清掉 files / messages / artifacts / search_*，不留孤儿行。"""
    import anyio

    from app.services.sse import hub

    case_id = _new_case(client, "级联删除")
    _upload(client, case_id, "material.md", b"# material")
    artifacts_service.save_artifact_sync(case_id, "disclosure_md", "# doc", "md", title="X")
    client.post(
        f"{API}/cases/{case_id}/search/hits",
        json={"hits": [{"url": "https://example.com/p1", "title": "在先文献"}]},
    )
    anyio.run(hub.emit, case_id, "log", {"message": "hello"})

    tables = ("files", "messages", "artifacts", "search_queries", "search_hits")
    before = {t: db.query_one(f"SELECT COUNT(*) n FROM {t} WHERE case_id=?", (case_id,))["n"]
              for t in tables}
    assert all(n > 0 for n in before.values()), before

    assert client.delete(f"{API}/cases/{case_id}").status_code == 200

    after = {t: db.query_one(f"SELECT COUNT(*) n FROM {t} WHERE case_id=?", (case_id,))["n"]
             for t in tables}
    assert after == dict.fromkeys(tables, 0), f"级联清理不干净：{before} → {after}"


def test_delete_case_with_purge_files_clears_disk(client: TestClient) -> None:
    case_id = _new_case(client, "清磁盘")
    _upload(client, case_id, "x.md", b"# x")
    artifacts_service.save_artifact_sync(case_id, "disclosure_md", "# doc", "md", title="X")
    cfg = get_config()
    uploads, outputs = cfg.uploads_dir / case_id, cfg.outputs_dir / case_id
    assert uploads.is_dir() and outputs.is_dir()

    assert client.delete(f"{API}/cases/{case_id}", params={"purge_files": "true"}).status_code == 200
    assert not uploads.exists() and not outputs.exists()


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 201}, "limit"),
        ({"offset": -1}, "offset"),
    ],
)
def test_case_list_pagination_bounds(
    client: TestClient, params: dict[str, int], field: str
) -> None:
    resp = client.get(f"{API}/cases", params=params)
    assert resp.status_code == 422
    assert any(err["loc"][-1] == field for err in resp.json()["detail"])


@pytest.mark.parametrize(
    "q", ["'; DROP TABLE cases; --", "%", "_", "100%", "\\", '"', "') OR 1=1 --"]
)
def test_case_list_keyword_is_parameterized(client: TestClient, q: str) -> None:
    """关键词走参数绑定：注入串与 LIKE 通配符都只当普通文本。"""
    resp = client.get(f"{API}/cases", params={"q": q})
    assert resp.status_code == 200, resp.text
    assert db.query_one("SELECT COUNT(*) n FROM cases")["n"] > 0   # 表还在


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"module": "nope"}, "module"),
        ({"title": "缺 module"}, "module"),
        ({"module": "disclosure", "patent_type": "nope"}, "patent_type"),
    ],
)
def test_create_case_validation(client: TestClient, body: dict, field: str) -> None:
    resp = client.post(f"{API}/cases", json=body)
    assert resp.status_code == 422
    assert any(err["loc"][-1] == field for err in resp.json()["detail"])


# ===========================================================================
# 消息 / SSE 重放（服务端重启后重连靠的就是 messages 表）
# ===========================================================================


async def test_sse_replay_is_restart_safe(client: TestClient) -> None:
    """SSE 重放读的是 messages 表，故与进程存活无关：重启后带 Last-Event-ID 重连仍能补齐。"""
    from app.services import sse

    case_id = _new_case(client, "SSE 重放")
    for i in range(4):
        await sse.hub.emit(case_id, "log", {"i": i})

    # 全量重放（Last-Event-ID=0）
    assert [e["id"] for e in await sse.replay(case_id, 0)] == ["1", "2", "3", "4"]
    # 断点续传（Last-Event-ID=2）：只补 seq>2 的
    tail = await sse.replay(case_id, 2)
    assert [e["id"] for e in tail] == ["3", "4"]
    assert json.loads(tail[0]["data"])["i"] == 2
    assert all(e["event"] == "log" for e in tail)

    # 断开所有内存订阅后（模拟进程重启）重放结果不变
    sse.hub._subs.pop(case_id, None)
    assert len(await sse.replay(case_id, 0)) == 4

    # messages 兜底接口与重放同源
    msgs = client.get(f"{API}/cases/{case_id}/messages", params={"after_seq": 2}).json()
    assert [m["seq"] for m in msgs] == [3, 4]


async def test_sse_hub_broadcasts_to_all_subscribers(client: TestClient) -> None:
    """同一案件多端订阅：每个订阅者都要拿到同一条事件，退出后自动注销。"""
    from app.services import sse

    case_id = _new_case(client, "多端订阅")
    async with sse.hub.subscribe(case_id) as q1, sse.hub.subscribe(case_id) as q2:
        assert sse.hub.subscriber_count(case_id) == 2
        await sse.hub.emit(case_id, "log", {"broadcast": True})
        assert json.loads(q1.get_nowait()["data"])["broadcast"] is True
        assert json.loads(q2.get_nowait()["data"])["broadcast"] is True
    assert sse.hub.subscriber_count(case_id) == 0


def test_sse_endpoint_404_for_unknown_case(client: TestClient) -> None:
    assert client.get(f"{API}/cases/{MISSING_ID}/events").status_code == 404


@pytest.mark.parametrize(
    ("params", "field"),
    [({"after_seq": -1}, "after_seq"), ({"limit": 0}, "limit"), ({"limit": 2001}, "limit")],
)
def test_messages_query_bounds(client: TestClient, params: dict, field: str) -> None:
    case_id = _new_case(client, "消息边界")
    resp = client.get(f"{API}/cases/{case_id}/messages", params=params)
    assert resp.status_code == 422
    assert any(err["loc"][-1] == field for err in resp.json()["detail"])


# ===========================================================================
# 查新 / OA 案例库的错误路径（均不触发 LLM）
# ===========================================================================


def test_search_hits_manual_entry_requires_url(client: TestClient) -> None:
    case_id = _new_case(client, "人工录入")
    assert client.post(
        f"{API}/cases/{case_id}/search/hits", json={"hits": [{"title": "缺 URL"}]}
    ).status_code == 422
    assert client.post(f"{API}/cases/{case_id}/search/hits", json={"hits": []}).status_code == 422


def test_search_hit_patch_requires_a_field(client: TestClient) -> None:
    case_id = _new_case(client, "勾选")
    created = client.post(
        f"{API}/cases/{case_id}/search/hits",
        json={"hits": [{"url": "https://example.com/p1", "title": "T1"}]},
    )
    assert created.status_code == 201, created.text
    hit_id = created.json()[0]["id"]

    assert client.patch(f"{API}/search/hits/{hit_id}", json={}).status_code == 422
    toggled = client.patch(f"{API}/search/hits/{hit_id}", json={"selected": False})
    assert toggled.status_code == 200 and toggled.json()["selected"] is False


def test_cnipa_search_rejects_empty_terms(client: TestClient) -> None:
    """检索词为空必须在起后台任务之前就 422（否则白开一个浏览器子进程）。"""
    case_id = _new_case(client, "空检索词")
    assert client.post(f"{API}/cases/{case_id}/search/cnipa", json={"terms": []}).status_code == 422


def test_oa_library_status_filter_validation(client: TestClient) -> None:
    assert client.get(f"{API}/oa/library", params={"status": "nope"}).status_code == 422
    assert client.get(f"{API}/oa/library", params={"status": "draft"}).status_code == 200


@pytest.mark.parametrize("params", [{"k": 0}, {"k": 51}])
def test_oa_search_k_bounds(client: TestClient, params: dict) -> None:
    assert client.get(f"{API}/oa/library/search", params=params).status_code == 422


def test_oa_search_always_reports_retrieval_mode(client: TestClient) -> None:
    """检索方式必须明示（未配 embedding 时降级为 keyword，接口照常 200）。"""
    resp = client.get(f"{API}/oa/library/search", params={"q": "新颖性 创造性"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["retrieval_mode"] in {"vector", "brute", "keyword"}


def test_oa_library_crud_and_404(client: TestClient) -> None:
    """直接建一条草稿（绕开需要 LLM 的 ingest），验证详情/更新/删除的错误码。"""
    from ulid import ULID

    library_id = str(ULID())
    db.execute(
        "INSERT INTO oa_library(id, case_note_md, frontmatter_json, status, embedded,"
        " user_id, created_at, updated_at) VALUES (?,?,?,'draft',0,?,?,?)",
        (
            library_id,
            "# 案例笔记\n正文",
            json.dumps({"tags": ["t1"], "defect_types": ["新颖性"]}, ensure_ascii=False),
            client.user["id"],
            db.now_str(),
            db.now_str(),
        ),
    )

    assert client.get(f"{API}/oa/library/{library_id}").status_code == 200
    assert client.put(f"{API}/oa/library/{library_id}", json={}).status_code == 422
    assert client.put(f"{API}/oa/library/{library_id}", json={"status": "nope"}).status_code == 422
    assert client.put(
        f"{API}/oa/library/{library_id}", json={"note_md": "# 改过的笔记"}
    ).status_code == 200

    assert client.delete(f"{API}/oa/library/{library_id}").status_code == 200
    assert client.delete(f"{API}/oa/library/{library_id}").status_code == 404
    assert client.get(f"{API}/oa/library/{library_id}").status_code == 404


@pytest.mark.parametrize(
    ("kwargs", "expect"),
    [
        ({"json": {}}, 422),                                              # 没给任何材料
        ({"json": ["not", "a", "dict"]}, 422),                            # body 不是对象
        ({"files": [("files", ("empty.md", b"", "text/markdown"))]}, 422),  # 空文件
    ],
)
def test_oa_ingest_rejects_unusable_input_before_llm(
    client: TestClient, kwargs: dict, expect: int
) -> None:
    """ingest 会调 LLM，所以「材料不可用」必须在调用之前就 422（`_no_llm` 保证没打出去）。"""
    assert client.post(f"{API}/oa/library/ingest", **kwargs).status_code == expect


def test_oa_ingest_rejects_file_without_text(client: TestClient) -> None:
    case_id = _new_case(client, "无文本件")
    item = _upload(client, case_id, "bin.exe", b"\x00\x01", mime="application/octet-stream")
    resp = client.post(f"{API}/oa/library/ingest", json={"file_id": item["file"]["id"]})
    assert resp.status_code == 422, resp.text


def test_oa_ingest_bad_json_body_returns_422(client: TestClient) -> None:
    resp = client.post(
        f"{API}/oa/library/ingest",
        content=b"{ this is not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422, resp.text


# ===========================================================================
# 系统级契约
# ===========================================================================


def test_openapi_exposes_every_operation_with_a_summary(client: TestClient) -> None:
    """openapi.json 是前端与本测试的唯一契约来源：操作数与 summary 都不能掉。"""
    spec = client.get("/openapi.json").json()
    operations = [(p, m, op) for p, ops in spec["paths"].items() for m, op in ops.items()]
    # 77 = 73 + 自助注册引入的 4 个：
    #   GET/POST /auth/registration-open|register、GET/PUT /admin/registration
    # 79 = 77 + 技能库的 2 个：GET /skills、PUT /skills/{key}
    # 80 = 79 + 解锁账号（POST /admin/users/{id}/unlock）
    # 81 = 80 + 案件媒体（GET /cases/{id}/media：网页端内联显示正文里的图）
    # 83 = 81 + 模型档位（GET/PUT /settings/model-tiers：快速 / 深度思考两档）
    # 84 = 83 + 档位试连（POST /settings/model-tiers/{tier}/test）
    assert len(operations) == 84, f"端点数变了：{len(operations)}"
    assert not [f"{m.upper()} {p}" for p, m, op in operations if not op.get("summary")]


def test_api_404_is_json_never_spa_html(client: TestClient) -> None:
    """/api 前缀绝不能被 SPA fallback 吐 index.html —— 否则前端拿到 HTML 当 JSON 解析必炸。

    M8 起 SPA 挂载点（`SpaMount`）对 /api 前缀**直接不匹配**，状态码的决定权还给
    API 路由：不存在的路径 404、方法不匹配 405，两者都是 JSON。
    """
    for path in ("/api/v1/no-such-endpoint", "/api/v1/render/mermaid", "/api/v1/cases/x/nope"):
        resp = client.get(path)
        assert resp.status_code in (404, 405), f"{path} → {resp.status_code}"
        assert resp.headers["content-type"].startswith("application/json"), path
        assert "<!doctype html" not in resp.text.lower(), path


def test_method_not_allowed_on_json_routes(client: TestClient) -> None:
    assert client.put(f"{API}/cases", json={}).status_code == 405
    assert client.delete(f"{API}/system/health").status_code == 405
    # POST-only 的 /api 路径被 GET 访问要给 405（而不是被 SPA 挂载点吞成 404）
    assert client.get(f"{API}/render/mermaid").status_code == 405
    assert client.get(f"{API}/auth/login").status_code == 405


def test_health_and_env(client: TestClient, admin_client: TestClient) -> None:
    health = client.get(f"{API}/system/health").json()
    assert health["ok"] is True and health["name"] == "引途医疗专利智能体"

    env = admin_client.get(f"{API}/system/env").json()
    for key in ("python", "word", "soffice", "chrome", "sqlite_vec", "data_dir", "playwright"):
        assert key in env, key
    assert client.get(f"{API}/system/env").status_code == 403, "普通用户不该看到服务器环境"


def test_public_health_does_not_name_upstream_models(client: TestClient) -> None:
    """/system/health 在中间件白名单里（看门狗要在无会话下 curl 它）。

    正因为它无需登录，就不能顺带告诉任何能连上端口的人「这台机器接的是哪家上游」。
    重启决策要的是计数（inflight / queued），不是模型名。
    """
    llm = client.get(f"{API}/system/health").json()["llm"]
    assert "inflight" in llm and "queued" in llm, "重启决策需要的计数必须保留"
    assert "inflight_models" not in llm


def test_browser_probe_never_raises(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """浏览器探测走本地子进程；探测脚本起不来时也必须 200 + ok=False。"""
    from app.models.search import BrowserProbe
    from app.services import cnipa

    monkeypatch.setattr(
        cnipa, "probe_browser_sync",
        lambda *a, **k: BrowserProbe(ok=False, error="模拟：探测脚本不可用"),
    )
    resp = client.get(f"{API}/search/browser-probe")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False


def test_mermaid_render_requires_code(client: TestClient) -> None:
    """缺 code 在校验层就 422，不会去起 Playwright。"""
    assert client.post(f"{API}/render/mermaid", json={}).status_code == 422
