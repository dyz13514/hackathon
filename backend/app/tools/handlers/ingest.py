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

from typing import Protocol, cast

from app.services import ingestion as ingestion_svc
from app.services.spreadsheet import ParsedFile, build_preview
from app.tools import models as m
from app.tools.registry import ToolContext


class IngestionToolSession(Protocol):
    """`ctx.session` 在列映射 ReAct 路径上携带的最小接口。

    摄取工具需要的不是 DB 会话，而是**当前上传的已解析文件**（预览/校验都在它上面跑）。
    `ToolContext.session` 刻意是 `Any`（registry 不硬依赖类型），因此这里用一个 Protocol 声明
    handler 期望的形状：一个能按 `upload_id` 取 `ParsedFile` 的对象。列映射编排服务
    （`app/services/ingestion_agent_run.py`）注入一个满足它的实例。
    """

    def parsed_for(self, upload_id: str) -> ParsedFile: ...


def _ingestion_session(ctx: ToolContext) -> IngestionToolSession:
    if ctx.session is None:
        raise RuntimeError("摄取 handler 需要 ctx.session（IngestionToolSession）；装配时注入")
    return cast(IngestionToolSession, ctx.session)


def read_uploaded_file_preview(
    args: m.ReadPreviewIn, ctx: ToolContext
) -> m.FilePreviewOut:
    """读上传文件的前若干行出预览（R2.2）。确定性——不调 LLM。

    样本值是 untrusted 文本（每个 ≤40 字符、每列 ≤3 个），由 `build_preview` 保证上界并交由
    装配层包裹（R23.1）。委派给确定性 `Spreadsheet_Parser.build_preview`（任务 10.2）。
    """
    parsed = _ingestion_session(ctx).parsed_for(args.upload_id)
    preview = build_preview(parsed, max_sample_rows=args.max_sample_rows)
    return m.FilePreviewOut(
        upload_id=args.upload_id,
        detected_header_row=preview.detected_header_row,
        total_rows=preview.total_rows,
        columns=[
            m.ColumnPreview(
                index=c.index,
                raw_header=c.raw_header,
                inferred_kind=c.inferred_kind,  # type: ignore[arg-type]
                null_ratio=c.null_ratio,
                sample_values=c.sample_values,
            )
            for c in preview.columns
        ],
        formula_columns=preview.formula_columns,
        preview_tokens=preview.preview_tokens,
    )


def propose_column_mapping(
    args: m.ProposeColumnMappingIn, ctx: ToolContext
) -> m.ColumnMappingProposal:
    """列映射提议（R2.6/2.7）。

    这是列映射路径里唯一「需要语言理解」的一步——在真实/录制 LLM 路径下由 `Ingestion_Agent`
    产出提案（Agent 的 `final`）。作为 ReAct 循环里的**工具**，本 handler 提供一份确定性的
    候选提议（表头别名 + 归一探测）供 Agent 参考/采纳：低置信字段标 `NEEDS_CONFIRMATION`、
    缺必填字段进 `missing_required_fields`、归一显式给 `conversion_factor`（R2.5）——绝不静默
    猜测（K-07）。Agent 的最终提案仍须经确定性 `validate_mapping` 与人工确认闸门。
    """
    parsed = _ingestion_session(ctx).parsed_for(args.upload_id)
    hint = args.entity_type_hint
    proposal = ingestion_svc.propose_mapping(parsed, entity_type_hint=hint)
    return m.ColumnMappingProposal(
        entity_type=proposal["entity_type"],
        entity_type_confidence=proposal["entity_type_confidence"],
        field_mappings=[m.FieldMapping(**fm) for fm in proposal["field_mappings"]],
        missing_required_fields=[
            m.MissingField(**mf) for mf in proposal["missing_required_fields"]
        ],
        normalisations=[
            m.NormalisationProposal(**n) for n in proposal["normalisations"]
        ],
    )


def validate_mapping(args: m.ValidateMappingIn, ctx: ToolContext) -> m.ValidateMappingOut:
    """**确定性**校验一个映射（R2.8）：拿它在整份文件上试跑解析，数出各类失败。不调 LLM。

    委派给确定性 `ingestion.validate_mapping`：逐行套用字段映射与归一化，把解析不了的单元格
    收进 `unparsed_cells`（含行号、列名、原始值），并计 `type_error_count` /
    `normalisation_failure_count`。这条是 K-07「绝不静默丢弃」的确定性一半。
    """
    parsed = _ingestion_session(ctx).parsed_for(args.upload_id)
    outcome = ingestion_svc.validate_mapping(parsed, args.mapping.model_dump(mode="python"))
    return m.ValidateMappingOut(
        upload_id=args.upload_id,
        parsed_row_count=outcome.parsed_row_count,
        unparsed_cells=[m.UnparsedCell(**c) for c in outcome.unparsed_cells],
        type_error_count=outcome.type_error_count,
        normalisation_failure_count=outcome.normalisation_failure_count,
    )
