# -*- coding: utf-8 -*-
"""数据目录内的路径存取：**入库存相对，读盘再拼绝对**。

**为什么必须这样。**
此前 `files.stored_path` / `files.md_path` / `artifacts.stored_path` 存的是绝对路径，
形如 `C:\\Users\\jielu\\Desktop\\PatentAgent\\data\\outputs\\…`。这在原机上没问题，
一旦把备份恢复到别的目录 / 别的机器就全断：

- 下载 404、预览 404、正文里的插图和说明书附图变成裂图；
- 而且**全都是静默的**——数据库好好的、案件列表好好的、正文好好的，
  只有点下去才发现打不开。灾备最怕的就是这种「看起来恢复成功了」。

所以路径的存储形态与它所在的机器解耦：库里只留 `outputs/<case>/<file>` 这样的
相对路径，绝对路径在读的那一刻由当前 `DATA_DIR` 拼出来。

**旧数据怎么办。**
`resolve()` 认三种形态：相对路径、当前机器上仍然有效的绝对路径、以及**换了根目录的
绝对路径**——最后一种按 `uploads/` 或 `outputs/` 这个锚点截断后重新拼。
启动时另有一次 `normalize_stored_paths()` 把库里的旧值就地改成相对路径，
一次就够，之后库本身就是可搬的。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..config import get_config

logger = logging.getLogger(__name__)

# 数据目录下的顶层分区。既是 to_stored 的判定依据，也是旧绝对路径的重定位锚点。
_ANCHORS = ("uploads", "outputs", "tmp")


def data_dir() -> Path:
    return get_config().data_dir


def to_stored(path: Path | str) -> str:
    """磁盘路径 → 入库形态。

    在数据目录内 → 相对 `DATA_DIR` 的 POSIX 路径（`outputs/<case>/x.docx`）。
    用 POSIX 分隔符是为了让同一个库在 Windows 与 Linux 之间可搬；
    读回时 `Path()` 两边都认。

    不在数据目录内的（理论上不该出现）原样存绝对路径并告警——
    悄悄存下去只会让它在换机时变成一条查不出成因的裂图。
    """
    p = Path(path)
    try:
        rel = p.resolve().relative_to(data_dir().resolve())
    except (ValueError, OSError):
        logger.warning("路径不在数据目录内，仍按绝对路径入库（换机恢复后会失效）：%s", p)
        return str(p)
    return rel.as_posix()


def _relocate(raw: str) -> Path | None:
    """把一条「别的机器上的绝对路径」重新落到当前数据目录。

    按 `uploads/` `outputs/` `tmp/` 这几个锚点截断——它们是数据目录的顶层分区，
    锚点右边的部分（案件 id / 文件名）与机器无关。

    源库可能来自 Windows，而当前进程可能在 Linux 上（反之亦然），
    所以两种分隔符都要拆。
    """
    for cls in (PureWindowsPath, PurePosixPath):
        parts = cls(raw).parts
        # 从右往左找：路径里可能有别的同名目录（如 …/backup/outputs/…），
        # 靠右的那个才是真正的分区锚点
        for i in range(len(parts) - 1, -1, -1):
            token = parts[i].strip("\\/").lower()
            if token in _ANCHORS and i + 1 < len(parts):
                return data_dir().joinpath(*parts[i:])
    return None


def resolve(value: str | Path | None) -> Path | None:
    """入库形态 → 可用的磁盘路径；无从落实时返回 None。

    次序：相对路径拼当前数据目录 → 绝对路径本身仍存在 → 按锚点重定位。
    三条都不成立才返回 None（调用方据此报「文件已不在磁盘」）。
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    p = Path(raw)
    if not p.is_absolute():
        candidate = data_dir() / p
        if candidate.exists():
            return candidate
        # 不存在也返回它：调用方要拿这个路径去报错 / 去创建
        relocated = _relocate(raw)
        return relocated if relocated is not None and relocated.exists() else candidate

    if p.exists():
        return p

    relocated = _relocate(raw)
    if relocated is not None and relocated.exists():
        logger.info("按数据目录重定位了一条旧的绝对路径：%s → %s", raw, relocated)
        return relocated
    return p


def resolve_existing(value: str | Path | None) -> Path | None:
    """同 resolve，但文件不存在时返回 None —— 给「取不到就 404」的读路径用。"""
    path = resolve(value)
    return path if path is not None and path.is_file() else None


# ---------------------------------------------------------------------------
# 启动时的一次性归一
# ---------------------------------------------------------------------------

# (表, 列)。三列都是「数据目录内某个文件」的指针。
_PATH_COLUMNS = (
    ("files", "stored_path"),
    ("files", "md_path"),
    ("artifacts", "stored_path"),
)


def normalize_stored_paths(conn: sqlite3.Connection) -> dict[str, int]:
    """把库里的绝对路径就地改成相对 `DATA_DIR` 的形态。

    幂等：已是相对路径的行不动。落不到当前数据目录内的行也不动——
    那种行改不出正确答案，硬改只会把「路径可疑」变成「路径错得理直气壮」，
    留着原值至少 `resolve()` 还能按锚点救一次。
    """
    stats = {"scanned": 0, "rewritten": 0, "left_alone": 0}
    root = data_dir().resolve()

    for table, column in _PATH_COLUMNS:
        rows = conn.execute(
            f"SELECT id, {column} AS value FROM {table} "  # noqa: S608 列名来自本模块常量
            f"WHERE {column} IS NOT NULL AND {column} != ''"
        ).fetchall()
        for row in rows:
            raw = str(row["value"])
            stats["scanned"] += 1
            if not Path(raw).is_absolute():
                continue
            try:
                rel = Path(raw).resolve().relative_to(root)
            except (ValueError, OSError):
                relocated = _relocate(raw)
                if relocated is None:
                    stats["left_alone"] += 1
                    continue
                try:
                    rel = relocated.resolve().relative_to(root)
                except (ValueError, OSError):
                    stats["left_alone"] += 1
                    continue
            conn.execute(
                f"UPDATE {table} SET {column}=? WHERE id=?",  # noqa: S608 同上
                (rel.as_posix(), row["id"]),
            )
            stats["rewritten"] += 1

    return stats
