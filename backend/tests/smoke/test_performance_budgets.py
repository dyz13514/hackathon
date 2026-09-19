"""性能时限冒烟（任务 12.5，R27.3/R5.1/R9.2/R16.8/R2.2，design.md Testing Strategy §1/§5）。

tasks.md 12.5 点名的时限预算，在演示数据集规模（≤20 订单、≤60 作业、≤10 机器）下各跑
**一次**并断言用时在预算内（时限与运行环境相关，跑 100 次无额外信息——见 tasks.md）：

- 单次排产 `Scheduling_Core.generate_schedule` ≤ **2 秒**（R27.3 / R5.1）；
- 计划生成周期（确定性流水线）≤ **60 秒**（R5.1）；
- 扰动响应（`replan`）≤ **90 秒**（R9.2）；
- 单场景 What-if 模拟（`run_sandbox`）≤ **30 秒**（R16.8）；
- 列映射提案（确定性 `propose_mapping`）≤ **30 秒**（R2.2）。

## 为什么在服务/内核层而非 FastAPI 层计时

这些预算是**确定性计算**的时限，与 HTTP 层无关；在内核/服务层直接计时排除了网络与序列化
噪声，度量的正是被点名的那步计算。这也**规避了一处与本任务无关的既有缺陷**：`app.main`
（`create_app`）因 `app/api/preferences.py` 的 DELETE 端点在锁定的 fastapi==0.115.5 下 import
期 `AssertionError`（`status_code=204` 带响应体）而无法导入，连带 `/health` 冒烟也被阻断——
该缺陷不在本任务范围内修复，记录在案。`/health` 端点的可用性与状态页首屏 ≤3 秒由
`tests/smoke/test_health.py`（当前被同一缺陷阻断）与前端 Vitest 承担。

时限设得**宽松**是刻意的：演示规模下真实用时远低于预算，宽松阈值让它当「有没有意外劣化到
一个量级」的探测器，而不是对机器性能敏感的绊线。全程 `LLM_MODE=STUB`/`REPLAY`，零 Bedrock。
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.replanner import Disruption, replan
from app.core.scheduler import generate_schedule
from app.db import audit
from app.db.models import Base
from app.db.session import create_db_engine, create_session_factory, session_scope
from app.seed import dataset
from app.seed.loader import load_demo_data
from app.services.snapshot_loader import load_snapshot
from app.settings import Settings

# 时限预算（秒）——tasks.md 12.5 逐条点名。
SINGLE_SCHEDULE_BUDGET_S = 2.0
PLAN_GENERATION_BUDGET_S = 60.0
DISRUPTION_RESPONSE_BUDGET_S = 90.0
SCENARIO_SIMULATION_BUDGET_S = 30.0
MAPPING_PROPOSAL_BUDGET_S = 30.0

NOW = dataset.DEMO_ANCHOR
PRODUCTION_DATE = dataset.DEMO_ANCHOR.date()


@pytest.fixture
def seeded() -> Iterator[tuple[sessionmaker[Session], Engine]]:
    """演示数据集内存库（规模 ≤ tasks.md 12.5 的上限）。"""
    settings = Settings(
        database_url="sqlite:///:memory:",
        session_shared_password=SecretStr("test-shared-password"),
        session_secret_key=SecretStr("test-secret-key-that-is-long-enough-32"),
        llm_mode="STUB",
    )
    engine = create_db_engine(settings)
    Base.metadata.create_all(engine)
    audit.set_audit_engine(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        load_demo_data(session)
    try:
        yield factory, engine
    finally:
        audit.set_audit_engine(None)
        engine.dispose()


def test_single_schedule_within_two_seconds(
    seeded: tuple[sessionmaker[Session], Engine],
) -> None:
    """单次 `generate_schedule` ≤ 2 秒（R27.3 / R5.1，演示规模）。"""
    factory, _ = seeded
    with factory() as session:
        snapshot = load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)

    start = time.perf_counter()
    candidate = generate_schedule(snapshot)
    elapsed = time.perf_counter() - start

    assert candidate.scheduled_jobs, "演示数据下应能排出作业"
    assert elapsed <= SINGLE_SCHEDULE_BUDGET_S, (
        f"单次排产耗时 {elapsed:.3f}s 超过 {SINGLE_SCHEDULE_BUDGET_S}s 预算"
    )


def test_plan_generation_cycle_within_sixty_seconds(
    seeded: tuple[sessionmaker[Session], Engine],
) -> None:
    """确定性计划生成周期 ≤ 60 秒（R5.1，演示规模）。"""
    from app.orchestrator.pipelines import plan_generation

    factory, _ = seeded
    start = time.perf_counter()
    with factory() as session:
        result = plan_generation.run_plan_generation(
            session, now=NOW, production_date=PRODUCTION_DATE, session_id="perf-plan"
        )
    elapsed = time.perf_counter() - start

    assert result.status == "PENDING_APPROVAL"
    assert elapsed <= PLAN_GENERATION_BUDGET_S, (
        f"计划生成周期耗时 {elapsed:.3f}s 超过 {PLAN_GENERATION_BUDGET_S}s 预算"
    )


def test_disruption_response_within_ninety_seconds(
    seeded: tuple[sessionmaker[Session], Engine],
) -> None:
    """扰动响应（`replan`）≤ 90 秒（R9.2，演示规模）。"""
    factory, _ = seeded
    with factory() as session:
        snapshot = load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)
    active = generate_schedule(snapshot)

    disruption = Disruption(type="MACHINE_BREAKDOWN", machine_id=dataset.BOTTLENECK_MACHINE_ID)
    start = time.perf_counter()
    result = replan(active, disruption, snapshot)
    elapsed = time.perf_counter() - start

    assert result.candidate is not None
    assert elapsed <= DISRUPTION_RESPONSE_BUDGET_S, (
        f"扰动响应耗时 {elapsed:.3f}s 超过 {DISRUPTION_RESPONSE_BUDGET_S}s 预算"
    )


def test_scenario_simulation_within_thirty_seconds(
    seeded: tuple[sessionmaker[Session], Engine],
) -> None:
    """单场景 What-if 模拟（`run_sandbox`）≤ 30 秒（R16.8，演示规模）。

    需要一个 `ACTIVE` 计划作对比基准：生成 → 审批激活 → 跑一次沙箱推演并计时。
    """
    from app.db import models as orm
    from app.orchestrator.pipelines import plan_generation
    from app.services.approval import ApprovalService, ApprovalStatus
    from app.services.events import EventBus
    from app.services.sandbox import run_sandbox
    from app.tools.models import ChangeMaterialAvailability

    factory, _ = seeded
    with factory() as session:
        result = plan_generation.run_plan_generation(
            session, now=NOW, production_date=PRODUCTION_DATE, session_id="perf-scn"
        )
    # 激活（run_sandbox 需要一个 ACTIVE 计划作对比基准）。
    with factory() as session:
        plan_row = session.get(orm.ProductionPlan, result.plan_id)
        assert plan_row is not None
        version = plan_row.version
    with factory() as session:
        service = ApprovalService(session=session, now=NOW, events=EventBus())
        approved = service.approve(result.plan_id, actor="PLANNER", expected_version=version)
        assert approved.status is ApprovalStatus.OK

    start = time.perf_counter()
    with factory() as session:
        sandbox_result = run_sandbox(
            session,
            mutations=[
                ChangeMaterialAvailability(
                    material_id=dataset.SCARCE_MATERIAL_ID, quantity_available=0.0
                )
            ],
            now=NOW,
        )
    elapsed = time.perf_counter() - start

    assert sandbox_result.feasibility in {"FEASIBLE", "PARTIAL", "NO_FEASIBLE_PLAN"}
    assert elapsed <= SCENARIO_SIMULATION_BUDGET_S, (
        f"单场景模拟耗时 {elapsed:.3f}s 超过 {SCENARIO_SIMULATION_BUDGET_S}s 预算"
    )


def test_mapping_proposal_within_thirty_seconds() -> None:
    """列映射提案（确定性 `propose_mapping`）≤ 30 秒（R2.2）。

    用固化的脏表格样例——它含混合日期格式、多余列、缺失表头，是最坏情形的代表性输入。
    """
    from app.seed.fixtures import DIRTY_ORDERS_CSV
    from app.services.ingestion import propose_mapping
    from app.services.spreadsheet import parse_spreadsheet

    parsed = parse_spreadsheet(
        filename="dirty_orders.csv", content=DIRTY_ORDERS_CSV.read_bytes()
    )
    start = time.perf_counter()
    proposal = propose_mapping(parsed, entity_type_hint="ORDER")
    elapsed = time.perf_counter() - start

    assert proposal["field_mappings"], "应产出若干字段映射"
    assert elapsed <= MAPPING_PROPOSAL_BUDGET_S, (
        f"列映射提案耗时 {elapsed:.3f}s 超过 {MAPPING_PROPOSAL_BUDGET_S}s 预算"
    )
