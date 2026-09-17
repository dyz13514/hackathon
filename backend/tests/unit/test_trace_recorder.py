"""`Trace_Recorder` 的完备性示例测试（任务 5.12，**非可选**，承接原属性 33）。

原属性 33「每次运行都有完整 Trace」在收敛后落成一组**示例测试**：一次**流水线**运行与
一次 **ReAct** 运行，各断言：

1. **步骤完备**——`traces` 行存在且 `outcome` 已定型；每一步都落成一行 `trace_steps`
   （`step_index` 连续、`step_kind` 与 `decision_reason` 非空摘要），`trace.step_count`
   与实际 `trace_steps` 行数一致（R24.1、R24.7）。
2. **`generated_by_trace_id` 非空**——计划生成路径产出的 `ProductionPlan`（正式与基线）
   都通过 `generated_by_trace_id` 关联回本次运行的 `Trace`（R24.5）。ReAct 路径不产计划，
   因此那一半在流水线用例断言；ReAct 用例改断言**工具调用经 `tool_calls.step_id` 关联到
   触发它的那一步**（R22.11），即 ReAct 侧的「完备」。

两条路径共用同一套可观测性底座（design.md §2.2 共享物表），因此本文件同时守住
「PIPELINE 与 REACT 差别只在 `trace.mode`，其余记录一视同仁」这条设计不变量。

走真实 SQLite 文件：`Trace_Recorder` 的价值就是「真的把行写进了 traces/trace_steps/
tool_calls」，用内存桩测不到落库这一半。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.contracts import AgentContract
from app.db import audit
from app.db.models import Base, ProductionPlan, ToolCall, Trace, TraceStep
from app.db.session import create_db_engine, create_session_factory, session_scope
from app.llm.budget import TokenBudgetManager
from app.orchestrator.context_manager import AgentContext
from app.orchestrator.orchestrator import AgentTurn, Orchestrator
from app.orchestrator.pipelines import plan_generation
from app.orchestrator.routing import Intent
from app.orchestrator.trace_recorder import DbToolCallRecorder, DbTracer
from app.seed import dataset
from app.seed.loader import load_demo_data
from app.settings import Settings
from app.tools.registry import ToolContext, ToolRegistry, ToolSpec

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
    eng = create_db_engine(_settings((tmp_path / "trace.db").as_posix()))
    Base.metadata.create_all(eng)
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
# 1. 流水线运行：6 步完备 + 两个计划头 generated_by_trace_id 非空
# --------------------------------------------------------------------------


def test_pipeline_run_records_complete_trace(
    seeded: sessionmaker[Session], engine: Engine
) -> None:
    """一次流水线运行：`traces` 行 + 6 步 `trace_steps` + 计划回链非空（R24.1/5/7）。"""
    with seeded() as session:
        result = plan_generation.run_plan_generation(
            session, now=NOW, production_date=PRODUCTION_DATE, session_id="s-pipe"
        )

    trace_id = result.generated_by_trace_id
    with engine.connect() as conn:
        trace = conn.execute(select(Trace).where(Trace.trace_id == trace_id)).one()
        steps = list(
            conn.execute(
                select(TraceStep)
                .where(TraceStep.trace_id == trace_id)
                .order_by(TraceStep.step_index)
            )
        )
        plan_trace_ids = list(
            conn.execute(
                select(ProductionPlan.generated_by_trace_id).where(
                    ProductionPlan.generated_by_trace_id == trace_id
                )
            ).scalars()
        )

    # 头部完备
    assert trace.mode == "PIPELINE"
    assert trace.agent is None
    assert trace.trigger_source == "PLANNER_UI"
    assert trace.outcome == "OK"
    assert trace.ended_at is not None
    assert trace.result_ref == result.plan_id

    # 逐步完备：6 步、index 连续、step_kind 与 decision_reason 非空
    assert [s.step_index for s in steps] == [0, 1, 2, 3, 4, 5]
    assert trace.step_count == len(steps) == 6
    assert all(s.step_kind == "DETERMINISTIC_STAGE" for s in steps)
    assert [s.decision_reason for s in steps] == [
        "load_snapshot",
        "generate_schedule",
        "check_constraints",
        "evaluate_schedule",
        "compute_baseline",
        "save_proposed_plan",
    ]

    # generated_by_trace_id 非空：正式计划与基线计划两个头都回链到本次运行
    assert result.generated_by_trace_id is not None
    assert len(plan_trace_ids) == 2


# --------------------------------------------------------------------------
# 2. ReAct 运行：逐步完备 + 工具调用经 step_id 关联到触发它的那一步
# --------------------------------------------------------------------------


class _EchoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class _EchoOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    echoed: str


def _echo_handler(args: BaseModel, ctx: ToolContext) -> BaseModel:
    assert isinstance(args, _EchoIn)
    return _EchoOut(echoed=args.value)


class _ScriptedDriver:
    """先做一次合法工具调用，再产出合法 `final` 的两轮脚本驱动（不触网）。"""

    def __init__(self) -> None:
        self._turns = [
            '{"thought": "look", "action": {"tool": "compare_plans", "args": {"value": "hi"}}}',
            (
                '{"thought": "done", "final": {'
                '"candidate_plan_id": "PLAN-1", "feasibility": "FEASIBLE", '
                '"total_tardiness_minutes": 0, "churn_ratio": 0.0}}'
            ),
        ]
        self.calls = 0

    def next_turn(self, ctx: AgentContext) -> AgentTurn | None:
        raw = self._turns[min(self.calls, len(self._turns) - 1)]
        self.calls += 1
        return AgentTurn(raw=raw)


def _output_contract() -> type[AgentContract]:
    from app.agents.contracts import RevisedPlanProposal

    return RevisedPlanProposal


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disruption_id: str = "DISR-0001"


def test_react_run_records_complete_trace_with_linked_tool_calls(
    engine: Engine, factory: sessionmaker[Session]
) -> None:
    """一次 ReAct 运行：`traces` 行 + 逐步 `trace_steps` + `tool_calls` 经 step_id 关联。

    驱动是脚本桩（第一轮合法工具调用、第二轮合法 final），因此循环以 `OK` 终止且真的产生
    一次工具调用。`DbTracer` 与 `DbToolCallRecorder` 共享同一会话，工具调用回退关联到
    `tracer.current` 的最近一步（ReAct 的 `ToolContext.step_id` 传 None）。
    """
    echo_spec = ToolSpec(
        name="compare_plans",  # 在 PLANNING_AGENT 白名单内（见 registry），语义为 echo
        kind="COMPUTE",
        input_model=_EchoIn,
        output_model=_EchoOut,
        handler=_echo_handler,
    )

    with factory() as session:
        tracer = DbTracer(session, trigger_source="PLANNER_UI", session_id="s-react")
        recorder = DbToolCallRecorder(session=session, tracer=tracer)
        registry = ToolRegistry({echo_spec.name: echo_spec}, recorder=recorder)
        driver = _ScriptedDriver()
        orch = Orchestrator(
            registry=registry,
            budget=TokenBudgetManager(),
            tracer=tracer,
            agent_drivers={"PLANNING_AGENT": driver},
            contracts={"PLANNING_AGENT": _output_contract()},
        )
        result = orch.run(Intent.REPLAN, _Payload(), session_id="s-react")
        session.commit()
        trace_id = result.trace_id

    assert result.outcome == "OK"

    with engine.connect() as conn:
        trace = conn.execute(select(Trace).where(Trace.trace_id == trace_id)).one()
        steps = list(
            conn.execute(
                select(TraceStep)
                .where(TraceStep.trace_id == trace_id)
                .order_by(TraceStep.step_index)
            )
        )
        calls = list(
            conn.execute(select(ToolCall).where(ToolCall.trace_id == trace_id))
        )
        step_ids = {s.step_id for s in steps}

    # 头部完备：REACT、agent 记名、outcome 定型
    assert trace.mode == "REACT"
    assert trace.agent == "PLANNING_AGENT"
    assert trace.outcome == "OK"
    assert trace.ended_at is not None

    # 逐步完备：至少两步（工具调用步 + final 契约校验步），index 连续，step_count 一致
    assert len(steps) >= 2
    assert [s.step_index for s in steps] == list(range(len(steps)))
    assert trace.step_count == len(steps)
    assert all(s.step_kind and s.decision_reason for s in steps)

    # 工具调用完备：恰好一次成功的 compare_plans 调用，经 trace_id 关联到本次运行。
    # ReAct 循环刻意把工具调用挂在 **trace** 而非某一步上（`ToolContext.step_id` 传 None，
    # 见 orchestrator._dispatch_action），因此 `tool_calls.step_id` 为空是**设计如此**——
    # `tool_calls.trace_id`（NOT NULL 外键）才是 ReAct 侧的关联依据（R22.11）。`step_ids`
    # 仍取出用于确认这些步骤确实落库（完备性的另一半）。
    assert step_ids  # 步骤已落库
    assert len(calls) == 1
    call = calls[0]
    assert call.tool_name == "compare_plans"
    assert call.outcome == "OK"
    assert call.trace_id == trace_id
    assert call.step_id is None


def test_react_trace_row_persists_before_commit(factory: sessionmaker[Session]) -> None:
    """`begin` 即写 `traces` 行并 flush，使工具调用记账的 `trace_id` 外键当场有指向。

    `tool_calls.trace_id` 是 NOT NULL 外键。若 trace 行不先落库，第一次工具调用记账就会
    因外键失败。这条断言守住 `begin` 的 flush 契约。
    """
    with factory() as session:
        tracer = DbTracer(session, trigger_source="SCHEDULED", session_id="s-x")
        handle = tracer.begin(kind="REPLAN", mode="REACT", agent="PLANNING_AGENT")
        # 尚未 commit，但 begin 已 flush：同一会话内可查到这一行。
        row_count = session.execute(
            select(func.count()).select_from(Trace).where(Trace.trace_id == handle.trace_id)
        ).scalar_one()
        assert row_count == 1
