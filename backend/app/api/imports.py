"""电子表格摄取端点（design.md §4.2/§6 `/import`，任务 10，R2/R3）。

P0 的摄取入口，全部确定性、无 LIVE LLM：

- `POST /api/imports/upload`（multipart）—— 安全闸门 + 解析 + 暂存，返回 `upload_id`
  （R2.1、R23.7）。重复 `file_checksum` 提示上次导入时间（R3.6）。
- `GET  /api/imports/{upload_id}/proposal` —— 确定性列映射提议 + 有界预览（R2.2/2.6/2.7）。
- `POST /api/imports/{upload_id}/validate` —— 确定性校验，返回 `unparsed_cells` 等（R2.8）。
- `POST /api/imports/{upload_id}/confirm` —— 人工确认后落库（R2.9/3.2）；经 `AcceptedMapping`
  闸门（低置信/缺必填/未处置 unparsed → 拒绝，不写库）。落库后触发风险扫描。
- `GET  /api/imports` —— 列出批次。
- `POST /api/imports/{batch_id}/revert` —— 整批回滚（R3.4/3.5）。

写端点受 `Session_Auth` 保护；读端点无需认证（与其余 GET 同口径）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.deps import PlannerSession
from app.api.errors import ErrorCode, NextAction, error_response
from app.db import models as orm
from app.services import ingestion
from app.services.ingestion import (
    AcceptedMapping,
    AcceptedMappingError,
    UploadStore,
    _Upload,
)
from app.services.risk_triggers import trigger_scan
from app.services.spreadsheet import (
    SpreadsheetError,
    build_preview,
    compute_checksum,
    parse_spreadsheet,
)

router = APIRouter(prefix="/imports", tags=["imports"])


def _store(request: Request) -> UploadStore:
    store = getattr(request.app.state, "upload_store", None)
    if store is None:
        store = UploadStore()
        request.app.state.upload_store = store
    return store


# --------------------------------------------------------------------------
# 响应契约
# --------------------------------------------------------------------------


class UploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: str
    file_name: str
    total_rows: int
    duplicate_of: str | None = None
    last_imported_at: str | None = None


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: str
    field_mappings: list[dict]
    unparsed_cells_resolved: bool = False


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


@router.post(
    "/upload", response_model=UploadResponse, summary="上传表格并安全解析（R2.1/R23.7）"
)
async def upload_import(
    request: Request,
    session: PlannerSession,
    file: Annotated[UploadFile, File()],
) -> UploadResponse | JSONResponse:
    content = await file.read()
    try:
        parsed = parse_spreadsheet(filename=file.filename or "upload", content=content)
    except SpreadsheetError as error:
        return error_response(
            status_code=422,
            code=_code_for(error.code),
            message=str(error),
            next_actions=[NextAction(action="reupload", href="/import")],
        )
    checksum = compute_checksum(content)
    upload_id = f"UP-{uuid.uuid4().hex[:12]}"
    _store(request).put(
        upload_id,
        _Upload(filename=file.filename or "upload", parsed=parsed, checksum=checksum),
    )

    # 重复上传检测（R3.6）：同 checksum 的既有批次 → 提示上次导入时间。
    factory = request.app.state.session_factory
    duplicate_of = None
    last_imported = None
    with factory() as db:
        prior = db.execute(
            select(orm.ImportBatch)
            .where(orm.ImportBatch.file_checksum == checksum)
            .order_by(orm.ImportBatch.created_at.desc())
        ).scalars().first()
        if prior is not None:
            duplicate_of = prior.batch_id
            last_imported = prior.imported_at.isoformat() if prior.imported_at else None

    return UploadResponse(
        upload_id=upload_id,
        file_name=file.filename or "upload",
        total_rows=parsed.row_count,
        duplicate_of=duplicate_of,
        last_imported_at=last_imported,
    )


@router.get("/{upload_id}/proposal", summary="确定性列映射提议 + 有界预览（R2.2/2.6/2.7）")
def get_proposal(request: Request, upload_id: str) -> JSONResponse:
    try:
        upload = _store(request).get(upload_id)
    except KeyError:
        return _upload_not_found(upload_id)
    # 列映射经真实 Ingestion_Agent ReAct 路径（STUB/REPLAY，不触网）；STUB 无 cassette 时诚实
    # 回退确定性提议（run_ingestion_mapping 内部处理，返回 from_agent 标注）。惰性 import
    # 打破 app.api ← imports ← ingestion_agent_run ← orchestrator ← budget ← app.api.admin 的环。
    from app.services.ingestion_agent_run import run_ingestion_mapping

    run = run_ingestion_mapping(
        parsed=upload.parsed,
        upload_id=upload_id,
        adapter=request.app.state.llm_adapter,
    )
    proposal = run.proposed_mapping
    preview = build_preview(upload.parsed)
    return JSONResponse(
        {
            "upload_id": upload_id,
            "proposal": proposal,
            "agent_outcome": run.agent_outcome,
            "from_agent": run.from_agent,
            "preview": {
                "detected_header_row": preview.detected_header_row,
                "total_rows": preview.total_rows,
                "preview_tokens": preview.preview_tokens,
                "columns": [
                    {
                        "index": c.index,
                        "raw_header": c.raw_header,
                        "inferred_kind": c.inferred_kind,
                        "null_ratio": c.null_ratio,
                        "sample_values": c.sample_values,
                    }
                    for c in preview.columns
                ],
                "formula_columns": preview.formula_columns,
            },
        }
    )


@router.post("/{upload_id}/validate", summary="确定性校验映射（R2.8）")
def validate_import(
    request: Request, upload_id: str, body: dict, session: PlannerSession
) -> JSONResponse:
    try:
        upload = _store(request).get(upload_id)
    except KeyError:
        return _upload_not_found(upload_id)
    outcome = ingestion.validate_mapping(upload.parsed, body.get("mapping", body))
    return JSONResponse(
        {
            "upload_id": upload_id,
            "parsed_row_count": outcome.parsed_row_count,
            "unparsed_cells": outcome.unparsed_cells,
            "type_error_count": outcome.type_error_count,
            "normalisation_failure_count": outcome.normalisation_failure_count,
        }
    )


@router.post("/{upload_id}/confirm", summary="人工确认后落库（R2.9/R3.2）")
def confirm_import(
    request: Request, upload_id: str, body: ConfirmRequest, session: PlannerSession
) -> JSONResponse:
    try:
        upload = _store(request).get(upload_id)
    except KeyError:
        return _upload_not_found(upload_id)

    try:
        accepted = AcceptedMapping(
            upload_id=upload_id,
            entity_type=body.entity_type,
            field_mappings=body.field_mappings,
            unparsed_cells_resolved=body.unparsed_cells_resolved,
        )
    except AcceptedMappingError as error:
        return error_response(
            status_code=422,
            code=ErrorCode.IMPORT_MAPPING_INCOMPLETE,
            message=str(error),
            next_actions=[NextAction(action="fix_mapping", href="/import")],
        )

    from app.services.ingestion_agent_run import run_ingestion_mapping

    factory = request.app.state.session_factory
    # 持久化 Agent 的列映射提案到 import_batches.proposed_mapping（spec 10.4/10.6）。
    run = run_ingestion_mapping(
        parsed=upload.parsed,
        upload_id=upload_id,
        adapter=request.app.state.llm_adapter,
        entity_type_hint=body.entity_type,
    )
    with factory() as db:
        result = ingestion.commit_batch(
            db,
            parsed=upload.parsed,
            accepted=accepted,
            file_name=upload.filename,
            file_checksum=upload.checksum,
            proposed_mapping=run.proposed_mapping,
        )
    # 数据变更后触发风险扫描（R14.1 第 2 类），尽力而为、独立会话。
    trigger_scan(request.app.state.session_factory, trigger="DATA_CHANGE")
    return JSONResponse(
        {
            "batch_id": result.batch_id,
            "entity_type": result.entity_type,
            "imported_row_count": result.imported_row_count,
        }
    )


@router.get("", summary="列出导入批次")
def list_imports(request: Request) -> JSONResponse:
    factory = request.app.state.session_factory
    with factory() as db:
        rows = db.execute(
            select(orm.ImportBatch).order_by(orm.ImportBatch.created_at.desc())
        ).scalars().all()
        items = [
            {
                "batch_id": r.batch_id,
                "file_name": r.file_name,
                "entity_type": r.entity_type,
                "row_count": r.row_count,
                "status": r.status,
                "imported_at": r.imported_at.isoformat() if r.imported_at else None,
            }
            for r in rows
        ]
    return JSONResponse({"batches": items})


@router.post("/{batch_id}/revert", summary="整批回滚（R3.4/R3.5）")
def revert_import(request: Request, batch_id: str, session: PlannerSession) -> JSONResponse:
    factory = request.app.state.session_factory
    with factory() as db:
        batch = db.get(orm.ImportBatch, batch_id)
        if batch is None:
            return error_response(
                status_code=404,
                code=ErrorCode.IMPORT_BATCH_NOT_FOUND,
                message=f"批次 {batch_id} 不存在。",
                next_actions=[NextAction(action="list_imports", href="/import")],
            )
        reverted = ingestion.revert_batch(db, batch_id=batch_id)
    trigger_scan(request.app.state.session_factory, trigger="DATA_CHANGE")
    return JSONResponse({"batch_id": batch_id, "reverted_row_count": reverted})


def _upload_not_found(upload_id: str) -> JSONResponse:
    return error_response(
        status_code=404,
        code=ErrorCode.UPLOAD_NOT_FOUND,
        message=f"上传 {upload_id} 不存在或已过期，请重新上传。",
        next_actions=[NextAction(action="reupload", href="/import")],
        details={"upload_id": upload_id},
    )


def _code_for(spreadsheet_code: str) -> ErrorCode:
    return {
        "MACRO_NOT_ALLOWED": ErrorCode.MACRO_NOT_ALLOWED,
        "FILE_TOO_LARGE": ErrorCode.FILE_TOO_LARGE,
        "TOO_MANY_ROWS": ErrorCode.TOO_MANY_ROWS,
        "UNSUPPORTED_FILE_TYPE": ErrorCode.UNSUPPORTED_FILE_TYPE,
    }[spreadsheet_code]
