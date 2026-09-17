# Feature: production-planning-agent, Property 4: 对于任意 DomainSnapshot，
# 全部 ProductionJob 恰好被划分为 scheduled_jobs 与 unschedulable_jobs 两个
# 不相交且并集为全集的子集，且每个 unschedulable_job 均带量化解锁建议
"""Property 4：作业划分完备且不可排产项均被量化（任务 2.7，R8.1–R8.4；design.md
Correctness Properties「Property 4」）。

*For any* `DomainSnapshot`，`Scheduling_Core.generate_schedule` 产出的 `PlanCandidate` 满足：

1. **划分完备**：把该快照全部 `Order` 经 `expand` 展开出的 `ProductionJob` 集合记为 `U`。
   `scheduled_jobs` 与 `unschedulable_jobs` 的 `job_id` 两集合**不相交**（∩ = ∅）且**并集为
   全集**（∪ = `U`）——没有任何作业被静默丢弃，也没有任何作业被同时排上又标为不可排产。
2. **`feasibility` 与划分一致**：`unschedulable` 为空 → `FEASIBLE`；`scheduled` 为空 →
   `NO_FEASIBLE_PLAN`；两者都非空 → `PARTIAL`。
3. **每个 `unschedulable_job` 均被量化**：`blocking_reason` 属于 R6.1 的 **9 类**之一
   （R8.2）；解锁条件是可量化的、而非一句空泛的「排不上」（R8.3）。这条「可量化」的机器
   判定按类别分档（见下文 Option-A 决策）——两个引用型类别断言引用字段存在，其余 7 类断言
   至少含一个数值型量化字段。

## Option-A：量化字段规则按类别分档（design.md §3.1.6）

原始断言要求 R6.1 的**全部 9 类**在 `unblock_suggestion` 里都含至少一个数值型量化字段。这与
design.md §3.1.6 的逐类量化字段表不符：表里有**两类**失败按设计只携带**引用/标识型**字段、
不含任何数值叶子，因此这条「全类都要数值叶子」的规则对它们是伪命题。用户决策 Option A 就是
把数值字段规则**收窄到 §3.1.6 里确实产出数值量的类别**，对这两个引用型类别改断言其引用字段
存在（指向根因），而不强求数值叶子。

**两个引用型类别（§3.1.6，无数值叶子）：**

- `OPERATION_PRECEDENCE_VIOLATION` —— **转指**类别，输出 `{predecessor_job_id,
  predecessor_blocking_reason}`：它自身不携带数值叶子，而是把量化责任转指给**前序根因**那道
  工序，后者才携带自己的数值型量化字段。对这道后续工序而言，「它排不上」的可量化解锁条件就是
  「让前序排上」，数值细节落在前序自身的 `unblock_suggestion` 上；在这里强行要求数值叶子既不
  符合 §3.1.6 的输出形状，也会掩盖「转指」这一真实语义。断言 `predecessor_job_id` 与
  `predecessor_blocking_reason` 两个转指字段均存在且非 `None`。
- `MACHINE_CAPABILITY_MISMATCH` —— **能力匹配**类别，输出 `{required_capability,
  qualifying_machine_types}`：这里的解锁条件是「需要哪种能力、哪些机型具备它」，本质是引用
  性的定位信息而非「补多少料 / 加几分钟」的数量。`qualifying_machine_types` 在无任何机器具备
  该能力时为**空列表**（scheduler.py `_machine_types_with_capability`：诚实地说「当前没有机型
  具备该能力」而非虚构一个，R8.7），因此这一类恒无数值叶子。断言 `required_capability` 存在
  （非 `None`）且 `qualifying_machine_types` 键存在（允许空列表——空正是「一台都没有」的量化
  事实）。

**其余 7 类**：保持原有要求，`unblock_suggestion` 至少含**一个数值型量化字段**（R8.3），如
`shortfall_quantity` / `minutes_needed` / `worker_minutes_needed` / `deficit_minutes` 等。

## 这条属性守的是什么：作业被静默丢弃

design.md「Property 4」把它定位成**无便宜替代探测器**的一条硬属性：一个排产器最隐蔽也最
危险的失败模式，是把某道排不上的工序**悄悄扔掉**——既不排上、也不进 `unschedulable_jobs`，
于是计划看起来「可行」，车间却少了一道工序。这种缺陷不会在「可行计划的约束校验」（属性 2）
里暴露，因为被丢的作业根本不在校验范围内。唯一能抓住它的，是把展开出的全集与两个输出子集
逐一对齐、断言划分完备。因此这条属性**非可选**（tasks.md 任务 2.7）。

第 3 条（量化）守的是另一半：即便作业没被丢，若 `unblock_suggestion` 是空的或纯文字的，
规划员也无法据此行动（该补多少料、加几分钟机时）。R8.3 要求「至少一项可量化的解锁条件」，
本测试把它落成「至少一个数值型叶子」的机器可判定断言。

## 三档 scarcity 都要跑到

`domain_snapshots` 的 `scarcity` 调节物料丰俭，直接决定走到划分的哪一侧（tests/generators.py）：

- `ABUNDANT`：物料从不成为瓶颈，可行侧密集——多数快照 `FEASIBLE`，验证「全排上时
  unschedulable 恒空且 feasibility=FEASIBLE」。
- `TIGHT`：缺料时有时无，`PARTIAL` 与两侧非空的划分被稳定触发——这是第 2 条一致性最吃紧的
  分支。
- `INFEASIBLE`：需要物料的作业必然缺料，`NO_FEASIBLE_PLAN` / `PARTIAL` 被稳定触发——验证
  「全排不上时 scheduled 恒空、每个作业带量化 blocking_reason」。

用 `st.sampled_from` 把三档都喂进同一个测试，让 Hypothesis 在三种输入空间里各自收缩，比只
测默认的 `TIGHT` 覆盖到更多划分分支。

## 不比较排产决策本身，只比较「划分」与「量化形状」

本属性**不**断言哪个作业排在哪台机器（那是属性 2 与分支覆盖单元测试的职责），只断言集合
层面的划分完备与每个不可排产项的量化形状。这样它与属性 1（确定性）、属性 2（约束满足）
正交，各守一面。

## 不碰数据库、不碰 LLM

输入是 `domain_snapshots` 产出的**内存**快照；`generate_schedule` 是内核纯函数，不读库、
不触达 LLM。`conftest.py` 已把测试期 `LLM_MODE` 强制为 `STUB`，本路径根本不调用 LLM，故
额度纪律在此平凡满足（tests/generators.py 模块 docstring、Testing Strategy §2 第 5 条）。
"""

from __future__ import annotations

from decimal import Decimal
from numbers import Number

from app.core.scheduler import PlanCandidate, expand, generate_schedule
from app.core.snapshot import DomainSnapshot
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.generators import Scarcity, domain_snapshots

# R6.1 的 9 类硬约束 = `unschedulable_job.blocking_reason` 的合法取值全集（design.md §3.1.6、
# requirements.md R6.1）。初始生成路径（无 locked 集）只会产出其中 7 类，但属性断言的是
# **membership ∈ 9 类**——这是有意的上界断言：任何未来落在 9 类之外的 reason 都应被抓出。
BLOCKING_REASONS: frozenset[str] = frozenset(
    {
        "MATERIAL_INSUFFICIENT",
        "MACHINE_UNAVAILABLE",
        "MACHINE_CAPABILITY_MISMATCH",
        "WORKER_UNAVAILABLE",
        "WORKER_SKILL_MISMATCH",
        "MACHINE_DOUBLE_BOOKING",
        "WORKER_DOUBLE_BOOKING",
        "OPERATION_PRECEDENCE_VIOLATION",
        "SHIFT_BOUNDARY_VIOLATION",
    }
)

# 属性 4 与其余五条 `domain_snapshots` 消费者一致，用 `max_examples=100`（design.md
# Testing Strategy §2 第 3 条：只有属性 1 提高到 300）。快照构造成本略高，放宽 deadline 并
# 抑制「过慢」健康检查——覆盖三档 scarcity 会让部分 example 偏慢，慢不是错误。
_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# 三档 scarcity 都要喂进属性，让划分的两侧（全排上 / 全排不上 / 部分）都被稳定触发。
_SCARCITIES: tuple[Scarcity, ...] = ("ABUNDANT", "TIGHT", "INFEASIBLE")


def _all_expanded_job_ids(snapshot: DomainSnapshot) -> frozenset[str]:
    """快照全部 `Order` 经 `expand` 展开出的 `ProductionJob` 的 `job_id` 全集 `U`。

    这是划分完备的**参照全集**：`generate_schedule` 内部对同一批订单调用同一个 `expand`
    （每 Order 按 sequence 升序展开 1–3 道工序，`job_id = "{order_id}-OP{sequence}"`），因此
    这里独立地重算一遍全集，再与两个输出子集对齐——若排产器少展开了某道工序或多凭空造出
    一个 job_id，与本全集的比对就会失败。

    `expand` 可能因非法路线抛 `InvalidRoutingError`，但 `domain_snapshots` 只产出合法路线
    （1–3 道、sequence 连续无重复，见 tests/generators.py），故此处不会抛。
    """
    products = snapshot.products_by_id()
    job_ids: set[str] = set()
    for order in snapshot.orders:
        product = products[order.product_id]  # 引用完整性由生成器保证
        for job in expand(order, product):
            job_ids.add(job.job_id)
    return frozenset(job_ids)


def _has_numeric_leaf(suggestion: dict[str, object]) -> bool:
    """`unblock_suggestion` 是否至少含一个**数值型**量化叶子（R8.3）。

    「数值型」的判定：递归遍历 dict / list 的全部叶子，只要出现一个 `int` / `float` /
    `Decimal`（`bool` 除外——它虽是 `int` 子类，但 True/False 不是量化条件），或一个能被
    `Decimal(...)` 解析的数字字符串（`quantify` 把 `shortfall_quantity` 渲染为 `str(Decimal)`，
    见 scheduler.py），即判为含数值叶子。

    偏宽松地把「数字字符串」也算进来是有意的：R8.3 要的是「可量化」，而 `quantify` 对物料
    缺口用的正是字符串形式的 Decimal（避免 JSON 里的浮点漂移）。若只认原生数字类型，会把
    一个合法的量化建议误判为不达标。标识符字符串（如 `MAT-000` / `CNC`）不会被 `Decimal`
    解析，因此不会造成误报。
    """

    def is_numeric(value: object) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, Number):  # int / float / Decimal 均是 numbers.Number 的实例
            return True
        if isinstance(value, str):
            try:
                Decimal(value)
            except (ArithmeticError, ValueError):
                return False
            return True
        return False

    def walk(node: object) -> bool:
        if isinstance(node, dict):
            return any(walk(v) for v in node.values())
        if isinstance(node, list | tuple):
            return any(walk(v) for v in node)
        return is_numeric(node)

    return walk(suggestion)


def _assert_partition_complete(candidate: PlanCandidate, universe: frozenset[str]) -> None:
    """断言 `scheduled_jobs` 与 `unschedulable_jobs` 的 job_id 构成 `universe` 的一个划分。

    - 各自内部无重复（同一 job_id 不会在同一子集里出现两次）；
    - 两子集不相交（∩ = ∅）；
    - 并集为全集（∪ = `universe`）——既不遗漏（静默丢弃）也不越界（凭空造作业）。
    """
    scheduled_ids = [sj.job_id for sj in candidate.scheduled_jobs]
    unschedulable_ids = [uj.job_id for uj in candidate.unschedulable_jobs]

    scheduled_set = frozenset(scheduled_ids)
    unschedulable_set = frozenset(unschedulable_ids)

    assert len(scheduled_ids) == len(scheduled_set), "scheduled_jobs 内出现重复 job_id"
    assert len(unschedulable_ids) == len(unschedulable_set), (
        "unschedulable_jobs 内出现重复 job_id"
    )
    assert scheduled_set.isdisjoint(unschedulable_set), (
        "同一 job_id 同时出现在 scheduled 与 unschedulable（划分不相交被破坏）"
    )
    assert scheduled_set | unschedulable_set == universe, (
        "scheduled ∪ unschedulable ≠ 展开出的全部 ProductionJob（有作业被静默丢弃或凭空造出）"
    )


def _assert_feasibility_consistent(candidate: PlanCandidate) -> None:
    """断言 `feasibility` 与划分一致（design.md「Property 4」、R8.1 / R8.4）。

    无 unschedulable → `FEASIBLE`；无 scheduled → `NO_FEASIBLE_PLAN`；两者都非空 →
    `PARTIAL`。初始生成 `freeze=∅`，故「scheduled 为空」等价于「本次一个作业都没排上」。

    注意 unschedulable 与 scheduled 不可能**同时为空**：全集 `U` 非空（生成器保证 ≥1 个
    订单、每订单 ≥1 道工序），而两子集并集为全集，故至少一侧非空——因此三态判定覆盖全部
    可能，无「两空」的病态分支。
    """
    has_scheduled = len(candidate.scheduled_jobs) > 0
    has_unschedulable = len(candidate.unschedulable_jobs) > 0

    if not has_unschedulable:
        assert candidate.feasibility == "FEASIBLE", (
            f"无不可排产作业却 feasibility={candidate.feasibility}（应为 FEASIBLE）"
        )
    elif not has_scheduled:
        assert candidate.feasibility == "NO_FEASIBLE_PLAN", (
            f"无已排产作业却 feasibility={candidate.feasibility}（应为 NO_FEASIBLE_PLAN）"
        )
    else:
        assert candidate.feasibility == "PARTIAL", (
            f"两侧均非空却 feasibility={candidate.feasibility}（应为 PARTIAL）"
        )


def _assert_unschedulable_quantified(candidate: PlanCandidate) -> None:
    """断言每个 `unschedulable_job` 的 reason ∈ 9 类，且解锁建议按类别可量化（Option A）。

    这是 R8.2（reason 取自 R6.1 的 9 类）与 R8.3（至少一项可量化解锁条件）的机器可判定
    形式。量化的形状（哪一类含哪些字段）由任务 2.6 的 `quantify` 与其单元测试守护，本属性
    只断言对每一类都成立的可量化下界，并按类别分档（见模块 docstring「Option-A」、
    design.md §3.1.6）：

    - `OPERATION_PRECEDENCE_VIOLATION` 是转指类别，`unblock_suggestion` 输出
      `{predecessor_job_id, predecessor_blocking_reason}` 指向前序根因、本身无数值叶子——
      断言这两个转指字段存在且非 `None`，而**不**要求数值叶子；
    - `MACHINE_CAPABILITY_MISMATCH` 是能力匹配类别，`unblock_suggestion` 输出
      `{required_capability, qualifying_machine_types}`（后者可为空列表）、本身无数值叶子——
      断言 `required_capability` 非 `None` 且 `qualifying_machine_types` 键存在，而**不**要求
      数值叶子；
    - 其余 7 类断言 `unblock_suggestion` 至少含一个数值型量化字段。
    """
    for uj in candidate.unschedulable_jobs:
        assert uj.blocking_reason in BLOCKING_REASONS, (
            f"作业 {uj.job_id} 的 blocking_reason={uj.blocking_reason!r} 不属于 R6.1 的 9 类"
        )
        suggestion = uj.unblock_suggestion
        if uj.blocking_reason == "OPERATION_PRECEDENCE_VIOLATION":
            assert suggestion.get("predecessor_job_id") is not None, (
                f"作业 {uj.job_id}（OPERATION_PRECEDENCE_VIOLATION）的 unblock_suggestion 缺少"
                f"非空 predecessor_job_id（转指前序根因）：{suggestion!r}"
            )
            assert suggestion.get("predecessor_blocking_reason") is not None, (
                f"作业 {uj.job_id}（OPERATION_PRECEDENCE_VIOLATION）的 unblock_suggestion 缺少"
                f"非空 predecessor_blocking_reason（转指前序根因）：{suggestion!r}"
            )
        elif uj.blocking_reason == "MACHINE_CAPABILITY_MISMATCH":
            assert suggestion.get("required_capability") is not None, (
                f"作业 {uj.job_id}（MACHINE_CAPABILITY_MISMATCH）的 unblock_suggestion 缺少"
                f"非空 required_capability（缺失的能力）：{suggestion!r}"
            )
            assert "qualifying_machine_types" in suggestion, (
                f"作业 {uj.job_id}（MACHINE_CAPABILITY_MISMATCH）的 unblock_suggestion 缺少"
                f"qualifying_machine_types 键（具备该能力的机型，可为空）：{suggestion!r}"
            )
        else:
            assert _has_numeric_leaf(suggestion), (
                f"作业 {uj.job_id}（{uj.blocking_reason}）的 unblock_suggestion "
                f"无任何数值型量化字段：{suggestion!r}"
            )


# --------------------------------------------------------------------------
# Property 4：划分完备 + feasibility 一致 + 每个不可排产项均被量化
# --------------------------------------------------------------------------


@_SETTINGS
@given(
    scarcity=st.sampled_from(_SCARCITIES),
    data=st.data(),
)
def test_job_partition_is_complete_and_unschedulable_are_quantified(
    scarcity: Scarcity, data: st.DataObject
) -> None:
    """**Validates: Requirements 8.1, 8.2, 8.3, 8.4**

    对任意 `DomainSnapshot`（三档 scarcity 各自抽样），`generate_schedule` 的输出满足：

    1. **划分完备**（R8.1）：展开出的全部 `ProductionJob` 恰好被划分为 `scheduled_jobs` 与
       `unschedulable_jobs` 两个不相交且并集为全集的子集——没有作业被静默丢弃或凭空造出；
    2. **`feasibility` 一致**（R8.1、R8.4）：两侧空/非空的组合与 `FEASIBLE` /
       `NO_FEASIBLE_PLAN` / `PARTIAL` 三态严格对应；
    3. **每个不可排产项均被量化**（R8.2、R8.3；Option-A 分档见模块 docstring 与 design.md
       §3.1.6）：`blocking_reason` ∈ R6.1 的 9 类；两个引用型类别断言引用字段存在
       （`OPERATION_PRECEDENCE_VIOLATION` → `predecessor_job_id` /
       `predecessor_blocking_reason`；`MACHINE_CAPABILITY_MISMATCH` → `required_capability` /
       `qualifying_machine_types`），其余 7 类要求至少含一个数值型量化字段。

    `scarcity` 用 `sampled_from` 与内层 `data.draw` 组合，让三档物料丰俭都在同一属性下被
    Hypothesis 独立收缩，覆盖到全排上 / 全排不上 / 部分可行三种划分形态。
    """
    snapshot = data.draw(domain_snapshots(scarcity=scarcity), label="snapshot")

    candidate = generate_schedule(snapshot)
    universe = _all_expanded_job_ids(snapshot)

    _assert_partition_complete(candidate, universe)
    _assert_feasibility_consistent(candidate)
    _assert_unschedulable_quantified(candidate)
