"""`load_snapshot()`：把库里的规划实体读成一份冻结的 `DomainSnapshot`（任务 2.1）。

## 为什么这个函数不在 `app/core/` 里

它需要 `Session`，而 `tests/structure/test_layering.py` 的第 ① 条断言 `app/core/**` 不
import `sqlalchemy`、也不 import `app.db`。design.md §3.7 把这条讲得很直接：

> 沙箱执行的全部计算都发生在这一层里，所以「沙箱代码里根本拿不到会话」不是约定，而是包
> 依赖关系。

于是分工是：**纯模型与纯语义在 `core/snapshot.py`，唯一一次读库在这里。** 内核只看得见
入参，看不见库。

## 三条不变量

1. **单个只读事务。** 版本号与全部实体在同一个事务里读出，因此
   `snapshot.snapshot_version` 与快照内容必然同源。分两次读会得到「版本号说 v7、内容是
   v8」这种组合，而审批的陈旧检测（R12.2–3）恰好就是拿这个版本号去比对的——那时错的不是
   一次读取，是一次激活。
2. **排除 `record_status = REVERTED`。** 导入批次回滚是软删除（R3.4），被回滚的记录仍在
   表里。`load_snapshot` 是「哪些数据参与排产」这个问题的唯一答案点（design.md §4.2：
   「`REVERTED` 记录被 `load_snapshot` 的查询条件排除，因此不参与任何排产」）。
3. **`expunge_all()` 与 ORM identity map 解绑。** 读完即清空会话的身份映射，快照里因此
   没有任何一个对象还连着 `Session`。这是沙箱第 1 层隔离的另一半：`frozen=True` 让快照改
   不动，`expunge_all()` 让「就算改动了也没有对象能被 flush 回库」。

## 子表的过滤方式：跟着父实体走

`operations` / `product_materials` / `incoming_deliveries` / `machine_downtime` /
`worker_absences` / `changeover_rules` 都没有 `record_status` 列。它们的可见性由父实体决定
——父实体被回滚，子行随之不进快照。这比给每张子表加一列软删除标记更可靠：两个标记就有
不一致的可能，而「父在子在」没有中间状态。

被回滚的父实体若仍被**活着的**记录引用（例如一个 `ACTIVE` 订单指向已回滚的产品），那不是
过滤问题而是数据完整性问题，由 `require_referential_integrity()` 报 `DATA_INTEGRITY_ERROR`
（R5.6）。

## 加载期排序 vs 内核的确定性

本模块按 ID 排序读出，让「同一份库两次加载得到逐字段相同的快照」成立。但
`DomainSnapshot` 自己**不**排序（见 `core/snapshot.py` 模块 docstring）：属性 1 要打乱集合
顺序再跑一次排产，若快照会规范化顺序，那次打乱就成了空操作。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, cast

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.snapshot import (
    BomLine,
    ChangeoverRule,
    DomainSnapshot,
    DowntimeReason,
    DowntimeWindow,
    IncomingDelivery,
    Machine,
    MachineStatus,
    Material,
    Operation,
    Order,
    PreferenceRule,
    Priority,
    Product,
    TimeWindow,
    Worker,
    require_referential_integrity,
)
from app.db import models as orm
from app.db.repositories import current_input_snapshot_version

#: 参与排产的记录状态。`REVERTED` 是软删除（R3.4），见模块 docstring 第 2 条。
ACTIVE_RECORD_STATUS: Final = "ACTIVE"


# --------------------------------------------------------------------------
# 取值转换
#
# ORM 侧有几列声明为 `Mapped[object]`（`Numeric` 与 `JSON`），因此从行对象取出来的静态
# 类型是 `object`。下面三个函数是那道类型边界的唯一穿越点：转换集中在一处，mypy strict
# 才能在其余地方保持严格，而不是在每个字段上撒 `cast`。
# --------------------------------------------------------------------------


def _decimal(value: object) -> Decimal:
    """`Numeric` 列 → `Decimal`。

    SQLAlchemy 在 SQLite 上通常已经返回 `Decimal`，但 `int` / `float` / `str` 都可能出现
    （手工写入、其他方言、旧数据）。经 `str` 中转而不是 `Decimal(float)`：后者会把
    `0.1` 变成 `0.1000000000000000055511151231257827`，而这些数值会进入排产算术
    （design.md §3.1.4 禁止浮点）。
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(str(value))


def _string_tuple(value: object) -> tuple[str, ...]:
    """JSON 数组列 → `tuple[str, ...]`。`None` 与空数组都得到空元组。"""
    if value is None:
        return ()
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    raise TypeError(f"期望 JSON 数组，实际得到 {type(value).__name__}")


def _json_object(value: object) -> dict[str, Any]:
    """JSON 对象列 → `dict`。`structured_form` 的原样携带（任务 11.x 才解释它）。"""
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    raise TypeError(f"期望 JSON 对象，实际得到 {type(value).__name__}")


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------


def _active(stmt: Select[Any], column: Any) -> Select[Any]:
    """给带 `record_status` 的表加上「只要 `ACTIVE`」的条件。"""
    return stmt.where(column == ACTIVE_RECORD_STATUS)


def _rows(session: Session, stmt: Select[Any]) -> list[Any]:
    return list(session.execute(stmt).scalars().all())


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


def resolve_production_date(production_date: date | None, *, now: datetime) -> date:
    """把「请求未指定生产日」解析成确定的一天：缺省取 `now.date()`。

    这是全流程**唯一**一次由「现在」派生生产日的规则（R5.7 可重现性的入口条件，见
    `load_snapshot` docstring）。调用方若需要**提前**知道目标生产日——例如生成端点的
    `PENDING_PLAN_EXISTS` 前置检查——必须调用本函数，而不是各自再写一遍 `or now.date()`：
    两处口径一旦分叉，前置检查就会拿一个与流水线不同的日期去判断。请求体里
    `production_date` 缺省为 `None` 时，`column == None` 会渲染成 `IS NULL`，那正是
    「重复生成的前置检查恒不命中」的成因。
    """
    return production_date if production_date is not None else now.date()


def load_snapshot(
    session: Session,
    *,
    now: datetime,
    production_date: date | None = None,
    snapshot_version: int | None = None,
) -> DomainSnapshot:
    """读出一份通过引用完整性预检的 `DomainSnapshot`。

    `now` 是**必填关键字参数**，没有默认值。这是内核可重现性（R5.7）的入口条件：默认成
    `datetime.now()` 会让「当前时间」重新变成一个隐式输入，而调用方多半察觉不到——排产结果
    随之取决于运行时刻，属性 1 却仍然通过，因为它构造的入参没变。

    `production_date` 默认取 `now.date()`（规则封装在 `resolve_production_date()`，调用方若
    需要提前知道目标生产日应当复用它）。这是全流程里唯一一次由「现在」派生生产日，且它
    发生在内核之外。

    `snapshot_version` 默认在同一事务里读 `MAX(input_snapshots.snapshot_version)`。允许传入
    是为了沙箱与基线：design.md §3.4 要求基线与正式计划在**完全相同**的快照上运行，属性 37
    断言两者 `snapshot_version` 相等，而做到这一点最直接的方式是复用同一份快照对象。

    引用完整性预检在返回前执行，失败抛 `DataIntegrityError`（R5.6，载荷是全部悬空引用）。
    预检放在这里而不是留给调用方，是因为「未预检的快照」不该存在：design.md 的流水线序列里
    它紧跟 `load_snapshot` 之后，中间没有任何其他步骤。

    调用方持有事务边界：本函数不 `commit()` 也不 `rollback()`。
    """
    if session.new or session.deleted or session.dirty:
        # 未 flush 的改动对 SELECT 不可见（会话工厂 `autoflush=False`），于是快照会拿到
        # 旧行，而 `snapshot_version` 可能已被同事务里的钩子推进——两者不同源，正是第 1
        # 条不变量要防的那种组合。这不是可以放宽的边界情况：写到一半读快照，读出来的东西
        # 没有任何一个时刻与之对应。
        raise RuntimeError("load_snapshot 要求一个干净的会话：请先提交或回滚待写入的改动")

    version = (
        current_input_snapshot_version(session) if snapshot_version is None else snapshot_version
    )

    # ---- 单个只读事务：先读全部行，再一次性解绑 ----
    product_rows = _rows(
        session,
        _active(select(orm.Product), orm.Product.record_status).order_by(orm.Product.product_id),
    )
    material_rows = _rows(
        session,
        _active(select(orm.Material), orm.Material.record_status).order_by(
            orm.Material.material_id
        ),
    )
    machine_rows = _rows(
        session,
        _active(select(orm.Machine), orm.Machine.record_status).order_by(orm.Machine.machine_id),
    )
    worker_rows = _rows(
        session,
        _active(select(orm.Worker), orm.Worker.record_status).order_by(orm.Worker.worker_id),
    )
    order_rows = _rows(
        session, _active(select(orm.Order), orm.Order.record_status).order_by(orm.Order.order_id)
    )

    operation_rows = _rows(
        session,
        select(orm.Operation).order_by(orm.Operation.product_id, orm.Operation.sequence),
    )
    bom_rows = _rows(
        session,
        select(orm.ProductMaterial).order_by(
            orm.ProductMaterial.product_id, orm.ProductMaterial.material_id
        ),
    )
    delivery_rows = _rows(
        session,
        select(orm.IncomingDelivery).order_by(
            orm.IncomingDelivery.material_id,
            orm.IncomingDelivery.eta,
            orm.IncomingDelivery.delivery_id,
        ),
    )
    downtime_rows = _rows(
        session,
        select(orm.MachineDowntime).order_by(
            orm.MachineDowntime.machine_id,
            orm.MachineDowntime.start_time,
            orm.MachineDowntime.downtime_id,
        ),
    )
    absence_rows = _rows(
        session,
        select(orm.WorkerAbsence).order_by(
            orm.WorkerAbsence.worker_id,
            orm.WorkerAbsence.start_time,
            orm.WorkerAbsence.absence_id,
        ),
    )
    changeover_rows = _rows(
        session,
        # 查表按 `specificity` 降序取首条（design.md §3.1.4）：排序在加载期做完，内核里
        # 那次查表因此是一次线性扫描而不是一次排序。
        select(orm.ChangeoverRule).order_by(
            orm.ChangeoverRule.specificity.desc(), orm.ChangeoverRule.rule_id
        ),
    )
    preference_rows = _rows(
        session,
        select(orm.PreferenceRule)
        # 只有已启用的规则进快照（design.md 的 `DomainSnapshot`：「仅 enabled」）。未启用
        # 的候选规则连内核都进不去，因此 R18.4「只有显式人工确认能启用」不依赖内核自觉。
        .where(orm.PreferenceRule.enabled.is_(True))
        .order_by(orm.PreferenceRule.rule_id),
    )

    # 第 3 条不变量：读完即解绑。此后没有任何一个快照字段来自还连着会话的对象。
    session.expunge_all()

    active_machine_ids = {row.machine_id for row in machine_rows}

    snapshot = DomainSnapshot(
        snapshot_version=version,
        production_date=resolve_production_date(production_date, now=now),
        now=now,
        orders=tuple(_order(row) for row in order_rows),
        products=_products(product_rows, operation_rows, bom_rows),
        materials=_materials(material_rows, delivery_rows),
        machines=_machines(machine_rows, downtime_rows),
        workers=_workers(worker_rows, absence_rows),
        changeover_rules=tuple(
            _changeover_rule(row)
            for row in changeover_rows
            # 规则挂在已回滚的机器上即不适用（模块 docstring：「缺了它只是不适用的，过滤」）。
            # `machine_id is None` 是全局默认规则，永远保留。
            if row.machine_id is None or row.machine_id in active_machine_ids
        ),
        preference_rules=tuple(_preference_rule(row) for row in preference_rows),
    )
    return require_referential_integrity(snapshot)


def load_sandbox_snapshot(
    session: Session,
    *,
    now: datetime,
    production_date: date | None = None,
    snapshot_version: int | None = None,
) -> DomainSnapshot:
    """沙箱第 1 层隔离的入口（任务 8.1，design.md §3.7、ADR-009）。

    委派给 `load_snapshot`——两者读同一批行、走同一个「单只读事务 → `expunge_all()` →
    `frozen=True` 快照」的路径，因此**沙箱快照与正式快照逐字段同构**，没有第二套加载逻辑
    会漂移。分出这个命名入口是为了让沙箱调用点在代码里自解释：读到「`load_sandbox_snapshot`」
    就知道这份快照将喂给沙箱推演，而它天然满足 §3.7 第 1 层的两个条件——

    1. **没有 ORM 对象可写**：`load_snapshot` 读完即 `expunge_all()`，快照里的每个字段都是
       从行拷出的纯值，不再连着 `Session`；因此沙箱里根本不存在 `session.add(obj)` 能作用的
       对象（第 1 层「写入几乎不可达」）。
    2. **快照不可变**：`DomainSnapshot` 及其全部嵌套模型 `frozen=True`，赋值即抛
       `ValidationError`；沙箱的变体经 `snapshot.model_copy(deep=True, update=...)` 得到一份
       新的冻结副本，绝不就地改这一份。

    第 2 层（引擎级 DML 拦截）由 `app/db/sandbox_guard.py` 在 `sandbox_guard(...)` 语境内
    提供——它是「万一某次改动让沙箱路径上重新出现了真实会话」时的检测网。两层的分工见 ADR-009。

    参数语义与 `load_snapshot` 完全一致（`now` 必填、干净会话、引用完整性预检）。
    """
    return load_snapshot(
        session,
        now=now,
        production_date=production_date,
        snapshot_version=snapshot_version,
    )


# --------------------------------------------------------------------------
# 行 → 值对象
# --------------------------------------------------------------------------


def _order(row: orm.Order) -> Order:
    """订单行 → 值对象。**不带 `notes`**（不受信任文本不进内核，见 `core/snapshot.py`）。"""
    return Order(
        order_id=row.order_id,
        product_id=row.product_id,
        quantity=_decimal(row.quantity),
        due_date=row.due_date,
        promised_date=row.promised_date,
        # `cast` 只是把运行期校验的位置告诉 mypy：`Order.priority` 是 `Literal`，下一行的
        # 构造会真的校验它，取值域错了在这里就抛 `ValidationError`。
        priority=cast(Priority, row.priority),
    )


def _products(
    product_rows: list[Any],
    operation_rows: list[Any],
    bom_rows: list[Any],
) -> tuple[Product, ...]:
    """产品 + 工序 + BOM。子行按 `product_id` 分组，孤儿子行随父实体一起消失。"""
    operations: dict[str, list[Operation]] = defaultdict(list)
    for row in operation_rows:
        operations[row.product_id].append(
            Operation(
                sequence=row.sequence,
                required_machine_type=row.required_machine_type,
                required_capability=row.required_capability,
                required_worker_skill=row.required_worker_skill,
                base_processing_time_per_unit=_decimal(row.base_processing_time_per_unit),
                setup_time=row.setup_time,
            )
        )

    bom: dict[str, list[BomLine]] = defaultdict(list)
    for row in bom_rows:
        bom[row.product_id].append(
            BomLine(material_id=row.material_id, quantity_per_unit=_decimal(row.quantity_per_unit))
        )

    return tuple(
        Product(
            product_id=row.product_id,
            name=row.name,
            operations=tuple(operations[row.product_id]),
            bom=tuple(bom[row.product_id]),
        )
        for row in product_rows
    )


def _materials(material_rows: list[Any], delivery_rows: list[Any]) -> tuple[Material, ...]:
    """物料 + 在途到货。到货已按 `(material_id, eta, delivery_id)` 排序读出。"""
    deliveries: dict[str, list[IncomingDelivery]] = defaultdict(list)
    for row in delivery_rows:
        deliveries[row.material_id].append(
            IncomingDelivery(
                delivery_id=row.delivery_id,
                quantity=_decimal(row.quantity),
                eta=row.eta,
                confirmed=row.confirmed,
            )
        )

    return tuple(
        Material(
            material_id=row.material_id,
            name=row.name,
            unit=row.unit,
            quantity_available=_decimal(row.quantity_available),
            reserved_quantity=_decimal(row.reserved_quantity),
            incoming_deliveries=tuple(deliveries[row.material_id]),
        )
        for row in material_rows
    )


def _machines(machine_rows: list[Any], downtime_rows: list[Any]) -> tuple[Machine, ...]:
    """机器 + 停机窗。"""
    downtime: dict[str, list[DowntimeWindow]] = defaultdict(list)
    for row in downtime_rows:
        downtime[row.machine_id].append(
            DowntimeWindow(
                start=row.start_time,
                end=row.end_time,
                reason=cast(DowntimeReason, row.reason),
            )
        )

    return tuple(
        Machine(
            machine_id=row.machine_id,
            machine_type=row.machine_type,
            capabilities=_string_tuple(row.capabilities),
            status=cast(MachineStatus, row.status),
            available_start=row.available_start,
            available_end=row.available_end,
            rate_multiplier=_decimal(row.rate_multiplier),
            downtime_windows=tuple(downtime[row.machine_id]),
        )
        for row in machine_rows
    )


def _workers(worker_rows: list[Any], absence_rows: list[Any]) -> tuple[Worker, ...]:
    """工人 + 缺勤窗。"""
    absences: dict[str, list[TimeWindow]] = defaultdict(list)
    for row in absence_rows:
        absences[row.worker_id].append(TimeWindow(start=row.start_time, end=row.end_time))

    return tuple(
        Worker(
            worker_id=row.worker_id,
            name=row.name,
            skills=_string_tuple(row.skills),
            shift_start=row.shift_start,
            shift_end=row.shift_end,
            absences=tuple(absences[row.worker_id]),
        )
        for row in worker_rows
    )


def _changeover_rule(row: orm.ChangeoverRule) -> ChangeoverRule:
    return ChangeoverRule(
        rule_id=row.rule_id,
        machine_id=row.machine_id,
        from_product_id=row.from_product_id,
        to_product_id=row.to_product_id,
        changeover_minutes=row.changeover_minutes,
        specificity=row.specificity,
    )


def _preference_rule(row: orm.PreferenceRule) -> PreferenceRule:
    return PreferenceRule(
        rule_id=row.rule_id,
        human_text=row.human_text,
        structured_form=_json_object(row.structured_form),
    )
