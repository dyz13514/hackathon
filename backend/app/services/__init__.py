"""应用服务层（确定性）。

模块与落地任务：`approval.py`（3.1–3.3）、`exporter.py`（3.6）、
`guardrail.py`（5.8–5.10）、`sandbox.py`（9.x）、`spreadsheet.py`（10.1）、
`ingestion.py`（10.4）、`preference.py`（11.1）、`value_ledger.py`（11.x）。

`Approval_Service.approve()` 是 `ACTIVE` 状态的唯一到达路径（属性 15）。
"""
