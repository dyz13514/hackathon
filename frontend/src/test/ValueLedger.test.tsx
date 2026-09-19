import { render, screen, within } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ValueLedger as ValueLedgerData } from '../api/valueLedger';

// mock 隔离网络：本套测试守的是「视图拿到台账数据后画对了什么」，不测后端。
vi.mock('../api/valueLedger', async () => {
  const actual = await vi.importActual<typeof import('../api/valueLedger')>('../api/valueLedger');
  return { ...actual, getValueLedger: vi.fn() };
});

import { getValueLedger } from '../api/valueLedger';
import { ValueLedger } from '../routes/ValueLedger';

const LEDGER: ValueLedgerData = {
  auto_handled_count: 3,
  escalated_count: 1,
  total_decisions: 4,
  auto_handled_ratio: 0.75,
  decisions: [
    {
      assessment_id: 'IA-001',
      candidate_plan_id: 'PLAN-rev1',
      impact_class: 'IMPACT_MODERATE',
      autonomy_level: 'L3',
      execution_path: 'PROPOSED',
      decisive_predicates: ['changed_job_count=1 <= 2', 'churn_ratio=0.10 <= 0.20'],
    },
    {
      assessment_id: 'IA-002',
      candidate_plan_id: 'PLAN-rev2',
      impact_class: 'IMPACT_MAJOR',
      autonomy_level: 'L5',
      execution_path: 'ESCALATED',
      decisive_predicates: ['touches_high_priority=true'],
    },
  ],
  active_plan_id: 'PLAN-active',
  on_time_rate: 0.9,
  baseline_on_time_rate: 0.6,
  total_tardiness_minutes: 47,
  baseline_total_tardiness_minutes: 210,
};

afterEach(() => {
  vi.clearAllMocks();
});

describe('ValueLedger 视图', () => {
  it('渲染自主 vs 上报计数与占比（K-14）', async () => {
    vi.mocked(getValueLedger).mockResolvedValue(LEDGER);
    render(<ValueLedger />);

    const ratioSection = (await screen.findByRole('heading', { name: /自主处理 vs 上报人工/ }))
      .closest('section') as HTMLElement;
    expect(within(ratioSection).getByText('3')).toBeInTheDocument(); // auto_handled
    expect(within(ratioSection).getByText('1')).toBeInTheDocument(); // escalated
    expect(within(ratioSection).getByText('75.0%')).toBeInTheDocument(); // ratio
  });

  it('逐行列出每次判定的决定性判据（R13.12），含执行路径中文标签', async () => {
    vi.mocked(getValueLedger).mockResolvedValue(LEDGER);
    render(<ValueLedger />);
    await screen.findByRole('heading', { name: /每次判定的决定性判据/ });

    // 执行路径标签除颜色外带文字
    expect(screen.getByText('自主提案')).toBeInTheDocument();
    expect(screen.getByText('上报人工')).toBeInTheDocument();
    // 判据逐条可见
    expect(screen.getByText('changed_job_count=1 <= 2')).toBeInTheDocument();
    expect(screen.getByText('touches_high_priority=true')).toBeInTheDocument();
    // 影响等级与自主等级可见
    expect(screen.getByText('IMPACT_MAJOR')).toBeInTheDocument();
    expect(screen.getByText('L5')).toBeInTheDocument();
  });

  it('无裁决时显示诚实空态而不是空白', async () => {
    vi.mocked(getValueLedger).mockResolvedValue({
      auto_handled_count: 0,
      escalated_count: 0,
      total_decisions: 0,
      auto_handled_ratio: 0,
      decisions: [],
      active_plan_id: null,
      on_time_rate: null,
      baseline_on_time_rate: null,
      total_tardiness_minutes: null,
      baseline_total_tardiness_minutes: null,
    });
    render(<ValueLedger />);
    expect(await screen.findByText(/尚无影响分级裁决/)).toBeInTheDocument();
  });

  it('后端不可用时显示错误而不是空白', async () => {
    vi.mocked(getValueLedger).mockRejectedValue(new Error('boom'));
    render(<ValueLedger />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('无严重可访问性违规（axe-core）', async () => {
    vi.mocked(getValueLedger).mockResolvedValue(LEDGER);
    const { container } = render(<ValueLedger />);
    await screen.findByRole('heading', { name: /每次判定的决定性判据/ });

    const results = await axe.run(container, {
      rules: {
        // jsdom 不渲染真实布局，颜色对比类规则会误报，关掉这一类；结构性规则保留。
        'color-contrast': { enabled: false },
      },
    });
    const serious = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(serious).toEqual([]);
  });
});
