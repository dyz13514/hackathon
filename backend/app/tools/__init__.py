"""工具注册表：Agent 与系统流水线触达能力的唯一入口。

`registry.py` 的 `invoke()` 固定 7 步闸门（白名单 → 输入 schema → 执行 →
输出 schema → 字段投影 → token 截断 → 记账），两种执行形态共享此单点，因此
`Trace` / `Audit_Log` / 预算记账对形态 A 与 B 一视同仁（design.md §2.3）。

`handlers/` 不被 `app/agents/` import（分层规则 2）。

模块与落地任务：`registry.py` / `clamp.py` ── 任务 5.1；`models.py` 与
`handlers/{read,compute,write,ingest}.py` ── 任务 5.2。
"""
