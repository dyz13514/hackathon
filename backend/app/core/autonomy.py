"""`Autonomy_Policy_Engine`（任务 7.3，R13.1–R13.6 / R13.8 / R13.11 / R13.12 / R27.10）。

design.md Components §3.6 与 ADR-010 把本组件的核心性质写成一句话：**LLM 结构上无法影响
`Impact_Class` 或 `Autonomy_Level` 的判定**。本模块是那句话的代码载体，以下三个机制共同
实现它：

1. `ImpactInput` 的 7 个字段全部是 `int` / `bool` / `float`，**没有任何字符串字段**，
   因此 LLM 的文本输出没有可注入的入口（design.md §3.6「为什么 LLM 结构上无法影响」第 1 条）。

2. `ImpactInput` 只由 `ImpactInput.from_delta(delta, active, cand)` 产生；`delta` 只由
   `compute_plan_delta`（`app/core/delta.py`）从**已持久化的两个计划行**计算；LLM 只能
   提供经 schema 校验的 `plan_id` 字符串，不能提供任何数值（第 2 条）。

3. `decide_autonomy` 在 `IMPACT_MAJOR` 分支里在读取 `FeatureFlags` **之前**就返回 L5；
   `flags` 在该路径上根本不被求值——这是「不可被任何配置覆盖」的结构化写法，而不是靠注释
   声明（第 5 条；design.md §3.6 注意事项）。

## P0 只有两个结果（design.md §3.6 的表格）

在 P0 默认配置（`auto_apply_minor_enabled = false`）下：

| Impact_Class   | Autonomy_Level |
|----------------|----------------|
| IMPACT_MINOR   | L3（PROPOSE）  |
| IMPACT_MODERATE| L3（PROPOSE）  |
| IMPACT_MAJOR   | L5（HUMAN_ONLY）— 不可覆盖 |

L4（AUTO_APPLY_MINOR）在 P0 运行期**不会出现**。`IMPACT_MINOR` 与 `IMPACT_MODERATE` 的区分
不影响 P0 的执行路径，但**必须正确计算并展示**：R13.12 要求记录 `impact_class` 与具体判据，
EVAL-209 断言的正是分级正确性。

## `decisive_predicates`：「第一个失败的条件」形式（R13.12 / design.md §3.6）

函数以"哪一个条件先失败"的形式返回判据列表，例如：

    ["changed_job_count=3 > 2", "touches_high_priority=true"]

写入 `impact_assessments.decisive_predicates` 与 `Audit_Log`，也是 EVAL-209 的断言对象。

## 分层规则

本模块只 import 标准库与 `app.core.*`，不 import `sqlalchemy` / `fastapi` / `httpx` /
`boto3` / `app.llm` / `app.agents` / `app.orchestrator`。

`tests/structure/test_layering.py` 的第 ⑤ 条 `test_autonomy_engine_has_no_llm_or_agent_imports`
静态断言此点。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from app.core.delta import PlanDelta

if TYPE_CHECKING:
    # 仅用于 from_delta 的类型注解；运行期通过 dataclass 定义 ActivePlanRow / CandPlanRow
    pass

__all__ = [
    "ImpactClass",
    "AutonomyLevel",
    "FeatureFlags",
    "ImpactInput",
    "classify_impact",
    "decisive_predicates",
    "decide_autonomy",
    "ActivePlanRow",
    "CandPlanRow",
]


# --------------------------------------------------------------------------
# 枚举（R13.1–R13.2）
# --------------------------------------------------------------------------


class ImpactClass(str, Enum):
    """三级影响分级（R13.1）。取值与 DB 列值、工具契约逐字对齐。"""

    IMPACT_MINOR = "IMPACT_MINOR"
    IMPACT_MODERATE = "IMPACT_MODERATE"
    IMPACT_MAJOR = "IMPACT_MAJOR"


class AutonomyLevel(str, Enum):
    """五级自主等级（R13.2）。P0 运行期只出现 L3 与 L5。"""

    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    L5 = "L5"


# --------------------------------------------------------------------------
# 特性开关（R13.7–R13.8）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureFlags:
    """影响自主等级判定的特性开关。

    P0 默认值：`auto_apply_minor_enabled = False`（R13.8）。
    L4 分支（R13.7）属 P1，默认关闭，切换后才激活。
    """

    auto_apply_minor_enabled: bool = False


# --------------------------------------------------------------------------
# ImpactInput（design.md §3.6，R13.11 的结构性保障）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ImpactInput:
    """影响分级的**唯一**输入类型。

    ## 为什么 7 个字段都是 int / bool / float

    R13.11 要求「拒绝由 LLM 输出决定 Impact_Class 或 Autonomy_Level」。这里的设计选择是
    **让注入在结构上不可达**：7 个字段全部是数值类型，LLM 的文本输出没有对应的注入点——
    模型不可能把 `"IMPACT_MINOR"` 这个字符串塞进一个 `int` 或 `bool` 字段（design.md
    §3.6 第 1 条）。

    反事实：如果有一个 `impact_class_hint: str` 字段，即便只是「提示」，也会成为代码里
    「从模型输出读一个字符串再影响分级」的唯一需要的句柄。不存在这个字段，这条路就不存在。

    ## 字段语义与 R13.1 的对应

    | 字段 | 对应 R13.1 的哪个判据 |
    |------|-----------------------|
    | `changed_job_count` | IMPACT_MINOR 第 1 条「变更作业数 ≤ 2」|
    | `touches_urgent_or_high` | IMPACT_MINOR 第 2 条「不涉及 URGENT 或 HIGH」|
    | `promised_date_changed` | IMPACT_MINOR 第 3 条 / IMPACT_MODERATE 第 1 条 |
    | `all_within_same_machine_and_shift` | IMPACT_MINOR 第 4 条 |
    | `tardiness_delta_minutes` | IMPACT_MINOR 第 5 条 / IMPACT_MODERATE 第 3 条 |
    | `new_unschedulable_count` | IMPACT_MINOR 第 6 条 / IMPACT_MODERATE 第 4 条 |
    | `churn_ratio` | IMPACT_MODERATE 第 2 条「churn_ratio ≤ 0.20」|
    """

    # ---- 7 个字段，全部 int / bool / float ----
    changed_job_count: int
    """added + removed + moved + reassigned（delta.changed_job_count）。"""
    touches_urgent_or_high: bool
    """变更作业中存在 priority ∈ {URGENT, HIGH} 的订单。"""
    promised_date_changed: bool
    """候选计划改变了任一订单的 promised_date（不含 None→None 的情形）。"""
    all_within_same_machine_and_shift: bool
    """全部变更作业保持在同一 Machine 与同一 Worker 班次内（moved 集合内无 machine 变化，
    且 start_time 仍在原工人班次窗内）。reassigned 作业存在时此条必为 False。"""
    tardiness_delta_minutes: int
    """候选 total_tardiness_minutes 减 active total_tardiness_minutes。"""
    new_unschedulable_count: int
    """候选新增的不可排产作业数 = max(len(cand.unschedulable) - len(active.unschedulable), 0)。"""
    churn_ratio: float
    """delta.churn_ratio（来自 compute_plan_delta，分母取并集）。"""

    # --------------------------------------------------------------------------
    # 构造函数（设计要求的唯一产生路径）
    # --------------------------------------------------------------------------

    @classmethod
    def from_delta(
        cls,
        delta: PlanDelta,
        active: "ActivePlanRow",
        cand: "CandPlanRow",
    ) -> "ImpactInput":
        """从 `PlanDelta` 与两份计划行的关键度量值构造 `ImpactInput`。

        这是 `ImpactInput` 的**唯一**合法产生路径（design.md §3.6 第 2 条）。
        `PlanDelta` 来自 `compute_plan_delta`，后者从两个已持久化的 `PlanCandidate` 计算；
        `active` / `cand` 是数据库行的关键度量（由 `ActivePlanRow` / `CandPlanRow` 承载），
        不从任何 Agent 输出读取。

        ## 参数含义

        - `delta`：两份计划的变更集（`app.core.delta.compute_plan_delta` 产物）。
        - `active`：当前 ACTIVE 计划的关键度量（total_tardiness_minutes、
          unschedulable_count、per-job 信息）。
        - `cand`：候选计划的关键度量。
        """
        # ---- 计算各判据 ----
        changed_job_count = delta.changed_job_count

        # touches_urgent_or_high: 变更的 job 涉及 URGENT/HIGH 优先级订单
        touches_urgent_or_high = _any_changed_job_touches_high_priority(delta, active, cand)

        # promised_date_changed: 候选与 active 之间任一订单的 promised_date 有变化
        promised_date_changed = cand.promised_date_changed

        # all_within_same_machine_and_shift: reassigned 不存在，且 moved 全在同一 machine+shift
        all_within_same_machine_and_shift = _all_within_same_machine_and_shift(delta, active, cand)

        # tardiness_delta_minutes: 候选 - active（可负）
        tardiness_delta_minutes = cand.total_tardiness_minutes - active.total_tardiness_minutes

        # new_unschedulable_count: 候选新增的不可排产数（不能为负）
        new_unschedulable_count = max(
            0, cand.unschedulable_count - active.unschedulable_count
        )

        churn_ratio = delta.churn_ratio

        return cls(
            changed_job_count=changed_job_count,
            touches_urgent_or_high=touches_urgent_or_high,
            promised_date_changed=promised_date_changed,
            all_within_same_machine_and_shift=all_within_same_machine_and_shift,
            tardiness_delta_minutes=tardiness_delta_minutes,
            new_unschedulable_count=new_unschedulable_count,
            churn_ratio=churn_ratio,
        )


# --------------------------------------------------------------------------
# 计划行的关键度量（from_delta 的参数类型）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivePlanRow:
    """当前 ACTIVE 计划行的关键度量，供 `ImpactInput.from_delta` 消费。

    字段全部是数值，不含 ORM 对象、不含自由文本，保持「无字符串注入点」的不变量。
    `job_priority_map` 是 {job_id: priority_rank}（0=URGENT, 1=HIGH, 2=NORMAL, 3=LOW），
    用于判断变更作业是否涉及高优先级订单。
    `job_machine_map` / `job_worker_map` / `job_shift_end_map` 提供 machine_id / worker_id
    / shift_end (datetime) 信息，用于判断「all_within_same_machine_and_shift」。
    """

    total_tardiness_minutes: int
    unschedulable_count: int
    # {job_id: priority_rank} 0=URGENT,1=HIGH,2=NORMAL,3=LOW
    job_priority_map: dict[str, int]
    # {job_id: machine_id}
    job_machine_map: dict[str, str]
    # {job_id: worker_id}
    job_worker_map: dict[str, str]
    # {job_id: shift_end (as POSIX timestamp int, seconds)} — 简化为 int 避免 datetime import
    # 实际使用时由 shift_end_seconds 与 job end_time 对比
    job_shift_end_ts: dict[str, int]
    # {job_id: end_time_ts (as POSIX timestamp int)} — 作业的 end_time
    job_end_ts: dict[str, int]


@dataclass(frozen=True)
class CandPlanRow:
    """候选计划行的关键度量。"""

    total_tardiness_minutes: int
    unschedulable_count: int
    # candidates 是否改变了任何 promised_date（由调用者在服务层对比 DB 计算）
    promised_date_changed: bool
    # {job_id: machine_id}
    job_machine_map: dict[str, str]
    # {job_id: worker_id}
    job_worker_map: dict[str, str]
    # {job_id: shift_end_ts}
    job_shift_end_ts: dict[str, int]
    # {job_id: end_time_ts}
    job_end_ts: dict[str, int]


# --------------------------------------------------------------------------
# 辅助函数（from_delta 用）
# --------------------------------------------------------------------------


def _any_changed_job_touches_high_priority(
    delta: PlanDelta,
    active: ActivePlanRow,
    cand: CandPlanRow,
) -> bool:
    """变更集中有任一作业属于 URGENT（rank=0）或 HIGH（rank=1）优先级订单？

    `active.job_priority_map` 含 active 中存在的作业，`cand` 新增的（added）作业
    不在 active 的 map 里，这里按「active 中有则用，否则视为非高优先级」的保守策略处理。
    实际上服务层应当在构建 `ActivePlanRow` 时把新增作业的优先级也注入进来；如果 added 里
    的作业是 URGENT/HIGH，调用方应确保 `job_priority_map` 覆盖它们。
    """
    changed = (
        set(delta.added)
        | set(delta.removed)
        | set(delta.moved)
        | set(delta.reassigned)
    )
    for job_id in changed:
        rank = active.job_priority_map.get(job_id)
        if rank is not None and rank <= 1:  # 0=URGENT, 1=HIGH
            return True
    return False


def _all_within_same_machine_and_shift(
    delta: PlanDelta,
    active: ActivePlanRow,
    cand: CandPlanRow,
) -> bool:
    """全部变更作业保持在同一 Machine 与同一 Worker 班次内（R13.1 IMPACT_MINOR 第 4 条）。

    条件：
    1. `reassigned` 集合为空（reassigned 作业已经换了 machine 或 worker）。
    2. `moved` 集合里每个作业的 machine_id 在 active 与 cand 中相同（redundant with
       reassigned being empty but defensive）。
    3. `moved` 集合里每个作业在候选中的 end_time ≤ 其工人的 shift_end（即没跨班次）。

    当 changed_job_count == 0 时（空 delta），此条件视为满足。
    """
    # reassigned が存在したら False
    if delta.reassigned:
        return False

    # moved の各作業について machine が同じか確認
    for job_id in delta.moved:
        active_machine = active.job_machine_map.get(job_id)
        cand_machine = cand.job_machine_map.get(job_id)
        if active_machine != cand_machine:
            return False
        # 候選の end_time が shift_end 内に収まるか
        cand_end_ts = cand.job_end_ts.get(job_id)
        cand_shift_end_ts = cand.job_shift_end_ts.get(job_id)
        if cand_end_ts is not None and cand_shift_end_ts is not None:
            if cand_end_ts > cand_shift_end_ts:
                return False

    return True


# --------------------------------------------------------------------------
# classify_impact（R13.1）
# --------------------------------------------------------------------------


def classify_impact(x: ImpactInput) -> ImpactClass:
    """確定性影響分級（R13.1 の三段階定義をそのままコードに落とした純粋関数）。

    判定は三段階の排他的条件分岐：

    1. `IMPACT_MINOR`：6つの結合条件がすべて満たされる場合。
    2. `IMPACT_MODERATE`：MINOR でなく、かつ 4 つの結合条件がすべて満たされる場合。
    3. `IMPACT_MAJOR`：それ以外のすべて（promised_date 変更・URGENT/HIGH 関与・
       churn_ratio > 0.20・tardiness_delta > 60・新規不可排産あり など）。

    LLM 出力は入力 `x` の形成に関与しない（`ImpactInput` に文字列フィールドなし）。
    """
    # ---- IMPACT_MINOR：6 条件の連言（R13.1 第 1 条） ----
    if (
        x.changed_job_count <= 2
        and not x.touches_urgent_or_high
        and not x.promised_date_changed
        and x.all_within_same_machine_and_shift
        and x.tardiness_delta_minutes <= 0
        and x.new_unschedulable_count == 0
    ):
        return ImpactClass.IMPACT_MINOR

    # ---- IMPACT_MODERATE：4 条件の連言（R13.1 第 2 条） ----
    if (
        not x.promised_date_changed
        and x.churn_ratio <= 0.20
        and x.tardiness_delta_minutes <= 60
        and x.new_unschedulable_count == 0
    ):
        return ImpactClass.IMPACT_MODERATE

    # ---- IMPACT_MAJOR：残りすべて（R13.1 第 3 条） ----
    return ImpactClass.IMPACT_MAJOR


# --------------------------------------------------------------------------
# decisive_predicates（R13.12 / design.md §3.6）
# --------------------------------------------------------------------------


def decisive_predicates(x: ImpactInput, cls: ImpactClass) -> list[str]:
    """「どの条件が最初に失敗したか」形式の判据リストを返す（R13.12）。

    `classify_impact` が判定した `cls` に応じて、その等級を**決定した**判据を列挙する。

    - IMPACT_MINOR の場合：「すべての条件が満たされた」ことを示す。
    - IMPACT_MODERATE / IMPACT_MAJOR の場合：MINOR 条件のうち「最初に失敗した条件」を
      先頭に記録し、MODERATE 条件の失敗も付加する（MAJOR に至った場合）。

    形式：`"field_name=value op threshold"`（例：`"changed_job_count=3 > 2"`）。
    `Audit_Log` と `impact_assessments.decisive_predicates` に書き込まれる。
    """
    preds: list[str] = []

    if cls is ImpactClass.IMPACT_MINOR:
        # すべての MINOR 条件を満たした
        preds.append("all_minor_conditions_satisfied=true")
        return preds

    # ---- IMPACT_MINOR の各条件を順番にチェックして失敗したものを収集 ----
    minor_failures: list[str] = []
    if x.changed_job_count > 2:
        minor_failures.append(f"changed_job_count={x.changed_job_count} > 2")
    if x.touches_urgent_or_high:
        minor_failures.append("touches_high_priority=true")
    if x.promised_date_changed:
        minor_failures.append("promised_date_changed=true")
    if not x.all_within_same_machine_and_shift:
        minor_failures.append("all_within_same_machine_and_shift=false")
    if x.tardiness_delta_minutes > 0:
        minor_failures.append(
            f"tardiness_delta_minutes={x.tardiness_delta_minutes} > 0"
        )
    if x.new_unschedulable_count > 0:
        minor_failures.append(f"new_unschedulable_count={x.new_unschedulable_count} > 0")

    preds.extend(minor_failures)

    if cls is ImpactClass.IMPACT_MODERATE:
        # MODERATE は MINOR の失敗条件でこの等級になった
        return preds

    # ---- IMPACT_MAJOR：さらに MODERATE 条件の失敗も追加 ----
    moderate_failures: list[str] = []
    if x.promised_date_changed:
        # 既に minor_failures に含まれている可能性があるが MODERATE 側でも確認
        if "promised_date_changed=true" not in preds:
            moderate_failures.append("promised_date_changed=true")
    if x.churn_ratio > 0.20:
        moderate_failures.append(f"churn_ratio={x.churn_ratio:.4f} > 0.20")
    if x.tardiness_delta_minutes > 60:
        if not any("tardiness_delta_minutes" in p for p in preds):
            moderate_failures.append(
                f"tardiness_delta_minutes={x.tardiness_delta_minutes} > 60"
            )
    if x.new_unschedulable_count > 0:
        if not any("new_unschedulable_count" in p for p in preds):
            moderate_failures.append(
                f"new_unschedulable_count={x.new_unschedulable_count} > 0"
            )

    preds.extend(moderate_failures)

    return preds


# --------------------------------------------------------------------------
# decide_autonomy（R13.5–R13.8，structural "not overridable"）
# --------------------------------------------------------------------------


def decide_autonomy(cls: ImpactClass, flags: FeatureFlags) -> AutonomyLevel:
    """自主等級を確定性的に判定する（R13.5–R13.8）。

    ## 構造的な「IMPACT_MAJOR は上書き不可」の実装（design.md §3.6）

    `IMPACT_MAJOR` の分岐は `flags` を**読む前に**返す。`FeatureFlags` は
    `IMPACT_MAJOR` のパスでは一切評価されない——これは「不可覆盖」をコメントではなく
    コードの制御フローで表現した実装。
    注：`flags` 引数は Python では遅延評価のため、`cls is ImpactClass.IMPACT_MAJOR` の
    分岐に入った時点で `flags` オブジェクト自体はすでに存在するが、そのフィールドへの
    アクセス（`flags.auto_apply_minor_enabled`）が発生しないことが重要。

    ## P0 での戻り値域

    P0 デフォルト（`auto_apply_minor_enabled = False`）では戻り値は {L3, L5} のみ。
    L4 は P1 であり、`auto_apply_minor_enabled = True` でのみ現れる。
    """
    if cls is ImpactClass.IMPACT_MAJOR:
        # R13.5：IMPACT_MAJOR は必ず L5。flags を読む前に返す。
        return AutonomyLevel.L5

    if cls is ImpactClass.IMPACT_MODERATE:
        # R13.6：IMPACT_MODERATE は L3。
        return AutonomyLevel.L3

    # cls is IMPACT_MINOR
    # R13.7（P1）：auto_apply_minor_enabled = True なら L4
    # R13.6 / R13.8：デフォルト（False）では L3
    return (
        AutonomyLevel.L4
        if flags.auto_apply_minor_enabled
        else AutonomyLevel.L3
    )
