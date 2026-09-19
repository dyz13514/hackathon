import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import type { BottleneckInsights } from '../api/insights';

// mock 隔离网络：本套测试守的是「视图拿到洞察数据后画对了什么」，不测后端。
vi.mock('../api/insights', () => ({ getBottlenecks: vi.fn() }));

import { getBottlenecks } from '../api/insights';
import { Insights } from '../routes/Insights';

const INSIGHTS: BottleneckInsights = {
  active_plan_id: 'PLAN-1',
  machines: [
    {
      machine_id: 'CNC-01',
      machine_type: 'CNC',
      capabilities: ['milling'],
      utilisation: 0.92,
      busy_minutes: 552,
      available_minutes: 600,
      job_count: 14,
      order_value_share: 0.75,
      is_critical: true,
      tardiness_delta_if_plus_20pct: -30,
    },
    {
      machine_id: 'LATHE-01',
      machine_type: 'LATHE',
      capabilities: ['turning'],
      utilisation: 0.3,
      busy_minutes: 180,
      available_minutes: 600,
      job_count: 4,
      order_value_share: 0.1,
      is_critical: false,
      tardiness_delta_if_plus_20pct: 0,
    },
  ],
  skill_gaps: [
    { skill: 'CNC_OPERATION', required_minutes: 600, available_minutes: 480, gap_minutes: 120 },
    { skill: 'TURNING', required_minutes: 180, available_minutes: 480, gap_minutes: -300 },
  ],
};

afterEach(() => {
  vi.clearAllMocks();
});

describe('Insights 视图', () => {
  it('渲染每台机器的利用率、作业数、订单价值占比与 +20% 拖期变化（R15.1/R15.2）', async () => {
    vi.mocked(getBottlenecks).mockResolvedValue(INSIGHTS);
    render(<Insights />);
    await screen.findByRole('heading', { name: /机器产能/ });

    const row = screen.getByRole('row', { name: /CNC-01/ });
    expect(row).toHaveTextContent('92.0%'); // 利用率
    expect(row).toHaveTextContent('75.0%'); // 订单价值占比
    // +20% 工时改善 30 分钟 → 显示「改善」。
    expect(row).toHaveTextContent(/-30（改善）/);
  });

  it('关键机器带「关键」标识，非关键显示「有替代」（R15.3）', async () => {
    vi.mocked(getBottlenecks).mockResolvedValue(INSIGHTS);
    const { container } = render(<Insights />);
    await screen.findByRole('heading', { name: /机器产能/ });
    // 关键机器行有 badge-critical 徽章；非关键行显示「有替代」。
    expect(container.querySelector('.badge-critical')).not.toBeNull();
    expect(screen.getByText(/有替代/)).toBeInTheDocument();
  });

  it('渲染技能缺口，供不应求显示「缺」，富余显示「富余」（R15.4）', async () => {
    vi.mocked(getBottlenecks).mockResolvedValue(INSIGHTS);
    render(<Insights />);
    await screen.findByRole('heading', { name: /技能缺口/ });
    expect(screen.getByText(/缺 120/)).toBeInTheDocument();
    expect(screen.getByText(/富余 300/)).toBeInTheDocument();
  });

  it('无 ACTIVE 计划时显示明确提示（不空白）', async () => {
    vi.mocked(getBottlenecks).mockRejectedValue(
      new ApiError(409, 'NO_ACTIVE_PLAN', '无活动计划'),
    );
    render(<Insights />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/没有 ACTIVE 计划/);
  });

  it('刷新按钮重新拉取', async () => {
    vi.mocked(getBottlenecks).mockResolvedValue(INSIGHTS);
    render(<Insights />);
    await screen.findByRole('heading', { name: /机器产能/ });
    fireEvent.click(screen.getByRole('button', { name: /刷新瓶颈洞察/ }));
    await waitFor(() => expect(getBottlenecks).toHaveBeenCalledTimes(2));
  });

  it('无严重可访问性违规（axe-core）', async () => {
    vi.mocked(getBottlenecks).mockResolvedValue(INSIGHTS);
    const { container } = render(<Insights />);
    await screen.findByRole('heading', { name: /机器产能/ });
    const results = await axe.run(container, {
      rules: { 'color-contrast': { enabled: false } },
    });
    const serious = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(serious).toEqual([]);
  });
});
