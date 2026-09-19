/**
 * 风险面板端点的类型化客户端（design.md Components §6 `/risks`，任务 8.5/8.6，R14）。
 *
 * 形状逐字对应后端 `app/api/risks.py` 的 `RiskFindingOut` / `RiskListResponse`。手写而非
 * OpenAPI 生成，理由同 `state.ts`：本任务只落这几个端点。
 */

import { apiFetch } from './client';

/** 一条风险发现（R14.9）。`narrative_source` 在 P0 恒为 `TEMPLATE`（R14.5）。 */
export interface RiskFinding {
  readonly finding_id: string;
  readonly risk_type: string;
  readonly severity: 'INFO' | 'WARNING' | 'CRITICAL';
  readonly entity_type: string;
  readonly entity_id: string;
  readonly metric_value: number;
  readonly threshold_value: number;
  readonly affected_order_ids: readonly string[];
  readonly narrative: string | null;
  /** `TEMPLATE`（P0 模板）或 `LLM`（P1）——UI 以徽章区分二者（R14.11）。 */
  readonly narrative_source: string | null;
  readonly first_seen_at: string;
  readonly last_seen_at: string;
  /** CRITICAL 风险的缓解提案 plan_id（R14.7）；INFO/WARNING 恒为 null（面板专属）。 */
  readonly mitigation_plan_id: string | null;
}

/** 当前风险发现列表 + 三档计数（R14.6，供面板顶栏与分组渲染）。逐字对应后端 `RiskListResponse`。 */
export interface RiskList {
  readonly findings: readonly RiskFinding[];
  readonly critical_count: number;
  readonly warning_count: number;
  readonly info_count: number;
}

/** 一次手动扫描的结果（R14.1）。 */
export interface ScanResult {
  readonly finding_count: number;
  readonly inserted: number;
  readonly updated: number;
  readonly findings: readonly RiskFinding[];
}

/** 列出当前风险发现（R14.6）。只读端点，无需认证。 */
export function getRisks(): Promise<RiskList> {
  return apiFetch<RiskList>('/risks');
}

/** 手动触发一次确定性风险扫描（R14.1）。写端点，需已登录会话。 */
export function scanRisks(): Promise<ScanResult> {
  return apiFetch<ScanResult>('/risks/scan', {
    method: 'POST',
    body: JSON.stringify({}),
  });
}
