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

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(init.headers ?? {}),
    },
  });

  if (!response.ok) {
    let body: ErrorEnvelope = {};
    try {
      body = (await response.json()) as ErrorEnvelope;
    } catch {
      // 非 JSON 错误体（如反向代理返回的 502）保持默认值
    }
    // 优先读统一错误包的 `error.*`，回退到顶层字段（非包裹形态）。
    const inner = body.error;
    throw new ApiError(
      response.status,
      inner?.code ?? body.code ?? 'UNKNOWN_ERROR',
      inner?.message ?? body.message ?? body.detail ?? response.statusText,
      inner?.details ?? {},
    );
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}
