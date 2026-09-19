/**
 * 瓶颈与产能洞察端点的类型化客户端（design.md Components §6 `/insights`，任务 13.5，R15）。
 *
 * 形状逐字对应后端 `app/api/insights.py`。全部数值确定性、无 LLM；「+20% 工时的拖期变化量」由
 * 后端沙箱实算（R15.2）。只读端点。
 */

import { apiFetch } from './client';

/** 一台机器在当前 ACTIVE 计划中的产能画像（R15.1–R15.3）。 */
export interface MachineInsight {
  readonly machine_id: string;
  readonly machine_type: string;
  readonly capabilities: readonly string[];
  readonly utilisation: number;
  readonly busy_minutes: number;
  readonly available_minutes: number;
  readonly job_count: number;
  readonly order_value_share: number;
  readonly is_critical: boolean;
  /** 可用工时 +20% 时 total_tardiness_minutes 的变化量（R15.2，沙箱实算，负=改善）。 */
  readonly tardiness_delta_if_plus_20pct: number;
}

/** 按 required_worker_skill 聚合的技能缺口（R15.4）。gap>0 表示供不应求。 */
export interface SkillGap {
  readonly skill: string;
  readonly required_minutes: number;
  readonly available_minutes: number;
  readonly gap_minutes: number;
}

export interface BottleneckInsights {
  readonly active_plan_id: string | null;
  readonly machines: readonly MachineInsight[];
  readonly skill_gaps: readonly SkillGap[];
}

/** 拉取当前 ACTIVE 计划的瓶颈与产能洞察。只读。无 ACTIVE 计划时后端返回 409 NO_ACTIVE_PLAN。 */
export function getBottlenecks(): Promise<BottleneckInsights> {
  return apiFetch<BottleneckInsights>('/insights/bottlenecks');
}
