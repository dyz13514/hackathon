"""测试期共享夹具。

两条纪律在此落成：

1. **测试永不消耗 Bedrock 额度**——`LLM_MODE` 强制为 `STUB`（Testing Strategy §2
   第 5 条：属性测试中 `LLM_MODE` 恒为 `STUB` 或 `DISABLED`）。
2. **「配置缺失即拒绝启动」这条性质本身要可测**——因此不在 import 期污染
   `os.environ`，而是用 fixture 显式设置，并把工作目录切到临时目录，使开发机上
   真实存在的 `.env` 不会影响结果。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.settings import Settings, get_settings

#: 一组合法的最小配置。密钥长度满足 `MIN_SECRET_KEY_LENGTH`。
VALID_ENV: dict[str, str] = {
    "DATABASE_URL": "sqlite:///:memory:",
    "SESSION_SHARED_PASSWORD": "test-shared-password",
    "SESSION_SECRET_KEY": "test-secret-key-that-is-long-enough-32",
    "LLM_MODE": "STUB",
    "APP_ENV": "TEST",
}

#: settings 读取的全部环境变量名，用于测试前清场。
SETTING_ENV_NAMES: tuple[str, ...] = (
    "DATABASE_URL",
    "SESSION_SHARED_PASSWORD",
    "SESSION_SECRET_KEY",
    "LLM_MODE",
    "APP_ENV",
    "LOG_LEVEL",
    "CORS_ALLOW_ORIGINS",
    "UPLOAD_DIR",
    "BEDROCK_GATEWAY_URL",
    "BEDROCK_API_KEY",
)


@pytest.fixture
def clean_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[pytest.MonkeyPatch]:
    """清空全部配置环境变量、切到无 `.env` 的临时目录，并让配置缓存失效。"""
    for name in SETTING_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
def valid_env(clean_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """在清场后注入一组合法配置。"""
    for name, value in VALID_ENV.items():
        clean_env.setenv(name, value)
    return clean_env


@pytest.fixture
def settings(valid_env: pytest.MonkeyPatch) -> Settings:
    """合法配置下的 `Settings` 实例。"""
    return Settings()  # type: ignore[call-arg]  # 值由环境变量提供
