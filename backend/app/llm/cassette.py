"""按请求哈希录制回放的 cassette（任务 5.3，R26.5、design.md ADR-011）。

design.md 成本章节把三种模式的分工写死：

- `REPLAY`——**本地与 CI 的默认**。按请求哈希回放已录制的响应，零成本零网络。找不到
  录制即失败（`get_or_fail` 抛错），因为「本该已录制的调用却没有录制」是一个需要人处理
  的缺口，静默返回一个假响应会让评估失去意义。
- `STUB`——用于**尚未录制的新用例**（conftest 强制测试用此模式）。找不到录制时返回一个
  固定的模板响应，让测试能在不触网、不消耗额度的前提下跑通新路径。
- `LIVE`——只有它真实调用网关（在 `adapter.py` 里），并可把响应**录制**进 cassette 供
  日后 `REPLAY`。

## 存储形态

每条录制是一个 JSON 文件，文件名即请求的 `content_hash`（`<hash>.json`），放在
`tests/cassettes/` 下（`.gitkeep` 已说明纳入版本控制）。文件内容是 `LlmResponse` 的
序列化加上原始请求的少量元信息，便于人工核对「这条录制对应哪个 Agent 的什么请求」。
按哈希命名而非按序号，是为了让同一请求在任何机器上都定位到同一个文件——哈希是内容的
函数，与录制顺序无关。

## 为什么 cassette 不认识 `LlmResponse` 的构造细节

cassette 只做序列化与反序列化，`LlmResponse` 的字段语义（尤其 `usage` 的零成本副本）
归 `adapter.py`。这样单价、用量口径的改动不会波及存储层。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.llm.adapter import LlmMode, LlmRequest, LlmResponse


#: 默认 cassette 目录：`backend/tests/cassettes/`。以本文件位置反推，避免依赖 CWD。
#: `app/llm/cassette.py` → parents[2] 是 `backend/`。
_DEFAULT_CASSETTE_DIR = Path(__file__).resolve().parents[2] / "tests" / "cassettes"

#: `STUB` 模式下未录制用例返回的固定文本。刻意平淡、可辨认——它出现在输出里就说明
#: 走的是 stub 路径而非真实响应或录制。
_STUB_CONTENT = "[STUB] 未录制的 LLM 响应占位文本。"


class CassetteMiss(RuntimeError):
    """`REPLAY` 模式下按哈希找不到录制。

    这不是可忽略的缺省，而是一个需要处理的缺口：要么补录（`LLM_MODE=LIVE` 跑一次），
    要么把该用例改用 `STUB`。错误信息带上哈希与 Agent，便于定位该补哪条。
    """


class Cassette:
    """请求哈希 → 录制响应的文件后端。"""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory if directory is not None else _DEFAULT_CASSETTE_DIR

    def _path_for(self, content_hash: str) -> Path:
        return self._dir / f"{content_hash}.json"

    def get_or_fail(
        self, content_hash: str, *, mode: LlmMode, request: LlmRequest
    ) -> LlmResponse:
        """按哈希取录制。`REPLAY` 未命中抛 `CassetteMiss`；`STUB` 未命中返回占位。

        `mode` 决定未命中时的行为，`request` 只在两处用到：`STUB` 的占位响应需要一个
        零用量的 `LlmUsage`，以及 `CassetteMiss` 的报错要带上 Agent 名。命中时两者都
        不影响返回值——录制是内容的函数。
        """
        # 延迟到运行期 import，避免与 adapter 形成 import 环（adapter 只在 TYPE_CHECKING
        # 下引用 Cassette；cassette 在函数体内引用 adapter 的值对象）。
        from app.llm.adapter import LlmMode as _Mode
        from app.llm.adapter import LlmResponse, LlmUsage

        path = self._path_for(content_hash)
        if path.exists():
            return self._load(path)

        if mode is _Mode.STUB:
            return LlmResponse(content=_STUB_CONTENT, usage=LlmUsage.zero())

        raise CassetteMiss(
            f"REPLAY 模式下缺少录制：agent={request.agent!r} hash={content_hash}。"
            f"补录请以 LLM_MODE=LIVE 跑一次，或将该用例改用 STUB。"
        )

    def record(self, content_hash: str, request: LlmRequest, response: LlmResponse) -> None:
        """把一次真实响应录制进 cassette（`LIVE` 模式下由 adapter 调用）。

        写入原始请求的元信息（Agent、user 文本、max_tokens）供人工核对，但**回放只读
        `response`**——元信息不参与命中判定，命中判定只看文件名（哈希）。
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        document = {
            "content_hash": content_hash,
            "request": {
                "agent": request.agent,
                "user": request.user,
                "max_tokens": request.max_tokens,
            },
            "response": {
                "content": response.content,
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
            },
        }
        self._path_for(content_hash).write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    def _load(path: Path) -> LlmResponse:
        from app.llm.adapter import LlmResponse, LlmUsage

        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        response_raw = raw["response"]
        usage_raw = response_raw["usage"]
        return LlmResponse(
            content=str(response_raw["content"]),
            usage=LlmUsage(
                input_tokens=int(usage_raw["input_tokens"]),
                output_tokens=int(usage_raw["output_tokens"]),
            ),
        )
