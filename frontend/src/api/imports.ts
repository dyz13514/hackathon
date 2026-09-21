/**
 * 电子表格摄取端点的类型化客户端（design.md §4.2/§6 `/import`，任务 10.5，R2/R3）。
 *
 * 形状对应后端 `app/api/imports.py`。手写而非 OpenAPI 生成，理由同 `risks.ts` / `scenarios.ts`。
 * 上传走 multipart（不能用 `apiFetch` 的 JSON 头），其余走 `apiFetch`。
 */

import { API_BASE, ApiError, apiFetch } from './client';

export interface UploadResult {
  readonly upload_id: string;
  readonly file_name: string;
  readonly total_rows: number;
  readonly duplicate_of: string | null;
  readonly last_imported_at: string | null;
}

export interface FieldMapping {
  readonly target_field: string;
  readonly source_column: string | null;
  readonly confidence: number;
  readonly sample_values: readonly string[];
  readonly status: 'AUTO_ACCEPTED' | 'NEEDS_CONFIRMATION' | 'NOT_IMPORTED';
}

export interface MissingField {
  readonly target_field: string;
  readonly reason: string;
}

export interface Normalisation {
  readonly source_column: string;
  readonly kind: 'DATE_FORMAT' | 'UNIT_CONVERSION';
  readonly detected_pattern: string;
  readonly conversion_factor: number | null;
  readonly sample_before: readonly string[];
  readonly sample_after: readonly string[];
}

export interface Proposal {
  readonly entity_type: string;
  readonly entity_type_confidence: number;
  readonly field_mappings: readonly FieldMapping[];
  readonly missing_required_fields: readonly MissingField[];
  readonly normalisations: readonly Normalisation[];
}

export interface ColumnPreview {
  readonly index: number;
  readonly raw_header: string;
  readonly inferred_kind: string;
  readonly null_ratio: number;
  readonly sample_values: readonly string[];
}

export interface Preview {
  readonly detected_header_row: number;
  readonly total_rows: number;
  readonly preview_tokens: number;
  readonly columns: readonly ColumnPreview[];
  readonly formula_columns: readonly string[];
}

export interface ProposalResponse {
  readonly upload_id: string;
  readonly proposal: Proposal;
  readonly agent_outcome: string;
  readonly from_agent: boolean;
  readonly preview: Preview;
}

export interface ValidateResult {
  readonly upload_id: string;
  readonly parsed_row_count: number;
  readonly unparsed_cells: ReadonlyArray<{
    readonly row_number: number;
    readonly column_name: string;
    readonly raw_value: string;
    readonly reason: string;
  }>;
  readonly type_error_count: number;
  readonly normalisation_failure_count: number;
}

export interface CommitResult {
  readonly batch_id: string;
  readonly entity_type: string;
  readonly imported_row_count: number;
}

export interface BatchSummary {
  readonly batch_id: string;
  readonly file_name: string;
  readonly entity_type: string;
  readonly row_count: number;
  readonly status: string;
  readonly imported_at: string | null;
}

/** 上传一个表格文件（multipart）。写端点，需已登录会话。 */
export async function uploadImport(file: File): Promise<UploadResult> {
  const form = new FormData();
  form.append('file', file);
  const response = await fetch(`${API_BASE}/imports/upload`, {
    method: 'POST',
    credentials: 'include',
    body: form, // 不设 Content-Type：浏览器自动带 multipart boundary
  });
  if (!response.ok) {
    let code = 'UNKNOWN';
    let message = 'Upload failed';
    try {
      const body = (await response.json()) as { error?: { code?: string; message?: string } };
      code = body.error?.code ?? code;
      message = body.error?.message ?? message;
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new ApiError(response.status, code, message);
  }
  return (await response.json()) as UploadResult;
}

/** 取列映射提议 + 有界预览（经 Ingestion_Agent，R2.2/2.6/2.7）。 */
export function getProposal(uploadId: string): Promise<ProposalResponse> {
  return apiFetch<ProposalResponse>(`/imports/${encodeURIComponent(uploadId)}/proposal`);
}

/** 确定性校验一份映射（R2.8）。 */
export function validateMapping(uploadId: string, mapping: unknown): Promise<ValidateResult> {
  return apiFetch<ValidateResult>(`/imports/${encodeURIComponent(uploadId)}/validate`, {
    method: 'POST',
    body: JSON.stringify({ mapping }),
  });
}

/** 人工确认后落库（R2.9/R3.2）。 */
export function confirmImport(
  uploadId: string,
  body: {
    entity_type: string;
    field_mappings: FieldMapping[];
    unparsed_cells_resolved: boolean;
  },
): Promise<CommitResult> {
  return apiFetch<CommitResult>(`/imports/${encodeURIComponent(uploadId)}/confirm`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

/** 列出导入批次。 */
export function listImports(): Promise<{ batches: readonly BatchSummary[] }> {
  return apiFetch<{ batches: readonly BatchSummary[] }>('/imports');
}

/** 整批回滚（R3.4/R3.5）。 */
export function revertImport(batchId: string): Promise<{ batch_id: string; reverted_row_count: number }> {
  return apiFetch<{ batch_id: string; reverted_row_count: number }>(
    `/imports/${encodeURIComponent(batchId)}/revert`,
    { method: 'POST', body: JSON.stringify({}) },
  );
}
