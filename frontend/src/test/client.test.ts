/**
 * apiFetch 的 401 登录流程回归测试（fix/frontend-interaction-hardening）。
 *
 * 覆盖：
 * - 取消登录后 pending 请求立即以 LoginCancelledError 结束，不再挂起 / 重复请求。
 * - 多个并发 401 只触发一次 login-required（去重），共享同一登录流程。
 * - 登录成功后原请求只重试一次；仍 401 则抛出，不形成无限循环。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, LoginCancelledError, apiFetch } from '../api/client';
import { uploadImport } from '../api/imports';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function unauth(): Response {
  return jsonResponse(401, { error: { code: 'UNAUTHENTICATED', message: 'no session' } });
}

describe('apiFetch 401 登录流程', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('取消登录后 pending 请求以 LoginCancelledError 结束，不再重复请求', async () => {
    const fetchMock = vi.fn().mockResolvedValue(unauth());
    vi.stubGlobal('fetch', fetchMock);

    const required = vi.fn();
    window.addEventListener('login-required', required);

    const promise = apiFetch('/plans');
    const assertion = expect(promise).rejects.toBeInstanceOf(LoginCancelledError);

    // 等待首个 401 与 login-required 派发
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(required).toHaveBeenCalledTimes(1);

    // 用户取消
    window.dispatchEvent(new CustomEvent('login-cancelled'));
    await assertion;

    // 取消后不得再发起请求（无静默重试）
    expect(fetchMock).toHaveBeenCalledTimes(1);
    window.removeEventListener('login-required', required);
  });

  it('多个并发 401 只触发一次 login-required（共享去重）', async () => {
    const fetchMock = vi.fn().mockResolvedValue(unauth());
    vi.stubGlobal('fetch', fetchMock);

    const required = vi.fn();
    window.addEventListener('login-required', required);

    const p1 = apiFetch('/plans');
    const p2 = apiFetch('/risks');
    const p3 = apiFetch('/quotes');
    const a1 = expect(p1).rejects.toBeInstanceOf(LoginCancelledError);
    const a2 = expect(p2).rejects.toBeInstanceOf(LoginCancelledError);
    const a3 = expect(p3).rejects.toBeInstanceOf(LoginCancelledError);

    await vi.advanceTimersByTimeAsync(0);

    // 三个并发 401，但只弹一次窗
    expect(required).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(3); // 每个请求首次各发一次

    window.dispatchEvent(new CustomEvent('login-cancelled'));
    await Promise.all([a1, a2, a3]);
    window.removeEventListener('login-required', required);
  });

  it('登录成功后原请求只重试一次并返回结果', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(unauth()) // 首次 401
      .mockResolvedValueOnce(jsonResponse(200, { ok: true })); // 登录后重试成功
    vi.stubGlobal('fetch', fetchMock);

    const promise = apiFetch<{ ok: boolean }>('/plans');
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    window.dispatchEvent(new CustomEvent('login-succeeded'));
    const result = await promise;

    expect(result).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(2); // 首次 + 一次重试
  });

  it('登录成功后重试仍 401 时抛出错误，不再循环', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(unauth()) // 首次 401
      .mockResolvedValueOnce(unauth()); // 重试仍 401
    vi.stubGlobal('fetch', fetchMock);

    const promise = apiFetch('/plans');
    const assertion = expect(promise).rejects.toMatchObject({ status: 401 });
    await vi.advanceTimersByTimeAsync(0);

    window.dispatchEvent(new CustomEvent('login-succeeded'));
    await assertion;

    // 只重试一次：不形成无限循环
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('并发 401 在登录成功后各自重试一次', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      // 每个 url 首次 401，之后成功
      const calls = fetchMock.mock.calls.filter((c) => c[0] === url).length;
      return Promise.resolve(calls === 1 ? unauth() : jsonResponse(200, { url }));
    });
    vi.stubGlobal('fetch', fetchMock);

    const required = vi.fn();
    window.addEventListener('login-required', required);

    const p1 = apiFetch(`${API_PLANS}`);
    const p2 = apiFetch(`${API_RISKS}`);
    await vi.advanceTimersByTimeAsync(0);
    expect(required).toHaveBeenCalledTimes(1);

    window.dispatchEvent(new CustomEvent('login-succeeded'));
    await Promise.all([p1, p2]);
    window.removeEventListener('login-required', required);
  });
});

const API_PLANS = '/plans';
const API_RISKS = '/risks';

describe('ApiError 类型', () => {
  it('LoginCancelledError 是 ApiError 的子类且携带 401', () => {
    const err = new LoginCancelledError();
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(401);
    expect(err.code).toBe('LOGIN_CANCELLED');
  });
});

describe('CSV 上传会话续期', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('过期后弹登录窗、原 multipart 请求重试一次且不强设 JSON 头', async () => {
    const upload = { upload_id: 'UP-1', file_name: 'materials.csv', total_rows: 1, duplicate_of: null, last_imported_at: null };
    const fetchMock = vi.fn().mockResolvedValueOnce(unauth()).mockResolvedValueOnce(jsonResponse(200, upload));
    vi.stubGlobal('fetch', fetchMock);
    const required = vi.fn();
    window.addEventListener('login-required', required);

    const pending = uploadImport(new File(['material_id,name\nMAT-1,Steel'], 'materials.csv'));
    await vi.advanceTimersByTimeAsync(0);
    expect(required).toHaveBeenCalledTimes(1);
    window.dispatchEvent(new CustomEvent('login-succeeded'));
    expect(await pending).toEqual(upload);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (const [url, init] of fetchMock.mock.calls) {
      expect(url).toBe('/api/imports/upload');
      expect(init).toMatchObject({ method: 'POST', credentials: 'include' });
      expect(init.body).toBeInstanceOf(FormData);
      expect(init.headers).toBeUndefined();
    }
    window.removeEventListener('login-required', required);
  });
});
