"""摄取工具的 handler（任务 5.2，R22.6、R2）。

3 个摄取工具，只有 `Ingestion_Agent`（与系统流水线不碰它们）在白名单里。它们处理**最不受
信任的外部文件**，因此与排产逻辑物理隔离（`INGESTION_AGENT` 连「有哪些订单」都调不到）。

## `validate_mapping` 刻意是**确定性**工具（R2.8）

列映射的**提议**（`propose_column_mapping`）是 LLM 的活——把脏表头猜成目标字段需要语言理解。
但**校验**一个映射（拿它在整份文件上试跑一遍解析，数出哪些单元格解析失败、类型错误、归一化
失败）是**可判定**的，不该交给 LLM：让模型去「验算」既浪费 token 又不可靠。因此
`validate_mapping` 是纯确定性解析，`propose_column_mapping` 才调 LLM。这条分工是 K-07
「绝不静默猜测」的一半——歧义处 LLM 提议 + 人工确认，确定处代码验算。

## 接线归属

三个工具的落库/解析路径随摄取流水线（§2）落地：`read_uploaded_file_preview` 读上传文件出
预览、`propose_column_mapping` 调 LLM 出映射提议、`validate_mapping` 确定性试跑。契约在此
完整定义（使注册表 26 工具完整、白名单矩阵覆盖到它们），接线后委派对应服务。
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.orm import Session

from app.tools import models as m
from app.tools.registry import ToolContext


def _session(ctx: ToolContext) -> Session:
    if ctx.session is None:
        raise RuntimeError("摄取 handler 需要 ctx.session；registry 装配时必须注入会话")
    # `ToolContext.session` 刻意是 `Any`（registry 不硬依赖 ORM 类型）；handler 知道注入的
    # 是 `Session`，在此显式收窄，避免 `warn_return_any` 泄漏一个 `Any`。
    return cast(Session, ctx.session)


def read_uploaded_file_preview(
    args: m.ReadPreviewIn, ctx: ToolContext
) -> m.FilePreviewOut:
    """读上传文件的前若干行出预览（R2）。委派给 §2 的文件预览服务。

    样本值是 untrusted 文本（每个 ≤40 字符、每列 ≤3 个），装配提示词时须被 `<untrusted>`
    包裹（R23.1）——契约已把上限钉在字段约束里。预览服务随 §2 落地。
    """
    raise NotImplementedError(
        "read_uploaded_file_preview 委派 §2 的文件预览服务；契约已定义"
    )


def propose_column_mapping(
    args: m.ProposeColumnMappingIn, ctx: ToolContext
) -> m.ColumnMappingProposal:
    """LLM 列映射提议（R2）。委派给 §2 的映射提议服务（唯一调 LLM 的摄取工具）。

    低置信字段标 `NEEDS_CONFIRMATION`、缺必填字段进 `missing_required_fields`、归一化提议
    显式给出 `conversion_factor`（R2.5）——绝不静默猜测（K-07）。提议服务随 §2 落地。
    """
    raise NotImplementedError(
        "propose_column_mapping 委派 §2 的 LLM 映射提议服务；契约已定义"
    )


def validate_mapping(args: m.ValidateMappingIn, ctx: ToolContext) -> m.ValidateMappingOut:
    """**确定性**校验一个映射（R2.8）：拿它在整份文件上试跑解析，数出各类失败。

    不调 LLM——这一步可判定（见模块 docstring）。委派给 §2 的确定性解析器：逐行套用
    `mapping` 的字段映射与归一化，把解析不了的单元格收进 `unparsed_cells`（含行号、列名、
    原始值），并计 `type_error_count` / `normalisation_failure_count`。解析器随 §2 落地。
    """
    raise NotImplementedError(
        "validate_mapping 委派 §2 的确定性解析器（不调 LLM，R2.8）；契约已定义"
    )
