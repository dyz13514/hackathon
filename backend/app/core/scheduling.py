"""确定性排产内核的时间线原语（任务 2.3，R4.4 / R4.5 / R4.6）。

design.md Components §3 引言把内核的性质写成一句话：纯 Python、输入是冻结的
`DomainSnapshot`、输出是值对象、无 ORM、无 I/O、无 `datetime.now()`。本模块是内核里最
底层的一块：主循环（任务 2.4）在其上放置作业，`Constraint_Validator`（任务 2.8）不复用它
（那是刻意的双实现，见 design.md §3.2）。

本模块只承担 design.md §3.1.3 与 §3.1.4 两节：

- `Timeline` —— 单台机器 / 单个工人的占用时间线，有序不重叠区间列表；
- `earliest_feasible_slot` —— 对齐机器与工人两条时间线求最早可行槽位（§3.1.3）；
- `changeover` —— 按 `specificity` 降序查换型规则（§3.1.4）；
- `processing_minutes` —— `ceil(base × qty ÷ rate_multiplier)`，`Decimal` 后 `math.ceil`。

`processing_minutes` 与 `available_at` 是 design.md §3.2 点名的**唯二**允许 `Scheduling_Core`
与 `Constraint_Validator` 共用的实现：它们是纯算术，两处各写一遍得到的不是互相校验而是同
一公式的两份抄写，抄错的概率高于校验的收益。

## 为什么 `is_feasible_slot` 的签名里没有 `preference_rules`

task 11.1 会用反射断言这个签名不含 `preference_rules`（也不含 `Constraint_Validator.validate`
的签名）。偏好是**软目标**（R7、R18）：它进 `Objective_Scorer` 的打分，绝不进可行性判定。
可行性判定一旦看得见偏好，一条「避免用 CNC-03」的偏好就可能让一个物理上完全可行的槽位被
判为不可行——那是把软约束偷偷升级成硬约束，且没有任何测试会因此变红（结果仍然「可行」，
只是少了一些本该出现的选项）。签名不含它，是把这条纪律钉死在类型层面。

## 时间以「分钟」而非 `timedelta` 传递时长

`duration` / `setup` 都是 `int` 分钟。`datetime + timedelta(minutes=n)` 是唯一与时钟相关的
运算，但它不读时钟——`n` 来自 `processing_minutes` 的纯算术，`test_kernel_time_purity.py`
扫描的是 `.now()` / `random()` 之类的调用，`timedelta` 不在其列。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.snapshot import ChangeoverRule, Machine

#: 全局兜底换型时间（design.md §3.1.4）。当 `changeover_rules` 里连全局默认（三个 ID
#: 都通配、`specificity = 1`）都没有时使用。取一个非零值：两个不同产品之间总有换型成本，
#: 缺规则时把它当成 0 会让排产器低估换型、排出车间实际做不到的紧凑计划。
DEFAULT_CHANGEOVER_MINUTES: int = 30


# --------------------------------------------------------------------------
# 加工时长与换型（纯算术，可与 Constraint_Validator 共用）
# --------------------------------------------------------------------------


def processing_minutes(
    base_processing_time_per_unit: Decimal,
    quantity: Decimal,
    rate_multiplier: Decimal,
) -> int:
    """加工时长 `ceil(base × qty ÷ rate_multiplier)`，整分钟（R4.5）。

    全程 `Decimal`，最后一步 `math.ceil`。**禁止浮点**：`base` 是分钟/件的小数，`quantity`
    可能是几百，`0.1 + 0.2 != 0.3` 这类浮点误差会在最后一位有效数字上让「同输入不同结果」
    偶发地发生，而属性 1（R5.7）恰恰断言两次运行逐字段相同。`math.ceil(Decimal)` 返回 `int`
    且不经过浮点，因此这条链路上没有一处 `float`。

    向上取整而非四舍五入：加工占用的机时不能少报，少报会让下一个作业以为机器提前空出来，
    从而排出重叠。
    """
    exact = base_processing_time_per_unit * quantity / rate_multiplier
    return math.ceil(exact)


def changeover(
    machine: Machine,
    from_product: str | None,
    to_product: str,
    rules: tuple[ChangeoverRule, ...],
) -> int:
    """机器 `machine` 上从 `from_product` 换到 `to_product` 的换型分钟数（R4.4）。

    `from_product is None`（机器上还没做过任何产品）或 `from_product == to_product`（相邻
    两作业同产品，无需换型）→ 0。

    否则按 design.md §3.1.4 的三级退化查 `changeover_rules`：

        精确匹配 (machine_id, from, to)  →  机器默认 (machine_id, *, *)  →  全局默认 (*, *, *)

    这三级恰好对应 `ChangeoverRule.specificity` 的 3 / 2 / 1。查表实现为「筛出全部匹配规则，
    按 `specificity` 降序取首条」而不是三次分别查找：`specificity` 是一个**可排序的整数**，
    把优先级编码进数据，查表因此不必在运行期推断「哪条更具体」。同 `specificity` 多条时按
    `rule_id` 取最小，保证确定性 tie-break（属性 1）。

    连全局默认都没有 → `DEFAULT_CHANGEOVER_MINUTES`。
    """
    if from_product is None or from_product == to_product:
        return 0

    matches = [
        rule
        for rule in rules
        if _rule_matches(rule, machine.machine_id, from_product, to_product)
    ]
    if not matches:
        return DEFAULT_CHANGEOVER_MINUTES

    best = min(matches, key=lambda rule: (-rule.specificity, rule.rule_id))
    return best.changeover_minutes


def _rule_matches(
    rule: ChangeoverRule,
    machine_id: str,
    from_product: str,
    to_product: str,
) -> bool:
    """规则的每个非 `None` 维度都要匹配；`None` 是通配，永远匹配。"""
    return (
        (rule.machine_id is None or rule.machine_id == machine_id)
        and (rule.from_product_id is None or rule.from_product_id == from_product)
        and (rule.to_product_id is None or rule.to_product_id == to_product)
    )


# --------------------------------------------------------------------------
# 时间线原语
# --------------------------------------------------------------------------


class Interval(BaseModel):
    """时间线上的一段占用 `[start, end)`，附带其产品（用于换型查表）。

    半开区间与 `TimeWindow` 一致：一段 09:00–10:00 的占用与一个 10:00 开始的作业不冲突。
    `product_id` 让 `product_immediately_before` 能回答「这台机器此刻正在做什么产品」，
    换型查表需要它。
    """

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    product_id: str

    @model_validator(mode="after")
    def _non_empty(self) -> Interval:
        if self.end <= self.start:
            raise ValueError(f"占用区间必须非空且方向正确：{self.start} → {self.end}")
        return self

    def overlaps(self, start: datetime, end: datetime) -> bool:
        """与 `[start, end)` 有非空交集。零长探测区间与任何占用都不相交。"""
        return self.start < end and start < self.end


class Slot(BaseModel):
    """`earliest_feasible_slot` 找到的一个可行槽位。

    `setup_minutes = op.setup_time + changeover`（R4.4）：作业自身的固定装夹时间加上因换产品
    产生的换型。`changeover_minutes` 单列，是它俩之差，供 `Objective_Scorer` 的换型分量与
    候选打分的 `W_CHANGEOVER` 项使用（design.md §3.1.2）。

    `[start, end)` 是含换型在内的整段占用：`end = start + setup_minutes + duration`。占用时间
    线时用这一整段，否则换型区间会被下一个作业当成空闲插进去。
    """

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    setup_minutes: int = Field(ge=0)
    changeover_minutes: int = Field(ge=0)


class Timeline:
    """单台机器 / 单个工人的占用时间线：有序不重叠区间列表（design.md §3.1.3）。

    **刻意可变**。它与快照的 `frozen=True` 纪律不冲突：快照是内核的**输入**，冻结它承载沙箱
    第 1 层隔离；`Timeline` 是主循环的**工作内存**，逐订单地 `occupy`，本就该可变。它不进
    快照、不跨调用存活、也不进任何 ORM，因此可变性在这里没有隔离含义。

    不变量（构造后恒成立，`occupy` / `free` 维护）：区间按 `start` 升序、两两不重叠。
    `earliest_feasible_slot` 的候选起点扫描依赖这个有序性——它取每个区间的结束点作为候选
    起点，若列表无序，剪枝（`e > hard_end` 后续只会更晚）就不成立。
    """

    def __init__(self) -> None:
        self._intervals: list[Interval] = []

    def __len__(self) -> int:
        return len(self._intervals)

    @property
    def intervals(self) -> tuple[Interval, ...]:
        """当前占用区间的只读快照，按 `start` 升序。"""
        return tuple(self._intervals)

    def occupy(self, start: datetime, end: datetime, product_id: str) -> None:
        """占用 `[start, end)`。与既有占用重叠即抛错——重叠是排产 bug，不该被静默吸收。

        主循环只在候选槽位通过 `free(s, e)` 检查后才 `occupy`，因此正常路径不会触发这个断言；
        它存在是为了让「主循环算错了空隙」在最近的地方炸掉，而不是留到 `Constraint_Validator`
        才发现一个 `MACHINE_DOUBLE_BOOKING`。
        """
        candidate = Interval(start=start, end=end, product_id=product_id)
        if any(existing.overlaps(start, end) for existing in self._intervals):
            raise ValueError(f"占用 [{start}, {end}) 与既有区间重叠")
        # 二分位置插入以维持升序，避免每次全量排序。
        index = self._bisect_start(start)
        self._intervals.insert(index, candidate)

    def free(self, start: datetime, end: datetime) -> bool:
        """`[start, end)` 完全空闲（不与任何占用相交）。"""
        return not any(existing.overlaps(start, end) for existing in self._intervals)

    def end_points(self) -> frozenset[datetime]:
        """全部占用区间的结束点，作为 `earliest_feasible_slot` 的候选起点之一。

        用 `frozenset`：候选起点集合是并集且去重，返回集合让调用方直接 `∪` 而不必再去重。
        """
        return frozenset(interval.end for interval in self._intervals)

    def product_immediately_before(self, t: datetime) -> str | None:
        """`t` 时刻这台机器上一个作业的产品，用于换型查表。

        取结束点 `<= t` 中最晚的那个区间的 `product_id`。相等（`end == t`，前一作业刚好在
        `t` 结束）也算「之前」——那正是要换型的典型场景。没有任何在 `t` 之前结束的区间 →
        `None`（机器还没做过东西，`changeover` 据此返回 0）。
        """
        candidate: Interval | None = None
        for interval in self._intervals:
            if interval.end <= t and (candidate is None or interval.end > candidate.end):
                candidate = interval
        return candidate.product_id if candidate is not None else None

    def first_occupied_after(self, e: datetime) -> Interval | None:
        """结束点 `e` 之后（含 `start == e`）最早开始的占用区间，没有则 `None`。

        插入空隙时用它取「后一个作业」，以便为那次换型也留出时间（§3.1.3）。区间已按 `start`
        升序，首个 `start >= e` 的即是。
        """
        for interval in self._intervals:
            if interval.start >= e:
                return interval
        return None

    def _bisect_start(self, start: datetime) -> int:
        """升序列表里 `start` 的插入位（区间不重叠，只需按 start 比较）。"""
        low, high = 0, len(self._intervals)
        while low < high:
            mid = (low + high) // 2
            if self._intervals[mid].start < start:
                low = mid + 1
            else:
                high = mid
        return low


# --------------------------------------------------------------------------
# 最早可行槽位（design.md §3.1.3）
# --------------------------------------------------------------------------


def earliest_feasible_slot(
    machine_tl: Timeline,
    worker_tl: Timeline,
    *,
    machine: Machine,
    to_product: str,
    setup_time: int,
    ready_at: datetime,
    duration: int,
    hard_end: datetime,
    changeover_rules: tuple[ChangeoverRule, ...],
) -> Slot | None:
    """对齐机器与工人两条时间线，求 `ready_at` 之后最早的可行槽位（§3.1.3）。

    候选起点扫描而非区间代数：候选起点 = `{ready_at} ∪ 机器占用结束点 ∪ 工人占用结束点`，
    升序遍历，第一个使机器与工人都空闲、且不违反前后换型的起点即为答案。演示规模下
    （§3.1.3 的估算：≈63 万次区间检查）纯 Python < 1 秒，满足 R27.3。

    每个起点 `t` 处的占用是 `[t, t + setup + duration)`，其中
    `setup = setup_time + changeover(machine 上 t 之前的产品 → to_product)`——换型取决于**这台
    机器在 `t` 之前做的是什么**，所以 `setup` 必须逐候选起点重算，不能提到循环外。

    剪枝：一旦某个 `t` 的 `end > hard_end`，更晚的起点只会更晚，直接返回 `None`（§3.1.3）。
    `hard_end` 由调用方取 `min(worker.shift_end, machine.available_end)`——跨越 `shift_end`
    的槽位在此被拒（R4.6），无需另设检查。

    插入到两个既有作业之间的空隙时，除了本作业的换型，还必须为**后一个作业**留出它的换型
    时间：本作业结束后若紧接着一个不同产品的作业，那次换型也要塞得进 `[end, nxt.start)`，
    否则「排得下」是假象。
    """
    starts = sorted({ready_at} | machine_tl.end_points() | worker_tl.end_points())

    for t in starts:
        if t < ready_at:
            continue

        prev_product = machine_tl.product_immediately_before(t)
        setup = setup_time + changeover(machine, prev_product, to_product, changeover_rules)
        start = t
        end = t + timedelta(minutes=setup + duration)

        if end > hard_end:
            return None  # 后续起点只会更晚，提前剪枝

        if not (machine_tl.free(start, end) and worker_tl.free(start, end)):
            continue

        nxt = machine_tl.first_occupied_after(end)
        if nxt is not None and nxt.product_id != to_product:
            follow_changeover = changeover(machine, to_product, nxt.product_id, changeover_rules)
            if end + timedelta(minutes=follow_changeover) > nxt.start:
                continue  # 空隙塞不下「本作业 + 给后一个作业的换型」

        return Slot(
            start=start,
            end=end,
            setup_minutes=setup,
            changeover_minutes=setup - setup_time,
        )

    # 防御性回退：签名允许返回 None，但结构上不可达。候选起点集合 = `{ready_at} ∪ 两条时间线
    # 的占用结束点`；任一候选若在 307/311 处 `continue`（被占用/换型塞不下），其阻挡区间的结束点
    # 必是一个更晚的候选——因此「最后一个候选」不可能 `continue`，只能在 304 处剪枝返回或在 316
    # 处落位。经 1920 组占用/时长/换型/时域组合的穷举，此行从未被执行（任务 2.13 覆盖核验）。
    return None  # pragma: no cover


def is_feasible_slot(
    machine_tl: Timeline,
    worker_tl: Timeline,
    *,
    start: datetime,
    end: datetime,
    hard_end: datetime,
) -> bool:
    """`[start, end)` 是否是一个可放置的槽位：两条时间线都空闲且不跨 `hard_end`。

    签名中**刻意不含 `preference_rules`**（task 11.1 反射断言）：可行性是硬约束，偏好是软
    目标，两者不能在同一个判定里混合——理由见模块 docstring。
    """
    if end > hard_end:
        return False
    return machine_tl.free(start, end) and worker_tl.free(start, end)
