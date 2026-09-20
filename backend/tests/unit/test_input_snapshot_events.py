"""`input_snapshot_version` 推进钩子（任务 1.3）的行为断言。

被守的性质有三条，每条都对应 `Approval_Service` 陈旧检测的一种失效方式：

1. **规划相关数据变了，版本号必须推进**——漏推进 = 过期提案看起来新鲜 = 静默激活一个
   基于旧数据的计划（R12.2–3 的正面）。
2. **规划相关数据没变，版本号绝不推进**——多推进 = 所有待审批提案被无端判成陈旧，
   审批闭环卡死（R12.2–3 的反面，同样致命且更容易发生：一次纯读取就可能触发）。
3. **推进与变更同事务**——回滚一起消失，提交一起生效。这是「同一事务内插入」的实质。

测试走真实 SQLite 文件与真实 ORM flush，不 mock 事件系统：本任务交付的东西**就是**
SQLAlchemy 的 flush 语义在此 schema 上的行为，把它 mock 掉就什么也没验证。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.events import (
    DEFAULT_TRIGGER,
    compute_planning_fingerprint,
    register_input_snapshot_hooks,
    snapshot_trigger,
    unregister_input_snapshot_hooks,
)
from app.db.models import (
    Base,
    InputSnapshot,
    Machine,
    Material,
    Order,
    Product,
    ProductionPlan,
    Trace,
)
from app.db.repositories import (
    NO_SNAPSHOT_VERSION,
    SnapshotRepository,
    current_input_snapshot_version,
)
from app.db.session import create_db_engine, create_session_factory
from app.settings import Settings

NOW = datetime(2026, 3, 2, 8, 0)


def _settings(db_path: str) -> Settings:
    return Settings(
        database_url=f"sqlite:///{db_path}",
        session_shared_password=SecretStr("test-shared-password"),
        session_secret_key=SecretStr("test-secret-key-that-is-long-enough-32"),
        llm_mode="STUB",
    )


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_file = (tmp_path / "snapshots.db").as_posix()
    eng = create_db_engine(_settings(db_file))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    """挂了钩子的会话工厂。

    钩子挂在**工厂**而不是 `Session` 类上，用完即摘：挂在类上会泄漏到同一进程里其他
    测试模块的会话，让那些测试的行为取决于执行顺序。
    """
    session_factory = create_session_factory(engine)
    yield session_factory
    unregister_input_snapshot_hooks(session_factory)


# --------------------------------------------------------------------------
# 行工厂
# --------------------------------------------------------------------------


def _product(product_id: str = "PROD-01") -> Product:
    return Product(
        product_id=product_id,
        name="Widget",
        source="SEED_DATA",
        record_status="ACTIVE",
        last_updated_at=NOW,
    )


def _order(order_id: str, product_id: str = "PROD-01") -> Order:
    return Order(
        order_id=order_id,
        product_id=product_id,
        quantity=Decimal("10"),
        due_date=NOW + timedelta(days=3),
        priority="NORMAL",
        injection_suspected=False,
        source="SEED_DATA",
        record_status="ACTIVE",
        last_updated_at=NOW,
    )


def _material(material_id: str = "MAT-01") -> Material:
    return Material(
        material_id=material_id,
        name="Steel bar",
        unit="pcs",
        quantity_available=Decimal("100"),
        reserved_quantity=Decimal("0"),
        source="SEED_DATA",
        record_status="ACTIVE",
        last_updated_at=NOW,
    )


def _machine(machine_id: str = "CNC-01") -> Machine:
    return Machine(
        machine_id=machine_id,
        machine_type="CNC",
        capabilities=[],
        status="AVAILABLE",
        available_start=NOW,
        available_end=NOW + timedelta(hours=8),
        rate_multiplier=Decimal("1"),
        source="SEED_DATA",
        record_status="ACTIVE",
        last_updated_at=NOW,
    )


def _trace(trace_id: str = "TR-0001") -> Trace:
    return Trace(
        trace_id=trace_id,
        kind="GENERATE_PLAN",
        mode="PIPELINE",
        trigger_source="PLANNER_UI",
        session_id="SESS-01",
        started_at=NOW,
    )


def _snapshot_rows(session: Session) -> list[InputSnapshot]:
    return list(
        session.execute(select(InputSnapshot).order_by(InputSnapshot.snapshot_version))
        .scalars()
        .all()
    )


def _seed_product(factory: sessionmaker[Session]) -> None:
    """先放一个产品进去，作为后续订单的外键依赖。自身也推进一格。"""
    with factory() as session:
        session.add(_product())
        session.commit()


# --------------------------------------------------------------------------
# 性质 1：规划相关数据变了，版本号必须推进
# --------------------------------------------------------------------------


def test_empty_database_reports_no_snapshot_version(factory: sessionmaker[Session]) -> None:
    """一行快照都没有时返回哨兵 0，而不是抛异常。"""
    with factory() as session:
        assert current_input_snapshot_version(session) == NO_SNAPSHOT_VERSION


def test_insert_into_planning_table_advances_version_by_one(
    factory: sessionmaker[Session],
) -> None:
    """INSERT 推进一格，且恰好一格。"""
    with factory() as session:
        session.add(_product())
        session.commit()
        assert current_input_snapshot_version(session) == 1
        assert len(_snapshot_rows(session)) == 1


def test_update_of_planning_table_advances_version(factory: sessionmaker[Session]) -> None:
    """UPDATE 推进一格。"""
    _seed_product(factory)
    with factory() as session:
        product = session.get(Product, "PROD-01")
        assert product is not None
        product.name = "Widget Mk2"
        session.commit()
        assert current_input_snapshot_version(session) == 2


def test_delete_from_planning_table_advances_version(factory: sessionmaker[Session]) -> None:
    """DELETE 推进一格——删掉一台机器同样让既有提案过期。"""
    with factory() as session:
        session.add(_machine())
        session.commit()
    with factory() as session:
        machine = session.get(Machine, "CNC-01")
        assert machine is not None
        session.delete(machine)
        session.commit()
        assert current_input_snapshot_version(session) == 2


@pytest.mark.parametrize(
    "table_name",
    ["orders", "products", "materials", "machines"],
)
def test_each_planning_table_is_watched(factory: sessionmaker[Session], table_name: str) -> None:
    """被监听的是「一组表」，不是某一张表。

    抽查四张：`PLANNING_RELEVANT_TABLES` 的成员资格是由集合常量决定的，逐张全测只是在
    重复常量的内容；抽查覆盖「实体表 / 库存表 / 资源表」三类形态即可。
    """
    _seed_product(factory)
    before = 1
    with factory() as session:
        if table_name == "orders":
            session.add(_order("ORD-0001"))
        elif table_name == "products":
            session.add(_product("PROD-02"))
        elif table_name == "materials":
            session.add(_material())
        else:
            session.add(_machine())
        session.commit()
        assert current_input_snapshot_version(session) == before + 1


def test_repository_wrapper_returns_the_same_version(factory: sessionmaker[Session]) -> None:
    """`SnapshotRepository` 与模块级函数是同一份实现的两种形态。"""
    _seed_product(factory)
    with factory() as session:
        assert SnapshotRepository(session).current_input_snapshot_version() == 1


# --------------------------------------------------------------------------
# 性质 2：规划相关数据没变，版本号绝不推进
# --------------------------------------------------------------------------


def test_read_only_transaction_does_not_advance_version(factory: sessionmaker[Session]) -> None:
    """纯读取不推进。

    这是最容易踩的假阳性：状态看板每 3 秒轮询一次（R1），若读取推进版本号，任何提案都
    活不到被点「批准」的那一刻。
    """
    _seed_product(factory)
    with factory() as session:
        product = session.get(Product, "PROD-01")
        assert product is not None
        _ = product.name  # 碰一下属性，让对象进入 identity map
        session.commit()
        assert current_input_snapshot_version(session) == 1


def test_assigning_an_identical_value_does_not_advance_version(
    factory: sessionmaker[Session],
) -> None:
    """把原值赋回去不是变更。

    赋值会把对象放进 `session.dirty`，但 `is_modified` 复核会发现没有净变化。少了那道
    复核，一次「读出来又写回去」的幂等保存就会误伤全部待审批提案。
    """
    _seed_product(factory)
    with factory() as session:
        product = session.get(Product, "PROD-01")
        assert product is not None
        product.name = "Widget"  # 与原值相同
        session.commit()
        assert current_input_snapshot_version(session) == 1


@pytest.mark.parametrize("entity_name", ["trace", "plan"])
def test_non_planning_tables_do_not_advance_version(
    factory: sessionmaker[Session], entity_name: str
) -> None:
    """计划与 Trace 的写入不改变「排产输入」，因此不推进。

    这条决定了审批本身不会把它自己判成陈旧：`approve()` 要写 `production_plans` 与
    `plan_approvals`，若那些写入推进版本号，则「批准」这个动作会在提交的瞬间让刚刚
    通过陈旧检测的计划变成陈旧的。
    """
    _seed_product(factory)
    with factory() as session:
        if entity_name == "trace":
            session.add(_trace())
        else:
            session.add(
                ProductionPlan(
                    plan_id="PLAN-0001",
                    production_date=NOW.date(),
                    status="PENDING_APPROVAL",
                    feasibility="FEASIBLE",
                    plan_version=1,
                    version=1,
                    input_snapshot_version=1,
                    origin="PLAN_GENERATION",
                    created_at=NOW,
                )
            )
        session.commit()
        assert current_input_snapshot_version(session) == 1


# --------------------------------------------------------------------------
# 性质 3：推进与变更同事务
# --------------------------------------------------------------------------


def test_rollback_discards_the_version_advance(factory: sessionmaker[Session]) -> None:
    """回滚后既没有数据也没有快照行——两者同生共死。"""
    with factory() as session:
        session.add(_product())
        session.flush()
        assert current_input_snapshot_version(session) == 1, "flush 后事务内应已可见"
        session.rollback()
    with factory() as session:
        assert current_input_snapshot_version(session) == NO_SNAPSHOT_VERSION
        assert session.get(Product, "PROD-01") is None


def test_a_transaction_after_a_rollback_still_advances(factory: sessionmaker[Session]) -> None:
    """回滚后的下一个事务必须重新拿到一格。

    回滚时暂存的版本号若没被清掉，下一个事务会去 `UPDATE` 一个不存在的行——SQL 静默成功，
    版本号从此永不推进，陈旧检测彻底失效且不报错。
    """
    session = factory()
    try:
        session.add(_product())
        session.flush()
        session.rollback()
        session.add(_product("PROD-02"))
        session.commit()
        assert current_input_snapshot_version(session) == 1
        assert len(_snapshot_rows(session)) == 1
    finally:
        session.close()


def test_multiple_flushes_in_one_transaction_insert_exactly_one_row(
    factory: sessionmaker[Session],
) -> None:
    """一次事务 = 一次变更事件 = 一格版本号，不论中间 flush 了几次。

    seed 与批量导入都会在一个事务里多次 flush；按 flush 计数会让一次导入跳好几格，
    版本号就不再是「变更事件的序号」。
    """
    with factory() as session:
        session.add(_product())
        session.flush()
        session.add(_order("ORD-0001"))
        session.flush()
        session.add(_material())
        session.commit()
        assert len(_snapshot_rows(session)) == 1
        assert current_input_snapshot_version(session) == 1


def test_successive_transactions_each_advance_once(factory: sessionmaker[Session]) -> None:
    """连续三个事务各推进一格，版本号严格递增。"""
    _seed_product(factory)
    for index in range(2, 5):
        with factory() as session:
            session.add(_order(f"ORD-{index:04d}"))
            session.commit()
            assert current_input_snapshot_version(session) == index
    with factory() as session:
        versions = [row.snapshot_version for row in _snapshot_rows(session)]
        assert versions == [1, 2, 3, 4]


def test_same_session_reused_across_transactions_advances_each_time(
    factory: sessionmaker[Session],
) -> None:
    """同一个 `Session` 连续提交两次也要各推进一格（暂存状态按事务清理）。"""
    session = factory()
    try:
        session.add(_product())
        session.commit()
        session.add(_order("ORD-0001"))
        session.commit()
        assert current_input_snapshot_version(session) == 2
    finally:
        session.close()


# --------------------------------------------------------------------------
# trigger 与 fingerprint
# --------------------------------------------------------------------------


def test_trigger_defaults_to_manual_edit(factory: sessionmaker[Session]) -> None:
    """未声明来源时记 `MANUAL_EDIT`：不声称任何来源，只声称有人改了数据。"""
    _seed_product(factory)
    with factory() as session:
        assert _snapshot_rows(session)[0].trigger == DEFAULT_TRIGGER


@pytest.mark.parametrize("trigger", ["IMPORT_COMMIT", "SEED", "DISRUPTION", "REVERT"])
def test_declared_trigger_is_recorded(factory: sessionmaker[Session], trigger: str) -> None:
    """`snapshot_trigger()` 声明的来源落到快照行上。"""
    with snapshot_trigger(trigger), factory() as session:
        session.add(_product())
        session.commit()
    with factory() as session:
        assert _snapshot_rows(session)[0].trigger == trigger


def test_unknown_trigger_is_rejected() -> None:
    """取值域在这里守——`trigger` 列没有库级 CHECK。"""
    with pytest.raises(ValueError, match="未知的快照触发原因"), snapshot_trigger("WHATEVER"):
        pass  # pragma: no cover - 上一行即抛错


def test_trigger_is_restored_after_the_context_exits(factory: sessionmaker[Session]) -> None:
    """ContextVar 用完必须还原，否则一次导入会把后续所有手工编辑都记成 `IMPORT_COMMIT`。"""
    with snapshot_trigger("IMPORT_COMMIT"), factory() as session:
        session.add(_product())
        session.commit()
    with factory() as session:
        session.add(_material())
        session.commit()
    with factory() as session:
        assert [row.trigger for row in _snapshot_rows(session)] == [
            "IMPORT_COMMIT",
            DEFAULT_TRIGGER,
        ]


def test_fingerprint_is_recorded_and_changes_with_the_data(
    factory: sessionmaker[Session],
) -> None:
    """fingerprint 非空，且不同数据状态得到不同摘要。"""
    _seed_product(factory)
    with factory() as session:
        session.add(_order("ORD-0001"))
        session.commit()
        rows = _snapshot_rows(session)
    assert all(row.fingerprint for row in rows)
    assert rows[0].fingerprint != rows[1].fingerprint


def test_fingerprint_reflects_the_state_at_transaction_end(
    factory: sessionmaker[Session],
) -> None:
    """多次 flush 时，那唯一一行的 fingerprint 描述事务**结束时**的状态。

    在 `before_flush` 里算 fingerprint 就会得到变更前的状态；这条断言把那个错误钉死。
    """
    with factory() as session:
        session.add(_product())
        session.flush()
        session.add(_order("ORD-0001"))
        session.commit()
        recorded = _snapshot_rows(session)[0].fingerprint
        expected = compute_planning_fingerprint(session)
    assert recorded == expected


def test_fingerprint_is_deterministic_for_the_same_state(
    factory: sessionmaker[Session],
) -> None:
    """同一状态两次计算得到同一摘要——否则它无法用于比对。"""
    _seed_product(factory)
    with factory() as session:
        first = compute_planning_fingerprint(session)
        second = compute_planning_fingerprint(session)
    assert first == second


def test_fingerprint_ignores_non_planning_tables(factory: sessionmaker[Session]) -> None:
    """写 Trace 不改变 fingerprint：它只摘要 11 张规划相关表。"""
    _seed_product(factory)
    with factory() as session:
        before = compute_planning_fingerprint(session)
        session.add(_trace())
        session.commit()
        assert compute_planning_fingerprint(session) == before


# --------------------------------------------------------------------------
# 注册的幂等性
# --------------------------------------------------------------------------


def test_registering_twice_does_not_double_advance(factory: sessionmaker[Session]) -> None:
    """重复注册不能让一次 flush 插两行——版本号一次跳两格会让陈旧检测的语义失真。"""
    register_input_snapshot_hooks(factory)
    with factory() as session:
        session.add(_product())
        session.commit()
        assert len(_snapshot_rows(session)) == 1


def test_version_is_monotonic_across_the_whole_history(factory: sessionmaker[Session]) -> None:
    """版本号严格递增，且 `MAX` 与写入顺序一致（`sqlite_autoincrement` 的效果）。"""
    _seed_product(factory)
    for index in range(2, 6):
        with factory() as session:
            session.add(_order(f"ORD-{index:04d}"))
            session.commit()
    with factory() as session:
        versions = [row.snapshot_version for row in _snapshot_rows(session)]
        assert versions == sorted(versions)
        assert len(set(versions)) == len(versions)
        assert current_input_snapshot_version(session) == max(versions)
        # 与库层直读一致，排除 ORM 缓存造成的假通过。
        raw = session.execute(text("SELECT MAX(snapshot_version) FROM input_snapshots"))
        assert raw.scalar_one() == max(versions)
