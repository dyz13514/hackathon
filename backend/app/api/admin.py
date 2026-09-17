"""运维端点：`GET /health`（任务 1.7）与 `POST /demo/reset`（任务 1.6）。

`POST /settings/mode`（任务 5.4/2.6 的手动降级开关）后续加在同一模块，与
design.md Components §5 的「运维」分组一致。

## 两个端点的认证形态刻意不同

`GET /health` **无认证**——探针在 `Session_Auth` 之前就要能用，且它不返回任何业务数据。

`POST /demo/reset` 是写端点，因此受 `SessionAuthMiddleware` 保护（按方法默认拒绝，
`api/deps.py`），并额外显式标注 `session: PlannerSession` 让保护关系进 OpenAPI。它
**绝不**能进 `UNAUTHENTICATED_WRITE_PATHS`：那个集合只有登录与登出两条，而重置会把
整个库换掉——未认证可调用等于任何人都能在演示进行中把台上的数据清空。

## 响应字段恰好六个

design.md「运维要点」：`{status, mode, db_ok, llm_mode, project_usd_spent,
real_run_count}`。**不含 `prompt_caching_available`**——prompt caching 全套机具已移出
范围（tasks.md 1.7 与 5.3）。字段集合写成 `HealthResponse` 的 `model_config`
`extra="forbid"` 之外还由 `tests/smoke/test_health.py` 逐字段断言：多一个字段就意味着
有人在往健康检查里塞业务信息，那会让这个端点从「探针」变成「泄漏面」。

## 后三个字段现在就是真值，不是占位

`llm_mode` 直接来自配置；`project_usd_spent` 与 `real_run_count` 从 `traces` 表算出
（任务 1.2 已建表）：

- `real_run_count = COUNT(*) WHERE mode != 'REPLAY'`，即 `PROJECT_REAL_RUN_CAP = 150`
  的当前计数（design.md 成本章节 ②）。任务 5.4 的启动期配额检查读的是同一个口径，
  两处共用 `count_real_runs()` 而不是各写一遍 SQL。
- `project_usd_spent = SUM(estimated_usd)`。

任务 5.3 / 5.4 接线之前 `traces` 表为空，两者恒为 0——这是**正确的真值**（还没花钱），
不是未实现的占位。

## 探针失败时仍返回 200

`db_ok=false` 时 HTTP 状态码仍是 200，`status` 变为 `DEGRADED`。理由：这个端点是
**报告**而不是存活判定。返回 5xx 会让 systemd / Lightsail 的健康检查重启进程，而
SQLite 文件权限或迁移未执行这类问题重启一百次也不会好，反而把可读的错误信息换成了
一个反复重启的容器。
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import PlannerSession
from app.db.models import Trace
from app.logging_config import log_event
from app.seed import reset_demo_data
from app.seed.loader import PRESERVED_TABLES
from app.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])

#: 项目级真实运行次数硬上限（design.md 成本章节 ②、ADR-011）。
#: 任务 5.4 的 `Token_Budget_Manager` 从此处 import，不重新定义一份。
PROJECT_REAL_RUN_CAP = 150

#: `traces.mode` 中不计入真实运行配额的取值。回放不产生任何 Bedrock 支出。
REPLAY_MODE = "REPLAY"


class HealthResponse(BaseModel):
    """`GET /health` 的响应契约（R27.6）。字段集合是封闭的。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["OK", "DEGRADED"] = Field(description="OK 表示数据库可用")
    mode: Literal["NORMAL", "DETERMINISTIC_ONLY"] = Field(
        description="DETERMINISTIC_ONLY 表示全部 LLM 路径已旁路（R25.8）"
    )
    db_ok: bool = Field(description="一次 SELECT 1 加两次聚合查询是否成功")
    llm_mode: str = Field(description="LIVE / REPLAY / STUB / DISABLED")
    project_usd_spent: float = Field(
        description="traces.estimated_usd 的累计，对照 PROJECT_USD_CEILING = 35"
    )
    real_run_count: int = Field(
        description=f"mode != REPLAY 的运行次数，上限 {PROJECT_REAL_RUN_CAP}"
    )


def count_real_runs(engine: Engine) -> int:
    """真实（非回放）运行次数。任务 5.4 的启动期配额检查复用此函数。"""
    with engine.connect() as connection:
        result = connection.execute(
            select(func.count()).select_from(Trace).where(Trace.mode != REPLAY_MODE)
        ).scalar_one()
    return int(result)


def _probe(engine: Engine) -> tuple[bool, float, int]:
    """连通性探针 + 两个计数器，共用一条连接。

    三个查询绑在一起是刻意的：`SELECT 1` 成功但 `traces` 表不存在（迁移没跑）同样是
    「数据库不可用」，把它报成 `db_ok=true` 会让一个真实的部署故障看起来健康。
    """
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            spent = connection.execute(
                select(func.coalesce(func.sum(Trace.estimated_usd), 0))
            ).scalar_one()
            runs = connection.execute(
                select(func.count()).select_from(Trace).where(Trace.mode != REPLAY_MODE)
            ).scalar_one()
    except SQLAlchemyError as error:
        # 只记类型与摘要：异常文本里可能带连接串，而连接串在部署形态变化后可能含口令。
        log_event(
            logger,
            "HEALTH_DB_PROBE_FAILED",
            level=logging.ERROR,
            error_type=type(error).__name__,
        )
        return False, 0.0, 0
    return True, float(Decimal(str(spent))), int(runs)


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="服务状态、当前模式与数据库连通性（R27.6）",
)
def health(request: Request) -> HealthResponse:
    """健康检查。无认证：探针在 `Session_Auth` 之前就要能用，且不返回任何业务数据。"""
    settings: Settings = request.app.state.settings
    engine: Engine = request.app.state.engine

    db_ok, project_usd_spent, real_run_count = _probe(engine)

    # P0 的降级判定只有一个来源：`Bedrock_Adapter` 的模式（design.md §2.6「旁路点
    # 只有一个」）。手动开关（R25.11）与成本闸门都是通过改这个模式生效的，因此
    # 这里读它就够，不需要第二个状态源。
    mode: Literal["NORMAL", "DETERMINISTIC_ONLY"] = (
        "DETERMINISTIC_ONLY" if settings.llm_mode == "DISABLED" else "NORMAL"
    )

    return HealthResponse(
        status="OK" if db_ok else "DEGRADED",
        mode=mode,
        db_ok=db_ok,
        llm_mode=settings.llm_mode,
        project_usd_spent=round(project_usd_spent, 6),
        real_run_count=real_run_count,
    )


# --------------------------------------------------------------------------
# POST /demo/reset（任务 1.6，R28.8）
# --------------------------------------------------------------------------


class DemoResetResponse(BaseModel):
    """`POST /demo/reset` 的响应契约。字段集合封闭。

    返回 `row_counts` 而不只是一个 `"OK"`：演示开场前调用这个端点的人需要立刻知道
    「数据真的铺回去了吗」，而逐表行数是唯一能回答它的东西。`audit_id` 让那条
    `DEMO_RESET` 记录可被直接检索，`input_snapshot_version` 让「从 1 开始」这条不变量
    在响应里就可核对。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["OK"]
    input_snapshot_version: int = Field(
        description="重置后的输入版本号。按 design.md「运维要点」应为 1"
    )
    anchor: datetime = Field(description="演示数据的时间锚点，即演示里的「今天」")
    row_counts: dict[str, int] = Field(description="逐表写入行数，按表名排序")
    preserved_tables: list[str] = Field(
        description="未被清空的表。恒为 ['audit_log']（append-only，R24.3）"
    )
    audit_id: str = Field(description="本次重置补写的那条 DEMO_RESET 审计记录 ID")


@router.post(
    "/demo/reset",
    response_model=DemoResetResponse,
    summary="一键重置演示数据：清空业务表并重放 seed（R28.8）",
)
def demo_reset(request: Request, session: PlannerSession) -> DemoResetResponse:
    """事务内清空业务表并重放 seed；`audit_log` 不清空，改写一条 `DEMO_RESET`。

    实现全在 `app/seed/loader.py`——路由层只负责取会话工厂、把调用者身份传下去、把结果
    序列化。重置的三条不变量（同事务、版本号从 1 开始、审计不清空）与它们各自的理由写在
    那个模块的 docstring 里。

    `session` 参数不只是装饰：`session.subject` 进审计记录的 `actor`，因此「谁按了重置」
    在日志里有答案。演示环境只有一个 `Planner` 账户（R23.12），但字段现在就有语义。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory

    report = reset_demo_data(factory, actor=session.subject.upper())

    log_event(
        logger,
        "DEMO_RESET",
        message="演示数据已重置",
        anchor=report.anchor.isoformat(),
        input_snapshot_version=report.input_snapshot_version,
        audit_id=report.audit_id,
    )

    return DemoResetResponse(
        status="OK",
        input_snapshot_version=report.input_snapshot_version,
        anchor=report.anchor,
        row_counts=dict(report.row_counts),
        preserved_tables=sorted(PRESERVED_TABLES),
        audit_id=report.audit_id,
    )
