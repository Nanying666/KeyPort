'use client';

import { useState } from 'react';
import { api, ApiError } from '@/lib/api';
import { Button, Field, Modal } from '@/components/ui';

/** OAuth 一键登录：取授权链接 → 浏览器登录 → 轮询 token → 自动入库。 */
export function OAuthModal({
  open,
  onClose,
  onDone,
  realms,
}: {
  open: boolean;
  onClose: () => void;
  onDone: () => void | Promise<void>;
  realms: string[];
}) {
  const [realm, setRealm] = useState('cn');
  const [authUrl, setAuthUrl] = useState('');
  const [state, setState] = useState('');
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);

  async function start() {
    setBusy(true);
    setStatus('正在获取授权链接…');
    try {
      const res = await api.post<{ state: string; auth_url: string }>('/api/oauth/start', { realm });
      setState(res.state);
      setAuthUrl(res.auth_url);
      setStatus('请在浏览器完成登录，然后点击「我已登录」');
      window.open(res.auth_url, '_blank', 'noopener');
    } catch (err) {
      setStatus(err instanceof ApiError ? err.message : '获取授权链接失败');
    } finally {
      setBusy(false);
    }
  }

  async function poll() {
    setBusy(true);
    setStatus('正在校验登录结果…');
    try {
      await api.post('/api/oauth/poll', { state });
      setStatus('登录成功，账号已入库');
      await onDone();
      window.setTimeout(onClose, 800);
    } catch (err) {
      if (err instanceof ApiError && err.status === 202) {
        setStatus('尚未检测到登录完成，请确认浏览器已登录后重试');
      } else {
        setStatus(err instanceof ApiError ? err.message : '校验失败');
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      title="OAuth 一键登录"
      onClose={onClose}
      footer={
        <>
          <Button variant="outline" onClick={onClose}>
            关闭
          </Button>
          {authUrl ? (
            <Button variant="primary" disabled={busy} onClick={() => void poll()}>
              我已登录
            </Button>
          ) : (
            <Button variant="primary" disabled={busy} onClick={() => void start()}>
              获取授权链接
            </Button>
          )}
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <Field label="账号域" hint="国内版 codebuddy.cn / 国际版 workbuddy.ai / z.ai">
          <select className="input" value={realm} onChange={(e) => setRealm(e.target.value)}>
            {realms.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </Field>
        {authUrl ? (
          <Field label="授权链接">
            <input className="input" readOnly value={authUrl} onFocus={(e) => e.target.select()} />
          </Field>
        ) : null}
        {status ? <p className="text-xs text-[hsl(var(--ink-dim))]">{status}</p> : null}
      </div>
    </Modal>
  );
}


/** API Key 纳管（z.ai / 兼容 OpenAI 的第三方端点）。 */
export function ApiKeyAccountModal({
  open,
  onClose,
  onDone,
  realms,
}: {
  open: boolean;
  onClose: () => void;
  onDone: () => void | Promise<void>;
  realms: string[];
}) {
  const [realm, setRealm] = useState('zai');
  const [apiKey, setApiKey] = useState('');
  const [name, setName] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    setError('');
    try {
      await api.post('/api/accounts/apikey', { realm, api_key: apiKey, name, base_url: baseUrl });
      await onDone();
      setApiKey('');
      setName('');
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '纳管失败');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      title="API Key 纳管"
      onClose={onClose}
      footer={
        <>
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" disabled={busy || !apiKey} onClick={() => void submit()}>
            纳管
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <Field label="账号域">
          <select className="input" value={realm} onChange={(e) => setRealm(e.target.value)}>
            {realms.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </Field>
        <Field label="API Key">
          <input
            className="input"
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
        </Field>
        <Field label="备注名">
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="自定义 Base URL" hint="留空则用该域内置端点">
          <input className="input" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
        </Field>
        {error ? <p className="text-xs text-[hsl(var(--fault))]">{error}</p> : null}
      </div>
    </Modal>
  );
}


/** 账号池导入（粘贴凭证 / 选择文件）。 */
export function ImportModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  onDone: () => void | Promise<void>;
}) {
  const [text, setText] = useState('');
  const [message, setMessage] = useState('');
  const [failed, setFailed] = useState<{ index: string | number; reason: string }[]>([]);
  const [busy, setBusy] = useState(false);

  function reset() {
    setText('');
    setMessage('');
    setFailed([]);
  }

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setText(await file.text());
    setMessage(`已读取 ${file.name}`);
    setFailed([]);
    e.target.value = ''; // 允许重复选择同一文件
  }

  async function submit() {
    setBusy(true);
    setMessage('');
    setFailed([]);
    try {
      // 后端同时接受 accounts 数组与 text 原文，这里统一用 text：
      // 导出的整份文件、JSONL、单条凭证都能识别。
      const res = await api.post<{
        imported: number;
        skipped: number;
        failed: { index: string | number; reason: string }[];
      }>('/api/accounts/import', { text });

      if (res.imported > 0) {
        setMessage(`成功导入 ${res.imported} 个账号${res.skipped ? `，跳过 ${res.skipped} 条` : ''}`);
        await onDone();
      } else {
        setMessage(`未导入任何账号（跳过 ${res.skipped} 条）`);
      }
      setFailed(res.failed ?? []);
    } catch (err) {
      setMessage(err instanceof ApiError ? err.message : '导入失败');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      title="导入账号凭证"
      onClose={() => {
        reset();
        onClose();
      }}
      footer={
        <>
          <Button
            variant="outline"
            onClick={() => {
              reset();
              onClose();
            }}
          >
            关闭
          </Button>
          <Button variant="primary" disabled={busy || !text.trim()} onClick={() => void submit()}>
            {busy ? '导入中…' : '导入'}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <Field
          label="凭证内容"
          hint="支持：ZCode 账号包（zcode-accounts-bundle）、导出文件原文、单条凭证对象、每行一个 JSON（JSONL）"
        >
          <textarea
            className="input h-48 font-mono text-[11px]"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={'{"auth":{"accessToken":"..."},"account":{"uid":"..."}}'}
          />
        </Field>
        <Field label="或选择文件">
          <input
            className="input"
            type="file"
            accept=".json,.jsonl,.txt,application/json"
            onChange={(e) => void onFile(e)}
          />
        </Field>
        {message ? <p className="text-xs">{message}</p> : null}
        {failed.length ? (
          <div className="rounded-md border border-[hsl(var(--edge))] p-2">
            <p className="mb-1 text-[11px] font-medium text-[hsl(var(--fault))]">
              失败明细（{failed.length} 条）
            </p>
            <ul className="scroll-thin max-h-32 overflow-y-auto text-[11px]">
              {failed.map((f, i) => (
                <li key={i} className="flex gap-2">
                  <span className="text-[hsl(var(--ink-dim))]">#{String(f.index)}</span>
                  <span>{f.reason}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </Modal>
  );
}
