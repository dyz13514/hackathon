"""编排层（确定性）。

`Orchestrator` 是确定性路由器，按意图把请求分派到两种执行形态之一
（design.md Architecture §2、ADR-002）：

- 形态 A 确定性流水线：初始计划生成，零 LLM 编排 + 末端 1 次解释调用。
- 形态 B ReAct 循环：路线可变的路径（P0 只接线重排与列映射两条）。

模块与落地任务：`routing.py` / `orchestrator.py`（5.7）、`context_manager.py`（5.5）、
`handoff.py`（5.6）、`budget.py`（5.4）、`pipelines/plan_generation.py`（2.12）、
`pipelines/replan_deterministic.py`（7.x）。
"""
