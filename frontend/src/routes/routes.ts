/**
 * 视图清单，逐条对应 design.md Components §6 的表。
 *
 * `implemented` 为 false 的项不进导航——半个链接指向空白页在演示中比没有链接更糟。
 * 各项由其 `task` 标注的任务落地。
 *
 * 参数化的**上下文视图**（从某条计划进入、无静态顶栏链接）不进本表，只在 `App.tsx` 注册路由：
 *   - `/plans/:a/compare/:b` 方案对比（任务 7.5，design.md §6）——从审批/看板里的某个提案进入。
 */
export interface RouteDescriptor {
  readonly path: string;
  readonly label: string;
  /** 落地任务编号（tasks.md） */
  readonly task: string;
  readonly implemented: boolean;
}

export const ROUTES: readonly RouteDescriptor[] = [
  { path: '/', label: 'Dashboard', task: '3.7', implemented: true },
  { path: '/schedule', label: 'Schedule', task: '2.12', implemented: true },
  { path: '/approval', label: 'Approval', task: '3.7', implemented: true },
  { path: '/traces', label: 'Trace Viewer', task: '5.12', implemented: true },
  { path: '/import', label: 'Import & Mapping', task: '10.5', implemented: true },
  { path: '/risks', label: 'Risks', task: '8.5', implemented: true },
  { path: '/whatif', label: 'What-if', task: '8.3', implemented: true },
  { path: '/preferences', label: 'Preferences', task: '11.1', implemented: true },
  { path: '/value', label: 'Value Ledger', task: '7.6', implemented: true },
  { path: '/quote', label: 'Quote', task: '13.6', implemented: true },
  { path: '/insights', label: 'Insights', task: '13.5', implemented: true },
];
