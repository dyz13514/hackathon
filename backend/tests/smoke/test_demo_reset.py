"""`POST /api/demo/reset` 冒烟（R28.8 / R27.11，tasks.md 1.6，**非可选**）。

承接原属性 39（design.md「已裁剪的 32 条属性与其替代覆盖」表第 39 行）：

> 一键重置幂等且不清空审计 | 28.8, 27.11 | `POST /demo/reset` 冒烟测试
> （连续两次重置结果相同 + `Audit_Log` 行数不减 + 新增 `DEMO_RESET`）

四组断言，各守一个不同的失效模式：

1. **结果幂等**——连续两次重置后业务表逐行逐列相同。一处 `datetime.now()` 或一个 UUID
   主键就会让这条红掉，而那样的 seed 同时会让排产结果随运行时刻漂移（R5.7）。这条断言
   是那个更大性质在演示数据层面的哨兵。
2. **审计只增不减**——重置**不清空** `audit_log`，且每次都补一条 `DEMO_RESET`。
   R24.3 的 append-only 语义在「把整个库换掉」这个动作上最容易被顺手破坏：清空业务表的
   循环少一个 `continue`，历史就没了。
3. **`input_snapshots` 从 1 开始**——不是「行被删了」而是「自增水位被复位」。这两者在
   SQLite 上不是同一件事（`loader._restart_snapshot_sequence` 的 docstring）。
4. **写端点受保护**——未认证的重置请求得到 401。这个端点会把整个库换掉，未认证可调用
   等于任何人都能在演示进行中把台上的数据清空。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from app.db.models import (
    AuditLog,
    Base,
    IncomingDelivery,
    InputSnapshot,
    Machine,
    Order,
    Product,
)
from app.main import create_app
from app.seed.dataset import (
    BOTTLENECK_MACHINE_ID,
    DEMO_ANCHOR,
    SEED_SOURCE,
    ZERO_SLACK_ORDER_ID,
)
from app.seed.loader import PRESERVED_TABLES
from app.settings import Settings

RESET = "/api/demo/reset"
LOGIN = "/api/auth/login"

#: 幂等比对时整表跳过的表。只有 `audit_log`——它按设计每次重置都多一行。
_VOLATILE_TABLES = PRESERVED_TABLES

#: 幂等比对时跳过的**列**。
#:
#: `input_snapshots.created_at` 是「这次重置发生在何时」的真实记录，由推进钩子写入
#: （`db/events.py`），它当然会变。跳过它而不是跳过整张表，是因为同一行里的
#: `snapshot_version` / `trigger` / `fingerprint` 三列恰恰必须相同——尤其 `fingerprint`：
#: 它是 11 张规划相关表的内容摘要，两次重置得到同一个摘要本身就是一条独立的幂等证明。
_VOLATILE_COLUMNS: dict[str, frozenset[str]] = {"input_snapshots": frozenset({"created_at"})}


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "demo-reset.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    return Settings()  # type: ignore[call-arg]  # 值由环境变量提供


@pytest.fixture
def client(app_settings: Settings) -> Iterator[TestClient]:
    """已建表、已登录的客户端。

    `create_app()` 顺带把审计引擎指向同一个库（`main.create_app` 的注释），因此
    `DEMO_RESET` 记录落在这里而不是某个按 `get_settings()` 懒建的别处。
    """
    application = create_app(app_settings)
    Base.metadata.create_all(application.state.engine)
    with TestClient(application) as test_client:
        test_client.post(
            LOGIN, json={"password": app_settings.session_shared_password.get_secret_value()}
        )
        yield test_client


def _engine(client: TestClient) -> Engine:
    engine: Engine = client.app.state.engine  # type: ignore[attr-defined]
    return engine


def _snapshot_all_tables(engine: Engine) -> dict[str, list[tuple[Any, ...]]]:
    """把每张业务表读成有序元组列表，用于逐行逐列比对。

    行按主键排序、列按 schema 顺序：不排序的比对会因 SQLite 的返回顺序而偶发失败，
    那种红会被当成 flaky 处理掉，于是这条断言实际上就不存在了。

    `_VOLATILE_COLUMNS` 里的列**从投影里去掉**，而不是读出来再忽略：`select(table)` 取
    全部列，`input_snapshots.created_at` 于是会进元组，而它是墙上时钟——两次重置必然不
    同。逐列筛选让「跳过哪些列」只有一处定义，也让比对失败一定意味着数据本身不确定。
    """
    contents: dict[str, list[tuple[Any, ...]]] = {}
    with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name in _VOLATILE_TABLES:
                continue
            volatile = _VOLATILE_COLUMNS.get(table.name, frozenset())
            columns = [column for column in table.columns if column.name not in volatile]
            statement = select(*columns).order_by(*table.primary_key.columns)
            contents[table.name] = [tuple(row) for row in connection.execute(statement)]
    return contents


def _audit_count(engine: Engine, *, category: str | None = None) -> int:
    statement = select(func.count()).select_from(AuditLog)
    if category is not None:
        statement = statement.where(AuditLog.event_category == category)
    with engine.connect() as connection:
        return int(connection.execute(statement).scalar_one())


# --------------------------------------------------------------------------
# 1. 结果幂等
# --------------------------------------------------------------------------


def test_two_consecutive_resets_produce_identical_business_data(client: TestClient) -> None:
    """连续两次重置后，除 `audit_log` 之外的每张表逐行逐列相同（原属性 39 前半）。"""
    engine = _engine(client)

    assert client.post(RESET).status_code == 200
    first = _snapshot_all_tables(engine)

    assert client.post(RESET).status_code == 200
    second = _snapshot_all_tables(engine)

    assert first.keys() == second.keys()
    differing = [name for name in first if first[name] != second[name]]
    assert not differing, (
        f"两次重置后内容不同的表：{differing}。"
        "演示数据里出现了依赖时钟或随机数的取值（见 app/seed/dataset.py 的确定性要求）"
    )


def test_reset_response_is_identical_across_runs_except_the_audit_id(
    client: TestClient,
) -> None:
    """两次响应体除 `audit_id` 之外逐字段相同。

    `audit_id` 每次不同是正确的——它指向一条新的审计记录。其余字段（含
    `input_snapshot_version`）相同，才说明重置真的回到了同一个状态。
    """
    first = client.post(RESET).json()
    second = client.post(RESET).json()

    assert first["audit_id"] != second["audit_id"]
    del first["audit_id"], second["audit_id"]
    assert first == second


def test_reset_recovers_from_arbitrary_local_edits(client: TestClient) -> None:
    """在数据被改动之后重置，也回到同一个状态。

    这是「便于演示重跑」（R28.8）的实质：演示中途改了订单、停了机器、删了一条到货，
    一键之后台上还是开场那一屏。只测「连续两次重置」不够——那条路径上库本来就已经是
    seed 状态。

    ## 为什么删的是到货而不是机器

    改动要同时覆盖 UPDATE 与 DELETE 两条路径，因此需要真删掉一行。演示数据里的每台
    机器都被 `changeover_rules` 引用（`CO-006`–`CO-010` 逐台给了机器默认换型），而
    `PRAGMA foreign_keys = ON`（`db/session.py`）下删父行会被库直接拒绝——那是正确的
    外键行为，不是重置的缺陷。`incoming_deliveries` 没有任何表引用它，删它得到的是
    「一行没了」这个待恢复状态本身，而不是一次外键异常。
    """
    engine = _engine(client)
    client.post(RESET)
    baseline = _snapshot_all_tables(engine)

    factory = client.app.state.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        order = session.get(Order, ZERO_SLACK_ORDER_ID)
        assert order is not None
        order.quantity = order.quantity + 999

        machine = session.get(Machine, "CNC-03")
        assert machine is not None
        machine.status = "DOWN"

        delivery = session.get(IncomingDelivery, "DLV-002")
        assert delivery is not None
        session.delete(delivery)

        session.commit()

    assert _snapshot_all_tables(engine) != baseline, "改动没生效，这个用例什么也没测"

    client.post(RESET)
    assert _snapshot_all_tables(engine) == baseline


# --------------------------------------------------------------------------
# 2. 审计只增不减
# --------------------------------------------------------------------------


def test_audit_log_never_shrinks_and_gains_one_demo_reset_per_call(
    client: TestClient,
) -> None:
    """`audit_log` 行数不减，且每次重置恰好新增一条 `DEMO_RESET`（原属性 39 后半）。"""
    engine = _engine(client)

    before = _audit_count(engine)
    client.post(RESET)
    after_first = _audit_count(engine)
    client.post(RESET)
    after_second = _audit_count(engine)

    assert before <= after_first <= after_second, "审计行数减少了——append-only 被破坏"
    assert _audit_count(engine, category="DEMO_RESET") == 2
    assert after_first - before == 1
    assert after_second - after_first == 1


def test_earlier_audit_entries_survive_the_reset(client: TestClient) -> None:
    """重置**之前**写下的审计条目在重置之后仍然在。

    上一条测的是计数，这一条测的是身份：一个「先清空再补一条」的实现能让计数看起来对
    （每次 +1），却把历史抹掉了。
    """
    engine = _engine(client)
    client.post(RESET)

    with engine.connect() as connection:
        first_id = connection.execute(
            select(AuditLog.audit_id).order_by(AuditLog.occurred_at, AuditLog.audit_id).limit(1)
        ).scalar_one()

    client.post(RESET)

    with engine.connect() as connection:
        still_there = connection.execute(
            select(AuditLog.audit_id).where(AuditLog.audit_id == first_id)
        ).first()
    assert still_there is not None, f"重置抹掉了既有审计条目 {first_id}"


def test_the_demo_reset_entry_records_what_was_replayed(client: TestClient) -> None:
    """`DEMO_RESET` 记录的载荷说明了「铺了什么」与「保住了什么」。"""
    engine = _engine(client)
    audit_id = client.post(RESET).json()["audit_id"]

    with engine.connect() as connection:
        row = connection.execute(
            select(AuditLog).where(AuditLog.audit_id == audit_id)
        ).one()

    assert row.event_category == "DEMO_RESET"
    assert row.event_type == "DEMO_RESET"
    assert row.actor == "PLANNER"
    payload = row.payload
    assert isinstance(payload, dict)
    assert payload["input_snapshot_version"] == 1
    assert payload["preserved_tables"] == ["audit_log"]
    assert payload["row_counts"]["orders"] == 14
    assert payload["anchor"] == DEMO_ANCHOR.isoformat()


# --------------------------------------------------------------------------
# 3. input_snapshots 从 1 开始
# --------------------------------------------------------------------------


def test_input_snapshots_restart_from_one_after_every_reset(client: TestClient) -> None:
    """每次重置后 `input_snapshots` 恰好一行，版本号为 1。

    删行不等于复位水位：SQLite 的 `AUTOINCREMENT` 把水位记在 `sqlite_sequence` 里。
    第二次重置若拿到 2，说明 `_restart_snapshot_sequence()` 没生效——这条断言就是它
    唯一的探测器。
    """
    engine = _engine(client)

    for attempt in (1, 2, 3):
        body = client.post(RESET).json()
        assert body["input_snapshot_version"] == 1, f"第 {attempt} 次重置后版本号不是 1"
        with engine.connect() as connection:
            rows = list(connection.execute(select(InputSnapshot).order_by(
                InputSnapshot.snapshot_version
            )))
        assert len(rows) == 1, f"第 {attempt} 次重置后快照行数为 {len(rows)}"
        assert rows[0].snapshot_version == 1
        assert rows[0].trigger == "SEED", "快照的 trigger 应为 SEED 而不是默认的 MANUAL_EDIT"


def test_seed_advances_the_version_exactly_once(client: TestClient) -> None:
    """一次重置只产生一个变更事件。

    清空阶段走 Core `DELETE`，不进 ORM 的 unit-of-work，因此不触发版本推进钩子；
    seed 的多次 flush 又被钩子折叠成同一行。两者任一失效都会让这里看到 ≥2 行。
    """
    engine = _engine(client)
    client.post(RESET)
    with engine.connect() as connection:
        count = connection.execute(select(func.count()).select_from(InputSnapshot)).scalar_one()
    assert count == 1


# --------------------------------------------------------------------------
# 4. 端点契约与认证
# --------------------------------------------------------------------------


def test_reset_requires_a_session(app_settings: Settings) -> None:
    """未认证的重置请求得到 401，且不在豁免集合里。

    这个端点会把整个库换掉。`UNAUTHENTICATED_WRITE_PATHS` 只有登录与登出两条，
    重置**不能**被加进去——这条断言让那种改动无法悄悄通过。
    """
    from app.api.deps import UNAUTHENTICATED_WRITE_PATHS

    application = create_app(app_settings)
    Base.metadata.create_all(application.state.engine)

    with TestClient(application) as anonymous:
        response = anonymous.post(RESET)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert RESET not in UNAUTHENTICATED_WRITE_PATHS


def test_reset_response_fields_are_closed_and_report_the_row_counts(
    client: TestClient,
) -> None:
    """响应字段集合封闭，且行数与 R28.1 的规模一致。"""
    body = client.post(RESET).json()

    assert set(body) == {
        "status",
        "input_snapshot_version",
        "anchor",
        "row_counts",
        "preserved_tables",
        "audit_id",
    }
    assert body["status"] == "OK"
    assert body["preserved_tables"] == ["audit_log"]
    assert body["row_counts"]["products"] == 6
    assert body["row_counts"]["orders"] == 14
    assert body["row_counts"]["materials"] == 10
    assert body["row_counts"]["machines"] == 5
    assert body["row_counts"]["workers"] == 8


def test_reset_writes_seed_data_with_the_seed_source(client: TestClient) -> None:
    """全部落库行的 `source` 是 `SEED_DATA`（R27.11）。

    演示数据与规划员导入的数据必须可区分。若 seed 写成 `MANUAL_ENTRY`，导入批次回滚
    （R3.4–5）会把演示数据一起当成手工录入去还原。
    """
    engine = _engine(client)
    client.post(RESET)

    with engine.connect() as connection:
        sources = set(connection.execute(select(Product.source)).scalars())
        sources |= set(connection.execute(select(Order.source)).scalars())
        sources |= set(connection.execute(select(Machine.source)).scalars())
    assert sources == {SEED_SOURCE}


def test_reset_leaves_the_bottleneck_machine_in_place(client: TestClient) -> None:
    """瓶颈机在重置后仍然是那台不可替代的 `CNC-01`（R28.2）。

    这是一条端到端的贯通检查：数据集里的演示前提确实经 loader 落到了库里，而不是只在
    `test_seed_dataset.py` 的纯数据断言里成立。
    """
    engine = _engine(client)
    client.post(RESET)

    with engine.connect() as connection:
        rows = {
            machine_id: capabilities
            for machine_id, capabilities in connection.execute(
                select(Machine.machine_id, Machine.capabilities)
            )
        }

    assert "DEEP_DRILLING" in rows[BOTTLENECK_MACHINE_ID]
    others = [
        machine_id
        for machine_id, capabilities in rows.items()
        if machine_id != BOTTLENECK_MACHINE_ID and "DEEP_DRILLING" in capabilities
    ]
    assert not others, f"{others} 也具备 DEEP_DRILLING，瓶颈变得可替代"
