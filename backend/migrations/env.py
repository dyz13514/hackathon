"""Alembic 运行环境。

连接串来自 `app.settings`，不写在 `alembic.ini` 里——「配置只有一个入口」这条
在迁移脚本上同样成立（R23.11）。

`target_metadata` 指向 `app.db.models.Base.metadata`——schema 的唯一真源。
`tests/structure/test_schema_matches_models.py` 断言迁移建出的 schema 与它逐表逐列
一致，因此「改了模型忘了写迁移」会在测试里失败，而不是在演示时失败。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db.models import Base
from app.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # 批量模式：SQLite 不支持大多数 ALTER，缺了它后续迁移会在 SQLite 上失败
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
