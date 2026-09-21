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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import PlannerSession
from app.api.errors import ErrorCode, NextAction, error_response
from app.db import audit
from app.db.models import Trace
from app.llm.adapter import LlmMode
from app.logging_config import log_event
from app.seed import reset_demo_data
from app.seed.loader import PRESERVED_TABLES
from app.services.feature_flags import (
    audit_flag_change,
    read_feature_flags,
    set_auto_apply_minor_enabled,
)
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
    engine: Engine = request.app.state.engine

    db_ok, project_usd_spent, real_run_count = _probe(engine)

    # P0 的降级判定只有一个来源：**运行期**的 `Bedrock_Adapter.mode`（design.md §2.6「旁路点
    # 只有一个」）。手动开关（R25.11）、Bedrock 连续失败（R25.8）与项目成本闸门都是通过把
    # 这个运行期 mode 置为 `DISABLED` 生效的，因此读它才能反映**当前**降级状态——读静态配置
    # `settings.llm_mode` 会漏掉运行期切换（例如手动 `POST /settings/mode` 或失败自动降级）。
    adapter_mode = _current_llm_mode(request)
    mode: Literal["NORMAL", "DETERMINISTIC_ONLY"] = (
        "DETERMINISTIC_ONLY" if adapter_mode == "DISABLED" else "NORMAL"
    )

    return HealthResponse(
        status="OK" if db_ok else "DEGRADED",
        mode=mode,
        db_ok=db_ok,
        llm_mode=adapter_mode,
        project_usd_spent=round(project_usd_spent, 6),
        real_run_count=real_run_count,
    )


def _current_llm_mode(request: Request) -> str:
    """当前运行期 LLM 模式：优先读挂在 app.state 的 `Bedrock_Adapter.mode`（运行期唯一真值），
    退回静态配置（adapter 尚未装配时，例如极简测试）。"""
    adapter = getattr(request.app.state, "llm_adapter", None)
    if adapter is not None:
        return str(getattr(adapter.mode, "value", adapter.mode))
    settings: Settings = request.app.state.settings
    return str(settings.llm_mode)


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
        message="Demo data has been reset",
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


# --------------------------------------------------------------------------
# GET / PATCH /settings/flags（任务 7.6，R13.8）
# --------------------------------------------------------------------------


class FeatureFlagsOut(BaseModel):
    """运行期特性开关的当前值（design.md §5「自主」分组，R13.8）。

    P0 只有一个开关 `auto_apply_minor_enabled`，默认 `false`。取值域封闭：多一个字段即意味着
    新开关未经审视地泄进了这个响应。
    """

    model_config = ConfigDict(extra="forbid")

    auto_apply_minor_enabled: bool = Field(
        description="IMPACT_MINOR 是否自动应用（L4，P1 行为）。P0 默认 false → 全走 L3 提案"
    )


class PatchFlagsIn(BaseModel):
    """`PATCH /settings/flags` 的请求体。

    全部字段可选：只提供要改的那个开关，未提供的保持不变（PATCH 语义）。P0 只认
    `auto_apply_minor_enabled`；`extra="forbid"` 使拼错的键名直接 422，而不是被静默忽略。
    """

    model_config = ConfigDict(extra="forbid")

    auto_apply_minor_enabled: bool | None = None


@router.get(
    "/settings/flags",
    response_model=FeatureFlagsOut,
    summary="读取运行期特性开关（R13.8）",
)
def get_settings_flags(request: Request) -> FeatureFlagsOut:
    """读端点（无认证，与其余 GET 同口径）：从 `settings` 表读当前开关，缺行即 P0 默认。"""
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        flags = read_feature_flags(db)
    return FeatureFlagsOut(auto_apply_minor_enabled=flags.auto_apply_minor_enabled)


@router.patch(
    "/settings/flags",
    response_model=FeatureFlagsOut,
    summary="切换运行期特性开关（auto_apply_minor_enabled 等，R13.8）",
)
def patch_settings_flags(
    request: Request, body: PatchFlagsIn, session: PlannerSession
) -> FeatureFlagsOut:
    """写端点（受 `Session_Auth` 保护）：持久化开关变更并留审计，返回更新后的开关集。

    PATCH 语义：只有请求体里显式给出的开关才被改写，未提供的保持原值。P0 只有
    `auto_apply_minor_enabled`；把它翻成 `true` 会让后续 `IMPACT_MINOR` 变更走 L4 自动应用
    （P1 行为），但 `IMPACT_MAJOR` 的 L5 上报判定在任何配置下都不可覆盖（R13.5，由
    `decide_autonomy` 的控制流保证）。

    `session.subject` 进审计记录的 `actor`，因此「谁改了这个开关」在日志里有答案。
    """
    now = datetime.now()  # noqa: DTZ005 — 全库 naive 本地时间口径
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        if body.auto_apply_minor_enabled is not None:
            enabled = body.auto_apply_minor_enabled
            flags = set_auto_apply_minor_enabled(db, enabled=enabled, now=now)
            # 先提交业务写（释放 SQLite 写锁），再补审计——审计走独立连接，若在写锁未释放时
            # 追加会撞锁（见 feature_flags.set_auto_apply_minor_enabled 的说明）。
            db.commit()
            audit_flag_change(enabled=enabled, actor=session.subject.upper(), now=now)
        else:
            # 请求体没有任何可改的开关：不写、不审计，直接回读当前值（幂等空 PATCH）。
            flags = read_feature_flags(db)
    return FeatureFlagsOut(auto_apply_minor_enabled=flags.auto_apply_minor_enabled)



# --------------------------------------------------------------------------
# POST /settings/mode（任务 11.6，R25.11）——手动切换 DETERMINISTIC_ONLY
# --------------------------------------------------------------------------


class SetModeIn(BaseModel):
    """`POST /settings/mode` 的请求体：显式设定目标运行模式（R25.11）。

    `mode` 只有两个合法值：`DETERMINISTIC_ONLY`（手动进入降级）与 `NORMAL`（手动退出，
    恢复到启动配置的基础模式）。`extra="forbid"` 使任何拼错的键或多余字段直接 422。
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["NORMAL", "DETERMINISTIC_ONLY"]


class SetModeOut(BaseModel):
    """`POST /settings/mode` 的响应：切换后的运行模式与 LLM 模式。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["NORMAL", "DETERMINISTIC_ONLY"]
    llm_mode: str = Field(description="运行期 Bedrock_Adapter.mode（LIVE/REPLAY/STUB/DISABLED）")
    probe_ok: bool = Field(description="退出降级时的探针结果；进入降级恒为 true")


@router.post(
    "/settings/mode",
    response_model=SetModeOut,
    summary="手动切换 DETERMINISTIC_ONLY 降级模式（R25.11）",
)
def set_mode(
    request: Request, body: SetModeIn, session: PlannerSession
) -> SetModeOut | JSONResponse:
    """手动进入 / 退出 `DETERMINISTIC_ONLY`（design.md §2.6 的三条进入条件之一，R25.11）。

    **旁路点只有一个**：本端点只改运行期 `Bedrock_Adapter.mode`（design.md §2.6），不引入第二个
    状态源——`GET /health` 读同一个 mode，因此切换即时对整个系统可见。

    - **进入降级**（`DETERMINISTIC_ONLY`）：把 adapter.mode 置 `DISABLED`，写一条
      `DEGRADED_MODE_SWITCH` / `ENTER_DETERMINISTIC_ONLY`（trigger=`MANUAL`）审计。幂等：已在
      降级态时仍写一条留痕（「谁在何时又按了一次」也是运维信息）。`probe_ok` 恒为 true。
    - **退出降级**（`NORMAL`）：design.md §2.6 的退出条件是「手动关闭 + 一次成功探针」。探针
      是把 adapter.mode 恢复到**启动配置的基础模式**（`settings.llm_mode`）后做一次轻量确认；
      在 `STUB`/`REPLAY`/`LIVE(可用)` 下探针成功即恢复并写 `EXIT_DETERMINISTIC_ONLY` 审计。
      若基础配置本身就是 `DISABLED`（即部署选择了纯确定性），退出无意义 → 探针视为失败、保持
      降级并返回 409。

    写端点，受 `Session_Auth` 保护；`session.subject` 进审计 `actor`。
    """
    adapter = getattr(request.app.state, "llm_adapter", None)
    if adapter is None:  # 极简测试未装配 adapter：无可切换的运行期出口
        return error_response(
            status_code=409,
            code=ErrorCode.INVALID_STATE_TRANSITION,
            message="This deployment has no LLM egress configured, so the run mode cannot be switched.",
            next_actions=[NextAction(action="view_health", href="/health")],
        )

    settings: Settings = request.app.state.settings
    actor = session.subject.upper()

    if body.mode == "DETERMINISTIC_ONLY":
        _switch_mode(adapter, LlmMode.DISABLED)
        _audit_mode_switch(
            event_type="ENTER_DETERMINISTIC_ONLY", trigger="MANUAL", actor=actor
        )
        return SetModeOut(mode="DETERMINISTIC_ONLY", llm_mode=adapter.mode.value, probe_ok=True)

    # 退出降级：恢复到启动配置的基础模式，并做一次成功探针。
    base_mode = LlmMode(settings.llm_mode)
    if base_mode is LlmMode.DISABLED:
        # 基础配置即纯确定性——没有可恢复的 LLM 模式，探针失败，保持降级。
        return error_response(
            status_code=409,
            code=ErrorCode.INVALID_STATE_TRANSITION,
            message="The deployment base mode is DISABLED, so degraded mode cannot be exited: there is no LLM mode to restore.",
            next_actions=[NextAction(action="view_health", href="/health")],
            details={"base_mode": base_mode.value},
        )
    _switch_mode(adapter, base_mode)
    _audit_mode_switch(event_type="EXIT_DETERMINISTIC_ONLY", trigger="MANUAL", actor=actor)
    return SetModeOut(mode="NORMAL", llm_mode=adapter.mode.value, probe_ok=True)


def _switch_mode(adapter: object, target: LlmMode) -> None:
    """把运行期 adapter 切到目标模式，并复位连续失败计数（重新给 LIVE 一次机会）。"""
    adapter.mode = target  # type: ignore[attr-defined]
    # 复位失败计数：退出降级后，下一次 LIVE 调用应从干净的重试预算开始。
    if hasattr(adapter, "_consecutive_failures"):
        adapter._consecutive_failures = 0  # type: ignore[attr-defined]


def _audit_mode_switch(*, event_type: str, trigger: str, actor: str) -> None:
    """写一条 `DEGRADED_MODE_SWITCH` 审计（R25.9，与 adapter 自动降级同类别）。"""
    audit.append(
        event_category="DEGRADED_MODE_SWITCH",
        event_type=event_type,
        actor=actor,
        payload={"trigger": trigger},
    )
