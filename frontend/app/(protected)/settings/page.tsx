'use client';

import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '@/lib/api';
import { PageHeader } from '@/components/shell';
import { Button, Card, EmptyState, Field, Toggle } from '@/components/ui';
import { useToast } from '@/components/use-toast';
import { formatTime } from '@/lib/utils';

interface SettingsConfig {
  listen_host: string;
  listen_port: number;
  public_base_url: string;
  prompt: { mode: string; file: string; default_text: string };
  features: {
    sanitize_blacklist_fingerprints: boolean;
    compress_long_context: boolean;
    reasoning_injection: boolean;
  };
  security: {
    admin_username: string;
    global_allowlist: string[];
    global_blocklist: string[];
    trust_proxy_headers: boolean;
    audit_enabled: boolean;
  };
  pool: {
    max_in_flight: number;
    max_in_flight_global: number;
    breaker_threshold: number;
    top_k: number;
    pick_gap_ms: number;
    soft_rate_seconds: number;
    not_found_seconds: number;
  };
  guards: {
    reserve_credits: number;
    daily_token_limit: number;
    daily_credit_limit: number;
    model_daily_token_limit: number;
    realm_overrides: Record<string, Record<string, number>>;
  };
  models: {
    aliases: Record<string, string>;
  };
  upstream: {
    user_agent: string;
    client_version: string;
    cli_version: string;
    client_name: string;
    passthrough_ip: boolean;
  };
  realms: Record<
    string,
    { enabled: boolean; chat_base: string; billing_base: string; models_base: string }
  >;
}

/** 四条护栏的中文说明（阈值 0 = 关闭）。 */
const GUARD_FIELDS = [
  ['reserve_credits', '保留积分', '余额低于/等于该值时账号整体不再接单，避免余额用尽触发上游提醒'],
  ['daily_token_limit', '每日 Token 限额', '账号当天 token 达到该值后整体不再接单（本地 0 点解封）'],
  ['daily_credit_limit', '每日积分限额', '账号当天积分达到该值后只服务免费模型（本地 0 点解封）'],
  ['model_daily_token_limit', '按模型每日 Token', '账号在单个模型上当天 token 达到该值时，仅该模型不再接单'],
] as const;

interface GuardAccountRow {
  uid: string;
  realm: string;
  nickname: string;
  credits: number;
  daily_tokens_today: number | null;
  daily_credits_today: number | null;
  blocked_reason: string;
  limits: Record<string, number>;
}

interface GuardsResponse {
  limits: Record<string, number>;
  realm_overrides: Record<string, Record<string, number>>;
  pool: { enabled: boolean; accounts: GuardAccountRow[] };
}

interface AuditRow {
  id: number;
  ts: number;
  ip: string;
  action: string;
  target: string;
  result: string;
  detail: string;
}

function splitLines(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function SettingsPage() {
  const [config, setConfig] = useState<SettingsConfig | null>(null);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [guards, setGuards] = useState<GuardsResponse | null>(null);
  const [allowText, setAllowText] = useState('');
  const [blockText, setBlockText] = useState('');
  const [aliasText, setAliasText] = useState('');
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const { show, node } = useToast();

  const load = useCallback(async () => {
    try {
      const [cfgRes, auditRes, guardsRes] = await Promise.all([
        api.get<{ config: SettingsConfig }>('/api/settings'),
        api.get<{ audit: AuditRow[] }>('/api/audit?limit=50'),
        api.get<GuardsResponse>('/api/guards'),
      ]);
      setConfig(cfgRes.config);
      setAllowText(cfgRes.config.security.global_allowlist.join('\n'));
      setBlockText(cfgRes.config.security.global_blocklist.join('\n'));
      setAliasText(
        Object.entries(cfgRes.config.models?.aliases ?? {})
          .map(([k, v]) => `${k}=${v}`)
          .join('\n'),
      );
      setAudit(auditRes.audit);
      setGuards(guardsRes);
    } catch (err) {
      show(err instanceof ApiError ? err.message : '加载失败', 'err');
    }
  }, [show]);

  useEffect(() => {
    void load();
  }, [load]);

  async function patch(payload: unknown, label: string) {
    try {
      await api.patch('/api/settings', payload);
      show(`${label}已保存`);
      await load();
    } catch (err) {
      show(err instanceof ApiError ? err.message : '保存失败', 'err');
    }
  }

  /** 解析 `下游名=上游模型` 的按行输入；非法行整体拒绝，不做部分保存。 */
  function parseAliases(text: string): Record<string, string> | null {
    const out: Record<string, string> = {};
    for (const raw of text.split('\n')) {
      const line = raw.trim();
      if (!line || line.startsWith('#')) continue;
      const at = line.indexOf('=');
      if (at <= 0) return null;
      const key = line.slice(0, at).trim();
      const value = line.slice(at + 1).trim();
      if (!key || !value) return null;
      out[key] = value;
    }
    return out;
  }

  async function saveAliases() {
    const parsed = parseAliases(aliasText);
    if (parsed === null) {
      show('别名格式应为「下游模型名=上游模型名」，每行一条', 'err');
      return;
    }
    await patch({ models: { aliases: parsed } }, '模型别名');
  }

  async function changePassword() {
    try {
      await api.post('/api/password', { old_password: oldPassword, new_password: newPassword });
      setOldPassword('');
      setNewPassword('');
      show('面板密码已更新');
    } catch (err) {
      show(err instanceof ApiError ? err.message : '修改失败', 'err');
    }
  }

  if (!config) {
    return (
      <>
        <PageHeader title="系统设置" />
        <EmptyState message="加载中…" />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="系统设置"
        description="在线改配置热生效并落盘；面板密码以 PBKDF2-SHA256 摘要保存，不存明文"
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <h2 className="mb-3 text-sm font-semibold">提示词与脱敏</h2>
          <div className="flex flex-col gap-3">
            <Field
              label="系统提示词模式"
              hint="custom = 用网关自有提示词替换客户端 system；passthrough = 原样透传"
            >
              <select
                className="input"
                value={config.prompt.mode}
                onChange={(e) => void patch({ prompt: { mode: e.target.value } }, '提示词模式')}
              >
                <option value="custom">custom</option>
                <option value="passthrough">passthrough</option>
                <option value="append">append</option>
              </select>
            </Field>
            <Field label="提示词文件路径" hint="留空则用下方默认文本">
              <input
                className="input"
                defaultValue={config.prompt.file}
                onBlur={(e) => void patch({ prompt: { file: e.target.value } }, '提示词文件')}
              />
            </Field>
            <Field label="默认提示词">
              <textarea
                className="input h-20"
                defaultValue={config.prompt.default_text}
                onBlur={(e) =>
                  void patch({ prompt: { default_text: e.target.value } }, '默认提示词')
                }
              />
            </Field>
            {(
              [
                ['sanitize_blacklist_fingerprints', '指纹脱敏（出站请求体黑名单字段清洗）'],
                ['compress_long_context', '长上下文压缩投影（Codex CLI 友好）'],
                ['reasoning_injection', '推理模型思维链注入'],
              ] as const
            ).map(([key, label]) => (
              <div key={key} className="flex items-center gap-3">
                <Toggle
                  checked={config.features[key]}
                  onChange={(v) => void patch({ features: { [key]: v } }, label)}
                  label={label}
                />
                <span className="text-xs">{label}</span>
              </div>
            ))}
          </div>
        </Card>

        <Card>
          <h2 className="mb-3 text-sm font-semibold">安全与 IP 管控</h2>
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-3">
              <Toggle
                checked={config.security.trust_proxy_headers}
                onChange={(v) => void patch({ security: { trust_proxy_headers: v } }, '代理头信任')}
                label="信任代理头"
              />
              <span className="text-xs">
                信任 X-Forwarded-For（仅反代后开启；关闭时用 TCP 对端地址防伪造）
              </span>
            </div>
            <div className="flex items-center gap-3">
              <Toggle
                checked={config.security.audit_enabled}
                onChange={(v) => void patch({ security: { audit_enabled: v } }, '审计')}
                label="审计"
              />
              <span className="text-xs">记录访问审计</span>
            </div>
            <Field label="全局 IP 白名单" hint="CIDR，每行一个；非空则仅这些 IP 可访问">
              <textarea
                className="input h-20"
                value={allowText}
                onChange={(e) => setAllowText(e.target.value)}
                onBlur={() =>
                  void patch({ security: { global_allowlist: splitLines(allowText) } }, 'IP 白名单')
                }
                placeholder="10.0.0.0/8"
              />
            </Field>
            <Field label="全局 IP 黑名单" hint="CIDR，每行一个；优先于白名单">
              <textarea
                className="input h-20"
                value={blockText}
                onChange={(e) => setBlockText(e.target.value)}
                onBlur={() =>
                  void patch({ security: { global_blocklist: splitLines(blockText) } }, 'IP 黑名单')
                }
              />
            </Field>
            <div className="grid grid-cols-2 gap-2">
              <Field label="原密码">
                <input
                  className="input"
                  type="password"
                  value={oldPassword}
                  onChange={(e) => setOldPassword(e.target.value)}
                />
              </Field>
              <Field label="新密码" hint="至少 8 位">
                <input
                  className="input"
                  type="password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                />
              </Field>
            </div>
            <Button
              variant="primary"
              disabled={!oldPassword || newPassword.length < 8}
              onClick={() => void changePassword()}
            >
              修改面板密码
            </Button>
          </div>
        </Card>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card>
          <h2 className="mb-3 text-sm font-semibold">双域独立路由</h2>
          <div className="flex flex-col gap-3">
            {Object.entries(config.realms).map(([name, realm]) => (
              <div key={name} className="rounded-md border border-[hsl(var(--edge))] p-3">
                <div className="mb-2 flex items-center gap-3">
                  <Toggle
                    checked={realm.enabled}
                    onChange={(v) => void patch({ realms: { [name]: { enabled: v } } }, `${name} 域`) }
                    label={name}
                  />
                  <span className="text-xs font-medium">{name}</span>
                </div>
                <div className="flex flex-col gap-2">
                  <Field label="Chat Base">
                    <input
                      className="input"
                      defaultValue={realm.chat_base}
                      onBlur={(e) =>
                        void patch(
                          { realms: { [name]: { chat_base: e.target.value } } },
                          `${name} chat_base`,
                        )
                      }
                    />
                  </Field>
                  <Field label="Billing Base">
                    <input
                      className="input"
                      defaultValue={realm.billing_base}
                      onBlur={(e) =>
                        void patch(
                          { realms: { [name]: { billing_base: e.target.value } } },
                          `${name} billing_base`,
                        )
                      }
                    />
                  </Field>
                </div>
              </div>
            ))}
          </div>
        </Card>

        <Card>
          <h2 className="mb-1 text-sm font-semibold">账号限额护栏</h2>
          <p className="mb-3 text-[11px] text-[hsl(var(--ink-faint))]">
            上游不会阻止你把余额用尽，这四条护栏在本地拦住。全部填 0 = 关闭（默认）；
            计数按本地 0 点解封，且只在请求路径生效（定时任务不受影响）。
          </p>
          <div className="grid grid-cols-2 gap-3">
            {GUARD_FIELDS.map(([key, label, hint]) => (
              <Field key={key} label={label} hint={hint}>
                <input
                  className="input"
                  type="number"
                  min={0}
                  defaultValue={config.guards[key]}
                  onBlur={(e) =>
                    void patch({ guards: { [key]: Number(e.target.value) } }, label)
                  }
                />
              </Field>
            ))}
          </div>
          {guards?.pool.enabled ? (
            <div className="scroll-thin mt-3 max-h-56 overflow-auto">
              <table className="data">
                <thead>
                  <tr>
                    <th>账号</th>
                    <th>余额</th>
                    <th>今日 Token</th>
                    <th>今日积分</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {guards.pool.accounts.map((a) => (
                    <tr key={a.uid}>
                      <td className="text-[11px]">{a.nickname || a.uid}</td>
                      <td className="mono text-[11px]">{a.credits}</td>
                      <td className="mono text-[11px]">
                        {a.daily_tokens_today ?? '—'}
                      </td>
                      <td className="mono text-[11px]">
                        {a.daily_credits_today ?? '—'}
                      </td>
                      <td className="text-[11px]">
                        {a.blocked_reason ? (
                          <span className="text-[hsl(var(--caution))]">
                            受限 · {a.blocked_reason}
                          </span>
                        ) : (
                          <span className="text-[hsl(var(--ink-faint))]">可服务</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </Card>

        <Card>
          <h2 className="mb-1 text-sm font-semibold">模型别名映射</h2>
          <p className="mb-3 text-[11px] text-[hsl(var(--ink-faint))]">
            把下游在用的模型名映到实际上游模型，客户端硬编码的名字不必改。
            每行一条 <code className="mono">下游名=上游模型</code>，目标可带域前缀
            （如 <code className="mono">fast=zai:glm-5.3-flash</code>）；只做一层映射。
          </p>
          <textarea
            className="input h-32"
            placeholder={'gpt-4o=hy4-preview-f\nclaude-3-5-sonnet=glm-5.3'}
            value={aliasText}
            onChange={(e) => setAliasText(e.target.value)}
            onBlur={() => void saveAliases()}
          />
        </Card>

        <Card>
          <h2 className="mb-3 text-sm font-semibold">账号池与上游</h2>
          <div className="grid grid-cols-2 gap-3">
            {(
              [
                ['max_in_flight', '单号最大在途'],
                ['max_in_flight_global', '国际版最大在途'],
                ['breaker_threshold', '熔断阈值'],
                ['top_k', '短名单长度 Top-K'],
                ['pick_gap_ms', '防惊群窗口(ms)'],
                ['soft_rate_seconds', '429 软冷却(s)'],
                ['not_found_seconds', '404 短冷却(s)'],
              ] as const
            ).map(([key, label]) => (
              <Field key={key} label={label}>
                <input
                  className="input"
                  type="number"
                  defaultValue={config.pool[key]}
                  onBlur={(e) =>
                    void patch({ pool: { [key]: Number(e.target.value) } }, label)
                  }
                />
              </Field>
            ))}
          </div>
          <div className="mt-3 flex flex-col gap-2">
            <Field label="上游 User-Agent" hint="留空 = 按域自动拼装官方桌面端三段式 UA">
              <input
                className="input"
                defaultValue={config.upstream.user_agent}
                onBlur={(e) => void patch({ upstream: { user_agent: e.target.value } }, 'UA')}
              />
            </Field>
            <div className="grid grid-cols-2 gap-2">
              <Field label="客户端版本段">
                <input
                  className="input"
                  defaultValue={config.upstream.client_version}
                  onBlur={(e) =>
                    void patch({ upstream: { client_version: e.target.value } }, '客户端版本')
                  }
                />
              </Field>
              <Field label="CLI 版本段">
                <input
                  className="input"
                  defaultValue={config.upstream.cli_version}
                  onBlur={(e) => void patch({ upstream: { cli_version: e.target.value } }, 'CLI 版本')}
                />
              </Field>
            </div>
            <div className="flex items-center gap-3">
              <Toggle
                checked={config.upstream.passthrough_ip}
                onChange={(v) => void patch({ upstream: { passthrough_ip: v } }, 'IP 透传')}
                label="IP 透传"
              />
              <span className="text-xs">把客户端 IP 透传给上游（默认关闭，反代安全边界）</span>
            </div>
          </div>
        </Card>
      </div>

      <Card className="mt-4 p-0">
        <h2 className="px-4 py-3 text-sm font-semibold">访问审计</h2>
        <div className="scroll-thin max-h-72 overflow-auto">
          <table className="data">
            <thead>
              <tr>
                <th>时间</th>
                <th>IP</th>
                <th>动作</th>
                <th>目标</th>
                <th>结果</th>
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((row) => (
                <tr key={row.id}>
                  <td className="whitespace-nowrap text-[11px]">{formatTime(row.ts)}</td>
                  <td className="text-[11px]">{row.ip || '—'}</td>
                  <td className="text-[11px]">{row.action}</td>
                  <td className="text-[11px]">{row.target || '—'}</td>
                  <td className="text-[11px]">{row.result}</td>
                  <td className="max-w-[240px] truncate text-[11px]">{row.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {audit.length === 0 ? <EmptyState message="暂无审计记录" /> : null}
      </Card>
      {node}
    </>
  );
}
