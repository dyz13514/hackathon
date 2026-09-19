import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import axe from 'axe-core';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { PreferenceList, PreferenceRule } from '../api/preferences';

// mock 隔离网络：本套测试守的是「视图拿到规则数据后画对了什么、点了按钮调对了哪个 API」。
vi.mock('../api/preferences', async () => {
  const actual = await vi.importActual<typeof import('../api/preferences')>('../api/preferences');
  return {
    ...actual,
    listPreferences: vi.fn(),
    createPreference: vi.fn(),
    enablePreference: vi.fn(),
    disablePreference: vi.fn(),
    deletePreference: vi.fn(),
    updatePreference: vi.fn(),
    getAffectedJobs: vi.fn(),
  };
});

import {
  createPreference,
  disablePreference,
  enablePreference,
  listPreferences,
} from '../api/preferences';
import { Preferences } from '../routes/Preferences';

const RULE_LOW_EVIDENCE: PreferenceRule = {
  rule_id: 'PR-001',
  human_text: 'ORD-007 不要排 CNC-03',
  structured_form: {
    kind: 'AVOID_MACHINE_FOR_ORDER',
    order_id: 'ORD-007',
    machine_id: 'CNC-03',
    weight_delta: 2,
  },
  kind: 'AVOID_MACHINE_FOR_ORDER',
  enabled: false,
  low_evidence: true,
  created_at: '2026-03-02T08:15:00',
  updated_at: '2026-03-02T08:15:00',
  created_by: 'PLANNER',
  source_decision_ids: ['DEC-1'],
};

const RULE_ENABLED: PreferenceRule = {
  rule_id: 'PR-002',
  human_text: '技能 welding 优先 W-01',
  structured_form: { kind: 'PREFER_WORKER_FOR_SKILL', skill: 'welding', worker_id: 'W-01', weight_delta: 1 },
  kind: 'PREFER_WORKER_FOR_SKILL',
  enabled: true,
  low_evidence: false,
  created_at: '2026-03-02T09:00:00',
  updated_at: '2026-03-02T09:00:00',
  created_by: 'PLANNER',
  source_decision_ids: ['DEC-1', 'DEC-2'],
};

function listWith(items: PreferenceRule[], enabledCount = 0): PreferenceList {
  return { items, total: items.length, enabled_count: enabledCount, max_enabled: 20 };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('Preferences 视图', () => {
  it('列出规则并展示 human_text、来源决策、启用状态', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([RULE_LOW_EVIDENCE, RULE_ENABLED], 1));
    render(<Preferences />);

    expect(await screen.findByText('ORD-007 不要排 CNC-03')).toBeInTheDocument();
    expect(screen.getByText('技能 welding 优先 W-01')).toBeInTheDocument();
    // 来源决策链接（两条规则都引用了 DEC-1，因此用 getAllByText）
    expect(screen.getAllByText('DEC-1').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('DEC-2')).toBeInTheDocument();
    // 启用状态用文字（不仅颜色）
    expect(screen.getByText('已启用')).toBeInTheDocument();
    expect(screen.getByText('未启用')).toBeInTheDocument();
  });

  it('LOW_EVIDENCE 用图标 + 文字提示，不仅靠颜色（R18.10、R27.9）', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([RULE_LOW_EVIDENCE]));
    render(<Preferences />);
    expect(await screen.findByText(/证据不足/)).toBeInTheDocument();
  });

  it('展示已启用 N / 20 上限提示（R18.11）', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([RULE_ENABLED], 20));
    render(<Preferences />);
    const status = await screen.findByText(/已启用 20 \/ 20/);
    expect(status).toBeInTheDocument();
    expect(status.textContent).toMatch(/已达上限/);
  });

  it('创建表单没有「启用」勾选框——创建即未启用（R18.4）', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([]));
    render(<Preferences />);
    await screen.findByRole('heading', { name: '新建规则' });
    // 表单里不应出现任何 name/label 含「启用」的勾选控件
    expect(screen.queryByLabelText(/启用/)).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  });

  it('提交创建表单调用 createPreference 且入参不含 enabled', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([]));
    vi.mocked(createPreference).mockResolvedValue(RULE_LOW_EVIDENCE);
    render(<Preferences />);
    await screen.findByRole('heading', { name: '新建规则' });

    fireEvent.change(screen.getByLabelText(/规则说明/), {
      target: { value: 'ORD-007 避开 CNC-03' },
    });
    fireEvent.change(screen.getByLabelText('订单 ID'), { target: { value: 'ORD-007' } });
    fireEvent.change(screen.getByLabelText('机器 ID'), { target: { value: 'CNC-03' } });
    // jsdom 下点击 submit 按钮不总会触发带 required 字段的原生表单提交，
    // 直接 submit 表单本身（浏览器里点击按钮等价于此），断言真实的提交行为。
    const submitButton = screen.getByRole('button', { name: /创建规则/ });
    fireEvent.submit(submitButton.closest('form') as HTMLFormElement);

    await waitFor(() => expect(createPreference).toHaveBeenCalledTimes(1));
    const arg = vi.mocked(createPreference).mock.calls[0]?.[0];
    expect(arg).toBeDefined();
    expect(arg).not.toHaveProperty('enabled');
    expect(arg?.structured_form.kind).toBe('AVOID_MACHINE_FOR_ORDER');
  });

  it('点「启用」调用 enablePreference（独立动作）', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([RULE_LOW_EVIDENCE]));
    vi.mocked(enablePreference).mockResolvedValue({ ...RULE_LOW_EVIDENCE, enabled: true });
    render(<Preferences />);
    await screen.findByText('ORD-007 不要排 CNC-03');

    fireEvent.click(screen.getByRole('button', { name: /启用规则 PR-001/ }));
    await waitFor(() => expect(enablePreference).toHaveBeenCalledWith('PR-001'));
  });

  it('点已启用规则的「停用」调用 disablePreference', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([RULE_ENABLED], 1));
    vi.mocked(disablePreference).mockResolvedValue({ ...RULE_ENABLED, enabled: false });
    render(<Preferences />);
    await screen.findByText('技能 welding 优先 W-01');

    fireEvent.click(screen.getByRole('button', { name: /停用规则 PR-002/ }));
    await waitFor(() => expect(disablePreference).toHaveBeenCalledWith('PR-002'));
  });

  it('达上限时未启用规则的「启用」按钮被禁用', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([RULE_LOW_EVIDENCE], 20));
    render(<Preferences />);
    await screen.findByText('ORD-007 不要排 CNC-03');
    expect(screen.getByRole('button', { name: /启用规则 PR-001/ })).toBeDisabled();
  });

  it('后端不可用时显示错误而不是空白', async () => {
    vi.mocked(listPreferences).mockRejectedValue(new Error('boom'));
    render(<Preferences />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('无严重可访问性违规（axe-core）', async () => {
    vi.mocked(listPreferences).mockResolvedValue(listWith([RULE_LOW_EVIDENCE, RULE_ENABLED], 1));
    const { container } = render(<Preferences />);
    await screen.findByText('ORD-007 不要排 CNC-03');

    const results = await axe.run(container, {
      rules: { 'color-contrast': { enabled: false } },
    });
    const serious = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    );
    expect(serious).toEqual([]);
  });
});
