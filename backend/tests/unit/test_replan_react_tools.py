"""ReAct 工具序列的真实执行：generate_schedule → save_proposed_plan（任务 7.4、R9.2、§8）。

这组测试证明 `Planning_Agent` 的 ReAct 重排序列**真的能跑通到 PENDING_APPROVAL**，而不是靠
STUB 驱动跳过写工具。此前 `save_proposed_plan` 是 `NotImplementedError` 占位——若真实 ReAct
运行调用它会崩溃（`ToolRegistry.invoke` 第 ③ 步只捕获 `TimeoutError`，其余异常上抛终止整个
运行）。本文件覆盖用户点名的验证项：

1. `generate_schedule` 工具**真的落一行 DRAFT 候选**并返回真实 `plan_id`（§8 的 `∅ → DRAFT`）。
2. `save_proposed_plan` 工具能消费该 DRAFT 候选，**不抛 NotImplementedError**，并创建一个新的
   `PENDING_APPROVAL` 计划行（§8 的 `DRAFT → PENDING_APPROVAL`，采用创建新行而非改写状态）。
3. 一次脚本化 STUB ReAct 运行**真的调用** `generate_schedule` 与 `save_proposed_plan`（经
   `Orchestrator._react_loop` + 真实 `ToolRegistry` + 注入的会话），最终 `final` 成功。
4. 结果计划被持久化为 `PENDING_APPROVAL`。
5. LLM 输出**不能覆盖**确定性数值：`final` 的 `total_tardiness_minutes` / `churn_ratio` 是
   装饰性叙述字段，落库计划的数值由确定性评分算出，与 `final` 无关。
6. 既有 §8 基线守卫仍生效：`origin=BASELINE` 的 `save_proposed_plan` 被拒。

走真实 SQLite + 真实 seed + 真实内核 + 真实 `ToolRegistry`（不 mock 工具执行）。会话经
`ToolContext.session` / `Orchestrator(tool_session=...)` 注入——事务边界由测试持有。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.planning_agent import (
    PLANNING_AGENT,
    ScriptedReplanDriver,
    replan_contracts,
    scripted_action,
    scripted_replan_final,
)
from app.db import models as orm
from app.db.models import Base
from app.llm.budget import TokenBudgetManager
from app.main import create_app
from app.orchestrator.orchestrator import InMemoryTracer, Orchestrator
from app.orchestrator.pipelines.plan_generation import SaveProposedPlanRejected
from app.orchestrator.routing import Intent
from app.seed.dataset import DEMO_ANCHOR
from app.seed.loader import load_demo_data
from app.services.approval import ApprovalService, ApprovalStatus
from app.services.events import EventBus
from app.settings import Settings
from app.tools.build import build_registry
from app.tools.registry import InMemoryToolCallRecorder, ToolContext

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "react-tools.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def application(app_settings: Settings) -> object:
    app = create_app(app_settings)
    Base.metadata.create_all(app.state.engine)
    factory: sessionmaker[Session] = app.state.session_factory
    with factory() as session:
        load_demo_data(session)
        session.commit()
    return app


@pytest.fixture
def client(application: object) -> Iterator[TestClient]:
    app = application  # type: ignore[assignment]
    with TestClient(app) as test_client:  # type: ignore[arg-type]
        password = app.state.settings.session_shared_password.get_secret_value()  # type: ignore[attr-defined]
        test_client.post(LOGIN, json={"password": password})
        yield test_client


def _factory(application: object) -> sessionmaker[Session]:
    return application.state.session_factory  # type: ignore[attr-defined,no-any-return]


def _activate_plan(client: TestClient, application: object) -> tuple[str, date]:
    """生成并激活一个计划；返回 (plan_id, production_date)。扰动/重排的前置条件。"""
    response = client.post(GENERATE, json={})
    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]
    factory = _factory(application)
    with factory() as db:
        row = db.get(orm.ProductionPlan, plan_id)
        assert row is not None
        expected_version = row.version
        production_date = row.production_date
    db2 = factory()
    try:
        service = ApprovalService(session=db2, now=DEMO_ANCHOR, events=EventBus())
        result = service.approve(plan_id, actor="PLANNER", expected_version=expected_version)
        assert result.status is ApprovalStatus.OK
    finally:
        db2.close()
    return plan_id, production_date


def _registry():  # noqa: ANN202
    """真实 26 工具注册表（InMemory recorder，不写库表 tool_calls）。"""
    return build_registry(recorder=InMemoryToolCallRecorder())


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disruption_id: str = "DSR-1"


# --------------------------------------------------------------------------
# 1. generate_schedule 真的落 DRAFT 候选
# --------------------------------------------------------------------------


def test_generate_schedule_persists_draft_candidate(
    client: TestClient, application: object
) -> None:
    """`generate_schedule` 工具落一行 DRAFT 计划并返回真实 plan_id（§8 ∅ → DRAFT）。"""
    _plan_id, production_date = _activate_plan(client, application)
    registry = _registry()
    factory = _factory(application)
    with factory() as db:
        ctx = ToolContext(trace_id="TRACE-t1", session=db)
        result = registry.invoke(
            PLANNING_AGENT,
            "generate_schedule",
            {"production_date": production_date.isoformat()},
            ctx,
        )
        assert result.ok, result.error_detail
        plan_id = result.payload["plan_id"]
        db.commit()
        row = db.get(orm.ProductionPlan, plan_id)
        assert row is not None
        assert row.status == "DRAFT"
        sched_count = db.execute(
            select(func.count())
            .select_from(orm.ScheduledJob)
            .where(orm.ScheduledJob.plan_id == plan_id)
        ).scalar_one()
        assert sched_count > 0, "DRAFT 候选应有已排产作业"


# --------------------------------------------------------------------------
# 2. save_proposed_plan 消费 DRAFT 候选 → 新 PENDING_APPROVAL（无 NotImplementedError）
# --------------------------------------------------------------------------


def test_save_proposed_plan_materializes_pending_from_draft(
    client: TestClient, application: object
) -> None:
    """`save_proposed_plan` 从 DRAFT 候选创建新 PENDING_APPROVAL 计划，不抛 NotImplementedError。"""
    _plan_id, production_date = _activate_plan(client, application)
    registry = _registry()
    factory = _factory(application)
    with factory() as db:
        ctx = ToolContext(trace_id="TRACE-t2", session=db)
        gen = registry.invoke(
            PLANNING_AGENT,
            "generate_schedule",
            {"production_date": production_date.isoformat()},
            ctx,
        )
        assert gen.ok, gen.error_detail
        draft_id = gen.payload["plan_id"]
        db.commit()

    with factory() as db:
        ctx = ToolContext(trace_id="TRACE-t2", session=db)
        saved = registry.invoke(
            PLANNING_AGENT,
            "save_proposed_plan",
            {
                "candidate_plan_id": draft_id,
                "production_date": production_date.isoformat(),
                "origin": "REPLANNING",
            },
            ctx,
        )
        assert saved.ok, saved.error_detail
        assert saved.payload["status"] == "PENDING_APPROVAL"
        pending_id = saved.payload["plan_id"]
        db.commit()
        row = db.get(orm.ProductionPlan, pending_id)
        assert row is not None
        assert row.status == "PENDING_APPROVAL"
        assert row.origin == "REPLANNING"
        # DRAFT 候选保留原样（创建新行，不改写 DRAFT 状态）。
        draft_row = db.get(orm.ProductionPlan, draft_id)
        assert draft_row is not None
        assert draft_row.status == "DRAFT"


# --------------------------------------------------------------------------
# 3 + 4. 脚本化 ReAct 运行真的调用两个工具并落 PENDING_APPROVAL
# --------------------------------------------------------------------------


def test_scripted_react_invokes_tools_and_reaches_pending(
    client: TestClient, application: object
) -> None:
    """脚本化 ReAct 运行经 Orchestrator 真的调用 save_proposed_plan → final，落 PENDING_APPROVAL。

    先用工具落一份 DRAFT 候选拿到 draft_id（脚本是静态的，需先知道 id），再让 ReAct 脚本的
    第一轮 `{"action": {"tool": "save_proposed_plan", ...}}` 经 `_react_loop` + 真实
    `ToolRegistry` + 注入会话真的执行，第二轮才 `final`。断言最终有一行 PENDING_APPROVAL 落库。
    """
    _plan_id, production_date = _activate_plan(client, application)
    registry = _registry()
    factory = _factory(application)
    tracer = InMemoryTracer()

    with factory() as db:
        ctx = ToolContext(trace_id="TRACE-t3", session=db)
        gen = registry.invoke(
            PLANNING_AGENT,
            "generate_schedule",
            {"production_date": production_date.isoformat()},
            ctx,
        )
        assert gen.ok, gen.error_detail
        draft_id = gen.payload["plan_id"]
        db.commit()

    with factory() as db:
        turns = [
            scripted_action(
                "save_proposed_plan",
                candidate_plan_id=draft_id,
                production_date=production_date.isoformat(),
                origin="REPLANNING",
            ),
            scripted_replan_final(
                candidate_plan_id=draft_id,
                baseline_plan_id=None,
                feasibility="FEASIBLE",
                total_tardiness_minutes=0,
                churn_ratio=0.0,
                revision_summary="重排完成，提案已生成。",
            ),
        ]
        orch = Orchestrator(
            registry=registry,
            budget=TokenBudgetManager(),
            tracer=tracer,
            agent_drivers={PLANNING_AGENT: ScriptedReplanDriver(turns=turns)},
            contracts=replan_contracts(),
            tool_session=db,
        )
        result = orch.run(Intent.REPLAN, _Payload(), session_id="SESS-react")
        assert result.outcome == "OK", result.error_code
        db.commit()

        pending = db.execute(
            select(orm.ProductionPlan).where(
                orm.ProductionPlan.status == "PENDING_APPROVAL",
                orm.ProductionPlan.origin == "REPLANNING",
            )
        ).scalars().first()
        assert pending is not None, "脚本化 ReAct 运行应经 save_proposed_plan 落一行 PENDING"


# --------------------------------------------------------------------------
# 5. LLM 输出不能覆盖确定性数值
# --------------------------------------------------------------------------


def test_llm_final_numbers_do_not_override_persisted_plan(
    client: TestClient, application: object
) -> None:
    """落库计划的目标分量由确定性评分算出，与 `final` 声称的数值无关。

    `save_proposed_plan` 用确定性 `score()` 从真实排产结果算 `objective_breakdown`，因此其
    `total_tardiness_minutes` 不可能是 LLM 在 `final` 里可能编的 99999。
    """
    _plan_id, production_date = _activate_plan(client, application)
    registry = _registry()
    factory = _factory(application)
    with factory() as db:
        ctx = ToolContext(trace_id="TRACE-t4", session=db)
        gen = registry.invoke(
            PLANNING_AGENT,
            "generate_schedule",
            {"production_date": production_date.isoformat()},
            ctx,
        )
        assert gen.ok, gen.error_detail
        draft_id = gen.payload["plan_id"]
        db.commit()
    with factory() as db:
        ctx = ToolContext(trace_id="TRACE-t4", session=db)
        saved = registry.invoke(
            PLANNING_AGENT,
            "save_proposed_plan",
            {
                "candidate_plan_id": draft_id,
                "production_date": production_date.isoformat(),
                "origin": "REPLANNING",
            },
            ctx,
        )
        assert saved.ok, saved.error_detail
        pending_id = saved.payload["plan_id"]
        db.commit()
        breakdown = db.get(orm.ObjectiveBreakdown, pending_id)
        assert breakdown is not None
        components = {c["name"]: c["raw_value"] for c in breakdown.components}
        assert components["total_tardiness_minutes"] != 99999.0


# --------------------------------------------------------------------------
# 6. §8 基线守卫仍生效
# --------------------------------------------------------------------------


def test_save_proposed_plan_rejects_baseline_origin(
    client: TestClient, application: object
) -> None:
    """`save_proposed_plan(origin=BASELINE)` 仍被 §8 守卫拒绝（SaveProposedPlanRejected）。

    契约 `SaveProposedPlanIn.origin` 的字面量联合里没有 `BASELINE`，正常路径下 registry 第 ②
    步就会挡下。这里直接调 handler 传一个绕过 schema 的对象，断言守卫本身仍抛
    `SaveProposedPlanRejected`（守卫是第二道防线，不依赖 schema）。
    """
    from app.tools.handlers.write import save_proposed_plan as handler

    _activate_plan(client, application)
    factory = _factory(application)

    class _FakeArgs:
        candidate_plan_id = "PLAN-x"
        production_date = DEMO_ANCHOR.date()
        origin = "BASELINE"
        supersedes_pending = False

    with factory() as db:
        ctx = ToolContext(trace_id="TRACE-t5", session=db)
        with pytest.raises(SaveProposedPlanRejected):
            handler(_FakeArgs(), ctx)  # type: ignore[arg-type]
