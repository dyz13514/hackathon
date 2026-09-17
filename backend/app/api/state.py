"""状态看板端点（design.md Components §6 `/` 行，任务 3.7）。

一个只读端点：`GET /state/dashboard` —— 五类实体（Order / Material / Machine /
Worker / ProductionPlan）的当前状态，供状态看板首屏渲染（R1.1–R1.5）。

## 为什么是读端点、不挂 `Session_Auth`

R1 是「生产状态可见性」，看板在演示里第一屏就要能看。`SessionAuthMiddleware` 只拦
`POST/PUT/PATCH/DELETE`（`api/deps.py`），`GET` 天然放行——与 `GET /plans/active`、
`GET /health` 同一口径。这一层因此不标注 `PlannerSession`。

## 每条都带 `source` 与 `last_updated_at`（R1.3）

五张实体表里 Order/Material/Machine/Worker 都有「来源追溯四件套」中的 `source` 与
`last_updated_at`（`db/models.py`）。`ProductionPlan` 没有 `source` 列——它不是被导入
的外部数据，而是系统生成的产物——因此用 `origin`（生成来源，如 `PLAN_GENERATION`）填
`source`，用 `created_at` 填 `last_updated_at`。这样五类实体在响应里形状一致，前端不必
为计划卡片写一套特例。

## Order.notes 与 `injection_suspected`（R1.4、R23.1）

`notes` 是不受信任输入。本层**原样透传**文本与 `injection_suspected` 标志，不做任何转义
或解释——转义是渲染层的事（HTML 上下文才谈得上转义），而「不解释指令语义」在前端是
「渲染为纯文本」。后端的职责只是如实把两个字段送出去，让前端能打 `untrusted` 徽章。

## 只列 `ACTIVE` 记录

四张实体表都有 `record_status`（`ACTIVE` / `REVERTED`）。看板只展示 `ACTIVE` 行——
被批次回滚的 `REVERTED` 记录不参与排产（R3.4），也就不该出现在「当前状态」里。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as orm

router = APIRouter(prefix="/state", tags=["state"])


# --------------------------------------------------------------------------
# 响应契约
# --------------------------------------------------------------------------


class OrderStateOut(BaseModel):
    """一条订单的当前状态。`notes` 与 `injection_suspected` 供前端打不受信任徽章（R1.4）。"""

    model_config = ConfigDict(extra="forbid")

    order_id: str
    product_id: str
    quantity: float
    due_date: datetime
    priority: str
    notes: str | None
    injection_suspected: bool
    source: str
    last_updated_at: datetime


class MaterialStateOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_id: str
    name: str
    unit: str
    quantity_available: float
    reserved_quantity: float
    source: str
    last_updated_at: datetime


class MachineStateOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    machine_id: str
    machine_type: str
    status: str
    source: str
    last_updated_at: datetime


class WorkerStateOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str
    name: str
    source: str
    last_updated_at: datetime


class PlanStateOut(BaseModel):
    """一条计划的当前状态。`source` 取 `origin`、`last_updated_at` 取 `created_at`
    （见模块 docstring）。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    production_date: datetime
    status: str
    feasibility: str
    plan_version: int
    source: str
    last_updated_at: datetime


class DashboardOut(BaseModel):
    """`GET /state/dashboard` 的响应契约：五类实体的当前状态（R1.1）。"""

    model_config = ConfigDict(extra="forbid")

    orders: list[OrderStateOut]
    materials: list[MaterialStateOut]
    machines: list[MachineStateOut]
    workers: list[WorkerStateOut]
    plans: list[PlanStateOut]


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


@router.get(
    "/dashboard",
    response_model=DashboardOut,
    summary="五类实体的当前状态，供状态看板首屏（R1.1–R1.5）",
)
def dashboard(request: Request) -> DashboardOut:
    """五类实体的当前状态。只读端点，无认证（与其余 GET 同口径）。

    只列 `record_status = 'ACTIVE'` 的实体行（`REVERTED` 记录不参与排产，R3.4）；
    计划列全部行，按生产日与 ID 稳定排序，使首屏渲染确定可重现。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        orders = [
            OrderStateOut(
                order_id=o.order_id,
                product_id=o.product_id,
                quantity=float(o.quantity),
                due_date=o.due_date,
                priority=o.priority,
                notes=o.notes,
                injection_suspected=o.injection_suspected,
                source=o.source,
                last_updated_at=o.last_updated_at,
            )
            for o in db.execute(
                select(orm.Order)
                .where(orm.Order.record_status == "ACTIVE")
                .order_by(orm.Order.order_id)
            ).scalars()
        ]
        materials = [
            MaterialStateOut(
                material_id=m.material_id,
                name=m.name,
                unit=m.unit,
                quantity_available=float(m.quantity_available),
                reserved_quantity=float(m.reserved_quantity),
                source=m.source,
                last_updated_at=m.last_updated_at,
            )
            for m in db.execute(
                select(orm.Material)
                .where(orm.Material.record_status == "ACTIVE")
                .order_by(orm.Material.material_id)
            ).scalars()
        ]
        machines = [
            MachineStateOut(
                machine_id=m.machine_id,
                machine_type=m.machine_type,
                status=m.status,
                source=m.source,
                last_updated_at=m.last_updated_at,
            )
            for m in db.execute(
                select(orm.Machine)
                .where(orm.Machine.record_status == "ACTIVE")
                .order_by(orm.Machine.machine_id)
            ).scalars()
        ]
        workers = [
            WorkerStateOut(
                worker_id=w.worker_id,
                name=w.name,
                source=w.source,
                last_updated_at=w.last_updated_at,
            )
            for w in db.execute(
                select(orm.Worker)
                .where(orm.Worker.record_status == "ACTIVE")
                .order_by(orm.Worker.worker_id)
            ).scalars()
        ]
        plans = [
            PlanStateOut(
                plan_id=p.plan_id,
                production_date=datetime(
                    p.production_date.year, p.production_date.month, p.production_date.day
                ),
                status=p.status,
                feasibility=p.feasibility,
                plan_version=p.plan_version,
                source=p.origin,
                last_updated_at=p.created_at,
            )
            for p in db.execute(
                select(orm.ProductionPlan).order_by(
                    orm.ProductionPlan.production_date, orm.ProductionPlan.plan_id
                )
            ).scalars()
        ]

    return DashboardOut(
        orders=orders,
        materials=materials,
        machines=machines,
        workers=workers,
        plans=plans,
    )
