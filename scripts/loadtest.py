# -*- coding: utf-8 -*-
"""多用户压力测试：登录风暴 / 读写混合 / 上游并发闸。

    backend\\.venv\\Scripts\\python.exe scripts\\loadtest.py

**测什么、为什么是这三项。**
平台是单进程 asyncio + SQLite（WAL）+ 单订阅的 LLM 上游，多人同时使用时
真正可能坏的是三处，本脚本各压一处：

  A. 登录风暴 —— argon2id 每次验证吃 64MiB / ~50ms 且走共享线程池：
     并发登录是唯一会同时打满「CPU + 内存 + 线程槽位」的入口；
  B. 读写混合 —— 建案/列表/详情/上传（含转换子进程）/交付物，全体经同一把
     SQLite 写锁与同一个事件循环：这里的 p95 就是多人日常操作的真实手感；
  C. 上游并发闸 —— N 路 LLM 调用打向慢上游（本地 mock，1.2s/次）：验证
     ① 上游看到的并发不超过 LLM_MAX_CONCURRENCY；② LLM 等待期间纯读接口
     不被拖慢（异步 I/O 不阻塞事件循环）。

**刻意不测的**：完整流水线蜂群（每步的 prompt 路由要复刻一整套 FakeLLM 剧本，
桩的保真度成本高于结论价值）与真实 LLM 上游（会烧真额度）。这两项的缺口
在报告末尾如实标注——测试环境比真实环境少了哪个维度，要说出来。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
ADMIN_PASSWORD = "LoadTest#2026"

sys.path.insert(0, str(ROOT / "backend"))

import httpx          # noqa: E402  —— backend venv 自带（openai 的依赖）
import uvicorn        # noqa: E402
from fastapi import FastAPI, Request           # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# Mock 上游：可调延迟，记录自己看到的最大并发
# ---------------------------------------------------------------------------

def build_mock_upstream() -> FastAPI:
    app = FastAPI()
    app.state.inflight = 0
    app.state.max_inflight = 0
    app.state.calls = 0

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.json()
        app.state.inflight += 1
        app.state.max_inflight = max(app.state.max_inflight, app.state.inflight)
        app.state.calls += 1
        try:
            if body.get("stream"):
                async def gen():
                    for i in range(6):
                        await asyncio.sleep(0.2)
                        chunk = {"choices": [{"delta": {"content": f"分片{i}"},
                                              "finish_reason": None}]}
                        yield f"data: {json.dumps(chunk)}\n\n"
                    yield 'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
                    yield "data: [DONE]\n\n"
                return StreamingResponse(gen(), media_type="text/event-stream")
            await asyncio.sleep(1.2)
            return {
                "id": "mock", "model": body.get("model", "mock"),
                "choices": [{"message": {"role": "assistant", "content": "好"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            }
        finally:
            app.state.inflight -= 1

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": "mock-model", "object": "model"}]}

    @app.get("/stats")
    async def stats():
        return {"max_inflight": app.state.max_inflight, "calls": app.state.calls}

    return app


# ---------------------------------------------------------------------------
# 虚拟用户
# ---------------------------------------------------------------------------

class VUser:
    """一个已登录的虚拟用户（自带 Cookie 与 CSRF 头，互不串味）。"""

    def __init__(self, base: str):
        self.http = httpx.AsyncClient(base_url=base, timeout=60)
        self.csrf = ""

    async def login(self) -> float:
        t0 = time.perf_counter()
        r = await self.http.post("/api/v1/auth/login",
                                 json={"username": "admin", "password": ADMIN_PASSWORD})
        r.raise_for_status()
        self.csrf = self.http.cookies.get("pa_csrf") or ""
        return time.perf_counter() - t0

    async def call(self, method: str, path: str, **kw) -> tuple[float, int]:
        headers = dict(kw.pop("headers", {}) or {})
        if method not in ("GET", "HEAD"):
            headers["X-CSRF-Token"] = self.csrf
        t0 = time.perf_counter()
        r = await self.http.request(method, path, headers=headers, **kw)
        return time.perf_counter() - t0, r.status_code


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def report(name: str, values: list[float], errors: int = 0) -> str:
    if not values:
        return f"  {name:<22} 无样本"
    return (f"  {name:<22} n={len(values):<4} p50={pct(values, 0.5)*1000:6.0f}ms  "
            f"p95={pct(values, 0.95)*1000:6.0f}ms  max={max(values)*1000:6.0f}ms"
            + (f"  错误={errors}" if errors else ""))


async def main() -> None:
    data_dir = Path(tempfile.mkdtemp(prefix="pa-loadtest-"))
    app_port, mock_port = free_port(), free_port()
    mock_base = f"http://127.0.0.1:{mock_port}/v1"
    app_base = f"http://127.0.0.1:{app_port}"

    # --- mock 上游（本进程事件循环内） ---
    mock_app = build_mock_upstream()
    mock_server = uvicorn.Server(uvicorn.Config(
        mock_app, host="127.0.0.1", port=mock_port, log_level="error"))
    mock_task = asyncio.create_task(mock_server.serve())

    # --- 被测应用（子进程，贴近真实部署形态） ---
    env = dict(os.environ, DATA_DIR=str(data_dir), ADMIN_USERNAME="admin",
               ADMIN_PASSWORD=ADMIN_PASSWORD, COOKIE_SECURE="false",
               PORT=str(app_port), LLM_MAX_CONCURRENCY="3", LOG_LEVEL="WARNING")
    server = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(app_port), "--log-level", "warning"],
        cwd=ROOT / "backend", env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient() as probe:
            for _ in range(120):
                try:
                    r = await probe.get(f"{app_base}/api/v1/system/health", timeout=2)
                    if r.status_code == 200:
                        break
                except httpx.HTTPError:
                    await asyncio.sleep(0.5)
            else:
                raise RuntimeError("被测应用没起来")
        print(f"被测应用: {app_base}  mock 上游: {mock_base}  DATA_DIR: {data_dir}")

        # ================= A. 登录风暴 =================
        n_users = 20
        users = [VUser(app_base) for _ in range(n_users)]
        t0 = time.perf_counter()
        login_times = await asyncio.gather(*[u.login() for u in users])
        wall_a = time.perf_counter() - t0
        print(f"\n=== A. 登录风暴（{n_users} 并发，argon2id 64MiB/次）===")
        print(report("并发登录", list(login_times)))
        print(f"  墙钟总耗时 {wall_a:.1f}s（串行下限 ≈ {n_users} × 单次哈希）")

        # 配置 LLM 指向 mock（供 C 阶段）
        admin = users[0]
        _, code = await admin.call("PUT", "/api/v1/settings/llm", json={
            "base_url": mock_base, "api_key": "sk-loadtest", "model": "mock-model",
            "temperature": 0.3, "max_output_tokens": 4096, "context_window": 131072,
        })
        assert code == 200, f"配置 mock LLM 失败：{code}"

        # ================= B. 读写混合 =================
        print(f"\n=== B. 读写混合（{n_users} 用户 × 12s：列表/建案/详情/上传/交付物）===")
        lat: dict[str, list[float]] = {k: [] for k in
                                       ("列表", "建案", "详情", "消息", "上传(md)", "交付物")}
        errors = {"n": 0}
        md_payload = ("# 压测材料\n" + "这是一段用于压力测试的正文内容。" * 60).encode()

        async def browse(u: VUser, idx: int) -> None:
            deadline = time.perf_counter() + 12
            case_id = ""
            while time.perf_counter() < deadline:
                try:
                    d, c = await u.call("GET", "/api/v1/cases"); lat["列表"].append(d)
                    if c != 200: errors["n"] += 1
                    if not case_id:
                        d, c = await u.call("POST", "/api/v1/cases", json={
                            "module": "disclosure", "title": f"压测案件-{idx}"})
                        lat["建案"].append(d)
                        case_id = "" if c not in (200, 201) else "pending"
                        if case_id:
                            r = await u.http.get("/api/v1/cases")
                            case_id = next((it["id"] for it in r.json()["items"]
                                            if it["title"] == f"压测案件-{idx}"), "")
                    if case_id and case_id != "pending":
                        d, c = await u.call("GET", f"/api/v1/cases/{case_id}")
                        lat["详情"].append(d)
                        d, c = await u.call("GET", f"/api/v1/cases/{case_id}/messages")
                        lat["消息"].append(d)
                        d, c = await u.call(
                            "POST", f"/api/v1/cases/{case_id}/files",
                            files=[("files", (f"材料{idx}.md", md_payload, "text/markdown"))])
                        lat["上传(md)"].append(d)
                        if c != 201: errors["n"] += 1
                        d, c = await u.call("GET", f"/api/v1/cases/{case_id}/artifacts")
                        lat["交付物"].append(d)
                except httpx.HTTPError:
                    errors["n"] += 1
                await asyncio.sleep(0.05)

        await asyncio.gather(*[browse(u, i) for i, u in enumerate(users)])
        for name, values in lat.items():
            print(report(name, values))
        total_ops = sum(len(v) for v in lat.values())
        print(f"  合计 {total_ops} 次操作 / 12s ≈ {total_ops/12:.0f} ops/s，错误 {errors['n']}")

        # ================= C. 上游并发闸 =================
        print("\n=== C. 上游并发闸（8 路 LLM 调用 · 慢上游 1.2s/次 · 闸=3）===")
        read_lat: list[float] = []
        stop = asyncio.Event()

        async def read_probe(u: VUser) -> None:
            while not stop.is_set():
                d, _ = await u.call("GET", "/api/v1/cases")
                read_lat.append(d)
                await asyncio.sleep(0.1)

        probe_task = asyncio.create_task(read_probe(users[1]))
        t0 = time.perf_counter()
        results = await asyncio.gather(
            *[admin.call("POST", "/api/v1/settings/llm/test", json={}) for _ in range(8)])
        wall_c = time.perf_counter() - t0
        stop.set()
        await probe_task

        async with httpx.AsyncClient() as probe:
            stats = (await probe.get(f"http://127.0.0.1:{mock_port}/stats")).json()
        codes = [c for _, c in results]
        print(f"  8 路调用全部返回：{codes.count(200)}/8 成功，墙钟 {wall_c:.1f}s"
              f"（闸=3 × 1.2s ⇒ 理论 ≈ {8/3*1.2:.1f}s）")
        print(f"  上游实际看到的最大并发：{stats['max_inflight']}（上限 3）"
              + ("  ✓ 闸生效" if stats["max_inflight"] <= 3 else "  ✗ 闸未生效！"))
        print(report("LLM 满载期间纯读接口", read_lat))

        print("\n=== 未覆盖（测试环境比真实环境少的维度）===")
        print("  · 完整流水线蜂群（多步 prompt 路由桩的保真度成本高于结论价值）")
        print("  · 真实 LLM 上游的思维链长静默（会烧真额度；由生产 quirks 机制兜底）")
        print("  · 跨公网/隧道链路（本测全部走回环网卡）")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()
        mock_server.should_exit = True
        await asyncio.sleep(0.3)
        mock_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
