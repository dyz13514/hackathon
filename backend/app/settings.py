"""环境变量校验：缺失即拒绝启动。

依据：design.md Architecture §4「凭证只经环境变量注入（R23.11）；`.env` 在 `.gitignore`
中；启动时校验必需环境变量存在，缺失则拒绝启动」。

两条硬性质：

1. **缺失即拒绝启动**——`get_settings()` 在必需变量缺失时抛 `ConfigurationError`，
   `app.main.create_app()` 因此无法装配。不提供「用默认值凑合」的分支。
2. **凭证不可被日志或响应带出**——口令与密钥类字段一律 `SecretStr`，其 `repr` 为
   `**********`。任务 1.7 的 `tests/structure/test_no_secret_in_logs.py` 会对此断言
   （R23.10）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# LLM 模式的取值域。规范的 `LlmMode` 枚举随 `llm/adapter.py` 落地（任务 5.3）；
# 此处只做取值校验，避免 settings 反向依赖 llm 层。
LlmModeName = Literal["LIVE", "REPLAY", "STUB", "DISABLED"]

#: 会话签名密钥的最小长度。短密钥在演示环境同样不可接受。
MIN_SECRET_KEY_LENGTH = 32


class ConfigurationError(RuntimeError):
    """必需环境变量缺失或取值非法。抛出即意味着进程不应继续启动。"""


class Settings(BaseSettings):
    """全部运行期配置的唯一来源。

    没有 `os.environ` 的第二个读取点：其他模块一律经 `get_settings()` 取值，
    这样「哪些变量是必需的」这个问题只有一个答案。
    """

    # 两个 `.env` 候选位置：仓库根（`make dev` 与部署用）与 `backend/`（手工从
    # backend 目录启动时用）。后者优先。两者都不存在时，值只能来自真实环境变量。
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- 必需 ---
    database_url: str = Field(
        ...,
        description="SQLAlchemy 连接串。SQLite 或 PostgreSQL（R27.4 兼容性要求）",
    )
    session_shared_password: SecretStr = Field(
        ...,
        description="演示环境的单一共享口令（Session_Auth，任务 1.5）",
    )
    session_secret_key: SecretStr = Field(
        ...,
        description="会话令牌签名密钥，≥32 字符",
    )

    # --- 可选，有安全默认 ---
    app_env: Literal["LOCAL", "DEMO", "TEST"] = "LOCAL"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    llm_mode: LlmModeName = Field(
        default="REPLAY",
        description="默认 REPLAY：构建期与 CI 不消耗 Bedrock 额度（成本纪律，任务 5.4）",
    )
    # 声明为 `str` 而非 `tuple[str, ...]` 是刻意的：pydantic-settings 对复合类型字段
    # 会先按 JSON 解析环境变量，`CORS_ALLOW_ORIGINS=http://a,http://b` 这种朴素写法
    # 会在解析阶段直接报错。原始串留给 `cors_origins` 属性切分。
    cors_allow_origins: str = "http://localhost:5173"
    upload_dir: Path = Path("./var/uploads")

    # --- 仅 LLM_MODE=LIVE 时必需 ---
    bedrock_gateway_url: str | None = None
    bedrock_api_key: SecretStr | None = None
    bedrock_model: str = "sonnet4.5:latest"

    @property
    def cors_origins(self) -> tuple[str, ...]:
        """`CORS_ALLOW_ORIGINS` 的逗号分隔形式切分结果。"""
        return tuple(
            item.strip() for item in self.cors_allow_origins.split(",") if item.strip()
        )

    @field_validator("database_url")
    @classmethod
    def _non_empty_database_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("DATABASE_URL 不能为空字符串")
        return value.strip()

    @model_validator(mode="after")
    def _check_secret_strength(self) -> Settings:
        if len(self.session_secret_key.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"SESSION_SECRET_KEY 至少需要 {MIN_SECRET_KEY_LENGTH} 个字符"
            )
        if not self.session_shared_password.get_secret_value():
            raise ValueError("SESSION_SHARED_PASSWORD 不能为空")
        return self

    @model_validator(mode="after")
    def _check_live_mode_credentials(self) -> Settings:
        """`LLM_MODE=LIVE` 需要网关地址与密钥；缺一即拒绝启动。

        把这条放在启动期而不是首次调用处，是为了让「配置错了」在演示开始前就暴露，
        而不是在第一次 LLM 调用时才暴露。
        """
        if self.llm_mode != "LIVE":
            return self
        api_key = (
            self.bedrock_api_key.get_secret_value() if self.bedrock_api_key else None
        )
        missing = [
            name
            for name, value in (
                ("BEDROCK_GATEWAY_URL", self.bedrock_gateway_url),
                ("BEDROCK_API_KEY", api_key),
            )
            if value is None or not value.strip()
        ]
        if missing:
            raise ValueError(
                "LLM_MODE=LIVE 要求以下环境变量：" + "、".join(missing)
            )
        return self


def _format_validation_error(error: ValidationError) -> str:
    """把 Pydantic 报错转成运维可读的清单，且不回显任何取值。"""
    lines = ["配置校验失败，拒绝启动："]
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "<root>"
        lines.append(f"  - {location.upper()}: {item['msg']}")
    lines.append("  参考 .env.example 补齐后重试。")
    return "\n".join(lines)


def load_settings() -> Settings:
    """读取并校验配置。失败抛 `ConfigurationError`，不返回半成品对象。"""
    try:
        return Settings()  # type: ignore[call-arg]  # 值由环境变量提供
    except ValidationError as error:
        raise ConfigurationError(_format_validation_error(error)) from error
    except ValueError as error:
        # pydantic-settings 的 SettingsError（如 `.env` 无法解析）继承自 ValueError，
        # 同样属于「配置错了」，不应带着半成品对象继续启动。
        raise ConfigurationError(f"配置校验失败，拒绝启动：{error}") from error


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试改环境变量后需调用 `get_settings.cache_clear()`。"""
    return load_settings()
