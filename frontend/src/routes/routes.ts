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
  { path: '/', label: '状态看板', task: '3.7', implemented: true },
  { path: '/schedule', label: '排产甘特图', task: '2.12', implemented: true },
  { path: '/approval', label: '审批', task: '3.7', implemented: true },
  { path: '/traces', label: 'Trace 查看器', task: '5.12', implemented: true },
  { path: '/import', label: '摄取与映射确认', task: '10.5', implemented: false },
  { path: '/risks', label: '风险面板', task: '8.x', implemented: false },
  { path: '/whatif', label: 'What-if', task: '9.x', implemented: false },
  { path: '/preferences', label: '偏好规则管理', task: '11.x', implemented: false },
  { path: '/value', label: '价值台账', task: '7.6', implemented: true },
  { path: '/quote', label: '交期报价 (P1)', task: '13.x', implemented: false },
  { path: '/insights', label: '瓶颈与产能 (P1)', task: '13.x', implemented: false },
];
