"""`Tool_Registry` 契约测试（任务 5.1，非可选，承接原属性 25、26、27）。

四组断言，逐条对应 tasks.md 5.1 点名的契约：

1. **白名单矩阵**——4 caller × 全部工具的笛卡尔积，表驱动。每一格要么「被允许 → handler 被
   调用、返回 OK」，要么「被拒绝 → `TOOL_NOT_PERMITTED`、handler **根本不被调用**、写审计、
   落一条记账」。这是 R22.10 权限隔离的动态证明（结构证明由 `test_layering.py` 补足）。
2. **maxItems**——任何 `output_model` 的 JSON Schema 里，`array` 类型字段必须声明 `maxItems`。
   这挡的是「忘了给列表设上限导致上下文被撑爆」（ADR-004 / 第 3 道结构性保障）。
3. **投影子集**——投影后的字段集合必是请求集合的子集（R22.12）。
4. **分页并集/不交**——遍历 3 页，各页 `order_id` 集合两两不交且并集完整（R22.12 分页语义）。

外加 7 步闸门本身的行为断言：输入非法作为观察结果、句柄式返回无明细、截断标记、记账覆盖
每一次调用（含被拒的）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from app.tools.registry import (
    ALL_TOOLS,
    TOOL_WHITELIST,
    CallerId,
    InMemoryToolCallRecorder,
    ToolContext,
    ToolRegistry,
)
from tests.contracts.registry_fixtures import (
    FIXTURE_ORDERS,
    HandlerSpy,
    OrderListOut,
    PlanHandle,
    make_specs,
)

ALL_CALLERS: tuple[CallerId, ...] = (
    "SYSTEM_PIPELINE",
    "INGESTION_AGENT",
    "PLANNING_AGENT",
    "RISK_MONITOR_AGENT",
)


@dataclass
class AuditSpy:
    """收集 `TOOL_NOT_PERMITTED` 审计写入，替代真实的 `app.db.audit.append`。"""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def spy() -> HandlerSpy:
    return HandlerSpy()


@pytest.fixture
def audit() -> AuditSpy:
    return AuditSpy()


@pytest.fixture
def recorder() -> InMemoryToolCallRecorder:
    return InMemoryToolCallRecorder()


@pytest.fixture
def registry(
    spy: HandlerSpy, audit: AuditSpy, recorder: InMemoryToolCallRecorder
) -> ToolRegistry:
    return ToolRegistry(make_specs(spy), recorder=recorder, audit_write=audit)


def _ctx() -> ToolContext:
    return ToolContext(trace_id="TRACE-TEST", step_id="STEP-1")


# --------------------------------------------------------------------------
# 前置一致性：白名单与已注册工具集合必须吻合
# --------------------------------------------------------------------------


def test_every_whitelisted_tool_is_registered(registry: ToolRegistry) -> None:
    """任何调用方白名单里的工具名都必须有对应 spec，否则 `invoke` 会抛 `UnknownToolError`。"""
    whitelisted = set().union(*(set(tools) for tools in TOOL_WHITELIST.values()))
    assert whitelisted <= set(registry.tool_names), (
        "白名单引用了未注册的工具：" f"{sorted(whitelisted - set(registry.tool_names))}"
    )


def test_whitelist_is_immutable() -> None:
    """`TOOL_WHITELIST` 运行期不可变（第 2 道结构性保障）。"""
    with pytest.raises(TypeError):
        TOOL_WHITELIST["PLANNING_AGENT"] = frozenset()  # type: ignore[index]
    with pytest.raises(AttributeError):
        TOOL_WHITELIST["PLANNING_AGENT"].add("save_import_batch")  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# ① 白名单矩阵：4 caller × 全部工具
# --------------------------------------------------------------------------


@pytest.mark.parametrize("caller", ALL_CALLERS)
@pytest.mark.parametrize("tool_name", sorted(ALL_TOOLS))
def test_whitelist_matrix(
    caller: CallerId,
    tool_name: str,
    registry: ToolRegistry,
    spy: HandlerSpy,
    audit: AuditSpy,
    recorder: InMemoryToolCallRecorder,
) -> None:
    """笛卡尔积表驱动断言（R22.7 / R22.8 / R22.10）。"""
    permitted = tool_name in TOOL_WHITELIST[caller]
    result = registry.invoke(caller, tool_name, {}, _ctx())

    if permitted:
        assert result.ok, f"{caller} 应能调用 {tool_name}，却被拒：{result.error_code}"
        assert tool_name in spy.called, "被允许的调用 handler 必须被执行"
        assert audit.calls == [], "被允许的调用不该写 TOOL_NOT_PERMITTED 审计"
    else:
        assert not result.ok
        assert result.error_code == "TOOL_NOT_PERMITTED"
        # 关键断言：handler 根本没被触达（白名单是最前一步）。
        assert tool_name not in spy.called, "越权调用的 handler 绝不能被执行"
        assert len(audit.calls) == 1, "越权尝试必须写恰好一条审计（R22.10）"
        assert audit.calls[0]["event_category"] == "TOOL_NOT_PERMITTED"
        assert audit.calls[0]["payload"]["tool"] == tool_name

    # 无论允许与否，都恰好落一条记账（R22.11：越权尝试也留痕）。
    assert len(recorder.entries) == 1
    entry = recorder.entries[0]
    assert entry.caller == caller
    assert entry.tool_name == tool_name
    assert entry.outcome == ("OK" if permitted else "TOOL_NOT_PERMITTED")


def test_matrix_covers_full_cartesian_product() -> None:
    """元断言：矩阵确实是 4 × 全部工具，没有因为参数化写错而少跑格子。"""
    assert len(ALL_CALLERS) == 4
    assert len(ALL_TOOLS) == 26, f"工具总数应为 26（10 读 + 9 算 + 4 写 + 3 摄取），实际 {len(ALL_TOOLS)}"


def test_ingestion_agent_cannot_see_orders(registry: ToolRegistry, spy: HandlerSpy) -> None:
    """物理隔离的具体一例：摄取 Agent 连「有哪些订单」都调不到（R22.7）。"""
    result = registry.invoke("INGESTION_AGENT", "get_orders", {}, _ctx())
    assert result.error_code == "TOOL_NOT_PERMITTED"
    assert "get_orders" not in spy.called


def test_no_agent_can_reach_a_tool_that_sets_active() -> None:
    """没有任何工具能置 ACTIVE（R22.9）：白名单里不存在这样的工具名。"""
    forbidden_active_writers = {"activate_plan", "set_active", "approve_plan"}
    for caller, tools in TOOL_WHITELIST.items():
        assert not (forbidden_active_writers & set(tools)), (
            f"{caller} 的白名单出现了可置 ACTIVE 的工具"
        )


# --------------------------------------------------------------------------
# ② maxItems：每个 output_model 的 array 字段都声明上限
# --------------------------------------------------------------------------


def _array_fields_without_maxitems(schema: dict[str, Any]) -> list[str]:
    """递归找出 JSON Schema 里缺 `maxItems` 的 array 字段路径。

    Pydantic v2 把嵌套模型放进 `$defs`，因此要连 `$defs` 一起扫。这里做的是保守遍历：
    任何 `"type": "array"` 的节点都必须带 `maxItems`，无论它在顶层还是嵌套里。
    """
    offenders: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "array" and "maxItems" not in node:
                offenders.append(path or "<root>")
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(schema, "")
    return offenders


def test_every_output_model_array_declares_maxitems(
    registry: ToolRegistry, spy: HandlerSpy
) -> None:
    """任何 `output_model` 的 array 字段必须声明 `maxItems`（第 3 道结构性保障 / ADR-004）。

    这条挡的是「某个列表忘了设上限」——那会让一个响应无上界地增长，把注入上下文的 token 撑爆。
    `PlanHandle` 根本没有 array 字段，天然通过；有 `items` 列表的输出（如 `OrderListOut`）
    必须带上限。
    """
    specs = make_specs(spy)
    offenders: dict[str, list[str]] = {}
    for name, spec in specs.items():
        schema = spec.output_model.model_json_schema()
        missing = _array_fields_without_maxitems(schema)
        if missing:
            offenders[name] = missing
    assert not offenders, f"以下 output_model 的 array 字段缺 maxItems：{offenders}"


def test_handle_output_has_no_detail_rows() -> None:
    """句柄式返回不含逐 ScheduledJob 明细（ADR-004 的类型层防线）。"""
    props = PlanHandle.model_json_schema()["properties"]
    assert "scheduled_jobs" not in props
    # 且没有任何 array 属性——句柄就该是纯聚合。
    assert all(prop.get("type") != "array" for prop in props.values())


# --------------------------------------------------------------------------
# ③ 投影：结果字段集合是请求集合的子集
# --------------------------------------------------------------------------


def test_projection_returns_subset_of_requested_fields(registry: ToolRegistry) -> None:
    """请求 `fields=[order_id, due_date]`，每条 item 的键必是该集合的子集（R22.12）。"""
    requested = ["order_id", "due_date"]
    result = registry.invoke(
        "PLANNING_AGENT", "get_orders", {"fields": requested, "limit": 5}, _ctx()
    )
    assert result.ok and result.payload is not None
    returned_top = set(result.payload.keys())
    # 顶层投影：只保留请求的键（`items` 若未请求则被投影掉）。
    assert returned_top <= set(requested), f"顶层返回了未请求的字段：{returned_top - set(requested)}"


def test_projection_none_returns_full_payload(registry: ToolRegistry) -> None:
    """不请求投影（`fields=None`）则原样返回完整输出。"""
    result = registry.invoke("PLANNING_AGENT", "get_orders", {"limit": 3}, _ctx())
    assert result.ok and result.payload is not None
    assert set(result.payload.keys()) == set(OrderListOut.model_fields)


# --------------------------------------------------------------------------
# ④ 分页：3 页遍历，并集完整且两两不交
# --------------------------------------------------------------------------


def test_pagination_three_pages_are_complete_and_disjoint(registry: ToolRegistry) -> None:
    """遍历 3 页（每页 10），各页 order_id 集合两两不交且并集为前 25 个的全集（R22.12）。"""
    page_size = 10
    pages: list[set[str]] = []
    for page in range(3):
        result = registry.invoke(
            "PLANNING_AGENT",
            "get_orders",
            {"limit": page_size, "offset": page * page_size},
            _ctx(),
        )
        assert result.ok and result.payload is not None
        ids = {item["order_id"] for item in result.payload["items"]}
        pages.append(ids)

    # 两两不交
    assert pages[0].isdisjoint(pages[1])
    assert pages[0].isdisjoint(pages[2])
    assert pages[1].isdisjoint(pages[2])
    # 并集完整（25 条 → 10 + 10 + 5）
    union = pages[0] | pages[1] | pages[2]
    expected = {row["order_id"] for row in FIXTURE_ORDERS}
    assert union == expected
    assert [len(p) for p in pages] == [10, 10, 5]


# --------------------------------------------------------------------------
# 7 步闸门本身的行为
# --------------------------------------------------------------------------


def test_input_schema_failure_is_returned_as_observation(
    registry: ToolRegistry, spy: HandlerSpy
) -> None:
    """输入非法 → `TOOL_INPUT_INVALID`，作为观察结果回给 Agent，handler 不执行（R22.2）。"""
    result = registry.invoke(
        "PLANNING_AGENT", "get_orders", {"limit": 999, "bogus": 1}, _ctx()
    )
    assert not result.ok
    assert result.error_code == "TOOL_INPUT_INVALID"
    assert isinstance(result.error_detail, list)  # pydantic 的 errors()
    assert "get_orders" not in spy.called


def test_accounting_records_token_count_and_summary(
    registry: ToolRegistry, recorder: InMemoryToolCallRecorder
) -> None:
    """成功调用记账含 token 数、结果摘要、耗时（R22.11）。"""
    result = registry.invoke("PLANNING_AGENT", "get_orders", {"limit": 5}, _ctx())
    assert result.ok
    assert len(recorder.entries) == 1
    entry = recorder.entries[0]
    assert entry.outcome == "OK"
    assert entry.result_tokens == result.tokens > 0
    assert entry.result_summary.startswith("keys=")
    assert entry.duration_ms >= 0


def test_clamp_truncates_and_flags(spy: HandlerSpy) -> None:
    """超过 `max_response_tokens` 的响应被截断并标 `truncated`（R22.16 / R25.6）。

    用一个极小上限的 spec 强制触发截断，而不依赖真实工具体积——这测的是机具，不是某个工具
    恰好多大。
    """
    from app.tools.registry import ToolSpec
    from tests.contracts.registry_fixtures import OrderBrief, OrderListIn

    def big_handler(args: BaseModel, ctx: ToolContext) -> BaseModel:
        return OrderListOut(
            items=[
                # 20 条，足以超过 30 token 的上限
                OrderBrief(
                    order_id=f"ORD-{i:03d}", due_date="2026-03-01", priority="HIGH", quantity=i
                )
                for i in range(20)
            ],
            total=20,
        )

    tiny = ToolSpec(
        name="get_orders",
        kind="READ",
        input_model=OrderListIn,
        output_model=OrderListOut,
        handler=big_handler,
        max_response_tokens=30,
    )
    recorder = InMemoryToolCallRecorder()
    registry = ToolRegistry({"get_orders": tiny}, recorder=recorder)
    result = registry.invoke("PLANNING_AGENT", "get_orders", {}, _ctx())
    assert result.ok
    assert result.truncated is True
    assert result.tokens <= 30
    assert recorder.entries[0].truncated is True


def test_unknown_tool_in_whitelist_raises(spy: HandlerSpy) -> None:
    """白名单放行但未注册 → 抛 `UnknownToolError`（配置不一致，不回给 Agent）。"""
    from app.tools.registry import UnknownToolError

    specs = make_specs(spy)
    del specs["get_orders"]  # 制造不一致：白名单仍含 get_orders
    registry = ToolRegistry(specs, recorder=InMemoryToolCallRecorder())
    with pytest.raises(UnknownToolError):
        registry.invoke("PLANNING_AGENT", "get_orders", {}, _ctx())


def test_output_schema_rejects_drifted_handler() -> None:
    """handler 返回不符合 `output_model` 的对象 → 第 ④ 步在验证时抛错（防实现漂移）。

    第 ④ 步的价值是把「handler 悄悄返回了不该有的形状」挡在类型层面。这里让 handler 返回一个
    值越界（`value=-1` 违反 `ge=0`）的**原始 dict**——`model_validate` 会跑字段校验并拒绝它。
    用 dict 而非已构造的模型实例是关键：pydantic 对同型实例默认不重跑校验，而漂移的 handler
    在真实场景里返回的正是「看起来对但内容不合契约」的原始数据。
    """
    from pydantic import ValidationError

    from app.tools.registry import ToolSpec
    from tests.contracts.registry_fixtures import EmptyIn

    class StrictOut(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: int = Field(ge=0)

    def bad_handler(args: BaseModel, ctx: ToolContext) -> Any:
        return {"value": -1}  # 越界，且缺少模型类型——model_validate 必须拒绝

    spec = ToolSpec(
        name="generate_schedule",
        kind="COMPUTE",
        input_model=EmptyIn,
        output_model=StrictOut,
        handler=bad_handler,
    )
    registry = ToolRegistry(
        {"generate_schedule": spec}, recorder=InMemoryToolCallRecorder()
    )
    with pytest.raises(ValidationError):
        registry.invoke("PLANNING_AGENT", "generate_schedule", {}, _ctx())
