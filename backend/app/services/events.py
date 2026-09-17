"""进程内领域事件接缝（任务 3.1 的最小实现，任务 8.5 消费）。

`Approval_Service.approve()` 在成功激活一个计划后发 `PlanActivated`（design.md §4.1 的
`self.events.emit(PlanActivated(plan_id))`）。R14.1 要求「计划被激活时触发一次风险扫描」，
而那个触发器是任务 8.5 的 `Risk_Scanner` 的订阅方。本模块提供两者之间的接缝：**现在只发，
将来才有人订阅**。

## 为什么是一个显式的发布/订阅对象，而不是直接调用风险扫描

approve() 属应用服务层，风险扫描属另一个组件；让 approve() 直接 import 并调用扫描器会把
「审批」和「风险扫描」焊死，两者的事务边界、失败处理、是否降级都会纠缠在一起。design.md
把它写成一次 `emit`，正是要保留「激活」与「激活后的副作用」之间的松耦合——8.5 落地时只需
`subscribe(PlanActivated, run_risk_scan)`，approve() 一行不改。

## 语义：发布是同步的、尽力而为的、不参与审批事务

- **同步**：`emit` 在当前调用栈里依次调用订阅者。演示是单进程，不引入队列或线程。
- **尽力而为**：订阅者抛异常不回滚已经提交的激活——计划已经 `ACTIVE` 是既成事实，风险
  扫描失败不该把它撤销（那会让「审批成功了吗」变得不可回答）。异常被吞掉并记进标准日志，
  由 8.5 决定如何补偿。因此 `emit` **必须在业务事务提交之后**调用（与审计写入同一纪律）。
- **不参与审批事务**：本模块不持有会话、不写库。它只是把「发生了什么」告诉登记过的订阅者。

## P0 没有订阅者

P0 运行期 `_subscribers[PlanActivated]` 为空，`emit` 是一次空循环。这不是缺陷：它让审批
闭环（任务 3.x）在风险扫描（任务 8.5）落地之前就完整可测——approve() 发的事件此刻没人接，
但「approve() 在成功时确实发了一个 PlanActivated」这条行为可以被订阅一个测试探针来断言，
正是本任务的单元测试所做的。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DomainEvent:
    """领域事件基类。`frozen=True`：事件是已发生事实的记录，不可变。"""


@dataclass(frozen=True)
class PlanActivated(DomainEvent):
    """一个计划刚刚被 `Approval_Service.approve()` 置为 `ACTIVE`（R11.1）。

    载荷只有 `plan_id`：订阅者（任务 8.5 的风险扫描触发器）拿它去读最新的 `ACTIVE` 计划
    并扫描。事件不携带计划全文——那会在事件里复制一份随时可能过期的状态，而订阅者本就该
    从库里读权威的当前值。
    """

    plan_id: str


#: 类型别名：一个订阅者吃一个事件，无返回值。
Subscriber = Callable[[DomainEvent], None]


class EventBus:
    """最小进程内事件总线。按事件类型登记订阅者，`emit` 同步依次调用。

    不是单例：`Approval_Service` 在构造时接收一个 `EventBus` 实例（依赖注入），测试因此
    能塞一个装了探针的总线，生产装配处（任务 3.2 / 应用启动）持有一个共享实例。
    """

    def __init__(self) -> None:
        self._subscribers: dict[type[DomainEvent], list[Subscriber]] = {}

    def subscribe(self, event_type: type[DomainEvent], handler: Subscriber) -> None:
        """登记一个订阅者。同一类型可有多个，按登记顺序被调用。"""
        self._subscribers.setdefault(event_type, []).append(handler)

    def emit(self, event: DomainEvent) -> None:
        """把事件同步派发给该类型的全部订阅者。

        **尽力而为**：某个订阅者抛异常不阻断其余订阅者，也不向上传播——调用 `emit` 的
        approve() 此刻已经提交了激活，事件的副作用失败不该回滚一个已成事实的激活。异常
        记进日志由订阅方的任务（8.5）去补偿。因此 `emit` 只应在业务事务提交后调用。
        """
        for handler in self._subscribers.get(type(event), ()):
            try:
                handler(event)
            except Exception:  # noqa: BLE001  尽力而为：见 docstring
                logger.exception(
                    "事件订阅者处理 %s 时抛异常，已忽略（激活已提交，不回滚）",
                    type(event).__name__,
                )
