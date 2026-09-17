# Feature: production-planning-agent, Property 15: 对于任意 API 请求序列与任意 Agent
# 工具调用序列，使某个计划的 status 变为 ACTIVE 的操作只可能是 Approval_Service.approve()
# （或 P1 的 activate_internal 回滚路径）；其余一切尝试均返回 403 FORBIDDEN 或
# TOOL_NOT_PERMITTED 并写审计；且任一 production_date 上处于 ACTIVE 的计划数恒 ≤ 1。
"""Property 15：`ACTIVE` 状态的唯一到达路径（任务 3.4，R11.1 / R11.7 / R11.8 / R16.9 /
R22.9 / R23.4；design.md Correctness Properties「Property 15」）。

*For any* API 请求与任意 Agent 工具调用序列，本属性同时守三条不变量：

1. **唯一到达路径**：使某个计划 `status` 变为 `ACTIVE` 的操作**只可能**是
   `Approval_Service.approve()`（P1 另有 `activate_internal` 回滚路径，P0 未落地）。序列里
   刻意注入的三类攻击——直接 `PATCH status=ACTIVE`、越权工具调用、并发 `approve`——都不能
   把一个计划推进到 `ACTIVE`。
2. **越界尝试被拒且留痕**：直接 `PATCH status` 返回 `403 FORBIDDEN`
   （`PLAN_STATUS_WRITE_FORBIDDEN`）并写审计（R11.8 / R22.9 / R23.4，EVAL-207）；任何试图
   经工具到达 `ACTIVE` 的调用返回 `TOOL_NOT_PERMITTED`——而这在结构上根本不可能，因为
   计划状态机的迁移许可表把 `→ ACTIVE` 的唯一授权组件钉死为 `Approval_Service`，没有任何
   工具组件被登记（R22.9、design.md §8）。
3. **每日至多一个 `ACTIVE`**：任一 `production_date` 上 `ACTIVE` 计划数恒 ≤ 1
   （`ux_active_per_day` 部分唯一索引 + `approve()` 内 `supersede_previous_active` 共同保证，
   R11.3）。

## 这条属性守的是什么：人在环审批闸门

design.md 把「人在环审批」列为核心护栏（R11.1：`Approval_Service` 是**唯一**能置 `ACTIVE`
的组件）。一个 Agent 系统最危险的失败模式是**绕过审批自动上线一个计划**——无论是经 REST
直接写 `status`、还是让某个工具悄悄把计划激活。本属性把「无论请求怎么排，ACTIVE 只能由
approve() 达成」变成一条对任意序列都成立的机器可判定断言，因此**非可选**（tasks.md 3.4）。

## 抽象请求序列如何被翻译成对真实系统的操作

输入是 `tests/generators.py` 的 `approval_request_sequences()`，它产出**抽象** `ApprovalRequest`
序列（`kind × caller × plan_id × tool_name × …`）。本测试把每一类 `kind` 翻译成对**真实**
系统组件的一次操作，断言其结果落在允许集合内：

- `APPROVE` → `ApprovalService.approve()`。这是唯一被允许推进到 `ACTIVE` 的路径。成功
  （`OK`）意味着这个计划**经 approve 合法激活**；其余结果（陈旧 / 重校验失败 / 并发 /
  非法迁移）都不激活。无论如何，激活只发生在这条路径上——本测试据此把「哪些计划变 ACTIVE」
  与「哪条操作导致的」对齐。
- `PATCH_STATUS_ACTIVE` → 真实 `PATCH /api/plans/{id}`，请求体含 `status=ACTIVE`。断言
  `403 FORBIDDEN` + `PLAN_STATUS_WRITE_FORBIDDEN` 错误码 + 写下一条同名审计
  （EVAL-207，R11.8）。
- `TOOL_CALL` → P0 的 `Tool_Registry`（任务 5.1）尚未落地，因此不存在可调用的真实工具入口。
  但「工具能否到达 ACTIVE」是一个**结构性**问题，不依赖注册表是否已接线：计划状态机的迁移
  许可表（`app.services.plan_state_machine`）是 `→ ACTIVE` 的唯一权威判据，本测试断言该表里
  通向 `ACTIVE` 的迁移**唯一授权组件恒为 `Approval_Service`**，没有任何工具/Agent 组件被
  登记（R22.9、design.md §8）。这覆盖了「越权工具调用不可能激活计划」这一断言的结构根因。
- `REJECT` / `MODIFY` / `GENERATE_PLAN`：这些动作按定义不通向 `ACTIVE`
  （`REJECT → REJECTED`、`MODIFY → 新 PENDING_APPROVAL`、`GENERATE_PLAN → PENDING_APPROVAL`），
  本测试把它们视作序列中的噪声——它们的存在制造交错，但不应改变「唯一到达路径」的结论。

## 为什么每个 example 都重铺一个干净的 PENDING_APPROVAL 计划

`approve()` 的合法性依赖真实的输入快照、`scheduled_jobs`、Trace 与版本号（它要跑重校验、
要做乐观并发 UPDATE）。因此本测试不构造裸计划行，而是经 `POST /plans/generate` 铺出一个
与生产同口径的 `PENDING_APPROVAL` 计划，再把抽象序列施加其上。应用与 seed 只建一次
（`_app` 模块级缓存），每个 example 只重置计划相关的状态，避免把毫秒级属性测试拖成每次都
建库拆库的集成测试——但审批、快照、内核全部走**真实**实现，不 mock。

## `max_examples=100`

与其余 `domain_snapshots` 消费者同口径（design.md Testing Strategy §2 第 3 条：仅属性 1
提高到 300）。每个 example 要跑一次真实流水线生成 + 一串真实 HTTP / 服务调用，成本偏高，
故 `deadline=None` 且抑制「过慢」健康检查。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as orm
from app.db.audit import set_audit_engine
from app.db.models import Base
from app.db.session import create_db_engine
from app.main import create_app
from app.seed.dataset import DEMO_ANCHOR
from app.seed.loader import load_demo_data
from app.services.approval import ACTIVE_STATUS, ApprovalService, ApprovalStatus
from app.services.events import EventBus
from app.services.plan_state_machine import (
    ALLOWED_TRANSITIONS,
    COMPONENT_APPROVAL_SERVICE,
    PlanStatus,
    is_allowed_transition,
    transition_authority,
)
from app.settings import Settings
from tests.generators import ApprovalRequest, approval_request_sequences

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"

# 属性 15 与其余 5 条 `domain_snapshots` 消费者一致，用 `max_examples=100`。每个 example
# 跑真实流水线 + 真实审批 + 真实 HTTP，慢不是错误：放宽 deadline 并抑制「过慢」健康检查。
# `function_scoped_fixture` 也被抑制——`_app` 是本测试自建的模块级缓存，不是每次重建的
# pytest fixture，因此那条健康检查在这里是误报。
_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


# --------------------------------------------------------------------------
# 应用 + seed：只建一次（模块级缓存），每个 example 只重置计划状态
# --------------------------------------------------------------------------


class _Harness:
    """一次性建好的应用 + 已登录客户端 + 会话工厂，供所有 example 复用。

    每个 example 之间用 `reset_plans()` 把计划相关的表清空，再重新铺一个 `PENDING_APPROVAL`
    计划——审批、快照、内核全部走真实实现，只有「建库 + 铺 seed」这一次性开销被缓存。
    """

    def __init__(self) -> None:
        # ignore_cleanup_errors：Windows 下 SQLite 文件句柄释放有滞后，即便先 dispose 引擎，
        # 偶发的 WinError 32（文件占用）也不该让整个属性测试红掉——临时目录终会被系统回收。
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        db_file = Path(self._tmp.name) / "property15.db"
        # 直接构造合法配置：属性测试不经 conftest 的 valid_env fixture（`@given` 与
        # function-scoped fixture 组合会触发健康检查），因此这里显式给一组最小合法值，
        # 与 `conftest.VALID_ENV` 同口径（密钥长度满足 `MIN_SECRET_KEY_LENGTH`）。
        import os

        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{db_file.as_posix()}",
                "SESSION_SHARED_PASSWORD": "test-shared-password",
                "SESSION_SECRET_KEY": "test-secret-key-that-is-long-enough-32",
                "LLM_MODE": "STUB",
                "APP_ENV": "TEST",
            }
        )
        self.settings = Settings()  # type: ignore[call-arg]  # 值由环境变量提供
        self.app = create_app(self.settings)
        Base.metadata.create_all(self.app.state.engine)
        self.factory: sessionmaker[Session] = self.app.state.session_factory
        with self.factory() as session:
            load_demo_data(session)
            session.commit()
        self.client = TestClient(self.app)
        self.client.post(
            LOGIN,
            json={"password": self.settings.session_shared_password.get_secret_value()},
        )
        # 审计与业务同库（`create_app` 已 `set_audit_engine` 指向本库），审计断言直接读它。
        self.audit_engine = create_db_engine(self.settings)

    def reset_plans(self) -> None:
        """清空全部计划及其明细/审批/决策行，回到「有 seed、无计划」的干净起点。

        清的是**计划域**的表，不动 seed 的订单/机器/工人/物料——那些是排产输入，重铺计划
        时还要用。删除顺序服从外键：先明细（作业/审批/决策/目标拆解/对比），后计划头。
        """
        with self.factory() as session:
            session.execute(delete(orm.PlanApproval))
            session.execute(delete(orm.PlannerDecision))
            session.execute(delete(orm.ScheduledJob))
            session.execute(delete(orm.ObjectiveBreakdown))
            session.execute(delete(orm.BaselineComparison))
            # 计划头之间有 supersedes/superseded 自引用外键：先解开再删，避免 FK 顺序问题。
            session.execute(
                orm.ProductionPlan.__table__.update().values(
                    supersedes_plan_id=None, superseded_by_plan_id=None
                )
            )
            session.execute(delete(orm.ProductionPlan))
            session.commit()

    def generate_pending_plan(self) -> str:
        """经真实流水线铺出一个 `PENDING_APPROVAL` 计划，返回其 `plan_id`。"""
        response = self.client.post(GENERATE, json={})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "PENDING_APPROVAL"
        return str(body["plan_id"])

    def new_service(self) -> tuple[ApprovalService, Session]:
        """在本应用的会话工厂上开一个新 `ApprovalService`；`now` 取演示锚点（同生成端点口径）。"""
        db = self.factory()
        return ApprovalService(session=db, now=DEMO_ANCHOR, events=EventBus()), db

    def close(self) -> None:
        # 先释放全部 SQLite 连接池，再清临时目录：否则 Windows 会因文件仍被占用而
        # 抛 WinError 32。audit_engine 与业务 engine 是两个独立引擎，都要 dispose。
        set_audit_engine(None)
        self.client.close()
        self.audit_engine.dispose()
        self.app.state.engine.dispose()
        self._tmp.cleanup()


_harness: _Harness | None = None


@pytest.fixture(scope="module")
def harness() -> Iterator[_Harness]:
    """模块级一次性建好的应用；`@given` 的每个 example 复用它，只重置计划状态。"""
    global _harness
    _harness = _Harness()
    try:
        yield _harness
    finally:
        _harness.close()
        _harness = None


# --------------------------------------------------------------------------
# 结构性断言：状态机里 `→ ACTIVE` 的唯一授权组件恒为 Approval_Service
# --------------------------------------------------------------------------


def _assert_only_approval_service_reaches_active() -> None:
    """断言迁移许可表里通向 `ACTIVE` 的迁移**唯一且授权组件为 `Approval_Service`**。

    这是「越权工具调用不可能激活计划」的**结构根因**（R22.9、design.md §8）：无论 Agent 用
    哪个 caller 调哪个工具，工具最终要激活一个计划都得经过一次 `→ ACTIVE` 的状态迁移，而这
    张表把该迁移的唯一授权组件钉死为 `Approval_Service`，没有登记任何工具/Agent 组件。因此
    任何试图经工具到达 `ACTIVE` 的调用在结构上就不可能成功——对应的运行期表现即
    `TOOL_NOT_PERMITTED`（P0 `Tool_Registry` 尚未接线，此处断言的是它未来必须遵守的表）。
    """
    into_active = {
        (src, dst): comp
        for (src, dst), comp in ALLOWED_TRANSITIONS.items()
        if dst is PlanStatus.ACTIVE
    }
    # 表内通向 ACTIVE 的迁移恰有一条：PENDING_APPROVAL → ACTIVE。
    assert into_active == {
        (PlanStatus.PENDING_APPROVAL, PlanStatus.ACTIVE): COMPONENT_APPROVAL_SERVICE
    }, f"通向 ACTIVE 的迁移不唯一或授权组件不是 Approval_Service：{into_active!r}"
    # 每一条通向 ACTIVE 的合法迁移，其授权组件都必须是 Approval_Service。
    for (src, dst), comp in into_active.items():
        assert comp == COMPONENT_APPROVAL_SERVICE, (
            f"迁移 {src} → {dst} 的授权组件为 {comp!r}，不是 Approval_Service"
        )
        assert transition_authority(src, dst) == COMPONENT_APPROVAL_SERVICE

    # 任何**其他**源状态直达 ACTIVE 都是表外迁移 → 非法（没有组件被允许）。
    for src in PlanStatus:
        if src is PlanStatus.PENDING_APPROVAL:
            continue
        assert not is_allowed_transition(src, PlanStatus.ACTIVE), (
            f"{src} → ACTIVE 不应是合法迁移（唯一合法源是 PENDING_APPROVAL）"
        )


def _active_count_by_date(harness: _Harness) -> dict:
    """按 `production_date` 统计 `ACTIVE` 计划数（用于断言每日 ≤ 1）。"""
    with harness.factory() as session:
        rows = session.execute(
            select(orm.ProductionPlan.production_date, func.count())
            .where(orm.ProductionPlan.status == ACTIVE_STATUS)
            .group_by(orm.ProductionPlan.production_date)
        ).all()
    return {row[0]: int(row[1]) for row in rows}


def _audit_count(harness: _Harness, *, subject_id: str, event_type: str) -> int:
    """某个计划上某类审计事件的条数。"""
    from sqlalchemy import text

    with harness.audit_engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE subject_id = :sid AND event_type = :et"
                ),
                {"sid": subject_id, "et": event_type},
            ).scalar_one()
        )


# --------------------------------------------------------------------------
# 每一类抽象请求的翻译与逐请求断言
# --------------------------------------------------------------------------


def _apply_request(harness: _Harness, request: ApprovalRequest, real_plan_id: str) -> bool:
    """把一条抽象 `ApprovalRequest` 翻译成对真实系统的一次操作，返回「本次是否合法激活」。

    只有 `APPROVE` 且返回 `OK` 时返回 `True`（表示这个计划经 `approve()` 合法变为 `ACTIVE`）；
    其余一切路径返回 `False`，并就地断言其越界结果（403 / 结构性不可达）落在允许集合内。

    抽象请求的 `plan_id` 来自生成器的小池子（`PLAN-000`…），与库里真实的 plan_id 无关；
    本测试把序列里每一条**指向计划的**请求都作用到当前那个真实的 `PENDING_APPROVAL` 计划
    （`real_plan_id`）上，这样并发/重复 approve、重复 PATCH 都真实地打在同一个计划上。
    """
    if request.kind == "APPROVE":
        service, db = harness.new_service()
        try:
            plan = db.get(orm.ProductionPlan, real_plan_id)
            expected_version = plan.version if plan is not None else 0
            result = service.approve(
                real_plan_id, actor=request.caller, expected_version=expected_version
            )
        finally:
            db.close()
        # 激活只可能经这条路径；OK 即合法激活，其余结果一律不激活。
        return result.status is ApprovalStatus.OK

    if request.kind == "PATCH_STATUS_ACTIVE":
        # 攻击①：绕过审批直接 PATCH status=ACTIVE → 必 403 + 写审计（EVAL-207，R11.8）。
        before = _audit_count(
            harness, subject_id=real_plan_id, event_type="PLAN_STATUS_WRITE_FORBIDDEN"
        )
        response = harness.client.patch(
            f"/api/plans/{real_plan_id}", json={"status": "ACTIVE"}
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "PLAN_STATUS_WRITE_FORBIDDEN"
        after = _audit_count(
            harness, subject_id=real_plan_id, event_type="PLAN_STATUS_WRITE_FORBIDDEN"
        )
        assert after == before + 1, "绕过尝试必须留痕一条 PLAN_STATUS_WRITE_FORBIDDEN 审计"
        return False

    if request.kind == "TOOL_CALL":
        # 攻击②：越权工具调用。P0 `Tool_Registry` 尚未接线，因此不存在可调用的真实工具入口；
        # 「工具能否到达 ACTIVE」是结构性问题——由 _assert_only_approval_service_reaches_active
        # 在每个 example 起点断言（状态机表里没有任何工具组件被授权到达 ACTIVE）。此处不产生
        # 任何激活。
        return False

    # REJECT / MODIFY / GENERATE_PLAN：按定义不通向 ACTIVE，作为序列噪声，不产生激活。
    return False


# --------------------------------------------------------------------------
# Property 15
# --------------------------------------------------------------------------


@_SETTINGS
@given(sequence=approval_request_sequences())
def test_active_is_reachable_only_via_approval_service(
    harness: _Harness, sequence: tuple[ApprovalRequest, ...]
) -> None:
    """**Validates: Requirements 11.1, 11.7, 11.8, 16.9, 22.9, 23.4**

    对任意 API 请求 + Agent 工具调用序列：

    1. 使某个计划 `status` 变为 `ACTIVE` 的操作只可能是 `Approval_Service.approve()`——
       序列跑完后，库里每一个 `ACTIVE` 计划都能对应到本序列里一次返回 `OK` 的 `approve()`；
    2. 直接 `PATCH status=ACTIVE` 返回 `403 FORBIDDEN` + `PLAN_STATUS_WRITE_FORBIDDEN` 并写
       审计；越权工具调用在结构上不可能到达 `ACTIVE`（状态机表唯一授权 `Approval_Service`）；
    3. 任一 `production_date` 上 `ACTIVE` 计划数恒 ≤ 1。
    """
    # ① 结构不变量：状态机里通向 ACTIVE 的迁移唯一且只授权 Approval_Service（每个 example
    #    都断言一次——它不依赖 example 数据，但把「工具不可达 ACTIVE」的根因钉在属性里）。
    _assert_only_approval_service_reaches_active()

    # ② 重置到干净起点并铺一个真实的 PENDING_APPROVAL 计划。
    harness.reset_plans()
    real_plan_id = harness.generate_pending_plan()

    # ③ 逐条施加抽象序列，收集「合法激活」发生的次数。
    legal_activations = 0
    for request in sequence:
        if _apply_request(harness, request, real_plan_id):
            legal_activations += 1

    # ④ 唯一到达路径：库里 ACTIVE 的计划要么是被 approve() 激活的那个，要么一个都没有。
    with harness.factory() as session:
        active_ids = list(
            session.execute(
                select(orm.ProductionPlan.plan_id).where(
                    orm.ProductionPlan.status == ACTIVE_STATUS
                )
            ).scalars()
        )
    if legal_activations == 0:
        assert active_ids == [], (
            f"没有任何 approve() 成功，却出现了 ACTIVE 计划：{active_ids}——"
            "存在绕过审批的激活路径"
        )
    else:
        # 至少一次 approve OK：被激活的只可能是那个真实计划（同一序列打在同一计划上，
        # 重复 approve 幂等——第二次因状态已非 PENDING_APPROVAL 得 INVALID_STATE_TRANSITION）。
        assert active_ids == [real_plan_id], (
            f"ACTIVE 计划集合 {active_ids} 与被 approve() 激活的计划 {real_plan_id} 不一致"
        )

    # ⑤ 每日至多一个 ACTIVE（ux_active_per_day + supersede_previous_active，R11.3）。
    for production_date, count in _active_count_by_date(harness).items():
        assert count <= 1, f"生产日 {production_date} 上出现 {count} 个 ACTIVE 计划（应 ≤ 1）"


def test_state_machine_authority_into_active_is_approval_service_only() -> None:
    """结构性断言单列一份（不经 Hypothesis）：`→ ACTIVE` 唯一授权 `Approval_Service`。

    这是属性主体第 ① 步的独立入口——即便属性测试因环境问题被跳过，这条「工具不可达
    ACTIVE」的结构根因仍被直接守护（R22.9、design.md §8）。
    """
    _assert_only_approval_service_reaches_active()
