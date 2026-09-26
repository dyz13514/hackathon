"""Planner-managed material availability. Plan and trace history remain immutable."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import PlannerSession
from app.api.errors import ErrorCode, NextAction, error_response
from app.db import audit
from app.db import models as orm
from app.db.repositories import current_input_snapshot_version
from app.services.risk_triggers import trigger_scan

router = APIRouter(prefix="/materials", tags=["materials"])


class MaterialAvailabilityOut(BaseModel):
    material_id: str
    name: str
    unit: str
    quantity_available: Decimal
    reserved_quantity: Decimal
    input_snapshot_version: int


class UpdateAvailabilityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity_available: Decimal = Field(ge=0)
    reason: str = Field(min_length=5, max_length=200)


def _out(row: orm.Material, version: int) -> MaterialAvailabilityOut:
    return MaterialAvailabilityOut(
        material_id=row.material_id,
        name=row.name,
        unit=row.unit,
        quantity_available=row.quantity_available,
        reserved_quantity=row.reserved_quantity,
        input_snapshot_version=version,
    )


@router.get("/{material_id}", response_model=MaterialAvailabilityOut)
def get_material(request: Request, material_id: str) -> MaterialAvailabilityOut | JSONResponse:
    with request.app.state.session_factory() as db:
        row = db.get(orm.Material, material_id)
        if row is None or row.record_status != "ACTIVE":
            return error_response(
                status_code=404, code=ErrorCode.MATERIAL_NOT_FOUND,
                message=f"Active material {material_id} was not found.",
                next_actions=[NextAction(action="review_imports", href="/import")],
            )
        return _out(row, current_input_snapshot_version(db))


@router.patch("/{material_id}/availability", response_model=MaterialAvailabilityOut)
def update_availability(
    request: Request, material_id: str, body: UpdateAvailabilityIn,
    session: PlannerSession,
) -> MaterialAvailabilityOut | JSONResponse:
    resolved_now = datetime.now()
    with request.app.state.session_factory() as db:
        row = db.get(orm.Material, material_id)
        if row is None or row.record_status != "ACTIVE":
            return error_response(
                status_code=404, code=ErrorCode.MATERIAL_NOT_FOUND,
                message=f"Active material {material_id} was not found.",
                next_actions=[NextAction(action="review_imports", href="/import")],
            )
        old_quantity = row.quantity_available
        if old_quantity == body.quantity_available:
            return _out(row, current_input_snapshot_version(db))
        row.quantity_available = body.quantity_available
        row.source = "MANUAL_ENTRY"
        row.last_updated_at = resolved_now
        db.commit()
        db.refresh(row)
        version = current_input_snapshot_version(db)
        result = _out(row, version)
    audit.append(
        event_category="DATA_IMPORT", event_type="MATERIAL_AVAILABILITY_EDIT",
        actor="PLANNER",
        payload={
            "material_id": material_id,
            "old_quantity_available": str(old_quantity),
            "new_quantity_available": str(body.quantity_available),
            "reason": body.reason,
            "input_snapshot_version": version,
        },
        subject_type="Material", subject_id=material_id, occurred_at=resolved_now,
    )
    trigger_scan(
        request.app.state.session_factory, trigger="DATA_CHANGE",
        app_env=request.app.state.settings.app_env,
    )
    return result
