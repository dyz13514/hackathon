# 评估套件（`tests/eval/`）

29 条 EVAL 用例：EVAL-001–015（黄金路径）与 EVAL-201–214（对抗），**全部非可选**
（design.md Testing Strategy §4、`R26`）。

## 一条命令入口

```
make eval          # LLM_MODE=REPLAY，零成本、零网络，CI 默认
make eval-live     # LLM_MODE=LIVE，触达真实网关，计入 PROJECT_REAL_RUN_CAP，仅演示前使用
make eval-report   # LLM_MODE=REPLAY 跑一遍并生成 eval_report.md（逐用例状态 + 断言明细，R26.4）
```

- 选择方式：本目录下的每个用例由 `conftest.py` 自动打上 `eval` 标记，`make eval` 用
  `pytest -m eval` 选中；`make test` 用 `--ignore=tests/eval` 排除。边界由**目录**决定。
- 模式：`conftest.py` 兜底把 `LLM_MODE` 设为 `REPLAY`（除非 `make eval-live` 显式设了
  `LIVE`），因此 `make eval` 绝不产生真实 Bedrock 调用。

## 共享夹具（骨架，任务 12.1）

`conftest.py` 提供 `eval_engine` / `eval_factory` / `eval_seeded`：在临时 SQLite 库上建表、
绑定审计引擎、载入演示数据（`app.seed`）。EVAL 用例从 `eval_seeded` 这个确定性输入起点
开始。`test_eval_harness.py` 是「夹具怎么用」的最小范例，不是任何一条 EVAL-xxx 业务用例。

## cassette 新鲜度纪律

需要 LLM 响应的用例走 `tests/cassettes/` 的录制回放（按请求哈希命名）。**提示词一旦变更，
必须以 `LLM_MODE=LIVE` 重新录制**——否则 `REPLAY` 会按旧哈希 `CassetteMiss` 而失败，这是
刻意的：陈旧录制静默通过比失败更危险。详见 `app/llm/cassette.py` 的模块文档。
