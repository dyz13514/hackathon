"""`Token_Budget_Manager` 与成本纪律（任务 5.4，R25.1–4、design.md §2.5、成本章节 §2）。

本模块逐条守 design.md §2.5 的不变量：

- **恰好 2 个作用域**（R25.2 封闭清单）：`BUDGETS` 的键正好是 `PLAN_GENERATION` 与
  `REPLANNING`，且运行期不可追加。
- **`None` 作用域是 no-op 句柄**：`open_scope(None)` 返回 `None`；`record(usage, None)`
  仍写逐次台账、每日累计、项目累计，只跳过作用域累计。
- **`gate` 四态**：`ALLOW / DENY_SCOPE / DENY_DAILY / DENY_PROJECT`，判定顺序固定，
  `None` 时跳过 `DENY_SCOPE` 但仍评估每日与项目上限；`DENY_*` 不抛异常。
- **真实运行硬上限**：`enforce_real_run_cap_on_startup` 在 `LIVE` 且达 150 时拒绝启动
  并写 `PROJECT_BUDGET_CEILING` 审计。
- **项目美元上限 90% 自动降级**：`record` 使项目累计过 90% 时写一条 `DEGRADED_MODE_SWITCH`
  且只写一次。

记账走内存 + `traces` 派生，测试因此以小额上限构造出越线场景，而不依赖真实账单量级。
涉及审计写入的用例把审计引擎指向临时库文件（审计走独立连接，`:memory:` 会看不到表）。
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select

from app.db import audit
from app.db.models import AuditLog, Base, Trace
from app.db.session import create_db_engine
from app.llm.adapter import LlmUsage
from app.llm.budget import (
    BUDGETS,
    DEFAULT_DAILY_USD_CEILING,
    PROJECT_REAL_RUN_CAP,
    PROJECT_USD_CEILING,
    BudgetScope,
    GateDecision,
    RealRunCapExceededError,
    TokenBudgetManager,
    enforce_real_run_cap_on_startup,
)
from app.settings import Settings

# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """临时文件库 + 建表 + 指向审计引擎。

    降级审计与真实运行配额审计都走独立连接（`db/audit.py`），因此库必须是真实文件而非
    `:memory:`——后者每个连接是独立库，审计连接会看不到表。
    """
    settings = Settings(  # type: ignore[call-arg]
        database_url=f"sqlite:///{(tmp_path / 'budget.db').as_posix()}",
        session_shared_password="pw",
        session_secret_key="k" * 32,
    )
    engine = create_db_engine(settings)
    Base.metadata.create_all(engine)
    audit.set_audit_engine(engine)
    yield engine
    audit.set_audit_engine(None)
    engine.dispose()


def _usage(*, input_tokens: int = 0, output_tokens: int = 0) -> LlmUsage:
    return LlmUsage(input_tokens=input_tokens, output_tokens=output_tokens)


def _degraded_audit_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.event_category == "DEGRADED_MODE_SWITCH")
            ).scalar_one()
        )


def _make_trace(trace_id: str, mode: str) -> Trace:
    from datetime import datetime

    return Trace(
        trace_id=trace_id,
        kind="GENERATE_PLAN",
        mode=mode,
        agent=None,
        trigger_source="PLANNER",
        session_id="SESS-0001",
        started_at=datetime(2025, 3, 1, 8, 0),  # noqa: DTZ001 - 与全库 naive 口径一致
        estimated_usd=Decimal("0.14"),
    )


# --------------------------------------------------------------------------
# 恰好 2 个作用域（R25.2）
# --------------------------------------------------------------------------


def test_budgets_has_exactly_two_scopes() -> None:
    """`BUDGETS` 恰好 `PLAN_GENERATION` 与 `REPLANNING`，不多不少（R25.2 封闭清单）。"""
    assert set(BUDGETS) == {"PLAN_GENERATION", "REPLANNING"}
    assert set(BUDGETS) == set(BudgetScope.__args__)  # type: ignore[attr-defined]


def test_budget_limits_match_kpi_table() -> None:
    """两个作用域的 token/USD 上限对应 K-10 与 K-16。"""
    assert BUDGETS["PLAN_GENERATION"].max_tokens == 4_000
    assert BUDGETS["PLAN_GENERATION"].max_usd == Decimal("0.02")
    assert BUDGETS["REPLANNING"].max_tokens == 14_000
    assert BUDGETS["REPLANNING"].max_usd == Decimal("0.06")


def test_budgets_is_immutable_at_runtime() -> None:
    """`MappingProxyType` 冻结：运行期不能追加第三个作用域（R25.2）。"""
    with pytest.raises(TypeError):
        BUDGETS["EXTRA_SCOPE"] = BUDGETS["PLAN_GENERATION"]  # type: ignore[index]


def test_cost_discipline_constants() -> None:
    """项目上限单一 USD 35、每日默认 USD 5.00、真实运行硬上限 150。"""
    assert PROJECT_USD_CEILING == Decimal("35")
    assert DEFAULT_DAILY_USD_CEILING == Decimal("5.00")
    assert PROJECT_REAL_RUN_CAP == 150


# --------------------------------------------------------------------------
# None 作用域：no-op 句柄，但记账照常
# --------------------------------------------------------------------------


def test_open_scope_none_returns_noop_handle() -> None:
    """`open_scope(None)` 返回 `None`（no-op 句柄，不建作用域行）。"""
    manager = TokenBudgetManager()
    assert manager.open_scope(None, trace_id="TR-1") is None


def test_open_scope_named_returns_handle_with_budget() -> None:
    """具名作用域返回带对应 `ScopeBudget` 的句柄。"""
    manager = TokenBudgetManager()
    handle = manager.open_scope("REPLANNING", trace_id="TR-1")
    assert handle is not None
    assert handle.scope_name == "REPLANNING"
    assert handle.budget.max_tokens == 14_000
    assert handle.trace_id == "TR-1"


def test_record_with_none_scope_still_accounts(db_engine: Engine) -> None:
    """`record(usage, None)`：逐次台账、每日累计、项目累计**照常**，只跳过作用域累计。"""
    _ = db_engine  # record 可能触发降级审计，需可用审计库
    manager = TokenBudgetManager()
    usage = _usage(input_tokens=1000, output_tokens=200)

    manager.record(usage, scope=None)

    expected_cost = Decimal("3.00") * 1000 / 1_000_000 + Decimal("15.00") * 200 / 1_000_000
    assert len(manager.ledger) == 1  # 逐次台账
    assert manager.daily.total == expected_cost  # 每日累计
    assert manager.project_total.total == expected_cost  # 项目累计


def test_record_default_scope_satisfies_budget_recorder_protocol(db_engine: Engine) -> None:
    """`record(usage)` 单参调用（adapter 的 `BudgetRecorder` 接缝）等价于 `scope=None`。"""
    _ = db_engine
    manager = TokenBudgetManager()
    manager.record(_usage(input_tokens=500, output_tokens=100))  # adapter 只传 usage
    assert len(manager.ledger) == 1
    assert manager.project_total.total > 0


def test_record_with_scope_accumulates_scope_totals(db_engine: Engine) -> None:
    """具名作用域下作用域累计随之推进（用于 K-10/K-16 的作用域级判定）。"""
    _ = db_engine
    manager = TokenBudgetManager()
    handle = manager.open_scope("PLAN_GENERATION", trace_id="TR-1")
    assert handle is not None

    manager.record(_usage(input_tokens=800, output_tokens=200), scope=handle)

    assert handle.input_tokens == 800
    assert handle.output_tokens == 200
    assert handle.total_tokens == 1000
    assert handle.usd > 0


def test_close_scope_accepts_none_and_handle() -> None:
    """`close_scope` 对 `None` 与真实句柄都是安全 no-op（无作用域合计表）。"""
    manager = TokenBudgetManager()
    manager.close_scope(None)  # 不抛
    handle = manager.open_scope("REPLANNING", trace_id="TR-1")
    manager.close_scope(handle)  # 不抛


# --------------------------------------------------------------------------
# gate 四态
# --------------------------------------------------------------------------


def test_gate_allow_when_within_all_limits() -> None:
    """一切在限内 → `ALLOW`。"""
    manager = TokenBudgetManager()
    handle = manager.open_scope("PLAN_GENERATION", trace_id="TR-1")
    assert manager.gate(handle) is GateDecision.ALLOW
    assert manager.gate(None) is GateDecision.ALLOW


def test_gate_deny_scope_when_scope_token_limit_reached(db_engine: Engine) -> None:
    """作用域 token 达上限 → `DENY_SCOPE`（只可能来自 2 个作用域）。"""
    _ = db_engine
    manager = TokenBudgetManager()
    handle = manager.open_scope("PLAN_GENERATION", trace_id="TR-1")  # 上限 4,000 token
    assert handle is not None

    # 一次记满 4,000 token（低 USD，不触每日/项目上限）。
    manager.record(_usage(input_tokens=4000, output_tokens=0), scope=handle)

    assert handle.exceeds_limit()
    assert manager.gate(handle) is GateDecision.DENY_SCOPE


def test_gate_none_scope_skips_deny_scope(db_engine: Engine) -> None:
    """`None` 作用域跳过 `DENY_SCOPE`：即便消耗巨大，只要每日/项目未超也 `ALLOW`。"""
    _ = db_engine
    # 每日与项目上限抬得足够高，确保不会误触发 DENY_DAILY / DENY_PROJECT。
    manager = TokenBudgetManager(
        daily_ceiling=Decimal("1000"), project_ceiling=Decimal("1000")
    )
    manager.record(_usage(input_tokens=100_000, output_tokens=100_000), scope=None)
    assert manager.gate(None) is GateDecision.ALLOW


def test_gate_deny_daily_when_daily_ceiling_reached(db_engine: Engine) -> None:
    """每日上限达标 → `DENY_DAILY`，即使作用域仍在限内。"""
    _ = db_engine
    manager = TokenBudgetManager(
        daily_ceiling=Decimal("0.01"), project_ceiling=Decimal("1000")
    )
    # 约 USD 0.0165，越过每日 0.01，但远低于项目 1000 与作用域 token 上限。
    manager.record(_usage(input_tokens=5000, output_tokens=100), scope=None)
    assert manager.daily.exceeded()
    assert manager.gate(None) is GateDecision.DENY_DAILY


def test_gate_deny_project_when_project_ceiling_reached(db_engine: Engine) -> None:
    """项目上限达标而每日未达 → `DENY_PROJECT`（判定顺序：作用域 → 每日 → 项目）。"""
    _ = db_engine
    manager = TokenBudgetManager(
        daily_ceiling=Decimal("1000"), project_ceiling=Decimal("0.01")
    )
    manager.record(_usage(input_tokens=5000, output_tokens=100), scope=None)
    assert not manager.daily.exceeded()
    assert manager.project_total.exceeded()
    assert manager.gate(None) is GateDecision.DENY_PROJECT


def test_gate_does_not_raise_on_any_deny(db_engine: Engine) -> None:
    """`DENY_*` 是决策不是异常：`gate` 在任何越线状态下都正常返回（R25.3）。"""
    _ = db_engine
    manager = TokenBudgetManager(
        daily_ceiling=Decimal("0.0001"), project_ceiling=Decimal("0.0001")
    )
    manager.record(_usage(input_tokens=5000, output_tokens=500), scope=None)
    decision = manager.gate(None)  # 不抛
    assert decision in {GateDecision.DENY_DAILY, GateDecision.DENY_PROJECT}


# --------------------------------------------------------------------------
# 每日 80% 告警（R25.4）
# --------------------------------------------------------------------------


def test_daily_warning_activates_at_eighty_percent(db_engine: Engine) -> None:
    """每日成本达 80% → 告警激活；未达则不激活。"""
    _ = db_engine
    manager = TokenBudgetManager(
        daily_ceiling=Decimal("0.01"), project_ceiling=Decimal("1000")
    )
    # 约 USD 0.0075（75%）：未到 80%。
    manager.record(_usage(input_tokens=2500, output_tokens=0), scope=None)
    assert not manager.daily_warning_active()

    # 再加约 USD 0.0015 → 累计 ≈ USD 0.009（90%）：越过 80%。
    manager.record(_usage(input_tokens=500, output_tokens=0), scope=None)
    assert manager.daily_warning_active()


# --------------------------------------------------------------------------
# 项目美元上限 90% → 自动降级 + 审计
# --------------------------------------------------------------------------


def test_project_ceiling_ninety_percent_degrades_and_audits(db_engine: Engine) -> None:
    """项目累计过 90% → 切降级标记并写一条 `DEGRADED_MODE_SWITCH`（design.md 成本章节 ③）。"""
    manager = TokenBudgetManager(
        daily_ceiling=Decimal("1000"), project_ceiling=Decimal("0.02")
    )

    # 先记到 50%（USD 0.01）：不降级、不写审计。
    manager.record(_usage(input_tokens=3333, output_tokens=0), scope=None)  # ≈ USD 0.01
    assert not manager.degraded
    assert _degraded_audit_count(db_engine) == 0

    # 再记过 90%（累计 ≈ USD 0.019）：触发降级 + 一条审计。
    manager.record(_usage(input_tokens=3000, output_tokens=0), scope=None)  # +≈USD 0.009
    assert manager.project_total.fraction() >= Decimal("0.90")
    assert manager.degraded
    assert _degraded_audit_count(db_engine) == 1


def test_project_ceiling_degrade_audit_written_only_once(db_engine: Engine) -> None:
    """越线后继续记账（在途调用收尾）不重复写审计——只写一次。"""
    manager = TokenBudgetManager(
        daily_ceiling=Decimal("1000"), project_ceiling=Decimal("0.02")
    )
    # 一次就冲过 90%。
    manager.record(_usage(input_tokens=7000, output_tokens=0), scope=None)  # ≈ USD 0.021
    assert manager.degraded
    assert _degraded_audit_count(db_engine) == 1

    # 再来两次，审计计数不增。
    manager.record(_usage(input_tokens=1000, output_tokens=0), scope=None)
    manager.record(_usage(input_tokens=1000, output_tokens=0), scope=None)
    assert _degraded_audit_count(db_engine) == 1


def test_project_degrade_audit_payload_has_reason(db_engine: Engine) -> None:
    """降级审计载荷标明成因是项目美元上限，便于排查区分于 Bedrock 抖动降级。"""
    manager = TokenBudgetManager(
        daily_ceiling=Decimal("1000"), project_ceiling=Decimal("0.02")
    )
    manager.record(_usage(input_tokens=7000, output_tokens=0), scope=None)

    with db_engine.connect() as conn:
        row = conn.execute(
            select(AuditLog).where(AuditLog.event_category == "DEGRADED_MODE_SWITCH")
        ).one()
    assert row.payload["reason"] == "PROJECT_USD_CEILING_90PCT"
    assert row.payload["trigger"] == "PROJECT_BUDGET"


# --------------------------------------------------------------------------
# 真实运行硬上限：启动期强制（design.md 成本章节 ②）
# --------------------------------------------------------------------------


def test_real_run_cap_not_enforced_for_replay(db_engine: Engine) -> None:
    """非 LIVE 模式不强制配额，只返回当前计数（回放不烧钱）。"""
    for i in range(3):
        with _session(db_engine) as session:
            session.add(_make_trace(f"TR-REPLAY-{i}", "REPLAY"))
            session.commit()
    # 即便再多 REPLAY 行也不拒绝启动；REPLAY 不计入配额。
    assert enforce_real_run_cap_on_startup(db_engine, llm_mode="REPLAY") == 0


def test_real_run_cap_counts_only_non_replay(db_engine: Engine) -> None:
    """配额口径 `mode != REPLAY`：LIVE 行计数，REPLAY 行不计。"""
    with _session(db_engine) as session:
        session.add(_make_trace("TR-REPLAY", "REPLAY"))
        session.add(_make_trace("TR-LIVE-1", "LIVE"))
        session.add(_make_trace("TR-LIVE-2", "LIVE"))
        session.commit()
    assert enforce_real_run_cap_on_startup(db_engine, llm_mode="LIVE") == 2


def test_real_run_cap_rejects_live_startup_at_limit(db_engine: Engine) -> None:
    """真实运行数达上限 → 拒绝以 LIVE 启动并写 `PROJECT_BUDGET_CEILING` 审计。"""
    # 用一个很小的替身上限，避免真的插 150 行：直接把 cap 打到 1。
    from app.llm import budget as budget_module

    with _session(db_engine) as session:
        session.add(_make_trace("TR-LIVE-1", "LIVE"))
        session.commit()

    original_cap = budget_module.PROJECT_REAL_RUN_CAP
    budget_module.PROJECT_REAL_RUN_CAP = 1
    try:
        with pytest.raises(RealRunCapExceededError):
            enforce_real_run_cap_on_startup(db_engine, llm_mode="LIVE")
    finally:
        budget_module.PROJECT_REAL_RUN_CAP = original_cap

    with db_engine.connect() as conn:
        row = conn.execute(
            select(AuditLog).where(AuditLog.event_category == "PROJECT_BUDGET_CEILING")
        ).one()
    assert row.event_type == "REAL_RUN_CAP_EXCEEDED"
    assert row.payload["attempted_mode"] == "LIVE"


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _session(engine: Engine):  # noqa: ANN202 - 测试内联工具
    from sqlalchemy.orm import Session

    return Session(engine)
