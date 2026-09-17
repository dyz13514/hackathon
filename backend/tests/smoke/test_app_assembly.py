"""应用装配冒烟：`create_app()` 能起来、OpenAPI 可生成、`/api` 前缀就位。

同时锁住「装配依赖配置」这条：配置非法时 `create_app()` 不应返回一个半成品应用。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from app.main import create_app
from app.settings import ConfigurationError, Settings


def test_create_app_returns_application(settings: Settings) -> None:
    application = create_app(settings)
    assert isinstance(application, FastAPI)
    assert application.state.settings is settings


def test_openapi_schema_generates(settings: Settings) -> None:
    schema = create_app(settings).openapi()
    assert schema["info"]["title"] == "AI Production Planning Agent"


def test_api_prefix_is_reserved(settings: Settings) -> None:
    """全部业务端点前缀 `/api`（design.md Components §5）。

    骨架阶段路由集合为空，此处断言的是 docs / openapi 也在该前缀下，
    这样反向代理只需转发一个前缀。
    """
    # 新版 Starlette 下 `include_router(prefix=...)` 会把子路由包成一个没有 `.path` 的
    # 包装对象（`_IncludedRouter`），因此不能假设每个 route 都有 `.path`。用 `getattr`
    # 取，缺失的跳过——本断言关心的是 docs / openapi 这两条**有** path 的顶层路由在
    # `/api` 前缀下，而不是遍历整棵路由树。
    paths = {
        getattr(route, "path", None) for route in create_app(settings).routes
    }
    assert "/api/openapi.json" in paths
    assert "/api/docs" in paths


def test_create_app_refuses_invalid_configuration(
    clean_env: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ConfigurationError):
        create_app()
