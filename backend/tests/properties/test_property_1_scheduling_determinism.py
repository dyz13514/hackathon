"""Property 1：排产确定性可重现（任务 2.5，R5.7；design.md Correctness Properties「Property 1」）。

*For any* `DomainSnapshot`，`Scheduling_Core.generate_schedule` 连续两次运行、以及在**打乱输入
集合元素顺序**后运行，产出的 `ScheduledJob` 集合、`unschedulable_jobs` 集合与 `feasibility`
**逐字段完全相同**（design.md §2546「Property 1」）。

## 为什么这条属性排在最前、`max_examples=300`

它是其余六条属性的前提（tasks.md 任务 2.5）：排产若不确定，任何其他属性一旦失败，其
counterexample 都无法在下一次运行里复现——收缩机制会对着一个每次都变的目标空转。因此它
必须先被钉死，且它是七条里单次执行成本最低的一条（只跑 `generate_schedule`，不跑校验器 /
打分器 / 审批流），把预算加厚到 300 例是划算的。

## 「逐字段完全相同」如何断言

`ScheduledJob` / `UnschedulableJob` / `PlanCandidate` 都是 `frozen=True` 的 Pydantic 模型，
`__eq__` 基于**全部字段**。据此分两种口径：

- 连续两次**同序**运行：直接比 `PlanCandidate` 整体相等（含 `unblock_suggestion`），逐字段、
  逐元素（含顺序）比较——纯函数在相同输入上必然逐字节一致。
- **打乱输入顺序**后运行：比**排产决策**（`ScheduledJob` 逐字段、`unschedulable_jobs` 的稳定
  身份、`feasibility`），与元素排列无关。这里不用 `frozenset`——`UnschedulableJob` 含不可
  哈希的 `dict` 字段（`unblock_suggestion`），改用「取稳定字段、排序成多重集」的写法；
  而这些量化细节本就不在比较范围内（见下）。

design.md 的措辞是「`ScheduledJob` 集合、`unschedulable_jobs` 集合与 `feasibility` 逐字段
完全相同」——集合语义。属性 1 守的是**排产决策**的确定性：哪些作业排上了、排在哪台机器
哪个工人的哪段时间（`ScheduledJob` 的全部字段）、哪些作业排不上、各自属于 9 类阻塞里的
哪一类（`blocking_reason`）、以及整体 `feasibility`。这三样由任务 2.4 的主循环负责，它对
订单施加确定性全序 `(PRIORITY_RANK, due_date, order_id)`、候选 tie-break 也是全序
`(cost, machine_id, worker_id)`，因此打乱输入后这三样逐字段稳定。

## `unblock_suggestion` 的量化细节不在本属性的比较范围内（任务边界）

`UnschedulableJob.unblock_suggestion` 里的量化字段（如 `shift_window` 报的是**哪一个**候选
工人的班次窗）由 `diagnose_blocking` / `quantify` 渲染，那是**任务 2.6** 的职责，且此刻正在
并行开发中。这些量化细节的确定性（例如「多个等价候选里报哪一个的窗」）属于 2.6 要保证的
性质，与本属性守的「排产决策可重现」正交——tasks.md 任务 2.5 的备注也明确：本测试断言的
determinism「与具体的阻塞原因（blocking reasons）正交」。因此比较 `unschedulable_jobs` 时
只比其**稳定身份** `(job_id, order_id, blocking_reason)`，不比 `unblock_suggestion` 的内部
量化字段——后者由 2.6 的属性 4（任务 2.7）与其单元测试守护。

连续两次**同序**运行则断言 `PlanCandidate` 整体相等（含 `unblock_suggestion`）：纯函数在
完全相同的输入上必然逐字节一致，这与 2.6 的量化选择无关——同一份输入喂两次，2.6 无论怎么
渲染都会渲染出同一个结果。只有「打乱输入顺序」才会触碰到「多个等价候选选哪个」这一 2.6
的自由度，故打乱侧退到稳定身份比较。

## 不碰数据库、不碰 LLM

输入是 `domain_snapshots` 产出的**内存**快照（tests/generators.py 的主力生成器，属性 1 是
它的六个消费者之一）。属性守的是内核纯函数 `generate_schedule` 的确定性，把数据库拖进来
只会让每个 example 退化成集成测试。`conftest.py` 已把测试期 `LLM_MODE` 强制为 `STUB`，
而本路径根本不触达 LLM，故额度纪律在此平凡满足（tests/generators.py 模块 docstring）。
"""

from __future__ import annotations

import random

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.scheduler import PlanCandidate, generate_schedule
from app.core.snapshot import DomainSnapshot
from tests.generators import domain_snapshots

# 属性 1 是七条里成本最低的一条，且是其余各条可复现的前提，因此把预算加厚到 300 例
# （tasks.md 任务 2.5 明确要求 `max_examples=300`）。快照构造成本略高，放宽 deadline 并
# 抑制「过慢」健康检查——慢不是错误，只有确定性才是这里要守的。
_SETTINGS = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# 打乱输入元素顺序用的种子池。用固定的一组种子而非再抽一个随机数，是为了让 counterexample
# 完全由 `domain_snapshots` 的抽样决定、可精确复现（R5.7 的精神延伸到测试本身）。
_SHUFFLE_SEEDS: tuple[int, ...] = (1, 7, 42, 1234)


def _shuffled_snapshot(snapshot: DomainSnapshot, seed: int) -> DomainSnapshot:
    """返回一个把七个输入集合各自**打乱元素顺序**后的等价快照。

    七个集合字段（`orders` / `products` / `materials` / `machines` / `workers` /
    `changeover_rules` / `preference_rules`）都是 tuple，用同一个种子的独立 `Random` 实例
    分别打乱，再经 `model_copy(update=...)` 得到变体——`DomainSnapshot` 是 `frozen=True`，
    这是唯一的合法造变体途径（snapshot.py 模块 docstring）。

    打乱只改变**元素排列**，不增删任何元素，因此变体与原件在语义上完全等价：引用完整性
    不变、路线合法不变。排产结果理应逐字段相同——这正是 R5.7 要守的。
    """
    rng = random.Random(seed)

    def shuffle(items: tuple[object, ...]) -> tuple[object, ...]:
        lst = list(items)
        rng.shuffle(lst)
        return tuple(lst)

    return snapshot.model_copy(
        update={
            "orders": shuffle(snapshot.orders),
            "products": shuffle(snapshot.products),
            "materials": shuffle(snapshot.materials),
            "machines": shuffle(snapshot.machines),
            "workers": shuffle(snapshot.workers),
            "changeover_rules": shuffle(snapshot.changeover_rules),
            "preference_rules": shuffle(snapshot.preference_rules),
        }
    )


def _scheduled_multiset(jobs: tuple[object, ...]) -> list[str]:
    """把 `ScheduledJob` 集合转成**与排列无关**的逐字段表示（`repr` 后排序）。

    不能用 `frozenset(...)`：虽然 `ScheduledJob` 本身可哈希，但统一走 `repr` 排序既覆盖全部
    字段又与排列无关，且与 `_unschedulable_multiset` 的写法对齐。`ScheduledJob` 的全部字段
    （机器 / 工人 / 起止时刻 / 换型分钟）都是排产决策，全部纳入比较。
    """
    return sorted(repr(job) for job in jobs)


def _unschedulable_multiset(jobs: tuple[object, ...]) -> list[tuple[str, str, str]]:
    """把 `unschedulable_jobs` 转成**稳定身份**的多重集：`(job_id, order_id, blocking_reason)`。

    刻意**不**纳入 `unblock_suggestion`：它的量化细节由任务 2.6 的 `quantify` 渲染，「多个
    等价候选里报哪一个的班次窗」是 2.6 的自由度，与本属性守的「排产决策可重现」正交
    （见模块 docstring 的任务边界说明）。稳定身份三元组捕捉的是排产决策本身——**哪个**作业
    排不上、属于 9 类阻塞里的**哪一类**——这三者由任务 2.4 的主循环确定性地产出。

    也因此不用 `frozenset`：`unblock_suggestion` 是不可哈希的 `dict`，而我们本就不比它；
    取三个字符串字段组成元组、排序成多重集，既与排列无关又避开 dict 哈希。
    """
    return sorted(
        (job.job_id, job.order_id, job.blocking_reason)  # type: ignore[attr-defined]
        for job in jobs
    )


def _assert_same_decisions(a: PlanCandidate, b: PlanCandidate) -> None:
    """断言两份结果的**排产决策**一致：`ScheduledJob` 集合逐字段相同、`unschedulable_jobs`
    的稳定身份集合相同、`feasibility` 相同（design.md「Property 1」的集合语义）。
    """
    assert _scheduled_multiset(a.scheduled_jobs) == _scheduled_multiset(b.scheduled_jobs), (
        "ScheduledJob 集合不一致（逐字段）"
    )
    assert _unschedulable_multiset(a.unschedulable_jobs) == _unschedulable_multiset(
        b.unschedulable_jobs
    ), "unschedulable_jobs 稳定身份集合不一致（job_id / order_id / blocking_reason）"
    assert a.feasibility == b.feasibility, "feasibility 不一致"


# --------------------------------------------------------------------------
# Property 1（主）：连续两次同序运行整体相等 + 打乱输入后排产决策不变
# --------------------------------------------------------------------------


@_SETTINGS
@given(snapshot=domain_snapshots(), seed=st.sampled_from(_SHUFFLE_SEEDS))
def test_scheduling_is_deterministic_and_order_independent(
    snapshot: DomainSnapshot, seed: int
) -> None:
    """**Validates: Requirements 5.7**

    在同一 `DomainSnapshot` 上：

    1. 连续两次**同序**运行 `generate_schedule`，结果 `PlanCandidate` **整体相等**
       （含 `unblock_suggestion` 在内的每一个字段）——纯函数在相同输入上必然逐字节一致，
       这是 determinism 最基本的保证，与任务 2.6 的量化选择无关（同一输入喂两次，2.6 渲染
       出的也是同一个结果）；
    2. 在打乱七个输入集合元素顺序后再运行，产出的**排产决策**与原运行相同：`ScheduledJob`
       集合逐字段相同、`unschedulable_jobs` 的稳定身份 `(job_id, order_id, blocking_reason)`
       集合相同、`feasibility` 相同（design.md「Property 1」的集合语义）。`unblock_suggestion`
       的量化细节不在比较范围内——它是任务 2.6 的职责，与本属性正交（见模块 docstring）。
    """
    first = generate_schedule(snapshot)
    second = generate_schedule(snapshot)

    # ① 连续两次同序运行：整体相等（含全部字段）。frozen 模型的 __eq__ 覆盖全部字段。
    assert first == second, "同一快照连续两次运行的结果不一致（determinism 被破坏）"

    # ② 打乱七个输入集合的元素顺序后运行，排产决策不变。
    shuffled = _shuffled_snapshot(snapshot, seed)
    from_shuffled = generate_schedule(shuffled)
    _assert_same_decisions(first, from_shuffled)


# --------------------------------------------------------------------------
# Property 1（补强）：多个不同种子的打乱互相之间也一致
# --------------------------------------------------------------------------


@_SETTINGS
@given(snapshot=domain_snapshots())
def test_all_shuffles_agree_with_each_other(snapshot: DomainSnapshot) -> None:
    """**Validates: Requirements 5.7**

    对同一快照用多个不同种子分别打乱输入顺序，全部运行的**排产决策**两两一致。

    单个种子的打乱只探测一种排列；用一组互不相同的种子覆盖多种排列，把「排产决策不依赖
    任何特定输入排列」这条从「对某一种打乱成立」加强为「对多种打乱一致成立」。若排产里
    潜藏一处依赖插入顺序的非确定性（例如用了 set 迭代顺序或非全序的 tie-break），单一种子
    可能恰好躲过，多种排列一起比对更容易把它逼出来。

    比较口径与主属性一致：`ScheduledJob` 逐字段、`unschedulable_jobs` 稳定身份、`feasibility`
    ——`unblock_suggestion` 的量化细节归任务 2.6，不在此比较（见模块 docstring）。
    """
    baseline = generate_schedule(snapshot)
    for seed in _SHUFFLE_SEEDS:
        result = generate_schedule(_shuffled_snapshot(snapshot, seed))
        _assert_same_decisions(baseline, result)
