"""AI Production Planning Agent 后端。

分层与目录职责见 design.md「项目结构」。三条可测的分层规则（任务 1.8 的
`tests/structure/test_layering.py` 静态断言）：

1. `app/core/**` 不 import `sqlalchemy` / `fastapi` / `httpx` / `boto3`。
2. `app/agents/**` 不 import 内核或 `tools/handlers`，只经 `Tool_Registry.invoke()`。
3. 全仓库仅 `app/llm/adapter.py` 出现 Bedrock 网关 URL。
"""

__version__ = "0.1.0"
