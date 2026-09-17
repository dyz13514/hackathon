"""append-only 的 `Audit_Log` 写入通路（任务 1.4，R24.3–4）。

R24.3 只有一句话：审计日志为 append-only，且不提供修改或删除既有条目的接口。
design.md Data Models §7 把它落成**两道可执行的强制**，而不是一条文档约定：

1. **ORM 层只有 `append()`**——本模块的公开面就是这一个写函数。没有 `update()`、
   没有 `delete()`、没有返回 ORM 实例让调用方改字段的读函数。「不提供接口」这件事
   在这里是字面意义上的：想改一条审计记录，没有函数可调。
2. **语句级监听器**——`before_cursor_execute` 拦截任何以 `audit_log` 为目标的
   UPDATE / DELETE（含 SQLite 的 `REPLACE` 变体）并抛 `AuditImmutableError`。
   第 1 道防的是「顺手写个改法」，第 2 道防的是「绕过 ORM 直接发 SQL」。

刻意的减法：**没有哈希链**（`entry_hash` / `prev_hash` 与链校验端点已按 requirements
第 3 节拒绝清单移出范围）。哈希链防御的威胁模型是「有权限的操作者事后改写历史」，
演示是单 Planner 单组织，该威胁不在模型内。上面两道机制完整满足 R24.3。

## 为什么审计写入要走独立连接

「业务失败了」这件事本身必须被记录（design.md Error Handling §4）。如果审计写入参与
业务事务，那么「记录一次阻断」会和被阻断的业务一起回滚，日志里恰好缺掉最该在的那几条。
因此 `append()` 不接受 `Session`，而是在**独立引擎的独立事务**里立即提交。

同一次写入还会把 `AUDIT_BYPASS` ContextVar 置真。任务 8.1 的沙箱守卫在
`SANDBOX_ACTIVE and not AUDIT_BYPASS` 时拒绝一切 DML——没有这个旁路，
`SANDBOX_WRITE_BLOCKED` 这条记录本身也会被沙箱拦住。**8.1 必须从本模块 import
`AUDIT_BYPASS`**：在 `sandbox_guard.py` 里另建一个同名 ContextVar 会让旁路静默失效
（两个 ContextVar 是两个独立的值），而失效的表现是审计静默丢记录，最难发现的那一类。

## 一个已知的 SQLite 约束

独立连接意味着独立的写锁竞争者。SQLite（即使在 WAL 下）同一时刻只允许一个写者，
因此在业务事务**已经写过东西**（持有写锁）的时候调用 `append()`，会阻塞到
`busy_timeout = 5000ms`（`db/session.py`）。安全网够用，但调用点的习惯应当是：
审计写在业务写之前，或在业务事务结束之后。演示规模下不构成实际问题，记在这里是为了
将来出现「审计偶发变慢」时不必重新查一遍原因。
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Final
from uuid import uuid4

from sqlalchemy import Engine, event, insert

from app.db.audit_events import AUDIT_EVENT_CATEGORIES, AuditCategory
from app.db.models import AuditLog
from app.db.session import create_db_engine

__all__ = [
    # 写入面只有 append()。这个列表的长度本身就是 R24.3 的一部分。
    "AUDIT_BYPASS",
    "AUDIT_EVENT_CATEGORIES",
    "AuditCategory",
    "AuditImmutableError",
    "append",
    "audit_bypass",
    "get_audit_engine",
    "set_audit_engine",
]

#: 受保护的表名。从模型取，避免与 `models.py` 的 `__tablename__` 漂移。
AUDIT_TABLE_NAME: Final[str] = AuditLog.__tablename__

#: 审计写入进行中的标记。任务 8.1 的沙箱守卫读它来放行审计旁路——见模块 docstring。
AUDIT_BYPASS: ContextVar[bool] = ContextVar("AUDIT_BYPASS", default=False)


class AuditImmutableError(RuntimeError):
    """有人试图 UPDATE / DELETE `audit_log`。

    继承 `RuntimeError` 而非 `ValueError`：这不是「参数不对」，而是「这条路不存在」。
    它也**不是**一个应当被业务代码捕获后重试的错误——捕获它唯一正确的方式是改掉发出
    该语句的代码。
    """


# --------------------------------------------------------------------------
# 第 2 道机制：语句级 UPDATE / DELETE 拦截
# --------------------------------------------------------------------------

#: 匹配 DML 语句的动词与**目标表**。
#:
#: 只看目标表是关键的一点：`UPDATE orders SET ... WHERE id IN (SELECT subject_id FROM
#: audit_log)` 里出现了表名但目标不是审计表，那是一条合法语句。用 `"audit_log" in
#: statement` 这种朴素判断会把它误杀，而误杀会以「某个业务写入莫名失败」的形式出现。
#:
#: `REPLACE` / `INSERT OR REPLACE` 一并拦下：它在同一主键上是删除加插入，效果就是改写
#: 一条既有记录，与 UPDATE 无异。DDL（`DROP` / `ALTER`）刻意**不**拦——Alembic 的
#: downgrade 要 `drop_table('audit_log')`，拦住它会让迁移无法回退，而 R24.3 说的是
#: 「不提供修改或删除条目的接口」，不是「表不可删除」。
_MUTATION_RE: Final = re.compile(
    r"""^\s*
    (?:
        (?P<update>UPDATE)\s+(?:OR\s+\w+\s+)?            # UPDATE [OR ROLLBACK] <table>
      | (?P<delete>DELETE)\s+FROM\s+                     # DELETE FROM <table>
      | (?P<replace>(?:INSERT\s+OR\s+REPLACE|REPLACE)     # REPLACE [INTO] <table>
            (?:\s+INTO)?)\s+
    )
    (?P<target>[^\s(;]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _mutation_target(statement: str) -> tuple[str, str] | None:
    """返回 `(动词, 目标表名)`，非 UPDATE/DELETE/REPLACE 语句返回 `None`。

    表名要归一化：`main.audit_log`、`"audit_log"`、`` `audit_log` ``、`[audit_log]`
    指的都是同一张表，而这四种写法都可能出现（schema 限定来自 SQLite，各种引号来自
    不同方言的 identifier preparer）。
    """
    match = _MUTATION_RE.match(statement)
    if match is None:
        return None
    if match.group("update"):
        verb = "UPDATE"
    elif match.group("delete"):
        verb = "DELETE"
    else:
        verb = "REPLACE"
    # 先去 schema 限定，再剥引号——顺序反过来会在 `"main"."audit_log"` 上出错。
    target = match.group("target").split(".")[-1].strip("\"'`[]")
    return verb, target.lower()


@event.listens_for(Engine, "before_cursor_execute")
def _block_audit_mutation(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    """在语句到达驱动之前拦下针对 `audit_log` 的改写。

    挂在 `Engine` **类**上而不是某个实例上：审计表在任何引擎上都不可改写，包括测试里
    临时建的引擎、以及将来某个人为了「批量修数据」新建的引擎。把它绑到单个实例，
    就等于给绕过留了一个门。
    """
    parsed = _mutation_target(statement)
    if parsed is None:
        return
    verb, target = parsed
    if target != AUDIT_TABLE_NAME:
        return
    raise AuditImmutableError(
        f"{AUDIT_TABLE_NAME} 是 append-only（R24.3）：拒绝 {verb} 语句。"
        f"审计条目一旦写入即不可修改或删除；唯一的写入方式是 app.db.audit.append()。"
        f"被拒绝的语句：{statement.strip()[:200]}"
    )


# --------------------------------------------------------------------------
# 独立连接
# --------------------------------------------------------------------------

_audit_engine: Engine | None = None
#: 只保护引擎的懒创建。审计写入本身的并发由 SQLite 的写锁与 `busy_timeout` 处理。
_engine_lock = threading.Lock()


def get_audit_engine() -> Engine:
    """审计专用引擎（进程内单例，懒创建）。

    与业务引擎是**两个**引擎、两个连接池，因此审计事务与业务事务互不影响。
    """
    global _audit_engine
    if _audit_engine is None:
        with _engine_lock:
            if _audit_engine is None:
                _audit_engine = create_db_engine()
    return _audit_engine


def set_audit_engine(engine: Engine | None) -> None:
    """显式设置（或用 `None` 清空）审计引擎。

    两个用途：应用启动时复用已建好的配置；测试里指向临时库。清空后下次
    `get_audit_engine()` 会按当前配置重建。
    """
    global _audit_engine
    with _engine_lock:
        _audit_engine = engine


@contextmanager
def audit_bypass() -> Iterator[None]:
    """在块内把 `AUDIT_BYPASS` 置真，退出时**恢复原值**而不是置假。

    用 `reset(token)` 而非 `set(False)`：嵌套调用（审计写入过程中又触发一次审计写入）
    下，内层退出时若硬置假，外层剩下的语句就失去了旁路。
    """
    token = AUDIT_BYPASS.set(True)
    try:
        yield
    finally:
        AUDIT_BYPASS.reset(token)


def _new_audit_id(occurred_at: datetime) -> str:
    """可读且按时间排序的审计 ID。

    形如 `AUDIT-20260302T081500-3f9ac1b2`。时间前缀让 `SELECT audit_id` 的输出天然按
    发生顺序排列（演示时直接读日志表，这一点很实用）；随机后缀保证同一秒内多条不撞。
    审计 ID 不参与任何确定性断言，因此这里用随机不违反 R5.7。
    """
    return f"AUDIT-{occurred_at:%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"


def append(
    *,
    event_category: AuditCategory,
    event_type: str,
    actor: str,
    payload: Mapping[str, Any],
    subject_type: str | None = None,
    subject_id: str | None = None,
    trace_id: str | None = None,
    occurred_at: datetime | None = None,
    engine: Engine | None = None,
) -> str:
    """写一条审计记录并立即提交，返回 `audit_id`。

    全部参数都是关键字参数：调用点会散落在服务层、工具层与守卫层，
    `append("APPROVAL_ACTION", "APPROVE", "PLANNER", {...})` 这种位置调用在
    review 时无法判断第三个参数到底是 actor 还是 subject。

    `event_category` 必须是 `audit_events.AUDIT_EVENT_CATEGORIES` 之一（R24.4 的闭集合）；
    `event_type` 是该类别下的具体事件，取值自由但不能为空。

    `payload` 必须可 JSON 序列化——它落进 `audit_log.payload` 这个 JSON 列。

    **不参与调用方的事务**：写入走 `engine`（默认审计专用引擎）的独立事务，函数返回时
    已提交。调用方随后回滚业务事务，这条记录依然在。
    """
    if event_category not in AUDIT_EVENT_CATEGORIES:
        raise ValueError(
            f"未知的审计事件类别 {event_category!r}；"
            f"合法取值见 app.db.audit_events（新增类别须在那里显式定义）"
        )
    if not event_type.strip():
        raise ValueError("event_type 不能为空")
    if not actor.strip():
        raise ValueError("actor 不能为空（PLANNER | SYSTEM | <AGENT_NAME>）")

    # naive 本地时间，与全库时间列口径一致（models.py 模块 docstring 第 1 条）。
    moment = occurred_at if occurred_at is not None else datetime.now()  # noqa: DTZ005
    audit_id = _new_audit_id(moment)
    target_engine = engine if engine is not None else get_audit_engine()

    # 先置旗标再执行：沙箱守卫在语句级读它（任务 8.1），置晚一步就等于没置。
    with audit_bypass(), target_engine.begin() as conn:
        conn.execute(
            insert(AuditLog).values(
                audit_id=audit_id,
                event_category=event_category,
                event_type=event_type,
                actor=actor,
                subject_type=subject_type,
                subject_id=subject_id,
                payload=dict(payload),
                trace_id=trace_id,
                occurred_at=moment,
            )
        )
    return audit_id
