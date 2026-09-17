"""仓储层：对持久化状态的读访问入口。

本模块目前只有一个成员——`current_input_snapshot_version()`（任务 1.3，R12.1 / R12.3）。
这是**刻意的最小面**：`Approval_Service` 的陈旧提案检测只需要回答「提案生成之后数据是否
变过」，一次 `MAX(snapshot_version)` 就够了。原设计里的 `entity_changes_between(a, b)`
（逐字段列出变化）已按 requirements 第 3 节拒绝清单移出范围——无论变的是哪个字段，规划员
的动作都一样：走 R12.4 的「基于最新数据重新生成」入口。多提供一个查询，就多一处会被
UI 拿去渲染「变化清单」的诱惑，而那份清单没有对应的需求也没有对应的测试。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import InputSnapshot

#: 一行快照都还没有时的返回值。
#:
#: 取 0 而非抛异常：`production_plans.input_snapshot_version` 有指向
#: `input_snapshots` 的外键，任何计划所引用的版本号必 ≥ 1，因此 0 与任何计划的版本号都
#: 不相等——空库状态下的陈旧检测会得出「变过」，这是安全的方向（拒绝激活而非放行）。
NO_SNAPSHOT_VERSION = 0


def current_input_snapshot_version(session: Session) -> int:
    """当前输入数据版本号，即 `MAX(input_snapshots.snapshot_version)`。

    空表返回 `NO_SNAPSHOT_VERSION`。取 `MAX` 而不是 `COUNT`：`sqlite_autoincrement=True`
    保证版本号单调递增但**不保证连续**（删行后不复用 rowid），只有 `MAX` 与写入侧的语义
    一致（见 `models.py` 的 `InputSnapshot` docstring）。
    """
    latest = session.execute(select(func.max(InputSnapshot.snapshot_version))).scalar_one_or_none()
    return NO_SNAPSHOT_VERSION if latest is None else int(latest)


@dataclass(frozen=True)
class SnapshotRepository:
    """`current_input_snapshot_version()` 的会话绑定形态。

    design.md §4.1 的 `Approval_Service` 写作 `self.repo.current_input_snapshot_version()`
    ——注入一个仓储对象比在服务里到处传 `Session` 更容易在测试中替换。两种形态共用同一个
    实现，不存在两份 SQL。
    """

    session: Session

    def current_input_snapshot_version(self) -> int:
        """见模块级同名函数。"""
        return current_input_snapshot_version(self.session)
