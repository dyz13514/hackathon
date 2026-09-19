"""偏好规则管理端点（design.md Components §5「偏好」、§6 `/preferences`，任务 11.1，R18）。

**P0 唯一的建规则入口**（`CREATE_PREFERENCE_RULE`，无 LLM）。规划员在管理界面手写 `human_text`
+ 四类 `structured_form` 之一的参数，本端点把它交给 `Preference_Store` 落库。蒸馏路径
（`POST /api/preferences/distil` + `propose_preference_rule` 接线）属 P1（任务 13.3），此处不实现。

## 端点

- `POST   /api/preferences`               创建规则（**恒 enabled=false**，见下）
- `GET    /api/preferences`               列出规则（`?enabled_only=true` 只看已启用）
- `GET    /api/preferences/{rule_id}`     取单条
- `PATCH  /api/preferences/{rule_id}`     编辑 human_text / structured_form / 来源决策
                                          （**不能改 enabled**，见下）
- `POST   /api/preferences/{rule_id}/enable`   显式启用（独立可审计动作，受 20 条上限约束）
- `POST   /api/preferences/{rule_id}/disable`  停用
- `DELETE /api/preferences/{rule_id}`     删除
- `GET    /api/preferences/{rule_id}/affected-jobs`  「影响了哪些作业」（R18.7 展示入口）

## 「显式启用」在 API 边界的两道强制（tasks.md 11.1 第 3 点）

1. **创建接口不接受 enabled=true**：`CreatePreferenceRequest` 里根本没有 `enabled` 字段
   （`extra="forbid"`），因此请求体里写 `"enabled": true` 会被 Pydantic 拒为 422，而不是被静默接受。
   创建后规则一律 `enabled=false`。
2. **通用 PATCH 不能启用**：`UpdatePreferenceRequest` 同样没有 `enabled` 字段。启用/停用只能经
   `/enable`、`/disable` 两条**独立**的、各自写审计的动作。这样「启用」永远是一次有意的、可在
   审计日志里单独看到的动作，而不是夹在一次字段编辑里悄悄发生。

## 越界与上限

- `structured_form` 越界（指向硬约束开关或非软目标 component）→ 服务层抛
  `PreferenceRuleOutOfScopeError`，翻译成 `PREFERENCE_RULE_OUT_OF_SCOPE`（422，R18.8、EVAL-206）。
  Pydantic 的判别联合先拦一层，服务层再判一道。
- 启用数达 20 → `PreferenceRuleLimitError` → `PREFERENCE_RULE_LIMIT_REACHED`（409，R18.11）。

写端点一律经 `Session_Auth`（`session: PlannerSession`）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import PlannerSession
from app.api.errors import ErrorCode, NextAction, error_response
from app.db import models as orm
from app.seed.dataset import DEMO_ANCHOR
from app.services import preferences as store
from app.services.preferences import (
    PreferenceRuleLimitError,
    PreferenceRuleNotFoundError,
    PreferenceRuleOutOfScopeError,
    PreferenceRuleView,
)
from app.tools.models import PreferenceForm

router = APIRouter(prefix="/preferences", tags=["preferences"])


# --------------------------------------------------------------------------
# 请求 / 响应契约
# --------------------------------------------------------------------------


class CreatePreferenceRequest(BaseModel):
    """`POST /preferences` 请求体。**没有 `enabled` 字段**（extra=forbid 使 enabled=true 被拒）。

    `structured_form` 复用 `app.tools.models.PreferenceForm` 判别联合：4 类之一，越界的
    `component` 在此就被 Pydantic 拒（第一道结构性保障）。
    """

    model_config = ConfigDict(extra="forbid")

    human_text: str = Field(min_length=1, max_length=200)
    structured_form: PreferenceForm
    source_decision_ids: list[str] = Field(default_factory=list, max_length=10)


class UpdatePreferenceRequest(BaseModel):
    """`PATCH /preferences/{id}` 请求体。**没有 `enabled` 字段**：通用编辑不能启用/停用规则。

    三个字段全部可选，只更新提供的部分。`structured_form` 若提供则重新越界校验。
    """

    model_config = ConfigDict(extra="forbid")

    human_text: str | None = Field(default=None, min_length=1, max_length=200)
    structured_form: PreferenceForm | None = None
    source_decision_ids: list[str] | None = Field(default=None, max_length=10)


class PreferenceRuleOut(BaseModel):
    """一条偏好规则的可展示形态。逐字对应 `PreferenceRuleView`。"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    human_text: str
    structured_form: dict[str, Any]
    kind: str = Field(description="structured_form 的 4 类判别键，便于前端分组渲染")
    enabled: bool
    low_evidence: bool = Field(description="source_decision_ids < 2，界面提示证据不足（R18.10）")
    created_at: datetime
    updated_at: datetime
    created_by: str
    source_decision_ids: list[str]


class PreferenceListOut(BaseModel):
    """`GET /preferences` 响应：规则列表 + 启用计数与 20 条上限（供界面上限提示）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[PreferenceRuleOut]
    total: int
    enabled_count: int = Field(description="当前启用数，界面据此提示是否接近 20 条上限")
    max_enabled: int = Field(description="启用上限（R18.11），恒为 20")


class AffectedJobsOut(BaseModel):
    """`GET /preferences/{id}/affected-jobs` 响应：当前 ACTIVE 计划里被该规则命中的作业。"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    plan_id: str | None = Field(default=None, description="被检视的计划；无 ACTIVE 计划时为 null")
    job_ids: list[str]


# --------------------------------------------------------------------------
# 视图 → 响应
# --------------------------------------------------------------------------


def _to_out(view: PreferenceRuleView) -> PreferenceRuleOut:
    kind = view.structured_form.get("kind", "") if view.structured_form else ""
    return PreferenceRuleOut(
        rule_id=view.rule_id,
        human_text=view.human_text,
        structured_form=view.structured_form,
        kind=str(kind),
        enabled=view.enabled,
        low_evidence=view.low_evidence,
        created_at=view.created_at,
        updated_at=view.updated_at,
        created_by=view.created_by,
        source_decision_ids=list(view.source_decision_ids),
    )


def _factory(request: Request) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    return factory


def _not_found(rule_id: str) -> JSONResponse:
    return error_response(
        status_code=404,
        code=ErrorCode.PREFERENCE_RULE_NOT_FOUND,
        message=f"偏好规则 {rule_id} 不存在。",
        next_actions=[NextAction(action="list_preferences", href="/preferences")],
        details={"rule_id": rule_id},
    )


def _out_of_scope(message: str) -> JSONResponse:
    return error_response(
        status_code=422,
        code=ErrorCode.PREFERENCE_RULE_OUT_OF_SCOPE,
        message=message,
        next_actions=[NextAction(action="fix_rule", href="/preferences")],
    )


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


@router.get("", response_model=PreferenceListOut, summary="列出偏好规则（R18.6）")
def list_preferences(
    request: Request,
    enabled_only: bool = Query(default=False, description="只返回已启用规则"),
) -> PreferenceListOut:
    """只读列表。附启用计数与上限，供界面渲染「已启用 N/20」的上限提示。"""
    with _factory(request)() as db:
        views = store.list_rules(db, enabled_only=enabled_only)
        enabled_count = store.enabled_rule_count(db)
    return PreferenceListOut(
        items=[_to_out(v) for v in views],
        total=len(views),
        enabled_count=enabled_count,
        max_enabled=store.MAX_ENABLED_RULES,
    )


@router.post(
    "",
    response_model=PreferenceRuleOut,
    status_code=201,
    summary="手写创建偏好规则（P0 唯一入口，恒 enabled=false，R18.3/R18.4）",
)
def create_preference(
    request: Request, body: CreatePreferenceRequest, session: PlannerSession
) -> PreferenceRuleOut | JSONResponse:
    """创建一条规则。请求体无 `enabled` 字段 → 规则恒未启用，需随后 `/enable` 显式启用。写端点。"""
    with _factory(request)() as db:
        try:
            view = store.create_rule(
                db,
                human_text=body.human_text,
                structured_form=body.structured_form.model_dump(mode="json"),
                source_decision_ids=body.source_decision_ids,
                now=DEMO_ANCHOR,
            )
        except PreferenceRuleOutOfScopeError as error:
            db.rollback()
            return _out_of_scope(str(error))
        db.commit()
    return _to_out(view)


@router.get(
    "/{rule_id}", response_model=PreferenceRuleOut, summary="取单条偏好规则"
)
def get_preference(request: Request, rule_id: str) -> PreferenceRuleOut | JSONResponse:
    with _factory(request)() as db:
        try:
            view = store.get_rule(db, rule_id)
        except PreferenceRuleNotFoundError:
            return _not_found(rule_id)
    return _to_out(view)


@router.patch(
    "/{rule_id}",
    response_model=PreferenceRuleOut,
    summary="编辑规则（不能改 enabled；启用只经 /enable，R18.6）",
)
def update_preference(
    request: Request, rule_id: str, body: UpdatePreferenceRequest, session: PlannerSession
) -> PreferenceRuleOut | JSONResponse:
    """编辑 human_text / structured_form / 来源决策。请求体无 `enabled` 字段：不能静默启用。

    写端点。
    """
    with _factory(request)() as db:
        try:
            view = store.update_rule(
                db,
                rule_id,
                human_text=body.human_text,
                structured_form=(
                    body.structured_form.model_dump(mode="json")
                    if body.structured_form is not None
                    else None
                ),
                source_decision_ids=body.source_decision_ids,
                now=DEMO_ANCHOR,
            )
        except PreferenceRuleNotFoundError:
            db.rollback()
            return _not_found(rule_id)
        except PreferenceRuleOutOfScopeError as error:
            db.rollback()
            return _out_of_scope(str(error))
        db.commit()
    return _to_out(view)


@router.post(
    "/{rule_id}/enable",
    response_model=PreferenceRuleOut,
    summary="显式启用规则（独立可审计动作，受 20 条上限约束，R18.4/R18.11）",
)
def enable_preference(
    request: Request, rule_id: str, session: PlannerSession
) -> PreferenceRuleOut | JSONResponse:
    """启用一条规则。达 20 条上限 → PREFERENCE_RULE_LIMIT_REACHED（409）。写端点。"""
    with _factory(request)() as db:
        try:
            view = store.set_enabled(db, rule_id, True, now=DEMO_ANCHOR)
        except PreferenceRuleNotFoundError:
            db.rollback()
            return _not_found(rule_id)
        except PreferenceRuleLimitError as error:
            db.rollback()
            return error_response(
                status_code=409,
                code=ErrorCode.PREFERENCE_RULE_LIMIT_REACHED,
                message=str(error),
                next_actions=[
                    NextAction(action="disable_rule", href="/preferences"),
                ],
                details={"max_enabled": store.MAX_ENABLED_RULES},
            )
        db.commit()
    return _to_out(view)


@router.post(
    "/{rule_id}/disable",
    response_model=PreferenceRuleOut,
    summary="停用规则（下一次排产完全忽略，R18.9）",
)
def disable_preference(
    request: Request, rule_id: str, session: PlannerSession
) -> PreferenceRuleOut | JSONResponse:
    """停用一条规则。写端点。"""
    with _factory(request)() as db:
        try:
            view = store.set_enabled(db, rule_id, False, now=DEMO_ANCHOR)
        except PreferenceRuleNotFoundError:
            db.rollback()
            return _not_found(rule_id)
        db.commit()
    return _to_out(view)


@router.delete("/{rule_id}", status_code=204, summary="删除规则（R18.6）")
def delete_preference(
    request: Request, rule_id: str, session: PlannerSession
) -> JSONResponse | None:
    """删除一条规则及其来源决策关联行。写端点。"""
    with _factory(request)() as db:
        try:
            store.delete_rule(db, rule_id, now=DEMO_ANCHOR)
        except PreferenceRuleNotFoundError:
            db.rollback()
            return _not_found(rule_id)
        db.commit()
    return None


@router.get(
    "/{rule_id}/affected-jobs",
    response_model=AffectedJobsOut,
    summary="该规则在当前 ACTIVE 计划里影响了哪些作业（R18.7 展示入口）",
)
def affected_jobs(request: Request, rule_id: str) -> AffectedJobsOut | JSONResponse:
    """只读：返回被该规则命中的 job_id。ADJUST_OBJECTIVE_WEIGHT 不针对单作业，返回空列表。"""
    with _factory(request)() as db:
        try:
            store.get_rule(db, rule_id)  # 存在性检查
        except PreferenceRuleNotFoundError:
            return _not_found(rule_id)
        plan_id = db.execute(
            select(orm.ProductionPlan.plan_id).where(orm.ProductionPlan.status == "ACTIVE")
        ).scalars().first()
        job_ids = store.affected_job_ids(db, rule_id, plan_id=plan_id)
    return AffectedJobsOut(rule_id=rule_id, plan_id=plan_id, job_ids=job_ids)
