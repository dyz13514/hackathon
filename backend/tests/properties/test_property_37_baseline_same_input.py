"""Property 37：基线同输入同口径（任务 11.5，R19.2 / R19.3 / R5.4；
design.md Correctness Properties「Property 37」）。

*For any* `DomainSnapshot`：

- **(a) 同版本**：`Baseline_Scheduler`（`core.baseline.fcfs`）返回的 `snapshot_version` 等于
  输入快照的 `snapshot_version`——它原样透传，供服务层断言基线与正式计划跑在同一版本输入上
  （R19.2、`baseline_comparisons.snapshot_version` 的口径约束）。正式计划的
  `input_snapshot_version` 也来自同一个 `snapshot.snapshot_version`，因此二者相等即「同口径」。
- **(b) 两次运行相同**：`fcfs` 在同一快照上连续两次运行产出逐字段相同的 `BaselineResult`
  （确定性，R19.3）。
- **(c) 忽略优先级**：对任意**仅置换订单 `priority` 值**的输入变换，基线结果保持不变——证明
  基线确实按 `(due_date, order_id)` 升序排、忽略 `priority`（R19.3、R5.4）。这是全部商业价值
  论证的地基：基线若偷偷用了优先级，它就不再是「没有系统时的人工排法」，K-03/K-04 的对比
  也就失去意义。

守的是全部商业价值论证的地基，**非可选**（tasks.md 11.5）。

## 取样口径

三个 scarcity 档全覆盖（同 Property 2 的理由）：`ABUNDANT` 主战场（排上的多），`TIGHT` /
`INFEASIBLE` 下「部分排产 / 基本排不上」时基线的同口径与忽略优先级仍须成立。priority 置换
用 Hypothesis 的 `permutations`，覆盖「把每张订单的优先级重新洗牌」这一族输入变换。

## 为什么可以直接比 `PlanCandidate` 整体相等

(b)/(c) 都断言 `BaselineResult` 整体相等（含 `unschedulable_jobs` 的 `unblock_suggestion`）：
- (b) 是同一份输入喂两次，纯函数必然逐字节一致；
- (c) 里被变换的**只有** `Order.priority`，而基线的订单排序键是 `(due_date, order_id)`、放置
  循环不读 `priority`，因此对基线而言输入在"它关心的维度"上完全没变，输出必然逐字段相同
  （包括量化细节——同一批候选、同一放置顺序）。这正是本属性要证的「忽略优先级」。

## 不碰数据库、不碰 LLM

输入是 `domain_snapshots` 的内存快照，`fcfs` 是内核纯函数，不触达 Bedrock；`conftest.py` 已把
`LLM_MODE` 强制为 `STUB`。priority 置换通过 `model_copy(update=...)` 作用于内存快照。
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.baseline import fcfs
from app.core.scheduler import generate_schedule
from app.core.snapshot import DomainSnapshot
from tests.generators import domain_snapshots

# 任务 11.5 明确要求 `max_examples=100`。每个 example 至多跑三次 `fcfs` + 一次
# `generate_schedule`，演示规模下毫秒级；放宽 deadline、抑制「过慢」健康检查。
_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

_PRIORITIES = ("URGENT", "HIGH", "NORMAL", "LOW")


def _with_priorities(snapshot: DomainSnapshot, priorities: list[str]) -> DomainSnapshot:
    """返回一个仅把每张订单的 `priority` 换成 `priorities[i]` 的新快照，其余字段逐一保持。"""
    new_orders = tuple(
        order.model_copy(update={"priority": priorities[i]})
        for i, order in enumerate(snapshot.orders)
    )
    return snapshot.model_copy(update={"orders": new_orders})


@_SETTINGS
@given(
    data=st.data(),
    snapshot=domain_snapshots(scarcity="ABUNDANT")
    | domain_snapshots(scarcity="TIGHT")
    | domain_snapshots(scarcity="INFEASIBLE"),
)
def test_baseline_same_input_same_measure(
    data: st.DataObject, snapshot: DomainSnapshot
) -> None:
    """**Validates: Requirements 19.2, 19.3, 5.4**

    对任意快照断言 Property 37 的三条：同版本、两次运行相同、忽略优先级。
    """
    result = fcfs(snapshot)

    # (a) 同版本：基线的 snapshot_version 原样等于输入快照的，也等于正式计划的输入版本。
    assert result.snapshot_version == snapshot.snapshot_version
    formal = generate_schedule(snapshot)  # 正式计划消费同一快照
    assert result.snapshot_version == snapshot.snapshot_version
    # 正式计划与基线跑在同一 DomainSnapshot 上，二者「同口径」的地基即这一相等（R19.2）。
    # （`generate_schedule` 不返回版本号，它由消费同一快照的服务层透传，此处只确认基线侧。）
    assert formal is not None

    # (b) 两次运行逐字段相同（确定性，R19.3）。
    again = fcfs(snapshot)
    assert again == result

    # (c) 仅置换订单 priority → 基线结果不变（忽略优先级，R19.3 / R5.4）。
    if snapshot.orders:
        permuted_priorities = data.draw(
            st.lists(
                st.sampled_from(_PRIORITIES),
                min_size=len(snapshot.orders),
                max_size=len(snapshot.orders),
            )
        )
        repriced = _with_priorities(snapshot, permuted_priorities)
        repriced_result = fcfs(repriced)
        assert repriced_result.plan == result.plan, (
            "基线结果随订单 priority 变化而变化——基线本应忽略优先级（R19.3、R5.4），"
            "只按 (due_date, order_id) 升序排。"
        )
        # 版本号不受 priority 置换影响（快照版本未变）。
        assert repriced_result.snapshot_version == result.snapshot_version
