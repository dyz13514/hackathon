"""`ROUTING_TABLE`：意图 → 执行形态的**纯查表**（任务 5.7，design.md Architecture §2.3）。

`Orchestrator.run()` 的第一行是 `route = ROUTING_TABLE[intent]`——没有 LLM 参与选择，路由是
一次编译期就定死的字典查找（design.md §1 代码草图第一行的注释「纯查表，无 LLM 参与」）。这条
性质很重要：**哪个意图走流水线、哪个走 ReAct、用哪个 Agent、要不要强制预算上限，全部是常量**，
不受任何运行期状态影响。因此同一个意图永远路由到同一条路径，可复现、可断言（R5.7 的编排侧
延伸）。

## 两种执行形态（ADR-002）

- **形态 A（`Mode.PIPELINE`）**：确定性流水线。序列是我们已经知道的（例如计划生成的固定 6 步），
  让模型重新发现它是纯粹的 token 浪费。P0 只有 `GENERATE_PLAN` 一条。`Route.pipeline` 指向那条
  流水线的入口函数，`Route.agent` 为 `None`（流水线没有 Agent 在循环里）。
- **形态 B（`Mode.REACT`）**：Reason/Act/Observe 循环。仅用于 4 类**路线可变**的路径
  （design.md §2.2）：重排、列映射（P0 接线），自然语言 What-if 翻译、风险归因叙述（P1 留位）。
  `Route.agent` 指明由哪个 Agent 驱动，`Route.pipeline` 为 `None`。

`Mode.NONE` 用于既不走 LLM 也不走流水线内核的纯服务入口（审批、导出）。它们经 `Orchestrator`
只是为了统一记账与审计（Trace + budget 的 no-op 句柄），实际工作由对应服务完成。本任务不接线
这些服务的执行体——`Route.pipeline` 为 `None`，dispatch 时留一个显式的「未接线」信号（见
`orchestrator.py`），而不是假装能跑。

## 强制预算上限恰好 2 条（R25.2 封闭清单）

design.md §2.3 的路由表里，`budget_scope` 那一列只有两行非空：

- `GENERATE_PLAN` → `PLAN_GENERATION`（4,000 token，K-10）；
- `REPLAN`（及 `REGISTER_DISRUPTION`，两者共用重排路径）→ `REPLANNING`（14,000 token，K-16）。

**其余 10 条入口的 `budget_scope` 是 `None`**。这不是遗漏——requirements 第 3 节明确要求恰好
2 个预算作用域，其余路径靠逐次记账 + `MAX_AGENT_STEPS` 步数上限 + 每日 USD 上限三重约束兜住
（design.md 成本章节 §2）。`None` 一路传到 `TokenBudgetManager.open_scope`，后者返回 no-op
句柄，因此 `Orchestrator.run` 无条件调用、无需分支（budget.py 的刻意设计）。

## P1 三条路线：留位但不接线

`WHATIF_NL` / `RISK_NARRATION` / `DISTIL_PREFERENCE` 在表中给出完整路由（形态、Agent、
`budget_scope=None`），但它们的 ReAct 驱动在 P0 不接线——各自有确定性的 P0 前门（结构化场景
表单 / 模板叙述 / 手写偏好规则）。提前把路由定义好是零成本的（一行字典项），让 P1 落地时只需
接线 Agent 驱动，不必回头改这张表。P0 的 `Orchestrator` 若被要求跑这三条，会以「未接线」明确
拒绝，而不是静默走一条空路径。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Final

from app.llm.budget import BudgetScope

__all__ = [
    "ROUTING_TABLE",
    "AgentName",
    "Intent",
    "Mode",
    "Route",
]


#: ReAct 形态里驱动循环的 Agent 名（design.md ADR-001）。与 `context_manager.AgentName`
#: 同一集合；此处独立声明以免 `routing` 依赖 `context_manager` 的类型别名。
AgentName = str


class Mode(str, Enum):
    """一条路由的执行形态（design.md Architecture §2）。

    - `PIPELINE`：形态 A 确定性流水线（`Route.pipeline` 非空，`agent` 为 None）。
    - `REACT`：形态 B ReAct 循环（`Route.agent` 非空，`pipeline` 为 None）。
    - `NONE`：既非 LLM 也非内核流水线的纯服务入口（审批 / 导出）。经 `Orchestrator` 仅为
      统一记账；`agent` 与 `pipeline` 均为 None。
    """

    PIPELINE = "PIPELINE"
    REACT = "REACT"
    NONE = "NONE"


class Intent(str, Enum):
    """路由意图：`ROUTING_TABLE` 的键（design.md §2.3 路由表第一列）。

    取值就是 `Trace.kind` 写入的值（design.md Data Models §7 的 `kind` 列注释
    「GENERATE_PLAN / REPLAN / ...」）。因此 `Intent` 是编排入口的封闭枚举，新增一条路径
    = 在这里加一个成员并在 `ROUTING_TABLE` 补一行，二者的一致性由本模块末尾的断言强制。
    """

    # 形态 A：确定性流水线（P0）
    GENERATE_PLAN = "GENERATE_PLAN"
    # 形态 B：ReAct（P0 接线两条）
    REGISTER_DISRUPTION = "REGISTER_DISRUPTION"
    REPLAN = "REPLAN"
    INGEST_MAPPING = "INGEST_MAPPING"
    # 形态 B：ReAct（P1 留位，P0 不接线）
    WHATIF_NL = "WHATIF_NL"
    RISK_NARRATION = "RISK_NARRATION"
    DISTIL_PREFERENCE = "DISTIL_PREFERENCE"
    # 无 LLM 的纯服务入口（经 Orchestrator 只为统一记账）
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    MODIFY = "MODIFY"
    EXPORT_PLAN = "EXPORT_PLAN"
    # 触发风险扫描（P0 确定性：Risk_Scanner + 模板；无 LLM 编排）
    SCAN_RISKS = "SCAN_RISKS"


#: 形态 A 流水线入口的类型。签名放宽为 `(payload, trace, budget) -> Any`——本任务不接线
#: 具体流水线（`plan_generation` 的入口签名是 `run_plan_generation(session, ...)`，与这里的
#: `(payload, trace, budget)` 草图不同，接线属任务 5.11/后续），因此 `ROUTING_TABLE` 里
#: `pipeline` 恒为 `None`，dispatch 时以「未接线」信号处理（见 orchestrator.py）。类型留在
#: 这里是为了让草图与 design.md §1 的 `route.pipeline(payload, trace, budget)` 对齐。
PipelineFn = Callable[..., Any]


@dataclass(frozen=True)
class Route:
    """一条路由的全部静态属性（design.md §2.3 一行）。

    `frozen=True`：路由是常量，装配后不可改。四个字段的互斥关系由 `__post_init__` 校验，
    使一条自相矛盾的路由（例如 `mode=REACT` 却没有 `agent`）在 import 期就炸出来，而不是
    等到运行时 dispatch 才发现。
    """

    mode: Mode
    #: ReAct 形态下驱动循环的 Agent 名；非 ReAct 时为 None。
    agent: AgentName | None = None
    #: 强制预算作用域名。12 条入口中只有 2 条非空（R25.2）；其余为 None → no-op 句柄。
    budget_scope: BudgetScope | None = None
    #: 形态 A 流水线入口。本任务不接线，恒为 None（见 PipelineFn 注释）。
    pipeline: PipelineFn | None = None

    def __post_init__(self) -> None:
        if self.mode is Mode.REACT and self.agent is None:
            raise ValueError("Mode.REACT 的路由必须指定 agent")
        if self.mode is not Mode.REACT and self.agent is not None:
            raise ValueError("只有 Mode.REACT 的路由才应指定 agent")
        if self.mode is not Mode.PIPELINE and self.pipeline is not None:
            raise ValueError("只有 Mode.PIPELINE 的路由才应指定 pipeline")


#: 意图 → 路由的纯查表（design.md §2.3）。`MappingProxyType` 冻结：运行期不可增删，新增
#: 路径只能改这段源码——与 `TOOL_WHITELIST` / `BUDGETS` 同一「能力集合是启动期常量」的纪律。
#:
#: 只有 `GENERATE_PLAN`（PLAN_GENERATION）与 `REGISTER_DISRUPTION` / `REPLAN`（REPLANNING）
#: 有强制预算上限；其余 10 条 `budget_scope=None`。P1 三条（WHATIF_NL / RISK_NARRATION /
#: DISTIL_PREFERENCE）留位但驱动不接线。
ROUTING_TABLE: Final = MappingProxyType(
    {
        # 形态 A：计划生成流水线（P0） —— 强制上限 PLAN_GENERATION
        Intent.GENERATE_PLAN: Route(mode=Mode.PIPELINE, budget_scope="PLAN_GENERATION"),
        # 形态 B：重排（P0） —— REGISTER_DISRUPTION 与 REPLAN 共用重排路径，强制上限 REPLANNING
        Intent.REGISTER_DISRUPTION: Route(
            mode=Mode.REACT, agent="PLANNING_AGENT", budget_scope="REPLANNING"
        ),
        Intent.REPLAN: Route(
            mode=Mode.REACT, agent="PLANNING_AGENT", budget_scope="REPLANNING"
        ),
        # 形态 B：列映射（P0） —— 无强制上限（步数 ≤6 + 每日上限）
        Intent.INGEST_MAPPING: Route(mode=Mode.REACT, agent="INGESTION_AGENT"),
        # 形态 B：P1 留位（P0 不接线） —— 无强制上限
        Intent.WHATIF_NL: Route(mode=Mode.REACT, agent="PLANNING_AGENT"),
        Intent.RISK_NARRATION: Route(mode=Mode.REACT, agent="RISK_MONITOR_AGENT"),
        Intent.DISTIL_PREFERENCE: Route(mode=Mode.REACT, agent="PLANNING_AGENT"),
        # 无 LLM 的纯服务入口 —— 无预算作用域
        Intent.APPROVE: Route(mode=Mode.NONE),
        Intent.REJECT: Route(mode=Mode.NONE),
        Intent.MODIFY: Route(mode=Mode.NONE),
        Intent.EXPORT_PLAN: Route(mode=Mode.NONE),
        Intent.SCAN_RISKS: Route(mode=Mode.NONE),
    }
)

# `ROUTING_TABLE` 的键必须与 `Intent` 的成员集合逐一对应——漏配一条意图会让 `run()` 在该
# 意图上 KeyError，而那本该在启动/测试期就暴露。import 期断言把漏配变成即时失败。
assert frozenset(ROUTING_TABLE) == frozenset(Intent), (
    "ROUTING_TABLE 的键必须覆盖全部 Intent 成员（纯查表的完备性）"
)
