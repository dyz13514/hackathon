"""FastAPI 应用装配。

只做六件事：读配置（缺失即拒绝启动）、建应用、建引擎与会话工厂（含审计专用的第二个
引擎）、挂 `Session_Auth`、挂 CORS、挂 `/api` 路由聚合点。
业务逻辑一律不在此处——路由在 `app/api/`，编排在 `app/orchestrator/`，
确定性计算在 `app/core/`（design.md Architecture §1）。

以工厂函数暴露而非模块级 `app` 实例：这样 `import app.main` 没有副作用，
配置缺失的失败发生在**显式调用**时。启动命令因此是

    uvicorn app.main:create_app --factory --workers 1

`--workers 1` 是设计约束（design.md Architecture §4）：SQLite 写并发受限，
且 R12.7 的乐观并发控制在单进程下更容易正确。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import api_router, ops_router
from app.api.deps import install_session_auth
from app.db.audit import set_audit_engine
from app.db.session import create_db_engine, create_session_factory
from app.llm.adapter import BedrockAdapter
from app.llm.cassette import Cassette
from app.logging_config import configure_logging
from app.services.events import EventBus
from app.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """装配应用。`settings` 仅供测试注入，生产路径走 `get_settings()`。"""
    resolved = settings or get_settings()

    # 日志先装：此后的任何装配失败都会以结构化 JSON 落到 stdout，且凭证脱敏已就位
    # （任务 1.7、R24.8、R23.10）。
    configure_logging(resolved)

    application = FastAPI(
        title="AI Production Planning Agent",
        version=__version__,
        summary="确定性排产内核 + 人在环审批 + 有界 LLM 编排",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    application.state.settings = resolved

    # 引擎与会话工厂各建一个，挂在 app.state 上：连接池是进程级资源，每请求新建会
    # 让 SQLite 的 WAL 与 `busy_timeout` 设置失去意义（每条新连接都要重设 PRAGMA）。
    # `create_engine` 不会立刻连库，因此配置错误在第一次真实查询时才暴露——`/health`
    # 的探针就是这个第一次。
    application.state.engine = create_db_engine(resolved)
    application.state.session_factory = create_session_factory(application.state.engine)

    # 进程级事件总线（`Approval_Service` 成功激活后 emit `PlanActivated`，任务 8.5 订阅）。
    # 一个共享实例挂在 app.state 上：审批端点每请求新建 `ApprovalService`，但它们共用同一个
    # 总线，因此将来登记的订阅者（风险扫描）对所有审批可见。P0 无订阅者，emit 是空循环。
    application.state.event_bus = EventBus()

    # LLM 出口：全仓库唯一的 `Bedrock_Adapter`（R21.10）。挂在 app.state 上，供解释路径
    # （任务 5.11 的 `GET /plans/{id}/explanation`）与将来的 ReAct 编排（任务 5.7）共用同一个
    # 实例——共享的内容哈希缓存（R25.7）与预算记账接缝因此对所有调用可见。`LLM_MODE` 由配置
    # 决定：本地/CI 默认 `REPLAY`（零成本零网络），演示预热可手工切 `DISABLED`（降级模式）。
    application.state.llm_adapter = BedrockAdapter.from_settings(
        resolved, cassette=Cassette()
    )

    # 审计走**第二个**引擎（`db/audit.py`：审计写入不参与业务事务，因此不能共用连接池）。
    # 在这里显式注册，而不是让 `get_audit_engine()` 自己懒建：懒建走的是
    # `get_settings()`，那是一个进程级 lru_cache，与本次装配用的 `settings` 可能不是同一
    # 份（测试注入、多库脚本）。不一致的表现是审计记录落进另一个库——最难发现的那类
    # 失效，因为业务侧一切正常，只是日志里少了东西。
    application.state.audit_engine = create_db_engine(resolved)
    set_audit_engine(application.state.audit_engine)

    # Session_Auth 先装、CORS 后装：Starlette 里**后装的在更外层**，因此 CORS 会包住
    # 认证中间件，401 响应也带上 `Access-Control-Allow-*` 头。反过来的话浏览器只会
    # 看到一个不透明的网络错误，而不是「请重新登录」（任务 1.5）。
    install_session_auth(application)

    # 前端在开发期跑在 Vite 端口上，与后端不同源；会话令牌是 HttpOnly Cookie，
    # 因此必须允许携带凭证，且 origin 不能用通配符。
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Requested-With"],
    )

    application.include_router(api_router)
    # 根路径上的 `/health`，不进 OpenAPI：前端的类型化客户端由 `/api` 下的同一端点
    # 生成，两份会产生重复 operationId。
    application.include_router(ops_router, include_in_schema=False)
    return application
