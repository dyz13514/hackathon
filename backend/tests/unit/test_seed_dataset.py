"""演示数据集是否满足 R28.1–R28.7（任务 1.6）。

## 为什么这些断言必须存在

R28 的每一条都是「演示当天台上要发生的事」的前置条件，而它们全都可以在**数据层面**被
悄悄破坏：把一台机器的 `capabilities` 补齐一个能力，瓶颈就有了替代，情节 3 的
`CNC-01 承担 62% 作业且无替代` 变成一句假话；把 `MAT-STEEL-01` 的库存调高 40 kg，情节 9
的三个不可排产作业就凭空消失。这两种改动都不会让任何其他测试变红——它们看起来像是在
「把数据调得合理一点」。

因此本文件断言的不是实现，而是**数据集与需求的对应关系**。每条断言的失败信息里都写着
它守的是哪个演示情节。

## 有意不在这里断言的一条

R28.7（FCFS 明显劣于 Agent）需要 `Baseline_Scheduler`（任务 2.11）才能测量。这里只断言
让差距得以出现的三个**结构条件**：瓶颈机不可替代、瓶颈上有非对称换型规则、非瓶颈工序
有多台候选机器。数值差距在任务 4 的检查点复核。把一条测不了的断言写成 `assert True`
比没有这条断言更糟——它会让人以为已经验过了。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db.models import (
    MACHINE_STATUSES,
    ORDER_PRIORITIES,
    RECORD_SOURCES,
)
from app.seed.dataset import (
    ABSENCES,
    BOTTLENECK_CAPABILITY,
    BOTTLENECK_MACHINE_ID,
    BOUNDARY_DEMO_ORDER_IDS,
    CHANGEOVER_RULES,
    DELIVERIES,
    DEMO_ANCHOR,
    DOWNTIME,
    EXPECTED_STEEL_SHORTFALL,
    HORIZON_DAYS,
    INJECTION_DEMO_ORDER_ID,
    MACHINES,
    MATERIALS,
    ORDERS,
    PRODUCTS,
    SCARCE_MATERIAL_ID,
    SEED_SOURCE,
    WORKERS,
    ZERO_SLACK_ORDER_ID,
    bottleneck_job_count,
    material_demand,
    material_supply_within_horizon,
    order_committed_minutes,
    order_slack_minutes,
    product_index,
    total_job_count,
)

# --------------------------------------------------------------------------
# R28.1 规模
# --------------------------------------------------------------------------


def test_dataset_has_the_required_entity_counts() -> None:
    """6 Product / 14 Order / 10 Material / 5 Machine / 8 Worker（R28.1）。"""
    assert len(PRODUCTS) == 6
    assert len(ORDERS) == 14
    assert len(MATERIALS) == 10
    assert len(MACHINES) == 5
    assert len(WORKERS) == 8


def test_at_least_three_products_have_two_or_three_operations() -> None:
    """R28.1 的「至少 3 个具备 2–3 道 Operation」。"""
    multi = [product for product in PRODUCTS if 2 <= len(product.operations) <= 3]
    assert len(multi) >= 3, f"只有 {len(multi)} 个多工序产品，情节 2 的三道工序演示会落空"


def test_every_operation_sequence_is_a_valid_linear_chain() -> None:
    """工序号从 1 连续递增且 ≤ 3——DDL 的 `CHECK (sequence BETWEEN 1 AND 3)` 与
    `UNIQUE (product_id, sequence)` 会拒绝其他形态（R4.1、R4.7）。

    在数据层先断言一遍，是为了让「seed 写不进去」这个失败在毫秒级的单元测试里暴露，
    而不是在 `make dev` 的第三步以一条外键/约束错误的形式暴露。
    """
    for product in PRODUCTS:
        sequences = [operation.sequence for operation in product.operations]
        assert sequences == list(range(1, len(sequences) + 1)), (
            f"{product.product_id} 的工序号不是从 1 起的连续链：{sequences}"
        )
        assert len(sequences) <= 3


def test_every_reference_inside_the_dataset_resolves() -> None:
    """数据集内部引用完整。

    `load_snapshot()` 的引用完整性预检（任务 2.1）会对缺失引用返回
    `DATA_INTEGRITY_ERROR`。演示数据自己触发那条错误是最尴尬的失败形态，因此在这里
    先把它排除。
    """
    product_ids = {product.product_id for product in PRODUCTS}
    material_ids = {material.material_id for material in MATERIALS}
    machine_ids = {machine.machine_id for machine in MACHINES}
    worker_ids = {worker.worker_id for worker in WORKERS}

    for order in ORDERS:
        assert order.product_id in product_ids, f"{order.order_id} 指向未知产品"
    for product in PRODUCTS:
        for line in product.bom:
            assert line.material_id in material_ids, f"{product.product_id} 的 BOM 指向未知物料"
    for delivery in DELIVERIES:
        assert delivery.material_id in material_ids
    for downtime in DOWNTIME:
        assert downtime.machine_id in machine_ids
    for absence in ABSENCES:
        assert absence.worker_id in worker_ids
    for rule in CHANGEOVER_RULES:
        assert rule.machine_id is None or rule.machine_id in machine_ids
        assert rule.from_product_id is None or rule.from_product_id in product_ids
        assert rule.to_product_id is None or rule.to_product_id in product_ids


def test_ids_are_unique_within_each_entity_type() -> None:
    """主键不重复。重复 ID 会让 seed 在 flush 时撞主键冲突。"""
    for label, ids in (
        ("product", [product.product_id for product in PRODUCTS]),
        ("order", [order.order_id for order in ORDERS]),
        ("material", [material.material_id for material in MATERIALS]),
        ("machine", [machine.machine_id for machine in MACHINES]),
        ("worker", [worker.worker_id for worker in WORKERS]),
        ("changeover", [rule.rule_id for rule in CHANGEOVER_RULES]),
        ("delivery", [delivery.delivery_id for delivery in DELIVERIES]),
    ):
        assert len(ids) == len(set(ids)), f"{label} 的 ID 有重复：{ids}"


def test_enumerated_values_stay_inside_the_schema_domains() -> None:
    """优先级、机器状态、`source` 的取值都在 schema 的取值域里。"""
    assert SEED_SOURCE in RECORD_SOURCES
    for order in ORDERS:
        assert order.priority in ORDER_PRIORITIES
    for machine in MACHINES:
        assert machine.status in MACHINE_STATUSES


# --------------------------------------------------------------------------
# R28.2 瓶颈机
# --------------------------------------------------------------------------


def test_bottleneck_machine_carries_at_least_half_of_all_jobs() -> None:
    """`CNC-01` 上的作业 ≥ 全部作业的 50%（R28.2，情节 3 与 5）。

    分子是「必须落在 CNC-01 上」的作业数——由能力集合决定，与调度顺序无关。因此这条
    断言给出的是**下界**：实际排产还会把部分铣削作业也放上去（CNC-01 的
    `rate_multiplier` 最高），占比只会更高。
    """
    total = total_job_count()
    on_bottleneck = bottleneck_job_count()
    assert total == 25, f"作业总数变了（{total}），占比断言的分母需要复核"
    assert on_bottleneck * 2 >= total, (
        f"CNC-01 只承担 {on_bottleneck}/{total} 个作业，不足 50%："
        "情节 3「承担 62% 作业且无替代」与情节 5 的设备故障演示都会失去说服力"
    )


def test_no_other_machine_can_substitute_for_the_bottleneck() -> None:
    """没有第二台机器具备 `DEEP_DRILLING`（R28.2 的「无同 `capabilities` 替代」）。

    断言的是**能力集合**而不是负载：靠「其他机器都忙」造出来的不可替代性会随排产结果
    变化，而演示需要的是一个恒真的事实。
    """
    bottleneck = next(m for m in MACHINES if m.machine_id == BOTTLENECK_MACHINE_ID)
    assert BOTTLENECK_CAPABILITY in bottleneck.capabilities

    substitutes = [
        machine.machine_id
        for machine in MACHINES
        if machine.machine_id != BOTTLENECK_MACHINE_ID
        and machine.machine_type == bottleneck.machine_type
        and BOTTLENECK_CAPABILITY in machine.capabilities
    ]
    assert not substitutes, f"{substitutes} 能替代 CNC-01，情节 5 的故障演示会被自动绕过"


# --------------------------------------------------------------------------
# R28.3 会耗尽的物料
# --------------------------------------------------------------------------


def test_scarce_material_runs_out_within_the_horizon() -> None:
    """`MAT-STEEL-01` 在 3 天时域内缺 40.0 kg（R28.3，情节 3 与 9）。

    缺口从 BOM × 订单量反算，不是写死的常量对写死的常量。改任一处 BOM 用量、订单数量、
    库存或到货，这条断言就会报出新的缺口值——那时该做的是有意识地更新
    `EXPECTED_STEEL_SHORTFALL`，而不是让情节 3 在台上悄悄变成「物料充足」。
    """
    demand = material_demand()[SCARCE_MATERIAL_ID]
    supply = material_supply_within_horizon()[SCARCE_MATERIAL_ID]
    assert demand - supply == EXPECTED_STEEL_SHORTFALL, (
        f"钢材缺口为 {demand - supply}，预期 {EXPECTED_STEEL_SHORTFALL}"
    )


def test_only_the_scarce_material_is_short() -> None:
    """其余 9 种物料都够。

    多一种缺料会让情节 9 的解锁条件清单变长、让 `PARTIAL` 计划的成因变得含混——
    「缺 40 kg 钢材」这句话之所以有力，是因为它是**唯一**的物料瓶颈。
    """
    demand = material_demand()
    supply = material_supply_within_horizon()
    short = {
        material_id: supply[material_id] - required
        for material_id, required in demand.items()
        if supply[material_id] < required
    }
    assert set(short) == {SCARCE_MATERIAL_ID}, f"意外的缺料：{short}"


def test_every_material_participates_in_some_bom() -> None:
    """10 种物料都被至少一个产品用到。

    一种没人用的物料在状态看板上是纯噪声：它永远不会出现在任何计划里，规划员却要在
    库存卡片上看到它。
    """
    used = set(material_demand())
    declared = {material.material_id for material in MATERIALS}
    assert used == declared, f"未被任何 BOM 使用的物料：{sorted(declared - used)}"


def test_a_delivery_falls_outside_the_horizon_and_is_not_counted() -> None:
    """存在一条 ETA 落在时域之外的到货，且它不计入可用量。

    `available_at(material, t)` 只计 `eta < t` 的到货（R6.3）。这条断言让那个边界在
    演示数据里有一个真实样本，而不是只存在于单元测试的构造里。
    """
    outside = [delivery for delivery in DELIVERIES if delivery.eta[0] >= HORIZON_DAYS]
    assert outside, "演示数据里没有时域外到货，`eta < t` 这条边界缺少真实样本"

    supply = material_supply_within_horizon()
    for delivery in outside:
        material = next(m for m in MATERIALS if m.material_id == delivery.material_id)
        assert supply[delivery.material_id] == (
            material.quantity_available - material.reserved_quantity
        ), f"{delivery.delivery_id} 落在时域之外却被计入了可用量"


def test_an_unconfirmed_delivery_exists_for_the_assumptions_list() -> None:
    """存在未确认的到货 ETA——它是解释里假设清单的素材（R10.4，情节 6）。"""
    assert any(not delivery.confirmed for delivery in DELIVERIES)


# --------------------------------------------------------------------------
# R28.4 零裕度订单
# --------------------------------------------------------------------------


def test_zero_slack_order_has_non_positive_slack() -> None:
    """`ORD-004` 的裕度 ≤ 0，且这件事不依赖排产顺序（R28.4，情节 3）。

    裕度按**下限**耗时算（准备 + 加工，忽略换型与排队，倍率取最有利的 1.0）。下限都越过
    交期，任何调度顺序都越过；把它做成「排在队尾所以来不及」会让这个演示情节取决于
    调度器实现。
    """
    order = next(order for order in ORDERS if order.order_id == ZERO_SLACK_ORDER_ID)
    slack = order_slack_minutes(order)
    assert slack <= 0, f"{ZERO_SLACK_ORDER_ID} 的裕度为 {slack} 分钟，ZERO_SLACK_ORDER 情节不成立"


def test_exactly_one_order_has_non_positive_slack() -> None:
    """只有一张零裕度订单。

    R28.4 要的是「一个」。多几张会让情节 3 的风险雷达一次亮起一片红，规划员分不出该先
    看哪条，而演示要展示的恰是「逐条可解释的风险」。
    """
    negative = [order.order_id for order in ORDERS if order_slack_minutes(order) <= 0]
    assert negative == [ZERO_SLACK_ORDER_ID], f"裕度 ≤ 0 的订单为 {negative}"


def test_zero_slack_order_committed_minutes_exceed_time_to_due_date() -> None:
    """把上一条的算术摊开写一遍，防止 `order_slack_minutes` 自己算错还自证正确。"""
    order = next(order for order in ORDERS if order.order_id == ZERO_SLACK_ORDER_ID)
    minutes_needed = order_committed_minutes(order)
    due_day, due_hour = order.due_date
    minutes_available = (due_day * 24 + due_hour - DEMO_ANCHOR.hour) * 60
    assert minutes_needed > minutes_available, (
        f"需要 {minutes_needed} 分钟，到交期还有 {minutes_available} 分钟——裕度并不为负"
    )


# --------------------------------------------------------------------------
# R28.1 的 promised_date 与情节 8 的越界对
# --------------------------------------------------------------------------


def test_at_least_five_orders_have_a_promised_date() -> None:
    """≥5 张订单填了 `promised_date`（tasks.md 1.6）。

    R13 的 `IMPACT_MINOR` 判据是「不改变任何订单的 `promised_date`」。若几乎没有订单
    对外承诺过，那条判据就恒真，情节 8 的分级演示会退化成「一切都是 MINOR」。
    """
    promised = [order.order_id for order in ORDERS if order.promised_date is not None]
    assert len(promised) >= 5, f"只有 {len(promised)} 张订单有承诺日：{promised}"


def test_the_two_boundary_demo_orders_are_promised_and_high_priority() -> None:
    """情节 8 的两张越界订单：已承诺 + `HIGH` 优先级，因此必然强制上报人工。"""
    index = {order.order_id: order for order in ORDERS}
    for order_id in BOUNDARY_DEMO_ORDER_IDS:
        order = index[order_id]
        assert order.promised_date is not None, f"{order_id} 没有承诺日，越界演示不成立"
        assert order.priority == "HIGH", f"{order_id} 的优先级是 {order.priority}，不是 HIGH"


def test_the_injection_demo_order_carries_untrusted_text_but_is_not_pre_flagged() -> None:
    """情节 9 的对抗订单：`notes` 里有注入文本。

    注入判定属于 `Guardrail_Layer`（任务 5.8），seed 不代它下结论——`injection_suspected`
    在写库时恒为 `False`（`loader.build_rows`）。若 seed 预先置位，`EVAL-203` 会在一个
    已被标记的输入上通过，证明不了检测本身工作。
    """
    order = next(order for order in ORDERS if order.order_id == INJECTION_DEMO_ORDER_ID)
    assert order.notes is not None
    assert "忽略先前指令" in order.notes


# --------------------------------------------------------------------------
# R28.7 的三个结构条件
# --------------------------------------------------------------------------


def test_bottleneck_has_asymmetric_exact_changeover_rules() -> None:
    """瓶颈上存在精确换型规则，且**非对称**（R28.7 的第二个结构条件）。

    「同一批作业换个顺序，换型总时长差出几小时」是 FCFS 输给 Agent 的主要来源。若全部
    换型时间相等，排序就不影响换型成本，基线对比里那一栏会是 0。
    """
    exact = [rule for rule in CHANGEOVER_RULES if rule.specificity == 3]
    assert exact, "没有任何精确换型规则，查表的第一条分支不会被走到"
    assert all(rule.machine_id == BOTTLENECK_MACHINE_ID for rule in exact)

    minutes = {rule.changeover_minutes for rule in exact}
    assert len(minutes) > 1, f"精确规则的换型时间全都相等（{minutes}），排序不影响成本"
    assert max(minutes) - min(minutes) >= 30, (
        f"精确规则的换型时间跨度只有 {max(minutes) - min(minutes)} 分钟，差距不足以在演示中可见"
    )


def test_all_three_changeover_specificity_levels_have_samples() -> None:
    """三级 `specificity` 都有样本，因此查表的三条分支都会被走到（design.md §3.1.4）。"""
    levels = {rule.specificity for rule in CHANGEOVER_RULES}
    assert levels == {1, 2, 3}, f"specificity 覆盖不全：{sorted(levels)}"

    global_rules = [rule for rule in CHANGEOVER_RULES if rule.specificity == 1]
    assert len(global_rules) == 1, "全局默认规则必须恰好一条，否则查表结果取决于行顺序"


def test_non_bottleneck_operations_have_more_than_one_candidate_machine() -> None:
    """铣削工序有多台候选机器（R28.7 的第三个结构条件）。

    FCFS 刻意退化为「取 ID 最小的可行机器」。只有当存在多台候选时，这条退化才会把铣削
    也堆到已经饱和的 CNC-01 上，而 Agent 会把它们摊到 CNC-02/03——这是基线对比里最容易
    看懂的一处差异。
    """
    milling_capable = [
        machine.machine_id
        for machine in MACHINES
        if machine.machine_type == "CNC" and "PRECISION_MILLING" in machine.capabilities
    ]
    assert len(milling_capable) >= 3, f"铣削候选只有 {milling_capable}"
    assert min(milling_capable) == BOTTLENECK_MACHINE_ID, (
        "CNC-01 必须是 ID 最小的铣削候选，否则 FCFS 的「取 ID 最小」不会加剧瓶颈拥塞"
    )


def test_machines_and_workers_cover_every_required_capability_and_skill() -> None:
    """每道工序要求的能力与技能都有持有者。

    缺一个就意味着相应作业**永远**不可排产。那不是「演示了 PARTIAL」，那是数据集坏了：
    `MACHINE_CAPABILITY_MISMATCH` 会盖住本该出现的物料缺口。
    """
    capabilities = {cap for machine in MACHINES for cap in machine.capabilities}
    skills = {skill for worker in WORKERS for skill in worker.skills}
    machine_types = {machine.machine_type for machine in MACHINES}

    for product in PRODUCTS:
        for operation in product.operations:
            assert operation.required_machine_type in machine_types
            assert operation.required_worker_skill in skills
            if operation.required_capability is not None:
                assert operation.required_capability in capabilities


def test_scarcity_points_exist_for_shift_and_absence_blocking_reasons() -> None:
    """存在窄班次工人与缺勤记录。

    `SHIFT_WINDOW_EXCEEDED` 与 `WORKER_UNAVAILABLE` 是 9 类阻塞原因里的两类
    （R8.2）。没有这两个稀缺点，它们在演示数据上永远不会出现，
    `diagnose_blocking` 的对应分支也就只有合成测试覆盖。
    """
    full_span = max(worker.shift_end for worker in WORKERS)
    narrow = [worker.worker_id for worker in WORKERS if worker.shift_end < full_span]
    assert narrow, "没有窄班次工人，SHIFT_WINDOW_EXCEEDED 在演示数据上无从出现"
    assert ABSENCES, "没有缺勤记录，WORKER_UNAVAILABLE 在演示数据上无从出现"


def test_a_maintenance_window_exists_without_a_disruption() -> None:
    """seed 里有计划保养窗口，且不挂在任何扰动上。

    `MachineDowntime.disruption_id` 可空正是为这种情况留的（见该模型 docstring）。
    保养窗口同时收窄了铣削的替代余量，让第 2 天的排产真的需要权衡。
    """
    assert DOWNTIME
    assert all(downtime.reason == "MAINTENANCE" for downtime in DOWNTIME)


# --------------------------------------------------------------------------
# 确定性
# --------------------------------------------------------------------------


def test_derived_quantities_are_stable_across_calls() -> None:
    """派生量两次计算结果相同——它们是纯函数，不依赖时钟或迭代顺序。

    这条是 `POST /demo/reset` 幂等性的前提之一：若同一份数据集能算出两个不同的需求表，
    「连续两次重置结果相同」就无从谈起。
    """
    assert material_demand() == material_demand()
    assert material_supply_within_horizon() == material_supply_within_horizon()
    assert product_index().keys() == product_index().keys()


@pytest.mark.parametrize("attribute", ["quantity_available", "reserved_quantity"])
def test_material_quantities_are_decimal_not_float(attribute: str) -> None:
    """物料数量是 `Decimal`。

    design.md §3.1.4 禁止浮点进入排产算术；`Decimal("0.35") * 470` 与
    `0.35 * 470` 得到的不是同一个数，而缺口断言精确到 0.1 kg。
    """
    for material in MATERIALS:
        assert isinstance(getattr(material, attribute), Decimal)
