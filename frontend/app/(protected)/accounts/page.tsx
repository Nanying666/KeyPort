'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError, type AccountView } from '@/lib/api';
import { PageHeader } from '@/components/shell';
import {
  AlertDialog,
  Badge,
  Button,
  Card,
  EmptyState,
  Lamp,
  StatCard,
} from '@/components/ui';
import { useToast } from '@/components/use-toast';
import { ApiKeyAccountModal, ImportModal, OAuthModal } from '@/components/account-modals';
import {
  TOKEN_STATE_STYLE,
  creditUnitLabelFor,
  formatDuration,
  formatNumber,
  formatRelative,
  formatTime,
  lampToneFor,
  statusStyle,
} from '@/lib/utils';

interface AccountsResponse {
  accounts: AccountView[];
  summary: { total: number; healthy: number; inflight: number; by_realm: Record<string, number> };
  realms: string[];
}

/**
 * 两个账号池分页。
 *
 * 为什么分开：ZCode（Coding Plan）与 WorkBuddy / CodeBuddy 是**两套完全不同的
 * 上游身份体系** ——
 *   - ZCode 认桌面 SKU（`x-platform` / `x-os-version` / `x-client-*` / `x-device-mid`），
 *     账号入池会分配成套设备档案，还要跑官方首启安装序、领套餐；
 *   - WorkBuddy / CodeBuddy 只认 `X-Machine-ID`（按 uid 派生），没有 SKU、没有安装序。
 * 混在一张表里会让人误以为后者也有设备档案。分开后每页只显示**该体系真正存在**
 * 的列与操作。
 */
type PoolKey = 'wb' | 'zcode';

interface PoolMeta {
  key: PoolKey;
  label: string;
  hint: string;
  /** 该池是否展示设备档案 / 安装序相关列与操作 */
  hasSku: boolean;
  /** 该池是否支持套餐领取与安装序 */
  hasInstall: boolean;
}

const POOLS: readonly PoolMeta[] = [
  {
    key: 'wb',
    label: 'WorkBuddy / CodeBuddy',
    hint: '国内版 + 国际版；身份为按 uid 派生的 X-Machine-ID，无需桌面 SKU',
    hasSku: false,
    hasInstall: false,
  },
  {
    key: 'zcode',
    label: 'ZCode',
    hint: 'z.ai Coding Plan；持有成套桌面 SKU，支持首启安装序与限时套餐领取',
    hasSku: true,
    hasInstall: true,
  },
] as const;

export default function AccountsPage() {
  const [data, setData] = useState<AccountsResponse | null>(null);
  const [pool, setPool] = useState<PoolKey>('wb');
  const [oauthOpen, setOauthOpen] = useState(false);
  const [keyOpen, setKeyOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<AccountView | null>(null);
  const { show, node } = useToast();

  const load = useCallback(async () => {
    try {
      setData(await api.get<AccountsResponse>('/api/accounts'));
    } catch (err) {
      show(err instanceof ApiError ? err.message : '加载失败', 'err');
    }
  }, [show]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 15000);
    return () => window.clearInterval(timer);
  }, [load]);

  /** 按上游身份体系分池：zcode_platform 由后端判定（realm + 端点），前端不猜。 */
  const pools = useMemo(() => {
    const list = data?.accounts ?? [];
    return {
      wb: list.filter((a) => !a.zcode_platform),
      zcode: list.filter((a) => a.zcode_platform),
    } as Record<PoolKey, AccountView[]>;
  }, [data]);

  const current = POOLS.find((p) => p.key === pool)!;
  const accounts = pools[pool];

  async function act(path: string, label: string) {
    try {
      await api.post(path);
      show(`${label}成功`);
      await load();
    } catch (err) {
      show(err instanceof ApiError ? err.message : `${label}失败`, 'err');
    }
  }

  async function remove(uid: string) {
    setPendingDelete(null);
    try {
      await api.del(`/api/accounts/${uid}`);
      show('已删除');
      await load();
    } catch (err) {
      show(err instanceof ApiError ? err.message : '删除失败', 'err');
    }
  }

  async function refreshCredits(uid: string) {
    try {
      await api.post(`/api/accounts/${uid}/refresh-credits`);
      show('额度已刷新');
      await load();
    } catch (err) {
      show(err instanceof ApiError ? err.message : '刷新失败', 'err');
    }
  }

  async function exportPool() {
    try {
      const res = await fetch('/api/accounts/export/download', {
        credentials: 'same-origin',
      });
      if (!res.ok) {
        const text = await res.text();
        let detail = `导出失败（HTTP ${res.status}）`;
        try {
          detail = JSON.parse(text).detail ?? detail;
        } catch {
          /* 非 JSON 响应，保留默认提示 */
        }
        throw new ApiError(res.status, detail);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `keyport-accounts-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      show('已导出账号凭证');
    } catch (err) {
      show(err instanceof ApiError ? err.message : '导出失败', 'err');
    }
  }

  /** 单页列表：按该池的 meta 决定是否渲染设备档案列。 */
  function renderTable(meta: PoolMeta, rows: AccountView[]) {
    return (
      <Card className="p-0">
        <div className="scroll-thin overflow-x-auto">
          <table className="data has-actions">
            <thead>
              <tr>
                <th>账号</th>
                <th>域</th>
                <th>状态</th>
                <th>Token</th>
                <th>{creditUnitLabelFor(rows.map((a) => a.realm))}</th>
                <th>快过期</th>
                <th>调用/失败</th>
                <th>在途</th>
                {meta.hasSku ? <th>设备档案</th> : null}
                <th>最后使用</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => {
                const st = statusStyle(a.status);
                const tk = TOKEN_STATE_STYLE[a.token_state] ?? TOKEN_STATE_STYLE.healthy;
                return (
                  <tr key={a.uid}>
                    <td>
                      <div className="flex items-center gap-2">
                        <Lamp
                          tone={lampToneFor(a.status)}
                          label={`${a.nickname || a.uid}：${st.label}`}
                        />
                        <div className="flex flex-col">
                          <span className="font-medium text-[hsl(var(--ink))]">
                            {a.nickname || a.uid}
                          </span>
                          <span className="mono text-[10px] text-[hsl(var(--ink-faint))]">
                            {a.uid} · {a.kind}
                          </span>
                        </div>
                      </div>
                    </td>
                    <td>
                      <Badge tone="neutral">{a.realm}</Badge>
                    </td>
                    <td>
                      <div className="flex flex-col gap-0.5">
                        <Badge className={st.cls}>{st.label}</Badge>
                        {a.cooldown_remaining > 0 ? (
                          <span className="text-[10px] text-[hsl(var(--ink-faint))]">
                            剩 {formatDuration(a.cooldown_remaining)}
                          </span>
                        ) : null}
                        {a.risk_strikes > 0 ? (
                          <span
                            className="text-[10px] text-[hsl(var(--caution))]"
                            title={`风控命中 ${a.risk_strikes} 次（窗口内累计到阈值会升级禁用）`}
                          >
                            风控 ×{a.risk_strikes}
                          </span>
                        ) : null}
                      </div>
                    </td>
                    <td>
                      <div className="flex flex-col gap-0.5">
                        <Badge className={tk.cls}>{tk.label}</Badge>
                        {a.token_expires_at ? (
                          <span className="mono text-[10px] text-[hsl(var(--ink-faint))]">
                            {formatTime(a.token_expires_at)}
                          </span>
                        ) : null}
                      </div>
                    </td>
                    <td className="mono text-[hsl(var(--ink))]">{formatNumber(a.credits)}</td>
                    <td className="mono">{formatNumber(a.credits_expiring)}</td>
                    <td className="mono">
                      {a.total_calls} /{' '}
                      <span className={a.failed_calls > 0 ? 'text-[hsl(var(--fault))]' : undefined}>
                        {a.failed_calls}
                      </span>
                    </td>
                    <td className="mono">{a.inflight}</td>
                    {meta.hasSku ? (
                      <td>
                        <div className="flex flex-col gap-0.5">
                          <span className="mono text-[10px]">
                            {a.fingerprint?.platform
                              ? `${a.fingerprint.platform}-${a.fingerprint.arch}`
                              : '—'}
                          </span>
                          <span className="text-[10px] text-[hsl(var(--ink-faint))]">
                            {a.fingerprint?.screen ?? ''}
                          </span>
                          <span
                            className={
                              a.installed_at
                                ? 'text-[10px] text-[hsl(var(--signal))]'
                                : 'text-[10px] text-[hsl(var(--ink-faint))]'
                            }
                          >
                            {a.installed_at ? '已安装' : '未安装'}
                          </span>
                        </div>
                      </td>
                    ) : null}
                    <td className="text-[11px]">{formatRelative(a.last_used_at)}</td>
                    <td>
                      <div className="flex flex-wrap gap-1">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => void refreshCredits(a.uid)}
                        >
                          刷额度
                        </Button>
                        {meta.hasInstall ? (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() =>
                              void act(`/api/accounts/${a.uid}/install`, '补跑安装序')
                            }
                          >
                            安装
                          </Button>
                        ) : null}
                        {meta.hasSku ? (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() =>
                              void act(`/api/accounts/${a.uid}/rotate-fingerprint`, '换发设备')
                            }
                          >
                            换设备
                          </Button>
                        ) : null}
                        {a.manual_disabled ? (
                          <Button
                            size="sm"
                            onClick={() => void act(`/api/accounts/${a.uid}/enable`, '放回池')}
                          >
                            放回池
                          </Button>
                        ) : (
                          <Button
                            size="sm"
                            onClick={() => void act(`/api/accounts/${a.uid}/disable`, '摘除')}
                          >
                            摘除
                          </Button>
                        )}
                        {!a.healthy || !a.enabled ? (
                          <Button
                            size="sm"
                            onClick={() => void act(`/api/accounts/${a.uid}/revive`, '恢复')}
                          >
                            恢复
                          </Button>
                        ) : null}
                        <Button size="sm" variant="danger" onClick={() => setPendingDelete(a)}>
                          删除
                        </Button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {rows.length === 0 ? (
          <EmptyState
            message={
              meta.key === 'zcode'
                ? '暂无 ZCode 账号：右上角「API Key 纳管」或「OAuth 登录」选 zai 域'
                : '暂无 WorkBuddy / CodeBuddy 账号：右上角「OAuth 登录」或「导入」'
            }
          />
        ) : null}
      </Card>
    );
  }

  return (
    <>
      <PageHeader
        title="账号管理"
        description="两套上游身份体系分开管理：WorkBuddy / CodeBuddy 用派生机器码，ZCode 用桌面 SKU"
        actions={
          <>
            <Button variant="outline" onClick={() => setImportOpen(true)}>
              导入
            </Button>
            <Button variant="outline" onClick={() => void exportPool()}>
              导出
            </Button>
            <Button
              variant="outline"
              onClick={() => void act('/api/accounts/reload-files', '扫描 auths/')}
            >
              扫描 auths/
            </Button>
            <Button variant="ghost" onClick={() => setKeyOpen(true)}>
              API Key 纳管
            </Button>
            <Button variant="primary" onClick={() => setOauthOpen(true)}>
              OAuth 登录
            </Button>
          </>
        }
      />

      <div className="mb-4 flex flex-wrap gap-3">
        <StatCard label="账号总数" value={data?.summary.total ?? 0} />
        <StatCard
          label="健康"
          value={data?.summary.healthy ?? 0}
          tone={(data?.summary.healthy ?? 0) > 0 ? 'success' : 'danger'}
        />
        <StatCard label="在途" value={data?.summary.inflight ?? 0} />
        {Object.entries(data?.summary.by_realm ?? {}).map(([realm, count]) => (
          <StatCard key={realm} label={`域 ${realm}`} value={count} />
        ))}
      </div>

      {/* 池切换：两个池各自的账号数直接标在页签上 */}
      <div className="mb-3 flex flex-wrap gap-1.5 border-b border-[hsl(var(--edge))] pb-2">
        {POOLS.map((p) => {
          const active = p.key === pool;
          return (
            <button
              key={p.key}
              type="button"
              onClick={() => setPool(p.key)}
              aria-current={active ? 'page' : undefined}
              className={
                'flex items-center gap-2 rounded px-3 py-1.5 text-[12.5px] transition-colors ' +
                (active
                  ? 'bg-[hsl(var(--well))] font-medium text-[hsl(var(--ink))]'
                  : 'text-[hsl(var(--ink-dim))] hover:bg-[hsl(var(--well)/0.6)] hover:text-[hsl(var(--ink))]')
              }
            >
              {p.label}
              <span className="mono text-[10px] text-[hsl(var(--ink-faint))]">
                {pools[p.key].length}
              </span>
            </button>
          );
        })}
      </div>

      <p className="mb-3 text-[11px] text-[hsl(var(--ink-faint))]">{current.hint}</p>

      {renderTable(current, accounts)}

      <OAuthModal
        open={oauthOpen}
        onClose={() => setOauthOpen(false)}
        onDone={load}
        realms={data?.realms ?? []}
      />
      <ApiKeyAccountModal
        open={keyOpen}
        onClose={() => setKeyOpen(false)}
        onDone={load}
        realms={data?.realms ?? []}
      />
      <ImportModal open={importOpen} onClose={() => setImportOpen(false)} onDone={load} />
      <AlertDialog
        open={pendingDelete !== null}
        title="删除账号"
        message={`确认删除账号 ${pendingDelete?.nickname || pendingDelete?.uid || ''}？该操作不可撤销。`}
        confirmLabel="删除"
        onConfirm={() => void remove(pendingDelete!.uid)}
        onCancel={() => setPendingDelete(null)}
      />
      {node}
    </>
  );
}
