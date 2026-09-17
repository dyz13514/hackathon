"""Agent 输出契约（任务 5.6，ADR-001、R21.3、design.md §3.3）。

## 这里的契约 ≠ `app/tools/models.py` 里的工具契约

同名的 `ColumnMappingProposal` 在本仓库有两个：
- `app/tools/models.py::ColumnMappingProposal` 是 **工具 `propose_column_mapping` 的输出模型**
  ——它是 `Ingestion_Agent` 在某一 ReAct 步里调用工具后拿到的观察结果的形状。
- 本模块的 `ColumnMappingProposal` 是 **Agent 整轮运行的最终输出契约**（`{"final": {...}}`
  里那个 `...`）——它是 `handoff()` 唯一接受的载荷类型（design.md §3.3 的 `HANDOFF_CONTRACTS`）。

两者刻意分离：工具输出可以随 handler 实现演化，而 Agent 输出契约是**跨 Agent 边界的稳定接口**，
其字节形状被 `handoff()` 序列化后可能进入下一个消费者（人工确认 UI、审批流、模板渲染）。把它们
绑成同一个类会让「工具返回多了个字段」意外改变跨边界接口。本模块因此独立声明，不 `import` 工具
模型（也不能——Agent 层只能 import `app.tools.registry` / `app.tools.models` 的契约，但把工具
输出直接当 Agent 输出会模糊 R21.3 的边界）。

## `untrusted_field_names()`：自由文本字段的显式声明（R21.3、R23.2）

跨 Agent 传递时，`handoff()` 只序列化契约声明的字段；其中**自由文本**字段（模型生成的、可能被
提示注入污染的散文）必须显式登记，`Context_Manager` 在把它们注入下一段上下文前用
`<untrusted source="...">` 包裹（R23.2）。登记的机制是每个契约声明一个类级
`UNTRUSTED_FIELDS: ClassVar[frozenset[str]]`，并由基类的 `untrusted_field_names()` 暴露。

为什么用「白名单登记」而不是「按类型自动判定」（例如「凡 `str` 皆不受信任」）：契约里也有
**受信任的 `str`**——`plan_id`、`entity_type` 这类是系统内部生成的标识符/枚举，包裹它们既无意义
又会污染下游解析。哪些字段是「模型自由发挥的散文」是**语义**判断，只能显式声明。声明也让
`test` 能逐契约断言「该包的都包了、不该包的没混进来」。

## `extra="forbid"` + `frozen=True`

- `extra="forbid"`：契约就是给 LLM 看的 JSON Schema（`model_json_schema()` 进 [OUTPUT] 段）。
  多一个字段，模型就会学到一个我们不支持的输出键。这与 `app/tools/models.py::_Contract` 同理。
- `frozen=True`：契约实例一经校验即不可变——`handoff()` 拿到的是一个已定型的事实，序列化前不会
  再被人「顺手改一个字段」。

## P0 与 P1 一并定义（模型定义零成本）

P0 实际接线的只有 `ColumnMappingProposal`（列映射）、`RevisedPlanProposal` / `ExplanationDraft`
（重排 + 解释）。P1 的 `ScenarioTranslation` / `PreferenceRuleCandidate` / `RiskNarrative` 一并
在此声明：一个 Pydantic 模型定义不消耗任何运行期成本，也不给 P0 引入依赖；提前定型让 P1 落地时
只需接线 handler 与提示词，不必回头改这一层的稳定接口。
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AgentContract",
    "ColumnMappingProposal",
    "ExplanationDraft",
    "PreferenceRuleCandidate",
    "RevisedPlanProposal",
    "RiskNarrative",
    "ScenarioTranslation",
]


class AgentContract(BaseModel):
    """全部 Agent 输出契约的基类（design.md §3.3）。

    子类通过覆盖 `UNTRUSTED_FIELDS` 声明其自由文本字段。基类给出 `untrusted_field_names()`，
    这是 `handoff()` 唯一用来查询「哪些字段要被 `<untrusted>` 包裹」的入口——它不猜、不按类型
    推断，只读这份显式登记。

    `extra="forbid"` + `frozen=True` 见模块 docstring。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 本契约中属于「模型自由发挥的散文」的字段名。默认空——纯结构化契约（无自由文本）此集为空。
    UNTRUSTED_FIELDS: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def untrusted_field_names(cls) -> frozenset[str]:
        """本契约的自由文本字段集合（R21.3、R23.2）。

        返回的每个名字都必须是本模型真实声明的字段——否则 `handoff()` 会声称包裹一个不存在的
        字段，掩盖「字段被改名但登记没跟上」这种错误。`test_contracts.py` 逐契约断言
        `untrusted_field_names() <= set(model_fields)`。
        """
        return cls.UNTRUSTED_FIELDS


# --------------------------------------------------------------------------
# 共享的小类型
# --------------------------------------------------------------------------


class Feasibility(str, Enum):
    """计划可行性三态（R8.1）。与 `app/tools/models.py::Feasibility` 取值一致但独立声明——
    Agent 契约不依赖工具契约类型（见模块 docstring）。"""

    FEASIBLE = "FEASIBLE"
    PARTIAL = "PARTIAL"
    NO_FEASIBLE_PLAN = "NO_FEASIBLE_PLAN"


# --------------------------------------------------------------------------
# P0 契约（实际接线）
# --------------------------------------------------------------------------


class MappedColumn(AgentContract):
    """一列的映射决定。`source_column` / `target_field` 是结构化标识符，非自由文本。"""

    target_field: str
    source_column: str | None = None
    confidence: float = Field(ge=0, le=1)


class ColumnMappingProposal(AgentContract):
    """`Ingestion_Agent` 的整轮输出契约（design.md §3.1、§3.3、R2）。

    这是**列映射提案**的最终形态：经 schema 校验后写入 `import_batches.proposed_mapping`，
    只能被人工确认 UI 消费——**没有任何代码路径**把它拼进 `Planning_Agent` 的提示词（R21.3 的
    物理隔离，design.md §3.1 第 3 道机制）。

    `mapping_rationale` 是模型对「为什么这么映」的散文说明，来自处理**最不受信任的外部文件**的
    Agent，因此登记为 untrusted：展示给规划员时用 `<untrusted>` 包裹，其中任何指令都只作数据。
    """

    entity_type: Literal["ORDER", "PRODUCT", "MATERIAL", "MACHINE", "WORKER"]
    entity_type_confidence: float = Field(ge=0, le=1)
    columns: list[MappedColumn] = Field(default_factory=list, max_length=40)
    mapping_rationale: str = Field(default="", max_length=2_000)

    UNTRUSTED_FIELDS: ClassVar[frozenset[str]] = frozenset({"mapping_rationale"})


class RevisedPlanProposal(AgentContract):
    """`Planning_Agent` 重排路径的输出契约（design.md §3.3、R9）。

    **句柄化**：只携带 `candidate_plan_id` + 聚合指标，绝不含逐 `ScheduledJob` 明细（ADR-004）
    ——明细泄漏会让这个契约的每次跨边界传递都重传上千 token。`revision_summary` 是模型对本次
    重排的散文说明，登记为 untrusted。数值型字段（拖期、churn 等）由确定性内核算出，不是模型
    编的，因此不属 untrusted。
    """

    candidate_plan_id: str
    baseline_plan_id: str | None = None
    feasibility: Feasibility
    total_tardiness_minutes: int = Field(ge=0)
    churn_ratio: float = Field(ge=0, le=1)
    revision_summary: str = Field(default="", max_length=2_000)

    UNTRUSTED_FIELDS: ClassVar[frozenset[str]] = frozenset({"revision_summary"})


class ExplanationDraft(AgentContract):
    """`Planning_Agent` 解释路径的输出契约（design.md §3.3、R10）。

    解释叙述由 LLM 生成，但**数字全部来自确定性组件，模型不得改数**（requirements §「解释叙述、
    反事实措辞」）。因此 `plan_id` 是结构化标识符（受信任），而 `narrative` /
    `counterfactual_text` 是自由散文（untrusted）——展示时包裹，注入任何下游上下文时同样包裹。
    """

    plan_id: str
    narrative: str = Field(max_length=4_000)
    counterfactual_text: str = Field(default="", max_length=2_000)

    UNTRUSTED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"narrative", "counterfactual_text"}
    )


# --------------------------------------------------------------------------
# P1 契约（模型定义零成本，提前定型；P0 不接线）
# --------------------------------------------------------------------------


class ScenarioMutation(AgentContract):
    """一条结构化场景变更。全部字段结构化，无自由文本。"""

    kind: Literal[
        "ADD_ORDER",
        "REMOVE_ORDER",
        "MACHINE_DOWN",
        "WORKER_ABSENT",
        "CHANGE_PRIORITY",
    ]
    target_id: str
    value: str | None = None


class ScenarioTranslation(AgentContract):
    """P1：自然语言 What-if → 结构化场景的翻译（design.md §3.3、R16）。

    翻译的**输入**是不受信任的自然语言查询（R23.1）；翻译的**输出**（结构化 mutations）经此
    契约呈现给规划员确认（R16.10）。`source_query_echo` 原样回显用户输入的那句自然语言——它
    正是不受信任来源本身，必须登记：回显给规划员时包裹，绝不当作指令。
    """

    mutations: list[ScenarioMutation] = Field(default_factory=list, max_length=5)
    source_query_echo: str = Field(default="", max_length=2_000)

    UNTRUSTED_FIELDS: ClassVar[frozenset[str]] = frozenset({"source_query_echo"})


class PreferenceRuleCandidate(AgentContract):
    """P1：从接受/拒绝理由蒸馏出的候选偏好规则（design.md §3.3、R18.3）。

    规则必须**人工确认后**才生效（requirements §「拒绝理由 → 候选 PreferenceRule 的蒸馏」）。
    `structured_form` 是 4 类封闭形态之一（ADR-008），受信任；`human_text` 与
    `source_rationale` 是模型生成/回显的散文，登记为 untrusted。
    """

    structured_form: Literal[
        "PREFER_EARLY",
        "AVOID_MACHINE",
        "GROUP_BY_PRODUCT",
        "ADJUST_OBJECTIVE_WEIGHT",
    ]
    weight_delta_minutes: int = Field(ge=0)
    human_text: str = Field(default="", max_length=1_000)
    source_rationale: str = Field(default="", max_length=2_000)

    UNTRUSTED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"human_text", "source_rationale"}
    )


class RiskNarrative(AgentContract):
    """P1：`Risk_Monitor_Agent` 的风险归因叙述（design.md §3.3、R14）。

    P0 的风险叙述由 `Risk_Scanner` + 模板渲染覆盖，不经 Agent；本契约是 P1 让 Agent 生成叙述的
    形态。`finding_id` 是结构化标识符（受信任），`narrative` / `recommended_action_text` 是
    自由散文（untrusted）。Agent **无写权限**——本契约不携带任何可触发写操作的字段（R14.8）。
    """

    finding_id: str
    severity: Literal["INFO", "WARNING", "CRITICAL"]
    narrative: str = Field(max_length=4_000)
    recommended_action_text: str = Field(default="", max_length=2_000)

    UNTRUSTED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"narrative", "recommended_action_text"}
    )
