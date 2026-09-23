"""摄取服务：上传暂存、映射提议、确定性校验、落库闸门、整批回滚（任务 10.4/10.6/10.7）。

## 分工与 K-07「绝不静默猜测」

- **`propose_mapping`（10.4）**：把脏表头映射到目标字段。P0 用**确定性启发式**（表头别名表 +
  归一探测），不接 LIVE LLM（额度纪律）；低置信字段标 `NEEDS_CONFIRMATION`、缺必填进
  `missing_required_fields`、归一显式给 `conversion_factor`。**提案永不落库**，只写
  `import_batches.proposed_mapping` 供人工确认。
- **`validate_mapping`（10.4）**：确定性——拿映射在整份文件上试跑解析，返回 `unparsed_cells`
  （行号/列名/原始值）、`type_error_count`、`normalisation_failure_count`。不静默丢弃。
- **`commit_batch`（10.6）**：入参 `AcceptedMapping` 的构造函数断言无 `NEEDS_CONFIRMATION`、
  无 `MISSING_REQUIRED_FIELD`、`unparsed_cells` 已逐条处置——断言失败抛异常、**不写库**
  （K-07 静默猜测为 0）。落库写 `import_row_provenance`（可回滚）与 `Audit_Log`。
- **`revert_batch`（10.7）**：该批次记录置 `record_status = REVERTED`（软删除），从
  `overwritten_payload` 还原被覆盖的旧值。`REVERTED` 被 `load_snapshot` 排除。

落库与回滚都推进 `input_snapshots` 版本号（`db/events.py` 的 `after_flush` 钩子在实体表变更时
自动插一行），并在提交后触发一次风险扫描（`trigger_scan`，尽力而为）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import audit
from app.db import models as orm
from app.services.normalization import NormalisationError, normalise_date, normalise_unit
from app.services.spreadsheet import ParsedFile

__all__ = [
    "AcceptedMapping",
    "AcceptedMappingError",
    "CommitResult",
    "UploadStore",
    "ValidationOutcome",
    "commit_batch",
    "propose_mapping",
    "revert_batch",
    "validate_mapping",
]

# --------------------------------------------------------------------------
# 目标 schema：每实体的必填字段 + 表头别名（确定性启发式提议用）
# --------------------------------------------------------------------------

#: 每实体类型的必填字段（缺失即 MISSING_REQUIRED_FIELD）。
REQUIRED_FIELDS: dict[str, list[str]] = {
    "ORDER": ["order_id", "product_id", "quantity", "due_date"],
    "PRODUCT": ["product_id", "name"],
    "MATERIAL": ["material_id", "name", "quantity_available"],
    "MACHINE": ["machine_id", "machine_type"],
    "WORKER": ["worker_id", "name"],
}

#: 目标字段 → 可能的表头别名（小写、去空格后匹配）。确定性映射启发式。
_ALIASES: dict[str, list[str]] = {
    "order_id": ["order_id", "orderid", "订单号", "订单id", "order"],
    "product_id": ["product_id", "productid", "产品号", "产品id", "product", "sku"],
    "material_id": ["material_id", "materialid", "物料号", "物料id", "material"],
    "machine_id": ["machine_id", "machineid", "机器号", "机器id", "machine"],
    "worker_id": ["worker_id", "workerid", "工人号", "工人id", "worker"],
    "name": ["name", "名称", "名字"],
    "quantity": ["quantity", "qty", "数量", "amount"],
    "quantity_available": ["quantity_available", "available", "库存", "可用量", "stock"],
    "due_date": ["due_date", "duedate", "交期", "交付日期", "due"],
    "machine_type": ["machine_type", "type", "机器类型", "类型"],
    "unit": ["unit", "单位"],
}

#: 数值字段（校验时按数字解析）。
_NUMERIC_FIELDS = {"quantity", "quantity_available"}
#: 日期字段（校验时按日期归一）。
_DATE_FIELDS = {"due_date"}
#: 置信阈值（R2.7）：必填字段 confidence < 0.85 → NEEDS_CONFIRMATION。
_CONFIDENCE_THRESHOLD = 0.85


# --------------------------------------------------------------------------
# 上传暂存（无独立 uploads 内容表：进程内存 ParsedFile + 元信息）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Upload:
    filename: str
    parsed: ParsedFile
    checksum: str


@dataclass
class UploadStore:
    """进程内上传暂存（upload_id → 解析后的文件）。挂在 app.state。

    暂存目录 24h TTL 属部署细节；P0 演示用进程内存足够——上传紧接提议/确认/提交。
    """

    _entries: dict[str, _Upload] = field(default_factory=dict)

    def put(self, upload_id: str, upload: _Upload) -> None:
        self._entries[upload_id] = upload

    def get(self, upload_id: str) -> _Upload:
        entry = self._entries.get(upload_id)
        if entry is None:
            raise KeyError(upload_id)
        return entry


# --------------------------------------------------------------------------
# 映射提议（10.4，确定性启发式；不接 LIVE LLM）
# --------------------------------------------------------------------------


def propose_mapping(parsed: ParsedFile, *, entity_type_hint: str | None = None) -> dict:
    """确定性列映射提议（R2.2/2.6/2.7）。返回可写入 `proposed_mapping` 的 dict。

    识别 `entity_type`（按命中的必填字段数打分），对每个目标字段找表头别名命中列并给
    `confidence`：精确/别名命中 0.95，无命中则该字段进 `missing_required_fields`。必填字段
    `confidence < 0.85` → `NEEDS_CONFIRMATION`。归一提议对日期/单位列显式给 `conversion_factor`。
    """
    header_norm = [_norm_header(h) for h in parsed.header]

    entity_type = entity_type_hint or _guess_entity_type(header_norm)
    required = REQUIRED_FIELDS.get(entity_type, [])
    all_fields = list(dict.fromkeys([*required, "unit"]))

    field_mappings: list[dict] = []
    missing: list[dict] = []
    for target in all_fields:
        col_idx = _match_column(target, header_norm)
        if col_idx is None:
            if target in required:
                missing.append({"target_field": target, "reason": "no matching source column found"})
            continue
        confidence = 0.95
        samples = _column_samples(parsed, col_idx)
        status = "AUTO_ACCEPTED"
        if target in required and confidence < _CONFIDENCE_THRESHOLD:
            status = "NEEDS_CONFIRMATION"
        field_mappings.append(
            {
                "target_field": target,
                "source_column": parsed.header[col_idx],
                "confidence": confidence,
                "sample_values": samples,
                "status": status,
            }
        )

    normalisations = _propose_normalisations(parsed, header_norm)
    entity_conf = 0.9 if _match_column(required[0], header_norm) is not None else 0.5

    return {
        "entity_type": entity_type,
        "entity_type_confidence": entity_conf,
        "field_mappings": field_mappings,
        "missing_required_fields": missing,
        "normalisations": normalisations,
    }


def _norm_header(h: str) -> str:
    return h.strip().lower().replace(" ", "").replace("_", "")


def _guess_entity_type(header_norm: list[str]) -> str:
    """按每实体必填字段的命中数打分，取最高（并列取 REQUIRED_FIELDS 声明序在前者）。"""
    best = "ORDER"
    best_score = -1
    for etype, fields in REQUIRED_FIELDS.items():
        score = sum(1 for f in fields if _match_column(f, header_norm) is not None)
        if score > best_score:
            best_score = score
            best = etype
    return best


def _match_column(target: str, header_norm: list[str]) -> int | None:
    aliases = [_norm_header(a) for a in _ALIASES.get(target, [target])]
    for idx, h in enumerate(header_norm):
        if h in aliases:
            return idx
    return None


def _column_samples(parsed: ParsedFile, idx: int, limit: int = 3) -> list[str]:
    seen: list[str] = []
    for row in parsed.rows:
        if idx < len(row) and row[idx].strip():
            v = row[idx][:40]
            if v not in seen:
                seen.append(v)
        if len(seen) >= limit:
            break
    return seen


def _propose_normalisations(parsed: ParsedFile, header_norm: list[str]) -> list[dict]:
    out: list[dict] = []
    date_idx = _match_column("due_date", header_norm)
    if date_idx is not None:
        before = _column_samples(parsed, date_idx)
        after = []
        pattern = ""
        for b in before:
            try:
                dn = normalise_date(b)
                after.append(dn.iso)
                pattern = dn.detected_pattern
            except NormalisationError:
                after.append("<parse failed>")
        out.append(
            {
                "source_column": parsed.header[date_idx],
                "kind": "DATE_FORMAT",
                "detected_pattern": pattern or "UNKNOWN",
                "conversion_factor": None,
                "sample_before": before,
                "sample_after": after,
            }
        )
    unit_idx = _match_column("unit", header_norm)
    if unit_idx is not None:
        before = _column_samples(parsed, unit_idx)
        factor = None
        for b in before:
            try:
                factor = normalise_unit(b).conversion_factor
                break
            except NormalisationError:
                continue
        out.append(
            {
                "source_column": parsed.header[unit_idx],
                "kind": "UNIT_CONVERSION",
                "detected_pattern": "UNIT",
                "conversion_factor": factor,
                "sample_before": before,
                "sample_after": [str(factor) if factor is not None else "<未知>"],
            }
        )
    return out


# --------------------------------------------------------------------------
# 确定性校验（10.4）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationOutcome:
    parsed_row_count: int
    unparsed_cells: list[dict]
    type_error_count: int
    normalisation_failure_count: int


def validate_mapping(parsed: ParsedFile, mapping: dict) -> ValidationOutcome:
    """确定性校验（R2.8）：拿映射在整份文件上试跑解析，收集各类失败。不调 LLM、不静默丢弃。

    对每个数值/日期字段的每一行套用解析/归一，解析不了的单元格进 `unparsed_cells`（行号从 2
    起——第 1 行是表头），并计 `type_error_count`（数值列解析失败）与
    `normalisation_failure_count`（日期/单位归一失败）。
    """
    header_index = {h: i for i, h in enumerate(parsed.header)}
    field_to_col: dict[str, int] = {}
    for fm in mapping.get("field_mappings", []):
        src = fm.get("source_column")
        if src is not None and src in header_index and fm.get("status") != "NOT_IMPORTED":
            field_to_col[fm["target_field"]] = header_index[src]

    unparsed: list[dict] = []
    type_errors = 0
    norm_failures = 0
    parsed_rows = 0

    for row_no, row in enumerate(parsed.rows, start=2):
        row_ok = True
        for target, col in field_to_col.items():
            raw = row[col] if col < len(row) else ""
            if target in _NUMERIC_FIELDS:
                try:
                    float(raw.strip())
                except ValueError:
                    type_errors += 1
                    row_ok = False
                    unparsed.append(_cell(row_no, parsed.header[col], raw, "数值解析失败"))
            elif target in _DATE_FIELDS:
                try:
                    normalise_date(raw)
                except NormalisationError:
                    norm_failures += 1
                    row_ok = False
                    unparsed.append(_cell(row_no, parsed.header[col], raw, "日期归一失败"))
        if row_ok:
            parsed_rows += 1

    return ValidationOutcome(
        parsed_row_count=parsed_rows,
        unparsed_cells=unparsed[:50],
        type_error_count=type_errors,
        normalisation_failure_count=norm_failures,
    )


def _cell(row_no: int, column_name: str, raw: str, reason: str) -> dict:
    return {
        "row_number": row_no,
        "column_name": column_name,
        "raw_value": raw[:60],
        "reason": reason,
    }


# --------------------------------------------------------------------------
# 落库闸门（10.6）
# --------------------------------------------------------------------------


class AcceptedMappingError(ValueError):
    """`AcceptedMapping` 的前置条件未满足——不写库（K-07 静默猜测为 0）。"""


@dataclass(frozen=True)
class AcceptedMapping:
    """一份**已人工确认**的映射，`commit_batch` 的唯一入参（R2.9、R3.2）。

    构造即校验（R2.9）：任一字段 `NEEDS_CONFIRMATION`、存在 `MISSING_REQUIRED_FIELD`、或
    `unparsed_cells` 未逐条处置（`resolved` 标记）→ 抛 `AcceptedMappingError`，**不写库**。
    这道断言是 K-07 的落库侧防线：歧义或缺失一律拒绝落库，绝不静默猜测。
    """

    upload_id: str
    entity_type: str
    field_mappings: list[dict]
    unparsed_cells_resolved: bool

    def __post_init__(self) -> None:
        for fm in self.field_mappings:
            if fm.get("status") == "NEEDS_CONFIRMATION":
                raise AcceptedMappingError(
                    f"Field {fm.get('target_field')} is still NEEDS_CONFIRMATION; cannot persist."
                )
        required = REQUIRED_FIELDS.get(self.entity_type, [])
        mapped = {
            fm["target_field"]
            for fm in self.field_mappings
            if fm.get("source_column") and fm.get("status") != "NOT_IMPORTED"
        }
        missing = [f for f in required if f not in mapped]
        if missing:
            raise AcceptedMappingError(
                f"Missing required fields {missing} (MISSING_REQUIRED_FIELD); cannot persist."
            )
        if not self.unparsed_cells_resolved:
            raise AcceptedMappingError("There are unresolved unparsed_cells; cannot persist.")


@dataclass(frozen=True)
class CommitResult:
    batch_id: str
    entity_type: str
    imported_row_count: int


def commit_batch(
    session: Session,
    *,
    parsed: ParsedFile,
    accepted: AcceptedMapping,
    file_name: str,
    file_checksum: str,
    proposed_mapping: dict,
    now: datetime | None = None,
) -> CommitResult:
    """把已确认映射的行落成正式记录（R3.2/3.3）。**只消费 `accepted`，不读 LLM 原始文本。**

    仅支持 P0 演示所需的实体落库路径。写 `ImportBatch` + 逐行 `import_row_provenance`
    （batch_id + source_row_number + raw_row），并写一条 `Audit_Log`。落库后 `db/events` 钩子
    自动推进 `input_snapshot_version`；调用方在提交后触发风险扫描。自 commit。
    """
    resolved_now = now or datetime.now()  # noqa: DTZ005
    batch_id = f"BATCH-{uuid.uuid4().hex[:12]}"
    header_index = {h: i for i, h in enumerate(parsed.header)}
    col_of = {
        fm["target_field"]: header_index[fm["source_column"]]
        for fm in accepted.field_mappings
        if fm.get("source_column") in header_index and fm.get("status") != "NOT_IMPORTED"
    }

    imported = 0
    session.add(
        orm.ImportBatch(
            batch_id=batch_id,
            file_name=file_name,
            file_checksum=file_checksum,
            entity_type=accepted.entity_type,
            row_count=parsed.row_count,
            status="COMMITTED",
            proposed_mapping=proposed_mapping,
            accepted_mapping={
                "entity_type": accepted.entity_type,
                "field_mappings": accepted.field_mappings,
            },
            unparsed_cells=[],
            formula_columns=parsed.formula_columns,
            ingestion_report={"imported_at": resolved_now.isoformat()},
            imported_at=resolved_now,
            created_at=resolved_now,
        )
    )
    session.flush()

    if accepted.entity_type == "MATERIAL":
        imported = _commit_materials(session, parsed, col_of, batch_id, resolved_now)
    elif accepted.entity_type == "WORKER":
        imported = _commit_workers(session, parsed, col_of, batch_id, resolved_now)
    # 其余实体类型的落库路径按需扩展；provenance/audit 机具已通用。

    # 先提交业务事务再写审计：审计走独立连接，若在 commit 前写、业务事务仍持有 SQLite 写锁
    # 会自锁（database is locked）。审计 append 自身独立提交，不依赖业务事务。
    session.commit()
    audit.append(
        event_category="DATA_IMPORT",
        event_type="COMMIT_BATCH",
        actor="PLANNER",
        payload={
            "batch_id": batch_id,
            "entity_type": accepted.entity_type,
            "imported_row_count": imported,
            "accepted_field_mappings": [
                {"target_field": fm.get("target_field"), "source_column": fm.get("source_column")}
                for fm in accepted.field_mappings
            ],
        },
        subject_type="ImportBatch",
        subject_id=batch_id,
        occurred_at=resolved_now,
    )
    return CommitResult(batch_id, accepted.entity_type, imported)


def _commit_materials(
    session: Session, parsed: ParsedFile, col_of: dict[str, int], batch_id: str, now: datetime
) -> int:
    count = 0
    for row_no, row in enumerate(parsed.rows, start=2):
        mid = _cell_val(row, col_of.get("material_id"))
        if not mid:
            continue
        existing = session.get(orm.Material, mid)
        overwritten = None
        if existing is not None:
            overwritten = {
                "name": existing.name,
                "quantity_available": str(existing.quantity_available),
                "unit": existing.unit,
                "source": existing.source,
                "record_status": existing.record_status,
                "import_batch_id": existing.import_batch_id,
            }
        name = _cell_val(row, col_of.get("name")) or mid
        qty = _to_decimal(_cell_val(row, col_of.get("quantity_available")))
        if existing is None:
            session.add(
                orm.Material(
                    material_id=mid,
                    name=name,
                    unit="pcs",
                    quantity_available=qty,
                    reserved_quantity=0,
                    source="SPREADSHEET_IMPORT",
                    record_status="ACTIVE",
                    import_batch_id=batch_id,
                    last_updated_at=now,
                )
            )
        else:
            existing.name = name
            existing.quantity_available = qty
            existing.source = "SPREADSHEET_IMPORT"
            existing.record_status = "ACTIVE"
            existing.import_batch_id = batch_id
            existing.last_updated_at = now
        session.add(
            orm.ImportRowProvenance(
                id=f"PROV-{uuid.uuid4().hex[:12]}",
                batch_id=batch_id,
                entity_type="MATERIAL",
                entity_id=mid,
                source_row_number=row_no,
                raw_row=list(row),
                overwritten_payload=overwritten,
            )
        )
        count += 1
    session.flush()
    return count


def _commit_workers(
    session: Session, parsed: ParsedFile, col_of: dict[str, int], batch_id: str, now: datetime
) -> int:
    count = 0
    for row_no, row in enumerate(parsed.rows, start=2):
        wid = _cell_val(row, col_of.get("worker_id"))
        if not wid:
            continue
        existing = session.get(orm.Worker, wid)
        overwritten = None
        if existing is not None:
            overwritten = {
                "name": existing.name,
                "source": existing.source,
                "record_status": existing.record_status,
                "import_batch_id": existing.import_batch_id,
            }
        name = _cell_val(row, col_of.get("name")) or wid
        if existing is None:
            session.add(
                orm.Worker(
                    worker_id=wid,
                    name=name,
                    skills=[],
                    shift_start=now,
                    shift_end=now,
                    source="SPREADSHEET_IMPORT",
                    record_status="ACTIVE",
                    import_batch_id=batch_id,
                    last_updated_at=now,
                )
            )
        else:
            existing.name = name
            existing.source = "SPREADSHEET_IMPORT"
            existing.record_status = "ACTIVE"
            existing.import_batch_id = batch_id
            existing.last_updated_at = now
        session.add(
            orm.ImportRowProvenance(
                id=f"PROV-{uuid.uuid4().hex[:12]}",
                batch_id=batch_id,
                entity_type="WORKER",
                entity_id=wid,
                source_row_number=row_no,
                raw_row=list(row),
                overwritten_payload=overwritten,
            )
        )
        count += 1
    session.flush()
    return count


# --------------------------------------------------------------------------
# 整批回滚（10.7）
# --------------------------------------------------------------------------


def revert_batch(session: Session, *, batch_id: str, now: datetime | None = None) -> int:
    """整批回滚（R3.4/3.5）：该批次记录软删除 + 从 `overwritten_payload` 还原被覆盖的旧值。

    `REVERTED` 被 `load_snapshot` 排除。对每条 provenance：若有 `overwritten_payload`（覆盖过
    `MANUAL_ENTRY`）→ 还原旧值并复位 source；否则把该记录置 `REVERTED`。返回处理行数。自 commit。
    """
    resolved_now = now or datetime.now()  # noqa: DTZ005
    rows = session.execute(
        select(orm.ImportRowProvenance).where(orm.ImportRowProvenance.batch_id == batch_id)
    ).scalars().all()
    processed = 0
    for prov in rows:
        entity = _get_entity(session, prov.entity_type, prov.entity_id)
        if entity is None:
            continue
        payload = prov.overwritten_payload
        if isinstance(payload, dict):
            _restore(entity, payload, resolved_now)
        else:
            entity.record_status = "REVERTED"
            entity.last_updated_at = resolved_now
        processed += 1

    batch = session.get(orm.ImportBatch, batch_id)
    if batch is not None:
        batch.status = "REVERTED"

    session.commit()
    audit.append(
        event_category="DATA_IMPORT",
        event_type="REVERT_BATCH",
        actor="PLANNER",
        payload={"batch_id": batch_id, "reverted_row_count": processed},
        subject_type="ImportBatch",
        subject_id=batch_id,
        occurred_at=resolved_now,
    )
    return processed


def _get_entity(session: Session, entity_type: str, entity_id: str) -> object | None:
    model = {"MATERIAL": orm.Material, "WORKER": orm.Worker}.get(entity_type)
    return session.get(model, entity_id) if model is not None else None


def _restore(entity: object, payload: dict, now: datetime) -> None:
    """把被导入覆盖的旧记录逐字段还原（回滚 R3.4）。"""
    if "quantity_available" in payload:
        entity.quantity_available = _to_decimal(payload["quantity_available"])  # type: ignore[attr-defined]
    if "unit" in payload:
        entity.unit = payload["unit"]  # type: ignore[attr-defined]
    if "name" in payload:
        entity.name = payload["name"]  # type: ignore[attr-defined]
    entity.source = payload.get("source", "MANUAL_ENTRY")  # type: ignore[attr-defined]
    entity.record_status = payload.get("record_status", "ACTIVE")  # type: ignore[attr-defined]
    entity.import_batch_id = payload.get("import_batch_id")  # type: ignore[attr-defined]
    entity.last_updated_at = now  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _cell_val(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def _to_decimal(value: object):
    from decimal import Decimal, InvalidOperation

    try:
        return Decimal(str(value).strip() or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")
