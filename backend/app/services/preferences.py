"""`Preference_Store`：手写偏好规则的确定性存储与生命周期（任务 11.1，R18，design.md §4.3）。

**归属：确定性** · R18.4–R18.12 · P0

本模块是 P0 唯一的建规则入口（`CREATE_PREFERENCE_RULE`，design.md §2.3 的路由表）背后的服务层。
它只做「存储与生命周期」这一件事：创建、列出、编辑、显式启用/停用、删除。**它不打分**——把偏好
规则变成 `preference_penalty` 的一项是任务 11.2 的事（`core/scoring.py` 与 `core/scheduler.py`）。
这条边界是有意的：11.2 之前偏好规则可以被创建、启用、审计、在快照里被内核携带，但对排产结果
零影响，因此本模块的正确性不依赖 11.2，反之亦然。

## 四条与安全论证直接相关的不变量（R18.8 的载体）

1. **默认未启用**：`create_rule()` 恒写 `enabled=False`（R18.4）。启用只能经
   `set_enabled(..., True)` 这一条显式路径，且它是独立可审计的动作。没有任何创建/编辑路径能把
   规则悄悄启用。
2. **越界即拒**：`structured_form` 是 `app.tools.models.PreferenceForm` 这个封闭判别联合，只有
   4 类。`AdjustObjectiveWeight.component` 被 `SoftWeightKey`（6 个软目标分量）约束，任何指向
   硬约束开关的字符串在 Pydantic 校验时就被拒。本模块 `_validate_form()` 再判一道并抛
   `PreferenceRuleOutOfScopeError`——两道都在，前一道防「类型层面写不出」，后一道防「绕过类型层
   直接塞 dict」。偏好规则永远不进 `validate()` / `is_feasible_slot()` 的硬约束路径（EVAL-206）。
3. **启用上限 20**：`set_enabled(..., True)` 在启用前统计当前启用数，达 20 →
   `PreferenceRuleLimitError`（R18.11）。计数用 `ix_pref_rules_enabled` 索引，O(log n)。
4. **证据不足标记**：`source_decision_ids` 少于 2 条 → `low_evidence=True`（R18.10）。手写规则也可能
   证据不足，因此这条对 P0 手写入口同样适用；它只是一个**提示标记**，不阻断创建也不阻断启用。

## 审计（R18.12）

创建 / 编辑 / 启用 / 停用 / 删除**每一次**都写一条 `PREFERENCE_RULE_CHANGE` 审计记录，`event_type`
区分具体动作（`CREATE` / `UPDATE` / `ENABLE` / `DISABLE` / `DELETE`）。审计经 `db.audit.append()`
的独立事务写入，因此即使随后业务事务回滚，「发生过这次变更」的记录仍在（见 `db/audit.py`）。

## `PreferenceRuleSource`（`source_decision_ids` 的规范化）

来源决策以 `PreferenceRuleSource(rule_id, decision_id)` 复合主键行存储（天然去重），而不是把 id 列表
塞进一个 JSON 列：这样「这条规则来自哪些决策」可被 join 查询，也让 `low_evidence` 的判定
（`count < 2`）落在同一处真相上。编辑规则时若传入新的来源集合，整批替换。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import audit
from app.db import models as orm
from app.tools.models import PreferenceForm

__all__ = [
    "MAX_ENABLED_RULES",
    "MIN_EVIDENCE_DECISIONS",
    "PreferenceRuleLimitError",
    "PreferenceRuleNotFoundError",
    "PreferenceRuleOutOfScopeError",
    "PreferenceRuleView",
    "create_rule",
    "delete_rule",
    "get_rule",
    "list_rules",
    "set_enabled",
    "update_rule",
]

#: 启用状态偏好规则数量上限（R18.11）。上限是「启用」这个状态的，未启用规则不计数、无上限。
MAX_ENABLED_RULES: Final = 20

#: 证据充分的门槛（R18.10）：少于这么多条来源决策即标 `low_evidence`。
MIN_EVIDENCE_DECISIONS: Final = 2

#: 审计 `actor`。P0 单一 Planner，全部手写规则动作都记在 PLANNER 名下。
_ACTOR: Final = "PLANNER"

#: `PreferenceForm` 判别联合的运行期校验器。用它把「来自 API 或 DB 的原始 dict」校验成 4 类之一，
#: 越界（如指向硬约束开关的 component）在此抛 `ValidationError`，由 `_validate_form` 翻译。
_FORM_ADAPTER: Final[TypeAdapter[Any]] = TypeAdapter(PreferenceForm)


class PreferenceRuleOutOfScopeError(Exception):
    """`structured_form` 越界：指向硬约束开关或非软目标 component（R18.8、EVAL-206）。

    这不是「参数格式不对」而是「这类规则不允许存在」——偏好规则只能改变打分排序，永不放宽
    硬约束。API 层把它翻译成 `PREFERENCE_RULE_OUT_OF_SCOPE`。
    """


class PreferenceRuleLimitError(Exception):
    """启用状态规则已达 `MAX_ENABLED_RULES`（R18.11）。要求先停用既有规则。"""


class PreferenceRuleNotFoundError(Exception):
    """按 `rule_id` 找不到规则。"""

    def __init__(self, rule_id: str) -> None:
        self.rule_id = rule_id
        super().__init__(f"偏好规则 {rule_id} 不存在")


@dataclass(frozen=True, slots=True)
class PreferenceRuleView:
    """一条偏好规则的只读视图。`frozen`：读出后不应在服务边界之外被改写。

    `structured_form` 原样透传存库的 dict（4 类判别联合之一）；`source_decision_ids` 从
    `preference_rule_sources` 行聚合而来，排序稳定以保证同数据同输出。
    """

    rule_id: str
    human_text: str
    structured_form: dict[str, Any]
    enabled: bool
    low_evidence: bool
    created_at: datetime
    updated_at: datetime
    created_by: str
    source_decision_ids: tuple[str, ...]


# --------------------------------------------------------------------------
# 校验（R18.8 的服务层一道）
# --------------------------------------------------------------------------


def _validate_form(structured_form: dict[str, Any]) -> dict[str, Any]:
    """把原始 `structured_form` 校验成 4 类 `PreferenceForm` 之一，返回归一化后的 dict。

    越界（未知 `kind`、指向硬约束开关的 `component`、`weight_delta`/`multiplier` 出界）在此
    抛 `PreferenceRuleOutOfScopeError`。这是 design.md §4.3 四道结构性保障的第 3 道在服务层的
    落点：即便调用方绕过 API 的 Pydantic 体、直接把一个恶意 dict 传进来，这里仍会拒。
    """
    try:
        model = _FORM_ADAPTER.validate_python(structured_form)
    except ValidationError as error:
        raise PreferenceRuleOutOfScopeError(
            "偏好规则的 structured_form 超出允许范围：只支持 AVOID_MACHINE_FOR_ORDER / "
            "AVOID_MACHINE_FOR_PRODUCT / PREFER_WORKER_FOR_SKILL / ADJUST_OBJECTIVE_WEIGHT，"
            "且 ADJUST_OBJECTIVE_WEIGHT 只能作用于 6 个软目标分量。偏好规则不能放宽任何硬约束。"
        ) from error
    # `mode="json"` 让 datetime 等类型落成可 JSON 序列化的形态；这里都是标量与字面量，稳定。
    return model.model_dump(mode="json")


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def _sources_of(session: Session, rule_id: str) -> tuple[str, ...]:
    rows = session.execute(
        select(orm.PreferenceRuleSource.decision_id)
        .where(orm.PreferenceRuleSource.rule_id == rule_id)
        .order_by(orm.PreferenceRuleSource.decision_id)
    ).scalars().all()
    return tuple(rows)


def _to_view(session: Session, row: orm.PreferenceRule) -> PreferenceRuleView:
    form = row.structured_form if isinstance(row.structured_form, dict) else {}
    return PreferenceRuleView(
        rule_id=row.rule_id,
        human_text=row.human_text,
        structured_form=dict(form),
        enabled=row.enabled,
        low_evidence=row.low_evidence,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        source_decision_ids=_sources_of(session, row.rule_id),
    )


def _get_or_raise(session: Session, rule_id: str) -> orm.PreferenceRule:
    row = session.get(orm.PreferenceRule, rule_id)
    if row is None:
        raise PreferenceRuleNotFoundError(rule_id)
    return row


def get_rule(session: Session, rule_id: str) -> PreferenceRuleView:
    """按 id 取一条规则，不存在抛 `PreferenceRuleNotFoundError`。"""
    return _to_view(session, _get_or_raise(session, rule_id))


def list_rules(session: Session, *, enabled_only: bool = False) -> list[PreferenceRuleView]:
    """列出规则，按 `created_at` 升序（稳定，同数据同顺序）。`enabled_only` 只返回已启用。"""
    stmt = select(orm.PreferenceRule).order_by(
        orm.PreferenceRule.created_at, orm.PreferenceRule.rule_id
    )
    if enabled_only:
        stmt = stmt.where(orm.PreferenceRule.enabled.is_(True))
    rows = session.execute(stmt).scalars().all()
    return [_to_view(session, row) for row in rows]


def enabled_rule_count(session: Session) -> int:
    """当前启用状态规则数（R18.11 的计数点）。"""
    return int(
        session.execute(
            select(func.count())
            .select_from(orm.PreferenceRule)
            .where(orm.PreferenceRule.enabled.is_(True))
        ).scalar_one()
    )


# --------------------------------------------------------------------------
# 写（每一次都写 PREFERENCE_RULE_CHANGE 审计）
# --------------------------------------------------------------------------


def _replace_sources(
    session: Session, rule_id: str, source_decision_ids: Sequence[str]
) -> tuple[str, ...]:
    """整批替换某规则的来源决策行，返回去重排序后的实际集合。"""
    session.query(orm.PreferenceRuleSource).filter(
        orm.PreferenceRuleSource.rule_id == rule_id
    ).delete(synchronize_session=False)
    unique = sorted(set(source_decision_ids))
    for decision_id in unique:
        session.add(
            orm.PreferenceRuleSource(rule_id=rule_id, decision_id=decision_id)
        )
    return tuple(unique)


def _is_low_evidence(source_decision_ids: Sequence[str]) -> bool:
    return len(set(source_decision_ids)) < MIN_EVIDENCE_DECISIONS


def _audit(
    *, event_type: str, rule: orm.PreferenceRule, extra: dict[str, Any] | None = None
) -> None:
    payload: dict[str, Any] = {
        "rule_id": rule.rule_id,
        "human_text": rule.human_text,
        "structured_form": rule.structured_form,
        "enabled": rule.enabled,
        "low_evidence": rule.low_evidence,
    }
    if extra:
        payload.update(extra)
    audit.append(
        event_category="PREFERENCE_RULE_CHANGE",
        event_type=event_type,
        actor=_ACTOR,
        subject_type="PREFERENCE_RULE",
        subject_id=rule.rule_id,
        payload=payload,
    )


def create_rule(
    session: Session,
    *,
    human_text: str,
    structured_form: dict[str, Any],
    source_decision_ids: Sequence[str] = (),
    now: datetime | None = None,
    created_by: str = _ACTOR,
) -> PreferenceRuleView:
    """手写创建一条偏好规则（R18.3 前半句，P0 唯一建规则入口）。

    **恒 `enabled=False`**（R18.4）：本函数没有 `enabled` 参数，创建即未启用，启用是随后一次
    显式的 `set_enabled(..., True)` 动作。`structured_form` 先经 `_validate_form` 越界校验；
    `source_decision_ids` 少于 2 条则 `low_evidence=True`（R18.10）。写一条 `CREATE` 审计。
    """
    normalized = _validate_form(structured_form)
    moment = now if now is not None else datetime.now()  # noqa: DTZ005
    rule_id = f"PR-{uuid4().hex[:12]}"
    rule = orm.PreferenceRule(
        rule_id=rule_id,
        human_text=human_text,
        structured_form=normalized,
        enabled=False,
        low_evidence=_is_low_evidence(source_decision_ids),
        created_at=moment,
        updated_at=moment,
        created_by=created_by,
    )
    session.add(rule)
    session.flush()  # 让外键可用于 sources 行
    sources = _replace_sources(session, rule_id, source_decision_ids)
    session.flush()
    _audit(event_type="CREATE", rule=rule, extra={"source_decision_ids": list(sources)})
    return _to_view(session, rule)


def update_rule(
    session: Session,
    rule_id: str,
    *,
    human_text: str | None = None,
    structured_form: dict[str, Any] | None = None,
    source_decision_ids: Sequence[str] | None = None,
    now: datetime | None = None,
) -> PreferenceRuleView:
    """编辑规则的 `human_text` / `structured_form` / 来源决策（R18.6）。

    **不改 `enabled`**：本函数无法启用或停用规则——启用状态只经 `set_enabled` 变更，避免通用编辑
    路径静默把规则启用（第 3 点要求「显式启用」）。改 `structured_form` 时重新越界校验；改来源集合
    时整批替换并据新集合重算 `low_evidence`。写一条 `UPDATE` 审计。
    """
    rule = _get_or_raise(session, rule_id)
    if human_text is not None:
        rule.human_text = human_text
    if structured_form is not None:
        rule.structured_form = _validate_form(structured_form)
    if source_decision_ids is not None:
        sources = _replace_sources(session, rule_id, source_decision_ids)
        rule.low_evidence = _is_low_evidence(sources)
    rule.updated_at = now if now is not None else datetime.now()  # noqa: DTZ005
    session.flush()
    _audit(
        event_type="UPDATE",
        rule=rule,
        extra={"source_decision_ids": list(_sources_of(session, rule_id))},
    )
    return _to_view(session, rule)


def set_enabled(
    session: Session,
    rule_id: str,
    enabled: bool,
    *,
    now: datetime | None = None,
) -> PreferenceRuleView:
    """显式启用或停用一条规则（R18.4、R18.9、R18.11）。

    这是**唯一**能改 `enabled` 的入口，也是那次「显式人工确认」动作在服务层的落点。启用时先统计
    当前启用数，达 `MAX_ENABLED_RULES` → `PreferenceRuleLimitError`（R18.11）。已是目标状态则是幂等
    的（仍写审计留痕这次动作）。停用后规则下一次排产被完全忽略，因为快照只加载 `enabled=True`
    的规则（见 `services/snapshot_loader.py`）。写一条 `ENABLE` / `DISABLE` 审计。
    """
    rule = _get_or_raise(session, rule_id)
    if enabled and not rule.enabled and enabled_rule_count(session) >= MAX_ENABLED_RULES:
        raise PreferenceRuleLimitError(
            f"启用状态的偏好规则已达上限 {MAX_ENABLED_RULES} 条，请先停用一条既有规则再启用。"
        )
    rule.enabled = enabled
    rule.updated_at = now if now is not None else datetime.now()  # noqa: DTZ005
    session.flush()
    _audit(event_type="ENABLE" if enabled else "DISABLE", rule=rule)
    return _to_view(session, rule)


def delete_rule(session: Session, rule_id: str, *, now: datetime | None = None) -> None:
    """删除一条规则（R18.6）。级联删除其 `preference_rule_sources` 行（ondelete=CASCADE）。

    删除前先写 `DELETE` 审计——审计走独立事务，但把它放在删除前，语义上「先记录再动手」更清晰，
    且审计载荷此刻还能读到规则的完整内容。删除的是业务表行，审计行不受影响（append-only）。
    """
    rule = _get_or_raise(session, rule_id)
    _audit(
        event_type="DELETE",
        rule=rule,
        extra={"source_decision_ids": list(_sources_of(session, rule_id))},
    )
    session.delete(rule)
    session.flush()


# --------------------------------------------------------------------------
# 「影响了哪些作业」（R18.7 的 P0 展示入口）
# --------------------------------------------------------------------------


def affected_job_ids(
    session: Session, rule_id: str, *, plan_id: str | None = None, limit: int = 50
) -> list[str]:
    """返回当前（或指定）计划里被该规则命中的 `job_id`，供前端「影响了哪些作业」入口。

    匹配口径与 design.md §4.3 的 `preference_penalty()` 逐规则命中一致（但**不打分**——本函数
    只回答「哪些作业」，分值归 11.2）：
      - `AVOID_MACHINE_FOR_ORDER`：该订单排在该机器上的作业
      - `AVOID_MACHINE_FOR_PRODUCT`：该产品排在该机器上的作业
      - `PREFER_WORKER_FOR_SKILL`：需要该技能但**未**用该工人的作业
      - `ADJUST_OBJECTIVE_WEIGHT`：不命中具体作业（它改的是目标权重，不针对单个作业），返回空

    `plan_id` 缺省取当前 `ACTIVE` 计划；没有 ACTIVE 计划则返回空（台账早于任何计划时的诚实空态）。
    这是一个只读展示辅助，不参与任何确定性排产断言。
    """
    rule = _get_or_raise(session, rule_id)
    form = rule.structured_form if isinstance(rule.structured_form, dict) else {}
    kind = form.get("kind")
    if kind == "ADJUST_OBJECTIVE_WEIGHT":
        return []

    if plan_id is None:
        active = session.execute(
            select(orm.ProductionPlan.plan_id).where(orm.ProductionPlan.status == "ACTIVE")
        ).scalars().first()
        if active is None:
            return []
        plan_id = active

    stmt = (
        select(orm.ScheduledJob.job_id)
        .join(orm.ProductionJob, orm.ProductionJob.job_id == orm.ScheduledJob.job_id)
        .where(orm.ScheduledJob.plan_id == plan_id)
    )
    if kind == "AVOID_MACHINE_FOR_ORDER":
        stmt = stmt.where(
            orm.ProductionJob.order_id == form.get("order_id"),
            orm.ScheduledJob.machine_id == form.get("machine_id"),
        )
    elif kind == "AVOID_MACHINE_FOR_PRODUCT":
        stmt = stmt.where(
            orm.ProductionJob.product_id == form.get("product_id"),
            orm.ScheduledJob.machine_id == form.get("machine_id"),
        )
    elif kind == "PREFER_WORKER_FOR_SKILL":
        stmt = stmt.where(
            orm.ProductionJob.required_worker_skill == form.get("skill"),
            orm.ScheduledJob.worker_id != form.get("worker_id"),
        )
    else:  # pragma: no cover - 判别联合已穷尽，防御性分支
        return []

    stmt = stmt.order_by(orm.ScheduledJob.job_id).limit(limit)
    return list(session.execute(stmt).scalars().all())
