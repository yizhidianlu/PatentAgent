"""M1 冒烟测试：健康检查 / 设置掩码 / 建案列表 / SSE 重放。"""

from __future__ import annotations

import asyncio
import socket

import httpx
from fastapi.testclient import TestClient



def test_health(client: TestClient) -> None:
    resp = client.get("/api/v1/system/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["name"] == "引途医疗专利智能体"


def test_system_env(admin_client: TestClient) -> None:
    resp = admin_client.get("/api/v1/system/env")
    assert resp.status_code == 200
    body = resp.json()
    assert "sqlite_vec" in body and "loaded" in body["sqlite_vec"]
    assert body["python"]["version"]


def test_system_env_is_admin_only(client: TestClient) -> None:
    """环境自检对普通用户不可见。

    它 report 的每一项都是服务器内部事实：数据目录绝对路径（连带泄露服务器账户名
    与目录结构）、Python 可执行文件路径、操作系统精确版本、已装软件、磁盘容量。
    对运维是诊断信息，对普通用户是侦察材料——平台是多用户的，普通用户不该看见。
    """
    assert client.get("/api/v1/system/env").status_code == 403


def test_settings_llm_masked(admin_client: TestClient) -> None:
    secret = "sk-abcdef1234567890wxyz"
    put = admin_client.put(
        "/api/v1/settings/llm",
        json={"base_url": "https://api.example.com/v1", "api_key": secret, "model": "test-model"},
    )
    assert put.status_code == 200
    assert secret not in put.text            # PUT 响应也不得回显明文
    got = admin_client.get("/api/v1/settings/llm")
    assert got.status_code == 200
    body = got.json()
    assert body["model"] == "test-model"
    assert body["api_key"] == "sk-***wxyz"   # 掩码：sk-*** + 尾 4 位
    assert secret not in got.text

    # 空 api_key 表示不修改：改 model 不应清掉已存 key
    put2 = admin_client.put(
        "/api/v1/settings/llm",
        json={"base_url": "https://api.example.com/v1", "api_key": "", "model": "test-model-2"},
    )
    assert put2.status_code == 200
    got2 = admin_client.get("/api/v1/settings/llm").json()
    assert got2["model"] == "test-model-2"
    assert got2["api_key"] == "sk-***wxyz"


def test_case_create_and_list(client: TestClient) -> None:
    created = client.post("/api/v1/cases", json={"module": "disclosure", "title": "冒烟测试案件"})
    assert created.status_code == 201
    case = created.json()
    assert case["status"] == "draft"
    case_id = case["id"]

    listed = client.get("/api/v1/cases", params={"module": "disclosure", "q": "冒烟"})
    assert listed.status_code == 200
    page = listed.json()
    assert page["total"] >= 1
    assert any(item["id"] == case_id for item in page["items"])

    detail = client.get(f"/api/v1/cases/{case_id}")
    assert detail.status_code == 200
    # 新建案件的工作内存里只有模型档位（建案时定下，见 api/cases.create_case）
    assert detail.json()["state"] == {"_model_tier": "deep"}

    assert client.get("/api/v1/cases/no-such-id").status_code == 404


def _free_port() -> int:
    """向操作系统申请一个空闲端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_sse_replay(client) -> None:
    """先落一条持久化事件，再以 Last-Event-ID=0 连接 SSE，应立即重放该事件。

    说明：httpx 的 ASGITransport 会缓冲完整响应、无法测无限 SSE 流，
    故在测试内起真实 uvicorn（随机端口）走 TCP 流式读取。
    """
    import uvicorn

    from app.main import app
    from app.services.sse import hub

    created = client.post("/api/v1/cases", json={"module": "reader", "title": "SSE 测试"})
    case_id = created.json()["id"]

    seq = await hub.emit(case_id, "log", {"text": "你好 SSE"})
    assert seq == 1

    # messages 兜底接口应能读到同一条
    msgs = client.get(f"/api/v1/cases/{case_id}/messages").json()
    assert len(msgs) == 1 and msgs[0]["seq"] == 1

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server_task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):  # 等服务就绪（最多 10s）
            if server.started:
                break
            await asyncio.sleep(0.1)
        assert server.started, "uvicorn 未能在 10s 内启动"

        lines: list[str] = []

        async def read_stream() -> None:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                cookies={"pa_session": client.session_id},
            ) as ac:
                async with ac.stream(
                    "GET",
                    f"/api/v1/cases/{case_id}/events",
                    headers={"Last-Event-ID": "0"},
                    timeout=10,
                ) as resp:
                    assert resp.status_code == 200
                    assert resp.headers["content-type"].startswith("text/event-stream")
                    async for line in resp.aiter_lines():
                        lines.append(line)
                        if line.startswith("data:"):
                            break

        await asyncio.wait_for(read_stream(), timeout=20)
        joined = "\n".join(lines)
        assert "event: log" in joined
        assert "你好 SSE" in joined
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)
