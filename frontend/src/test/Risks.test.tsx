import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { RiskList } from '../api/risks';

// mock 隔离网络：本套测试守的是「视图拿到风险数据后画对了什么」，不测后端。
vi.mock('../api/risks', async () => {
  const actual = await vi.importActual<typeof import('../api/risks')>('../api/risks');
  return { ...actual, getRisks: vi.fn(), scanRisks: vi.fn() };
});

import { getRisks, scanRisks } from '../api/risks';
import { Risks } from '../routes/Risks';

const RISKS: RiskList = {
  critical_count: 1,
  warning_count: 1,
  info_count: 1,
  findings: [
    {
      finding_id: 'RF-crit',
      risk_type: 'ZERO_SLACK_ORDER',
      severity: 'CRITICAL',
      entity_type: 'ORDER',
      entity_id: 'ORD-1',
      metric_value: 0,
      threshold_value: 120,
      affected_order_ids: ['ORD-1'],
      narrative: '订单零余量，需立即处理。',
      narrative_source: 'TEMPLATE',
      first_seen_at: '2026-01-01T00:00:00Z',
      last_seen_at: '2026-01-01T00:00:00Z',
      mitigation_plan_id: 'PLAN-mit',
    },
    {
      finding_id: 'RF-warn',
      risk_type: 'MATERIAL_RUNOUT_FORECAST',
      severity: 'WARNING',
      entity_type: 'MATERIAL',
      entity_id: 'MAT-9',
      metric_value: 30,
      threshold_value: 24,
      affected_order_ids: ['ORD-2'],
      narrative: '物料将耗尽。',
      narrative_source: 'TEMPLATE',
      first_seen_at: '2026-01-01T00:00:00Z',
      last_seen_at: '2026-01-01T00:00:00Z',
      mitigation_plan_id: null,
    },
    {
      finding_id: 'RF-info',
      risk_type: 'BOTTLENECK_RESOURCE',
      severity: 'INFO',
      entity_type: 'RESOURCE',
      entity_id: 'RES-3',
      metric_value: 0.91,
      threshold_value: 0.9,
      affected_order_ids: [],
      narrative: '资源利用率偏高。',
      narrative_source: 'TEMPLATE',
      first_seen_at: '2026-01-01T00:00:00Z',
      last_seen_at: '2026-01-01T00:00:00Z',
      mitigation_plan_id: null,
    },
  ],
};

afterEach(() => {
  vi.clearAllMocks();
});

describe('Risks 视图', () => {
  it('按严重度分组渲染三档计数与分组标题（R14.6）', async () => {
    vi.mocked(getRisks).mockResolvedValue(RISKS);
    render(<Risks />);

    const summary = await screen.findByLabelText('风险计数');
    // 三档计数各为 1（severity/warning/info），因此顶栏应有恰好三个 <strong>1</strong>。
    // 用 getAllByText 精确断言三项，避免 getByText 命中多元素而抛「found multiple」。
    expect(within(summary).getAllByText('1', { selector: 'strong' })).toHaveLength(3);
    expect(screen.getByRole('heading', { name: /严重（1）/ })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /警告（1）/ })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /提示（1）/ })).toBeInTheDocument();
  });

  it('CRITICAL 显示缓解提案入口，INFO/WARNING 无入口（R14.6–7）', async () => {
    vi.mocked(getRisks).mockResolvedValue(RISKS);
    render(<Risks />);
    await screen.findByRole('heading', { name: /严重（1）/ });

    const link = screen.getByRole('link', { name: /查看缓解提案/ });
    expect(link).toHaveAttribute('href', '/plans/PLAN-mit');
    // 只有一个缓解入口（CRITICAL 那条），WARNING/INFO 不生成
    expect(screen.getAllByRole('link', { name: /查看缓解提案/ })).toHaveLength(1);
  });

  it('渲染叙述来源徽章（TEMPLATE）以区分 P0 模板与 P1 LLM（R14.11）', async () => {
    vi.mocked(getRisks).mockResolvedValue(RISKS);
    render(<Risks />);
    await screen.findByRole('heading', { name: /严重（1）/ });

    expect(screen.getAllByText('TEMPLATE').length).toBeGreaterThanOrEqual(1);
  });

  it('LLM 归因叙述以独立徽章区分于 TEMPLATE（R14.11，任务 13.2）', async () => {
    // 一条 LLM 叙述 + 一条模板叙述：两种来源徽章都应出现且可区分。
    const [crit, warn] = RISKS.findings;
    const withLlm: RiskList = {
      ...RISKS,
      findings: [
        { ...crit!, narrative_source: 'LLM', narrative: '[LLM] CNC-01 归因叙述。' },
        warn!,
      ],
    };
    vi.mocked(getRisks).mockResolvedValue(withLlm);
    render(<Risks />);
    await screen.findByRole('heading', { name: /严重（1）/ });

    const llmBadge = screen.getByText('LLM');
    expect(llmBadge).toHaveClass('source-LLM');
    expect(screen.getByText('TEMPLATE')).toHaveClass('source-TEMPLATE');
    // 叙述来源可访问标注区分两类。
    expect(screen.getByLabelText('叙述来源：LLM')).toBeInTheDocument();
  });

  it('「重新扫描」触发 POST 后回读（R14.1）', async () => {
    vi.mocked(getRisks).mockResolvedValue(RISKS);
    vi.mocked(scanRisks).mockResolvedValue({
      finding_count: 3,
      inserted: 0,
      updated: 3,
      findings: [],
    });
    render(<Risks />);
    await screen.findByRole('heading', { name: /严重（1）/ });

    fireEvent.click(screen.getByRole('button', { name: /重新扫描风险/ }));
    await waitFor(() => expect(scanRisks).toHaveBeenCalledTimes(1));
    // 扫描后回读一次 getRisks（初始 1 次 + 扫描后 1 次）
    await waitFor(() => expect(getRisks).toHaveBeenCalledTimes(2));
  });

  it('无风险时显示诚实空态而不是空白', async () => {
    vi.mocked(getRisks).mockResolvedValue({
      findings: [],
      critical_count: 0,
      warning_count: 0,
      info_count: 0,
    });
    render(<Risks />);
    expect(await screen.findByText(/当前无风险发现/)).toBeInTheDocument();
  });

  it('后端不可用时显示错误而不是空白', async () => {
    vi.mocked(getRisks).mockRejectedValue(new Error('boom'));
    render(<Risks />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('无严重可访问性违规（axe-core）', async () => {
    vi.mocked(getRisks).mockResolvedValue(RISKS);
    const { container } = render(<Risks />);
    await screen.findByRole('heading', { name: /严重（1）/ });

    const results = await axe.run(container, {
      rules: {
        'color-contrast': { enabled: false },
      },
    });
    const serious = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(serious).toEqual([]);
  });
});
