"""引擎与会话工厂。SQLite 的 PRAGMA 在这里一次性设好。

四条 PRAGMA，每条都有具体理由（design.md Data Models、Architecture §4）：

- `journal_mode = WAL`——读不阻塞写。演示时前端在轮询状态看板，同时规划员在审批；
  默认的 rollback journal 会让读把写锁住。
- `busy_timeout = 5000`——写冲突时等 5 秒再报错，而不是立刻 `database is locked`。
  R12.7 的乐观并发控制处理的是「业务层面的并发修改」，SQLite 的文件锁竞争是另一层，
  两者都要处理。
- `foreign_keys = ON`——SQLite **默认不强制外键**。不开这个开关，schema 里所有
  `REFERENCES` 都只是注释。
- `synchronous = NORMAL`——WAL 下的推荐值，比 `FULL` 快得多且在进程崩溃时仍安全
  （只在操作系统崩溃时可能丢最后几个事务，演示环境可接受）。

`foreign_keys` 尤其关键：`disruptions.active_plan_id` 那条 NOT NULL 外键是环上唯一
保住的一侧（见 `models.py` docstring），不开 PRAGMA 就等于两侧都没有。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.db.events import register_input_snapshot_hooks
from app.settings import Settings, get_settings

#: 写冲突等待时长。design.md 明确要求 5000ms。
SQLITE_BUSY_TIMEOUT_MS = 5000


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """每次新建物理连接时设 PRAGMA。

    PRAGMA 是**连接级**的，不是数据库级：连接池新开一条连接就要重设一次，
    否则池扩容后的新连接会退回默认值。这是 SQLite 上一个容易漏的点。
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        # PostgreSQL 下不适用：WAL 是 SQLite 概念，外键与超时由服务端配置。
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA synchronous = NORMAL")
    finally:
        cursor.close()


def create_db_engine(settings: Settings | None = None) -> Engine:
    """按配置建引擎并挂上 PRAGMA 监听器。

    `check_same_thread=False` 只对 SQLite 有意义：FastAPI 的同步端点跑在线程池里，
    连接会在线程间流转。这不放松并发安全——真正的串行化由 uvicorn 的
    `--workers 1` 与 SQLite 自身的写锁保证（design.md Architecture §4）。
    """
    resolved = settings or get_settings()
    url = resolved.database_url

    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_engine(
        url,
        connect_args=connect_args,
        # 演示规模下 SQL 回显只会淹没日志；需要时经 LOG_LEVEL=DEBUG 单独打开。
        echo=False,
        future=True,
    )
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """会话工厂，**并挂上 `input_snapshot_version` 推进钩子**（任务 1.3 / 1.6）。

    `expire_on_commit=False`：提交后仍能读取 ORM 对象的属性。默认的 `True` 会在
    commit 后把属性置为过期，随后的读触发新查询——而审批路径在提交后还要把计划
    序列化进响应体，那时会话可能已关闭。

    ## 钩子在这里挂，而不是在 `create_app()` 里

    任务 1.3 交付了 `register_input_snapshot_hooks()`，但没有接线；未接线的钩子等于
    没有钩子——`current_input_snapshot_version()` 会恒为 0，于是 `Approval_Service`
    的陈旧检测（R12.2–3）对每个提案都得出「数据变过」，审批闭环从第一次点击起就卡死。
    接线点选在工厂而不是应用装配处，理由是覆盖面：`python -m app.seed --demo`、
    `POST /api/demo/reset`、以及将来的一切脚本都经这个工厂取会话，而它们都不经
    `create_app()`。挂在工厂上，「拿到会话就带着钩子」成为构造上的事实。

    挂在**工厂**而不是 `Session` 类上，是为了不污染测试里另建的会话
    （`register_input_snapshot_hooks()` 默认目标是 `Session` 类）。函数本身幂等，
    因此重复调用同一工厂不会让一次 flush 插两行快照。
    """
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    register_input_snapshot_hooks(factory)
    return factory


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """事务边界。异常一律回滚，不留半提交状态。

    审计写入**不**走这条通路：它需要在业务事务回滚后依然留存（「记录一次阻断」
    这件事本身不能被回滚掉），因此走 `db/audit.py` 的 `AUDIT_BYPASS` 独立连接
    （任务 1.4）。
    """
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
