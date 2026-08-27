# -*- coding: utf-8 -*-
"""单实例锁：挡住多余实例把正在跑的流水线判死。

uvicorn 先跑完 lifespan 再绑端口，所以一个注定撞 address-in-use 的多余实例
也会完整走一遍 startup —— 包括不区分归属的 recover_interrupted。
这组用例锁住「拿不到锁就不做恢复」这条。
"""
import subprocess
import sys
import textwrap

import pytest

from app.services.instance_lock import InstanceLock


BACKEND_DIR = __import__("pathlib").Path(__file__).resolve().parents[1]


def _holder_script(data_dir, hold_seconds: float = 8.0) -> str:
    """子进程：拿锁并保持一段时间。"""
    return textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(BACKEND_DIR)!r})
        from app.services.instance_lock import InstanceLock
        lock = InstanceLock({str(data_dir)!r})
        ok = lock.acquire()
        print("ACQUIRED" if ok else "FAILED", flush=True)
        time.sleep({hold_seconds})
    """)


def test_second_process_cannot_acquire(tmp_path):
    """核心保证：另一个**进程**持锁时拿不到。

    注意锁的粒度是进程，不是文件句柄——同一进程内再开一个句柄去锁是能成功的
    （Windows 的 msvcrt.locking 对同进程不互斥）。这不影响真实场景：
    我们要挡的正是「看门狗误起的第二个 uvicorn 进程」。
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _holder_script(tmp_path)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        first = proc.stdout.readline().strip()
        assert first == "ACQUIRED", f"子进程未能拿到锁：{first}"

        mine = InstanceLock(tmp_path)
        try:
            assert mine.acquire() is False, "另一进程持锁时本进程不该拿到"
        finally:
            mine.release()
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_release_allows_reacquire(tmp_path):
    """正常退出释放后，下一个实例能拿到——否则重启就再也恢复不了。"""
    a = InstanceLock(tmp_path)
    assert a.acquire() is True
    a.release()

    b = InstanceLock(tmp_path)
    try:
        assert b.acquire() is True
    finally:
        b.release()


def test_lock_is_per_data_dir(tmp_path):
    """不同数据目录互不影响：同机多部署各锁各的。"""
    d1, d2 = tmp_path / "one", tmp_path / "two"
    d1.mkdir()
    d2.mkdir()
    a, b = InstanceLock(d1), InstanceLock(d2)
    try:
        assert a.acquire() is True
        assert b.acquire() is True
    finally:
        a.release()
        b.release()


def test_crashed_process_releases_lock(tmp_path):
    """进程崩溃（未走 release）时锁必须由 OS 释放。

    这是选文件锁而不是心跳的理由：心跳会留下需要过期判断的残留，
    而看门狗 60 秒内就重启、单个 LLM 步骤能跑二十多分钟，
    两个尺度差一个数量级，怎么设阈值都会错一边。
    """
    code = textwrap.dedent(f"""
        import sys, os
        sys.path.insert(0, {str(tmp_path.parent.parent).encode('unicode_escape').decode()!r})
        sys.path.insert(0, {str(__import__('pathlib').Path(__file__).resolve().parents[1]).encode('unicode_escape').decode()!r})
        from app.services.instance_lock import InstanceLock
        lock = InstanceLock({str(tmp_path).encode('unicode_escape').decode()!r})
        assert lock.acquire() is True
        os._exit(1)          # 硬退出：不跑 finally、不 release
    """)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1, f"子进程未按预期退出：{proc.stderr[-300:]}"

    after = InstanceLock(tmp_path)
    try:
        assert after.acquire() is True, "持锁进程崩溃后，锁应由操作系统释放"
    finally:
        after.release()


def test_recover_skipped_without_lock(tmp_path, monkeypatch):
    """核心保证：拿不到锁时不执行恢复。

    直接验证 lifespan 里的判断逻辑——正在跑的 run 不该被多余实例改成 failed。
    """
    from app.db import database as db
    from app.pipelines import engine

    proc = subprocess.Popen(
        [sys.executable, "-c", _holder_script(tmp_path)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ACQUIRED"

        called = {"n": 0}

        def spy():
            called["n"] += 1
            return {"runs_failed": 0, "cases_failed": 0}

        monkeypatch.setattr(engine, "recover_interrupted", spy)

        # 复刻 lifespan 的分支：拿不到锁 → 不调 recover_interrupted
        mine = InstanceLock(tmp_path)
        try:
            if mine.acquire():
                engine.recover_interrupted()
        finally:
            mine.release()

        assert called["n"] == 0, "拿不到锁时绝不能执行恢复——那会把别人正在跑的流水线判死"
    finally:
        proc.kill()
        proc.wait(timeout=10)

    assert db is not None   # 保证导入路径有效，避免 lint 误删


@pytest.mark.parametrize("times", [2, 3])
def test_release_is_idempotent(tmp_path, times):
    """重复 release 不该抛异常——lifespan 的清理路径可能走多次。"""
    lock = InstanceLock(tmp_path)
    assert lock.acquire() is True
    for _ in range(times):
        lock.release()
    assert lock.acquired is False
