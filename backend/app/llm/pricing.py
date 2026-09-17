"""单价表：集中一处（任务 5.3，design.md §2.5、KPI 表脚注）。

成本口径只有一个来源。design.md §2.5 把单价写成 `PRICE = PriceTable(...)`，
理由是「便于校准」——单价散落在多处时，改一个漏一个的错误不会报错，只会让
`traces` / `llm_cache` 里的 USD 估算悄悄不一致。因此本模块是全仓库唯一定义
每 token 单价的地方，`Token_Budget_Manager`（任务 5.4）与 `Bedrock_Adapter`
都从这里取 `cost_of(...)`。

单价来自 KPI 表脚注：输入 USD 3.00 / 输出 USD 15.00 每百万 token。用 `Decimal`
而非 `float`：美元金额的累计要能逐分核对（成本章节要求每一步可独立复核），
浮点累加会引入不可复现的尾差。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final, Protocol

#: 一百万，单价表的分母。写成常量而非魔法数字，让 `cost_of` 的算术一眼可读。
_PER_MILLION: Final = Decimal(1_000_000)


class _UsageLike(Protocol):
    """`cost_of` 需要的最小接口：两个 token 计数。

    用 `Protocol` 而非直接依赖 `LlmUsage`，是为了让 `pricing` 不反向 import
    `adapter`——单价表处在依赖链的最底层，被 adapter 与 budget manager 共用。
    """

    @property
    def input_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int: ...


class PriceTable:
    """每百万 token 的输入 / 输出单价。不可变，实例化后单价不再变化。"""

    __slots__ = ("input_per_mtok", "output_per_mtok")

    def __init__(self, *, input_per_mtok: Decimal, output_per_mtok: Decimal) -> None:
        self.input_per_mtok = input_per_mtok
        self.output_per_mtok = output_per_mtok

    def cost_of(self, usage: _UsageLike) -> Decimal:
        """按输入 / 输出 token 数算出这次调用的美元成本。

        零 token（缓存命中）算出恰好 `Decimal("0")`：`with_zero_cost()` 的响应
        代入这里应当得到零成本，这是「同输入第二次零支出」（R25.7）的算术保证。
        """
        input_cost = self.input_per_mtok * Decimal(usage.input_tokens) / _PER_MILLION
        output_cost = self.output_per_mtok * Decimal(usage.output_tokens) / _PER_MILLION
        return input_cost + output_cost


#: 全仓库唯一的单价来源（design.md §2.5）。校准单价只改这一处。
PRICE: Final = PriceTable(
    input_per_mtok=Decimal("3.00"),
    output_per_mtok=Decimal("15.00"),
)
