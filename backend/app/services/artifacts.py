"""交付物版本化落盘（M2）。

铁律（backend-architecture.md §2 artifacts 表 / §5 命名）：
- 只增不改、禁止覆盖：文件名 = {规范化案件名}_{YYYYMMDDHHmmss}.{ext}，
  若同名文件已存在则时间戳秒级 +1 直至不冲突；
- version = 同案件同 kind 的当前最大版本 +1（版本分配与插入在同一事务内）；
- 落盘目录 data/outputs/{case_id}/。

案件名规范化（§7.3）：
- 优先取 md 正文中「**案件名称**：xxx」行；
- 无则用传入 title；两者皆无（或为占位符）回落「未命名案件」；
- 去 Windows 非法字符与 markdown 装饰符，≤80 字符。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import anyio.to_thread
from ulid import ULID

from ..config import get_config
from ..db import database as db
from ..models.artifact import ArtifactOut

logger = logging.getLogger(__name__)

# 案件名行：「**案件名称**：xxx」（容忍粗体星号有无、全/半角冒号）
# 冒号后只吃同一行的空白（[^\S\n]），否则 \s* 会跨行把下一段正文当成案件名
_CASE_NAME_RE = re.compile(r"^[^\S\n]*\**[^\S\n]*案件名称[^\S\n]*\**[^\S\n]*[:：][^\S\n]*(.+?)[^\S\n]*$", re.MULTILINE)

# Windows 文件名非法字符 + 控制字符
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\r\n\t]')

# 占位符判定：整体为 X/×/口/下划线/空白，或含典型占位提示词
_PLACEHOLDER_FULL_RE = re.compile(r"^[Xx×口_\s]+$")
_PLACEHOLDER_HINTS = ("占位", "待填", "待定", "请填写", "此处填写", "TODO", "todo")

# 文件名最长字符数（不含扩展名与时间戳之外的部分整体约束）
MAX_NAME_LEN = 80

# 时间戳冲突时最多向后探测多少秒（防病态目录下的死循环）
MAX_TIMESTAMP_PROBES = 3600


def normalize_case_name(md_text: str | None = None, title: str | None = None) -> str:
    """规范化案件名：md 正文「**案件名称**：」行 → title → 「未命名案件」。"""
    raw: str | None = None
    if md_text:
        m = _CASE_NAME_RE.search(md_text)
        if m:
            raw = m.group(1)

    def clean(value: str | None) -> str:
        if not value:
            return ""
        # 去 markdown 装饰符与非法字符，收拢空白
        v = value.strip().strip("*`#").strip()
        v = _ILLEGAL_RE.sub("", v)
        v = re.sub(r"\s+", " ", v).strip().strip(".")
        # 占位符视为无效
        if not v or _PLACEHOLDER_FULL_RE.match(v):
            return ""
        if any(h in v for h in _PLACEHOLDER_HINTS):
            return ""
        return v

    name = clean(raw) or clean(title) or "未命名案件"
    return name[:MAX_NAME_LEN]


def _write_new_output(out_dir: Path, name: str, ext: str, payload: bytes) -> tuple[Path, str]:
    """原子占位并写入：{name}_{YYYYMMDDHHmmss}.{ext}；同名则秒级 +1 直至成功。

    必须用 'xb'（O_CREAT|O_EXCL）一步完成「占名 + 写入」——先 exists() 再 write 的写法
    存在 TOCTOU 竞态：两个并发落盘（例如用户连点两次导出、或流水线并行落多份交付物）
    会拿到同一个文件名互相覆盖，DB 里留下多条版本记录却只剩最后一份内容，
    直接违反 §2「只增不改、禁止覆盖」。
    """
    ts = datetime.now()
    for _ in range(MAX_TIMESTAMP_PROBES):
        filename = f"{name}_{ts.strftime('%Y%m%d%H%M%S')}.{ext}"
        path = out_dir / filename
        try:
            # 'x' = O_CREAT|O_EXCL：Path.write_bytes 没有这个语义，只能用内置 open
            with open(path, "xb") as fh:
                fh.write(payload)
        except FileExistsError:
            ts += timedelta(seconds=1)  # 禁覆盖：同秒冲突则推后一秒重试
            continue
        return path, filename
    raise RuntimeError(
        f"交付物落盘失败：目录 {out_dir} 内连续 {MAX_TIMESTAMP_PROBES} 个时间戳文件名都已被占用。"
    )


def save_artifact_sync(
    case_id: str,
    kind: str,
    data: bytes | str,
    ext: str,
    *,
    title: str | None = None,
    md_text: str | None = None,
    run_group: str | None = None,
    iteration_type: str | None = None,
    summary: str | None = None,
    source_artifact_id: str | None = None,
) -> ArtifactOut:
    """落盘并登记一件交付物（同步）。返回 ArtifactOut。

    - data 为 str 时按 UTF-8 编码写入；
    - md_text 未传且 ext 为 md 时，自动用 data 正文提取「案件名称」；
    - version 分配与 INSERT 在同一事务内，防并发跳号。
    """
    if isinstance(data, str):
        payload = data.encode("utf-8")
    else:
        payload = data
    ext = ext.lstrip(".").lower() or "bin"

    if md_text is None and ext == "md":
        try:
            md_text = payload.decode("utf-8")
        except UnicodeDecodeError:
            md_text = None

    name = normalize_case_name(md_text, title)
    cfg = get_config()
    out_dir = cfg.outputs_dir / case_id
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path, filename = _write_new_output(out_dir, name, ext, payload)
    except OSError as exc:
        raise OSError(
            f"交付物无法写入磁盘（目录 {out_dir}）：{exc}。"
            "请检查数据目录是否存在、是否有写入权限、磁盘空间是否充足。"
        ) from exc

    artifact_id = str(ULID())
    created_at = db.now_str()

    def op(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM artifacts WHERE case_id=? AND kind=?",
            (case_id, kind),
        ).fetchone()
        version = int(row[0]) + 1
        conn.execute(
            """
            INSERT INTO artifacts(id, case_id, version, kind, filename, stored_path,
                                  run_group, iteration_type, summary, source_artifact_id, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                artifact_id, case_id, version, kind, filename, str(path),
                run_group, iteration_type, summary, source_artifact_id, created_at,
            ),
        )
        return version

    try:
        version = db.transaction(op)
    except Exception:
        # 入库失败时清掉已写文件，避免孤儿
        path.unlink(missing_ok=True)
        raise

    logger.info("交付物已落盘：case=%s kind=%s v%s → %s", case_id, kind, version, filename)
    return ArtifactOut(
        id=artifact_id, case_id=case_id, version=version, kind=kind,  # type: ignore[arg-type]
        filename=filename, stored_path=str(path), run_group=run_group,
        iteration_type=iteration_type, summary=summary,  # type: ignore[arg-type]
        source_artifact_id=source_artifact_id, created_at=created_at,
    )


async def save_artifact(
    case_id: str,
    kind: str,
    data: bytes | str,
    ext: str,
    **kwargs,
) -> ArtifactOut:
    """save_artifact_sync 的异步包装。"""
    return await anyio.to_thread.run_sync(
        lambda: save_artifact_sync(case_id, kind, data, ext, **kwargs)
    )


def strip_timestamp(filename: str) -> str:
    """从交付物文件名去掉 _{YYYYMMDDHHmmss} 后缀，得到案件名（导出续名用）。"""
    stem = Path(filename).stem
    return re.sub(r"_\d{14}$", "", stem)
