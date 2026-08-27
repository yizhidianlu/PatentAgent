# -*- coding: utf-8 -*-
"""未认证端点的进程内限流。

**为什么注册这条路必须限流。**
密码哈希用的是 argon2id（time_cost=3, memory_cost=64MiB, parallelism=4），单次约 50ms、
吃 64 MiB。而 `db.arun` → `anyio.to_thread.run_sync` 全应用共用一个默认线程限流器
（40 个槽位）。匿名请求每次换个新用户名（3-32 位字母数字，取之不尽）打进来，
几百条并发就能把槽位占满、常驻内存推到 GB 级——**而真实客户的每一个接口
（案件列表、上传、流水线状态）都要经同一个 `db.arun` 拿槽位**，攻击持续期间整站超时。

这不是理论：单进程、无 `--limit-concurrency`、Cloudflare Access 未启用时，
中间没有任何一层能挡。

进程内实现足够：本平台是单进程部署（Word COM 要求非 Session 0，起不了多 worker）。
多进程部署时这层会退化成「每进程各限各的」，那时应当换成共享存储或前置网关。
"""
from __future__ import annotations

import threading
import time
from collections import deque

# 每个来源在窗口内允许的次数
DEFAULT_LIMIT = 5
DEFAULT_WINDOW_SEC = 300.0

# 记录的来源上限：防止攻击者用海量伪造 IP 把这张表本身撑成内存泄漏
MAX_TRACKED_KEYS = 4096


class SlidingWindowLimiter:
    """按 key 的滑动窗口计数器。"""

    def __init__(self, limit: int = DEFAULT_LIMIT, window_sec: float = DEFAULT_WINDOW_SEC) -> None:
        self.limit = limit
        self.window = window_sec
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """返回 (是否放行, 需等待秒数)。放行时同时记一次。"""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                if len(self._hits) >= MAX_TRACKED_KEYS:
                    self._evict(now)
                bucket = deque()
                self._hits[key] = bucket

            while bucket and now - bucket[0] > self.window:
                bucket.popleft()

            if len(bucket) >= self.limit:
                retry_after = int(self.window - (now - bucket[0])) + 1
                return False, max(1, retry_after)

            bucket.append(now)
            return True, 0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def _evict(self, now: float) -> None:
        """清掉窗口已空的 key；仍清不出空间就丢最旧的一批。

        调用方已持锁。
        """
        stale = [k for k, b in self._hits.items() if not b or now - b[-1] > self.window]
        for k in stale:
            self._hits.pop(k, None)
        if len(self._hits) >= MAX_TRACKED_KEYS:
            oldest = sorted(self._hits.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)
            for k, _ in oldest[: MAX_TRACKED_KEYS // 4]:
                self._hits.pop(k, None)


# 注册：5 次 / 5 分钟 / 每来源。正常人注册一次就够，连试五次已经很宽松了。
registration_limiter = SlidingWindowLimiter(limit=5, window_sec=300.0)
