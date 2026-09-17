"""Alembic 迁移建出的 schema 必须与 `Base.metadata` 逐表逐列一致。

## 为什么需要这个测试

任务 1.2 的初始迁移是 `alembic revision --autogenerate` 生成的，那一刻两者当然一致。
风险在**之后**：有人给 `models.py` 加一列却忘了写迁移，于是本地 `create_all` 的测试
全绿，而 `alembic upgrade head` 建出的生产库缺那一列。这类漂移在演示当天才暴露。

这个测试把「模型与迁移一致」变成一条可执行断言：真的跑一遍 `alembic upgrade head`，
再用 `inspect()` 读回真实 schema，和 `Base.metadata` 对比。

## 为什么不用 Alembic 自己的 `compare_metadata`

`compare_metadata` 会把很多与正确性无关的差异报成 diff（SQLite 不回读 server_default
的原始文本、`Numeric` 无精度时的类型往返、部分索引谓词的规范化形式），噪声大到需要一
长串豁免规则才能用。这里只断言**结构性的三件事**——表集合、列集合与可空性、以及 DDL
里那些点名要有的约束与索引——这三件事漂移了必然出错，而不漂移就不会有假警报。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.db.models import Base

BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: 必须存在于迁移产物中的索引。部分唯一索引尤其重要：它们是 R11.8 / R12.6 的
#: 结构性强制，一旦在迁移里丢掉，库层就不再阻止「同一天两个 ACTIVE 计划」。
REQUIRED_INDEXES = {
    "production_plans": {"ux_active_per_day", "ux_pending_per_day"},
    "scheduled_jobs": {"ix_sched_plan_machine_start"},
    "import_batches": {"ix_batch_checksum"},
}

#: 必须存在的唯一约束（表 -> 列元组集合）。
REQUIRED_UNIQUE_COLUMNS = {
    "operations": {("product_id", "sequence")},
    "scheduled_jobs": {("plan_id", "job_id")},
    "unschedulable_jobs": {("plan_id", "job_id")},
    "production_jobs": {("order_id", "operation_sequence")},
    "trace_steps": {("trace_id", "step_index")},
}


@pytest.fixture(scope="module")
def migrated_engine(tmp_path_factory):
    """跑一次真实的 `alembic upgrade head`，返回指向结果库的引擎。

    用子进程而不是 `alembic.command.upgrade()`：`migrations/env.py` 会调
    `get_settings()`，而那是个 `lru_cache` 单例。在同一进程内改 `DATABASE_URL`
    再触发迁移，很容易读到别的测试缓存下来的配置。子进程有干净的环境，也顺带证明
    了「命令行上的 `alembic upgrade head` 确实能用」——那是 `make dev` 真正走的路径。
    """
    db_file = tmp_path_factory.mktemp("migrated") / "schema.db"
    env = {
        "DATABASE_URL": f"sqlite:///{db_file.as_posix()}",
        "SESSION_SHARED_PASSWORD": "test-shared-password",
        "SESSION_SECRET_KEY": "test-secret-key-that-is-long-enough-32",
        "LLM_MODE": "STUB",
        # PATH 与 SYSTEMROOT 是 Windows 上启动 Python 子进程的必需项。
        "PATH": __import__("os").environ.get("PATH", ""),
        "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
    }
    result = subprocess.run(  # noqa: S603 - 参数全部由本测试构造
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"alembic upgrade head 失败（退出码 {result.returncode}）\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    yield engine
    engine.dispose()


def test_migration_creates_every_model_table(migrated_engine) -> None:
    """表集合必须完全一致——两个方向都查。

    缺表意味着模型加了表却没写迁移；多表意味着模型删了表却没删迁移。后者同样是
    bug：留下的孤表会让 `compare_metadata` 在下一次 autogenerate 时生成一条意外的
    `drop_table`。
    """
    inspector = inspect(migrated_engine)
    actual = set(inspector.get_table_names()) - {"alembic_version"}
    expected = set(Base.metadata.tables)

    assert actual == expected, (
        f"模型里有但迁移没建：{sorted(expected - actual)}\n"
        f"迁移建了但模型里没有：{sorted(actual - expected)}"
    )


def test_all_thirty_five_tables_exist(migrated_engine) -> None:
    """design.md Data Models §2–§7 一共 35 张表，一次性建齐（tasks.md 1.2）。

    这个数字写死在断言里是刻意的：它把「表数量」变成一个需要显式修改的事实，
    使增删表成为一次有意识的决定而不是顺手的改动。
    """
    inspector = inspect(migrated_engine)
    actual = set(inspector.get_table_names()) - {"alembic_version"}
    assert len(actual) == 35, f"期望 35 张表，实际 {len(actual)} 张：{sorted(actual)}"


def test_migration_columns_match_models(migrated_engine) -> None:
    """逐表比对列名与可空性。

    可空性一起比是因为它是**语义**而非风格：`disruptions.active_plan_id` 从 NOT NULL
    退化成 nullable，会让「扰动必须指向真实计划」这条保障静默失效。
    """
    inspector = inspect(migrated_engine)
    mismatches: list[str] = []

    for table_name, table in sorted(Base.metadata.tables.items()):
        actual_columns = {col["name"]: col for col in inspector.get_columns(table_name)}
        expected_columns = {col.name: col for col in table.columns}

        missing = set(expected_columns) - set(actual_columns)
        extra = set(actual_columns) - set(expected_columns)
        if missing:
            mismatches.append(f"{table_name}: 迁移缺列 {sorted(missing)}")
        if extra:
            mismatches.append(f"{table_name}: 迁移多列 {sorted(extra)}")

        for name in sorted(set(expected_columns) & set(actual_columns)):
            expected_nullable = expected_columns[name].nullable
            actual_nullable = actual_columns[name]["nullable"]
            if expected_nullable != actual_nullable:
                mismatches.append(
                    f"{table_name}.{name}: 可空性不一致"
                    f"（模型 nullable={expected_nullable}，迁移 nullable={actual_nullable}）"
                )

    assert not mismatches, "模型与迁移漂移：\n" + "\n".join(mismatches)


def test_required_indexes_survive_the_migration(migrated_engine) -> None:
    """点名要求的索引必须真的存在于迁移产物里。"""
    inspector = inspect(migrated_engine)
    for table_name, required in sorted(REQUIRED_INDEXES.items()):
        actual = {index["name"] for index in inspector.get_indexes(table_name)}
        missing = required - actual
        assert not missing, f"{table_name} 缺索引 {sorted(missing)}；现有 {sorted(actual)}"


def test_partial_unique_indexes_are_actually_partial(migrated_engine) -> None:
    """两个部分唯一索引必须带 WHERE 谓词，且既唯一又部分。

    只检查名字不够：一个**无谓词**的 `ux_active_per_day` 会把同一天的全部计划都限成
    一行，连 DRAFT 基线都存不进去；一个**非唯一**的索引则什么都不阻止。两种退化都
    是「索引还在但语义反了」，名字检查看不出来。
    """
    from sqlalchemy import text

    with migrated_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND name IN ('ux_active_per_day', 'ux_pending_per_day')"
            )
        ).all()

    found = {name: sql for name, sql in rows}
    assert set(found) == {"ux_active_per_day", "ux_pending_per_day"}

    for name, ddl in sorted(found.items()):
        assert "UNIQUE" in ddl.upper(), f"{name} 必须是唯一索引，实际 DDL：{ddl}"
        assert "WHERE" in ddl.upper(), f"{name} 必须带 WHERE 谓词，实际 DDL：{ddl}"

    assert "ACTIVE" in found["ux_active_per_day"].upper()
    assert "PENDING_APPROVAL" in found["ux_pending_per_day"].upper()


def test_required_unique_constraints_survive_the_migration(migrated_engine) -> None:
    """点名要求的唯一约束必须存在，且作用在正确的列组合上。"""
    inspector = inspect(migrated_engine)
    for table_name, required in sorted(REQUIRED_UNIQUE_COLUMNS.items()):
        actual = {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(table_name)
        }
        missing = required - actual
        assert not missing, f"{table_name} 缺唯一约束 {sorted(missing)}；现有 {sorted(actual)}"


def test_no_entity_change_log_table(migrated_engine) -> None:
    """`entity_change_log` 已移出范围，不能悄悄回来（tasks.md 1.2）。

    这是一条**反向**断言。被拒绝的设计有一种复活倾向：某次「顺手补全审计能力」的改动
    很容易把它加回来，而它带来的是 requirements 第 3 节明确拒绝的复杂度。
    """
    inspector = inspect(migrated_engine)
    assert "entity_change_log" not in inspector.get_table_names()


def test_audit_log_has_no_hash_chain_columns(migrated_engine) -> None:
    """`audit_log` 不含 `entry_hash` / `prev_hash`（哈希链已移出范围）。

    同样是反向断言。哈希链防御的威胁模型（有权限者事后改写历史）不在演示范围内，
    R24.3 的要求由任务 1.4 的两道 append-only 机制满足。
    """
    inspector = inspect(migrated_engine)
    columns = {col["name"] for col in inspector.get_columns("audit_log")}
    forbidden = {"entry_hash", "prev_hash"} & columns
    assert not forbidden, f"audit_log 不应有哈希链列，却发现 {sorted(forbidden)}"


def test_auto_applied_changes_table_exists_with_both_snapshots(migrated_engine) -> None:
    """`auto_applied_changes` 必须在 P0 就建成，含两个 JSON 快照列。

    L4 自动应用是 P1 行为，但数据模型属 P0：这样 P1-J 落地时不需要迁移
    （tasks.md 1.2）。P0 运行期该表恒为空。
    """
    inspector = inspect(migrated_engine)
    assert "auto_applied_changes" in inspector.get_table_names()
    columns = {col["name"] for col in inspector.get_columns("auto_applied_changes")}
    assert {"snapshot_before", "snapshot_after"} <= columns


def test_no_sqlite_specific_column_types(migrated_engine) -> None:
    """PostgreSQL 兼容性（R27.4）：不使用 SQLite 专有类型。

    检查的是模型侧的类型声明而非 SQLite 回读的类型名——SQLite 的类型亲和性会把很多
    声明规范化，回读结果不足以判断原始意图。
    """
    sqlite_only = {"UNSIGNED BIG INT", "INT2", "INT8", "NATIVE CHARACTER", "CLOB"}
    offenders: list[str] = []
    for table_name, table in sorted(Base.metadata.tables.items()):
        for column in table.columns:
            type_name = type(column.type).__name__.upper()
            if type_name in sqlite_only:
                offenders.append(f"{table_name}.{column.name}: {type_name}")
    assert not offenders, f"发现 SQLite 专有类型：{offenders}"
