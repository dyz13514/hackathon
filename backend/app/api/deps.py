"""`Session_Auth`：单一共享口令 → 服务端签发 HttpOnly Cookie 会话令牌（R23.12）。

依据 design.md Components §5 与「Open Questions 处置汇总」第 3 条：

> 演示认证方式：单一共享口令 → 服务端签发 HttpOnly Cookie 会话令牌。

需求侧的措辞（R23.12）里有一句不能被读漏：「WHERE 演示环境使用单一 `Planner` 账户,
THE System SHALL **仍然在服务端校验**会话令牌」。因此这里没有任何一处依赖前端。

## 四条设计决定

1. **默认拒绝，而不是逐路由记得挂依赖。** 校验的主体是一个 ASGI 中间件：凡是
   `POST / PUT / PATCH / DELETE`，除 `UNAUTHENTICATED_WRITE_PATHS` 里的登录/登出两条
   之外，一律要求有效令牌。这样任务 1.6 的 `POST /demo/reset` 与任务 2.12 的
   `POST /plans/generate` 在**作者什么都不做**的情况下就是受保护的。反过来的做法
   （每个写路由挂 `Depends(require_session)`）把安全性寄托在「下一个人记得加」上，
   而漏掉一处的后果是任意人可改生产数据并提交审批。

   `require_session` 依赖仍然提供，且**独立完成校验**（不只是读中间件的结果）：它让
   保护关系出现在路由签名与 OpenAPI 里，同时构成第二道。两道都在服务端。

2. **令牌是自包含的签名串，服务端不存会话表。** `HMAC-SHA256(payload)`，`payload`
   含 `exp`。演示只有一个 `Planner` 账户，会话表能提供的额外能力只有「单个会话吊销」，
   而改口令即可让全部令牌立刻失效——见下一条。

3. **签名密钥绑定口令指纹。** 签名密钥 = `HMAC(SESSION_SECRET_KEY, sha256(口令))`，
   于是**改口令或改密钥都会让既有令牌全部失效**，无需吊销名单。演示环境里「口令泄漏
   了怎么办」因此有一个一步的答案：改环境变量重启。

4. **口令与令牌不出现在任何输出里。** 请求模型用 `SecretStr`（校验失败的 422 里
   因此不会回显口令）；失败日志只记 `reason` 这一枚举化的原因；错误响应体只有
   `UNAUTHENTICATED` 与一句中文说明（R23.10）。令牌本身只经 `Set-Cookie` 出去一次，
   任何日志字段都不带它。

## 不在本任务范围内

登录失败的速率限制与锁定。演示部署面向单一口令，暴力破解是真实风险，但缓解措施
（IP 维度计数 + 退避）需要一处跨请求状态，且与 R23 的任何一条都不对应。此处只把
失败记成结构化事件（`AUTH_LOGIN_FAILED`），使「有人在试」可被观测。
TODO(任务 1.4)：`db/audit.py` 落地后，把 `AUTH_LOGIN_FAILED` 与 `AUTH_REJECTED`
一并写入 `Audit_Log`（当前只经结构化日志，见 `_audit_security_event`）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import Depends, FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response as StarletteResponse

from app.api.errors import ErrorCode, NextAction, error_response
from app.logging_config import log_event
from app.settings import Settings

logger = logging.getLogger(__name__)

#: 会话 Cookie 名。`pp_` 前缀避免与同域下其他应用的 `session` 撞名。
SESSION_COOKIE_NAME: Final = "pp_session"

#: 令牌有效期 12 小时：覆盖一个完整演示日，且过夜后必须重新输入口令。
SESSION_TTL_SECONDS: Final = 12 * 60 * 60

#: 令牌格式版本。写进载荷并参与签名，使将来换格式时旧令牌被判为无效而不是被误解。
TOKEN_VERSION: Final = 1

#: 单一 `Planner` 账户（R23.12）。载荷里保留 `sub` 字段，是为了将来加第二个角色时
#: 不需要改令牌格式。
SESSION_SUBJECT: Final = "planner"

#: 允许的时钟偏移。`iat` 落在未来这么多秒内仍接受，避免部署机与浏览器机的秒级差异
#: 把刚签发的令牌判成无效。
CLOCK_SKEW_SECONDS: Final = 60

#: 需要认证的 HTTP 方法。`GET` / `HEAD` / `OPTIONS` 是读与预检，演示环境公开
#: （R23.12 的约束是「所有**写**操作 API 端点」）。
MUTATING_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: 唯一允许未认证写入的两条路径。登录是取得凭证的入口；登出是幂等的清除动作，
#: 要求认证只会让「Cookie 已过期时点登出」变成一个 401。
#:
#: 这个集合被 `tests/unit/test_session_auth_api.py` 逐元素断言：新增豁免项必须是一次
#: 显式改动，且要在测试里说明理由。
UNAUTHENTICATED_WRITE_PATHS: Final[frozenset[str]] = frozenset(
    {"/api/auth/login", "/api/auth/logout"}
)

#: 401 响应里给出的下一步。
_LOGIN_NEXT_ACTIONS: Final = [NextAction(action="login", href="/api/auth/login")]

_UNAUTHENTICATED_MESSAGE: Final = "会话未认证或已过期，请重新输入访问口令。"


class SessionTokenError(Exception):
    """令牌缺失、格式非法、签名不符或已过期。

    `reason` 是枚举化的短标识（进日志用），**不含**令牌内容：把不合法的令牌原文记进
    日志等于把一个可能有效的凭证写进日志文件。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnauthenticatedError(Exception):
    """请求未通过 `Session_Auth`。由 `install_session_auth()` 注册的处理器渲染成
    `UNAUTHENTICATED` 错误包（design.md Error Handling §2）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SessionClaims:
    """已校验的令牌载荷。`frozen` 使它无法在请求处理链中途被改写。"""

    subject: str
    issued_at: datetime
    expires_at: datetime


# --------------------------------------------------------------------------
# 令牌编解码（纯函数，无 I/O）
# --------------------------------------------------------------------------


def _b64encode(raw: bytes) -> str:
    """URL 安全 base64，去掉 `=` 填充（Cookie 值里 `=` 需要引号包裹）。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def signing_key(settings: Settings) -> bytes:
    """派生签名密钥：`HMAC(SESSION_SECRET_KEY, "session-auth-v1|" + sha256(口令))`。

    口令指纹参与派生，因此改口令即让全部既有令牌失效（见模块 docstring 第 3 条）。
    口令本身不进密钥材料的可读部分，只进哈希。
    """
    password_digest = hashlib.sha256(
        settings.session_shared_password.get_secret_value().encode("utf-8")
    ).digest()
    return hmac.new(
        settings.session_secret_key.get_secret_value().encode("utf-8"),
        b"session-auth-v1|" + password_digest,
        hashlib.sha256,
    ).digest()


def issue_token(settings: Settings, *, now: int | None = None) -> tuple[str, datetime]:
    """签发令牌。返回 `(token, expires_at)`。"""
    issued_at = int(time.time()) if now is None else now
    expires_at = issued_at + SESSION_TTL_SECONDS
    payload = {
        "v": TOKEN_VERSION,
        "sub": SESSION_SUBJECT,
        "iat": issued_at,
        "exp": expires_at,
    }
    encoded = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _b64encode(
        hmac.new(signing_key(settings), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{encoded}.{signature}", datetime.fromtimestamp(expires_at, tz=UTC)


def verify_token(
    settings: Settings, token: str | None, *, now: int | None = None
) -> SessionClaims:
    """校验令牌，失败抛 `SessionTokenError`。

    顺序是刻意的：**先验签名，再看载荷**。载荷在签名通过之前是不受信任的字节，
    据它做任何判断（哪怕只是「过期了」）都等于相信未认证的输入。
    """
    if not token:
        raise SessionTokenError("MISSING")

    parts = token.split(".")
    if len(parts) != 2 or not all(parts):
        raise SessionTokenError("MALFORMED")
    encoded, signature = parts

    expected = _b64encode(
        hmac.new(signing_key(settings), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    # 常量时间比较：签名比较上的计时差可以被用来逐字节构造出有效签名。
    if not hmac.compare_digest(signature, expected):
        raise SessionTokenError("BAD_SIGNATURE")

    try:
        payload = json.loads(_b64decode(encoded))
    except (ValueError, binascii.Error) as error:
        # 签名对但载荷解不开，只可能是本进程签发格式变了。
        raise SessionTokenError("MALFORMED_PAYLOAD") from error
    if not isinstance(payload, dict):
        raise SessionTokenError("MALFORMED_PAYLOAD")

    if payload.get("v") != TOKEN_VERSION:
        raise SessionTokenError("VERSION_MISMATCH")
    subject = payload.get("sub")
    if subject != SESSION_SUBJECT:
        raise SessionTokenError("UNKNOWN_SUBJECT")

    issued_at = _int_field(payload, "iat")
    expires_at = _int_field(payload, "exp")
    current = int(time.time()) if now is None else now
    if expires_at <= current:
        raise SessionTokenError("EXPIRED")
    if issued_at > current + CLOCK_SKEW_SECONDS:
        raise SessionTokenError("NOT_YET_VALID")

    return SessionClaims(
        subject=subject,
        issued_at=datetime.fromtimestamp(issued_at, tz=UTC),
        expires_at=datetime.fromtimestamp(expires_at, tz=UTC),
    )


def _int_field(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    # `bool` 是 `int` 的子类，显式排除，避免 `{"exp": true}` 被当成 1970 年的时间戳。
    if not isinstance(value, int) or isinstance(value, bool):
        raise SessionTokenError("MALFORMED_PAYLOAD")
    return value


def verify_shared_password(settings: Settings, candidate: str) -> bool:
    """常量时间比对共享口令。

    比较的是两侧的 SHA-256 摘要而不是原文：`compare_digest` 对不等长的字符串会立刻
    返回，长度本身因此成为一个旁道。摘要长度恒定，把这个旁道去掉。
    """
    expected = hashlib.sha256(
        settings.session_shared_password.get_secret_value().encode("utf-8")
    ).digest()
    provided = hashlib.sha256(candidate.encode("utf-8")).digest()
    return hmac.compare_digest(provided, expected)


# --------------------------------------------------------------------------
# Cookie
# --------------------------------------------------------------------------


def cookie_secure(settings: Settings) -> bool:
    """是否给 Cookie 打 `Secure` 标记。

    判据是「已配置的前端来源是否全为 https」而不是 `APP_ENV`：`Secure` Cookie 在
    明文 http 下不会被浏览器回送，如果演示部署走的是 http，硬编码 `Secure=True`
    会让登录静默失效——一个只在真机上才出现的故障。来源清单是配置的一部分，
    因此这个判断在启动时就确定，不依赖运行期猜测。
    """
    origins = settings.cors_origins
    return bool(origins) and all(origin.startswith("https://") for origin in origins)


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    """写入会话 Cookie。`HttpOnly` 是本任务的硬要求（design.md §5）。

    `HttpOnly` 让 XSS 拿不到令牌；`SameSite=Lax` 挡掉跨站表单触发的写请求
    （CSRF 的主要形态），同时不影响 Vite 开发服务器与后端的同站不同端口调用。
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=cookie_secure(settings),
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    """删除会话 Cookie。属性必须与写入时一致，否则浏览器不认为是同一个 Cookie。"""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=cookie_secure(settings),
    )


# --------------------------------------------------------------------------
# 请求侧校验
# --------------------------------------------------------------------------


def _settings_of(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _audit_security_event(
    event: str, *, request: Request, reason: str, level: int = logging.WARNING
) -> None:
    """记录一次安全相关的拒绝。

    字段刻意只有方法、路径与原因：客户端提交的令牌或口令一律不记（R23.10）。
    TODO(任务 1.4)：同时 `AuditLog.append(...)`，`db/audit.py` 落地后接线。
    """
    log_event(
        logger,
        event,
        level=level,
        method=request.method,
        path=request.url.path,
        reason=reason,
    )


def authenticate_request(request: Request) -> SessionClaims:
    """校验请求携带的会话 Cookie，失败抛 `UnauthenticatedError`。

    中间件与 `require_session` 依赖共用它，因此「怎样算认证通过」只有一处定义。
    """
    try:
        return verify_token(_settings_of(request), request.cookies.get(SESSION_COOKIE_NAME))
    except SessionTokenError as error:
        raise UnauthenticatedError(error.reason) from error


def current_session(request: Request) -> SessionClaims | None:
    """当前请求的会话，未认证返回 `None`。供只读端点做「是否已登录」的展示。"""
    cached = getattr(request.state, "session", None)
    if isinstance(cached, SessionClaims):
        return cached
    try:
        return verify_token(_settings_of(request), request.cookies.get(SESSION_COOKIE_NAME))
    except SessionTokenError:
        return None


def require_session(request: Request) -> SessionClaims:
    """写端点的显式依赖。中间件已拦下未认证请求，这里是第二道，且让保护关系进
    OpenAPI。

    刻意不信任 `request.state.session`（除非它是本类型的实例）：把「中间件装没装」
    变成这个依赖的前提条件，会让某个未来的子应用漏装中间件时，保护静默消失。
    """
    cached = getattr(request.state, "session", None)
    if isinstance(cached, SessionClaims):
        return cached
    try:
        claims = authenticate_request(request)
    except UnauthenticatedError as error:
        _audit_security_event("AUTH_REJECTED", request=request, reason=error.reason)
        raise
    request.state.session = claims
    return claims


#: 写端点的标准注解形式：`session: PlannerSession`。
PlannerSession = Annotated[SessionClaims, Depends(require_session)]


def is_exempt_path(path: str) -> bool:
    """该路径是否允许未认证写入。尾部斜杠归一，避免 `/api/auth/login/` 绕过。"""
    return (path.rstrip("/") or "/") in UNAUTHENTICATED_WRITE_PATHS


class SessionAuthMiddleware(BaseHTTPMiddleware):
    """`Session_Auth` 中间件：默认拒绝一切未认证的写请求。

    覆盖面是**方法**而不是路由表，因此不存在「新写端点忘了挂依赖」这种失效方式；
    404 的写请求同样被拒（不认证就不该得知某条路径是否存在）。
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> StarletteResponse:
        if request.method in MUTATING_METHODS and not is_exempt_path(request.url.path):
            try:
                request.state.session = authenticate_request(request)
            except UnauthenticatedError as error:
                _audit_security_event("AUTH_REJECTED", request=request, reason=error.reason)
                return _unauthenticated_response()
        return await call_next(request)


def _unauthenticated_response() -> StarletteResponse:
    return error_response(
        status_code=401,
        code=ErrorCode.UNAUTHENTICATED,
        message=_UNAUTHENTICATED_MESSAGE,
        next_actions=list(_LOGIN_NEXT_ACTIONS),
    )


def install_session_auth(app: FastAPI) -> None:
    """把中间件与 `UnauthenticatedError` 处理器装上。

    在 `create_app()` 里**先于** CORS 中间件调用，使 CORS 处于更外层：否则 401 响应
    不带 `Access-Control-Allow-*` 头，浏览器只会看到一个不透明的网络错误，而不是
    「请重新登录」。
    """
    app.add_middleware(SessionAuthMiddleware)

    async def _handle_unauthenticated(
        request: Request, exc: Exception
    ) -> StarletteResponse:
        del request, exc  # 响应体不含任何来自请求的内容
        return _unauthenticated_response()

    app.add_exception_handler(UnauthenticatedError, _handle_unauthenticated)
