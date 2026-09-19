/**
 * 全局顶栏横幅（任务 11.6，R25.8/R25.9、R25.4）。
 *
 * 两条全局提示，跨所有视图可见（design.md §2.6：降级模式必须**显著**提示）：
 * 1. **降级模式横幅**——`GET /api/health` 的 `mode === 'DETERMINISTIC_ONLY'` 时显示：所有 LLM
 *    路径已旁路，计划生成 / 校验 / 审批 / 重排 / 风险扫描 / What-if / 台账 / 偏好规则照常，
 *    仅 LLM 列映射改用手工列映射（R25.10）。
 * 2. **预算告警**——累计成本达项目上限的 80% 时显示（R25.4）。
 *
 * 轮询 `GET /api/health`（只读、无认证），初次加载即拉一次，之后每 30s 刷新一次——降级是
 * 运行期状态（手动开关或 Bedrock 连续失败触发），顶栏因此要能在不刷新页面时更新。
 *
 * 可访问性（R27.9）：两条横幅都用 `role="alert"`（降级）/`role="status"`（预算），图标 + 文字
 * 传达状态，不仅靠颜色；健康不可达时**静默**（不把"拿不到 health"渲染成一条吓人的错误横幅）。
 */

import { useEffect, useState } from 'react';

import { getHealth, isBudgetWarning, PROJECT_USD_CEILING, type Health } from '../api/health';

/** 轮询间隔（毫秒）。降级是运行期状态，顶栏需在不刷新页面时更新。 */
const POLL_INTERVAL_MS = 30_000;

export function TopBar() {
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const result = await getHealth();
        if (!cancelled) {
          setHealth(result);
        }
      } catch {
        // health 拿不到时静默：顶栏是提示，不该在后端短暂不可达时喧宾夺主。
        if (!cancelled) {
          setHealth(null);
        }
      }
    };

    void poll();
    const timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  if (health === null) {
    return null;
  }

  const degraded = health.mode === 'DETERMINISTIC_ONLY';
  const budgetWarning = isBudgetWarning(health);

  if (!degraded && !budgetWarning) {
    return null;
  }

  return (
    <div className="app-banners">
      {degraded && (
        <div role="alert" className="banner banner-degraded">
          <span aria-hidden="true">⚠ </span>
          <strong>降级模式（DETERMINISTIC_ONLY）</strong>
          ：LLM 路径已旁路，计划生成、校验、审批、重排、风险扫描、What-if、价值台账与偏好规则照常；
          仅电子表格 LLM 列映射不可用，请改用手工列映射。
        </div>
      )}
      {budgetWarning && (
        <div role="status" className="banner banner-budget">
          <span aria-hidden="true">💰 </span>
          <strong>预算告警</strong>
          ：累计估算成本 ≈ USD {health.project_usd_spent.toFixed(2)}，已达上限 USD{' '}
          {PROJECT_USD_CEILING} 的 80% 以上。
        </div>
      )}
    </div>
  );
}
