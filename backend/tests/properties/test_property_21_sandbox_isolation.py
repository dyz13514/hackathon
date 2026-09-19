# Feature: production-planning-agent, Property 21: 对于任意场景变更序列（含刻意在沙箱执行
# 路径中发起写操作的用例），执行前后当前 ACTIVE 计划的 plan_id、内容哈希与 input_snapshot_version
# 三者均不变，生产数据表的行内容不变；任何写尝试都以 SandboxWriteBlocked 终止该次模拟并在
# Audit_Log 留下 SANDBOX_WRITE_BLOCKED 记录。
"""Property 21：沙箱隔离（任务 8.2，R16.4 / R16.5 / R16.6 / R17.4；design.md
Correctness Properties「Property 21」、ADR-009）。

*For any* 场景变更序列（**包含刻意在沙箱执行路径中发起写操作的用例**），本属性守：

1. **ACTIVE 计划三项不变**：执行前后当前 `ACTIVE` 计划的 `plan_id`、内容哈希、
   `input_snapshot_version` 三者均不变（R16.6）。
2. **生产数据行不变**：`orders` / `machines` / `materials` / `workers` 等生产表的行内容不变。
3. **写尝试被终止并留痕**：沙箱执行路径中的任何 DML 写尝试都以 `SandboxWriteBlocked` 终止
   该次模拟，并在 `Audit_Log` 留下一条 `SANDBOX_WRITE_BLOCKED`（R16.5、EVAL-204）。

## 这条属性守的是什么：模拟不能污染生产数据

What-if / 反事实 / 瓶颈 / 报价都跑在 `Scenario_Sandbox` 里（design.md §3.7）。一个沙箱最危险
的失败模式是**推演过程改动了真实生产数据或当前 ACTIVE 计划**——那样「问一句 what-if」就可能
悄悄改了今天的计划。本属性把「无论场景变更怎么排、哪怕其中夹着真实写语句，生产侧一律不变」
变成对任意序列都成立的机器可判定断言，因此**非可选**（tasks.md 8.2）。

## 抽象「沙箱操作序列」如何施加到真实系统

`run_sandbox` 统一入口属任务 8.3（尚未落地），因此本属性不经它，而是**直接**在
`sandbox_guard(...)` 语境内施加一串抽象 `SandboxOp`，每一步翻译成对真实组件的一次操作：

- `LOAD`：`load_sandbox_snapshot(session)`——第 1 层的冻结只读快照加载。
- `MUTATE`：`snapshot.model_copy(deep=True, update=...)` 得到变体（改交期 / 机器不可用 /
  物料量 / 工人不可用 / 订单优先级五类之一，逐字对齐 R16.2 的 5 类场景变更）——这是「场景
  变更」在纯内核层的表现，**不触库**，因此天然不该改任何生产行。
- `SCORE`：对变体跑一次真实 `Scheduling_Core.generate_schedule`（纯内核，无会话）——证明
  沙箱推演的主计算确实发生且不产生写。
- `WRITE`：**刻意的写尝试**——通过真实 `Session` 对生产表发起一条真实 DML（5 类 DML 动词 ×
  4 张生产表随机组合）。第 2 层引擎级监听器应在语句到达游标前抛 `SandboxWriteBlocked`，
  `sandbox_guard` 捕获后写 `SANDBOX_WRITE_BLOCKED` 审计并重抛，终止本次序列（R16.5）。

序列里只要出现过 `WRITE`，整段就应以 `SandboxWriteBlocked` 终止；未出现 `WRITE` 的纯读/推演
序列应正常跑完。无论哪条路径，收尾都断言 ACTIVE 计划三项 + 生产行指纹与序列执行前完全一致。

## 复用 `_Harness`（模块级一次性建库）与 max_examples=100

与 Property 15 同构：应用 + seed 只建一次，每个 example 只重铺一个 `ACTIVE` 计划再施加序列，
审批/快照/内核/沙箱守卫全部走真实实现，不 mock。`max_examples=100`、`deadline=None`、抑制
「过慢」与「function-scoped fixture」两条健康检查（design.md Testing Strategy §2 第 3 条）。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.scheduler import generate_schedule
from app.db import models as orm
from app.db.audit import set_audit_engine
from app.db.models import Base
from app.db.sandbox_guard import SANDBOX_ACTIVE, SandboxWriteBlocked, sandbox_guard
from app.db.session import create_db_engine
from app.seed.dataset import DEMO_ANCHOR
from app.seed.loader import load_demo_data
from app.services.approval import ApprovalService, ApprovalStatus
from app.services.events import EventBus
from app.services.snapshot_loader import load_sandbox_snapshot
from app.settings import Settings

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"

_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


# --------------------------------------------------------------------------
# 抽象沙箱操作序列（含刻意写尝试）
# --------------------------------------------------------------------------

#: 5 类 DML/DDL 动词——沙箱内任何一类写都应被拦（design.md §3.7 的正则覆盖这些）。
_DML_VERBS = ("UPDATE", "DELETE", "INSERT", "REPLACE", "CREATE")
#: 4 张生产表作为写尝试目标。
_PROD_TABLES = ("orders", "machines", "materials", "workers")
#: 5 类场景变更（R16.2），在纯内核层以 model_copy 表现——不触库。
_MUTATION_KINDS = ("DUE_DATE", "MACHINE_DOWN", "MATERIAL_QTY", "WORKER_OUT", "PRIORITY")


@dataclass(frozen=True, slots=True)
class SandboxOp:
    """一步抽象沙箱操作。`kind` 决定翻译成 LOAD / MUTATE / SCORE / WRITE 中的哪一种。"""

    kind: str  # "LOAD" | "MUTATE" | "SCORE" | "WRITE"
    mutation: str  # MUTATE 用
    verb: str  # WRITE 用
    table: str  # WRITE 用


@st.composite
def _sandbox_ops(draw: st.DrawFn) -> tuple[SandboxOp, ...]:
    """任意沙箱操作序列（长度 1–6），其中以一定概率夹入刻意的 WRITE 尝试。"""
    n = draw(st.integers(min_value=1, max_value=6))
    ops: list[SandboxOp] = []
    for _ in range(n):
        kind = draw(st.sampled_from(("LOAD", "MUTATE", "SCORE", "WRITE")))
        ops.append(
            SandboxOp(
                kind=kind,
                mutation=draw(st.sampled_from(_MUTATION_KINDS)),
                verb=draw(st.sampled_from(_DML_VERBS)),
                table=draw(st.sampled_from(_PROD_TABLES)),
            )
        )
    return tuple(ops)


# --------------------------------------------------------------------------
# 模块级 Harness（与 Property 15 同构）
# --------------------------------------------------------------------------


class _Harness:
    def __init__(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        db_file = Path(self._tmp.name) / "property21.db"
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{db_file.as_posix()}",
                "SESSION_SHARED_PASSWORD": "test-shared-password",
                "SESSION_SECRET_KEY": "test-secret-key-that-is-long-enough-32",
                "LLM_MODE": "STUB",
                "APP_ENV": "TEST",
            }
        )
        from app.main import create_app

        self.settings = Settings()  # type: ignore[call-arg]
        self.app = create_app(self.settings)
        Base.metadata.create_all(self.app.state.engine)
        self.factory: sessionmaker[Session] = self.app.state.session_factory
        with self.factory() as session:
            load_demo_data(session)
            session.commit()
        from fastapi.testclient import TestClient

        self.client = TestClient(self.app)
        self.client.post(
            LOGIN,
            json={"password": self.settings.session_shared_password.get_secret_value()},
        )
        self.audit_engine = create_db_engine(self.settings)

    def reset_plans(self) -> None:
        with self.factory() as session:
            session.execute(delete(orm.PlanApproval))
            session.execute(delete(orm.PlannerDecision))
            session.execute(delete(orm.ScheduledJob))
            session.execute(delete(orm.ObjectiveBreakdown))
            session.execute(delete(orm.BaselineComparison))
            session.execute(delete(orm.ImpactAssessment))
            session.execute(
                orm.ProductionPlan.__table__.update().values(
                    supersedes_plan_id=None, superseded_by_plan_id=None
                )
            )
            session.execute(delete(orm.ProductionPlan))
            session.commit()

    def generate_and_activate_plan(self) -> str:
        response = self.client.post(GENERATE, json={})
        assert response.status_code == 200, response.text
        plan_id = str(response.json()["plan_id"])
        with self.factory() as db:
            row = db.get(orm.ProductionPlan, plan_id)
            assert row is not None
            expected_version = row.version
        db2 = self.factory()
        try:
            service = ApprovalService(session=db2, now=DEMO_ANCHOR, events=EventBus())
            result = service.approve(
                plan_id, actor="PLANNER", expected_version=expected_version
            )
            assert result.status is ApprovalStatus.OK, f"激活失败：{result.status}"
        finally:
            db2.close()
        return plan_id

    def sandbox_block_audit_count(self) -> int:
        with self.audit_engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM audit_log "
                        "WHERE event_category = 'SANDBOX_WRITE_BLOCKED'"
                    )
                ).scalar_one()
            )

    def close(self) -> None:
        set_audit_engine(None)
        self.client.close()
        self.audit_engine.dispose()
        self.app.state.engine.dispose()
        self._tmp.cleanup()


_harness: _Harness | None = None


@pytest.fixture(scope="module")
def harness() -> Iterator[_Harness]:
    global _harness
    _harness = _Harness()
    try:
        yield _harness
    finally:
        _harness.close()
        _harness = None


# --------------------------------------------------------------------------
# 指纹：ACTIVE 计划三项 + 生产表行
# --------------------------------------------------------------------------


def _active_plan_fingerprint(harness: _Harness, plan_id: str) -> tuple[str, int, str]:
    with harness.factory() as db:
        plan = db.get(orm.ProductionPlan, plan_id)
        assert plan is not None
        jobs = db.execute(
            select(orm.ScheduledJob)
            .where(orm.ScheduledJob.plan_id == plan_id)
            .order_by(orm.ScheduledJob.job_id)
        ).scalars().all()
        content = repr(
            [
                plan.status,
                plan.plan_version,
                plan.feasibility,
                [
                    (j.job_id, j.machine_id, j.worker_id, j.start_time, j.end_time)
                    for j in jobs
                ],
            ]
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return plan.plan_id, plan.input_snapshot_version, digest


def _production_rows_fingerprint(harness: _Harness) -> str:
    with harness.factory() as db:
        orders = db.execute(
            select(orm.Order.order_id, orm.Order.priority, orm.Order.due_date).order_by(
                orm.Order.order_id
            )
        ).all()
        machines = db.execute(
            select(orm.Machine.machine_id, orm.Machine.status).order_by(
                orm.Machine.machine_id
            )
        ).all()
        materials = db.execute(
            select(orm.Material.material_id, orm.Material.quantity_available).order_by(
                orm.Material.material_id
            )
        ).all()
        workers = db.execute(
            select(orm.Worker.worker_id, orm.Worker.name).order_by(orm.Worker.worker_id)
        ).all()
    return hashlib.sha256(
        repr((orders, machines, materials, workers)).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------
# 施加一步操作
# --------------------------------------------------------------------------


def _apply_op(harness: _Harness, op: SandboxOp) -> None:
    """在**已处于 sandbox_guard 语境**下施加一步操作。WRITE 应抛 SandboxWriteBlocked。"""
    if op.kind == "LOAD":
        with harness.factory() as db:
            load_sandbox_snapshot(db, now=DEMO_ANCHOR)
        return
    if op.kind == "MUTATE":
        with harness.factory() as db:
            snap = load_sandbox_snapshot(db, now=DEMO_ANCHOR)
        # 5 类场景变更之一，以 model_copy(deep=True) 得到冻结变体——纯内核，不触库。
        _mutated_copy(snap, op.mutation)
        return
    if op.kind == "SCORE":
        with harness.factory() as db:
            snap = load_sandbox_snapshot(db, now=DEMO_ANCHOR)
        generate_schedule(snap)  # 纯内核推演，无会话、无写
        return
    # WRITE：刻意的真实 DML 写尝试 —— 第 2 层监听器应拦下。
    with harness.factory() as db:
        db.execute(_write_statement(op.verb, op.table))
        db.flush()


def _mutated_copy(snapshot: object, mutation: str) -> object:
    """把 5 类场景变更之一施加为 `model_copy(deep=True, update=...)`（纯内核，不触库）。

    只要求「变体是一份新的冻结副本、原快照不受影响」——它不涉及任何 DB 写。变更的具体数值
    取每类的第一个可改对象，属性关心的是隔离而非变更语义（那属 8.3）。
    """
    snap = snapshot  # type: ignore[assignment]
    if mutation == "DUE_DATE" and snap.orders:  # type: ignore[attr-defined]
        first = snap.orders[0]  # type: ignore[attr-defined]
        new_order = first.model_copy(update={"due_date": first.due_date + timedelta(days=1)})
        return snap.model_copy(  # type: ignore[attr-defined]
            deep=True, update={"orders": (new_order, *snap.orders[1:])}  # type: ignore[attr-defined]
        )
    if mutation == "PRIORITY" and snap.orders:  # type: ignore[attr-defined]
        first = snap.orders[0]  # type: ignore[attr-defined]
        new_order = first.model_copy(update={"priority": "URGENT"})
        return snap.model_copy(  # type: ignore[attr-defined]
            deep=True, update={"orders": (new_order, *snap.orders[1:])}  # type: ignore[attr-defined]
        )
    # 其余三类（机器不可用 / 物料量 / 工人不可用）语义在 8.3 补齐；此处以一次无字段变更的
    # deep copy 表现「场景变体」——足以验证隔离（copy 是新对象，不触库、不改原快照）。
    return snap.model_copy(deep=True)  # type: ignore[attr-defined]


def _write_statement(verb: str, table: str) -> object:
    """构造一条真实 DML 语句作为写尝试。用 ORM update 或原文 text，二者都应被监听器拦下。"""
    if verb == "UPDATE" and table == "orders":
        return update(orm.Order).values(priority="URGENT")
    if verb == "DELETE":
        return text(f"DELETE FROM {table}")
    if verb == "INSERT":
        # 故意不合法的 INSERT——监听器在语句到达游标前就拦下，不会真的执行。
        return text(f"INSERT INTO {table} (x) VALUES (1)")
    if verb == "REPLACE":
        return text(f"REPLACE INTO {table} (x) VALUES (1)")
    if verb == "CREATE":
        return text(f"CREATE TABLE _sbx_{table} (x INTEGER)")
    # 默认 UPDATE。
    return text(f"UPDATE {table} SET x = 1")


# --------------------------------------------------------------------------
# Property 21
# --------------------------------------------------------------------------


@_SETTINGS
@given(ops=_sandbox_ops())
def test_sandbox_isolation_holds_for_any_operation_sequence(
    harness: _Harness, ops: tuple[SandboxOp, ...]
) -> None:
    """**Validates: Requirements 16.4, 16.5, 16.6, 17.4**

    对任意沙箱操作序列（含刻意写尝试）：ACTIVE 计划三项 + 生产表行执行前后不变；任一写尝试
    以 `SandboxWriteBlocked` 终止并留 `SANDBOX_WRITE_BLOCKED` 审计。
    """
    harness.reset_plans()
    plan_id = harness.generate_and_activate_plan()

    before_plan = _active_plan_fingerprint(harness, plan_id)
    before_rows = _production_rows_fingerprint(harness)
    audit_before = harness.sandbox_block_audit_count()

    expects_write = any(op.kind == "WRITE" for op in ops)
    scenario_id = "SCN-prop21"

    if expects_write:
        # 序列中含写尝试：整段应以 SandboxWriteBlocked 终止（在**第一个** WRITE 处）。
        with pytest.raises(SandboxWriteBlocked), sandbox_guard(scenario_id):
            for op in ops:
                _apply_op(harness, op)
        # 恰好多一条 SANDBOX_WRITE_BLOCKED 审计（第一个写尝试触发一次）。
        assert harness.sandbox_block_audit_count() == audit_before + 1
    else:
        # 纯读 / 推演序列：正常跑完，不抛、不新增阻断审计。
        with sandbox_guard(scenario_id):
            for op in ops:
                _apply_op(harness, op)
        assert harness.sandbox_block_audit_count() == audit_before

    # 沙箱语境已复位，无泄漏。
    assert SANDBOX_ACTIVE.get() is False

    # 核心不变量：ACTIVE 计划三项 + 生产表行与执行前逐字节一致（R16.6）。
    assert _active_plan_fingerprint(harness, plan_id) == before_plan
    assert _production_rows_fingerprint(harness) == before_rows
