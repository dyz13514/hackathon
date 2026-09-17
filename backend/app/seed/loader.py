"""把 `dataset.py` 的纯数据写进库，并实现一键重置（任务 1.6，R28.8 / R27.11）。

## 重置的三条不变量

design.md「运维要点」：

> **一键重置**（R28.8）：`POST /api/demo/reset` 在事务内清空业务表并重放 seed，
> `input_snapshots` 重新从 1 开始；`Audit_Log` **不清空**（append-only 的语义要求），
> 改为写一条 `DEMO_RESET` 事件。

逐条落成的方式：

1. **事务内清空并重放。** 清空与重放在同一个 `session_scope` 里。中途失败则整体回滚，
   库退回重置前的状态——「清空成功但 seed 失败」会留下一个空库，那是演示开场前最不该
   出现的状态，而它恰好是最容易出现的：seed 比 DELETE 复杂得多。

2. **`input_snapshots` 从 1 开始。** 删掉表里的行**不够**：SQLite 的 `AUTOINCREMENT`
   把水位记在 `sqlite_sequence` 里，删行不重置水位，下一次插入会接着上次的号往下走。
   因此另有 `_restart_snapshot_sequence()` 显式复位（见其 docstring 里的方言分支）。
   这一条不是洁癖：`production_plans.input_snapshot_version` 指向它，而演示脚本里出现
   的版本号（「提案基于 v1，数据已到 v2」）必须每次重置后都一样。

3. **`Audit_Log` 不清空。** 清空表的循环显式跳过它。真要清也清不掉——`db/audit.py` 的
   语句级监听器会把 `DELETE FROM audit_log` 拦下并抛 `AuditImmutableError`（R24.3）。
   跳过是为了让意图明确，拦截是为了让意图无法被绕开；两者都在。

## 为什么清空用 Core `DELETE` 而不是 ORM

两个理由，第二个是硬性的：

- 快。不需要把几百行读成对象再逐个 `session.delete()`。
- **不触发 `input_snapshot_version` 钩子。** 钩子监听的是 ORM 的 flush
  （`session.new / deleted / dirty`），Core 语句不进 unit-of-work，因此清空阶段不会推进
  版本号。这正是我们要的：一次重置只应该产生**一个**变更事件，而那个事件是「seed 数据
  到位了」。若清空也算一次，重置后的版本号就会是 2 而不是 1，第 2 条不变量当场失效。

## 重置**不是**幂等的写操作，但结果是幂等的

连续两次重置产生的库内容逐字段相同（冒烟测试断言这一点），因为数据集里没有任何一处
依赖时钟或随机数（见 `dataset.py`）。但每次重置都会往 `audit_log` 追加一条记录——审计
行数只增不减，这正是 R24.3 要的语义，也是冒烟测试的第二条断言。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db import audit
from app.db.audit_events import DEMO_RESET
from app.db.events import snapshot_trigger
from app.db.models import (
    AuditLog,
    Base,
    ChangeoverRule,
    IncomingDelivery,
    InputSnapshot,
    Machine,
    MachineDowntime,
    Material,
    Operation,
    Order,
    Product,
    ProductMaterial,
    Worker,
    WorkerAbsence,
)
from app.db.repositories import current_input_snapshot_version
from app.db.session import session_scope
from app.seed.dataset import (
    ABSENCES,
    CHANGEOVER_RULES,
    DELIVERIES,
    DEMO_ANCHOR,
    DOWNTIME,
    MACHINES,
    MATERIALS,
    ORDERS,
    PRODUCTS,
    SEED_SOURCE,
    WORKERS,
    at_offset,
)

#: 重置时**不**清空的表。只有一张，理由见模块 docstring 第 3 条。
PRESERVED_TABLES: Final[frozenset[str]] = frozenset({AuditLog.__tablename__})

#: 一键重置的审计事件类型（`event_category` 同名，见 `audit_events.DEMO_RESET`）。
DEMO_RESET_EVENT_TYPE: Final = "DEMO_RESET"

#: 审计记录的 `subject_type` / `subject_id`：重置的主体是整个数据集，不是某一行。
_SUBJECT_TYPE: Final = "demo_dataset"
_SUBJECT_ID: Final = "demo"


@dataclass(frozen=True, slots=True)
class DemoResetReport:
    """一次重置的结果。`POST /api/demo/reset` 直接把它序列化进响应体。"""

    #: 重置后的输入版本号。第 2 条不变量要求它恒为 1。
    input_snapshot_version: int
    #: `表名 → 行数`，按表名排序，供冒烟测试逐表比对。
    row_counts: Mapping[str, int]
    #: 本次使用的时间锚点，写进响应便于演示时核对「今天」是哪天。
    anchor: datetime
    #: 补写的那条 `DEMO_RESET` 审计记录的 ID。
    audit_id: str


# --------------------------------------------------------------------------
# 纯数据 → ORM 行
# --------------------------------------------------------------------------


def build_row_tiers(anchor: datetime = DEMO_ANCHOR) -> list[list[Any]]:
    """把数据集展开成**两层**ORM 实例：先根实体，再引用它们的实体。

    返回**新构造**的实例而不是模块级常量：ORM 实例带持久化状态（identity map、session
    归属），复用同一批对象会让第二次 seed 拿到已经绑过会话的实例。

    ## 为什么必须分层，而不是交给 ORM 自己排

    ORM 在一次 flush 里的表间顺序来自 **mapper 依赖图**，而那张图是 `relationship()` 建
    起来的。`models.py` 刻意一个 `relationship()` 都没声明（外键只以列的形式存在），
    于是 SQLAlchemy 没有依赖信息可用，退化成按 mapper 的排序键——**类名字母序**。
    那个顺序在本 schema 上是错的：

    - `ChangeoverRule` < `Machine`——换型规则先插，撞 `machines` 外键；
    - `IncomingDelivery` < `Material`——到货先插，撞 `materials` 外键；
    - `Operation` / `Order` < `Product`——工序与订单先插，撞 `products` 外键。

    这三条都会在 `PRAGMA foreign_keys = ON` 下变成 `FOREIGN KEY constraint failed`。
    因此顺序由本函数显式给出，每层之间由 `load_demo_data()` 插一次 flush。分两层就够：
    第 2 层的每张表只引用第 1 层，层内没有互相引用。

    改动纪律：**新增实体时要判断它属于哪一层**。放错层的表现是 seed 直接失败（不是静默
    错乱），因此这个约定不需要额外的守卫。
    """
    return [_root_entities(anchor), _dependent_entities(anchor)]


def build_rows(anchor: datetime = DEMO_ANCHOR) -> list[Any]:
    """`build_row_tiers()` 的展平形式。供计数与表名核对用，不用于写库。"""
    return [row for tier in build_row_tiers(anchor) for row in tier]


def _root_entities(anchor: datetime) -> list[Any]:
    """第 1 层：不引用任何其他业务表的实体（产品、物料、机器、工人）。"""
    rows: list[Any] = []

    for product in PRODUCTS:
        rows.append(
            Product(
                product_id=product.product_id,
                name=product.name,
                description=product.description,
                source=SEED_SOURCE,
                record_status="ACTIVE",
                import_batch_id=None,
                last_updated_at=anchor,
            )
        )

    for material in MATERIALS:
        rows.append(
            Material(
                material_id=material.material_id,
                name=material.name,
                unit=material.unit,
                quantity_available=material.quantity_available,
                reserved_quantity=material.reserved_quantity,
                source=SEED_SOURCE,
                record_status="ACTIVE",
                import_batch_id=None,
                last_updated_at=anchor,
            )
        )

    for machine in MACHINES:
        rows.append(
            Machine(
                machine_id=machine.machine_id,
                machine_type=machine.machine_type,
                capabilities=list(machine.capabilities),
                status=machine.status,
                available_start=at_offset(anchor, *machine.available_from),
                available_end=at_offset(anchor, *machine.available_to),
                rate_multiplier=machine.rate_multiplier,
                source=SEED_SOURCE,
                record_status="ACTIVE",
                import_batch_id=None,
                last_updated_at=anchor,
            )
        )

    for worker in WORKERS:
        rows.append(
            Worker(
                worker_id=worker.worker_id,
                name=worker.name,
                skills=list(worker.skills),
                shift_start=at_offset(anchor, *worker.shift_start),
                shift_end=at_offset(anchor, *worker.shift_end),
                source=SEED_SOURCE,
                record_status="ACTIVE",
                import_batch_id=None,
                last_updated_at=anchor,
            )
        )

    return rows


def _dependent_entities(anchor: datetime) -> list[Any]:
    """第 2 层：引用第 1 层的实体。层内没有互相引用，因此顺序只影响可读性。"""
    rows: list[Any] = []

    for product in PRODUCTS:
        for operation in product.operations:
            rows.append(
                Operation(
                    # 确定性 ID：`{product}-OP{sequence}`，与 `ProductionJob.job_id` 的
                    # 构造同风格。UUID 会让两次重置产出不同的主键，幂等断言随即失败。
                    operation_id=f"{product.product_id}-OP{operation.sequence}",
                    product_id=product.product_id,
                    sequence=operation.sequence,
                    required_machine_type=operation.required_machine_type,
                    required_capability=operation.required_capability,
                    required_worker_skill=operation.required_worker_skill,
                    base_processing_time_per_unit=operation.base_processing_time_per_unit,
                    setup_time=operation.setup_time,
                )
            )
        for line in product.bom:
            rows.append(
                ProductMaterial(
                    product_id=product.product_id,
                    material_id=line.material_id,
                    quantity_per_unit=line.quantity_per_unit,
                )
            )

    for delivery in DELIVERIES:
        rows.append(
            IncomingDelivery(
                delivery_id=delivery.delivery_id,
                material_id=delivery.material_id,
                quantity=delivery.quantity,
                eta=at_offset(anchor, *delivery.eta),
                confirmed=delivery.confirmed,
                source=SEED_SOURCE,
                last_updated_at=anchor,
            )
        )

    for downtime in DOWNTIME:
        rows.append(
            MachineDowntime(
                downtime_id=downtime.downtime_id,
                machine_id=downtime.machine_id,
                start_time=at_offset(anchor, *downtime.start),
                end_time=at_offset(anchor, *downtime.end),
                reason=downtime.reason,
                # seed 里的保养不是扰动的产物（`MachineDowntime` 的 docstring）。
                disruption_id=None,
            )
        )

    for rule in CHANGEOVER_RULES:
        rows.append(
            ChangeoverRule(
                rule_id=rule.rule_id,
                machine_id=rule.machine_id,
                from_product_id=rule.from_product_id,
                to_product_id=rule.to_product_id,
                changeover_minutes=rule.changeover_minutes,
                specificity=rule.specificity,
            )
        )

    for absence in ABSENCES:
        rows.append(
            WorkerAbsence(
                absence_id=absence.absence_id,
                worker_id=absence.worker_id,
                start_time=at_offset(anchor, *absence.start),
                end_time=at_offset(anchor, *absence.end),
                disruption_id=None,
            )
        )

    for order in ORDERS:
        rows.append(
            Order(
                order_id=order.order_id,
                product_id=order.product_id,
                quantity=order.quantity,
                due_date=at_offset(anchor, *order.due_date),
                promised_date=(
                    at_offset(anchor, *order.promised_date)
                    if order.promised_date is not None
                    else None
                ),
                priority=order.priority,
                notes=order.notes,
                # 注入判定属于 `Guardrail_Layer`（任务 5.8）。seed 只提供原文，不代它
                # 下结论——否则 `EVAL-203` 会在一个已被标记的输入上通过，证明不了检测
                # 本身工作。
                injection_suspected=False,
                source=SEED_SOURCE,
                record_status="ACTIVE",
                import_batch_id=None,
                source_row_number=None,
                last_updated_at=anchor,
            )
        )

    return rows


# --------------------------------------------------------------------------
# 写入与清空
# --------------------------------------------------------------------------


def load_demo_data(session: Session, *, anchor: datetime = DEMO_ANCHOR) -> dict[str, int]:
    """把演示数据写进 `session`（**不提交**），返回逐表行数。

    不提交是刻意的：调用方决定事务边界。`reset_demo_data()` 需要把清空与写入放在同一个
    事务里，而一个自己 `commit()` 的函数会把那个事务切成两半。

    **逐层 flush**：根实体先落库，引用它们的实体才能通过外键检查
    （理由见 `build_row_tiers()`）。多次 flush 不会多推进版本号——钩子在一个事务里只插
    一行快照，后续 flush 只更新它的 fingerprint（`db/events.py`「每个事务恰好一行」）。
    """
    counts: dict[str, int] = {}
    for tier in build_row_tiers(anchor):
        session.add_all(tier)
        # 显式 flush：既满足外键顺序，也让 `input_snapshot_version` 的 `after_flush`
        # 钩子在本事务内落行，于是调用方紧接着读到的版本号就是这次 seed 推进后的值。
        session.flush()
        for row in tier:
            table_name = type(row).__tablename__
            counts[table_name] = counts.get(table_name, 0) + 1
    return dict(sorted(counts.items()))


def clear_business_tables(session: Session) -> None:
    """清空除 `audit_log` 之外的全部表，并把快照版本号的水位复位。

    `reversed(Base.metadata.sorted_tables)` 给出「被依赖者最后删」的顺序。
    `sorted_tables` 是 SQLAlchemy 按外键拓扑排出来的，因此这里不需要自己维护一份删除
    顺序清单——那份清单会在下一次加表时悄悄过期。

    schema 里唯一的外键环（`production_plans` ↔ `disruptions`）不构成问题：环的一侧
    刻意没有库级外键（`models.py` 模块 docstring），拓扑排序因此仍然成立。
    """
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in PRESERVED_TABLES:
            continue
        session.execute(delete(table))
    _restart_snapshot_sequence(session)


def _restart_snapshot_sequence(session: Session) -> None:
    """把 `input_snapshots.snapshot_version` 的自增水位复位到「下一个是 1」。

    `DELETE FROM input_snapshots` 只删行，不动水位：

    - **SQLite**——`AUTOINCREMENT` 的水位存在 `sqlite_sequence` 这张内部表里，删掉对应
      行即复位。这是全仓库为数不多的方言特定语句之一，因此加了存在性检查：
      `sqlite_sequence` 只在库里存在 `AUTOINCREMENT` 列时才被创建。
    - **PostgreSQL**——`ALTER SEQUENCE ... RESTART WITH 1`。序列名按 SERIAL 的默认命名
      规则拼出（`{表}_{列}_seq`）。
    - **其他方言**——不做处理。此时第 2 条不变量退化为「版本号单调」，冒烟测试的幂等
      断言仍然成立（它比对的是业务表内容），只有「从 1 开始」这一条不再保证。

    放在这里而不是 `models.py` 是因为它是**运维动作**而非 schema 的一部分：正常运行期
    永远不该复位一个单调计数器。
    """
    dialect = session.get_bind().dialect.name
    table_name = InputSnapshot.__tablename__

    if dialect == "sqlite":
        exists = session.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'")
        ).first()
        if exists is not None:
            session.execute(
                text("DELETE FROM sqlite_sequence WHERE name = :name"), {"name": table_name}
            )
        return

    if dialect == "postgresql":
        column = InputSnapshot.snapshot_version.key
        session.execute(text(f"ALTER SEQUENCE {table_name}_{column}_seq RESTART WITH 1"))


# --------------------------------------------------------------------------
# 一键重置
# --------------------------------------------------------------------------


def reset_demo_data(
    factory: sessionmaker[Session],
    *,
    actor: str = "PLANNER",
    anchor: datetime = DEMO_ANCHOR,
    trace_id: str | None = None,
) -> DemoResetReport:
    """清空业务表、重放 seed、补写一条 `DEMO_RESET` 审计（R28.8）。

    `snapshot_trigger("SEED")` 包住整个事务，因此那唯一一行 `input_snapshots` 的
    `trigger` 列记的是 `SEED` 而不是默认的 `MANUAL_EDIT`——重置后回看快照历史时，
    「这一格是重置铺出来的」与「这一格是有人改了数据」必须分得开。

    审计写在事务**之后**。两个理由：一是「重置成功了」这件事只有提交之后才为真；二是
    审计走独立连接，而 SQLite 同一时刻只允许一个写者，在业务事务持锁期间调用
    `append()` 会一路阻塞到 `busy_timeout`（`db/audit.py` 的「一个已知的 SQLite 约束」）。
    """
    with snapshot_trigger("SEED"), session_scope(factory) as session:
        clear_business_tables(session)
        row_counts = load_demo_data(session, anchor=anchor)
        version = current_input_snapshot_version(session)

    audit_id = audit.append(
        event_category=DEMO_RESET,
        event_type=DEMO_RESET_EVENT_TYPE,
        actor=actor,
        payload={
            "anchor": anchor.isoformat(),
            "input_snapshot_version": version,
            "row_counts": row_counts,
            "preserved_tables": sorted(PRESERVED_TABLES),
        },
        subject_type=_SUBJECT_TYPE,
        subject_id=_SUBJECT_ID,
        trace_id=trace_id,
    )

    return DemoResetReport(
        input_snapshot_version=version,
        row_counts=row_counts,
        anchor=anchor,
        audit_id=audit_id,
    )


def demo_data_present(session: Session) -> bool:
    """库里是否已有 seed 数据。`python -m app.seed --demo` 用它决定要不要动手。"""
    found = session.execute(
        select(Product.product_id).where(Product.source == SEED_SOURCE).limit(1)
    ).first()
    return found is not None


def seeded_table_names() -> Sequence[str]:
    """seed 会写入的表名，按字母序。供测试与运维核对覆盖面。"""
    return sorted({type(row).__tablename__ for row in build_rows()})
