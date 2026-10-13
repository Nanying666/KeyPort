'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError, type ModelEntry } from '@/lib/api';
import { PageHeader } from '@/components/shell';
import { Badge, Button, Card, Field, EmptyState } from '@/components/ui';
import { useToast } from '@/components/use-toast';
import { formatNumber } from '@/lib/utils';

interface Turn {
  role: 'user' | 'assistant';
  content: string;
  reasoning?: string;
  credits?: number;
  tokens?: number;
  model?: string;
  error?: string;
}

export default function PlaygroundPage() {
  const [models, setModels] = useState<ModelEntry[]>([]);
  const [model, setModel] = useState('');
  const [effort, setEffort] = useState('');
  const [realm, setRealm] = useState('');
  const [input, setInput] = useState('');
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const { show, node } = useToast();

  const loadModels = useCallback(async () => {
    try {
      const res = await api.get<{ models: ModelEntry[]; realms: string[] }>(
        '/api/playground/models',
      );
      setModels(res.models);
      setModel((prev) => prev || res.models[0]?.id || '');
    } catch (err) {
      show(err instanceof ApiError ? err.message : '加载模型失败', 'err');
    }
  }, [show]);

  useEffect(() => {
    void loadModels();
  }, [loadModels]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [turns]);

  const selected = models.find((m) => m.id === model);
  const sessionCredits = turns.reduce((sum, t) => sum + (t.credits ?? 0), 0);
  const sessionTokens = turns.reduce((sum, t) => sum + (t.tokens ?? 0), 0);

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    const history: Turn[] = [...turns, { role: 'user', content: text }];
    setTurns(history);
    setInput('');
    setBusy(true);
    try {
      const res = await api.post<{
        content: string;
        reasoning: string;
        credits: number;
        usage: { total_tokens?: number };
        model: string;
      }>('/api/playground/chat', {
        model,
        realm,
        reasoning_effort: effort || undefined,
        messages: history.map((t) => ({ role: t.role, content: t.content })),
      });
      setTurns([
        ...history,
        {
          role: 'assistant',
          content: res.content,
          reasoning: res.reasoning,
          credits: res.credits,
          tokens: res.usage?.total_tokens ?? 0,
          model: res.model,
        },
      ]);
    } catch (err) {
      setTurns([
        ...history,
        {
          role: 'assistant',
          content: '',
          error: err instanceof Error ? err.message : '请求失败',
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title="聊天测试台"
        description="不建密钥直接试调模型，请求经管理端登录态转发到上游（与下游同一账号池），会真实扣积分"
        actions={
          <>
            <select className="input w-auto" value={realm} onChange={(e) => setRealm(e.target.value)}>
              <option value="">不限域</option>
              <option value="cn">cn</option>
              <option value="global">global</option>
              <option value="zai">zai</option>
            </select>
            <select className="input w-auto" value={model} onChange={(e) => setModel(e.target.value)}>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.display_name ? `${m.display_name} (${m.id})` : m.id}
                </option>
              ))}
            </select>
            <select className="input w-auto" value={effort} onChange={(e) => setEffort(e.target.value)}>
              <option value="">默认思考强度</option>
              {(selected?.reasoning_efforts ?? ['minimal', 'low', 'medium', 'high']).map((e) => (
                <option key={e} value={e}>
                  {e}
                </option>
              ))}
            </select>
            <Button variant="outline" onClick={() => setTurns([])}>
              清空会话
            </Button>
          </>
        }
      />

      {selected ? (
        <div className="mb-3 flex flex-wrap gap-2 text-[11px]">
          {selected.context_window ? (
            <Badge className="bg-[hsl(var(--well))]">
              上下文 {formatNumber(selected.context_window)}
            </Badge>
          ) : null}
          {selected.max_output_tokens ? (
            <Badge className="bg-[hsl(var(--well))]">
              最大输出 {formatNumber(selected.max_output_tokens)}
            </Badge>
          ) : null}
          {selected.series ? (
            <Badge className="bg-[hsl(var(--well))]">{selected.series}</Badge>
          ) : null}
          {selected.supports_images ? (
            <Badge className="bg-[hsl(var(--well))]">支持图片</Badge>
          ) : null}
        </div>
      ) : null}

      <Card className="mb-3 flex h-[52vh] flex-col gap-3 overflow-y-auto">
        {turns.length === 0 ? (
          <EmptyState message="输入消息开始试调；每条回答会标注本次扣费与 Token 数" />
        ) : null}
        {turns.map((turn, i) => (
          <div
            key={i}
            className={
              turn.role === 'user'
                ? 'ml-auto max-w-[80%] rounded-lg bg-[hsl(var(--signal))] px-3 py-2 text-sm text-[hsl(var(--surface))]'
                : 'mr-auto max-w-[85%] rounded-lg bg-[hsl(var(--well))] px-3 py-2 text-sm'
            }
          >
            {turn.reasoning ? (
              <details className="mb-2 text-[11px] opacity-80">
                <summary className="cursor-pointer">思考过程</summary>
                <pre className="mt-1 whitespace-pre-wrap font-sans">{turn.reasoning}</pre>
              </details>
            ) : null}
            <div className="whitespace-pre-wrap break-words">
              {turn.error ? (
                <span className="text-[hsl(var(--fault))]">{turn.error}</span>
              ) : (
                turn.content || '（空响应）'
              )}
            </div>
            {turn.role === 'assistant' && !turn.error ? (
              <div className="mt-1.5 border-t border-[hsl(var(--edge))] pt-1 text-[10px] opacity-70">
                {/* 上游不回传单次积分，仅在有值时展示，避免恒显「扣费 0」。 */}
                {turn.credits ? `本次扣费 ${turn.credits} 积分 · ` : ''}
                {formatNumber(turn.tokens ?? 0)} Token
                {turn.model ? ` · ${turn.model}` : ''}
              </div>
            ) : null}
          </div>
        ))}
        <div ref={bottomRef} />
      </Card>

      <Card className="flex items-end gap-2">
        <div className="flex-1">
          <Field label="消息">
            <textarea
              className="input h-20 resize-none"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void send();
              }}
              placeholder="Ctrl/Cmd + Enter 发送"
            />
          </Field>
        </div>
        <Button variant="primary" disabled={busy || !input.trim()} onClick={() => void send()}>
          {busy ? '生成中…' : '发送'}
        </Button>
      </Card>

      <div className="fixed bottom-4 right-4 rounded-lg bg-[hsl(var(--panel))] px-3 py-2 text-[11px] shadow-lg ring-1 ring-[hsl(var(--edge))]">
        本次会话累计消耗：
        {sessionCredits ? (
          <span className="font-semibold text-[hsl(var(--signal))]">{sessionCredits} 积分 · </span>
        ) : null}
        {formatNumber(sessionTokens)} Token
      </div>
      {node}
    </>
  );
}
