"""`save_proposed_plan` 的前置条件校验（任务 3.3，design.md Data Models §8）。

design.md §8 在状态机表下补的硬约束：

> `DRAFT` 状态的 `BASELINE` 计划与沙箱产生的候选计划永不迁移到 `PENDING_APPROVAL`：
> `save_proposed_plan` 的实现校验 `origin != 'BASELINE'` 且 `plan.produced_in_sandbox == false`。

本文件守这道校验的三个面：

1. `origin == 'BASELINE'` → 抛 `SaveProposedPlanRejected`（基线永不进审批流）。
2. `produced_in_sandbox == True` → 抛 `SaveProposedPlanRejected`（沙箱候选永不进审批流）。
3. 正常路径（`origin = PLAN_GENERATION`、`produced_in_sandbox = False`）落成
   `PENDING_APPROVAL`，且没有 `status` 入参——状态是硬编码的，调用方无从选择。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.scoring import ObjectiveWeights
from app.db import audit
from app.db.models import Base, ProductionPlan
from app.db.session import create_db_engine, create_session_factory, session_scope
from app.orchestrator.pipelines import plan_generation
from app.orchestrator.pipelines.plan_generation import (
    BASELINE_ORIGIN,
    FORMAL_ORIGIN,
    PENDING_STATUS,
    SaveProposedPlanRejected,
    save_proposed_plan,
)
from app.seed import dataset
from app.seed.loader import load_demo_data
from app.settings import Settings

NOW = dataset.DEMO_ANCHOR
PRODUCTION_DATE = dataset.DEMO_ANCHOR.date()


def _settings(db_path: str) -> Settings:
    return Settings(
        database_url=f"sqlite:///{db_path}",
        session_shared_password=SecretStr("test-shared-password"),
        session_secret_key=SecretStr("test-secret-key-that-is-long-enough-32"),
        llm_mode="STUB",
    )


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_db_engine(_settings((tmp_path / "guard.db").as_posix()))
    Base.metadata.create_all(engine)
    audit.set_audit_engine(engine)
    # 单一会话工厂：`input_snapshot_version` 推进钩子挂在工厂上，seed 与后续运行必须共用
    # 同一个工厂，否则 seed 触发的版本推进与运行期读到的版本号不同源（FK 指向 0）。
    single = create_session_factory(engine)
    with session_scope(single) as session:
        load_demo_data(session)
    yield single
    audit.set_audit_engine(None)
    engine.dispose()


def _run(factory: sessionmaker[Session]) -> plan_generation.PlanGenerationResult:
    """跑一次正常流水线，返回结果对象（供 guard 单元测试复用它的 result）。"""
    with factory() as session:
        return plan_generation.run_plan_generation(
            session, now=NOW, production_date=PRODUCTION_DATE, session_id="s-guard"
        )


def test_happy_path_saves_pending_approval(factory: sessionmaker[Session]) -> None:
    """正常路径：origin=PLAN_GENERATION、produced_in_sandbox=False → PENDING_APPROVAL。"""
    result = _run(factory)
    assert result.status == PENDING_STATUS
    with factory() as session:
        plan = session.get(ProductionPlan, result.plan_id)
        assert plan is not None
        assert plan.status == PENDING_STATUS
        assert plan.origin == FORMAL_ORIGIN


def test_baseline_origin_is_rejected(factory: sessionmaker[Session]) -> None:
    """`origin == 'BASELINE'` → `SaveProposedPlanRejected`，绝不把基线推进成待审批。"""
    result = _run(factory)
    with factory() as session:
        with pytest.raises(SaveProposedPlanRejected, match="基线"):
            save_proposed_plan(
                session,
                result=result,
                weights=ObjectiveWeights(),
                origin=BASELINE_ORIGIN,
                produced_in_sandbox=False,
            )


def test_sandbox_result_is_rejected(factory: sessionmaker[Session]) -> None:
    """`produced_in_sandbox == True` → `SaveProposedPlanRejected`，沙箱候选不进审批流。"""
    result = _run(factory)
    with factory() as session:
        with pytest.raises(SaveProposedPlanRejected, match="沙箱"):
            save_proposed_plan(
                session,
                result=result,
                weights=ObjectiveWeights(),
                origin=FORMAL_ORIGIN,
                produced_in_sandbox=True,
            )


def test_save_proposed_plan_has_no_status_parameter() -> None:
    """`save_proposed_plan` 的签名里没有 `status` 参数——状态不是调用方能选的（R11.8）。"""
    import inspect

    params = set(inspect.signature(save_proposed_plan).parameters)
    assert "status" not in params
    # 校验入参就是 §8 硬约束点名的那两个 + 落库所需。
    assert {"origin", "produced_in_sandbox"} <= params


def test_generation_pipeline_produces_no_baseline_pending(
    factory: sessionmaker[Session],
) -> None:
    """整条流水线跑完后，唯一的 PENDING_APPROVAL 计划来源是 PLAN_GENERATION，不是 BASELINE。"""
    _run(factory)
    with factory() as session:
        pending = list(
            session.execute(
                select(ProductionPlan).where(ProductionPlan.status == PENDING_STATUS)
            ).scalars()
        )
        assert pending, "seed 数据下应生成一个待审批计划"
        assert all(plan.origin == FORMAL_ORIGIN for plan in pending)
        assert all(plan.origin != BASELINE_ORIGIN for plan in pending)
