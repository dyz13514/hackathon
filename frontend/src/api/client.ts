/**
 * 后端访问的唯一出口。
 *
 * 三条约定：
 * 1. 全部路径以 `/api` 为前缀，开发期由 Vite 代理转发，生产期由 Caddy 转发。
 * 2. `credentials: 'include'`——会话令牌是 HttpOnly Cookie（R23.12），前端读不到它，
 *    也**不做任何权限判断**：写端点的授权一律由服务端校验。
 * 3. 错误以 `ApiError` 抛出并携带后端的错误码，供各视图按 code 决定下一步入口
 *    （如 `STALE_PROPOSAL` 显示「基于最新数据重新生成」）。
 *
 * 401 自动重试机制（去重 + 可取消）：
 * - 收到 401 时进入共享的登录流程；并发的多个 401 只触发**一次** `login-required`
 *   事件、只挂一组监听器与一个超时器（避免叠加多个弹窗 / 监听器 / timer）。
 * - 登录成功（`login-succeeded`）后，所有等待中的请求各自重试**一次**；仍 401 则
 *   抛出错误，不再循环。
 * - 用户取消登录（`login-cancelled`）后，所有等待中的请求立即以
 *   `LoginCancelledError` 结束，不会继续挂起、重复请求或静默重试。
 *
 * 类型化的端点函数由 OpenAPI 生成，随各端点落地补齐（design.md「项目结构」）。
 */

export const API_BASE = '/api';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    /** 后端错误包的 `details`（如 `STALE_PROPOSAL` 的两个版本号），供视图决定下一步。 */
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/**
 * 用户主动取消登录时抛出。视图可据此区分「取消」与真实错误：
 * 取消不应弹出错误提示，静默结束即可。
 */
export class LoginCancelledError extends ApiError {
  constructor() {
    super(401, 'LOGIN_CANCELLED', 'Authentication was cancelled.');
    this.name = 'LoginCancelledError';
  }
}

/**
 * 后端统一错误包（`app/api/errors.py`）：`{"error": {code, message, details, ...}}`。
 * 兼容极少数非包裹形态（如反向代理的 502 只有 `detail`）。
 */
interface ErrorEnvelope {
  readonly error?: {
    readonly code?: string;
    readonly message?: string;
    readonly details?: Record<string, unknown>;
  };
  readonly code?: string;
  readonly message?: string;
  readonly detail?: string;
}

/** 解析响应体为 ApiError，供内部复用。 */
async function parseApiError(response: Response): Promise<ApiError> {
  let body: ErrorEnvelope = {};
  try {
    body = (await response.json()) as ErrorEnvelope;
  } catch {
    // 非 JSON 错误体（如反向代理返回的 502）保持默认值
  }
  const inner = body.error;
  return new ApiError(
    response.status,
    inner?.code ?? body.code ?? 'UNKNOWN_ERROR',
    inner?.message ?? body.message ?? body.detail ?? response.statusText,
    inner?.details ?? {},
  );
}

/** 登录流程默认超时（5 分钟）。 */
const LOGIN_TIMEOUT_MS = 5 * 60 * 1000;

/**
 * 共享的登录流程：并发的多个 401 复用同一个 Promise，因此只会
 * - 派发一次 `login-required`（弹一个窗），
 * - 挂一组事件监听器与一个超时器，
 * - 在成功 / 取消 / 超时时统一清理并唤醒所有等待者。
 */
let pendingLogin: Promise<void> | null = null;

function waitForLogin(): Promise<void> {
  if (pendingLogin) return pendingLogin;

  pendingLogin = new Promise<void>((resolve, reject) => {
    let timer: ReturnType<typeof setTimeout>;

    const cleanup = () => {
      clearTimeout(timer);
      window.removeEventListener('login-succeeded', onSuccess);
      window.removeEventListener('login-cancelled', onCancel);
      pendingLogin = null;
    };

    const onSuccess = () => {
      cleanup();
      resolve();
    };
    const onCancel = () => {
      cleanup();
      reject(new LoginCancelledError());
    };

    timer = setTimeout(() => {
      cleanup();
      reject(new ApiError(401, 'UNAUTHENTICATED', 'Login timed out. Please try again.'));
    }, LOGIN_TIMEOUT_MS);

    window.addEventListener('login-succeeded', onSuccess);
    window.addEventListener('login-cancelled', onCancel);
    window.dispatchEvent(new CustomEvent('login-required'));
  });

  return pendingLogin;
}

/** 原始响应入口：multipart 等非 JSON 请求也复用相同的会话续期和错误解析。 */
export async function apiFetchResponse(path: string, init: RequestInit = {}): Promise<Response> {
  const doFetch = () =>
    fetch(`${API_BASE}${path}`, {
      ...init,
      credentials: 'include',
    });

  let response = await doFetch();

  // 收到 401：进入共享登录流程，成功后重试一次；取消 / 超时则抛出，不再挂起。
  if (response.status === 401) {
    await waitForLogin();
    response = await doFetch();
  }

  if (!response.ok) {
    throw await parseApiError(response);
  }

  return response;
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetchResponse(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init.headers ?? {}),
    },
  });

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}
