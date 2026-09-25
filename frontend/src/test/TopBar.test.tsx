import { render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AutoAppliedChange } from '../api/autonomy';
import type { Health } from '../api/health';

// mock 隔离网络：本套测试守的是「顶栏拿到 health / 自动应用记录后画对了什么横幅」，不测后端。
vi.mock('../api/health', async () => {
  const actual = await vi.importActual<typeof import('../api/health')>('../api/health');
  return { ...actual, getHealth: vi.fn() };
});
vi.mock('../api/autonomy', () => ({
  listAutoAppliedChanges: vi.fn(),
  revertAutoAppliedChange: vi.fn(),
}));

import { listAutoAppliedChanges, revertAutoAppliedChange } from '../api/autonomy';
import { getHealth } from '../api/health';
import { TopBar } from '../components/TopBar';

const NO_CHANGES = { changes: [] as AutoAppliedChange[] };

const ONE_CHANGE: { changes: AutoAppliedChange[] } = {
  changes: [
    {
      change_id: 'AAC-1',
      assessment_id: 'IA-1',
      plan_id_before: 'PLAN-before',
      plan_id_after: 'PLAN-after',
      applied_at: '2026-03-02T08:00:00',
      reverted: false,
      reverted_at: null,
      revert_plan_id: null,
    },
  ],
};

const NORMAL: Health = {
  status: 'OK',
  mode: 'NORMAL',
  db_ok: true,
  llm_mode: 'LIVE',
  project_usd_spent: 1.0,
  real_run_count: 2,
};

const DEGRADED: Health = { ...NORMAL, mode: 'DETERMINISTIC_ONLY', llm_mode: 'DISABLED' };

// 累计成本达 PROJECT_USD_CEILING(35) 的 80% = 28。
const BUDGET_WARN: Health = { ...NORMAL, project_usd_spent: 30 };

beforeEach(() => {
  // 默认无自动应用记录；个别用例覆盖。
  vi.mocked(listAutoAppliedChanges).mockResolvedValue(NO_CHANGES);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('TopBar 全局横幅', () => {
  it('NORMAL 且预算未告警时不渲染任何横幅', async () => {
    vi.mocked(getHealth).mockResolvedValue(NORMAL);
    const { container } = render(<TopBar />);
    // 给轮询的初次 fetch 一点时间落定，然后断言无横幅。
    await waitFor(() => expect(getHealth).toHaveBeenCalled());
    expect(container.querySelector('.banner')).toBeNull();
  });

  it('降级模式显示横幅（图标 + 文字，R25.8/R25.9/R27.9）', async () => {
    vi.mocked(getHealth).mockResolvedValue(DEGRADED);
    render(<TopBar />);
    const banner = await screen.findByRole('alert');
    expect(banner.textContent).toMatch(/Degraded mode/);
    expect(banner.textContent).toMatch(/retry those actions after model access is restored/);
  });

  it('离线模型模式明确提示不是 LIVE API 结果', async () => {
    vi.mocked(getHealth).mockResolvedValue({ ...NORMAL, llm_mode: 'STUB' });
    render(<TopBar />);
    expect(await screen.findByText(/model output is offline test\/recording data/)).toBeInTheDocument();
  });

  it('累计成本达 80% 上限时显示预算告警（R25.4）', async () => {
    vi.mocked(getHealth).mockResolvedValue(BUDGET_WARN);
    render(<TopBar />);
    const status = await screen.findByRole('status');
    expect(status.textContent).toMatch(/Budget warning/);
  });

  it('health 不可达时静默（不渲染吓人的错误横幅）', async () => {
    vi.mocked(getHealth).mockRejectedValue(new Error('boom'));
    const { container } = render(<TopBar />);
    await waitFor(() => expect(getHealth).toHaveBeenCalled());
    expect(container.querySelector('.banner')).toBeNull();
  });

  it('无严重可访问性违规（axe-core）', async () => {
    vi.mocked(getHealth).mockResolvedValue(DEGRADED);
    const { container } = render(<TopBar />);
    await screen.findByRole('alert');
    const results = await axe.run(container, {
      rules: { 'color-contrast': { enabled: false } },
    });
    const serious = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(serious).toEqual([]);
  });

  // ---- 任务 13.4：L4 自动应用通知 + 一键回滚 ----

  it('有未回滚的自动应用记录时显示通知与一键回滚入口（R13.9）', async () => {
    vi.mocked(getHealth).mockResolvedValue(NORMAL);
    vi.mocked(listAutoAppliedChanges).mockResolvedValue(ONE_CHANGE);
    render(<TopBar />);
    const btn = await screen.findByRole('button', { name: /Roll back auto-applied change AAC-1/ });
    expect(btn).toBeInTheDocument();
    // 通知横幅含变更前后计划句柄。
    expect(screen.getByText(/PLAN-before/)).toBeInTheDocument();
  });

  it('点「一键回滚」调用 revert 后刷新列表（R13.10）', async () => {
    vi.mocked(getHealth).mockResolvedValue(NORMAL);
    // 首次返回一条；回滚后再拉返回空。
    vi.mocked(listAutoAppliedChanges)
      .mockResolvedValueOnce(ONE_CHANGE)
      .mockResolvedValue(NO_CHANGES);
    vi.mocked(revertAutoAppliedChange).mockResolvedValue({
      change_id: 'AAC-1',
      revert_plan_id: 'PLAN-revert',
      superseded_plan_id: 'PLAN-after',
      status: 'OK',
    });
    render(<TopBar />);
    const btn = await screen.findByRole('button', { name: /Roll back auto-applied change AAC-1/ });
    btn.click();
    await waitFor(() => expect(revertAutoAppliedChange).toHaveBeenCalledWith('AAC-1'));
  });

  it('已回滚的记录不再显示通知', async () => {
    vi.mocked(getHealth).mockResolvedValue(NORMAL);
    vi.mocked(listAutoAppliedChanges).mockResolvedValue({
      changes: [{ ...ONE_CHANGE.changes[0]!, reverted: true, revert_plan_id: 'PLAN-r' }],
    });
    const { container } = render(<TopBar />);
    await waitFor(() => expect(listAutoAppliedChanges).toHaveBeenCalled());
    expect(container.querySelector('.banner-auto-applied')).toBeNull();
  });
});
