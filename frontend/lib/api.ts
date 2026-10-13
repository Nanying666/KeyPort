/**
 * 统一 API 客户端。
 * 单进程单端口部署：前端与 /api 同源，无需配置 base URL。
 */

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  });

  const text = await res.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }

  if (!res.ok) {
    const detail =
      body && typeof body === 'object' && 'detail' in body
        ? String((body as { detail: unknown }).detail)
        : typeof body === 'string' && body
          ? body
          : `请求失败（HTTP ${res.status}）`;
    throw new ApiError(res.status, detail);
  }
  return body as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, data?: unknown) =>
    request<T>(path, { method: 'POST', body: JSON.stringify(data ?? {}) }),
  patch: <T>(path: string, data?: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(data ?? {}) }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
};

// ---- 类型定义（与后端返回结构一一对应） ----

export interface AccountFingerprint {
  platform?: string;
  arch?: string;
  os_version?: string;
  language?: string;
  timezone?: string;
  screen?: string;
  device_mid?: string;
}

export interface AccountView {
  uid: string;
  realm: string;
  nickname: string;
  kind: string;
  enabled: boolean;
  manual_disabled: boolean;
  in_pool: boolean;
  healthy: boolean;
  status: string;
  cooldown_remaining: number;
  disabled_reason: string;
  credits: number;
  credits_used: number;
  credits_expiring: number;
  credits_updated_at: number;
  total_calls: number;
  failed_calls: number;
  consecutive_failures: number;
  inflight: number;
  last_used_at: number;
  last_error: string;
  last_checkin_at: number;
  token_expires_at: number;
  token_remaining: number;
  token_state: 'expired' | 'soon' | 'tight' | 'healthy';
  model_cooldowns: Record<string, number>;
  /** 风控（3012 / unusual activity）命中计数；窗口内累计到阈值会升级禁用 */
  risk_strikes: number;
  last_risk_at: number;
  /** 每账号设备档案（成套桌面 SKU + device_mid，一号一台设备） */
  fingerprint: AccountFingerprint;
  /** 官方首启安装序完成时间（0 = 未完成） */
  installed_at: number;
}

export interface ClaimPlanGrant {
  name: string;
  units: number;
  period: string;
}

export interface ClaimPlan {
  plan_id: string;
  name: string;
  description: string;
  priority: number;
  grants: ClaimPlanGrant[];
}

export interface ClaimAccountPreview {
  uid: string;
  nickname: string;
  ok: boolean;
  error?: string;
  plans?: ClaimPlan[];
  /** 1005 名额等待期记忆：plan_id → 名额恢复时间（毫秒） */
  holds?: Record<string, number>;
}

export interface ClaimHistoryRow {
  id: number;
  ts: number;
  uid: string;
  account: string;
  plan_id: string;
  plan_name: string;
  outcome: string;
  code: number;
  starts_at: number;
  ends_at: number;
  detail: string;
}

export interface ApiKeyView {
  id: number;
  name: string;
  key_prefix: string;
  enabled: boolean;
  realm: string;
  model_allow: string[];
  quota_credits: number;
  used_credits: number;
  quota_tokens: number;
  used_tokens: number;
  ip_allow: string[];
  ip_block: string[];
  note: string;
  created_at: number;
  last_used_at: number;
}

export interface RequestLog {
  id: number;
  ts: number;
  key_id: number;
  key_name: string;
  account_uid: string;
  account_name: string;
  realm: string;
  protocol: string;
  model: string;
  stream: number;
  status: number;
  ttfb_ms: number;
  total_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  credits: number;
  retries: number;
  client_ip: string;
  error: string;
}

export interface ModelEntry {
  id: string;
  display_name?: string;
  context_window?: number;
  max_output_tokens?: number;
  reasoning_efforts?: string[];
  reasoning_default_effort?: string;
  series?: string;
  supports_images?: boolean;
  realm?: string;
}

export interface Overview {
  pool: { total: number; healthy: number; inflight: number; sticky_bindings: number; by_realm: Record<string, number> };
  last_24h: { calls: number; tokens: number; credits: number; avg_ttfb_ms: number; errors: number };
  by_realm: { realm: string; calls: number; tokens: number }[];
  realms: string[];
  scheduler: SchedulerStatus | null;
}

export interface SchedulerStatus {
  running: boolean;
  schedule: Record<string, number[] | boolean>;
  last_results: { uid: string; realm: string; ok: boolean; detail: string }[];
}
