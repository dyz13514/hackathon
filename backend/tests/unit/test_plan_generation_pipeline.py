"""计划生成流水线的示例测试（任务 2.12，**非可选**，承接原属性 24 的一半）。

原属性 24「初始计划生成是一条固定序列」在收敛后拆成两半：本文件用**示例测试**锁住
「六步、这个顺序、不多不少」；另一半（周期 token 恒为该路径的确定性成本）由 `EVAL-015`
承担（tasks.md 2.12 与 5.x）。

三组断言，各守一个不同的失效模式：

1. **固定 6 步序列**——把六个步骤函数各换成一个记录器，断言调用顺序恰为
   `load_snapshot → generate_schedule → check_constraints → evaluate_schedule
   → compute_baseline → save_proposed_plan`。多一步、少一步、换个顺序都会让它红。
   这是「没有 LLM 选择下一个工具」在代码层面的证据（R21.11）。

2. **五表同事务落盘 + R5.5 六项齐全**——用真实 seed 数据跑一次，断言
   `production_plans` / `scheduled_jobs` / `unschedulable_jobs` / `objective_breakdowns`
   / `baseline_comparisons` 全部写入，且计划状态硬编码为 `PENDING_APPROVAL`。

3. **token 消耗 = 0 + 基线同口径**——断言这条路径的 `Trace` 是 `PIPELINE` 且 token 汇总
   为 0（design.md §2.1 的 Note），基线与正式计划的 `snapshot_version` 相等（R19.2）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core import baseline as baseline_mod
from app.core import scoring as scoring_mod
from app.core import validation as validation_mod
from app.db import audit
from app.db.models import (
    Base,
    BaselineComparison,
    ObjectiveBreakdown,
    ProductionPlan,
    ScheduledJob,
    Trace,
)
from app.db.session import create_db_engine, create_session_factory, session_scope
from app.orchestrator.pipelines import plan_generation
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
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_db_engine(_settings((tmp_path / "pipeline.db").as_posix()))
    Base.metadata.create_all(eng)
    # 审计写入走独立引擎——测试里指向同一个临时库，使 PLAN_GENERATION 审计能落盘。
    audit.set_audit_engine(eng)
    yield eng
    audit.set_audit_engine(None)
    eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def seeded(factory: sessionmaker[Session]) -> sessionmaker[Session]:
    with session_scope(factory) as session:
        load_demo_data(session)
    return factory


# --------------------------------------------------------------------------
# 1. 固定 6 步序列（承接原属性 24 的一半）
# --------------------------------------------------------------------------


def test_pipeline_calls_the_six_steps_in_fixed_order(
    seeded: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """六个步骤函数按固定顺序被调用一次，无「LLM 选择下一个工具」环节（R21.11）。

    每个步骤函数换成一个记录调用顺序的包装：包装内仍调用真实实现（流水线要能跑到底、真的
    写库），但在 `calls` 里追加自己的名字。断言 `calls` 恰为设计规定的六步序列。
    """
    calls: list[str] = []

    real_load = plan_generation.load_snapshot
    real_generate = plan_generation.generate_schedule
    real_validate = plan_generation.validate
    real_score = plan_generation.score
    real_fcfs = plan_generation.fcfs

    def rec_load(*args: object, **kwargs: object) -> object:
        calls.append("load_snapshot")
        return real_load(*args, **kwargs)  # type: ignore[arg-type]

    def rec_generate(*args: object, **kwargs: object) -> object:
        calls.append("generate_schedule")
        return real_generate(*args, **kwargs)  # type: ignore[arg-type]

    def rec_validate(*args: object, **kwargs: object) -> object:
        calls.append("check_constraints")
        return real_validate(*args, **kwargs)  # type: ignore[arg-type]

    def rec_score(*args: object, **kwargs: object) -> object:
        calls.append("evaluate_schedule")
        return real_score(*args, **kwargs)  # type: ignore[arg-type]

    def rec_fcfs(*args: object, **kwargs: object) -> object:
        calls.append("compute_baseline")
        return real_fcfs(*args, **kwargs)  # type: ignore[arg-type]

    real_persist = plan_generation._persist

    def rec_persist(*args: object, **kwargs: object) -> object:
        calls.append("save_proposed_plan")
        return real_persist(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(plan_generation, "load_snapshot", rec_load)
    monkeypatch.setattr(plan_generation, "generate_schedule", rec_generate)
    monkeypatch.setattr(plan_generation, "validate", rec_validate)
    monkeypatch.setattr(plan_generation, "score", rec_score)
    monkeypatch.setattr(plan_generation, "fcfs", rec_fcfs)
    monkeypatch.setattr(plan_generation, "_persist", rec_persist)

    with seeded() as session:
        plan_generation.run_plan_generation(
            session, now=NOW, production_date=PRODUCTION_DATE, session_id="s-1"
        )

    assert calls == [
        "load_snapshot",
        "generate_schedule",
        "check_constraints",
        "evaluate_schedule",
        "compute_baseline",
        "save_proposed_plan",
    ]


def test_pipeline_does_not_import_the_llm_adapter() -> None:
    """流水线模块不 import `app.llm.*`：确定性路径与 Bedrock 无耦合（R21.11、任务 4 检查点）。

    这条路径必须在**无 Bedrock 凭证**时端到端可用。import 了适配层不等于会调用它，但把
    「这里根本不碰 LLM 适配层」做成一条可执行断言，比靠注释保证更可靠。检查的是 import
    语句（`from app.llm` / `import app.llm`），而非注释里出现的「LLM」字样。
    """
    import ast

    import app.orchestrator.pipelines.plan_generation as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)

    offending = [name for name in imported if name.startswith("app.llm")]
    assert not offending, f"流水线不应 import LLM 适配层，却引用了 {offending}"


# --------------------------------------------------------------------------
# 2. 五表同事务落盘 + R5.5 六项齐全
# --------------------------------------------------------------------------


def test_pipeline_persists_all_five_tables_and_status_is_pending(
    seeded: sessionmaker[Session], engine: Engine
) -> None:
    """五张表全部写入，正式计划状态硬编码 PENDING_APPROVAL（design.md §4.4、R11.8）。"""
    with seeded() as session:
        result = plan_generation.run_plan_generation(
            session, now=NOW, production_date=PRODUCTION_DATE, session_id="s-1"
        )

    plan_id = result.plan_id
    with engine.connect() as conn:
        plan_status = conn.execute(
            select(ProductionPlan.status).where(ProductionPlan.plan_id == plan_id)
        ).scalar_one()
        sched_count = conn.execute(
            select(func.count()).select_from(ScheduledJob).where(ScheduledJob.plan_id == plan_id)
        ).scalar_one()
        breakdown = conn.execute(
            select(ObjectiveBreakdown).where(ObjectiveBreakdown.plan_id == plan_id)
        ).first()
        bc = conn.execute(
            select(BaselineComparison).where(BaselineComparison.plan_id == plan_id)
        ).first()
        # 基线计划头也落库（status=DRAFT, origin=BASELINE），且不进审批流。
        baseline_row = conn.execute(
            select(ProductionPlan.status, ProductionPlan.origin).where(
                ProductionPlan.plan_id == result.baseline.baseline_plan_id
            )
        ).one()

    assert plan_status == "PENDING_APPROVAL"
    assert sched_count == len(result.candidate.scheduled_jobs)
    assert sched_count > 0, "seed 数据下应能排出作业"
    assert breakdown is not None
    assert bc is not None
    assert baseline_row.status == "DRAFT"
    assert baseline_row.origin == "BASELINE"


def test_result_carries_all_six_r5_5_fields(seeded: sessionmaker[Session]) -> None:
    """结果含 R5.5 逐字列出的六项。"""
    with seeded() as session:
        result = plan_generation.run_plan_generation(
            session, now=NOW, production_date=PRODUCTION_DATE, session_id="s-1"
        )

    assert result.feasibility in {"FEASIBLE", "PARTIAL", "NO_FEASIBLE_PLAN"}
    assert result.candidate.scheduled_jobs is not None
    assert result.candidate.unschedulable_jobs is not None
    assert len(result.objective_breakdown.components) == 7  # R7.1
    assert result.baseline is not None
    assert result.generated_by_trace_id.startswith("TRACE-")


def test_referential_integrity_failure_leaves_no_partial_rows(
    factory: sessionmaker[Session], engine: Engine
) -> None:
    """引用完整性错误在写入前抛出，库里不留半成品（R5.6 + 五表同事务）。

    构造坏数据：一个订单指向不存在的产品。`load_snapshot` 的预检应抛 `DataIntegrityError`，
    流水线在任何写入之前终止，因此 `production_plans` 保持为空。
    """
    from app.core.snapshot import DataIntegrityError
    from app.db import models as orm

    with session_scope(factory) as session:
        load_demo_data(session)
        # 软删除一个仍被活着的订单引用的产品：`load_snapshot` 排除 REVERTED 记录（R3.4），
        # 于是那个订单指向一个「不在快照里」的产品——正是 R5.6 的悬空引用形态。直接改
        # `order.product_id` 会被库级外键（orders.product_id → products）当场拒绝，因此
        # 这里改父实体的可见性，而非制造一个库都不允许的悬空外键。
        order = session.get(orm.Order, dataset.ORDERS[0].order_id)
        assert order is not None
        product = session.get(orm.Product, order.product_id)
        assert product is not None
        product.record_status = "REVERTED"

    with pytest.raises(DataIntegrityError), factory() as session:
        plan_generation.run_plan_generation(
            session, now=NOW, production_date=PRODUCTION_DATE, session_id="s-1"
        )

    with engine.connect() as conn:
        plan_count = conn.execute(select(func.count()).select_from(ProductionPlan)).scalar_one()
    assert plan_count == 0, "预检失败却写入了计划头——五表原子性被破坏"


# --------------------------------------------------------------------------
# 3. token 消耗 = 0 + 基线同口径
# --------------------------------------------------------------------------


def test_pipeline_consumes_zero_tokens(seeded: sessionmaker[Session], engine: Engine) -> None:
    """这条路径的 Trace 是 PIPELINE 且 token 汇总为 0（design.md §2.1 的 Note）。"""
    with seeded() as session:
        result = plan_generation.run_plan_generation(
            session, now=NOW, production_date=PRODUCTION_DATE, session_id="s-1"
        )

    with engine.connect() as conn:
        trace = conn.execute(
            select(Trace).where(Trace.trace_id == result.generated_by_trace_id)
        ).one()

    assert trace.mode == "PIPELINE"
    assert trace.agent is None
    assert trace.total_input_tokens == 0
    assert trace.total_output_tokens == 0


def test_baseline_runs_on_the_same_snapshot_version(seeded: sessionmaker[Session]) -> None:
    """基线与正式计划的 snapshot_version 相等（R19.2、属性 37 的核心不变量）。"""
    with seeded() as session:
        result = plan_generation.run_plan_generation(
            session, now=NOW, production_date=PRODUCTION_DATE, session_id="s-1"
        )

    assert result.baseline.snapshot_version == result.input_snapshot_version


def test_reused_kernel_modules_are_the_real_ones() -> None:
    """哨兵：确保测试引用的内核模块就是流水线使用的那几个（防止 monkeypatch 打偏）。"""
    assert plan_generation.fcfs is baseline_mod.fcfs
    assert plan_generation.score is scoring_mod.score
    assert plan_generation.validate is validation_mod.validate
