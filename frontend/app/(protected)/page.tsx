'use client';

import { useCallback, useEffect, useState } from 'react';
import { api, type Overview } from '@/lib/api';
import { PageHeader } from '@/components/shell';
import { Card, PanelHeader, StatCard, StatGrid, Badge, EmptyState, Lamp, Meter } from '@/components/ui';
import { formatDuration, formatNumber, creditUnitLabelFor, scheduleKeyLabel } from '@/lib/utils';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from 'recharts';

export default function DashboardPage() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      setData(await api.get<Overview>('/api/overview'));
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败');
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 10000);
    return () => window.clearInterval(timer);
  }, [load]);

  const pool = data?.pool;
  const day = data?.last_24h;

  return (
    <>
      <PageHeader
        title="概览"
        description="账号池健康度、近 24 小时请求与用量一览（每 10 秒自动刷新）"
        actions={
          <Badge className="bg-[hsl(var(--well))]">{data?.realms.join(' · ') ?? '—'}</Badge>
        }
      />

      {error ? <p className="mb-3 text-xs text-[hsl(var(--fault))]">{error}</p> : null}

      <StatGrid>
        <StatCard
          label="可用账号"
          value={`${pool?.healthy ?? 0} / ${pool?.total ?? 0}`}
          tone={(pool?.healthy ?? 0) > 0 ? 'success' : 'danger'}
          hint="健康 / 总账号数"
        />
        <StatCard label="在途请求" value={pool?.inflight ?? 0} hint="当前占用租约" />
        <StatCard label="会话粘性绑定" value={pool?.sticky_bindings ?? 0} />
        <StatCard
          label="24h 调用"
          value={formatNumber(day?.calls)}
          hint={`失败 ${day?.errors ?? 0}`}
        />
        <StatCard label="24h Token" value={formatNumber(day?.tokens)} />
        {/* 上游 coding-plan 端点不回传单次积分，request_logs.credits 恒为 0。
            有值才显示数字，否则显示 — ，避免把「未回报」误读成「消耗为 0」。 */}
        <StatCard
          label={`24h ${creditUnitLabelFor(data?.realms ?? [])}`}
          value={day?.credits ? formatNumber(day.credits) : '—'}
          hint={day?.credits ? undefined : '上游未回报'}
        />
        <StatCard
          label="平均首字"
          value={day?.avg_ttfb_ms ? `${day.avg_ttfb_ms}ms` : '—'}
          hint="TTFB 上游响应快慢"
        />
      </StatGrid>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <PanelHeader title="各域近 24h 调用量" />
          {data?.by_realm?.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={data.by_realm}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--edge))" vertical={false} />
                <XAxis
                  dataKey="realm"
                  fontSize={11}
                  stroke="hsl(var(--ink-faint))"
                  tickLine={false}
                />
                <YAxis fontSize={11} stroke="hsl(var(--ink-faint))" tickLine={false} axisLine={false} />
                <Tooltip
                  cursor={{ fill: 'hsl(var(--well) / 0.5)' }}
                  contentStyle={{
                    background: 'hsl(var(--panel))',
                    border: '1px solid hsl(var(--edge))',
                    borderRadius: 6,
                    fontSize: 11,
                    color: 'hsl(var(--ink))',
                  }}
                  labelStyle={{ color: 'hsl(var(--ink-dim))' }}
                />
                <Bar dataKey="calls" name="调用" fill="hsl(var(--signal))" radius={[3, 3, 0, 0]} />
                <Bar dataKey="tokens" name="Token" fill="hsl(var(--live))" radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState message="近 24 小时暂无调用" />
          )}
        </Card>

        <Card>
          <PanelHeader title="定时任务" />
          {data?.scheduler ? (
            <div className="flex flex-col gap-3 text-xs">
              <Badge tone={data.scheduler.running ? 'ok' : 'neutral'}>
                {data.scheduler.running ? '调度器运行中' : '调度器已停止'}
              </Badge>
              <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5">
                {Object.entries(data.scheduler.schedule).map(([key, value]) => (
                  <div key={key} className="flex min-w-0 items-baseline justify-between gap-3">
                    <dt className="shrink-0 text-[hsl(var(--ink-faint))]">{scheduleKeyLabel(key)}</dt>
                    <dd className="mono truncate text-right text-[hsl(var(--ink-dim))]">
                      {Array.isArray(value)
                        ? value.map((h) => `${String(h).padStart(2, '0')}:00`).join(' ') || '—'
                        : value
                          ? '开'
                          : '关'}
                    </dd>
                  </div>
                ))}
              </dl>
              {data.scheduler.last_results?.length ? (
                <div className="mt-1">
                  <p className="eyebrow mb-1.5">最近任务结果</p>
                  <ul className="flex flex-col gap-1">
                    {data.scheduler.last_results.slice(-6).map((r, i) => (
                      <li key={`${r.uid}-${i}`} className="flex items-center justify-between gap-2">
                        <span className="truncate text-[hsl(var(--ink-dim))]">{r.uid}</span>
                        <span
                          className={
                            r.ok ? 'text-[hsl(var(--signal))]' : 'text-[hsl(var(--fault))]'
                          }
                        >
                          {r.ok ? '成功' : r.detail || '失败'}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          ) : (
            <EmptyState message="调度器未启动" />
          )}
        </Card>
      </div>

      {pool ? (
        <Card className="mt-4">
          <PanelHeader title="账号池 · 按域分布" />
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
            {Object.entries(pool.by_realm).map(([realm, count]) => (
              <div key={realm} className="flex items-center gap-2">
                <Lamp tone="ok" label={realm} />
                <span className="text-[11.5px] text-[hsl(var(--ink-dim))]">{realm}</span>
                <span className="mono text-[11.5px] font-semibold text-[hsl(var(--ink))]">
                  {count}
                </span>
              </div>
            ))}
            {day?.avg_ttfb_ms ? (
              <span className="ml-auto text-[11px] text-[hsl(var(--ink-faint))]">
                平均首字 {formatDuration(day.avg_ttfb_ms / 1000)}
              </span>
            ) : null}
          </div>
        </Card>
      ) : null}
    </>
  );
}
