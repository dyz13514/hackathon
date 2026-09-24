/**
 * 全局登录弹窗（HttpOnly Cookie 会话认证，R23.12）。
 *
 * 触发时机：任意 API 请求收到 401 UNAUTHENTICATED 时，由 App 层通过
 * `login-required` 自定义事件弹出此窗口。
 *
 * 关闭路径（可返回）：
 * - 头部的 Close 按钮（带可访问名称）。
 * - 底部的 Cancel 按钮。
 * - 键盘 Esc（原生 <dialog> 的 cancel 事件）。
 * - 点击 backdrop（遮罩空白处）——登录是非破坏性操作，允许点背景关闭。
 * 以上任一路径都会调用 `onCancel`，由上层向 client.ts 派发取消信号，
 * 让所有等待中的 401 请求及时结束。
 *
 * 登录成功后：
 * 1. Cookie 由后端通过 Set-Cookie 写入（HttpOnly，前端不可读取）。
 * 2. 上层派发 `login-succeeded` 自定义事件，通知原始请求方重试。
 * 3. 弹窗关闭。
 *
 * 状态清理：关闭时清空 password / error / loading，并把焦点恢复到打开弹窗前
 * 的元素（若已不存在则回退到 document.body）。
 *
 * 可访问性（R27.9）：
 * - 使用原生 <dialog> 元素，浏览器自动管理焦点陷阱与 aria-modal。
 * - 错误信息用 role="alert" 即时播报。
 * - 表单 submit 可键盘完成，不依赖鼠标点击。
 */

import { useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/client';
import { login } from '../api/auth';

interface LoginModalProps {
  /** 是否显示弹窗 */
  open: boolean;
  /** 登录成功回调 */
  onSuccess: () => void;
  /** 用户取消 / 关闭弹窗回调（Close / Cancel / Esc / backdrop） */
  onCancel: () => void;
}

export function LoginModal({ open, onSuccess, onCancel }: LoginModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  /** 打开弹窗前的焦点元素，用于关闭后恢复焦点。 */
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 同步 open 状态到原生 <dialog>
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) {
      // 记录打开前焦点，便于关闭时恢复
      previouslyFocused.current = (document.activeElement as HTMLElement) ?? null;
      if (!dialog.open) {
        // jsdom 不实现 showModal；回退到 show 以保证测试环境可用
        if (typeof dialog.showModal === 'function') {
          dialog.showModal();
        } else {
          dialog.show();
        }
      }
      // 打开时清空上次状态，聚焦输入框
      setPassword('');
      setError(null);
      setLoading(false);
      setTimeout(() => inputRef.current?.focus(), 0);
    } else {
      if (dialog.open) {
        dialog.close();
      }
      // 关闭时清理状态，恢复焦点
      setPassword('');
      setError(null);
      setLoading(false);
      const target = previouslyFocused.current;
      previouslyFocused.current = null;
      if (target && document.contains(target)) {
        target.focus();
      } else if (typeof document.body?.focus === 'function') {
        document.body.focus();
      }
    }
  }, [open]);

  const handleCancel = () => {
    if (loading) return; // 登录进行中不允许取消，避免中途状态错乱
    onCancel();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password.trim()) return;

    setLoading(true);
    setError(null);
    try {
      await login(password);
      onSuccess();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.status === 401
            ? 'Incorrect passphrase. Please try again.'
            : `Login failed (${err.code}): ${err.message}`
          : 'Network or service unavailable. Please try again later.';
      setError(message);
      setPassword('');
      setTimeout(() => inputRef.current?.focus(), 0);
    } finally {
      setLoading(false);
    }
  };

  return (
    <dialog
      ref={dialogRef}
      className="login-modal"
      aria-labelledby="login-modal-title"
      // 原生 Esc 触发 cancel：允许关闭并向上层发取消信号（非破坏性操作）
      onCancel={(e) => {
        e.preventDefault();
        handleCancel();
      }}
      // 点击 backdrop（dialog 元素本身，而非内部内容）关闭
      onClick={(e) => {
        if (e.target === dialogRef.current) {
          handleCancel();
        }
      }}
    >
      <form method="dialog" onSubmit={handleSubmit}>
        <div className="login-modal-header">
          <h2 id="login-modal-title">Authentication required</h2>
          <button
            type="button"
            className="login-modal-close"
            aria-label="Close authentication dialog"
            onClick={handleCancel}
            disabled={loading}
          >
            {/* ASCII multiplication sign as close glyph */}
            <span aria-hidden="true">x</span>
          </button>
        </div>
        <p className="login-modal-desc">
          This action requires an access passphrase. Enter the shared passphrase to continue.
        </p>

        {error && (
          <p role="alert" className="login-modal-error">
            {error}
          </p>
        )}

        <label htmlFor="login-password" className="login-modal-label">
          Access passphrase
        </label>
        <input
          ref={inputRef}
          id="login-password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          disabled={loading}
          autoComplete="current-password"
          className="login-modal-input"
          placeholder="Enter access passphrase"
        />

        <div className="login-modal-actions">
          <button
            type="button"
            onClick={handleCancel}
            disabled={loading}
            className="login-modal-btn-secondary"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={loading || !password.trim()}
            aria-busy={loading}
            className="login-modal-btn-primary"
          >
            {loading ? 'Signing in...' : 'Sign in'}
          </button>
        </div>
      </form>
    </dialog>
  );
}
