"""`python -m app.seed --demo` —— `make dev` 的第三步（R27.7、R28.8）。

## 默认不覆盖已有数据

`make dev` 每次启动都会跑这一行。若 `--demo` 无条件重置，那么「改了几张订单 → 重启看
效果」就会把手改的数据静默清掉。因此默认语义是**确保存在**：库里已有 `SEED_DATA` 来源
的产品就什么也不做，要强制重铺得显式加 `--force`。

演示当天的一键重置走的是 `POST /api/demo/reset`（带认证、写审计），不是这个 CLI。

## 退出码

- `0`——数据已就位（本次铺的，或本来就有）。
- `1`——配置缺失或非法（`ConfigurationError`）。「缺失即拒绝启动」（R27.2）在 CLI 上
  同样成立，不提供「用默认值凑合」的分支。
- `2`——表不存在。这几乎总是「忘了 `alembic upgrade head`」，因此单独一个退出码与一句
  明确的提示，而不是把 SQLAlchemy 的 `OperationalError` 原样抛给运维。
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy.exc import OperationalError

from app.db.audit import set_audit_engine
from app.db.session import create_db_engine, create_session_factory
from app.logging_config import configure_logging, log_event
from app.seed.dataset import DEMO_ANCHOR
from app.seed.loader import demo_data_present, reset_demo_data
from app.settings import ConfigurationError, get_settings

logger = logging.getLogger("app.seed")

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_SCHEMA_MISSING = 2


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.seed",
        description="铺演示数据集（R28）。默认不覆盖库里已有的 seed 数据。",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="确保演示数据集存在。已存在则不做任何写入。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即便已存在也清空业务表并重铺（audit_log 不清空）。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。返回退出码而不是 `sys.exit()`，便于测试直接调用。"""
    args = _parse_args(argv)
    if not args.demo and not args.force:
        # 什么都没要求时不猜意图：一个会写库的命令不该有隐式默认动作。
        _parse_args(["--help"])
        return EXIT_OK

    try:
        settings = get_settings()
    except ConfigurationError as error:
        # 不经 logger：配置错误可能发生在日志装配之前，而这条消息必须能被看到。
        print(error, file=sys.stderr)
        return EXIT_CONFIG_ERROR

    configure_logging(settings)

    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    # 审计走独立引擎（`db/audit.py`）。这里显式指向本进程的配置，避免它去自己
    # 懒建一个——CLI 与应用可能用不同的 DATABASE_URL（例如临时库）。
    set_audit_engine(create_db_engine(settings))

    try:
        with factory() as session:
            already_present = demo_data_present(session)
    except OperationalError:
        print(
            "业务表不存在。先执行 `alembic upgrade head`，再运行本命令。",
            file=sys.stderr,
        )
        return EXIT_SCHEMA_MISSING

    if already_present and not args.force:
        log_event(
            logger,
            "SEED_SKIPPED",
            message="库中已有演示数据，未做写入（加 --force 可强制重铺）",
            already_present=True,
        )
        return EXIT_OK

    report = reset_demo_data(factory, actor="SYSTEM", anchor=DEMO_ANCHOR)
    log_event(
        logger,
        "SEED_APPLIED",
        message="演示数据集已就位",
        forced=bool(args.force),
        anchor=report.anchor.isoformat(),
        input_snapshot_version=report.input_snapshot_version,
        row_counts=dict(report.row_counts),
        audit_id=report.audit_id,
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
