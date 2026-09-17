"""契约测试用的代表性 `ToolSpec` 与 handler（任务 5.1）。

## 为什么 fixture 住在测试树里而不是 `app/tools/handlers/`

5.1 交付的是**机具**——`invoke()` 的 7 步闸门、白名单、投影、截断、记账。具体工具的
`input_model` / `output_model` / `handler` 是 5.2 的活。为了让机具**现在**可测而又不把
占位代码混进 `app/`（那会让 5.2 落地时要先删占位、也会让分层扫描把占位当真 handler），
代表性 spec 放在这里。

这些 fixture 覆盖机具需要区分的每一类工具形态：
- **可投影的分页只读工具**（`get_orders` 形态）——投影子集断言与分页并集/不交断言的载体；
- **句柄式返回**（`PlanHandle` 形态，无 array 字段）——ADR-004 的类型层防线；
- 每个真实工具名一个最小 spec——让「4 caller × 全部工具」的白名单矩阵能真的调 `invoke()`，
  从而断言「越权时 handler 根本不被调用」而不只是「返回了错误码」。

5.2 落地后，真 spec 直接替换这里的 fixture，契约测试的断言逻辑一字不改——这正是把 spec
构造与注册表解耦（构造时注入 `specs`）想要的结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from app.tools.registry import (
    ALL_TOOLS,
    READ_ONLY_TOOLS,
    ToolContext,
    ToolSpec,
)

# --------------------------------------------------------------------------
# 数据：一个稳定的「订单」宇宙，供分页/投影 fixture 使用
# --------------------------------------------------------------------------

#: 25 条确定性的订单行。分页断言要遍历 3 页并求并集，因此需要一个已知全集。
FIXTURE_ORDERS: tuple[dict[str, object], ...] = tuple(
    {
        "order_id": f"ORD-{i:03d}",
        "due_date": f"2026-03-{(i % 27) + 1:02d}",
        "priority": ["LOW", "NORMAL", "HIGH", "URGENT"][i % 4],
        "quantity": (i * 7) % 200 + 1,
    }
    for i in range(25)
)


# --------------------------------------------------------------------------
# 模型
# --------------------------------------------------------------------------


class OrderBrief(BaseModel):
    """一条订单的摘要。字段刻意多于分页断言需要的，好让投影有东西可投。"""

    model_config = ConfigDict(extra="forbid")

    order_id: str
    due_date: str
    priority: str
    quantity: int


class OrderListOut(BaseModel):
    """分页只读输出：`items` 声明 `maxItems`（第 3 道结构性保障要求的）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[OrderBrief] = Field(max_length=50)
    total: int
    truncated: bool = False


class OrderListIn(BaseModel):
    """分页 + 投影输入。`fields` 是投影请求集合。"""

    model_config = ConfigDict(extra="forbid")

    fields: list[str] | None = None
    limit: int = Field(default=20, le=50, ge=1)
    offset: int = Field(default=0, ge=0)


class ObjectiveSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_score: float
    late_order_count: int
    total_tardiness_minutes: int


class PlanHandle(BaseModel):
    """句柄 + 聚合值。**刻意无任何 array / 逐 ScheduledJob 字段**（ADR-004）。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    plan_version: int
    feasibility: str
    objective: ObjectiveSummary
    scheduled_job_count: int
    unschedulable_count: int
    trace_id: str


class EmptyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EchoOut(BaseModel):
    """给「每个工具名一个最小 spec」用的最简输出。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    tool: str


# --------------------------------------------------------------------------
# handler（带调用探针）
# --------------------------------------------------------------------------


@dataclass
class HandlerSpy:
    """记录哪些工具的 handler 真的被调用了。

    白名单矩阵断言的核心是「越权调用时 handler **根本不被触达**」——这不能只看返回的错误码
    （那可能是执行后才失败的），必须证明执行那一步没发生。探针记下每次真实调用，矩阵测试
    据此断言：被拒的 (caller, tool) 组合从不出现在探针里。
    """

    called: list[str] = field(default_factory=list)

    def clear(self) -> None:
        self.called.clear()


def make_specs(spy: HandlerSpy) -> dict[str, ToolSpec]:
    """为 `ALL_TOOLS` 里的每个工具名建一个最小 `ToolSpec`。

    `get_orders` 用真实的分页/投影 spec（投影与分页断言的载体）；`generate_schedule` 用句柄式
    输出（ADR-004 断言的载体）；其余工具用回显 spec——它们只需要「存在且可被 `invoke` 调用」，
    好让白名单矩阵覆盖到每一格。
    """

    def orders_handler(args: BaseModel, ctx: ToolContext) -> BaseModel:
        spy.called.append("get_orders")
        assert isinstance(args, OrderListIn)
        window = FIXTURE_ORDERS[args.offset : args.offset + args.limit]
        return OrderListOut(
            items=[OrderBrief(**row) for row in window],  # type: ignore[arg-type]
            total=len(FIXTURE_ORDERS),
            truncated=args.offset + args.limit < len(FIXTURE_ORDERS),
        )

    def handle_handler(args: BaseModel, ctx: ToolContext) -> BaseModel:
        spy.called.append("generate_schedule")
        return PlanHandle(
            plan_id="PLAN-0012",
            plan_version=1,
            feasibility="PARTIAL",
            objective=ObjectiveSummary(
                total_score=4187.5, late_order_count=2, total_tardiness_minutes=315
            ),
            scheduled_job_count=27,
            unschedulable_count=3,
            trace_id=ctx.trace_id,
        )

    def echo_handler_for(name: str):
        def handler(args: BaseModel, ctx: ToolContext) -> BaseModel:
            spy.called.append(name)
            return EchoOut(tool=name)

        return handler

    specs: dict[str, ToolSpec] = {}
    for name in ALL_TOOLS:
        if name == "get_orders":
            specs[name] = ToolSpec(
                name=name,
                kind="READ",
                input_model=OrderListIn,
                output_model=OrderListOut,
                handler=orders_handler,
                supports_projection=True,
            )
        elif name == "generate_schedule":
            specs[name] = ToolSpec(
                name=name,
                kind="COMPUTE",
                input_model=EmptyIn,
                output_model=PlanHandle,
                handler=handle_handler,
            )
        else:
            kind = "READ" if name in READ_ONLY_TOOLS else "COMPUTE"
            specs[name] = ToolSpec(
                name=name,
                kind=kind,  # type: ignore[arg-type]
                input_model=EmptyIn,
                output_model=EchoOut,
                handler=echo_handler_for(name),
                supports_projection=name in READ_ONLY_TOOLS,
            )
    return specs
