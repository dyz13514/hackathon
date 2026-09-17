"""确定性内核：纯 Python，无 I/O。

**禁止 import** `sqlalchemy` / `fastapi` / `httpx` / `boto3`（design.md
Architecture §1 分层规则 1，由 `tests/structure/test_layering.py` 静态断言）。
这条规则同时是沙箱隔离的第一条支撑：沙箱计算全在这一层，「拿不到会话」是包依赖
事实而非约定。

模块与落地任务：`snapshot.py`（2.1）、`scheduling.py`（2.3、2.4、2.6）、
`validation.py`（2.8）、`scoring.py`（2.10）、`baseline.py`（2.11）、
`explain.py`（5.11）、`replan.py`（7.1）、`autonomy.py`（7.3）、`risk.py`（8.x）。
"""
