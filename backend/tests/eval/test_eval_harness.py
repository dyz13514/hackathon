"""评估套件骨架自检（任务 12.1）。

这不是任何一条 EVAL-xxx 业务用例——那些是 12.2–12.8 的事。本文件只证明**评估套件的
骨架与一条命令入口是通的**：

1. 用例被自动打上 `eval` 标记，因此 `make eval`（`pytest -m eval`）能选中它，而
   `make test`（`--ignore=tests/eval`）会排除它。
2. 评估套件在 `LLM_MODE=REPLAY` 下运行，且这条骨架路径不产生任何真实 Bedrock 调用
   （它走确定性计划生成流水线，token 汇总为 0）。
3. 共享夹具（`eval_seeded`）能在内存库上载入演示数据并把流水线跑到底。

一旦 12.2 开始填入真实用例，本文件可作为「夹具怎么用」的最小范例保留。
"""

from __future__ import annotations

import os

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ProductionPlan, Trace
from app.orchestrator.pipelines import plan_generation
from tests.eval.conftest import EVAL_NOW, EVAL_PRODUCTION_DATE


def test_eval_suite_runs_in_replay_mode() -> None:
    """评估套件的进程环境是 `REPLAY`（绝不是 `LIVE`）。

    `_replay_mode` 夹具兜底把 `LLM_MODE` 设为 `REPLAY`；这里断言它生效，等于断言
    `make eval` 这条命令入口不会意外触达真实网关。
    """
    assert os.environ.get("LLM_MODE") == "REPLAY"


def test_harness_can_drive_the_deterministic_pipeline(
    eval_seeded: sessionmaker[Session], eval_engine: Engine
) -> None:
    """骨架夹具能把确定性计划生成流水线跑到底，并落一个 PENDING_APPROVAL 计划。

    这条路径根本不 import LLM 适配层（见单元测试 test_plan_generation_pipeline），因此
    在 REPLAY 下运行时零 Bedrock 调用、零 cassette 依赖——它验证的是「骨架能驱动被测系统」
    而不是任何 LLM 行为。
    """
    with eval_seeded() as session:
        result = plan_generation.run_plan_generation(
            session,
            now=EVAL_NOW,
            production_date=EVAL_PRODUCTION_DATE,
            session_id="eval-skeleton",
        )

    with eval_engine.connect() as conn:
        status = conn.execute(
            select(ProductionPlan.status).where(ProductionPlan.plan_id == result.plan_id)
        ).scalar_one()
    assert status == "PENDING_APPROVAL"


def test_skeleton_path_consumes_zero_tokens(
    eval_seeded: sessionmaker[Session], eval_engine: Engine
) -> None:
    """骨架路径的 Trace 是 PIPELINE 且 token 汇总为 0——REPLAY 下零成本的证据。"""
    with eval_seeded() as session:
        result = plan_generation.run_plan_generation(
            session,
            now=EVAL_NOW,
            production_date=EVAL_PRODUCTION_DATE,
            session_id="eval-skeleton",
        )

    with eval_engine.connect() as conn:
        trace = conn.execute(
            select(Trace).where(Trace.trace_id == result.generated_by_trace_id)
        ).one()

    assert trace.mode == "PIPELINE"
    assert trace.total_input_tokens == 0
    assert trace.total_output_tokens == 0
