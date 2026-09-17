"""写入工具的 handler（任务 5.2，R22.5、R22.9、R23.4）。

4 个写入工具。它们把 Agent/流水线的意图落成持久记录，其中两处的**状态与启用位是硬编码的**，
不接受任何输入覆盖——这是 R22.9 / R23.4 / R18.4 在工具层的强制点：

- `save_proposed_plan` 只写 `status=PENDING_APPROVAL`（无 `status` 入参），复用
  `plan_generation` 流水线里那道 `save_proposed_plan` 守卫（`origin != 'BASELINE'` 且
  `produced_in_sandbox == False`）——没有任何工具能把计划写成 `ACTIVE`。
- `propose_preference_rule` 只写 `enabled=False`（无 `enabled` 入参）。**P0 不接线，仅 P1
  使用**；此处定义完整契约并留一个明确的占位，使 26 工具注册表完整、白名单矩阵契约覆盖到它。

`register_disruption` 与 `save_import_batch` 落库路径分别随 §7（扰动登记）与 §2（摄取流水线）
落地；契约在此完整定义，接线后委派对应服务。
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.orm import Session

from app.orchestrator.pipelines.plan_generation import (
    PENDING_STATUS,
    SaveProposedPlanRejected,
)
from app.tools import models as m
from app.tools.registry import ToolContext

#: 基线来源常量。`save_proposed_plan` 拒绝把基线推进审批流（design.md §8，与流水线守卫同源）。
BASELINE_ORIGIN = "BASELINE"


def _session(ctx: ToolContext) -> Session:
    if ctx.session is None:
        raise RuntimeError("写入 handler 需要 ctx.session；registry 装配时必须注入会话")
    # `ToolContext.session` 刻意是 `Any`（见 registry）；在此显式收窄为 `Session`。
    return cast(Session, ctx.session)


def save_proposed_plan(
    args: m.SaveProposedPlanIn, ctx: ToolContext
) -> m.SaveProposedPlanOut:
    """把候选计划落成 `PENDING_APPROVAL` 提案（R22.9、R23.4，复用流水线守卫）。

    **状态硬编码**：输出模型 `SaveProposedPlanOut.status` 是 `Literal["PENDING_APPROVAL"]`，
    输入模型没有 `status` 字段——「把计划写成 `ACTIVE`」在类型层面不可表达。此外复用流水线
    的 §8 守卫：`origin == 'BASELINE'` 直接抛 `SaveProposedPlanRejected`（基线永不进审批流）。

    落库路径（把 `candidate_plan_id` 的候选计划物化成五张表）随 §7 的「计划态读回/写回」机具
    落地——P0 的初始提案由 `plan_generation` 流水线直接产出并落库，不经本工具。本 handler 此刻
    强制状态语义并守住 §8 前置条件，持久化接线后委派 `plan_generation.save_proposed_plan`。
    """
    # §8 前置条件（与 plan_generation.save_proposed_plan 同源）：基线永不进审批流。
    if args.origin == BASELINE_ORIGIN:
        raise SaveProposedPlanRejected(
            f"基线计划（origin={args.origin!r}）永不进审批流，不能经 save_proposed_plan 落成 "
            f"{PENDING_STATUS}（design.md §8）"
        )
    raise NotImplementedError(
        "save_proposed_plan 的候选物化落库随 §7 落地；状态恒为 PENDING_APPROVAL（已硬编码），"
        "接线后委派 app.orchestrator.pipelines.plan_generation.save_proposed_plan"
    )


def register_disruption(
    args: m.RegisterDisruptionIn, ctx: ToolContext
) -> m.RegisterDisruptionOut:
    """登记一类扰动（R9.1）。委派给 §7 的扰动登记服务（写 `disruptions` + 相应停机/缺勤窗）。

    5 类扰动的判别联合载荷已在契约里定义。登记服务随 §7 落地：写 `disruptions` 行，
    `MACHINE_BREAKDOWN` / `WORKER_UNAVAILABLE` 类同时写 `machine_downtime` / `worker_absences`
    窗口并回指该扰动。
    """
    raise NotImplementedError(
        "register_disruption 委派 §7 的扰动登记服务；5 类载荷契约已定义"
    )


def propose_preference_rule(
    args: m.ProposePreferenceRuleIn, ctx: ToolContext
) -> m.ProposePreferenceRuleOut:
    """提出一条候选偏好规则，**恒 `enabled=False`**（R18.4）。**P0 不接线，仅 P1 使用。**

    输入模型没有 `enabled` 字段，输出 `enabled` 是 `Literal[False]`——「候选规则自动生效」在
    类型层面不可表达（R18.4：只有显式人工确认能启用）。P0 只有手工新建规则的入口，本工具的
    自动蒸馏路线属 P1；此处定义完整契约并留占位，使注册表 26 工具完整、白名单矩阵覆盖到它。
    """
    raise NotImplementedError(
        "propose_preference_rule 是 P1 偏好蒸馏路线（R18）；契约已定义，enabled 恒为 False"
    )


def save_import_batch(args: m.SaveImportBatchIn, ctx: ToolContext) -> m.SaveImportBatchOut:
    """把一批已确认映射的行落成正式记录（R3.2）。委派给 §2 的摄取落库服务。

    落库路径（写目标实体表 + `import_batches` + 逐行 `import_row_provenance`）随摄取流水线
    落地；契约在此完整定义，接线后委派对应服务。
    """
    raise NotImplementedError(
        "save_import_batch 委派 §2 的摄取落库服务；契约已定义"
    )
