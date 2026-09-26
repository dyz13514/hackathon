"""摄取落库闸门与整批回滚（任务 10.6/10.7，R2.9/R3.2/R3.3/R3.4/R3.5）。

**非可选**（承接原属性 35、36）：

- **10.6 落库闸门（属性 35）**：低置信 / 缺必填 / 未处置 `unparsed_cells` 三类各一例，
  断言 `AcceptedMapping` 构造即拒绝，生产表行数不变（K-07 静默猜测为 0）。
- **10.7 回滚往返（属性 36）**：导入 → 回滚 → 逐字段比对，含 `MANUAL_ENTRY` 原值还原分支。

走真实 SQLite（内存表结构），确定性、不触 LLM。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as orm
from app.db.models import Base
from app.db.session import create_db_engine, create_session_factory
from app.services.ingestion import (
    AcceptedMapping,
    AcceptedMappingError,
    ImportDataError,
    commit_batch,
    revert_batch,
)
from app.services.spreadsheet import ParsedFile
from app.settings import Settings

NOW = datetime(2026, 3, 2, 8, 0)


@pytest.fixture
def factory(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> sessionmaker[Session]:
    db_file = tmp_path / "ingest.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    settings = Settings()  # type: ignore[call-arg]
    engine = create_db_engine(settings)
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _material_count(factory: sessionmaker[Session]) -> int:
    with factory() as db:
        return int(db.execute(select(func.count()).select_from(orm.Material)).scalar_one())


def _accepted(**over) -> dict:
    base = {
        "target_field": "material_id",
        "source_column": "material_id",
        "confidence": 0.95,
        "sample_values": [],
        "status": "AUTO_ACCEPTED",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# 10.6 落库闸门：三类拒绝 → 不写库（属性 35）
# --------------------------------------------------------------------------


def test_accepted_mapping_rejects_needs_confirmation() -> None:
    with pytest.raises(AcceptedMappingError):
        AcceptedMapping(
            upload_id="U",
            entity_type="MATERIAL",
            field_mappings=[
                _accepted(target_field="material_id"),
                _accepted(target_field="name", source_column="name", status="NEEDS_CONFIRMATION"),
                _accepted(target_field="quantity_available", source_column="qty"),
            ],
            unparsed_cells_resolved=True,
        )


def test_accepted_mapping_rejects_missing_required() -> None:
    with pytest.raises(AcceptedMappingError):
        AcceptedMapping(
            upload_id="U",
            entity_type="MATERIAL",
            field_mappings=[_accepted(target_field="material_id")],  # 缺 name / quantity_available
            unparsed_cells_resolved=True,
        )


def test_accepted_mapping_rejects_unresolved_unparsed_cells() -> None:
    with pytest.raises(AcceptedMappingError):
        AcceptedMapping(
            upload_id="U",
            entity_type="MATERIAL",
            field_mappings=[
                _accepted(target_field="material_id"),
                _accepted(target_field="name", source_column="name"),
                _accepted(target_field="quantity_available", source_column="qty"),
            ],
            unparsed_cells_resolved=False,  # 未处置
        )


def test_rejected_mapping_does_not_write_any_row(factory: sessionmaker[Session]) -> None:
    """三类拒绝都在构造期抛出，commit_batch 从不被调用 → 生产表行数为 0（K-07）。"""
    assert _material_count(factory) == 0
    with pytest.raises(AcceptedMappingError):
        AcceptedMapping(
            upload_id="U",
            entity_type="MATERIAL",
            field_mappings=[_accepted(target_field="material_id", status="NEEDS_CONFIRMATION")],
            unparsed_cells_resolved=True,
        )
    assert _material_count(factory) == 0


# --------------------------------------------------------------------------
# 10.7 回滚往返（属性 36）：新增导入 + 覆盖 MANUAL_ENTRY，回滚后逐字段还原
# --------------------------------------------------------------------------


def _parsed_materials() -> ParsedFile:
    return ParsedFile(
        header=["material_id", "name", "quantity_available"],
        rows=[
            ["MAT-NEW", "新物料", "100"],
            ["MAT-MANUAL", "导入覆盖名", "500"],  # 覆盖既有 MANUAL_ENTRY
        ],
    )


def _accepted_materials() -> AcceptedMapping:
    return AcceptedMapping(
        upload_id="U",
        entity_type="MATERIAL",
        field_mappings=[
            _accepted(target_field="material_id"),
            _accepted(target_field="name", source_column="name"),
            _accepted(target_field="quantity_available", source_column="quantity_available"),
        ],
        unparsed_cells_resolved=True,
    )


def test_material_import_preserves_explicit_unit(factory: sessionmaker[Session]) -> None:
    accepted = AcceptedMapping(
        upload_id="U-MAT-UNIT", entity_type="MATERIAL",
        field_mappings=[
            *_accepted_materials().field_mappings,
            _accepted(target_field="unit", source_column="unit"),
        ],
        unparsed_cells_resolved=True,
    )
    parsed = ParsedFile(
        header=["material_id", "name", "quantity_available", "unit"],
        rows=[["MAT-KG", "Steel", "25", "kg"]],
    )
    with factory() as db:
        result = commit_batch(
            db, parsed=parsed, accepted=accepted, file_name="material.csv",
            file_checksum="material-kg", proposed_mapping={}, now=NOW,
        )
    assert result.imported_row_count == 1
    with factory() as db:
        material = db.get(orm.Material, "MAT-KG")
        assert material is not None
        assert material.unit == "kg"
        assert material.quantity_available == Decimal("25")


def test_commit_then_revert_round_trip(factory: sessionmaker[Session]) -> None:
    # 预置一条 MANUAL_ENTRY 物料，导入会覆盖它。
    with factory() as db:
        db.add(
            orm.Material(
                material_id="MAT-MANUAL",
                name="手工原名",
                unit="pcs",
                quantity_available=Decimal("42"),
                reserved_quantity=Decimal("0"),
                source="MANUAL_ENTRY",
                record_status="ACTIVE",
                last_updated_at=NOW,
            )
        )
        db.commit()

    # 导入：新增 MAT-NEW + 覆盖 MAT-MANUAL。
    with factory() as db:
        result = commit_batch(
            db,
            parsed=_parsed_materials(),
            accepted=_accepted_materials(),
            file_name="mats.csv",
            file_checksum="abc",
            proposed_mapping={},
            now=NOW,
        )
    batch_id = result.batch_id
    assert result.imported_row_count == 2

    with factory() as db:
        new_mat = db.get(orm.Material, "MAT-NEW")
        overwritten = db.get(orm.Material, "MAT-MANUAL")
        assert new_mat is not None and new_mat.source == "SPREADSHEET_IMPORT"
        assert overwritten is not None
        assert overwritten.name == "导入覆盖名"  # 被导入覆盖
        assert overwritten.quantity_available == Decimal("500")
        assert overwritten.source == "SPREADSHEET_IMPORT"

    # 回滚：新增记录软删除、被覆盖的 MANUAL_ENTRY 还原原值。
    with factory() as db:
        reverted = revert_batch(db, batch_id=batch_id, now=NOW)
    assert reverted == 2

    with factory() as db:
        new_mat = db.get(orm.Material, "MAT-NEW")
        restored = db.get(orm.Material, "MAT-MANUAL")
        # 新增记录：软删除（REVERTED），被 load_snapshot 排除。
        assert new_mat is not None and new_mat.record_status == "REVERTED"
        # 被覆盖的 MANUAL_ENTRY：逐字段还原到导入前。
        assert restored is not None
        assert restored.record_status == "ACTIVE"
        assert restored.name == "手工原名"
        assert restored.quantity_available == Decimal("42")
        assert restored.source == "MANUAL_ENTRY"


def _accepted_orders() -> AcceptedMapping:
    return AcceptedMapping(
        upload_id="U-ORDER",
        entity_type="ORDER",
        field_mappings=[
            _accepted(target_field=field, source_column=field)
            for field in ("order_id", "product_id", "quantity", "due_date", "priority", "unit")
        ],
        unparsed_cells_resolved=True,
    )


def test_order_import_persists_schedulable_rows_and_reverts(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        db.add(
            orm.Product(
                product_id="PRD-1", name="Part", description=None, source="MANUAL_ENTRY",
                record_status="ACTIVE", last_updated_at=NOW,
            )
        )
        db.commit()

    parsed = ParsedFile(
        header=["order_id", "product_id", "quantity", "due_date", "priority", "unit"],
        rows=[[" ORD-1 ", " PRD-1 ", "2", "2026-03-05", "HIGH", "箱"]],
    )
    with factory() as db:
        result = commit_batch(
            db, parsed=parsed, accepted=_accepted_orders(), file_name="orders.csv",
            file_checksum="orders-1", proposed_mapping={}, now=NOW,
        )
    assert result.imported_row_count == 1
    with factory() as db:
        order = db.get(orm.Order, "ORD-1")
        assert order is not None
        assert order.quantity == Decimal("24")
        assert order.due_date == datetime(2026, 3, 5, 23, 59, 59)
        assert order.source == "SPREADSHEET_IMPORT"
        assert db.get(orm.ImportBatch, result.batch_id).status == "COMMITTED"
        assert revert_batch(db, batch_id=result.batch_id, now=NOW) == 1
    with factory() as db:
        assert db.get(orm.Order, "ORD-1").record_status == "REVERTED"


def test_order_import_rejects_missing_product_without_a_fake_batch(
    factory: sessionmaker[Session],
) -> None:
    parsed = ParsedFile(
        header=["order_id", "product_id", "quantity", "due_date", "priority", "unit"],
        rows=[["ORD-1", "PRD-MISSING", "2", "2026-03-05", "HIGH", "pcs"]],
    )
    with factory() as db, pytest.raises(ImportDataError, match="not configured"):
        commit_batch(
            db, parsed=parsed, accepted=_accepted_orders(), file_name="orders.csv",
            file_checksum="orders-2", proposed_mapping={}, now=NOW,
        )
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(orm.ImportBatch)) == 0
        assert db.scalar(select(func.count()).select_from(orm.Order)) == 0


def test_worker_import_does_not_claim_schedulable_workers_without_shifts(
    factory: sessionmaker[Session],
) -> None:
    accepted = AcceptedMapping(
        upload_id="U-WORKER", entity_type="WORKER",
        field_mappings=[
            _accepted(target_field="worker_id", source_column="worker_id"),
            _accepted(target_field="name", source_column="name"),
        ],
        unparsed_cells_resolved=True,
    )
    parsed = ParsedFile(header=["worker_id", "name"], rows=[["W-1", "Ada"]])
    with factory() as db, pytest.raises(ImportDataError, match="complete scheduling fields"):
        commit_batch(
            db, parsed=parsed, accepted=accepted, file_name="workers.csv",
            file_checksum="workers-1", proposed_mapping={}, now=NOW,
        )
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(orm.ImportBatch)) == 0
        assert db.scalar(select(func.count()).select_from(orm.Worker)) == 0
