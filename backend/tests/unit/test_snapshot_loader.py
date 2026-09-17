"""`load_snapshot()` 的行为断言（任务 2.1，R5.6 / R5.7 / R6.3）。

测试走**真实 SQLite 文件与真实 seed 数据**，不 mock 会话：本任务交付的东西就是「这批
SQLAlchemy 查询在此 schema 上读出了什么、又漏掉了什么」，把会话 mock 掉之后剩下的只是一
组字段拷贝断言。

用 seed 数据集（任务 1.6）而不是手搓几行：它已经具备演示情节所需的全部结构——瓶颈机、
落在时域外的到货、窄班次工人、保养窗、三级换型规则。手搓夹具会漏掉恰好是边界的那几行。

四组性质：

1. **读全**——11 张规划相关表都进快照，子行挂在正确的父实体上。漏读一张表不会报错，只会
   让排产在一个「没有停机窗、没有缺勤」的理想车间里算出不可执行的计划。
2. **排除 `REVERTED`**——软删除的记录不参与排产（design.md §4.2）。漏掉这个条件等于批次
   回滚没有生效。
3. **解绑**——`expunge_all()` 之后没有对象还连着会话（沙箱第 1 层隔离的另一半）。
4. **引用完整性**——被回滚的父实体仍被活着的记录引用时，`DATA_INTEGRITY_ERROR` 列出全部
   坏引用（R5.6）。这是回滚在真实系统里最常见的失效形态。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.snapshot import DataIntegrityError, available_at
from app.db import models as orm
from app.db.models import Base
from app.db.repositories import current_input_snapshot_version
from app.db.session import create_db_engine, create_session_factory, session_scope
from app.seed import dataset
from app.seed.loader import load_demo_data
from app.services.snapshot_loader import load_snapshot
from app.settings import Settings

#: 内核的 `now` 由入参显式传入。取 seed 锚点，使断言里的时刻与演示数据同一坐标系。
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
    eng = create_db_engine(_settings((tmp_path / "snapshot.db").as_posix()))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def seeded(factory: sessionmaker[Session]) -> sessionmaker[Session]:
    """写入演示数据并提交。返回同一个工厂，供各测试自取会话。"""
    with session_scope(factory) as session:
        load_demo_data(session)
    return factory


# --------------------------------------------------------------------------
# ① 读全
# --------------------------------------------------------------------------


def test_load_snapshot_reads_every_planning_entity(seeded: sessionmaker[Session]) -> None:
    """五类实体的数量与 seed 数据集一致（R28.1 的 6 / 14 / 10 / 5 / 8）。"""
    with session_scope(seeded) as session:
        snapshot = load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)

    assert len(snapshot.products) == len(dataset.PRODUCTS) == 6
    assert len(snapshot.orders) == len(dataset.ORDERS) == 14
    assert len(snapshot.materials) == len(dataset.MATERIALS) == 10
    assert len(snapshot.machines) == len(dataset.MACHINES) == 5
    assert len(snapshot.workers) == len(dataset.WORKERS) == 8
    assert len(snapshot.changeover_rules) == len(dataset.CHANGEOVER_RULES) == 11
    #: seed 不建偏好规则，且未启用的规则本来也不进快照。
    assert snapshot.preference_rules == ()


def test_load_snapshot_attaches_child_rows_to_the_right_parent(
    seeded: sessionmaker[Session],
) -> None:
    """工序 / BOM / 到货 / 停机 / 缺勤挂在正确的父实体上。"""
    with session_scope(seeded) as session:
        snapshot = load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)

    products = snapshot.products_by_id()
    # `PRD-HOUSING` 有三道工序（深孔钻 → 精铣 → 焊接），BOM 四行。
    housing = products["PRD-HOUSING"]
    assert [op.sequence for op in housing.operations_in_sequence()] == [1, 2, 3]
    assert housing.operations_in_sequence()[0].required_capability == dataset.CAP_DEEP_DRILLING
    assert {line.material_id for line in housing.bom} == {
        dataset.SCARCE_MATERIAL_ID,
        "MAT-SEAL-07",
        "MAT-WELDWIRE-05",
        "MAT-COPPER-09",
    }

    materials = snapshot.materials_by_id()
    scarce = materials[dataset.SCARCE_MATERIAL_ID]
    assert [delivery.delivery_id for delivery in scarce.incoming_deliveries] == ["DLV-001"]
    assert scarce.incoming_deliveries[0].confirmed is False
    assert materials["MAT-ALU-02"].incoming_deliveries == ()

    machines = snapshot.machines_by_id()
    # `DT-001` 是 CNC-02 第 2 天 12:00–16:00 的保养窗。
    assert [window.reason for window in machines["CNC-02"].downtime_windows] == ["MAINTENANCE"]
    assert machines["CNC-02"].downtime_windows[0].start == dataset.at_offset(NOW, 1, 12)
    assert machines[dataset.BOTTLENECK_MACHINE_ID].downtime_windows == ()
    assert dataset.BOTTLENECK_CAPABILITY in machines[dataset.BOTTLENECK_MACHINE_ID].capabilities

    workers = snapshot.workers_by_id()
    assert len(workers["W-08"].absences) == 1
    assert workers["W-08"].absences[0].start == dataset.at_offset(NOW, 1, 8)
    assert workers["W-01"].absences == ()
    # 窄班次工人（第 1 天 08:00–16:00），`SHIFT_WINDOW_EXCEEDED` 的演示素材。
    assert workers["W-07"].shift_end == dataset.at_offset(NOW, 0, 16)


def test_load_snapshot_carries_now_and_version(seeded: sessionmaker[Session]) -> None:
    """`now` 原样进快照；`snapshot_version` 等于当前输入版本号（R12.2–3 的比对基准）。"""
    with session_scope(seeded) as session:
        version = current_input_snapshot_version(session)
        snapshot = load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)

    assert snapshot.now == NOW
    assert snapshot.production_date == PRODUCTION_DATE
    assert snapshot.snapshot_version == version >= 1


def test_production_date_defaults_to_the_date_of_now(seeded: sessionmaker[Session]) -> None:
    """`production_date` 缺省由 `now` 派生——全流程里唯一一次派生，且发生在内核之外。"""
    with session_scope(seeded) as session:
        snapshot = load_snapshot(session, now=datetime(2026, 3, 4, 6, 30))

    assert snapshot.production_date == date(2026, 3, 4)


def test_explicit_snapshot_version_is_honoured(seeded: sessionmaker[Session]) -> None:
    """允许传入版本号：基线与正式计划必须同口径（属性 37 / design.md §3.4）。"""
    with session_scope(seeded) as session:
        snapshot = load_snapshot(session, now=NOW, snapshot_version=42)

    assert snapshot.snapshot_version == 42


def test_changeover_rules_are_ordered_by_specificity_desc(
    seeded: sessionmaker[Session],
) -> None:
    """换型规则按 `specificity` 降序读出：内核的三级查表因此是一次线性扫描。"""
    with session_scope(seeded) as session:
        snapshot = load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)

    specificities = [rule.specificity for rule in snapshot.changeover_rules]
    assert specificities == sorted(specificities, reverse=True)
    assert specificities[0] == 3
    assert specificities[-1] == 1, "全局默认规则必须在快照里（否则无兜底换型时间）"


def test_material_semantics_hold_on_seed_data(seeded: sessionmaker[Session]) -> None:
    """`available_at` 在演示数据上给出与 seed 派生量一致的口径（R6.3、R28.3）。

    `DLV-003` 的 ETA 落在时域之外，因此在时域末端的可用量里**不**被计入——这条边界在
    `dataset.material_supply_within_horizon()` 里已经算过一遍，两处必须一致，否则「3 天内
    耗尽的物料」这个演示前提与排产器看到的现实不是一回事。
    """
    with session_scope(seeded) as session:
        snapshot = load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)

    horizon_end = dataset.at_offset(NOW, dataset.HORIZON_DAYS - 1, 20)
    expected = dataset.material_supply_within_horizon(NOW)
    for material in snapshot.materials:
        assert available_at(material, horizon_end) == expected[material.material_id]

    scarce = snapshot.materials_by_id()[dataset.SCARCE_MATERIAL_ID]
    demand = dataset.material_demand()[dataset.SCARCE_MATERIAL_ID]
    assert demand - available_at(scarce, horizon_end) == dataset.EXPECTED_STEEL_SHORTFALL


def test_two_loads_of_the_same_database_are_identical(seeded: sessionmaker[Session]) -> None:
    """同一份库两次加载得到逐字段相同的快照（R5.7 的加载侧前提）。"""
    with session_scope(seeded) as session:
        first = load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)
    with session_scope(seeded) as session:
        second = load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)

    assert first == second


def test_kernel_snapshot_omits_untrusted_order_notes(seeded: sessionmaker[Session]) -> None:
    """`Order.notes` 不进内核。

    seed 里的 `ORD-013` 的 `notes` 藏着注入文本（R28.6 的演示素材）。它在快照里根本没有
    落点，因此「未包裹的不受信任文本被拼进解释载荷」这条路径在类型层面就不存在——
    `Guardrail_Layer`（任务 5.8）从库里单独读它并包裹。
    """
    with session_scope(seeded) as session:
        snapshot = load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)

    injected = snapshot.orders_by_id()[dataset.INJECTION_DEMO_ORDER_ID]
    assert not hasattr(injected, "notes")
    assert "notes" not in injected.model_dump()


# --------------------------------------------------------------------------
# ② 排除 REVERTED
# --------------------------------------------------------------------------


def _revert(session: Session, model: type[orm.Base], pk_column: object, pk: str) -> None:
    """把一行置为 `REVERTED`（软删除，R3.4）。用 Core 语句，绕开 ORM 的 flush 钩子。"""
    session.execute(
        update(model).where(pk_column == pk).values(record_status="REVERTED")  # type: ignore[arg-type]
    )


def test_reverted_rows_do_not_participate_in_planning(seeded: sessionmaker[Session]) -> None:
    """`record_status = REVERTED` 的记录不进快照，其子行随之消失（design.md §4.2）。

    回滚的是 `WELD-01` 与订单 `ORD-005`：两者都不被别的活行引用，因此这里验的是纯过滤。
    `CO-010` 是 `WELD-01` 的机器默认换型规则——它随机器一起消失，而不是变成一条悬空引用
    （「缺了它只是不适用的，过滤」）。
    """
    with session_scope(seeded) as session:
        _revert(session, orm.Machine, orm.Machine.machine_id, "WELD-01")
        _revert(session, orm.Order, orm.Order.order_id, "ORD-005")

    with session_scope(seeded) as session:
        snapshot = load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)

    assert "WELD-01" not in snapshot.machines_by_id()
    assert len(snapshot.machines) == len(dataset.MACHINES) - 1
    assert "ORD-005" not in snapshot.orders_by_id()
    assert len(snapshot.orders) == len(dataset.ORDERS) - 1
    assert "CO-010" not in {rule.rule_id for rule in snapshot.changeover_rules}
    # 全局规则不挂机器，永远保留。
    assert "CO-011" in {rule.rule_id for rule in snapshot.changeover_rules}


# --------------------------------------------------------------------------
# ③ 与 ORM identity map 解绑
# --------------------------------------------------------------------------


def test_load_snapshot_detaches_everything_from_the_session(
    seeded: sessionmaker[Session],
) -> None:
    """`expunge_all()` 之后会话里不剩任何对象（沙箱第 1 层隔离，design.md §3.7）。

    留在 identity map 里的对象是**可写的**：拿到其中一个改一个字段，下一次 flush 就把它写
    进了库。快照本身冻结不冻结都挡不住这条路径，因为那条路径不经过快照。
    """
    with session_scope(seeded) as session:
        snapshot = load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)

        assert len(session.identity_map) == 0, "读出的 ORM 对象仍留在 identity map 里"
        assert not session.new and not session.dirty and not session.deleted
        # 快照里的每一个对象都不是 ORM 实例。
        assert all(not isinstance(order, orm.Order) for order in snapshot.orders)


def test_load_snapshot_refuses_a_dirty_session(seeded: sessionmaker[Session]) -> None:
    """会话里有未提交改动时拒绝加载。

    `autoflush=False` 让待写入的行对 SELECT 不可见，而 `input_snapshot_version` 可能已被
    同事务的钩子推进——快照内容与版本号从此不同源，而那个版本号正是审批时判断「数据是否
    变过」的唯一依据（R12.2–3）。
    """
    with session_scope(seeded) as session:
        session.add(
            orm.Material(
                material_id="MAT-NEW-11",
                name="新物料",
                unit="kg",
                quantity_available=Decimal("1"),
                reserved_quantity=Decimal("0"),
                source="MANUAL_ENTRY",
                record_status="ACTIVE",
                last_updated_at=NOW,
            )
        )
        with pytest.raises(RuntimeError, match="干净的会话"):
            load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)


# --------------------------------------------------------------------------
# ④ 引用完整性（R5.6）
# --------------------------------------------------------------------------


def test_reverted_parent_still_referenced_raises_data_integrity_error(
    seeded: sessionmaker[Session],
) -> None:
    """回滚了被引用的父实体 → `DATA_INTEGRITY_ERROR`，且列出**全部**坏引用。

    这是 R5.6 在真实系统里的主要成因：库级外键仍然满足（行还在表里，只是
    `record_status = REVERTED`），因此数据库不会报错——`load_snapshot` 的预检是唯一的
    发现点。

    回滚 `PRD-PLATE`（三张订单引用它）与 `MAT-GREASE-08`（`PRD-BUSHING` 的 BOM 引用它），
    两类坏引用一次出齐，断言四条都在清单里。
    """
    with session_scope(seeded) as session:
        _revert(session, orm.Product, orm.Product.product_id, "PRD-PLATE")
        _revert(session, orm.Material, orm.Material.material_id, "MAT-GREASE-08")

    with session_scope(seeded) as session, pytest.raises(DataIntegrityError) as excinfo:
        load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)

    error = excinfo.value
    assert error.code == "DATA_INTEGRITY_ERROR"
    reported = {(ref.entity_type, ref.entity_id, ref.missing_id) for ref in error.references}
    assert reported == {
        ("Order", "ORD-003", "PRD-PLATE"),
        ("Order", "ORD-010", "PRD-PLATE"),
        ("Order", "ORD-013", "PRD-PLATE"),
        ("Product", "PRD-BUSHING", "MAT-GREASE-08"),
    }
    # 载荷可直接进错误响应的 `details`（design.md Error Handling §2）。
    assert len(error.details()["bad_references"]) == 4


def test_intact_seed_data_passes_the_precheck(seeded: sessionmaker[Session]) -> None:
    """未经改动的演示数据必须通过预检——演示数据自己触发 R5.6 是最尴尬的失败形态。"""
    with session_scope(seeded) as session:
        snapshot = load_snapshot(session, now=NOW, production_date=PRODUCTION_DATE)

    assert len(snapshot.orders) == len(dataset.ORDERS)
