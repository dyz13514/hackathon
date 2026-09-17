"""登录、登出与会话状态三个端点（R23.12，tasks.md 1.5）。

design.md Components §5 的表里没有单列这三条——表列的是业务分组，而认证是「写操作
一律经 `Session_Auth` 中间件校验会话令牌」这句话的前提。口令换令牌必须有一个入口，
且这个入口只能在服务端：把口令比对放到前端，等于把口令发给每一个访客。

## 为什么有 `GET /auth/session`

前端需要知道「现在要不要弹登录框」。没有这个端点的话，唯一的办法是发一个写请求看
是否 401，那会在页面加载时产生副作用。它是只读的、不返回任何凭证，只回
`{authenticated, expires_at}`。

**它不是校验点**。真正的校验发生在每个写请求上（`api/deps.py` 的中间件）。前端拿到
`authenticated: true` 之后并不因此获得任何权限——这一点必须成立，否则就退回成了
「依赖前端校验」。
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.deps import (
    UnauthenticatedError,
    clear_session_cookie,
    current_session,
    issue_token,
    set_session_cookie,
    verify_shared_password,
)
from app.logging_config import log_event
from app.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """登录请求。

    `password` 用 `SecretStr` 而不是 `str`：校验失败时 Pydantic 会把输入值放进 422 的
    `input` 字段，`SecretStr` 的序列化形式是掩码，因此**口令不会经错误响应回显**
    （R23.10）。`extra="forbid"` 让「顺手多传一个字段」变成一个显式的 422。
    """

    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(min_length=1, description="演示环境的共享访问口令")


class SessionResponse(BaseModel):
    """登录/登出/查询共用的响应。**不含令牌**——令牌只经 `Set-Cookie` 出去。"""

    model_config = ConfigDict(extra="forbid")

    authenticated: bool
    expires_at: datetime | None = Field(
        default=None, description="会话过期时刻（UTC）；未认证时为 null"
    )


@router.post(
    "/login",
    response_model=SessionResponse,
    summary="共享口令换 HttpOnly Cookie 会话令牌（R23.12）",
    responses={401: {"description": "口令不正确"}},
)
def login(request: Request, payload: LoginRequest, response: Response) -> SessionResponse:
    """比对共享口令并签发会话令牌。"""
    settings: Settings = request.app.state.settings

    if not verify_shared_password(settings, payload.password.get_secret_value()):
        # 日志只记事件与客户端地址，不记尝试的口令，也不记「差在哪里」（R23.10）。
        # TODO(任务 1.4)：同时写 `Audit_Log`。
        log_event(
            logger,
            "AUTH_LOGIN_FAILED",
            level=logging.WARNING,
            client_host=request.client.host if request.client else None,
        )
        # 抛与「令牌无效」同一个异常，因此响应体逐字节相同：不区分「口令错」与
        # 「没登录」，探测者无法从响应差异推断口令是否接近。
        raise UnauthenticatedError("BAD_PASSWORD")

    token, expires_at = issue_token(settings)
    set_session_cookie(response, token, settings)
    log_event(logger, "AUTH_LOGIN_SUCCEEDED")
    return SessionResponse(authenticated=True, expires_at=expires_at)


@router.post(
    "/logout",
    response_model=SessionResponse,
    summary="清除会话 Cookie",
)
def logout(request: Request, response: Response) -> SessionResponse:
    """删除会话 Cookie。幂等：未登录时调用同样返回 `authenticated: false`。

    因此它在 `UNAUTHENTICATED_WRITE_PATHS` 里：要求认证才能登出，只会让「Cookie 已
    过期时点登出」变成一个 401，而这个动作本身不改变任何业务数据。
    """
    settings: Settings = request.app.state.settings
    clear_session_cookie(response, settings)
    log_event(logger, "AUTH_LOGOUT")
    return SessionResponse(authenticated=False)


@router.get(
    "/session",
    response_model=SessionResponse,
    summary="当前会话状态（只读，非校验点）",
)
def session_status(request: Request) -> SessionResponse:
    """回答「现在要不要弹登录框」。不签发、不刷新令牌。"""
    claims = current_session(request)
    if claims is None:
        return SessionResponse(authenticated=False)
    return SessionResponse(authenticated=True, expires_at=claims.expires_at)
