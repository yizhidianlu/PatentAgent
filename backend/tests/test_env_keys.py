# -*- coding: utf-8 -*-
"""backend/.env 中未知配置键的检测。

``extra="ignore"`` 让拼错的键被静默丢弃：把 COOKIE_SECURE 写成
SESSION_COOKIE_SECURE，得到的是一个 Cookie 不带 Secure 的生产环境，
而日志里一句提示都没有。这组用例锁住启动时的告警行为。
"""
from pathlib import Path

import pytest

from app import config as config_mod


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """把 .env 的探测路径指到临时目录。"""
    backend = tmp_path / "backend"
    backend.mkdir()
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", tmp_path)
    return backend / ".env"


def test_no_env_file(env_file):
    assert config_mod.unknown_env_keys() == []


def test_known_keys_pass(env_file: Path):
    env_file.write_text(
        "DATA_DIR=D:\\PatentAgentData\nPORT=8000\nCOOKIE_SECURE=true\n",
        encoding="utf-8",
    )
    assert config_mod.unknown_env_keys() == []


def test_typo_is_reported(env_file: Path):
    """真实踩过的坑：SESSION_COOKIE_SECURE 不是有效键，会被静默忽略。"""
    env_file.write_text("SESSION_COOKIE_SECURE=true\n", encoding="utf-8")
    assert config_mod.unknown_env_keys() == ["SESSION_COOKIE_SECURE"]


def test_case_insensitive(env_file: Path):
    """配置本身 case_sensitive=False，小写写法同样有效，不该误报。"""
    env_file.write_text("cookie_secure=true\nport=9000\n", encoding="utf-8")
    assert config_mod.unknown_env_keys() == []


def test_comments_and_blanks_ignored(env_file: Path):
    env_file.write_text(
        "# COOKIE_SECURE_TYPO=1\n\n   \nPORT=8000\n",
        encoding="utf-8",
    )
    assert config_mod.unknown_env_keys() == []


def test_duplicates_reported_once(env_file: Path):
    env_file.write_text("BOGUS=1\nBOGUS=2\nALSO_BOGUS=3\n", encoding="utf-8")
    assert config_mod.unknown_env_keys() == ["BOGUS", "ALSO_BOGUS"]
