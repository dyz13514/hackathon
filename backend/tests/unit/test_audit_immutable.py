"""`audit_log` 的 append-only 性质，逐条撞一次（任务 1.4，R24.3–4）。

承接原属性 32（design.md「已裁剪的 32 条属性与其替代覆盖」表：`test_audit_immutable.py`
直接构造 UPDATE / DELETE 语句断言抛错），**非可选**。

三组断言，各守一个不同的失效模式：

1. **改写被拒**——真的发出 UPDATE / DELETE / REPLACE 语句去撞监听器。不检查「监听器
   注册过了吗」：注册过而谓词写错（比如漏了引号形式的表名）是最可能的失效方式，
   而它只有靠真发一条语句才看得出来。
2. **误杀不发生**——目标是别的表、只是在子查询里提到 `audit_log` 的语句必须放行。
   一个过宽的守卫会让某个业务写入在演示当天莫名失败，比守卫太窄更难排查。
3. **旁路成立**——审计写入不参与业务事务回滚，且在沙箱激活期间仍然写得进去。
   没有这条，`SANDBOX_WRITE_BLOCKED` 与 `APPROVAL_REVALIDATION_FAILED` 这类「记录一次
   失败」的条目会跟着失败一起消失。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, event, insert, select, text
from sqlalchemy.orm import Session

from app.db import audit
from app.db.audit import AUDIT_BYPASS, AuditImmutableError
from app.db.audit_events import (
    AGENT_RESERVED_KEY_DROPPED,
    AUDIT_EVENT_CATEGORIES,
    EXPLANATION_NUMERIC_MISMATCH,
    STALE_PROPOSAL_REJECTED,
    AuditCategory,
)
from app.db.models import AuditLog, Base, Setting
from app.db.session import create_db_engine
from app.settings import Settings

NOW = datetime(2026, 3, 2, 8, 15, 0)

#: R24.4 点名的 14 类 + design.md 补充的 3 类 + 运维侧 1 类（`DEMO_RESET`，任务 1.6）。
#: 数字写死是刻意的：新增一个类别应当是一次有意识的决定，因此要连带改这个测试。
EXPECTED_CATEGORIES = {
    "DATA_IMPORT",
    "MAPPING_CONFIRMATION",
    "PLAN_GENERATION",
    "DISRUPTION_REGISTERED",
    "IMPACT_CLASSIFICATION",
    "APPROVAL_ACTION",
    "AUTO_APPLY",
    "AUTO_REVERT",
    "PREFERENCE_RULE_CHANGE",
    "WEIGHT_CHANGE",
    "PROMPT_INJECTION_SUSPECTED",
    "TOOL_NOT_PERMITTED",
    "SANDBOX_WRITE_BLOCKED",
    "DEGRADED_MODE_SWITCH",
    "AGENT_RESERVED_KEY_DROPPED",
    "EXPLANATION_NUMERIC_MISMATCH",
    "STALE_PROPOSAL_REJECTED",
    "DEMO_RESET",
}


def _settings(db_path: str) -> Settings:
    return Settings(
        database_url=f"sqlite:///{db_path}",
        session_shared_password=SecretStr("test-shared-password"),
        session_secret_key=SecretStr("test-secret-key-that-is-long-enough-32"),
        llm_mode="STUB",
    )


@pytest.fixture
def db_file(tmp_path: Path) -> str:
    """文件型 SQLite。

    刻意**不用** `:memory:`：本模块要证明「审计走的是另一条连接」，而两个引擎各自
    打开的 `:memory:` 是两个互不相干的数据库，那样的测试会以最误导的方式通过。
    """
    return (tmp_path / "audit.db").as_posix()


@pytest.fixture
def business_engine(db_file: str) -> Iterator[Engine]:
    """业务引擎，并在同一个库上建表。"""
    engine = create_db_engine(_settings(db_file))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def audit_engine(db_file: str, business_engine: Engine) -> Iterator[Engine]:
    """审计引擎：同一个库文件，另一个引擎与连接池。"""
    engine = create_db_engine(_settings(db_file))
    audit.set_audit_engine(engine)
    yield engine
    audit.set_audit_engine(None)
    engine.dispose()


def _append_one(
    *,
    event_category: str = "APPROVAL_ACTION",
    event_type: str = "APPROVE",
    actor: str = "PLANNER",
    payload: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
) -> str:
    """写一条测试用审计记录。

    `event_category` 声明为 `str` 而非 `AuditCategory`：本模块要断言运行期校验会拒绝
    非法类别，而非法类别在 `AuditCategory` 下过不了 mypy——那正是第一道防线，但不能因此
    让第二道防线无法被测。
    """
    return audit.append(
        event_category=cast(AuditCategory, event_category),
        event_type=event_type,
        actor=actor,
        payload=payload if payload is not None else {"plan_id": "PLAN-0007", "plan_version": 1},
        subject_type="production_plan",
        subject_id="PLAN-0007",
        trace_id=trace_id,
        occurred_at=NOW,
    )


# --------------------------------------------------------------------------
# 1. 类别常量与 append() 的入口校验
# --------------------------------------------------------------------------


def test_all_audit_event_categories_are_defined() -> None:
    """18 个类别一个不少，含 tasks.md 1.4 点名的三个补充类别与 1.6 的 `DEMO_RESET`。"""
    assert AUDIT_EVENT_CATEGORIES == EXPECTED_CATEGORIES
    assert AGENT_RESERVED_KEY_DROPPED in AUDIT_EVENT_CATEGORIES
    assert EXPLANATION_NUMERIC_MISMATCH in AUDIT_EVENT_CATEGORIES
    assert STALE_PROPOSAL_REJECTED in AUDIT_EVENT_CATEGORIES


def test_append_persists_every_field(audit_engine: Engine) -> None:
    """`append()` 写入并立即提交，逐字段落库。"""
    audit_id = _append_one(trace_id="TRACE-001")

    with audit_engine.connect() as conn:
        row = conn.execute(
            select(AuditLog).where(AuditLog.audit_id == audit_id)
        ).one()

    assert row.event_category == "APPROVAL_ACTION"
    assert row.event_type == "APPROVE"
    assert row.actor == "PLANNER"
    assert row.subject_id == "PLAN-0007"
    assert row.payload == {"plan_id": "PLAN-0007", "plan_version": 1}
    assert row.trace_id == "TRACE-001"
    assert row.occurred_at == NOW
    assert audit_id.startswith("AUDIT-20260302T081500-")


def test_append_rejects_unknown_category(audit_engine: Engine) -> None:
    """类别是闭集合（R24.4）。即兴造值会让按类别筛选静默漏记录。"""
    with pytest.raises(ValueError, match="未知的审计事件类别"):
        _append_one(event_category="APPROVALS")


@pytest.mark.parametrize(
    ("event_type", "actor", "expected"),
    [("   ", "PLANNER", "event_type"), ("APPROVE", "  ", "actor")],
)
def test_append_rejects_blank_required_text(
    audit_engine: Engine, event_type: str, actor: str, expected: str
) -> None:
    """空 `event_type` / `actor` 的记录等于没记录。"""
    with pytest.raises(ValueError, match=expected):
        _append_one(event_type=event_type, actor=actor)


# --------------------------------------------------------------------------
# 2. 改写被拒：直接构造 UPDATE / DELETE 语句
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        # 朴素形式
        "UPDATE audit_log SET actor = 'ATTACKER'",
        "DELETE FROM audit_log",
        "DELETE FROM audit_log WHERE audit_id = 'x'",
        # identifier preparer 的各种引号形式
        'UPDATE "audit_log" SET actor = \'ATTACKER\'',
        'DELETE FROM "audit_log"',
        "UPDATE `audit_log` SET actor = 'ATTACKER'",
        "DELETE FROM [audit_log]",
        # schema 限定
        "UPDATE main.audit_log SET actor = 'ATTACKER'",
        'DELETE FROM "main"."audit_log"',
        # SQLite 的冲突子句与 REPLACE：同主键上的改写，与 UPDATE 无异
        "UPDATE OR ROLLBACK audit_log SET actor = 'ATTACKER'",
        "REPLACE INTO audit_log (audit_id) VALUES ('x')",
        "INSERT OR REPLACE INTO audit_log (audit_id) VALUES ('x')",
        # 前导空白与换行不该成为绕过手段
        "\n   DELETE FROM audit_log\n",
    ],
)
def test_mutating_statements_are_blocked(audit_engine: Engine, statement: str) -> None:
    """每一种写法都真的发一次，撞监听器。"""
    _append_one()
    with pytest.raises(AuditImmutableError, match="append-only"), audit_engine.begin() as conn:
        conn.execute(text(statement))


def test_blocked_statement_leaves_the_row_untouched(audit_engine: Engine) -> None:
    """拦下之后数据必须原样：抛错但改了一半是最糟的结果。"""
    audit_id = _append_one()

    with pytest.raises(AuditImmutableError), audit_engine.begin() as conn:
        conn.execute(text("UPDATE audit_log SET actor = 'ATTACKER'"))

    with audit_engine.connect() as conn:
        actors = conn.execute(select(AuditLog.actor)).scalars().all()
        remaining = conn.execute(select(AuditLog.audit_id)).scalars().all()

    assert actors == ["PLANNER"]
    assert remaining == [audit_id]


def test_orm_delete_is_blocked(audit_engine: Engine) -> None:
    """经 ORM 删除同样走不通——第 2 道机制在语句层，ORM 也得发 SQL。"""
    _append_one()
    with Session(audit_engine) as session:
        entry = session.execute(select(AuditLog)).scalars().one()
        session.delete(entry)
        with pytest.raises(AuditImmutableError):
            session.commit()


def test_orm_attribute_update_is_blocked(audit_engine: Engine) -> None:
    """改 ORM 实例属性后 flush 出的 UPDATE 也被拦下。"""
    _append_one()
    with Session(audit_engine) as session:
        entry = session.execute(select(AuditLog)).scalars().one()
        entry.actor = "ATTACKER"
        with pytest.raises(AuditImmutableError):
            session.commit()


def test_audit_module_exposes_no_mutation_functions() -> None:
    """第 1 道机制：ORM 层的公开面只有 `append()`。

    这条断言看着像同义反复，作用是把「有人顺手加了个 `update_audit_entry()`」变成一次
    测试失败，而不是一次 code review 的运气。
    """
    forbidden = {"update", "delete", "remove", "purge", "edit", "modify", "truncate"}
    public_names = {name for name in dir(audit) if not name.startswith("_")}
    assert not (public_names & forbidden), f"审计模块出现改写面：{sorted(public_names & forbidden)}"
    assert "append" in audit.__all__
    assert not (set(audit.__all__) & forbidden)


# --------------------------------------------------------------------------
# 3. 误杀不发生
# --------------------------------------------------------------------------


def test_update_of_another_table_is_allowed(audit_engine: Engine) -> None:
    """守卫按**目标表**判定，不按「语句里有没有出现这个词」。

    子查询里引用 `audit_log` 是完全合法的读用法（例如按审计记录挑出要改的行）。
    """
    with audit_engine.begin() as conn:
        conn.execute(
            insert(Setting).values(key="weights", value={"tardiness": 1}, updated_at=NOW)
        )
    _append_one()

    with audit_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE settings SET value = '{\"tardiness\": 2}' "
                "WHERE key IN (SELECT 'weights' FROM audit_log)"
            )
        )
        value = conn.execute(text("SELECT value FROM settings WHERE key = 'weights'")).scalar_one()

    assert "2" in str(value)


def test_deleting_another_table_is_allowed(audit_engine: Engine) -> None:
    """业务表的 DELETE 不受影响——`POST /demo/reset` 要清业务表而保留审计（R28.8）。"""
    with audit_engine.begin() as conn:
        conn.execute(insert(Setting).values(key="mode", value={"llm": "STUB"}, updated_at=NOW))
    _append_one()

    with audit_engine.begin() as conn:
        conn.execute(text("DELETE FROM settings"))
        settings_left = conn.execute(text("SELECT COUNT(*) FROM settings")).scalar_one()
        audit_left = conn.execute(text("SELECT COUNT(*) FROM audit_log")).scalar_one()

    assert settings_left == 0
    assert audit_left == 1


def test_appending_more_entries_is_always_allowed(audit_engine: Engine) -> None:
    """append-only 的另一半：追加必须畅通，且不覆盖既有条目。"""
    first = _append_one()
    second = _append_one(event_category="PLAN_GENERATION", event_type="GENERATED")

    with audit_engine.connect() as conn:
        ids = set(conn.execute(select(AuditLog.audit_id)).scalars().all())

    assert ids == {first, second}


# --------------------------------------------------------------------------
# 4. 旁路：独立连接与 AUDIT_BYPASS
# --------------------------------------------------------------------------


def test_audit_entry_survives_business_rollback(
    audit_engine: Engine, business_engine: Engine
) -> None:
    """审计写入不参与业务事务回滚（design.md Error Handling §4）。

    「业务失败了」这件事本身必须留痕。如果审计跟着业务一起回滚，日志里缺掉的恰好是
    最该在的那几条。
    """
    with Session(business_engine) as session:
        audit_id = _append_one(event_category="STALE_PROPOSAL_REJECTED", event_type="REJECTED")
        session.add(Setting(key="doomed", value={"x": 1}, updated_at=NOW))
        session.flush()
        session.rollback()

    with audit_engine.connect() as conn:
        surviving = conn.execute(select(AuditLog.audit_id)).scalars().all()
        business_rows = conn.execute(text("SELECT COUNT(*) FROM settings")).scalar_one()

    assert surviving == [audit_id], "业务回滚把审计记录一起带走了"
    assert business_rows == 0, "业务写入本该被回滚"


def test_audit_bypass_is_visible_to_a_sandbox_style_guard(audit_engine: Engine) -> None:
    """审计写入不被沙箱守卫拦下（tasks.md 1.4 点名的断言）。

    任务 8.1 的守卫尚未落地，因此这里在测试内装一个**谓词与 design.md ADR-009 完全一致**
    的替身：`SANDBOX_ACTIVE and not AUDIT_BYPASS` 时拒绝一切 DML。它消费的是
    `app.db.audit` 里那个真实的 `AUDIT_BYPASS`，因此断言的是真实旁路是否成立——
    只有替身是本地的，被测的机制不是。

    8.1 落地时应当 import 本模块的 `AUDIT_BYPASS`，而不是另建同名 ContextVar：后者会让
    旁路静默失效，表现为审计悄悄丢记录。
    """
    sandbox_active: ContextVar[bool] = ContextVar("SANDBOX_ACTIVE_STANDIN", default=False)
    blocked: list[str] = []

    class SandboxWriteBlocked(RuntimeError):
        pass

    def guard(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if not sandbox_active.get() or AUDIT_BYPASS.get():
            return
        if statement.lstrip()[:6].upper() in {"INSERT", "UPDATE", "DELETE"}:
            blocked.append(statement)
            raise SandboxWriteBlocked(statement)

    event.listen(Engine, "before_cursor_execute", guard)
    try:
        token = sandbox_active.set(True)
        try:
            # 沙箱内的业务写入：被替身守卫拦下，证明守卫确实在工作。
            with pytest.raises(SandboxWriteBlocked), audit_engine.begin() as conn:
                conn.execute(
                    insert(Setting).values(key="sandbox", value={"x": 1}, updated_at=NOW)
                )

            # 同一沙箱上下文里的审计写入：必须畅通。
            audit_id = _append_one(
                event_category="SANDBOX_WRITE_BLOCKED",
                event_type="SCENARIO_WRITE_ATTEMPT",
                actor="SYSTEM",
            )
        finally:
            sandbox_active.reset(token)
    finally:
        event.remove(Engine, "before_cursor_execute", guard)

    assert len(blocked) == 1, "替身守卫没拦下业务写入，这个测试就不构成证明"
    with audit_engine.connect() as conn:
        assert conn.execute(select(AuditLog.audit_id)).scalars().all() == [audit_id]


def test_audit_bypass_is_restored_after_append(audit_engine: Engine) -> None:
    """`append()` 退出后旗标恢复原值，不给后续语句留一个敞开的旁路。"""
    assert AUDIT_BYPASS.get() is False
    _append_one()
    assert AUDIT_BYPASS.get() is False
