"""`Session_Auth` 在 HTTP 边界上的行为（R23.12、R23.10，tasks.md 1.5）。

被钉住的是五件事：

1. **默认拒绝**：未认证的写请求一律 401 `UNAUTHENTICATED`，**包括不存在的路径**
   （不认证就不该得知某条写路径是否存在）。
2. **豁免集合恰好两条**：登录与登出。这个集合逐元素断言——每加一项都必须是一次
   显式改动，否则「全部写端点都要认证」这条会被慢慢掏空。
3. **Cookie 属性**：`HttpOnly` + `SameSite=Lax` + `Path=/`，且令牌不出现在响应体里。
4. **口令与令牌不经响应或日志泄漏**（R23.10）；口令错与未登录的响应体逐字节相同。
5. **CORS 在认证之外层**：401 也带 `Access-Control-Allow-Origin`，否则浏览器端只能
   看到一个不透明的网络错误。

探针路由（`/api/demo/probe-*`）只在测试里注册：任务 1.5 时真实写端点还不存在，而要
断言的性质是「任意写端点」而不是「某个特定端点」。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.api.deps import (
    SESSION_COOKIE_NAME,
    UNAUTHENTICATED_WRITE_PATHS,
    PlannerSession,
    cookie_secure,
    issue_token,
)
from app.db.models import Base
from app.main import create_app
from app.settings import Settings

PROBE_WRITE = "/api/demo/probe-write"
PROBE_READ = "/api/demo/probe-read"
LOGIN = "/api/auth/login"
LOGOUT = "/api/auth/logout"
SESSION = "/api/auth/session"

DEV_ORIGIN = "http://localhost:5173"


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "auth.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    return Settings()  # type: ignore[call-arg]  # 值由环境变量提供


@pytest.fixture
def client(app_settings: Settings) -> Iterator[TestClient]:
    application = create_app(app_settings)
    Base.metadata.create_all(application.state.engine)

    @application.post(PROBE_WRITE)
    def _probe_write(session: PlannerSession) -> dict[str, str]:
        """代表任意未来写端点：只标注 `PlannerSession`，不自己做任何校验。"""
        return {"subject": session.subject}

    @application.get(PROBE_READ)
    def _probe_read() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(application) as test_client:
        yield test_client


def _password(settings: Settings) -> str:
    return settings.session_shared_password.get_secret_value()


def _envelope(response: Response) -> dict[str, object]:
    body = response.json()
    assert isinstance(body, dict), "错误响应必须是统一错误包"
    error = body["error"]
    assert isinstance(error, dict)
    return error


# --------------------------------------------------------------------------
# 默认拒绝
# --------------------------------------------------------------------------


def test_write_without_session_is_rejected_with_the_documented_envelope(
    client: TestClient,
) -> None:
    response = client.post(PROBE_WRITE)

    assert response.status_code == 401
    error = _envelope(response)
    assert error["code"] == "UNAUTHENTICATED"
    assert error["details"] == {}
    assert error["next_actions"] == [{"action": "login", "href": LOGIN}]
    assert set(error) == {"code", "message", "details", "next_actions", "trace_id"}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_mutating_method_requires_a_session(
    client: TestClient, method: str
) -> None:
    """覆盖面是方法而不是路由表，因此四个写方法都要被拦下。"""
    response = client.request(method, PROBE_WRITE)
    assert response.status_code == 401


def test_unknown_write_path_is_rejected_before_routing(client: TestClient) -> None:
    """未认证时得到 401 而不是 404：路径是否存在本身也是信息。"""
    response = client.post("/api/plans/generate")
    assert response.status_code == 401
    assert _envelope(response)["code"] == "UNAUTHENTICATED"


def test_reads_stay_open(client: TestClient) -> None:
    """R23.12 约束的是写操作；读端点在演示环境公开（design.md §5）。"""
    assert client.get(PROBE_READ).status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert client.get(SESSION).json() == {"authenticated": False, "expires_at": None}


def test_exempt_write_paths_are_exactly_login_and_logout() -> None:
    """豁免集合是「全部写端点都要认证」这条的唯一缺口，因此逐元素钉住。

    登录是取得凭证的入口；登出是幂等的清除动作，不改变任何业务数据。任何第三项都
    意味着有一个业务写操作绕过了认证。
    """
    assert set(UNAUTHENTICATED_WRITE_PATHS) == {LOGIN, LOGOUT}


def test_trailing_slash_does_not_bypass_the_exemption_check(client: TestClient) -> None:
    """`/api/auth/login/` 与 `/api/auth/login` 归一到同一判断，不产生新的缺口。"""
    response = client.post(LOGIN + "/", json={"password": "whatever"}, follow_redirects=False)
    # 307 是 FastAPI 的斜杠重定向；关键是它没有变成一个「未认证但被放行」的 500。
    assert response.status_code in {307, 401, 404}


# --------------------------------------------------------------------------
# 登录 / 登出闭环
# --------------------------------------------------------------------------


def test_login_issues_an_httponly_cookie_and_never_returns_the_token(
    client: TestClient, app_settings: Settings
) -> None:
    response = client.post(LOGIN, json={"password": _password(app_settings)})

    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["expires_at"] is not None
    assert set(body) == {"authenticated", "expires_at"}

    cookie_header = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE_NAME}=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header.replace("SameSite=Lax", "SameSite=lax")
    assert "Path=/" in cookie_header

    token = client.cookies[SESSION_COOKIE_NAME]
    assert token not in json.dumps(body)


def test_session_cookie_unlocks_write_endpoints(
    client: TestClient, app_settings: Settings
) -> None:
    assert client.post(PROBE_WRITE).status_code == 401

    client.post(LOGIN, json={"password": _password(app_settings)})

    response = client.post(PROBE_WRITE)
    assert response.status_code == 200
    assert response.json() == {"subject": "planner"}
    assert client.get(SESSION).json()["authenticated"] is True


def test_logout_revokes_access(client: TestClient, app_settings: Settings) -> None:
    client.post(LOGIN, json={"password": _password(app_settings)})
    assert client.post(PROBE_WRITE).status_code == 200

    assert client.post(LOGOUT).json() == {"authenticated": False, "expires_at": None}
    assert SESSION_COOKIE_NAME not in client.cookies
    assert client.post(PROBE_WRITE).status_code == 401


def test_logout_is_idempotent_without_a_session(client: TestClient) -> None:
    assert client.post(LOGOUT).status_code == 200


def test_wrong_password_is_indistinguishable_from_no_session(
    client: TestClient, app_settings: Settings
) -> None:
    """口令错与未登录的响应体逐字节相同，且不回显口令（R23.10）。"""
    attempted = _password(app_settings) + "-wrong"
    wrong = client.post(LOGIN, json={"password": attempted})
    missing = client.post(PROBE_WRITE)

    assert wrong.status_code == 401
    assert wrong.json() == missing.json()
    assert "set-cookie" not in wrong.headers
    assert attempted not in wrong.text
    assert SESSION_COOKIE_NAME not in client.cookies


def test_login_rejects_unknown_fields(client: TestClient, app_settings: Settings) -> None:
    """`extra="forbid"`：多传字段是 422，而不是被静默忽略。"""
    response = client.post(
        LOGIN, json={"password": _password(app_settings), "role": "admin"}
    )
    assert response.status_code == 422


def test_tampered_cookie_is_rejected(client: TestClient, app_settings: Settings) -> None:
    client.post(LOGIN, json={"password": _password(app_settings)})
    token = client.cookies[SESSION_COOKIE_NAME]
    encoded, signature = token.split(".")
    client.cookies.set(SESSION_COOKIE_NAME, f"{encoded}.{signature[:-1]}X")

    assert client.post(PROBE_WRITE).status_code == 401


def test_expired_cookie_is_rejected(client: TestClient, app_settings: Settings) -> None:
    """令牌自带 `exp`，服务端不维护会话表，因此过期判定完全在校验里。"""
    token, _ = issue_token(app_settings, now=0)  # 1970 年签发，早已过期
    client.cookies.set(SESSION_COOKIE_NAME, token)

    assert client.post(PROBE_WRITE).status_code == 401


# --------------------------------------------------------------------------
# 与 CORS 的关系
# --------------------------------------------------------------------------


def test_preflight_passes_without_a_session(client: TestClient) -> None:
    """预检是 `OPTIONS`，不在写方法集合内，且 CORS 在更外层，因此不需要认证。"""
    response = client.options(
        PROBE_WRITE,
        headers={
            "Origin": DEV_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == DEV_ORIGIN


def test_401_still_carries_cors_headers(client: TestClient) -> None:
    """中间件顺序的断言：装反了这里就没有 CORS 头，浏览器端只剩不透明错误。"""
    response = client.post(PROBE_WRITE, headers={"Origin": DEV_ORIGIN})

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == DEV_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


def test_cookie_is_marked_secure_only_when_all_origins_are_https(
    app_settings: Settings,
) -> None:
    """明文 http 部署下打 `Secure` 会让登录静默失效，因此判据来自来源清单。"""
    assert cookie_secure(app_settings) is False  # conftest 的 http://localhost:5173
    https_only = app_settings.model_copy(
        update={"cors_allow_origins": "https://demo.example.com"}
    )
    assert cookie_secure(https_only) is True


# --------------------------------------------------------------------------
# 日志（R23.10）
# --------------------------------------------------------------------------


def test_auth_failures_are_logged_without_credentials(
    client: TestClient, app_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """失败可观测（有人在试），但日志里没有口令、没有令牌。"""
    attempted = _password(app_settings) + "-wrong"
    with caplog.at_level(logging.WARNING, logger="app.api"):
        client.post(LOGIN, json={"password": attempted})
        client.post(PROBE_WRITE)

    events = {getattr(record, "event", None) for record in caplog.records}
    assert {"AUTH_LOGIN_FAILED", "AUTH_REJECTED"} <= events

    dumped = "\n".join(
        f"{record.getMessage()} {record.__dict__}" for record in caplog.records
    )
    assert attempted not in dumped
    assert _password(app_settings) not in dumped
