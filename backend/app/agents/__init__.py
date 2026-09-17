"""LLM Agent 层：三个 Agent，按输入信任级别 / 写权限 / 输出契约划分（ADR-001）。

- `Ingestion_Agent`（6 步）：最不受信任输入，白名单严格 4 个摄取/写入工具。
- `Planning_Agent`（8 步）：只读 + 计算 + 提案写。
- `Risk_Monitor_Agent`（6 步）：只读 + `scan_risks`。

**不允许** import 内核或 `tools/handlers`：Agent 只能经 `Tool_Registry.invoke()`
触达任何能力（design.md 分层规则 2，由 `test_layering.py` 静态断言）。

模块与落地任务：`prompts/`（静态提示词常量）与 `contracts.py` ── 任务 5.6；
三个 Agent 实现 ── 任务 5.6、5.7、7.2、10.3。
"""
