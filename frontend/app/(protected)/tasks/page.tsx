'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  api,
  ApiError,
  type ClaimAccountPreview,
  type ClaimHistoryRow,
} from '@/lib/api';
import { PageHeader } from '@/components/shell';
import { Badge, Button, Card, EmptyState, Toggle } from '@/components/ui';
import { useToast } from '@/components/use-toast';
import { formatNumber, formatTime, TASK_LABELS, TASK_SHORT } from '@/lib/utils';

interface TaskRun {
  id: number;
  ts: number;
  task: string;
  realm: string;
  account: string;
  ok: number;
  detail: string;
}

interface ScheduleState {
  checkin_hours: number[];
  travel_hours: number[];
  activity_hours: number[];
  keepalive_hours: number[];
  cat_hours: number[];
  checkin_enabled: boolean;
  travel_enabled: boolean;
  activity_enabled: boolean;
  keepalive_enabled: boolean;
  cat_enabled: boolean;
}

interface ClaimStatus {
  enabled: boolean;
  round_interval_seconds: number;
  auto_on_add: boolean;
  holds: Record<string, Record<string, number>>;
  last_results: { uid: string; ok: boolean; outcome?: string; message?: string }[];
}

interface TasksResponse {
  scheduler: { running: boolean; schedule: ScheduleState } | null;
  claims: ClaimStatus | null;
  runs: TaskRun[];
}

interface ClaimPreviewResponse {
  accounts: ClaimAccountPreview[];
  status: ClaimStatus;
}

const SCHEDULE_ROWS = [
  ['checkin', 'checkin_hours', 'checkin_enabled'],
  ['travel', 'travel_hours', 'travel_enabled'],
  ['activity', 'activity_hours', 'activity_enabled'],
  ['keepalive', 'keepalive_hours', 'keepalive_enabled'],
  ['cat', 'cat_hours', 'cat_enabled'],
] as const;

/** 领取结果码 → 中文：与后端 constants.CLAIM_CODE_MESSAGES 同口径。 */
const CLAIM_OUTCOME_LABEL: Record<string, string> = {
  claimed: '已领取',
  no_plans: '无可领套餐',
  failed: '领取失败',
  error: '上游错误',
};

export default function TasksPage() {
  const [data, setData] = useState<TasksResponse | null>(null);
  const [schedule, setSchedule] = useState<ScheduleState | null>(null);
  const [preview, setPreview] = useState<ClaimPreviewResponse | null>(null);
  const [history, setHistory] = useState<ClaimHistoryRow[]>([]);
  const [realm, setRealm] = useState('');
  const { show, node } = useToast();

  const load = useCallback(async () => {
    try {
      const res = await api.get<TasksResponse>('/api/tasks');
      setData(res);
      if (res.scheduler) setSchedule(res.scheduler.schedule);
    } catch (err) {
      show(err instanceof ApiError ? err.message : '加载失败', 'err');
    }
  }, [show]);

  const loadClaim = useCallback(async () => {
    try {
      const [p, h] = await Promise.all([
        api.get<ClaimPreviewResponse>('/api/claim/preview'),
        api.get<{ history: ClaimHistoryRow[] }>('/api/claim/history?limit=50'),
      ]);
      setPreview(p);
      setHistory(h.history);
    } catch {
      /* 领取服务未启用时静默：任务页主体仍可用 */
    }
  }, []);

  useEffect(() => {
    void load();
    void loadClaim();
    const timer = window.setInterval(() => {
      void load();
      void loadClaim();
    }, 20000);
    return () => window.clearInterval(timer);
  }, [load, loadClaim]);

  async function runTask(task: string) {
    try {
      const res = await api.post<{ result: { ok: boolean }[] }>('/api/tasks/run', { task, realm });
      const okCount = res.result.filter((r) => r.ok).length;
      show(`${TASK_LABELS[task] ?? task}：${okCount}/${res.result.length} 成功`);
      await load();
    } catch (err) {
      show(err instanceof ApiError ? err.message : '执行失败', 'err');
    }
  }

  async function runClaim(uid?: string) {
    try {
      await api.post('/api/claim/run', uid ? { uid } : {});
      show(uid ? '该账号领取完成' : '领取轮执行完成');
      await loadClaim();
    } catch (err) {
      show(err instanceof ApiError ? err.message : '领取失败', 'err');
    }
  }

  async function saveSchedule(patch: Partial<ScheduleState>) {
    try {
      const res = await api.patch<{ schedule: ScheduleState }>('/api/tasks/schedule', patch);
      setSchedule(res.schedule);
      show('排程已更新并落盘');
    } catch (err) {
      show(err instanceof ApiError ? err.message : '保存失败', 'err');
    }
  }

  return (
    <>
      <PageHeader
        title="任务调度"
        description="后台常驻定时调度器：签到 / 旅行 / 成长任务 / 保活 / 夜猫，独立排程与开关；限时套餐领取轮"
        actions={
          <>
            <input
              className="input w-40"
              placeholder="限定域（留空=全部）"
              value={realm}
              onChange={(e) => setRealm(e.target.value)}
            />
            {Object.keys(TASK_LABELS).map((task) => (
              <Button key={task} variant="outline" onClick={() => void runTask(task)}>
                执行 {TASK_SHORT[task] ?? task}
              </Button>
            ))}
          </>
        }
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <div className="mb-3 flex items-center gap-2">
            <h2 className="text-sm font-semibold">排程配置</h2>
            {data?.scheduler ? (
              <Badge
                className={
                  data.scheduler.running
                    ? 'bg-[hsl(var(--signal)/0.2)] text-[hsl(var(--signal))]'
                    : 'bg-[hsl(var(--well))]'
                }
              >
                {data.scheduler.running ? '运行中' : '已停止'}
              </Badge>
            ) : null}
          </div>
          {schedule ? (
            <div className="flex flex-col gap-3">
              {SCHEDULE_ROWS.map(([task, hoursKey, enabledKey]) => (
                <div key={task} className="flex items-center gap-3">
                  <Toggle
                    checked={Boolean(schedule[enabledKey])}
                    onChange={(v) =>
                      void saveSchedule({ [enabledKey]: v } as Partial<ScheduleState>)
                    }
                    label={TASK_LABELS[task] ?? task}
                  />
                  <span className="w-40 text-xs">{TASK_LABELS[task]}</span>
                  <input
                    className="input flex-1"
                    value={schedule[hoursKey].join(',')}
                    onChange={(e) =>
                      setSchedule({
                        ...schedule,
                        [hoursKey]: e.target.value
                          .split(',')
                          .map((v) => Number(v.trim()))
                          .filter((v) => Number.isInteger(v) && v >= 0 && v <= 23),
                      } as ScheduleState)
                    }
                    onBlur={() =>
                      void saveSchedule({ [hoursKey]: schedule[hoursKey] } as Partial<ScheduleState>)
                    }
                    placeholder="9,21"
                  />
                </div>
              ))}
              <p className="text-[11px] text-[hsl(var(--ink-dim))]">
                小时列表用逗号分隔（本地时区整点），失焦即保存并热生效
              </p>
            </div>
          ) : (
            <EmptyState message="调度器未启动" />
          )}
        </Card>

        <Card className="p-0">
          <h2 className="px-4 py-3 text-sm font-semibold">最近任务执行记录</h2>
          <div className="scroll-thin max-h-96 overflow-auto">
            <table className="data">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>任务</th>
                  <th>域</th>
                  <th>账号</th>
                  <th>结果</th>
                </tr>
              </thead>
              <tbody>
                {(data?.runs ?? []).map((run) => (
                  <tr key={run.id}>
                    <td className="whitespace-nowrap text-[11px]">{formatTime(run.ts)}</td>
                    <td className="text-[11px]">{TASK_LABELS[run.task] ?? run.task}</td>
                    <td className="text-[11px]">{run.realm}</td>
                    <td className="text-[11px]">{run.account}</td>
                    <td>
                      <div className="flex flex-col gap-0.5">
                        <Badge
                          className={
                            run.ok
                              ? 'bg-[hsl(var(--signal)/0.2)] text-[hsl(var(--signal))]'
                              : 'bg-[hsl(var(--fault)/0.2)] text-[hsl(var(--fault))]'
                          }
                        >
                          {run.ok ? '成功' : '失败'}
                        </Badge>
                        {run.detail ? (
                          <span className="text-[11px] text-[hsl(var(--ink-dim))]">
                            {run.detail}
                          </span>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(data?.runs ?? []).length === 0 ? <EmptyState message="暂无执行记录" /> : null}
        </Card>
      </div>

      {/* ---- 限时套餐领取 ---- */}
      <Card className="mt-4 p-0">
        <div className="flex flex-wrap items-center gap-2 px-4 py-3">
          <h2 className="text-sm font-semibold">限时套餐领取</h2>
          {preview?.status ? (
            <Badge
              className={
                preview.status.enabled
                  ? 'bg-[hsl(var(--signal)/0.2)] text-[hsl(var(--signal))]'
                  : 'bg-[hsl(var(--well))]'
              }
            >
              {preview.status.enabled
                ? `自动轮 ${preview.status.round_interval_seconds}s`
                : '自动轮关闭'}
            </Badge>
          ) : null}
          <Button variant="outline" onClick={() => void runClaim()}>
            执行领取轮
          </Button>
        </div>

        <div className="scroll-thin overflow-x-auto">
          <table className="data has-actions">
            <thead>
              <tr>
                <th>账号</th>
                <th>可领套餐</th>
                <th>名额等待</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {(preview?.accounts ?? []).map((acc) => (
                <tr key={acc.uid}>
                  <td className="text-[11px]">{acc.nickname || acc.uid}</td>
                  <td>
                    {acc.ok ? (
                      <div className="flex flex-col gap-0.5">
                        {(acc.plans ?? []).length === 0 ? (
                          <span className="text-[11px] text-[hsl(var(--ink-dim))]">
                            上游无投放
                          </span>
                        ) : null}
                        {(acc.plans ?? []).map((plan) => (
                          <span key={plan.plan_id} className="text-[11px]">
                            {plan.name || plan.plan_id}
                            {plan.grants.length
                              ? ` · ${plan.grants
                                  .map((g) => `${g.name} ${formatNumber(g.units)}`)
                                  .join(' / ')}`
                              : ''}
                          </span>
                        ))}
                      </div>
                    ) : (
                      <span className="text-[11px] text-[hsl(var(--fault))]">{acc.error}</span>
                    )}
                  </td>
                  <td className="text-[11px]">
                    {Object.entries(acc.holds ?? {}).map(([planId, at]) => (
                      <div key={planId}>
                        {planId} → {formatTime(Math.floor(at / 1000))}
                      </div>
                    ))}
                  </td>
                  <td>
                    <Button size="sm" variant="outline" onClick={() => void runClaim(acc.uid)}>
                      领取
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {(preview?.accounts ?? []).length === 0 ? (
          <EmptyState message="暂无账号或领取服务未启用" />
        ) : null}

        {history.length > 0 ? (
          <div className="border-t border-[hsl(var(--line))]">
            <h3 className="px-4 py-2 text-xs font-semibold text-[hsl(var(--ink-dim))]">
              领取历史
            </h3>
            <div className="scroll-thin max-h-64 overflow-auto">
              <table className="data">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>账号</th>
                    <th>套餐</th>
                    <th>结果</th>
                  </tr>
                </thead>
                <tbody>
                  {history.map((row) => (
                    <tr key={row.id}>
                      <td className="whitespace-nowrap text-[11px]">{formatTime(row.ts)}</td>
                      <td className="text-[11px]">{row.account || row.uid}</td>
                      <td className="text-[11px]">{row.plan_name || row.plan_id}</td>
                      <td>
                        <div className="flex flex-col gap-0.5">
                          <Badge
                            className={
                              row.outcome === 'claimed'
                                ? 'bg-[hsl(var(--signal)/0.2)] text-[hsl(var(--signal))]'
                                : 'bg-[hsl(var(--well))]'
                            }
                          >
                            {CLAIM_OUTCOME_LABEL[row.outcome] ?? row.outcome}
                          </Badge>
                          {row.detail ? (
                            <span className="text-[11px] text-[hsl(var(--ink-dim))]">
                              {row.detail}
                            </span>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
      </Card>
      {node}
    </>
  );
}
