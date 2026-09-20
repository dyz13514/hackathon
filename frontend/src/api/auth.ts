/**
 * 认证相关 API（对应后端 app/api/auth.py）。
 *
 * - login：共享口令换 HttpOnly Cookie 会话令牌
 * - logout：清除会话 Cookie
 * - getSession：查询当前会话状态（只读，非校验点）
 */

import { apiFetch } from './client';

export interface SessionStatus {
  readonly authenticated: boolean;
  readonly expires_at: string | null;
}

export function login(password: string): Promise<SessionStatus> {
  return apiFetch<SessionStatus>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ password }),
  });
}

export function logout(): Promise<SessionStatus> {
  return apiFetch<SessionStatus>('/auth/logout', { method: 'POST' });
}

export function getSession(): Promise<SessionStatus> {
  return apiFetch<SessionStatus>('/auth/session');
}
