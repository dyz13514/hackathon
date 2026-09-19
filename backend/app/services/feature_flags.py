"""特性开关的持久化读写（任务 7.6，R13.8）。

## 为什么开关落在 `settings` 键值表而不是 `Settings`（env）

`app/settings.py` 的 `Settings` 是**启动期**配置的唯一来源（env → Pydantic 校验），进程内
不可变。而 `auto_apply_minor_enabled` 是**运行期可切**的特性开关（`PATCH /api/settings/flags`，
R13.8）：规划员在演示中打开或关闭它，不应重启进程。因此它落在 `settings` 键值表
（`db/models.py` 的 `Setting`），与「权重、模式」等其它运行期开关同处一表——建表注释写的正是
「特性开关、权重、模式。键值表，避免为每个开关跑一次迁移」。

## 这一层守的边界

`FeatureFlags`（`app.core.autonomy`）是内核值对象，不 import `sqlalchemy`——它只承载判定
`decide_autonomy` 需要的布尔。本模块是**服务层**：把 `settings` 表的行读成一个 `FeatureFlags`
喂给内核，或把一次变更写回并留审计。核心的 `decide_autonomy` 因此仍是纯函数，运行期开关的
存取被隔离在这里，两者不耦合。

## P0 默认值 = False（R13.8）

`settings` 表里没有对应行时，`read_feature_flags` 返回 `FeatureFlags()`——其默认
`auto_apply_minor_enabled = False`。也就是说：从不写这张表，行为就是 P0 默认的「全部走 L3
提案 / L5 上报」。只有一次显式 `PATCH` 才会创建该行并可能翻成 `True`（L4 属 P1）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.autonomy import FeatureFlags
from app.db import audit
from app.db import models as orm

__all__ = [
    "AUTO_APPLY_MINOR_ENABLED_KEY",
    "audit_flag_change",
    "read_feature_flags",
    "set_auto_apply_minor_enabled",
]

#: `settings.key` 里承载 `auto_apply_minor_enabled` 的键名。逐字对齐 `FeatureFlags` 字段名，
#: 使「表里的键」与「内核字段」一一对应，读写两侧不会各写一个拼法。
AUTO_APPLY_MINOR_ENABLED_KEY = "auto_apply_minor_enabled"


def _read_bool(session: Session, key: str, *, default: bool) -> bool:
    """读 `settings[key]` 为布尔；缺行或值非布尔时回退 `default`。

    `Setting.value` 是 JSON 列，历史上可能存成 `true` / `"true"` / `1`——这里只认真正的
    `bool`，其余一律回退默认值。宁可回退到「安全的默认」，也不把一个可疑的存储值当真。
    """
    row = session.get(orm.Setting, key)
    if row is None:
        return default
    value = row.value
    return value if isinstance(value, bool) else default


def read_feature_flags(session: Session) -> FeatureFlags:
    """从 `settings` 表读出当前特性开关。缺行即 P0 默认（`auto_apply_minor_enabled=False`）。

    返回内核的 `FeatureFlags` 值对象，可直接喂给 `decide_autonomy`。读端点与重排流水线都
    经此取值，因此「当前开关是什么」在整个后端只有一个答案。
    """
    return FeatureFlags(
        auto_apply_minor_enabled=_read_bool(
            session, AUTO_APPLY_MINOR_ENABLED_KEY, default=False
        )
    )


def set_auto_apply_minor_enabled(
    session: Session,
    *,
    enabled: bool,
    now: datetime,
) -> FeatureFlags:
    """把 `auto_apply_minor_enabled` 写入 `settings` 表，返回更新后的开关集。**只写、不审计**。

    **不提交、不审计**——调用方（API 层）持有事务：先 `commit()` 释放业务写锁，**再**调用
    `audit_flag_change()` 补审计。这一分工是为了绕开 SQLite 的单写者约束（见 `db/audit.py`
    模块 docstring「一个已知的 SQLite 约束」）：`audit.append()` 走独立引擎的独立连接，若在
    本函数 `flush` 之后（业务事务仍持写锁）立即调用，第二个写者会撞锁到 `busy_timeout`。因此
    审计必须在业务事务结束之后进行，与 `run_replan` 的「先 commit 后 append」同一纪律。

    返回值从写入值直接构造，不回读（省一次查询，语义直白）。
    """
    row = session.get(orm.Setting, AUTO_APPLY_MINOR_ENABLED_KEY)
    if row is None:
        session.add(
            orm.Setting(
                key=AUTO_APPLY_MINOR_ENABLED_KEY,
                value=enabled,
                updated_at=now,
            )
        )
    else:
        row.value = enabled
        row.updated_at = now
    session.flush()
    return FeatureFlags(auto_apply_minor_enabled=enabled)


def audit_flag_change(*, enabled: bool, actor: str, now: datetime) -> None:
    """为一次 `auto_apply_minor_enabled` 变更补一条审计（R13.8 的开关变更可追溯）。

    **必须在业务事务 commit 之后调用**（见 `set_auto_apply_minor_enabled` 的说明）。类别复用
    `WEIGHT_CHANGE`——开关与权重同属「运行期配置被人工改动」这一维；`event_type` 用
    `FLAG_CHANGE` 区分具体事件。审计走独立引擎立即提交，与业务事务解耦。
    """
    audit.append(
        event_category="WEIGHT_CHANGE",
        event_type="FLAG_CHANGE",
        actor=actor,
        payload={"flag": AUTO_APPLY_MINOR_ENABLED_KEY, "enabled": enabled},
        subject_type="Setting",
        subject_id=AUTO_APPLY_MINOR_ENABLED_KEY,
        occurred_at=now,
    )
