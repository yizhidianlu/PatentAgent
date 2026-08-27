# -*- coding: utf-8 -*-
"""单实例锁：判断「本进程是不是这份数据目录上唯一活着的应用实例」。

**为什么需要它。**
uvicorn 的启动顺序是先跑完 lifespan.startup()、再绑端口。所以当看门狗在旧进程
仍活着时误起第二个实例，那个实例即便注定撞 address-in-use 退出，**也已经完整跑过
一次 startup**——其中包括 `recover_interrupted()`，两条无差别 UPDATE：

    UPDATE pipeline_runs SET status='failed', error='interrupted' WHERE status='running'
    UPDATE cases        SET status='failed'                        WHERE status='running'

它没有任何「这条 run 属于哪个进程」的概念，WAL 模式下外来进程的写入也不会被挡。
于是用户正在跑的流水线被一个马上就要退出的进程判死——而后台真正在跑的那个进程
还在继续往这条已被判死的记录里写。几十分钟的产出就此报废，且 failed 不是
waiting_user，走不了 resume。

**为什么用文件锁而不是别的判据。**
- 时间窗（心跳是否新鲜）不行：单个 LLM 步骤可以跑二十多分钟不写库，而看门狗
  在 60 秒内就会重启，两个尺度差了一个数量级，怎么取阈值都会错一边。
- 端口探测不行：lifespan 阶段拿不到 uvicorn 实际绑定的端口（--port 是命令行传的，
  未必等于配置里的 port）。
- 文件锁的语义恰好是我们要的：**拿到 = 我是唯一实例**，而锁由操作系统在进程退出时
  释放，正常退出与崩溃都算——不会像心跳那样留下需要过期判断的残留。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK_NAME = "instance.lock"


class InstanceLock:
    """数据目录级的独占锁。

    用法：
        lock = InstanceLock(data_dir)
        if lock.acquire():
            ...   # 我是唯一实例，可以做恢复这类破坏性操作
        lock.release()
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / _LOCK_NAME
        self._fh = None
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self) -> bool:
        """尝试拿独占锁。拿不到返回 False（说明另一个实例正活着），不抛异常。

        拿锁失败绝不能让应用起不来：第二实例仍需正常退出流程，
        而且锁机制本身出问题时，应用可用性优先于恢复钩子。
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 必须是**二进制**模式。文本模式下 Python 的 io 层有自己的缓冲，
            # seek(0) 与底层文件描述符的实际位置不同步，而 msvcrt.locking 锁的正是
            # 「从 fd 当前位置起的 N 字节」——实测文本模式下两个进程各锁各的位置，
            # 谁也挡不住谁，锁形同虚设。
            # r+b 要求文件存在，所以先确保它在（不截断：别人可能正持着锁）。
            if not self.path.exists():
                self.path.touch()
            self._fh = open(self.path, "r+b")
        except OSError as exc:
            logger.warning("无法打开实例锁文件 %s：%s；按「非唯一实例」处理", self.path, exc)
            return False

        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # 锁被别人持有 —— 这是预期路径，不是错误
            self._close()
            return False
        except Exception as exc:  # noqa: BLE001  锁不可用时宁可放行，不阻断启动
            logger.warning("实例锁不可用（%s）；按「非唯一实例」处理以免误判死正在跑的流水线", exc)
            self._close()
            return False

        self._acquired = True
        try:
            # 锁住的是第 0 字节，不能 truncate 掉它、也不能覆写它。
            # pid 写在其后，仅作排障线索；写完把指针复位——release 要在同一位置解锁。
            self._fh.seek(1)
            self._fh.truncate(1)
            self._fh.write(f" pid={os.getpid()}\n".encode("utf-8"))
            self._fh.flush()
            self._fh.seek(0)
        except OSError:
            pass   # 写不进去不影响锁本身
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        if self._acquired:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001  进程退出时 OS 也会释放，这里失败无妨
                pass
        self._acquired = False
        self._close()

    def _close(self) -> None:
        try:
            if self._fh is not None:
                self._fh.close()
        except OSError:
            pass
        self._fh = None
