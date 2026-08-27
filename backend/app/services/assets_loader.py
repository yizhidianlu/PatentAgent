"""prompt / reference 资产加载器。

- 按相对 key 读取 backend/app/assets/ 下的文本文件（UTF-8），带 lru 缓存；
- 支持解析 YAML front-matter 元数据（source_repo/source_path/treatment 等）；
  M1 阶段调用方只需原文（get_raw / get_text）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"


@dataclass(frozen=True)
class Asset:
    """一份资产文本：front-matter 元数据 + 正文。"""

    key: str
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""


def _resolve(key: str) -> Path:
    """把相对 key 解析为 assets 下的绝对路径；越界或不存在抛 KeyError。"""
    candidate = (ASSETS_DIR / key).resolve()
    # 防路径穿越：必须仍在 assets 目录内
    if ASSETS_DIR != candidate and ASSETS_DIR not in candidate.parents:
        raise KeyError(f"非法资产 key（越出 assets 目录）：{key}")
    if candidate.is_file():
        return candidate
    # 容忍省略 .md 后缀的 key
    with_md = candidate.with_suffix(candidate.suffix + ".md") if not candidate.suffix else None
    if with_md is not None and with_md.is_file():
        return with_md
    raise KeyError(f"资产不存在：{key}")


def _split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """解析文首 YAML front-matter（--- 包裹）；无或解析失败时 meta 为空、正文为全文。"""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            raw = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:])
            try:
                meta = yaml.safe_load(raw)
                if not isinstance(meta, dict):
                    meta = {}
            except yaml.YAMLError:
                return {}, text
            return meta, body.lstrip("\n")
    return {}, text


@lru_cache(maxsize=256)
def get_raw(key: str) -> str:
    """按 key 读取资产原文（含 front-matter，UTF-8）。"""
    return _resolve(key).read_text(encoding="utf-8")


@lru_cache(maxsize=256)
def get_asset(key: str) -> Asset:
    """按 key 读取资产并解析 front-matter。"""
    text = get_raw(key)
    meta, body = _split_front_matter(text)
    return Asset(key=key, meta=meta, body=body)


def get_text(key: str) -> str:
    """按 key 读取资产正文（剥离 front-matter）。"""
    return get_asset(key).body


def list_assets(prefix: str = "") -> list[str]:
    """列出某前缀下所有资产的相对 key（正斜杠分隔）。"""
    base = ASSETS_DIR / prefix if prefix else ASSETS_DIR
    if not base.is_dir():
        return []
    keys = []
    for p in sorted(base.rglob("*")):
        if p.is_file():
            keys.append(p.relative_to(ASSETS_DIR).as_posix())
    return keys


def clear_cache() -> None:
    """清空缓存（资产文件被修改后调用）。"""
    get_raw.cache_clear()
    get_asset.cache_clear()
