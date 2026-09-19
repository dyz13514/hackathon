/**
 * 可承诺交期报价端点的类型化客户端（design.md Components §6 `/quote`，任务 13.6，R17）。
 *
 * 形状逐字对应后端 `app/api/quotes.py`。报价是**只读沙箱计算**（R17.4）：最早可承诺完工日、被推迟
 * 订单、总拖期变化全部由后端沙箱确定性实算，不改动任何生产数据或计划。
 */

import { apiFetch } from './client';

export interface PromiseDateRequest {
  readonly product_id: string;
  readonly quantity: number;
  /** 期望交期（ISO8601）。 */
  readonly desired_due_date: string;
}

export interface PromiseDateResult {
  readonly feasible: boolean;
  /** 最早可承诺完工时刻（ISO8601）；不可行为 null。 */
  readonly earliest_completion: string | null;
  readonly desired_due_date: string;
  readonly desired_date_met: boolean;
  readonly deferred_order_ids: readonly string[];
  readonly total_tardiness_minutes: number;
  readonly active_total_tardiness_minutes: number;
  readonly total_tardiness_delta_minutes: number;
  /** 不可行 / 无法满足期望交期时的具体约束原因（R17.3）。 */
  readonly constraint_reason: string | null;
}

/**
 * 计算一笔询价的最早可承诺完工日（写端点，需已登录会话）。只读沙箱计算。
 *
 * 后端可能返回 409（无 ACTIVE 计划）或 422（产品不存在）——以 `ApiError` 抛出。
 */
export function quotePromiseDate(input: PromiseDateRequest): Promise<PromiseDateResult> {
  return apiFetch<PromiseDateResult>('/quotes/promise-date', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}
