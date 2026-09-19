"""风险扫描的三类触发器接线（任务 8.5，R14.1、ADR-013）。

R14.1 要求风险扫描由**三类触发器**驱动（不是常驻轮询，ADR-013）：

1. **计划置 ACTIVE 后** —— 订阅 `EventBus` 的 `PlanActivated`（任务 3.1 的接缝）。
2. **Order / Material / Machine 变更后** —— 数据变更后触发。任务 1.3 的
   `db/events.py` 只在 DB 侧推进 `input_snapshots` 版本号，**没有** Python 级的发布/订阅
   接缝，且给它加一个引擎/会话级监听器会在**任意业务事务**（含 flush、commit 前）里触发扫描，
   带来递归（扫描自身的写）与跨事务副作用的高风险。因此本触发器实现为一个**显式的、事务提交
   之后调用**的入口 `trigger_scan(...)`——由数据变更流程（导入落库 10.6、扰动 7.x）在自己的
   事务 `commit()` 之后调用，与 `PlanActivated` 的「提交后 emit」同一纪律。这既满足 R14.1
   「数据变更后触发」，又不把风险扫描焊进每一次写。
3. **手动 / 每日定时** —— `POST /api/risks/scan`（已在 `api/risks.py`）或 `APScheduler`
   每日一次（`start_daily_scan_scheduler`）。

## 幂等与尽力而为

所有触发器最终都调 `services.risk_scan.scan_and_persist`——它按 `finding_key` 去重
（R14.9），因此无论触发多频繁都不产生重复行。触发器**尽力而为**：扫描失败只记日志，不向上
传播，也不回滚触发它的业务动作（激活/数据变更已是既成事实）。这与 `EventBus.emit` 的语义
一致。

## 为什么用独立会话

每次触发用 `session_factory()` 开一个**新会话**跑扫描，绝不复用触发方的会话——`scan_and_persist`
自提交，混用会话会把它的提交与触发方的事务纠缠在一起。独立会话让扫描是一次干净的读→写闭环。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session, sessionmaker

from app.services.events import DomainEvent, EventBus, PlanActivated
from app.services.risk_scan import scan_and_persist

logger = logging.getLogger(__name__)

__all__ = [
    "register_plan_activated_trigger",
    "run_daily_scan",
    "start_daily_scan_scheduler",
    "trigger_scan",
]


def trigger_scan(
    session_factory: sessionmaker[Session],
    *,
    trigger: str,
) -> int:
    """在一个**新会话**里跑一次扫描，返回本次发现数。尽力而为：异常只记日志、不外抛。

    这是三类触发器共用的执行体。数据变更流程（导入 / 扰动）在其事务提交后调用
    `trigger_scan(factory, trigger="DATA_CHANGE")` 即完成「数据变更后触发」这一条。
    """
    try:
        with session_factory() as session:
            result = scan_and_persist(session, trigger=trigger)
        return result.finding_count
    except Exception:  # noqa: BLE001 — 尽力而为：触发方动作已成事实，扫描失败不回滚它
        logger.exception("风险扫描触发失败（trigger=%s），已忽略", trigger)
        return 0


def register_plan_activated_trigger(
    event_bus: EventBus,
    session_factory: sessionmaker[Session],
) -> None:
    """把「计划激活后扫描」订阅到事件总线（R14.1 第 1 类触发器）。

    `Approval_Service.approve()` 在成功激活后 `emit(PlanActivated(plan_id))`（提交之后、
    尽力而为）。本订阅者收到即在新会话里扫一次。`emit` 已吞掉订阅者异常，这里再包一层
    `trigger_scan` 的尽力而为是双保险。
    """

    def _on_plan_activated(event: DomainEvent) -> None:
        if isinstance(event, PlanActivated):
            trigger_scan(session_factory, trigger="PLAN_ACTIVATED")

    event_bus.subscribe(PlanActivated, _on_plan_activated)


def run_daily_scan(session_factory: sessionmaker[Session]) -> None:
    """APScheduler 每日任务体：跑一次扫描（R14.1 第 3 类触发器的定时那半）。"""
    trigger_scan(session_factory, trigger="DAILY_SCHEDULED")


def start_daily_scan_scheduler(session_factory: sessionmaker[Session]) -> object:
    """启动一个同进程 `APScheduler`，每日跑一次风险扫描（design.md Architecture §4）。

    只在演示部署（`app_env == "DEMO"`）由 `create_app` 调用——本地/CI/测试不启动真实调度器，
    避免后台线程干扰测试（那些环境用手动端点触发即可）。返回 scheduler 供调用方持有与关停。
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        run_daily_scan,
        trigger=CronTrigger(hour=0, minute=0),  # 每日 00:00 一次
        args=[session_factory],
        id="daily_risk_scan",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("每日风险扫描调度器已启动（daily_risk_scan @ 00:00）")
    return scheduler
