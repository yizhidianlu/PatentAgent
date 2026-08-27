"""测试公共配置与账号夹具（M8）。

两件事：
1. 在导入 app 之前把 `DATA_DIR` 指向独立的临时目录，并给出确定的管理员凭据
   （首启引导会用它建管理员，`must_change_password=0`）；
2. 提供 `client`（已登录**普通用户**）与 `admin_client`（已登录**管理员**）两个
   会话级夹具。

**这里没有、也绝不会有"测试环境跳过认证"的开关。**
两个夹具都真实走 `POST /auth/login` 拿 Cookie 与 CSRF token，
再由 `AuthedClient` 代理在每次请求上带 `pa_session` Cookie 与 `X-CSRF-Token` 头
—— 与浏览器的行为完全一致。任何绕过认证的后门一旦误配到生产就是灾难，
宁可让夹具复杂一点。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

# conftest 在测试模块之前被导入，此处设置环境变量即可影响 app.config
_TMP_DATA = Path(tempfile.mkdtemp(prefix="patentagent-test-data-"))
os.environ["DATA_DIR"] = str(_TMP_DATA)
# 首启引导用的管理员凭据：显式提供密码 → 不置 must_change_password
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "AdminTest#2026")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

API = "/api/v1"

ADMIN_USERNAME = os.environ["ADMIN_USERNAME"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]

#: 普通用户夹具用的账号（首次登录后立刻改成 USER_PASSWORD）
USER_USERNAME = "tester"
USER_INITIAL_PASSWORD = "Initial#Pw2026"
USER_PASSWORD = "TesterPw#2026"


class AuthedClient:
    """把某个已登录会话绑定到共享 TestClient 上的轻量代理。

    - 每次请求显式带该会话的 `pa_session` / `pa_csrf` Cookie（不依赖共享 jar，
      多个用户并存时互不串味）；
    - 非 GET 请求自动补 `X-CSRF-Token` 头（前端 API 层的行为）；
    - 其余参数（json / files / data / params / headers…）原样透传。
    """

    def __init__(
        self,
        raw: TestClient,
        session_id: str,
        csrf_token: str,
        user: dict[str, Any],
    ) -> None:
        self._raw = raw
        self.session_id = session_id
        self.csrf_token = csrf_token
        self.user = user

    # -- 内部 --------------------------------------------------------------

    def request(self, method: str, url: str, **kwargs: Any):
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("X-CSRF-Token", self.csrf_token)
        # 直接拼 Cookie 头（而非 httpx 的 per-request cookies=，那个已被弃用，
        # 且语义上会与共享 jar 纠缠）
        jar = {"pa_session": self.session_id, "pa_csrf": self.csrf_token}
        jar.update(kwargs.pop("cookies", None) or {})
        headers.setdefault("Cookie", "; ".join(f"{k}={v}" for k, v in jar.items()))
        return self._raw.request(method, url, headers=headers, **kwargs)

    # -- HTTP 动词 ---------------------------------------------------------

    def get(self, url: str, **kwargs: Any):
        return self.request("GET", url, **kwargs)

    def head(self, url: str, **kwargs: Any):
        return self.request("HEAD", url, **kwargs)

    def options(self, url: str, **kwargs: Any):
        return self.request("OPTIONS", url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any):
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any):
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any):
        return self.request("DELETE", url, **kwargs)


# ---------------------------------------------------------------------------
# 登录工具（测试可直接 import 使用）
# ---------------------------------------------------------------------------


def login(raw: TestClient, username: str, password: str) -> AuthedClient:
    """真实走 /auth/login 拿会话；返回绑定该会话的代理客户端。"""
    raw.cookies.clear()
    resp = raw.post(f"{API}/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"登录失败：{resp.status_code} {resp.text}"
    session_id = resp.cookies.get("pa_session")
    assert session_id, "登录响应没有下发 pa_session Cookie"
    body = resp.json()
    # 共享 jar 清空：后续请求一律靠 AuthedClient 显式带 Cookie
    raw.cookies.clear()
    return AuthedClient(raw, session_id, body["csrf_token"], body["user"])


def create_user(
    admin: AuthedClient,
    username: str,
    *,
    password: str | None = None,
    role: str = "user",
    display_name: str = "",
    quota: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """管理员建号；返回 `{user, password}`（密码仅此一次可取）。"""
    body: dict[str, Any] = {
        "username": username,
        "display_name": display_name or username,
        "role": role,
    }
    if password:
        body["password"] = password
    if quota:
        body["quota"] = quota
    resp = admin.post(f"{API}/admin/users", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def login_fresh_user(
    raw: TestClient, admin: AuthedClient, username: str, password: str
) -> AuthedClient:
    """建号 → 首登 → 强制改密 → 返回可正常使用的已登录客户端。

    管理员建的号一律 `must_change_password=1`，中间件会拦下除改密/登出之外的
    全部接口 —— 夹具走一遍真实流程把它解除，而不是去库里改标记。
    """
    initial = password + "Init0"
    create_user(admin, username, password=initial)
    fresh = login(raw, username, initial)
    resp = fresh.post(
        f"{API}/auth/change-password",
        json={"old_password": initial, "new_password": password},
    )
    assert resp.status_code == 200, resp.text
    # 改密不会吊销当前会话，但用户信息要刷新
    fresh.user = fresh.get(f"{API}/auth/me").json()
    return fresh


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def raw_client():
    """带 lifespan 的裸 TestClient（startup 建库 + 跑迁移 + 首启建管理员 + 恢复钩子）。

    整个测试会话共用一个 —— 只有进入上下文的 TestClient 才会持有常驻 portal，
    流水线的后台 asyncio 任务需要它活着才能跨请求推进。
    """
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def admin_client(raw_client: TestClient) -> AuthedClient:
    """已登录的管理员（平台设置、管理后台、跨用户查看都走它）。"""
    return login(raw_client, ADMIN_USERNAME, ADMIN_PASSWORD)


@pytest.fixture(scope="session")
def client(raw_client: TestClient, admin_client: AuthedClient) -> AuthedClient:
    """已登录的**普通用户**（既有业务测试的默认身份）。"""
    return login_fresh_user(raw_client, admin_client, USER_USERNAME, USER_PASSWORD)


@pytest.fixture(scope="session")
def other_client(raw_client: TestClient, admin_client: AuthedClient) -> AuthedClient:
    """另一个已登录的普通用户（跨用户隔离穿透测试用）。"""
    return login_fresh_user(raw_client, admin_client, "tester2", "Tester2Pw#2026")


def disk_path(value) -> Path:
    """入库形态的路径 → 真实磁盘路径（测试里读文件用）。

    `files.stored_path` / `artifacts.stored_path` 存的是**相对 DATA_DIR** 的路径，
    这样备份恢复到别的目录后仍然有效。测试要读盘时得走与生产同一条落实通路，
    不能直接 `Path(row["stored_path"])` —— 那样测的就不是产品的行为了。
    """
    from app.services import paths as paths_service

    resolved = paths_service.resolve(value)
    assert resolved is not None, f"路径落实失败：{value!r}"
    return resolved
