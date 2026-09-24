/**
 * 应用级错误边界回归测试（round-2）。
 *
 * 覆盖：渲染错误时显示英文回退页与恢复入口；Try again 可重置；
 * resetKey（路由 pathname）变化后自动脱离错误态。
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ErrorBoundary } from '../components/ErrorBoundary';

function Boom({ crash }: { crash: boolean }): JSX.Element {
  if (crash) throw new Error('kaboom');
  return <p>Recovered content</p>;
}

describe('ErrorBoundary', () => {
  beforeEach(() => {
    // 抑制 React 在错误边界测试里预期内的 console.error 噪声
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('渲染错误时显示英文回退页与恢复入口', () => {
    render(
      <ErrorBoundary resetKey="/a">
        <Boom crash />
      </ErrorBoundary>,
    );
    expect(screen.getByRole('heading', { name: 'Something went wrong' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Go to Dashboard' })).toHaveAttribute('href', '/');
    expect(screen.getByRole('button', { name: 'Reload page' })).toBeInTheDocument();
  });

  it('Try again 重置后可渲染正常内容', () => {
    const { rerender } = render(
      <ErrorBoundary resetKey="/a">
        <Boom crash />
      </ErrorBoundary>,
    );
    expect(screen.getByRole('heading', { name: 'Something went wrong' })).toBeInTheDocument();

    // 点击 Try again 前把子树切到不崩溃版本，模拟瞬时错误已消失
    rerender(
      <ErrorBoundary resetKey="/a">
        <Boom crash={false} />
      </ErrorBoundary>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(screen.getByText('Recovered content')).toBeInTheDocument();
  });

  it('resetKey（路由）变化后自动脱离错误态', () => {
    const { rerender } = render(
      <ErrorBoundary resetKey="/a">
        <Boom crash />
      </ErrorBoundary>,
    );
    expect(screen.getByRole('heading', { name: 'Something went wrong' })).toBeInTheDocument();

    // 导航到新路由：resetKey 变化且子树不再崩溃
    rerender(
      <ErrorBoundary resetKey="/b">
        <Boom crash={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText('Recovered content')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Something went wrong' })).not.toBeInTheDocument();
  });
});
