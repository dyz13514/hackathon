"""`input_snapshot_version` 版本推进钩子（任务 1.3，R12.1 / R12.3）。

一句话职责：**规划相关数据只要变过，版本号就必须往前走一格，且这件事和那次变更同生共死。**
`Approval_Service` 的陈旧提案检测把 `plan.input_snapshot_version` 和
`current_input_snapshot_version()` 做一次相等比较就得出结论（design.md Data Models §6）。
整条链上唯一的正确性前提是：版本号推进与数据变更在同一个事务里——变更回滚了版本号也必须
跟着回滚，否则会出现「数据没变但看起来变了」的假陈旧；变更提交了版本号却没提交，则会出现
「数据变了但提案看起来新鲜」的假新鲜，那是静默激活过期计划的路径。

## 为什么是 `before_flush` + `after_flush` 两个监听器

任务描述说的是「`after_flush` 监听」，落地时必须拆成两半，因为两件事需要的时机相反：

- **「哪些表变了」只能在 `before_flush` 问。** flush 一旦执行完，ORM 会把属性历史
  （`AttributeState.history`）提交掉，`session.is_modified()` 就不再报告任何改动。在
  `after_flush` 里判断「这个 dirty 对象真的有净变化吗」会得到假阴性。
- **fingerprint 只能在 `after_flush` 算。** 它是「变更之后的行摘要」，必须看到已经写进
  事务的新值。在 `before_flush` 算出来的是变更前的状态。

所以 `before_flush` 只做一件事：把改动过的规划相关表名记到 `session.info`。`after_flush`
拿着那份记录决定是否推进版本号，并在同一个连接、同一个事务里用 Core `INSERT` 落行。

## 为什么用 Core `INSERT` 而不是 `session.add(InputSnapshot(...))`

`after_flush` 里 `session.add()` 添加的对象**不属于本次 flush**，SQLAlchemy 会把它留到
下一次 flush；如果调用方 `commit()` 之后不再 flush，那行就永远不会落库。Core `INSERT`
在当前连接上立即发出 SQL，与业务变更共享同一个事务边界——这正是「同一事务内插入一行」
所要求的语义。它同时天然避免了递归：Core 语句不进 unit-of-work，不会再触发一次 flush。

## 每个事务恰好一行

一个事务里可能有多次 flush（seed 就是典型：分批 flush 若干实体）。若每次 flush 都插一行，
版本号会在一次逻辑变更中跳好几格。R12 关心的是「提案生成之后数据是否变过」，一次事务就是
一次变更事件，因此本模块在事务内**只插一行**，后续 flush 只 `UPDATE` 那一行的 fingerprint，
让它始终等于事务结束时的状态。版本号单调性由 `sqlite_autoincrement=True` 保证
（见 `models.py` 的 `InputSnapshot` docstring）。

## `trigger` 从 ContextVar 取

`trigger` 是业务语义（`IMPORT_COMMIT` / `SEED` / `REVERT` / ...），而 flush 钩子处在纯技术
层，看不到调用意图。写入方用 `snapshot_trigger("IMPORT_COMMIT")` 包住自己的事务即可；
未声明时记为 `MANUAL_EDIT`，那是最保守的默认——它不声称任何来源，只声称「有人改了数据」。

## 刻意不实现的东西

`entity_changes_between(a, b)` 与 `entity_change_log` 已按 requirements 第 3 节拒绝清单
移出范围。审批时唯一需要回答的问题是「数据是否变过」，无论变的是哪个字段，规划员的动作
都一样——走 R12.4 的「基于最新数据重新生成」入口。因此本模块只推进一个整数，不记差异。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from sqlalchemy import event, insert, inspect, select, update
from sqlalchemy.orm import Session, UOWTransaction, sessionmaker

from app.db.models import (
    PLANNING_RELEVANT_TABLES,
    SNAPSHOT_TRIGGERS,
    Base,
    InputSnapshot,
)

#: 未经 `snapshot_trigger()` 声明时记录的 `trigger`。
DEFAULT_TRIGGER = "MANUAL_EDIT"

#: `session.info` 键：本事务内改动过的规划相关表名。
_CHANGED_TABLES_KEY = "input_snapshot_changed_tables"
#: `session.info` 键：本事务已插入的快照版本号（None 表示尚未插入）。
_PENDING_VERSION_KEY = "input_snapshot_pending_version"

#: 行摘要里的字段分隔符与行分隔符，以及 NULL 的哨兵。
#: 用控制字符而不是逗号：它们不可能出现在列值里，因此
#: `("a", "b")` 与 `("a,b",)` 不会摘要成同一串。
_FIELD_SEP = b"\x1f"
_ROW_SEP = b"\x1e"
_NULL_MARKER = b"\x00"

_current_trigger: ContextVar[str] = ContextVar("input_snapshot_trigger", default=DEFAULT_TRIGGER)


@contextmanager
def snapshot_trigger(trigger: str) -> Iterator[None]:
    """声明「接下来这段写入属于哪类变更」，供快照行的 `trigger` 列使用。

    取值必须属于 `SNAPSHOT_TRIGGERS`。非法取值在这里就抛 `ValueError` 而不是留给库层
    ——`trigger` 在 schema 里是 TEXT 无 CHECK（models.py 的取值域说明），这里是唯一的
    守门点。
    """
    if trigger not in SNAPSHOT_TRIGGERS:
        raise ValueError(f"未知的快照触发原因 {trigger!r}，取值须属于 {SNAPSHOT_TRIGGERS}")
    token = _current_trigger.set(trigger)
    try:
        yield
    finally:
        _current_trigger.reset(token)


def current_trigger() -> str:
    """当前生效的 `trigger`。"""
    return _current_trigger.get()


def compute_planning_fingerprint(session: Session) -> str:
    """11 张规划相关表的内容摘要（`input_snapshots.fingerprint`）。

    三条性质是它有用的前提：

    1. **确定性**——表按名字排序、列按名字排序、行按主键排序。任何一处依赖字典或集合的
       迭代顺序，同样的数据就会摘要成不同的值，fingerprint 也就失去比对能力。
    2. **看得见未提交的变更**——在 `after_flush` 内经同一个 `Session` 查询，读到的是本
       事务已 flush 但未提交的新值，正是我们要摘要的状态。
    3. **只覆盖规划相关表**——计划、审批、Trace 的写入不改变「排产输入」，不能影响它。

    值统一走 `str()` 后编码：`Decimal("2.50")` 与 `Decimal("2.5")` 数值相等而字面不同，
    会被判成不同摘要。这是可接受的偏保守方向——fingerprint 只是辅助比对手段，权威判据
    始终是版本号；宁可多报一次「变了」，不可漏报。
    """
    hasher = hashlib.sha256()
    for table_name in sorted(PLANNING_RELEVANT_TABLES):
        table = Base.metadata.tables[table_name]
        columns = sorted(table.c, key=lambda column: column.name)
        statement = select(*columns).order_by(*table.primary_key.columns)
        hasher.update(table_name.encode("utf-8"))
        hasher.update(_ROW_SEP)
        for row in session.execute(statement):
            fields = (
                _NULL_MARKER if value is None else str(value).encode("utf-8") for value in row
            )
            hasher.update(_FIELD_SEP.join(fields))
            hasher.update(_ROW_SEP)
    return hasher.hexdigest()


def _table_name_of(instance: object) -> str | None:
    """实例所映射的表名。非 ORM 对象返回 `None`。"""
    state: Any = inspect(instance, raiseerr=False)
    if state is None:
        return None
    table = state.mapper.local_table
    if table is None:
        return None
    name: str = table.name
    return name


def _changed_planning_tables(session: Session) -> set[str]:
    """本次 flush 中发生 INSERT / UPDATE / DELETE 的规划相关表。

    `session.dirty` 会把「被碰过但没有净变化」的对象也算进来（读一次属性再赋回原值就够
    了），因此 UPDATE 一侧必须过 `is_modified` 复核。不复核会让一次纯读取的请求推进版本
    号，把所有待审批提案无端判成陈旧。
    """
    changed: set[str] = set()

    for instance in list(session.new) + list(session.deleted):
        table_name = _table_name_of(instance)
        if table_name in PLANNING_RELEVANT_TABLES:
            changed.add(table_name)

    for instance in session.dirty:
        table_name = _table_name_of(instance)
        if table_name in PLANNING_RELEVANT_TABLES and session.is_modified(
            instance, include_collections=False
        ):
            changed.add(table_name)

    return changed


def _record_changed_tables(
    session: Session, _flush_context: UOWTransaction, _instances: Any
) -> None:
    """`before_flush`：把改动过的规划相关表名累积到 `session.info`。

    只记录，不落库。落库要等 `after_flush`——那时 fingerprint 才能看到新值。
    """
    changed = _changed_planning_tables(session)
    if not changed:
        return
    accumulated: set[str] = session.info.setdefault(_CHANGED_TABLES_KEY, set())
    accumulated.update(changed)


def _advance_snapshot_version(session: Session, _flush_context: UOWTransaction) -> None:
    """`after_flush`：在同一事务内推进版本号。

    本事务第一次有规划相关变更时 `INSERT` 一行；后续 flush 只 `UPDATE` fingerprint，
    使那一行始终描述事务结束时的状态（见模块 docstring「每个事务恰好一行」）。
    """
    changed: set[str] = session.info.get(_CHANGED_TABLES_KEY, set())
    if not changed:
        return
    # 已消费：同一 flush 不重复处理，且下一次 flush 从空集重新累积。
    session.info[_CHANGED_TABLES_KEY] = set()

    fingerprint = compute_planning_fingerprint(session)
    pending_version: int | None = session.info.get(_PENDING_VERSION_KEY)

    if pending_version is None:
        result = session.execute(
            insert(InputSnapshot).values(
                created_at=datetime.now(),
                trigger=current_trigger(),
                fingerprint=fingerprint,
            )
        )
        inserted = result.inserted_primary_key
        assert inserted is not None, "input_snapshots 的自增主键必须被回填"
        session.info[_PENDING_VERSION_KEY] = int(inserted[0])
        return

    session.execute(
        update(InputSnapshot)
        .where(InputSnapshot.snapshot_version == pending_version)
        .values(fingerprint=fingerprint)
    )


def _reset_transaction_state(session: Session, *_args: Any) -> None:
    """事务结束后清空暂存：下一个事务要拿到自己的那一格版本号。

    提交与回滚都要清。回滚时那行 `INSERT` 已随事务消失，若不清掉暂存的版本号，下一个
    事务会去 `UPDATE` 一个不存在的行——SQL 不报错，版本号却再也不推进，陈旧检测从此失效。
    """
    session.info.pop(_CHANGED_TABLES_KEY, None)
    session.info.pop(_PENDING_VERSION_KEY, None)


#: 标记一个 target 上「本模块的钩子已挂」的哨兵属性。见 `register_input_snapshot_hooks`
#: 的 docstring：`event.contains` 以 `id(target)` 为键，无法区分「同一个对象重复注册」与
#: 「一个已被 GC 的旧对象，其地址被新对象复用」——后者会让新工厂被误判为已注册而跳过挂钩。
_HOOKS_REGISTERED_FLAG = "_input_snapshot_hooks_registered"


def register_input_snapshot_hooks(target: sessionmaker[Session] | type[Session] = Session) -> None:
    """把四个监听器挂到会话工厂（或 `Session` 类）上。幂等。

    默认挂在 `Session` 类上，覆盖全部会话；传入 `sessionmaker` 则只覆盖该工厂产出的会话，
    测试用后者以免相互干扰。

    ## 幂等为什么不能只靠 `event.contains`

    重复注册会让同一次 flush 插入两行快照，版本号一次跳两格，因此必须幂等。但
    `event.contains` 判断的是「事件注册表里有没有一条 `(id(target), 事件名, handler)`」，
    而注册表**以 `id(target)` 为键**。`sessionmaker` 在测试里成批创建又被 GC，CPython 会把
    刚回收的地址立刻分配给下一个 `sessionmaker`；若旧工厂在 GC 前没摘钩子，注册表里那条以
    旧 `id` 为键的陈旧记录会被新工厂（同地址）撞上——`event.contains` 返回 `True`，于是
    `event.listen` 被跳过，新工厂产出的会话**一个钩子都没挂**。表现是 seed 不推进
    `input_snapshot_version`（恒为 0），随后 `production_plans.input_snapshot_version` 的
    外键指向一个不存在的快照行，整条链偶发地崩在 `FOREIGN KEY constraint failed`——且只在
    全量跑、工厂多、地址被复用时才现形。

    因此幂等判据落在**对象自身**的一个哨兵属性上，而不是全局注册表：新对象（哪怕地址被复用）
    必然没有这个属性，于是总会真正挂上钩子；同一个活对象重复调用则因属性已在而跳过。
    挂钩前先防御性 `event.remove` 一次，清掉可能残留在这个 `id` 上的陈旧记录，保证最终
    恰好挂一份。
    """
    hooks: tuple[tuple[str, Any], ...] = (
        ("before_flush", _record_changed_tables),
        ("after_flush", _advance_snapshot_version),
        ("after_commit", _reset_transaction_state),
        ("after_rollback", _reset_transaction_state),
        ("after_soft_rollback", _reset_transaction_state),
    )
    # 已在这个**活对象**上挂过：直接返回，避免同一工厂重复挂钩导致版本号一次跳两格。
    if getattr(target, _HOOKS_REGISTERED_FLAG, False):
        return
    for name, handler in hooks:
        # 先清掉可能残留在这个 id 上的陈旧注册（旧工厂被 GC、地址被复用的情形），再挂新的，
        # 保证这个 target 上每个事件恰好一条本模块的 handler。
        if event.contains(target, name, handler):
            event.remove(target, name, handler)
        event.listen(target, name, handler)
    # 极少数 target 不允许设属性（例如打了 __slots__ 的自定义 Session 子类）；退回到
    # 「已尽力挂钩」——此路径不影响正确性，只是失去了「同一活对象再调用即跳过」的加速。
    with suppress(AttributeError, TypeError):
        object.__setattr__(target, _HOOKS_REGISTERED_FLAG, True)


def unregister_input_snapshot_hooks(
    target: sessionmaker[Session] | type[Session] = Session,
) -> None:
    """摘掉监听器。供测试隔离用，生产路径不调用。"""
    hooks: tuple[tuple[str, Any], ...] = (
        ("before_flush", _record_changed_tables),
        ("after_flush", _advance_snapshot_version),
        ("after_commit", _reset_transaction_state),
        ("after_rollback", _reset_transaction_state),
        ("after_soft_rollback", _reset_transaction_state),
    )
    for name, handler in hooks:
        if event.contains(target, name, handler):
            event.remove(target, name, handler)
    # 清掉哨兵：允许同一个工厂在摘钩后再挂钩（`test_input_snapshot_events` 依赖这一点）。
    with suppress(AttributeError, TypeError):
        if getattr(target, _HOOKS_REGISTERED_FLAG, False):
            object.__delattr__(target, _HOOKS_REGISTERED_FLAG)
