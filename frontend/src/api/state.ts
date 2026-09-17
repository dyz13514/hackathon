/**
 * 状态看板端点的类型化客户端（design.md Components §6 `/` 行，任务 3.7）。
 *
 * 形状逐字对应后端 `app/api/state.py` 的 `DashboardOut`。手写而非 OpenAPI 生成，理由同
 * `plans.ts`：本任务只落这一个端点，一份紧贴后端契约的手写类型比引入代码生成链更轻。
 */

import { apiFetch } from './client';

/** 来源枚举（R1.3）。计划的 `source` 复用 `origin`，因此是开放字符串而非严格联合。 */
export type RecordSource = 'SPREADSHEET_IMPORT' | 'MANUAL_ENTRY' | 'SEED_DATA' | string;

export interface OrderState {
  readonly order_id: string;
  readonly product_id: string;
  readonly quantity: number;
  readonly due_date: string;
  readonly priority: string;
  /** 不受信任（R23.1）。前端渲染为纯文本 + `untrusted` 徽章，不解释任何指令语义。 */
  readonly notes: string | null;
  /** 由后端 Guardrail 置位；为 true 时额外提示疑似注入。 */
  readonly injection_suspected: boolean;
  readonly source: RecordSource;
  readonly last_updated_at: string;
}

export interface MaterialState {
  readonly material_id: string;
  readonly name: string;
  readonly unit: string;
  readonly quantity_available: number;
  readonly reserved_quantity: number;
  readonly source: RecordSource;
  readonly last_updated_at: string;
}

export interface MachineState {
  readonly machine_id: string;
  readonly machine_type: string;
  readonly status: string;
  readonly source: RecordSource;
  readonly last_updated_at: string;
}

export interface WorkerState {
  readonly worker_id: string;
  readonly name: string;
  readonly source: RecordSource;
  readonly last_updated_at: string;
}

export interface PlanState {
  readonly plan_id: string;
  readonly production_date: string;
  readonly status: string;
  readonly feasibility: string;
  readonly plan_version: number;
  readonly source: RecordSource;
  readonly last_updated_at: string;
}

export interface Dashboard {
  readonly orders: readonly OrderState[];
  readonly materials: readonly MaterialState[];
  readonly machines: readonly MachineState[];
  readonly workers: readonly WorkerState[];
  readonly plans: readonly PlanState[];
}

/** 五类实体的当前状态（R1.1–R1.5）。只读端点，无需认证。 */
export function getDashboard(): Promise<Dashboard> {
  return apiFetch<Dashboard>('/state/dashboard');
}
