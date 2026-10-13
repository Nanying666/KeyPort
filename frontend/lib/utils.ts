import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatNumber(n: number | null | undefined): string {
  const value = Number(n ?? 0);
  if (!Number.isFinite(value)) return '0';
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (Math.abs(value) >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(Math.round(value));
}

export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s === 0) return '—';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d`;
}

export function formatTime(ts: number): string {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function formatRelative(ts: number): string {
  if (!ts) return '从未';
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 0) return '刚刚';
  if (diff < 60) return `${diff}s 前`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m 前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h 前`;
  return `${Math.floor(diff / 86400)}d 前`;
}

export function formatMs(ms: number): string {
  if (!ms) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

/**
 * 额度单位按域区分：z.ai Coding Plan 的额度单位是 token，
 * 其余域（cn / global）是积分。返回不含「剩余」的基础单位名。
 */
export function creditUnitLabel(realm: string): string {
  return realm === 'zai' ? '额度' : '积分';
}

/**
 * 一列多域混排时的单位列头。同域时按该域的单位命名，
 * 混排时用中性表述，避免把 token 与积分混为一谈。
 * 无 realm 信息（空列 / 历史数据）按默认域处理，即「积分」。
 */
export function creditUnitLabelFor(realms: (string | null | undefined)[]): string {
  const unique = new Set(realms.filter((r): r is string => Boolean(r)));
  if (unique.size === 0) return creditUnitLabel('');
  if (unique.size === 1) return creditUnitLabel([...unique][0]);
  return '额度 / 积分';
}

/** Token 有效期分档颜色（已过期 / 即将过期 / 偏紧 / 健康）。 */
export const TOKEN_STATE_STYLE: Record<string, { label: string; cls: string }> = {
  expired: { label: '已过期', cls: 'bg-[hsl(var(--fault)/0.18)] text-[hsl(var(--fault))]' },
  soon: { label: '即将过期', cls: 'bg-[hsl(var(--caution)/0.2)] text-[hsl(var(--caution))]' },
  tight: { label: '偏紧', cls: 'bg-[hsl(var(--caution)/0.12)] text-[hsl(var(--caution))]' },
  healthy: { label: '健康', cls: 'bg-[hsl(var(--signal)/0.14)] text-[hsl(var(--signal))]' },
};

export const STATUS_STYLE: Record<string, { label: string; cls: string }> = {
  ok: { label: '正常', cls: 'bg-[hsl(var(--signal)/0.14)] text-[hsl(var(--signal))]' },
  cooldown: { label: '冷却中', cls: 'bg-[hsl(var(--caution)/0.18)] text-[hsl(var(--caution))]' },
  breaker: { label: '熔断', cls: 'bg-[hsl(var(--fault)/0.16)] text-[hsl(var(--fault))]' },
  degraded: { label: '降权', cls: 'bg-[hsl(var(--caution)/0.18)] text-[hsl(var(--caution))]' },
  disabled: { label: '已禁用', cls: 'bg-[hsl(var(--well))] text-[hsl(var(--ink-faint))]' },
  manual_disabled: { label: '已摘除', cls: 'bg-[hsl(var(--well))] text-[hsl(var(--ink-faint))]' },
};

/** 账号健康状态 → 指示灯档位（状态轨与列表共用同一判定）。 */
export function lampToneFor(status: string): 'ok' | 'warn' | 'bad' | 'idle' {
  if (status === 'ok') return 'ok';
  if (status === 'cooldown' || status === 'degraded') return 'warn';
  if (status === 'breaker') return 'bad';
  return 'idle';
}

export function statusStyle(status: string) {
  return STATUS_STYLE[status] ?? { label: status, cls: 'bg-[hsl(var(--well))] text-[hsl(var(--ink-dim))]' };
}

/** 定时任务的中文全称（与调度器 TASKS 一一对应）。 */
export const TASK_LABELS: Record<string, string> = {
  checkin: '每日签到 + 连登管家',
  travel: '猫猫旅行 / 连续打卡',
  activity: '成长任务全自动完成',
  keepalive: 'Token 保活',
  cat: '夜猫任务',
};

/** 按钮/开关上的短标签：完整说明见 TASK_LABELS，此处只取动作名以免撑宽。 */
export const TASK_SHORT: Record<string, string> = {
  checkin: '签到',
  travel: '旅行',
  activity: '成长',
  keepalive: '保活',
  cat: '夜猫',
};

/**
 * 排程字段名 → 中文标签：`checkin_hours` → `签到时间`、`checkin_enabled` → `签到`。
 * 调度器 status() 直接回传原始字段名，前端不能原样展示给用户。
 */
export function scheduleKeyLabel(key: string): string {
  const m = /^(.+)_(hours|enabled)$/.exec(key);
  if (!m) return key;
  const name = TASK_SHORT[m[1]] ?? m[1];
  return m[2] === 'hours' ? `${name}时间` : name;
}
