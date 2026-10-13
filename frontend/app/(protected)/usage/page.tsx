'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '@/lib/api';
import { PageHeader } from '@/components/shell';
import { Badge, Button, Card, EmptyState, StatCard, StatGrid } from '@/components/ui';
import { useToast } from '@/components/use-toast';
import { formatNumber, creditUnitLabel, creditUnitLabelFor } from '@/lib/utils';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

interface UsageData {
  days: number;
  by_day: {
    day: string;
    realm: string;
    calls: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    credits: number;
    avg_ttfb: number;
  }[];
  by_model: {
    model: string;
    realm: string;
    calls: number;
    total_tokens: number;
    credits: number;
    avg_ttfb: number;
  }[];
  by_key: {
    key_id: number;
    key_name: string;
    realm: string;
    calls: number;
    total_tokens: number;
    credits: number;
  }[];
}

interface BillingData {
  accounts: {
    uid: string;
    realm: string;
    nickname: string;
    credits: number;
    credits_used: number;
  }[];
  note: string;
}

const tooltipStyle = {
  background: 'hsl(var(--panel))',
  border: '1px solid hsl(var(--edge))',
  borderRadius: 8,
  fontSize: 12,
};

// 多域曲线配色（按 dayRealms 顺序循环取用）
const REALM_COLORS = [
  'hsl(var(--signal))',
  'hsl(var(--signal))',
  'hsl(var(--caution))',
  'hsl(var(--fault))',
];

export default function UsagePage() {
  const [days, setDays] = useState(7);
  const [data, setData] = useState<UsageData | null>(null);
  const [billing, setBilling] = useState<BillingData | null>(null);
  const { show, node } = useToast();

  const load = useCallback(async () => {
    try {
      const [usage, bill] = await Promise.all([
        api.get<UsageData>(`/api/usage?days=${days}`),
        api.get<BillingData>(`/api/billing?days=${days}`),
      ]);
      setData(usage);
      setBilling(bill);
    } catch (err) {
      show(err instanceof ApiError ? err.message : '加载失败', 'err');
    }
  }, [days, show]);

  useEffect(() => {
    void load();
  }, [load]);

  const totals = (data?.by_day ?? []).reduce(
    (acc, d) => ({
      calls: acc.calls + d.calls,
      tokens: acc.tokens + d.total_tokens,
    }),
    { calls: 0, tokens: 0 },
  );

  // 额度按域分开累计：token 与积分不可相加
  const creditsByRealm = useMemo(() => {
    const m = new Map<string, number>();
    for (const d of data?.by_day ?? []) {
      m.set(d.realm, (m.get(d.realm) ?? 0) + d.credits);
    }
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [data]);

  // 按天趋势透视：同一天可能有多域行，需并成一行、每域一条曲线
  const { dayRows, dayRealms } = useMemo(() => {
    const rows = new Map<string, Record<string, number | string>>();
    const realms = new Set<string>();
    for (const d of data?.by_day ?? []) {
      realms.add(d.realm);
      const row = rows.get(d.day) ?? { day: d.day };
      row[d.realm] = Number(row[d.realm] ?? 0) + d.total_tokens;
      rows.set(d.day, row);
    }
    return {
      dayRows: [...rows.values()].sort((a, b) => String(a.day).localeCompare(String(b.day))),
      dayRealms: [...realms].sort(),
    };
  }, [data]);

  // 按模型分布：同一模型可能跨域各一行，Y 轴标签带域前缀区分
  const modelRows = useMemo(
    () =>
      (data?.by_model ?? [])
        .slice(0, 10)
        .map((m) => ({ ...m, label: m.realm ? `${m.realm}:${m.model}` : m.model })),
    [data],
  );

  async function refreshBilling() {
    try {
      await api.post('/api/billing/refresh');
      show('上游额度已刷新');
      await load();
    } catch (err) {
      show(err instanceof ApiError ? err.message : '刷新失败', 'err');
    }
  }

  return (
    <>
      <PageHeader
        title="用量统计"
        description="按天 / 按模型 / 按密钥多维统计 Token 消耗，并直读上游真实账单"
        actions={
          <>
            <select
              className="input w-auto"
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
            >
              <option value={1}>近 1 天</option>
              <option value={7}>近 7 天</option>
              <option value={30}>近 30 天</option>
              <option value={90}>近 90 天</option>
            </select>
            <Button variant="outline" onClick={() => void refreshBilling()}>
              刷新上游额度
            </Button>
          </>
        }
      />

      <StatGrid>
        <StatCard label="总调用" value={formatNumber(totals.calls)} />
        <StatCard label="总 Token" value={formatNumber(totals.tokens)} />
        {creditsByRealm.map(([realm, value]) => (
          <StatCard
            key={realm}
            label={`总${creditUnitLabel(realm)}（${realm}）`}
            value={value ? formatNumber(value) : '—'}
            hint={value ? undefined : '上游未回报'}
          />
        ))}
      </StatGrid>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <h2 className="mb-3 text-sm font-semibold">按天 Token 趋势</h2>
          {dayRows.length ? (
            <ResponsiveContainer width="100%" height={240}>
              <AreaChart data={dayRows}>
                <defs>
                  <linearGradient id="tokenFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="hsl(var(--signal))" stopOpacity={0.5} />
                    <stop offset="95%" stopColor="hsl(var(--signal))" stopOpacity={0.05} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--edge))" />
                <XAxis dataKey="day" fontSize={11} stroke="hsl(var(--ink-dim))" />
                <YAxis fontSize={11} stroke="hsl(var(--ink-dim))" />
                <Tooltip contentStyle={tooltipStyle} />
                {dayRealms.map((realm, i) => (
                  <Area
                    key={realm}
                    type="monotone"
                    dataKey={realm}
                    name={`${realm} Token`}
                    stroke={REALM_COLORS[i % REALM_COLORS.length]}
                    fill="url(#tokenFill)"
                  />
                ))}
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState message="暂无数据" />
          )}
        </Card>

        <Card>
          <h2 className="mb-3 text-sm font-semibold">按模型 Token 分布</h2>
          {modelRows.length ? (
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={modelRows} layout="vertical">
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--edge))" />
                <XAxis type="number" fontSize={11} stroke="hsl(var(--ink-dim))" />
                <YAxis
                  type="category"
                  dataKey="label"
                  width={140}
                  fontSize={11}
                  stroke="hsl(var(--ink-dim))"
                />
                <Tooltip contentStyle={tooltipStyle} />
                <Bar
                  dataKey="total_tokens"
                  name="Token"
                  fill="hsl(var(--signal))"
                  radius={[0, 4, 4, 0]}
                />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState message="暂无数据" />
          )}
        </Card>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card className="p-0">
          <h2 className="px-4 py-3 text-sm font-semibold">按密钥统计</h2>
          <div className="scroll-thin max-h-72 overflow-auto">
            <table className="data">
              <thead>
                <tr>
                  <th>密钥</th>
                  <th>域</th>
                  <th>调用</th>
                  <th>Token</th>
                  <th>{creditUnitLabelFor((data?.by_key ?? []).map((k) => k.realm))}</th>
                </tr>
              </thead>
              <tbody>
                {(data?.by_key ?? []).map((k) => (
                  <tr key={`${k.key_id}-${k.realm}`}>
                    <td>{k.key_name || `#${k.key_id}`}</td>
                    <td>
                      <Badge className="bg-[hsl(var(--well))]">{k.realm || '—'}</Badge>
                    </td>
                    <td className="tabular-nums">{formatNumber(k.calls)}</td>
                    <td className="tabular-nums">{formatNumber(k.total_tokens)}</td>
                    <td className="tabular-nums">{formatNumber(k.credits)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(data?.by_key ?? []).length === 0 ? <EmptyState message="暂无数据" /> : null}
        </Card>

        <Card className="p-0">
          <h2 className="px-4 py-3 text-sm font-semibold">上游真实账单（实时快照）</h2>
          <div className="scroll-thin max-h-72 overflow-auto">
            <table className="data">
              <thead>
                <tr>
                  <th>账号</th>
                  <th>域</th>
                  <th>剩余{creditUnitLabelFor((billing?.accounts ?? []).map((a) => a.realm))}</th>
                  <th>已消耗</th>
                </tr>
              </thead>
              <tbody>
                {(billing?.accounts ?? []).map((a) => (
                  <tr key={a.uid}>
                    <td>{a.nickname || a.uid}</td>
                    <td>{a.realm}</td>
                    <td className="tabular-nums">{formatNumber(a.credits)}</td>
                    <td className="tabular-nums">{formatNumber(a.credits_used)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(billing?.accounts ?? []).length === 0 ? <EmptyState message="暂无账号" /> : null}
          {billing?.note ? (
            <p className="px-4 pb-3 text-[11px] text-[hsl(var(--ink-dim))]">
              {billing.note}
            </p>
          ) : null}
        </Card>
      </div>
      {node}
    </>
  );
}
