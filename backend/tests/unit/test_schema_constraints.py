"""任务 1.2 要求写进 DDL 的结构性约束，逐条断言它们**真的会拒绝**违规行。

这些约束不是文档，是闸门。写在 DDL 里的意义就是「绕不过去」——服务层可以有 bug，
但库层必须挡住。因此每条约束都用一次真实 INSERT 去撞它，而不是检查 schema 元数据里
有没有那个约束名（后者只能证明约束被声明过，不能证明它生效）。

覆盖 tasks.md 1.2 明确点名的四组：
- `operations` 的 `CHECK (sequence BETWEEN 1 AND 3)` 与 `UNIQUE (product_id, sequence)`
- `ux_active_per_day` / `ux_pending_per_day` 两个部分唯一索引
- `scheduled_jobs` 的 `CHECK (end_time > start_time)` 与 `UNIQUE (plan_id, job_id)`
外加 design.md DDL 里字面写出的另外三条 CHECK，以及 SQLite 的 PRAGMA 与
`input_snapshots` 的单调性。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app.db.models import Base
from app.db.session import SQLITE_BUSY_TIMEOUT_MS, create_db_engine
from app.settings import Settings

NOW = datetime(2026, 3, 2, 8, 0)
PRODUCTION_DATE = date(2026, 3, 2)


def _dt(value: datetime) -> str:
    """把 datetime 转成 SQLAlchemy 的 SQLite DateTime 所用的字面格式。

    本模块用 `text()` 走原始 SQL，SQLAlchemy 因此不知道参数类型，会把 datetime 直接
    交给 sqlite3 的默认适配器——那个适配器在 Python 3.12 起已弃用。生产路径不受影响
    （ORM 与 Core 都知道列类型，走 SQLAlchemy 自己的 bind processor），但测试里让它
    继续报警会淹没真正需要注意的警告。

    格式与 SQLAlchemy 的 `DATETIME` 一致，因此 `CHECK (end_time > start_time)` 的字符串
    比较语义与生产写入完全相同——这一点很关键：格式不一致的话那条 CHECK 会在测试里
    以另一种方式生效，测出来的就不是生产行为。
    """
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def _d(value: date) -> str:
    """同理，对应 SQLAlchemy 的 SQLite `DATE` 字面格式。"""
    return value.isoformat()


def _settings(db_path: str) -> Settings:
    return Settings(
        database_url=f"sqlite:///{db_path}",
        session_shared_password="test-shared-password",  # noqa: S106 - 测试固定值
        session_secret_key="test-secret-key-that-is-long-enough-32",  # noqa: S106
        llm_mode="STUB",
    )


@pytest.fixture
def engine(tmp_path) -> Engine:
    """文件型 SQLite 引擎。

    刻意**不用** `:memory:`：WAL 只在文件型数据库上生效，而本模块要断言 PRAGMA
    真的被设上了。内存库会让那条断言变成假阳性。
    """
    db_file = (tmp_path / "constraints.db").as_posix()
    eng = create_db_engine(_settings(db_file))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


# --------------------------------------------------------------------------
# 最小行工厂：只填 NOT NULL 列，让每个测试的意图不被样板数据淹没
# --------------------------------------------------------------------------


def _insert_snapshot(conn, version: int = 1) -> int:
    conn.execute(
        text(
            "INSERT INTO input_snapshots (snapshot_version, created_at, trigger, fingerprint) "
            "VALUES (:v, :c, 'SEED', 'fp')"
        ),
        {"v": version, "c": _dt(NOW)},
    )
    return version


def _insert_product(conn, product_id: str = "PROD-01") -> str:
    conn.execute(
        text(
            "INSERT INTO products (product_id, name, source, record_status, last_updated_at) "
            "VALUES (:p, 'Widget', 'SEED_DATA', 'ACTIVE', :t)"
        ),
        {"p": product_id, "t": _dt(NOW)},
    )
    return product_id


def _insert_operation(conn, *, operation_id: str, product_id: str, sequence: int) -> None:
    conn.execute(
        text(
            "INSERT INTO operations (operation_id, product_id, sequence, required_machine_type, "
            "required_worker_skill, base_processing_time_per_unit, setup_time) "
            "VALUES (:o, :p, :s, 'CNC', 'machining', 2.5, 10)"
        ),
        {"o": operation_id, "p": product_id, "s": sequence},
    )


def _insert_plan(conn, *, plan_id: str, status: str, snapshot_version: int, origin: str) -> str:
    conn.execute(
        text(
            "INSERT INTO production_plans (plan_id, production_date, status, feasibility, "
            "plan_version, version, input_snapshot_version, origin, created_at) "
            "VALUES (:i, :d, :s, 'FEASIBLE', 1, 1, :v, :o, :t)"
        ),
        {
            "i": plan_id,
            "d": _d(PRODUCTION_DATE),
            "s": status,
            "v": snapshot_version,
            "o": origin,
            "t": _dt(NOW),
        },
    )
    return plan_id


def _insert_machine(conn, machine_id: str = "CNC-01") -> str:
    conn.execute(
        text(
            "INSERT INTO machines (machine_id, machine_type, capabilities, status, "
            "available_start, available_end, rate_multiplier, source, record_status, "
            "last_updated_at) "
            "VALUES (:m, 'CNC', '[]', 'AVAILABLE', :s, :e, 1.0, 'SEED_DATA', 'ACTIVE', :t)"
        ),
        {
            "m": machine_id,
            "s": _dt(NOW),
            "e": _dt(NOW + timedelta(hours=8)),
            "t": _dt(NOW),
        },
    )
    return machine_id


def _insert_worker(conn, worker_id: str = "W-01") -> str:
    conn.execute(
        text(
            "INSERT INTO workers (worker_id, name, skills, shift_start, shift_end, source, "
            "record_status, last_updated_at) "
            "VALUES (:w, 'Alice', '[]', :s, :e, 'SEED_DATA', 'ACTIVE', :t)"
        ),
        {
            "w": worker_id,
            "s": _dt(NOW),
            "e": _dt(NOW + timedelta(hours=8)),
            "t": _dt(NOW),
        },
    )
    return worker_id


def _insert_order(conn, order_id: str, product_id: str, quantity: float = 10) -> str:
    conn.execute(
        text(
            "INSERT INTO orders (order_id, product_id, quantity, due_date, priority, "
            "injection_suspected, source, record_status, last_updated_at) "
            "VALUES (:o, :p, :q, :d, 'NORMAL', 0, 'SEED_DATA', 'ACTIVE', :t)"
        ),
        {
            "o": order_id,
            "p": product_id,
            "q": quantity,
            "d": _dt(NOW + timedelta(days=3)),
            "t": _dt(NOW),
        },
    )
    return order_id


def _insert_job(conn, *, job_id: str, order_id: str, sequence: int) -> str:
    conn.execute(
        text(
            "INSERT INTO production_jobs (job_id, order_id, product_id, operation_sequence, "
            "quantity, required_machine_type, required_worker_skill) "
            "VALUES (:j, :o, 'PROD-01', :s, 10, 'CNC', 'machining')"
        ),
        {"j": job_id, "o": order_id, "s": sequence},
    )
    return job_id


def _insert_scheduled(
    conn,
    *,
    scheduled_job_id: str,
    plan_id: str,
    job_id: str,
    machine_id: str,
    worker_id: str,
    start: datetime,
    end: datetime,
) -> None:
    conn.execute(
        text(
            "INSERT INTO scheduled_jobs (scheduled_job_id, plan_id, job_id, machine_id, "
            "worker_id, start_time, end_time, setup_minutes, changeover_minutes, locked) "
            "VALUES (:i, :p, :j, :m, :w, :s, :e, 0, 0, 0)"
        ),
        {
            "i": scheduled_job_id,
            "p": plan_id,
            "j": job_id,
            "m": machine_id,
            "w": worker_id,
            "s": _dt(start),
            "e": _dt(end),
        },
    )


# --------------------------------------------------------------------------
# operations：R4.1 / R4.7
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_sequence", [0, 4, -1, 99])
def test_operation_sequence_must_be_between_1_and_3(engine: Engine, bad_sequence: int) -> None:
    """R4.1 限定 1–3 道工序。区间外的 sequence 必须被库层拒绝。"""
    with engine.begin() as conn:
        product_id = _insert_product(conn)
        with pytest.raises(IntegrityError, match="sequence_range|CHECK"):
            _insert_operation(
                conn, operation_id="OP-BAD", product_id=product_id, sequence=bad_sequence
            )


@pytest.mark.parametrize("good_sequence", [1, 2, 3])
def test_operation_sequence_accepts_the_full_valid_range(
    engine: Engine, good_sequence: int
) -> None:
    """区间内的取值必须被接受——CHECK 不能宽到无用也不能紧到误伤。"""
    with engine.begin() as conn:
        product_id = _insert_product(conn)
        _insert_operation(
            conn, operation_id=f"OP-{good_sequence}", product_id=product_id, sequence=good_sequence
        )


def test_operation_sequence_is_unique_per_product(engine: Engine) -> None:
    """R4.7：重复 sequence 在结构上不可构造。

    这条 UNIQUE 的价值在于让 `INVALID_ROUTING` 成为「写不进去的状态」，而不是
    「排产时才发现的状态」。
    """
    with engine.begin() as conn:
        product_id = _insert_product(conn)
        _insert_operation(conn, operation_id="OP-A", product_id=product_id, sequence=1)
        with pytest.raises(IntegrityError, match="uq_operations_product_sequence|UNIQUE"):
            _insert_operation(conn, operation_id="OP-B", product_id=product_id, sequence=1)


def test_same_sequence_on_different_products_is_allowed(engine: Engine) -> None:
    """UNIQUE 的作用域是 (product_id, sequence) 而非 sequence 单列。"""
    with engine.begin() as conn:
        first = _insert_product(conn, "PROD-01")
        second = _insert_product(conn, "PROD-02")
        _insert_operation(conn, operation_id="OP-A", product_id=first, sequence=1)
        _insert_operation(conn, operation_id="OP-B", product_id=second, sequence=1)


# --------------------------------------------------------------------------
# production_plans：R11.8 / R12.6 的两个部分唯一索引
# --------------------------------------------------------------------------


def test_only_one_active_plan_per_production_date(engine: Engine) -> None:
    """`ux_active_per_day`：属性 15 要求任一 production_date 上 ACTIVE 计划数恒 ≤ 1。"""
    with engine.begin() as conn:
        version = _insert_snapshot(conn)
        _insert_plan(
            conn,
            plan_id="PLAN-0001",
            status="ACTIVE",
            snapshot_version=version,
            origin="PLAN_GENERATION",
        )
        with pytest.raises(IntegrityError, match="ux_active_per_day|UNIQUE"):
            _insert_plan(
                conn,
                plan_id="PLAN-0002",
                status="ACTIVE",
                snapshot_version=version,
                origin="PLAN_GENERATION",
            )


def test_only_one_pending_approval_plan_per_production_date(engine: Engine) -> None:
    """`ux_pending_per_day`（R12.6）：同一天不能有两个待审批提案排队。"""
    with engine.begin() as conn:
        version = _insert_snapshot(conn)
        _insert_plan(
            conn,
            plan_id="PLAN-0001",
            status="PENDING_APPROVAL",
            snapshot_version=version,
            origin="PLAN_GENERATION",
        )
        with pytest.raises(IntegrityError, match="ux_pending_per_day|UNIQUE"):
            _insert_plan(
                conn,
                plan_id="PLAN-0002",
                status="PENDING_APPROVAL",
                snapshot_version=version,
                origin="PLAN_GENERATION",
            )


def test_partial_indexes_do_not_constrain_other_statuses(engine: Engine) -> None:
    """部分唯一索引只管 ACTIVE 与 PENDING_APPROVAL 两个状态。

    这条正是 `BASELINE` 计划能存在的原因：基线以 `status = DRAFT` 存为
    `production_plans` 行，同一天可以有任意多个 DRAFT / SUPERSEDED / REJECTED
    （design.md Data Models §3）。若索引不是部分的，基线计划就会和正式计划抢那个
    唯一位置。
    """
    with engine.begin() as conn:
        version = _insert_snapshot(conn)
        for index, status in enumerate(["DRAFT", "DRAFT", "SUPERSEDED", "REJECTED", "DRAFT"]):
            _insert_plan(
                conn,
                plan_id=f"PLAN-{index:04d}",
                status=status,
                snapshot_version=version,
                origin="BASELINE" if status == "DRAFT" else "PLAN_GENERATION",
            )
        count = conn.execute(text("SELECT COUNT(*) FROM production_plans")).scalar_one()
        assert count == 5


def test_active_plans_on_different_dates_are_allowed(engine: Engine) -> None:
    """索引的作用域是「每天」，不是「全局唯一 ACTIVE」。"""
    with engine.begin() as conn:
        version = _insert_snapshot(conn)
        conn.execute(
            text(
                "INSERT INTO production_plans (plan_id, production_date, status, feasibility, "
                "plan_version, version, input_snapshot_version, origin, created_at) "
                "VALUES ('PLAN-0001', :d1, 'ACTIVE', 'FEASIBLE', 1, 1, :v, "
                "'PLAN_GENERATION', :t)"
            ),
            {"d1": _d(PRODUCTION_DATE), "v": version, "t": _dt(NOW)},
        )
        conn.execute(
            text(
                "INSERT INTO production_plans (plan_id, production_date, status, feasibility, "
                "plan_version, version, input_snapshot_version, origin, created_at) "
                "VALUES ('PLAN-0002', :d2, 'ACTIVE', 'FEASIBLE', 1, 1, :v, "
                "'PLAN_GENERATION', :t)"
            ),
            {"d2": _d(PRODUCTION_DATE + timedelta(days=1)), "v": version, "t": _dt(NOW)},
        )


# --------------------------------------------------------------------------
# scheduled_jobs：时间区间与唯一性
# --------------------------------------------------------------------------


def _scheduling_fixture(conn) -> tuple[str, str, str, str]:
    version = _insert_snapshot(conn)
    plan_id = _insert_plan(
        conn,
        plan_id="PLAN-0001",
        status="DRAFT",
        snapshot_version=version,
        origin="PLAN_GENERATION",
    )
    product_id = _insert_product(conn)
    order_id = _insert_order(conn, "ORD-0001", product_id)
    job_id = _insert_job(conn, job_id="ORD-0001-OP1", order_id=order_id, sequence=1)
    machine_id = _insert_machine(conn)
    worker_id = _insert_worker(conn)
    return plan_id, job_id, machine_id, worker_id


@pytest.mark.parametrize("minutes", [0, -30])
def test_scheduled_job_end_must_be_after_start(engine: Engine, minutes: int) -> None:
    """零长与负长区间都必须被拒。

    零长同样非法：一个耗时 0 分钟的作业会让 `Timeline` 的重叠判定与
    `earliest_feasible_slot` 的候选起点扫描出现退化情形。
    """
    with engine.begin() as conn:
        plan_id, job_id, machine_id, worker_id = _scheduling_fixture(conn)
        with pytest.raises(IntegrityError, match="end_after_start|CHECK"):
            _insert_scheduled(
                conn,
                scheduled_job_id="SJ-0001",
                plan_id=plan_id,
                job_id=job_id,
                machine_id=machine_id,
                worker_id=worker_id,
                start=NOW,
                end=NOW + timedelta(minutes=minutes),
            )


def test_a_job_cannot_be_scheduled_twice_in_one_plan(engine: Engine) -> None:
    """`UNIQUE (plan_id, job_id)`：同一计划里一个作业只能出现一次。"""
    with engine.begin() as conn:
        plan_id, job_id, machine_id, worker_id = _scheduling_fixture(conn)
        _insert_scheduled(
            conn,
            scheduled_job_id="SJ-0001",
            plan_id=plan_id,
            job_id=job_id,
            machine_id=machine_id,
            worker_id=worker_id,
            start=NOW,
            end=NOW + timedelta(minutes=30),
        )
        with pytest.raises(IntegrityError, match="uq_scheduled_plan_job|UNIQUE"):
            _insert_scheduled(
                conn,
                scheduled_job_id="SJ-0002",
                plan_id=plan_id,
                job_id=job_id,
                machine_id=machine_id,
                worker_id=worker_id,
                start=NOW + timedelta(hours=2),
                end=NOW + timedelta(hours=3),
            )


def test_deleting_a_plan_cascades_to_its_scheduled_jobs(engine: Engine) -> None:
    """`ON DELETE CASCADE` 生效——这同时证明 `PRAGMA foreign_keys` 真的开着。"""
    with engine.begin() as conn:
        plan_id, job_id, machine_id, worker_id = _scheduling_fixture(conn)
        _insert_scheduled(
            conn,
            scheduled_job_id="SJ-0001",
            plan_id=plan_id,
            job_id=job_id,
            machine_id=machine_id,
            worker_id=worker_id,
            start=NOW,
            end=NOW + timedelta(minutes=30),
        )
        conn.execute(text("DELETE FROM production_plans WHERE plan_id = :p"), {"p": plan_id})
        remaining = conn.execute(text("SELECT COUNT(*) FROM scheduled_jobs")).scalar_one()
        assert remaining == 0


# --------------------------------------------------------------------------
# design.md DDL 里字面写出的其余 CHECK
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_multiplier", [0, -1.5])
def test_machine_rate_multiplier_must_be_positive(engine: Engine, bad_multiplier: float) -> None:
    """R4.5 的算式要除以 `rate_multiplier`；零会除零，负数会产生负工期。"""
    with engine.begin() as conn, pytest.raises(
        IntegrityError, match="rate_multiplier_positive|CHECK"
    ):
        conn.execute(
            text(
                "INSERT INTO machines (machine_id, machine_type, capabilities, status, "
                "available_start, available_end, rate_multiplier, source, record_status, "
                "last_updated_at) "
                "VALUES ('M-BAD', 'CNC', '[]', 'AVAILABLE', :s, :e, :r, 'SEED_DATA', "
                "'ACTIVE', :t)"
            ),
            {
                "s": _dt(NOW),
                "e": _dt(NOW + timedelta(hours=8)),
                "r": bad_multiplier,
                "t": _dt(NOW),
            },
        )


def test_order_quantity_must_be_positive(engine: Engine) -> None:
    """数量为 0 或负数的订单没有生产语义。"""
    with engine.begin() as conn:
        product_id = _insert_product(conn)
        with pytest.raises(IntegrityError, match="quantity_positive|CHECK"):
            _insert_order(conn, "ORD-BAD", product_id, quantity=0)


def test_changeover_minutes_cannot_be_negative(engine: Engine) -> None:
    """负换型时间会让 `earliest_feasible_slot` 算出比就绪时刻更早的开始时间。"""
    with engine.begin() as conn:
        machine_id = _insert_machine(conn)
        with pytest.raises(IntegrityError, match="changeover_non_negative|CHECK"):
            conn.execute(
                text(
                    "INSERT INTO changeover_rules (rule_id, machine_id, changeover_minutes, "
                    "specificity) VALUES ('CO-BAD', :m, -5, 2)"
                ),
                {"m": machine_id},
            )


def test_disruption_must_reference_an_existing_plan(engine: Engine) -> None:
    """环上保住的那一侧外键（`disruptions.active_plan_id`）必须真的强制。

    见 `models.py` docstring：`production_plans.disruption_id` 为打破外键环让出了
    库级约束，因此这一侧是唯一的库层保障，必须验证它没有跟着一起失效。
    """
    with engine.begin() as conn, pytest.raises(IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            text(
                "INSERT INTO disruptions (disruption_id, type, payload, reported_at, "
                "registered_at, source, active_plan_id) "
                "VALUES ('DIS-01', 'MACHINE_BREAKDOWN', '{}', :t, :t, 'MANUAL', "
                "'PLAN-DOES-NOT-EXIST')"
            ),
            {"t": _dt(NOW)},
        )


# --------------------------------------------------------------------------
# SQLite 运行期配置与版本号单调性
# --------------------------------------------------------------------------


def test_sqlite_pragmas_are_applied(engine: Engine) -> None:
    """WAL、busy_timeout、外键强制三者都必须在新连接上生效（tasks.md 1.2）。"""
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar_one().upper() == "WAL"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar_one() == SQLITE_BUSY_TIMEOUT_MS
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_pragmas_are_applied_to_every_pooled_connection(engine: Engine) -> None:
    """PRAGMA 是连接级的：池里每条连接都要设，不能只设第一条。"""
    for _ in range(3):
        with engine.connect() as conn:
            conn.execute(text("PRAGMA wal_checkpoint"))
            assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_snapshot_version_stays_monotonic_after_deletes(engine: Engine) -> None:
    """`sqlite_autoincrement=True` 的作用：删掉最后一行后版本号不回退。

    没有这个参数，SQLite 会复用被删行的 rowid，新快照拿到一个**用过的**版本号。
    R12.1 的陈旧检测比对的是 `MAX(snapshot_version)`，版本号回退会让一个陈旧提案
    看起来是新鲜的——这是静默的正确性故障，不是性能问题。
    """
    with engine.begin() as conn:
        for _ in range(3):
            conn.execute(
                text(
                    "INSERT INTO input_snapshots (created_at, trigger, fingerprint) "
                    "VALUES (:c, 'SEED', 'fp')"
                ),
                {"c": _dt(NOW)},
            )
        conn.execute(text("DELETE FROM input_snapshots WHERE snapshot_version = 3"))
        conn.execute(
            text(
                "INSERT INTO input_snapshots (created_at, trigger, fingerprint) "
                "VALUES (:c, 'MANUAL_EDIT', 'fp2')"
            ),
            {"c": _dt(NOW)},
        )
        latest = conn.execute(
            text("SELECT MAX(snapshot_version) FROM input_snapshots")
        ).scalar_one()
        assert latest == 4, "版本号必须继续递增，不能复用被删除的 3"
