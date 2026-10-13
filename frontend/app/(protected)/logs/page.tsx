'use client';

import { useCallback, useEffect, useState } from 'react';
import { api, ApiError, type RequestLog } from '@/lib/api';
import { PageHeader } from '@/components/shell';
import { AlertDialog, Badge, Button, Card, EmptyState } from '@/components/ui';
import { useToast } from '@/components/use-toast';
import { formatMs, formatNumber, formatTime } from '@/lib/utils';

export default function LogsPage() {
  const [logs, setLogs] = useState<RequestLog[]>([]);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const { show, node } = useToast();

  const load = useCallback(async () => {
    try {
      const res = await api.get<{ logs: RequestLog[] }>('/api/logs?limit=100');
      setLogs(res.logs);
    } catch (err) {
      show(err instanceof ApiError ? err.message : '加载失败', 'err');
    }
  }, [show]);

  useEffect(() => {
    void load();
    if (!autoRefresh) return;
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [load, autoRefresh]);

  async function prune() {
    setConfirmOpen(false);
    try {
      const res = await api.del<{ removed: number }>('/api/logs?keep_days=30');
      show(`已清理 ${res.removed} 条`);
      await load();
    } catch (err) {
      show(err instanceof ApiError ? err.message : '清理失败', 'err');
    }
  }

  return (
    <>
      <PageHeader
        title="调用日志"
        description="实时请求流水：调用密钥（key=）、首字延迟 TTFB、总耗时与 Token 计量"
        actions={
          <>
            <Button variant="outline" onClick={() => setAutoRefresh((v) => !v)}>
              {autoRefresh ? '暂停刷新' : '自动刷新'}
            </Button>
            <Button variant="outline" onClick={() => void load()}>
              立即刷新
            </Button>
            <Button variant="danger" onClick={() => setConfirmOpen(true)}>
              清理旧日志
            </Button>
          </>
        }
      />

      <Card className="p-0">
        <div className="scroll-thin max-h-[70vh] overflow-auto">
          <table className="data">
            <thead className="sticky top-0 bg-[hsl(var(--well))]">
              <tr>
                <th>时间</th>
                <th>key=</th>
                <th>协议</th>
                <th>模型</th>
                <th>账号</th>
                <th>状态</th>
                <th>首字</th>
                <th>总耗时</th>
                <th>Token</th>
                <th>积分</th>
                <th>重试</th>
                <th>IP</th>
                <th>错误</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((log) => (
                <tr key={log.id}>
                  <td className="whitespace-nowrap text-[11px]">{formatTime(log.ts)}</td>
                  <td className="text-[11px]">{log.key_name || '—'}</td>
                  <td>
                    <Badge tone="neutral">{log.protocol}</Badge>
                  </td>
                  <td className="text-[11px]">{log.model}</td>
                  <td className="text-[11px]">
                    {log.account_uid ? (
                      <span title={log.account_uid}>
                        {log.realm}/{log.account_name || log.account_uid.slice(0, 8)}
                      </span>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td>
                    <Badge tone={log.status < 400 ? 'ok' : 'bad'}>{log.status}</Badge>
                  </td>
                  <td className="mono">{formatMs(log.ttfb_ms)}</td>
                  <td className="mono">{formatMs(log.total_ms)}</td>
                  <td className="mono">{formatNumber(log.total_tokens)}</td>
                  <td className="mono">{log.credits || '—'}</td>
                  <td className="mono">{log.retries || ''}</td>
                  <td className="text-[11px]">{log.client_ip || '—'}</td>
                  <td className="max-w-[200px] truncate text-[11px] text-[hsl(var(--fault))]">
                    {log.error || ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {logs.length === 0 ? <EmptyState message="暂无调用记录" /> : null}
      </Card>
      <AlertDialog
        open={confirmOpen}
        title="清理旧日志"
        message="将删除 30 天前的全部调用记录，此操作不可撤销。"
        confirmLabel="清理"
        onConfirm={() => void prune()}
        onCancel={() => setConfirmOpen(false)}
      />
      {node}
    </>
  );
}
