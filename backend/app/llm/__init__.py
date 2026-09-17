"""模型接入层。

`adapter.py` 是全仓库唯一调用 Bedrock 的地方，因此 `DETERMINISTIC_ONLY` 只需在
此处旁路一次（design.md §2.1、R21.10）。静态扫描断言全仓库仅此一处出现网关 URL。

模块与落地任务：`adapter.py` / `cassette.py` / `pricing.py` ── 任务 5.3。
"""
