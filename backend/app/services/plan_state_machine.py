"""计划状态机的迁移许可表（design.md Data Models §8，任务 3.3，R11.8 / R22.9 / R23.4）。

design.md §8 用一张 mermaid 状态图 + 一张「允许的迁移与执行权限」表把计划生命周期钉死。
本模块是那张表的逐格翻译：**表内的迁移是仅有的合法迁移，任何表外迁移一律
`INVALID_STATE_TRANSITION`。**

## 为什么把它抽成一张显式的表，而不是散落在各处的 `if`

审批闸门里的状态检查（`Approval_Service.approve/reject/modify`）目前是「只有
`PENDING_APPROVAL` 才能继续」这一条散在三个方法里的 `if`。那些 `if` 是**正确**的，但它们
回答不了一个审计员会问的问题：「这个系统一共允许哪些状态迁移？」——答案散在代码各处，
没有一处能一眼看全。design.md §8 的表就是那个「一处看全」的权威清单，本模块让它成为
**可执行的**权威清单：迁移合法性只有一个判定入口（`is_allowed_transition`），新增一条
迁移必须显式改这张表，而不是在某个方法里悄悄多写一个分支。

## 表的内容（design.md §8「允许的迁移与执行权限」）

| 迁移 | 唯一允许执行的组件 |
|------|-------------------|
| `∅ → DRAFT` | `Scheduling_Core`（经 `save_candidate`） |
| `DRAFT → PENDING_APPROVAL` | `save_proposed_plan`（`Planning_Agent` 或流水线） |
| `PENDING_APPROVAL → ACTIVE` | **仅** `Approval_Service.approve()` |
| `PENDING_APPROVAL → REJECTED` | 仅 `Approval_Service.reject()` |
| `PENDING_APPROVAL → SUPERSEDED` | 仅 `Approval_Service.modify()/cancel()` |
| `ACTIVE → SUPERSEDED` | 仅 `Approval_Service`（`approve` 内 `supersede`，或 P1 `activate_internal`） |
| 任何 → `ACTIVE`（其他路径） | **无组件被允许** |

`∅ → DRAFT` 不是一次「状态迁移」（源状态不存在），因此不进 `ALLOWED_TRANSITIONS`
——它是「无中生有地创建一行 `DRAFT`」，由 `save_candidate` / 基线写入负责。本表只登记
**已存在的计划**从一个状态到另一个状态的迁移。

## 与既有 `if` 的关系

本模块不替换 `Approval_Service` 三个方法里的状态检查——那些检查除了「源状态对不对」还
夹带着各自的语义（陈旧检测、理由长度、并发版本），无法用一张纯粹的状态表覆盖。本模块
提供的是那张表的**独立、可查询的真相**：`Approval_Service` 的检查是它的一个切片，属性
15 的测试与将来的审计查询用的是整张表。两者一致性由 `is_allowed_transition` 的单点定义
保证。

本模块**只有常量与纯函数**，不 import `sqlalchemy` / `fastapi` / 任何应用层——因此任何层
都能安全引用它来回答「这次迁移合不合法」。
"""

from __future__ import annotations

from enum import StrEnum


class PlanStatus(StrEnum):
    """计划状态（design.md §8 / `models.PLAN_STATUSES`）。

    取值与 `app.db.models.PLAN_STATUSES` 一一对应；这里用 `StrEnum` 让状态机的键有类型，
    而字符串值仍能直接与库里存的 `production_plans.status` 比较。
    """

    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


#: 迁移的执行组件标识。用于 `ALLOWED_TRANSITIONS` 的值，回答「谁被允许做这次迁移」。
#: 这些不是运行期强制的（强制靠调用点约束与静态断言），而是让这张表**自带执行权限的
#: 文档**——审计员读表即知每条合法迁移背后唯一的授权组件。
COMPONENT_SCHEDULING_CORE = "Scheduling_Core.save_candidate"
COMPONENT_SAVE_PROPOSED_PLAN = "save_proposed_plan"
COMPONENT_APPROVAL_SERVICE = "Approval_Service"


#: 允许的状态迁移（design.md §8 表）。键是 `(from_status, to_status)`，值是唯一被允许
#: 执行该迁移的组件标识。**不在本表的 `(from, to)` 一律非法**（`INVALID_STATE_TRANSITION`）。
#:
#: 注意：`(from, from)` 自迁移不在表内 —— 把一个计划「迁移」到它已经所处的状态不是一次
#: 合法操作（没有对应的业务动作）。`∅ → DRAFT` 也不在表内（见模块 docstring）。
ALLOWED_TRANSITIONS: dict[tuple[PlanStatus, PlanStatus], str] = {
    # save_proposed_plan（Planning_Agent 或流水线）：已校验、当日无其他 PENDING_APPROVAL。
    (PlanStatus.DRAFT, PlanStatus.PENDING_APPROVAL): COMPONENT_SAVE_PROPOSED_PLAN,
    # 仅 Approval_Service.approve()：版本一致 + 重校验零违反 + 乐观并发成功。
    (PlanStatus.PENDING_APPROVAL, PlanStatus.ACTIVE): COMPONENT_APPROVAL_SERVICE,
    # 仅 Approval_Service.reject()：rejection_reason ≥5 字符。
    (PlanStatus.PENDING_APPROVAL, PlanStatus.REJECTED): COMPONENT_APPROVAL_SERVICE,
    # 仅 Approval_Service.modify()/cancel()：MODIFY 后新版本已建立 / 为新提案让位。
    (PlanStatus.PENDING_APPROVAL, PlanStatus.SUPERSEDED): COMPONENT_APPROVAL_SERVICE,
    # 仅 Approval_Service（approve 内 supersede_previous_active，或 P1 activate_internal）。
    (PlanStatus.ACTIVE, PlanStatus.SUPERSEDED): COMPONENT_APPROVAL_SERVICE,
}


def _coerce(status: str | PlanStatus) -> PlanStatus | None:
    """把库里存的裸字符串状态收敛成 `PlanStatus`。未知取值返回 `None`（视为无合法迁移）。

    库里的 `status` 是 `String` 列，理论上可能出现表外的脏值。此处不抛异常：状态机的语义
    是「未登记的迁移即非法」，一个未知的源/目标状态自然落在「非法」一侧。
    """
    if isinstance(status, PlanStatus):
        return status
    try:
        return PlanStatus(status)
    except ValueError:
        return None


def is_allowed_transition(
    from_status: str | PlanStatus, to_status: str | PlanStatus
) -> bool:
    """`(from_status → to_status)` 是否是 design.md §8 表内的合法迁移。

    这是迁移合法性的**唯一判定入口**（模块 docstring）。表外的一切 —— 包括自迁移
    （`from == to`）、未知状态、以及任何「跳过 `PENDING_APPROVAL` 直达 `ACTIVE`」的尝试
    —— 一律返回 `False`，调用方据此返回 `INVALID_STATE_TRANSITION`（R11.8 / R23.4）。
    """
    source = _coerce(from_status)
    target = _coerce(to_status)
    if source is None or target is None:
        return False
    return (source, target) in ALLOWED_TRANSITIONS


def transition_authority(
    from_status: str | PlanStatus, to_status: str | PlanStatus
) -> str | None:
    """唯一被允许执行该迁移的组件标识；迁移非法时返回 `None`。

    供审计载荷与将来的诊断使用：一条合法迁移背后总有且仅有一个授权组件（design.md §8
    的第二列），这个函数把「这次迁移本该由谁做」变成可查询的事实。
    """
    source = _coerce(from_status)
    target = _coerce(to_status)
    if source is None or target is None:
        return None
    return ALLOWED_TRANSITIONS.get((source, target))
