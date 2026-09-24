/**
 * 登录弹窗交互回归测试（fix/frontend-interaction-hardening）。
 *
 * 覆盖团队反馈的核心缺陷：
 * - 弹窗必须有可访问名称的 Close / Cancel 入口，Esc 可关闭。
 * - 切换路由时不得残留旧弹窗。
 * - 关闭时清理 password / error / loading 状态。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from '../App';
import * as auth from '../api/auth';

function openLoginModal() {
  // 模拟 client.ts 在收到 401 时派发的事件
  window.dispatchEvent(new CustomEvent('login-required'));
}

describe('LoginModal 关闭 / 取消 / 导航', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('弹窗有可访问名称的 Close 按钮，点击后关闭', async () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    );
    openLoginModal();

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveAttribute('open');

    const closeBtn = screen.getByRole('button', { name: 'Close authentication dialog' });
    fireEvent.click(closeBtn);

    await waitFor(() => expect(dialog).not.toHaveAttribute('open'));
  });

  it('底部 Cancel 按钮可关闭弹窗', async () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    );
    openLoginModal();
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveAttribute('open');

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(dialog).not.toHaveAttribute('open'));
  });

  it('Esc（cancel 事件）可关闭弹窗', async () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    );
    openLoginModal();
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveAttribute('open');

    // 原生 <dialog> 上 Esc 触发 cancel 事件
    fireEvent(dialog, new Event('cancel', { bubbles: false, cancelable: true }));
    await waitFor(() => expect(dialog).not.toHaveAttribute('open'));
  });

  it('点击 backdrop（dialog 本体）关闭', async () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    );
    openLoginModal();
    const dialog = await screen.findByRole('dialog');

    // 点击 dialog 元素本身（backdrop 区域），target === dialog
    fireEvent.click(dialog);
    await waitFor(() => expect(dialog).not.toHaveAttribute('open'));
  });

  it('取消时向 client.ts 派发 login-cancelled 事件', async () => {
    const cancelled = vi.fn();
    window.addEventListener('login-cancelled', cancelled);
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    );
    openLoginModal();
    await screen.findByRole('dialog');

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(cancelled).toHaveBeenCalled());
    window.removeEventListener('login-cancelled', cancelled);
  });

  it('切换路由时不残留旧弹窗，并派发取消信号', async () => {
    const cancelled = vi.fn();
    window.addEventListener('login-cancelled', cancelled);
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    );
    openLoginModal();
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveAttribute('open');

    // 点击主导航切换到另一个视图
    fireEvent.click(screen.getByRole('link', { name: 'Preferences' }));

    await waitFor(() => expect(dialog).not.toHaveAttribute('open'));
    expect(cancelled).toHaveBeenCalled();
    window.removeEventListener('login-cancelled', cancelled);
  });

  it('关闭后重新打开时 password / error 状态已清理', async () => {
    // 让登录失败以产生 error，并留下已输入的 password
    vi.spyOn(auth, 'login').mockRejectedValue(
      Object.assign(new Error('bad'), { name: 'ApiError', status: 401, code: 'UNAUTHENTICATED' }),
    );
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    );
    openLoginModal();
    await screen.findByRole('dialog');

    const input = screen.getByLabelText('Access passphrase') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'wrong-pass' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    // 出现错误提示
    await screen.findByRole('alert');

    // 关闭再打开
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() =>
      expect(screen.getByRole('dialog', { hidden: true })).not.toHaveAttribute('open'),
    );
    openLoginModal();
    const reopened = await screen.findByRole('dialog');
    await waitFor(() => expect(reopened).toHaveAttribute('open'));

    // 重新打开后弹窗内 error 已清除、password 为空
    // （Dashboard 视图本身可能有自己的 alert，故限定在 dialog 范围内查询）
    expect(reopened.querySelector('.login-modal-error')).toBeNull();
    expect((screen.getByLabelText('Access passphrase') as HTMLInputElement).value).toBe('');
  });
});
