'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/auth';
import { ApiError } from '@/lib/api';
import { Button, Field, Lamp } from '@/components/ui';
import { Server } from 'lucide-react';

export default function LoginPage() {
  const { login, authenticated } = useAuth();
  const router = useRouter();
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  if (authenticated) {
    router.replace('/');
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await login(username, password);
      router.replace('/');
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '登录失败');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-dvh items-center justify-center p-4">
      <form
        onSubmit={submit}
        className="card w-full max-w-sm p-6"
        style={{ boxShadow: '0 0 0 1px hsl(var(--edge)), 0 18px 48px -12px rgb(0 0 0 / 0.7)' }}
      >
        <div className="mb-6 flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded bg-[hsl(var(--signal)/0.14)]">
            <Server className="h-4 w-4 text-[hsl(var(--signal))]" />
          </span>
          <div>
            <div className="text-[13px] font-semibold tracking-tight text-[hsl(var(--ink))]">
              KeyPort
            </div>
            <div className="eyebrow">管理台</div>
          </div>
        </div>
        <div className="flex flex-col gap-3">
          <Field label="用户名">
            <input
              className="input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
            />
          </Field>
          <Field label="面板密码" hint="初始密码见服务启动日志，登录后请立即修改">
            <input
              className="input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              autoFocus
            />
          </Field>
        </div>
        {error ? (
          <p className="mt-3 flex items-center gap-1.5 text-[11px] text-[hsl(var(--fault))]">
            <Lamp tone="bad" label="错误" />
            {error}
          </p>
        ) : null}
        <Button type="submit" variant="primary" className="mt-5 w-full" disabled={busy}>
          {busy ? '登录中…' : '登录'}
        </Button>
      </form>
    </div>
  );
}
