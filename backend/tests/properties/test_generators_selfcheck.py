"""生成器的自检测试（任务 2.2，R26.2 / R26.3；design.md Testing Strategy §2）。

`domain_snapshots` 被六条属性（1、2、4、10、21、37）共用，因此它自己必须先被证明正确
——design.md Testing Strategy §2 明确要求「为它自己写一组自检测试（断言产出的快照通过
引用完整性预检）」。若生成器会产出引用不完整的快照，那六条属性的第一步 `load → precheck`
就会整批抛 `DataIntegrityError`，属性测试会以「输入非法」的名义变红或空过，而真正的 bug
反而查不到。这组自检把生成器的四条内部一致性承诺逐条钉死。

这些测试本身也用 Hypothesis 驱动（对生成器抽样再断言），但它们不是设计文档里的 7 条
属性之一——它们是**生成器的单元测试**，恰好用属性风格书写。
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, find, given, settings
from hypothesis import strategies as st

from app.core.snapshot import (
    MAX_OPERATION_SEQUENCE,
    check_referential_integrity,
    require_referential_integrity,
)
from tests.generators import (
    AdversarialAgentOutput,
    ApprovalRequest,
    DirtySpreadsheet,
    adversarial_agent_outputs,
    approval_request_sequences,
    assert_no_llm_budget_consumed,
    dirty_spreadsheets,
    domain_snapshots,
)

# 生成器构造成本略高（一次抽出整份快照），放宽 deadline 并抑制「过慢」健康检查——
# 慢不是错误，只有正确性才是这里要守的。
_SETTINGS = settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# --------------------------------------------------------------------------
# ① domain_snapshots 的核心承诺：引用完整（design.md 点名的自检）
# --------------------------------------------------------------------------


@_SETTINGS
@given(domain_snapshots())
def test_default_snapshots_pass_referential_integrity_precheck(snapshot: object) -> None:
    """默认参数下产出的快照通过任务 2.1 的引用完整性预检——这是 design.md 点名的断言。"""
    assert check_referential_integrity(snapshot) == ()  # type: ignore[arg-type]
    # require_ 返回原对象即代表通过；它抛异常才是失败。
    assert require_referential_integrity(snapshot) is snapshot  # type: ignore[arg-type]


@_SETTINGS
@given(
    domain_snapshots(scarcity=st.shared(st.just("ABUNDANT")))  # type: ignore[arg-type]
    if False  # 下面用显式三档，保留此行说明 scarcity 是关键字参数而非策略
    else st.one_of(
        domain_snapshots(scarcity="ABUNDANT"),
        domain_snapshots(scarcity="TIGHT"),
        domain_snapshots(scarcity="INFEASIBLE"),
    )
)
def test_all_scarcity_levels_still_pass_precheck(snapshot: object) -> None:
    """三档 scarcity 下引用完整性都成立——scarcity 只调物料数量，不该破坏引用闭合。"""
    assert check_referential_integrity(snapshot) == ()  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# ② 路线合法（R4.1）：1–3 道工序、sequence 为连续前缀、能力/技能与机型匹配
# --------------------------------------------------------------------------


@_SETTINGS
@given(domain_snapshots())
def test_routing_is_legal(snapshot: object) -> None:
    """每个产品 1–3 道工序，`sequence` 恰为 `1..k` 连续前缀，无重复、无越界（R4.1）。"""
    for product in snapshot.products:  # type: ignore[attr-defined]
        sequences = [op.sequence for op in product.operations]
        assert 1 <= len(sequences) <= MAX_OPERATION_SEQUENCE
        assert sorted(sequences) == list(range(1, len(sequences) + 1)), (
            f"{product.product_id} 的 sequence 不是连续前缀：{sequences}"
        )


@_SETTINGS
@given(domain_snapshots())
def test_operation_capabilities_are_consistent_with_a_present_machine_type(snapshot: object) -> None:
    """每道工序要求的机型都有机器存在，且要求的能力是该机型能力集的成员（或无要求）。

    这条守的是「路线合法」在能力维度的部分：若工序要求一个没有任何机器持有的能力，那类
    作业永远不可排产，属性 2 的可行侧会被稀释成噪声。
    """
    from tests.generators import _CAPABILITIES  # noqa: PLC0415  仅测试内省用

    present_types = {m.machine_type for m in snapshot.machines}  # type: ignore[attr-defined]
    for product in snapshot.products:  # type: ignore[attr-defined]
        for op in product.operations:
            assert op.required_machine_type in present_types, (
                f"{product.product_id} 工序要求机型 {op.required_machine_type} 但无此机器"
            )
            if op.required_capability is not None:
                assert op.required_capability in _CAPABILITIES[op.required_machine_type]


# --------------------------------------------------------------------------
# ③ 班次合理：shift_start < shift_end，缺勤/停机窗落在区间内、非零长、互不重叠
# --------------------------------------------------------------------------


@_SETTINGS
@given(domain_snapshots())
def test_shifts_and_windows_are_sensible(snapshot: object) -> None:
    for worker in snapshot.workers:  # type: ignore[attr-defined]
        assert worker.shift_start < worker.shift_end
        _assert_windows_disjoint_and_within(
            [(a.start, a.end) for a in worker.absences], worker.shift_start, worker.shift_end
        )
    for machine in snapshot.machines:  # type: ignore[attr-defined]
        assert machine.available_start < machine.available_end
        _assert_windows_disjoint_and_within(
            [(d.start, d.end) for d in machine.downtime_windows],
            machine.available_start,
            machine.available_end,
        )


def _assert_windows_disjoint_and_within(windows, lo, hi) -> None:  # type: ignore[no-untyped-def]
    ordered = sorted(windows)
    prev_end = None
    for start, end in ordered:
        assert start < end, "窗长度必须为正"
        assert lo <= start and end <= hi, "窗必须落在父区间内"
        if prev_end is not None:
            assert start >= prev_end, "窗互不重叠"
        prev_end = end


# --------------------------------------------------------------------------
# ④ 物料按 scarcity 调节：三档丰俭确实不同
# --------------------------------------------------------------------------


@_SETTINGS
@given(domain_snapshots(scarcity="INFEASIBLE"))
def test_infeasible_scarcity_zeroes_out_material(snapshot: object) -> None:
    """`INFEASIBLE`：全部物料可用量为 0 且无到货——需要物料的作业必然缺料。"""
    for material in snapshot.materials:  # type: ignore[attr-defined]
        assert material.quantity_available == Decimal("0")
        assert material.reserved_quantity == Decimal("0")
        assert material.incoming_deliveries == ()


@_SETTINGS
@given(domain_snapshots(scarcity="ABUNDANT"))
def test_abundant_scarcity_gives_large_stock(snapshot: object) -> None:
    """`ABUNDANT`：可用量远超任何演示量级的需求，物料从不成为瓶颈。"""
    for material in snapshot.materials:  # type: ignore[attr-defined]
        assert material.quantity_available >= Decimal("10000")


# --------------------------------------------------------------------------
# ⑤ 计数参数被尊重
# --------------------------------------------------------------------------


@_SETTINGS
@given(domain_snapshots(n_orders=(3, 3), n_machines=(2, 2), n_workers=(4, 4)))
def test_count_bounds_are_respected(snapshot: object) -> None:
    """`(min, max)` 相等时得到精确计数——属性测试要能把输入规模钉死。"""
    assert len(snapshot.orders) == 3  # type: ignore[attr-defined]
    assert len(snapshot.machines) == 2  # type: ignore[attr-defined]
    assert len(snapshot.workers) == 4  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# ⑥ approval_request_sequences：三类攻击面都可能出现，且形状可被属性 15 消费
# --------------------------------------------------------------------------


@_SETTINGS
@given(approval_request_sequences())
def test_request_sequences_are_well_formed(sequence: object) -> None:
    """每条请求都是 `ApprovalRequest`，`TOOL_CALL` 必带 `tool_name`，其余不带。"""
    seq = tuple(sequence)  # type: ignore[arg-type]
    assert 1 <= len(seq) <= 8
    for req in seq:
        assert isinstance(req, ApprovalRequest)
        if req.kind == "TOOL_CALL":
            assert req.tool_name is not None
        else:
            assert req.tool_name is None


def _has_patch_attack(sequence: tuple[ApprovalRequest, ...]) -> bool:
    """序列含「直接 PATCH status=ACTIVE」攻击（EVAL-207）。"""
    return any(req.kind == "PATCH_STATUS_ACTIVE" for req in sequence)


def _has_unauthorized_tool_attack(sequence: tuple[ApprovalRequest, ...]) -> bool:
    """序列含「越权写工具调用」攻击（非 PLANNER_API 调用方调写工具，EVAL-210）。"""
    write_tools = {"save_proposed_plan", "register_disruption", "save_import_batch"}
    return any(
        req.kind == "TOOL_CALL"
        and req.tool_name in write_tools
        and req.caller in {"INGESTION_AGENT", "RISK_MONITOR_AGENT"}
        for req in sequence
    )


def _has_concurrent_approve_attack(sequence: tuple[ApprovalRequest, ...]) -> bool:
    """序列含「并发 approve」攻击：同组、同计划、同 expected_version 的两条 APPROVE。"""
    approves: dict[tuple[str, int, int], int] = {}
    for req in sequence:
        if req.kind == "APPROVE" and req.concurrent_group is not None:
            key = (req.plan_id, req.expected_version, req.concurrent_group)
            approves[key] = approves.get(key, 0) + 1
            if approves[key] >= 2:
                return True
    return False


@pytest.mark.parametrize(
    ("name", "predicate"),
    [
        ("直接 PATCH status=ACTIVE", _has_patch_attack),
        ("越权工具调用", _has_unauthorized_tool_attack),
        ("并发 approve", _has_concurrent_approve_attack),
    ],
)
def test_generator_can_surface_each_attack_class(
    name: str,
    predicate: Callable[[tuple[ApprovalRequest, ...]], bool],
) -> None:
    """`approval_request_sequences` **可以**产出三类攻击的每一类（design.md Testing Strategy §2）。

    用 Hypothesis 的 `find(strategy, predicate)` —— 它在生成器的取值空间里**确定性地搜索**一个
    满足 `predicate` 的样例，找不到则抛 `NoSuchExample`。这直接证明「生成器能产出该攻击类」这一
    断言意图，且**不依赖**一次 `@given` 随机跑的抽样预算或用例执行顺序：此前用模块级累加器跨
    400 次随机抽样凑齐三类的写法，对稀有的「并发 approve」类在完整套件里排到上千个用例之后时
    会偶发漏采而 flaky——本写法把它变成确定性的存在性证明，断言强度不减反增（每类都必须真的
    被生成器命中，否则立即失败）。若哪一类的取值域或加权被误改掉，`find` 会立刻抛错。
    """
    found = find(approval_request_sequences(), predicate)
    assert predicate(found), f"生成器未能产出攻击类：{name}"


# --------------------------------------------------------------------------
# ⑦ 保留生成器仍能产出合理形状（它们服务固化用例集，非属性测试）
# --------------------------------------------------------------------------


@_SETTINGS
@given(dirty_spreadsheets())
def test_dirty_spreadsheets_have_rectangular_shape(sheet: object) -> None:
    """脏表格每行列数与表头一致——脏在内容不在结构错位。"""
    assert isinstance(sheet, DirtySpreadsheet)
    n_cols = len(sheet.headers)
    for row in sheet.rows:
        assert len(row) == n_cols


@_SETTINGS
@given(adversarial_agent_outputs())
def test_adversarial_outputs_carry_raw_string(output: object) -> None:
    """对抗输出始终带 `raw` 字符串；含保留键时 `parsed_keys` 非空。"""
    assert isinstance(output, AdversarialAgentOutput)
    assert isinstance(output.raw, str)
    if output.parsed_keys:
        for key in output.parsed_keys:
            assert key in output.raw


# --------------------------------------------------------------------------
# ⑧ LLM 额度守卫
# --------------------------------------------------------------------------


def test_property_tests_never_consume_llm_budget(valid_env: pytest.MonkeyPatch) -> None:
    """`conftest.py` 的 `valid_env` 把测试期 LLM_MODE 强制为 STUB；额度守卫因此应通过。

    依赖 `valid_env` 夹具：它注入 `LLM_MODE=STUB` 与必需字段，并清空配置缓存，使
    `get_settings()` 在无外部 env 时也能通过 Settings 校验。
    """
    from app.settings import get_settings

    # 该断言只对「不消耗额度」的取值通过。测试进程内必为 STUB。
    assert_no_llm_budget_consumed(get_settings().llm_mode)
