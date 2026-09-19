/**
 * `GET /api/health` 的类型化客户端（design.md「运维要点」、R27.6；任务 11.6 的顶栏消费它）。
 *
 * 顶栏用它做两件事（R25.8/R25.9、R25.4）：
 * 1. 降级模式横幅——`mode === 'DETERMINISTIC_ONLY'` 时全局提示 LLM 路径已旁路；
 * 2. 预算告警——`project_usd_spent` 接近 `PROJECT_USD_CEILING` 时提示（见 App 顶栏）。
 *
 * 形状逐字对应后端 `app/api/admin.py` 的 `HealthResponse`。健康检查无需认证（探针在
 * Session_Auth 之前就要能用），因此 `apiFetch` 的 Cookie 有无都不影响它。
 */

import { apiFetch } from './client';

/** 单一项目美元上限（design.md 成本章节 §2：PROJECT_USD_CEILING = 35）。 */
export const PROJECT_USD_CEILING = 35;

/** 预算告警阈值比例（R25.4：达 80% 告警）。 */
export const BUDGET_WARN_FRACTION = 0.8;

export interface Health {
  readonly status: 'OK' | 'DEGRADED';
  /** DETERMINISTIC_ONLY 表示全部 LLM 路径已旁路（R25.8）。 */
  readonly mode: 'NORMAL' | 'DETERMINISTIC_ONLY';
  readonly db_ok: boolean;
  readonly llm_mode: string;
  readonly project_usd_spent: number;
  readonly real_run_count: number;
}

export function getHealth(): Promise<Health> {
  return apiFetch<Health>('/health');
}

/** 累计成本是否达预算告警阈值（≥ 80% 的项目美元上限，R25.4）。 */
export function isBudgetWarning(health: Health): boolean {
  return health.project_usd_spent >= PROJECT_USD_CEILING * BUDGET_WARN_FRACTION;
}
