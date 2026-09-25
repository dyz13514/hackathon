/**
 * 全局顶栏横幅（任务 11.6，R25.8/R25.9、R25.4）。
 *
 * 全局提示跨所有视图可见（design.md §2.6）：降级模式明确区分确定性核心与
 * 暂不可用的模型能力；STUB/REPLAY 标为离线输出；累计成本达项目上限 80% 时告警。
 *
 * 轮询 `GET /api/health`（只读、无认证），初次加载即拉一次，之后每 30s 刷新一次——降级是
 * 运行期状态（手动开关或 Bedrock 连续失败触发），顶栏因此要能在不刷新页面时更新。
 *
 * 可访问性（R27.9）：两条横幅都用 `role="alert"`（降级）/`role="status"`（预算），图标 + 文字
 * 传达状态，不仅靠颜色；健康不可达时**静默**（不把"拿不到 health"渲染成一条吓人的错误横幅）。
 */

import { useCallback, useEffect, useState } from 'react';

import {
  type AutoAppliedChange,
  listAutoAppliedChanges,
  revertAutoAppliedChange,
} from '../api/autonomy';
import { ApiError } from '../api/client';
import { getHealth, isBudgetWarning, PROJECT_USD_CEILING, type Health } from '../api/health';

/** 轮询间隔（毫秒）。降级是运行期状态，顶栏需在不刷新页面时更新。 */
const POLL_INTERVAL_MS = 30_000;

export function TopBar() {
  const [health, setHealth] = useState<Health | null>(null);
  // 任务 13.4：L4 自动应用记录 + 一键回滚入口（R13.9）。未回滚的记录在顶栏通知区呈现。
  const [changes, setChanges] = useState<readonly AutoAppliedChange[]>([]);
  const [revertBusy, setRevertBusy] = useState<string | null>(null);
  const [revertError, setRevertError] = useState<string | null>(null);

  const loadChanges = useCallback(async () => {
    try {
      const result = await listAutoAppliedChanges();
      setChanges(result.changes);
    } catch {
      // 拿不到时静默（顶栏是提示）。未登录的 GET 也可能失败——不喧宾夺主。
      setChanges([]);
    }
  }, []);

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
      if (!cancelled) {
        await loadChanges();
      }
    };

    void poll();
    const timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [loadChanges]);

  const onRevert = useCallback(
    async (changeId: string) => {
      setRevertBusy(changeId);
      setRevertError(null);
      try {
        await revertAutoAppliedChange(changeId);
        await loadChanges();
      } catch (err) {
        setRevertError(
          err instanceof ApiError
            ? `Rollback failed (${err.code}): ${err.message}`
            : 'Rollback failed: backend service unavailable.',
        );
      } finally {
        setRevertBusy(null);
      }
    },
    [loadChanges],
  );

  const activeChanges = changes.filter((c) => !c.reverted);
  const degraded = health?.mode === 'DETERMINISTIC_ONLY';
  const offlineModel = health?.llm_mode === 'STUB' || health?.llm_mode === 'REPLAY';
  const budgetWarning = health !== null && isBudgetWarning(health);

  if (!degraded && !offlineModel && !budgetWarning && activeChanges.length === 0 && revertError === null) {
    return null;
  }

  return (
    <div className="app-banners">
      {degraded && (
        <div role="alert" className="banner banner-degraded">
          <strong>Degraded mode (DETERMINISTIC_ONLY)</strong>
          : deterministic planning, validation, approval, risk metrics and structured What-if remain
          available. LLM-dependent mapping, explanations and natural-language What-if may fail; retry
          those actions after model access is restored.
        </div>
      )}
      {offlineModel && (
        <div role="status" className="banner banner-offline-model">
          <strong>LLM mode: {health?.llm_mode}</strong>
          : model output is offline test/recording data, not a live API result.
        </div>
      )}
      {budgetWarning && health !== null && (
        <div role="status" className="banner banner-budget">
          <strong>Budget warning</strong>
          : cumulative estimated cost ~ USD {health.project_usd_spent.toFixed(2)}, now above 80% of the
          USD {PROJECT_USD_CEILING} ceiling.
        </div>
      )}
      {activeChanges.map((change) => (
        <div
          key={change.change_id}
          role="status"
          className="banner banner-auto-applied"
          data-change-id={change.change_id}
        >
          <strong>Auto-applied</strong>
          : the system automatically applied a low-impact change ({change.plan_id_before} -&gt;{' '}
          {change.plan_id_after}).
          <button
            type="button"
            onClick={() => void onRevert(change.change_id)}
            disabled={revertBusy === change.change_id}
            aria-label={`Roll back auto-applied change ${change.change_id}`}
          >
            {revertBusy === change.change_id ? 'Rolling back…' : 'Roll back'}
          </button>
        </div>
      ))}
      {revertError && (
        <div role="alert" className="banner banner-degraded">
          {revertError}
        </div>
      )}
    </div>
  );
}
