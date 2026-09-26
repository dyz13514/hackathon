/**
 * What-if 场景端点的类型化客户端（design.md Components §6 `/whatif`，任务 8.3，R16）。
 *
 * 形状逐字对应后端 `app/api/scenarios.py` 的 `RunScenarioRequest` / `ScenarioResultOut` /
 * `AdoptScenarioResponse`。手写而非 OpenAPI 生成，理由同 `risks.ts`：本任务只落这两个端点。
 *
 * **P0 只有结构化场景表单**（5 类之一 + 参数）；自然语言输入是 P1（任务 13.1）。
 */

import { apiFetch } from './client';

/** 5 类结构化场景变更之一（R16.2）。判别字段是 `kind`。 */
export type ScenarioMutation =
  | {
      readonly kind: 'ADD_OR_CHANGE_ORDER';
      readonly order_id?: string | null;
      readonly product_id?: string | null;
      readonly quantity?: number | null;
      readonly due_date?: string | null;
      readonly priority?: 'URGENT' | 'HIGH' | 'NORMAL' | 'LOW' | null;
    }
  | {
      readonly kind: 'SET_MACHINE_UNAVAILABLE';
      readonly machine_id: string;
      readonly start_time: string;
      readonly end_time: string;
    }
  | {
      readonly kind: 'CHANGE_MATERIAL_AVAILABILITY';
      readonly material_id: string;
      readonly quantity_available: number;
    }
  | {
      readonly kind: 'SET_WORKER_UNAVAILABLE';
      readonly worker_id: string;
      readonly start_time: string;
      readonly end_time: string;
    }
  | {
      readonly kind: 'CHANGE_ORDER_PRIORITY';
      readonly order_id: string;
      readonly priority: 'URGENT' | 'HIGH' | 'NORMAL' | 'LOW';
    };

/** 一次推演结果 + 与 ACTIVE 计划的对比（R16.8）。全部确定性、无 LLM。 */
export interface ScenarioResult {
  readonly scenario_id: string;
  readonly feasibility: string;
  readonly late_order_count: number;
  readonly active_late_order_count: number;
  readonly late_order_count_delta: number;
  readonly total_tardiness_minutes: number;
  readonly active_total_tardiness_minutes: number;
  readonly total_tardiness_delta_minutes: number;
  readonly total_score: number;
  readonly active_total_score: number;
  readonly new_unschedulable_jobs: readonly string[];
  readonly delayed_order_ids: readonly string[];
}

/** 采纳的响应：新提案句柄（仍待审批，R16.9）。 */
export interface AdoptResult {
  readonly scenario_id: string;
  readonly plan_id: string;
  readonly status: string;
}

/**
 * 自然语言 What-if 翻译结果（P1，任务 13.1，R16.1）。
 *
 * `mutations` 是翻译得到的**任务 8.3 结构化载荷**，**未执行**——供规划员确认后原样交
 * `runScenario` 执行。`injection_suspected` 供 UI 提示（不阻断，R16.10）。
 */
export interface TranslateResult {
  readonly mutations: readonly ScenarioMutation[];
  readonly supported_kinds: readonly string[];
  readonly injection_suspected: boolean;
  readonly source_query_echo: string;
}

/**
 * 把一句自然语言 What-if 提问翻译成结构化场景变更（P1，任务 13.1）。**不执行**——返回结果
 * 供确认后再调用 `runScenario`。写端点，需已登录会话。
 *
 * 后端可能返回 `SCENARIO_CLARIFICATION_REQUIRED` / `UNSUPPORTED_SCENARIO`（422）
 * 或 `LLM_UNAVAILABLE_USE_STRUCTURED_FORM`（503，
 * 降级模式）——两者都以 `ApiError` 抛出，调用方据 `code` 分别提示。
 */
export function translateScenario(query: string): Promise<TranslateResult> {
  return apiFetch<TranslateResult>('/scenarios/translate', {
    method: 'POST',
    body: JSON.stringify({ query }),
  });
}

/** 运行一个结构化 What-if 推演（R16.7）。写端点，需已登录会话。 */
export function runScenario(mutations: readonly ScenarioMutation[]): Promise<ScenarioResult> {
  return apiFetch<ScenarioResult>('/scenarios/run', {
    method: 'POST',
    body: JSON.stringify({ mutations }),
  });
}

/** 以该场景生成正式提案（R16.9，仍走审批）。写端点。 */
export function adoptScenario(scenarioId: string): Promise<AdoptResult> {
  return apiFetch<AdoptResult>(`/scenarios/${encodeURIComponent(scenarioId)}/adopt`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}
