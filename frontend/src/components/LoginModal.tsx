/**
 * 全局登录弹窗（HttpOnly Cookie 会话认证，R23.12）。
 *
 * 触发时机：任意 API 请求收到 401 UNAUTHENTICATED 时，由 App 层通过
 * `login-required` 自定义事件弹出此窗口。
 *
 * 登录成功后：
 * 1. Cookie 由后端通过 Set-Cookie 写入（HttpOnly，前端不可读取）。
 * 2. 触发 `login-succeeded` 自定义事件，通知原始请求方重试。
 * 3. 弹窗关闭。
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
}

export function LoginModal({ open, onSuccess }: LoginModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 同步 open 状态到原生 <dialog>
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) {
      if (!dialog.open) {
        dialog.showModal();
      }
      // 打开时清空上次状态，聚焦输入框
      setPassword('');
      setError(null);
      setTimeout(() => inputRef.current?.focus(), 0);
    } else {
      if (dialog.open) {
        dialog.close();
      }
    }
  }, [open]);

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
            ? '口令不正确，请重试。'
            : `登录失败（${err.code}）：${err.message}`
          : '网络或服务不可用，请稍后重试。';
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
      // 阻止点击背景关闭（必须显式登录才能继续）
      onCancel={(e) => e.preventDefault()}
    >
      <form method="dialog" onSubmit={handleSubmit}>
        <h2 id="login-modal-title">需要认证</h2>
        <p className="login-modal-desc">
          此操作需要访问口令。请输入共享口令后继续。
        </p>

        {error && (
          <p role="alert" className="login-modal-error">
            <span aria-hidden="true">⚠ </span>
            {error}
          </p>
        )}

        <label htmlFor="login-password" className="login-modal-label">
          访问口令
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
          placeholder="输入访问口令"
        />

        <div className="login-modal-actions">
          <button
            type="submit"
            disabled={loading || !password.trim()}
            aria-busy={loading}
            className="login-modal-btn-primary"
          >
            {loading ? '验证中…' : '确认登录'}
          </button>
        </div>
      </form>
    </dialog>
  );
}
