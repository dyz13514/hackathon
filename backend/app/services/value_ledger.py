"""价值台账的确定性聚合（任务 7.6 的 K-14 部分，R13.13、R19）。

本模块只承担 Task 7.6 需要的那一片：**自主处理 vs 上报人工的计数与逐条判据**（K-14，
R13.13）。它从 `impact_assessments` 表按 `execution_path` 聚合——那张表由重排流水线写入，
每一行是一次确定性的影响分级裁决（`impact_class` + `autonomy_level` + `execution_path` +
`decisive_predicates` + `impact_input`）。

## 计数口径（R13.13、design.md §3.6）

- `auto_handled_count` = `execution_path ∈ {PROPOSED, AUTO_APPLIED}` 的行数。P0 只有
  `PROPOSED`（L3 自主生成 PENDING_APPROVAL 提案）；`AUTO_APPLIED`（L4）属 P1，P0 运行期
  恒不出现，但计数口径此刻就把它算进「自主处理」，这样 P1 打开 L4 后无需改这里。
- `escalated_count` = `execution_path == ESCALATED` 的行数（L5 强制人工审批）。

「自主处理」指系统未经人工介入就推进到某个可执行状态（P0 是生成提案；P1 是自动应用）；
「上报」指系统主动停下要求人工决定。两者之和即被分级的变更总数，比例即 K-14 要展示的
「自主 vs 上报」。

## 为什么是纯查询、不落新表

K-14 是**可从既有事实重算**的派生量——`impact_assessments` 已是权威记录。再落一张计数表
会引入「计数与明细不一致」的失步风险；每次读时聚合，计数永远等于明细。演示规模下这点查询
成本可忽略。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import models as orm

__all__ = [
    "AUTO_HANDLED_PATHS",
    "AutonomyDecisionRow",
    "AutonomySummary",
    "autonomy_decisions",
    "autonomy_summary",
]

#: 计入「自主处理」的执行路径。`AUTO_APPLIED` 属 P1（P0 恒不出现），但口径此刻就纳入，
#: 使 P1 打开 L4 后计数自动正确，无需改动本模块。
AUTO_HANDLED_PATHS = ("PROPOSED", "AUTO_APPLIED")


@dataclass(frozen=True)
class AutonomySummary:
    """自主 vs 上报的聚合计数（K-14，R13.13）。

    `auto_handled_count + escalated_count == total`（P0 下 `execution_path` 只有这两类落点，
    因此二者之和即被分级的变更总数）。`auto_handled_ratio` 是 UI 顶栏那条比例；分母为 0 时
    取 0.0（还没有任何分级裁决，「自主占比」无定义，展示 0 比展示 NaN 诚实）。
    """

    auto_handled_count: int
    escalated_count: int

    @property
    def total(self) -> int:
        return self.auto_handled_count + self.escalated_count

    @property
    def auto_handled_ratio(self) -> float:
        return self.auto_handled_count / self.total if self.total > 0 else 0.0


@dataclass(frozen=True)
class AutonomyDecisionRow:
    """一次影响分级裁决的可展示摘要（R13.12：judgement + 决定性判据 + 执行路径）。

    逐字段来自 `impact_assessments`。UI 的价值台账逐行渲染它，让「每次判定的决定性判据」
    可见（情节 8 / K-14）。
    """

    assessment_id: str
    candidate_plan_id: str
    impact_class: str
    autonomy_level: str
    execution_path: str
    decisive_predicates: tuple[str, ...]


def autonomy_summary(session: Session) -> AutonomySummary:
    """按 `execution_path` 聚合 `impact_assessments`，返回自主 vs 上报计数（K-14）。

    一次 `GROUP BY execution_path` 的计数查询，确定性且轻量。无任何 assessment 时两个计数
    都是 0。
    """
    rows = session.execute(
        select(
            orm.ImpactAssessment.execution_path,
            func.count().label("n"),
        ).group_by(orm.ImpactAssessment.execution_path)
    ).all()
    counts = {str(path): int(n) for path, n in rows}
    auto_handled = sum(counts.get(path, 0) for path in AUTO_HANDLED_PATHS)
    escalated = counts.get("ESCALATED", 0)
    return AutonomySummary(auto_handled_count=auto_handled, escalated_count=escalated)


def autonomy_decisions(session: Session, *, limit: int = 50) -> list[AutonomyDecisionRow]:
    """最近若干次影响分级裁决，供 UI 逐行展示决定性判据（R13.12）。

    按 `created_at` 降序（最新的在前），最多 `limit` 行。`decisive_predicates` 是 JSON 列，
    读回时归一化为字符串元组。
    """
    stmt = (
        select(orm.ImpactAssessment)
        .order_by(orm.ImpactAssessment.created_at.desc(), orm.ImpactAssessment.assessment_id)
        .limit(limit)
    )
    result: list[AutonomyDecisionRow] = []
    for row in session.execute(stmt).scalars():
        preds = row.decisive_predicates
        predicates = tuple(str(p) for p in preds) if isinstance(preds, list | tuple) else ()
        result.append(
            AutonomyDecisionRow(
                assessment_id=row.assessment_id,
                candidate_plan_id=row.candidate_plan_id,
                impact_class=row.impact_class,
                autonomy_level=row.autonomy_level,
                execution_path=row.execution_path,
                decisive_predicates=predicates,
            )
        )
    return result
