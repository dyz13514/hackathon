"""`app/settings.py` 的校验行为。

守的是 R23.11 与 design.md Architecture §4 的一条硬性质：**必需环境变量缺失即拒绝
启动**。这条性质如果失效，进程会带着「默认口令」之类的东西起来，而失败会推迟到
第一个写请求——那时才发现远比启动时发现昂贵。
"""

from __future__ import annotations

import pytest

from app.settings import ConfigurationError, Settings, get_settings, load_settings


def test_valid_env_loads(valid_env: pytest.MonkeyPatch) -> None:
    loaded = load_settings()
    assert loaded.database_url == "sqlite:///:memory:"
    assert loaded.llm_mode == "STUB"
    assert loaded.app_env == "TEST"


def test_missing_required_vars_refuse_start(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    # 三个必需变量都应被点名，运维不必逐个试错
    assert "DATABASE_URL" in message
    assert "SESSION_SHARED_PASSWORD" in message
    assert "SESSION_SECRET_KEY" in message


@pytest.mark.parametrize(
    "missing",
    ["DATABASE_URL", "SESSION_SHARED_PASSWORD", "SESSION_SECRET_KEY"],
)
def test_each_required_var_is_individually_required(
    valid_env: pytest.MonkeyPatch, missing: str
) -> None:
    valid_env.delenv(missing)
    with pytest.raises(ConfigurationError, match=missing):
        load_settings()


def test_short_secret_key_rejected(valid_env: pytest.MonkeyPatch) -> None:
    valid_env.setenv("SESSION_SECRET_KEY", "too-short")
    with pytest.raises(ConfigurationError, match="SESSION_SECRET_KEY"):
        load_settings()


def test_live_mode_requires_gateway_credentials(valid_env: pytest.MonkeyPatch) -> None:
    valid_env.setenv("LLM_MODE", "LIVE")
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert "BEDROCK_GATEWAY_URL" in message
    assert "BEDROCK_API_KEY" in message


def test_live_mode_accepted_when_credentials_present(
    valid_env: pytest.MonkeyPatch,
) -> None:
    valid_env.setenv("LLM_MODE", "LIVE")
    valid_env.setenv("BEDROCK_GATEWAY_URL", "https://gateway.example/invoke")
    valid_env.setenv("BEDROCK_API_KEY", "k")
    assert load_settings().llm_mode == "LIVE"


def test_unknown_llm_mode_rejected(valid_env: pytest.MonkeyPatch) -> None:
    valid_env.setenv("LLM_MODE", "YOLO")
    with pytest.raises(ConfigurationError, match="LLM_MODE"):
        load_settings()


def test_secrets_are_not_exposed_in_repr(settings: Settings) -> None:
    """R23.10：凭证不得随对象打印泄漏到日志。"""
    rendered = repr(settings)
    assert "test-shared-password" not in rendered
    assert "test-secret-key-that-is-long-enough-32" not in rendered


def test_comma_separated_cors_origins(valid_env: pytest.MonkeyPatch) -> None:
    valid_env.setenv(
        "CORS_ALLOW_ORIGINS", "http://localhost:5173, https://demo.example "
    )
    assert load_settings().cors_origins == (
        "http://localhost:5173",
        "https://demo.example",
    )


def test_get_settings_is_cached(valid_env: pytest.MonkeyPatch) -> None:
    assert get_settings() is get_settings()
