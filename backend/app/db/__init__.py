"""持久层：SQLAlchemy 2.0 声明式模型，schema 保持 PostgreSQL 兼容（R27.4）。

模块与落地任务：`models.py` / `repositories.py`（1.2）、`events.py`（1.3，
`input_snapshot_version` 推进钩子）、`audit.py`（1.4，append-only）、
`sandbox_guard.py`（8.1，引擎级 DML 拦截 + `AUDIT_BYPASS`）。
"""
