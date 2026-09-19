import { render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { Health } from '../api/health';

// mock 隔离网络：本套测试守的是「顶栏拿到 health 后画对了什么横幅」，不测后端。
vi.mock('../api/health', async () => {
  const actual = await vi.importActual<typeof import('../api/health')>('../api/health');
  return { ...actual, getHealth: vi.fn() };
});

import { getHealth } from '../api/health';
import { TopBar } from '../components/TopBar';

const NORMAL: Health = {
  status: 'OK',
  mode: 'NORMAL',
  db_ok: true,
  llm_mode: 'STUB',
  project_usd_spent: 1.0,
  real_run_count: 2,
};

const DEGRADED: Health = { ...NORMAL, mode: 'DETERMINISTIC_ONLY', llm_mode: 'DISABLED' };

// 累计成本达 PROJECT_USD_CEILING(35) 的 80% = 28。
const BUDGET_WARN: Health = { ...NORMAL, project_usd_spent: 30 };

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
    expect(banner.textContent).toMatch(/降级模式/);
    expect(banner.textContent).toMatch(/手工列映射/);
  });

  it('累计成本达 80% 上限时显示预算告警（R25.4）', async () => {
    vi.mocked(getHealth).mockResolvedValue(BUDGET_WARN);
    render(<TopBar />);
    const status = await screen.findByRole('status');
    expect(status.textContent).toMatch(/预算告警/);
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
});
