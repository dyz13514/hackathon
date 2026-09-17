"""确定性内核的输入：冻结的 `DomainSnapshot`（任务 2.1）。

design.md Components §3 引言把内核的性质写成一句话：

> 内核是纯 Python，输入是**冻结的** `DomainSnapshot`，输出是值对象。无 ORM、无 I/O、
> 无 `datetime.now()`（当前时间由入参 `now` 显式传入），因此天然可重现（R5.7）。

本模块是那句话的前半句。它只依赖标准库与 pydantic —— `tests/structure/test_layering.py`
的第 ① 条断言 `app/core/**` 不 import `sqlalchemy` / `fastapi` / `app.db` / …，因此
**读库的 `load_snapshot()` 不在这里**，它在 `app/services/snapshot_loader.py`：那一侧需要
`Session`，而内核拿不到 `Session` 是包依赖事实而非约定（design.md §3.7）。

## `frozen=True` 承载的是沙箱第 1 层隔离

design.md §3.7 的两层隔离里，第 1 层是「冻结的内存副本」：

- 全部模型 `frozen=True`，赋值即抛 `pydantic.ValidationError`；
- 集合字段一律 `tuple` 而非 `list`，因此连 `snapshot.orders.append(...)` 这种绕过属性
  赋值的写法也不存在；
- 变体只能经 `snapshot.model_copy(deep=True, update=...)` 得到，原件不受影响。

任务 8.1 的沙箱写入阻断证明测试直接依赖这三条。**它们不是风格选择**：沙箱推演全部发生
在内核里，而内核里既没有 ORM 对象也没有可变状态，于是「沙箱改不了库」在语言层面成立，
不需要任何人在 review 时记得这件事。

## `now` 为什么必须是字段而不是调用

`now` 是 `DomainSnapshot` 的一个普通字段，由调用方显式传入。内核里任何一处
`datetime.now()` 都会让「同一输入两次运行产出相同结果」（R5.7、属性 1）失效——那个函数的
输出从此依赖一个不在入参里的东西，而属性测试构造的输入没变，所以它**仍然会通过**，只是
不再证明任何事情。`tests/structure/test_kernel_time_purity.py` 用 AST 扫描把这条钉住。

## 模型刻意**不**对集合排序

`load_snapshot()` 按 ID 排序读出，那是加载侧的可重现性。但模型本身不排序：属性 1 的第二
个断言是「打乱输入集合元素顺序后运行，结果逐字段相同」，若 `DomainSnapshot` 在校验期就把
元组重排成规范序，那次打乱会变成空操作，断言随即变得空洞。**确定性必须由排产器的全序
tie-break 提供，不能由输入的规范化伪造。**

## 与 ORM 模型的字段差异（都是刻意的）

- **不带 `Order.notes`**。它是不受信任文本（R23.1，任务 5.8 的 `UNTRUSTED_SOURCES`），
  而内核对它没有任何用途。不进内核，「未包裹的不受信任文本被拼进提示词」这条风险在快照
  这一层就已经不可能——解释载荷的构造者拿不到它。
- **不带 `record_status` / `source` / `import_batch_id`**。快照里只有 `ACTIVE` 记录
  （`REVERTED` 在加载期就被排除），保留一个恒等于 `"ACTIVE"` 的字段只会诱使内核去判断它。
- **`weights` 与 `active_plan` 尚未加入**。前者属 `Objective_Scorer`（任务 2.10），后者的
  类型是 `PlanCandidate`（任务 2.4 定义）。两者都是加字段的向后兼容改动，此刻先猜一个形状
  反而会在那两个任务里被推翻。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 订单优先级。取值域与 `db/models.ORDER_PRIORITIES` 一致，但在此重新声明为 `Literal`
#: ——内核不能 import `app.db`，而快照边界是这些取值最后一次可被校验的地方。
Priority = Literal["URGENT", "HIGH", "NORMAL", "LOW"]

#: 机器状态。`DOWN` / `MAINTENANCE` 使机器不进候选集（design.md §3.1.2）。
MachineStatus = Literal["AVAILABLE", "BUSY", "DOWN", "MAINTENANCE"]

#: 停机原因。与 `db/models.DOWNTIME_REASONS` 一致。
DowntimeReason = Literal["BREAKDOWN", "MAINTENANCE"]

#: 一道工序的 `sequence` 上界（R4.1，DDL 的 `CHECK (sequence BETWEEN 1 AND 3)`）。
MAX_OPERATION_SEQUENCE: Final = 3


class _Frozen(BaseModel):
    """全部快照模型的基类：冻结 + 拒绝未声明字段。

    `extra="forbid"` 与 `frozen=True` 是一对：只冻结属性赋值而放行未知字段，会让
    「快照里有什么」取决于调用方传了什么，加载器的一个笔误就能把一个多余字段带进内核。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------
# 时间窗
# --------------------------------------------------------------------------


class TimeWindow(_Frozen):
    """半开区间 `[start, end)`。机器停机与工人缺勤共用。

    半开而非闭区间：一个 12:00–16:00 的保养窗与一个 16:00 开始的作业**不**冲突。闭区间
    会让两者在端点上互斥，从而在排产结果里凭空多出一分钟的间隙——而那一分钟会随作业时长
    传播到整条时间线。
    """

    start: datetime
    end: datetime

    def contains(self, t: datetime) -> bool:
        """`t` 落在窗内（含左端、不含右端）。"""
        return self.start <= t < self.end

    def overlaps(self, start: datetime, end: datetime) -> bool:
        """与 `[start, end)` 有非空交集。零长区间与任何窗都不相交。"""
        return self.start < end and start < self.end


class DowntimeWindow(TimeWindow):
    """机器停机窗。`reason` 只进解释与量化建议，不参与可行性判定。"""

    reason: DowntimeReason


# --------------------------------------------------------------------------
# 产品与工序
# --------------------------------------------------------------------------


class Operation(_Frozen):
    """一道工序（R4.1）。`sequence` ∈ 1..3，线性链，无返工。

    `base_processing_time_per_unit` 是 `Decimal`：design.md §3.1.4 的
    `ceil(base × qty ÷ rate_multiplier)` 禁止浮点，否则「同输入不同结果」会在最后一位有效
    数字上偶发（R5.7）。
    """

    sequence: int = Field(ge=1, le=MAX_OPERATION_SEQUENCE)
    required_machine_type: str
    #: 可空。非空时须 ⊆ `machine.capabilities`（design.md §3.1.2）。
    required_capability: str | None
    required_worker_skill: str
    #: 分钟/件。
    base_processing_time_per_unit: Decimal = Field(gt=0)
    #: 分钟。与 `changeover` 相加得 `ScheduledJob.setup_minutes`（R4.4）。
    setup_time: int = Field(ge=0)


class BomLine(_Frozen):
    """单层 BOM 的一行（R4 限定，无多层展开）。"""

    material_id: str
    quantity_per_unit: Decimal = Field(gt=0)


class Product(_Frozen):
    """产品及其工艺路线与 BOM。

    `description` 是不受信任文本（R23.1），因此**不在**本模型里——理由同 `Order.notes`，
    见模块 docstring。
    """

    product_id: str
    name: str
    operations: tuple[Operation, ...]
    bom: tuple[BomLine, ...]

    def operations_in_sequence(self) -> tuple[Operation, ...]:
        """按 `sequence` 升序的工序。工序展开（任务 2.4）的输入。

        排序在这里而不在加载器里：`sequence` 是工艺语义上的全序，任何顺序的输入都表示
        同一条路线。这与「不对集合排序」那条纪律不冲突——属性 1 打乱的是**集合元素顺序**
        这种无语义的排列，而工序顺序有语义。
        """
        return tuple(sorted(self.operations, key=lambda op: op.sequence))


# --------------------------------------------------------------------------
# 物料
# --------------------------------------------------------------------------


class IncomingDelivery(_Frozen):
    """在途到货（R6.3）。`confirmed = False` 的 ETA 进入解释的假设清单（R10.4）。"""

    delivery_id: str
    quantity: Decimal = Field(gt=0)
    eta: datetime
    confirmed: bool


class Material(_Frozen):
    """物料。可用量语义见 `available_at()`——**唯一**允许排产器与校验器共用的那个函数。"""

    material_id: str
    name: str
    unit: str
    quantity_available: Decimal
    reserved_quantity: Decimal
    incoming_deliveries: tuple[IncomingDelivery, ...]


def available_at(material: Material, t: datetime) -> Decimal:
    """`t` 时刻可用的物料量（design.md §3.1.5、R6.3）。

        available_at(m, t) = quantity_available − reserved_quantity
                             + Σ{ d.quantity | d.eta < t }

    ## 为什么这一个函数被两个模块共用

    design.md §3.2 明确：这是 `Scheduling_Core` 与 `Constraint_Validator` **唯一**允许共用
    的实现（另一个是纯算术的 `processing_minutes`）。校验器与排产器刻意独立实现，好让
    「排产器写错了」被校验器抓到；但物料可用量是个**语义**而非算法——两处各写一遍，得到的
    不是互相校验，而是两种对 R6.3 的解释，且它们的分歧只会在缺料的边界上暴露，也就是演示
    里最要紧的那一刻。

    ## `eta < t` 而不是 `eta <= t`

    `t` 是作业的 `start_time`。到货与开工同一时刻，物料不算已到——车间里没人能在货车进门
    的同一分钟开始加工。这条严格不等号在两侧必须一致，否则校验器会否掉排产器刚排出的、
    刚好卡在到货时刻上的作业。

    返回值可能为负（预留超过库存），调用方据此报缺口，**不做任何补足**（R6.4）。
    """
    on_hand = material.quantity_available - material.reserved_quantity
    arrived = sum(
        (delivery.quantity for delivery in material.incoming_deliveries if delivery.eta < t),
        Decimal("0"),
    )
    return on_hand + arrived


# --------------------------------------------------------------------------
# 机器与工人
# --------------------------------------------------------------------------


class Machine(_Frozen):
    """机器。`rate_multiplier > 0` 由 DDL 与此处双重保证（除零会让工期变成无穷）。"""

    machine_id: str
    machine_type: str
    capabilities: tuple[str, ...]
    status: MachineStatus
    available_start: datetime
    available_end: datetime
    rate_multiplier: Decimal = Field(gt=0)
    downtime_windows: tuple[DowntimeWindow, ...]

    def has_capability(self, capability: str | None) -> bool:
        """`required_capability ⊆ capabilities`。`None` 表示工序无能力要求。"""
        return capability is None or capability in self.capabilities

    def is_blocked_during(self, start: datetime, end: datetime) -> bool:
        """`[start, end)` 与任一停机窗相交。"""
        return any(window.overlaps(start, end) for window in self.downtime_windows)


class ChangeoverRule(_Frozen):
    """换型规则（R4.4）。`machine_id` / `from_product_id` / `to_product_id` 为 `None` 即通配。

    `specificity` 3=精确 / 2=机器默认 / 1=全局。查表按它降序取首条（design.md §3.1.4），
    因此优先级是一个可排序的整数而不是运行期的「最长匹配」推断。

    规则挂在快照顶层而不是 `Machine` 上：全局默认规则（`machine_id is None`）没有归属的
    机器，塞进任一台都是错的。design.md 的 `DomainSnapshot` 注释写的是「machines 含
    changeover 配置」，这里按可表达性调整为顶层字段。
    """

    rule_id: str
    machine_id: str | None
    from_product_id: str | None
    to_product_id: str | None
    changeover_minutes: int = Field(ge=0)
    specificity: int = Field(ge=1, le=3)


class Worker(_Frozen):
    """工人。单班次建模（Open Question 1）：`[shift_start, shift_end)` 即全部可用时间。"""

    worker_id: str
    name: str
    skills: tuple[str, ...]
    shift_start: datetime
    shift_end: datetime
    absences: tuple[TimeWindow, ...]

    def has_skill(self, skill: str) -> bool:
        return skill in self.skills

    def is_absent_during(self, start: datetime, end: datetime) -> bool:
        """`[start, end)` 与任一缺勤窗相交。"""
        return any(window.overlaps(start, end) for window in self.absences)


# --------------------------------------------------------------------------
# 订单与偏好
# --------------------------------------------------------------------------


class Order(_Frozen):
    """订单。

    `due_date` 与 `promised_date` 分立（Open Question 2）：前者是客户期望日，后者是对外
    承诺日，`None` 表示尚未承诺。R13 的 `IMPACT_MINOR` 判据「不改变任何订单的
    `promised_date`」因此对未承诺订单不成立，这是刻意的语义而非缺省。
    """

    order_id: str
    product_id: str
    quantity: Decimal = Field(gt=0)
    due_date: datetime
    promised_date: datetime | None
    priority: Priority


class PreferenceRule(_Frozen):
    """已启用的偏好规则（R18.5）。快照里**只有** `enabled = True` 的规则。

    `structured_form` 的四类判别联合在任务 11.x 定义，此刻按 JSON 原样携带：内核在任务
    2.4 / 2.10 之前不解释它（`preference_delta` 先留桩返回 0）。此处不提前建模，是为了
    避免用一个猜出来的形状去约束那两个任务。

    这也是快照里唯一一个可变容器（`dict`）。它不破坏沙箱隔离——第 1 层的实质是「内核里
    没有 ORM 对象可写」，而不是深度不可变；但也因此**内核不得改写它**。
    """

    rule_id: str
    human_text: str
    structured_form: dict[str, Any]


# --------------------------------------------------------------------------
# 快照
# --------------------------------------------------------------------------


class DomainSnapshot(_Frozen):
    """一次规划的全部输入（design.md Components §3 引言）。

    `snapshot_version` 是 `input_snapshots` 的当前版本号：审批时的陈旧检测（R12.2–3）比对
    的就是它，因此它必须与快照内容同源同时刻——由 `load_snapshot()` 在同一个只读事务里读出。
    """

    snapshot_version: int = Field(ge=0)
    #: 排产的目标生产日。
    production_date: date
    #: 「现在」。由入参显式传入，见模块 docstring。
    now: datetime
    orders: tuple[Order, ...]
    products: tuple[Product, ...]
    materials: tuple[Material, ...]
    machines: tuple[Machine, ...]
    workers: tuple[Worker, ...]
    changeover_rules: tuple[ChangeoverRule, ...]
    preference_rules: tuple[PreferenceRule, ...]

    # ----------------------------------------------------------------------
    # 索引。**刻意不用 `cached_property`。**
    #
    # 缓存值住在实例 `__dict__` 里，而 `model_copy(deep=True, update=...)` 会把 `__dict__`
    # 整个拷过去再覆盖被 update 的字段——于是变体带着一份**指向旧集合**的索引。沙箱得到
    # 情景变体的唯一方式正是那个调用（design.md §3.7），所以这不是边缘情况：
    # 「假如 CNC-01 停机」的推演会拿着缓存里那台没停机的 CNC-01 算下去，而且不会报错。
    #
    # 每次重建是 O(n)，n 在演示规模上是几十。调用方（任务 2.4 的主循环）把它提到循环外
    # 取一次即可——那是个局部变量的事，而陈旧索引是个查不出来的错误结果。
    # ----------------------------------------------------------------------

    def products_by_id(self) -> dict[str, Product]:
        return {product.product_id: product for product in self.products}

    def materials_by_id(self) -> dict[str, Material]:
        return {material.material_id: material for material in self.materials}

    def machines_by_id(self) -> dict[str, Machine]:
        return {machine.machine_id: machine for machine in self.machines}

    def workers_by_id(self) -> dict[str, Worker]:
        return {worker.worker_id: worker for worker in self.workers}

    def orders_by_id(self) -> dict[str, Order]:
        return {order.order_id: order for order in self.orders}


# --------------------------------------------------------------------------
# 引用完整性预检（R5.6）
# --------------------------------------------------------------------------


class DanglingReference(_Frozen):
    """一处悬空引用。字段足以让规划员定位到具体的行与列，不需要再查日志。"""

    entity_type: str
    entity_id: str
    field: str
    missing_type: str
    missing_id: str

    def human_description(self) -> str:
        return (
            f"{self.entity_type} {self.entity_id} 的 {self.field} 指向了不存在的 "
            f"{self.missing_type} {self.missing_id}"
        )


class DataIntegrityError(Exception):
    """引用完整性预检失败（R5.6）。载荷是**全部**悬空引用，不是第一个。

    一次列全而非快速失败：这类错误的成因通常是一次导入或一次批次回滚
    （`record_status = REVERTED` 的记录被 `load_snapshot()` 排除，而引用它的记录还在），
    因此坏引用总是成批出现。逐个报会让规划员修一处、跑一次、再修一处。

    `code` 是字符串常量而不是 `api.errors.ErrorCode` 成员：内核不能 import `fastapi`
    （任务 1.8 断言 ①）。API 边界的映射随 `POST /api/plans/generate`（任务 2.12）落地，
    届时 `ErrorCode` 追加同名成员——`errors.py` 的约定是「枚举成员数恒等于系统当前真实
    能返回的错误种类数」，此刻还没有任何端点会返回它。
    """

    code: ClassVar[str] = "DATA_INTEGRITY_ERROR"

    def __init__(self, references: tuple[DanglingReference, ...]) -> None:
        self.references = references
        super().__init__(
            f"输入数据存在 {len(references)} 处引用完整性错误："
            + "；".join(reference.human_description() for reference in references)
        )

    def details(self) -> dict[str, Any]:
        """错误响应的 `details` 部分（design.md Error Handling §2 的统一形状）。"""
        return {
            "bad_references": [reference.model_dump(mode="json") for reference in self.references]
        }


def check_referential_integrity(snapshot: DomainSnapshot) -> tuple[DanglingReference, ...]:
    """快照内的引用是否闭合（R5.6）。纯函数，返回按 ID 排序的全部悬空引用。

    ## 检查的恰好是 R5.6 逐字列出的两类

    R5.6：「Order 引用了不存在的 Product、Product 引用了不存在的 Material」。这两类的共同
    点是**排产无法进行**：前者无法展开工序（R4.2），后者无法算出物料需求（R6.3）。

    其余引用（停机窗 → 机器、缺勤 → 工人、到货 → 物料、换型规则 → 机器/产品）不在此列，
    因为它们在加载期就被父实体的存在性过滤掉了：一个指向已回滚机器的停机窗，随那台机器一起
    不进快照即可，让它变成一次 `DATA_INTEGRITY_ERROR` 会把「一条配置过期」升级成「今天排不
    出计划」。取舍的界线是：**缺了它排不出来的，报错；缺了它只是不适用的，过滤。**

    排序键是 `(entity_type, entity_id, field, missing_id)`：错误清单会进 API 响应与审计，
    顺序必须与输入的元组顺序无关。
    """
    known_products = snapshot.products_by_id()
    known_materials = snapshot.materials_by_id()
    found: list[DanglingReference] = []

    for order in snapshot.orders:
        if order.product_id not in known_products:
            found.append(
                DanglingReference(
                    entity_type="Order",
                    entity_id=order.order_id,
                    field="product_id",
                    missing_type="Product",
                    missing_id=order.product_id,
                )
            )

    for product in snapshot.products:
        for line in product.bom:
            if line.material_id not in known_materials:
                found.append(
                    DanglingReference(
                        entity_type="Product",
                        entity_id=product.product_id,
                        field="bom.material_id",
                        missing_type="Material",
                        missing_id=line.material_id,
                    )
                )

    return tuple(
        sorted(
            found,
            key=lambda ref: (ref.entity_type, ref.entity_id, ref.field, ref.missing_id),
        )
    )


def require_referential_integrity(snapshot: DomainSnapshot) -> DomainSnapshot:
    """预检通过则原样返回快照，否则抛 `DataIntegrityError`（R5.6）。

    返回快照而不是 `None`，好让调用点写成 `snap = require_referential_integrity(load(...))`
    ——「未预检的快照被送进排产器」因此需要**刻意**跳过一个函数，而不是忘记调用一个函数。
    """
    references = check_referential_integrity(snapshot)
    if references:
        raise DataIntegrityError(references)
    return snapshot
