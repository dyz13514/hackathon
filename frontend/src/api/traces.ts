/**
 * 可观测性端点的类型化客户端（design.md Components §5「观测」分组，任务 5.12）。
 *
 * 形状逐字对应后端 `app/api/traces.py` 的 `TraceSummaryOut` / `TraceDetailOut` /
 * `AuditEntryOut`。手写而非 OpenAPI 生成，理由同 `plans.ts` / `state.ts`：本任务只落这
 * 三个只读端点，一份紧贴后端契约的手写类型比引入代码生成链更轻。
 */

import { apiFetch } from './client';

export interface TraceSummary {
  readonly trace_id: string;
  readonly kind: string;
  /** PIPELINE | REACT——决定详情页顶部的形态标注。 */
  readonly mode: string;
  readonly agent: string | null;
  readonly trigger_source: string;
  readonly session_id: string;
  readonly started_at: string;
  readonly ended_at: string | null;
  readonly outcome: string | null;
  readonly step_count: number;
  readonly total_input_tokens: number;
  readonly total_output_tokens: number;
  readonly estimated_usd: number;
  readonly result_ref: string | null;
}

export interface ToolCall {
  readonly call_id: string;
  readonly step_id: string | null;
  readonly caller: string;
  readonly tool_name: string;
  readonly args_digest: string;
  /** 输出摘要，非完整结果（完整 token 数在 result_tokens）。 */
  readonly result_summary: string;
  readonly result_tokens: number;
  readonly truncated: boolean;
  readonly outcome: string;
  readonly duration_ms: number;
}

export interface TraceStep {
  readonly step_id: string;
  readonly step_index: number;
  readonly step_kind: string;
  /** 结构化摘要，非模型原始推理链（R24.7）。 */
  readonly decision_reason: string | null;
  readonly duration_ms: number;
  readonly input_tokens: number;
  readonly output_tokens: number;
  readonly tool_calls: readonly ToolCall[];
}

export interface TraceDetail extends TraceSummary {
  readonly steps: readonly TraceStep[];
  readonly unassigned_tool_calls: readonly ToolCall[];
}

export interface AuditEntry {
  readonly audit_id: string;
  readonly event_category: string;
  readonly event_type: string;
  readonly actor: string;
  readonly subject_type: string | null;
  readonly subject_id: string | null;
  readonly payload: Record<string, unknown>;
  readonly trace_id: string | null;
  readonly occurred_at: string;
}

/** 可按时间 / Agent / 触发类型筛选的 Trace 列表（R24.2）。 */
export interface TraceFilter {
  readonly agent?: string;
  readonly triggerSource?: string;
  readonly startedAfter?: string;
  readonly startedBefore?: string;
}

function toQuery(params: Record<string, string | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') {
      search.set(key, value);
    }
  }
  const query = search.toString();
  return query ? `?${query}` : '';
}

/** Trace 列表，可按时间 / Agent / 触发类型筛选（R24.2）。 */
export function listTraces(filter: TraceFilter = {}): Promise<TraceSummary[]> {
  const query = toQuery({
    agent: filter.agent,
    trigger_source: filter.triggerSource,
    started_after: filter.startedAfter,
    started_before: filter.startedBefore,
  });
  return apiFetch<TraceSummary[]>(`/traces${query}`);
}

/** 单个 Trace 全文：逐步工具名 / 输入输出摘要 / 耗时 / token / decision_reason（R24.1）。 */
export function getTrace(traceId: string): Promise<TraceDetail> {
  return apiFetch<TraceDetail>(`/traces/${encodeURIComponent(traceId)}`);
}

/** 审计日志的只读查询（append-only，无写接口，R24.3）。 */
export function listAuditLog(subjectId?: string): Promise<AuditEntry[]> {
  return apiFetch<AuditEntry[]>(`/audit-log${toQuery({ subject_id: subjectId })}`);
}
