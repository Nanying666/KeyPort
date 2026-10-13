'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useAuth } from '@/lib/auth';
import { cn } from '@/lib/utils';
import {
  Activity,
  BarChart3,
  Bot,
  KeyRound,
  LayoutDashboard,
  ListTree,
  LogOut,
  Server,
  Settings,
  Users,
} from 'lucide-react';

const NAV = [
  { href: '/', label: '概览', icon: LayoutDashboard },
  { href: '/accounts', label: '账号管理', icon: Users },
  { href: '/keys', label: '密钥管理', icon: KeyRound },
  { href: '/logs', label: '调用日志', icon: ListTree },
  { href: '/usage', label: '用量统计', icon: BarChart3 },
  { href: '/tasks', label: '任务调度', icon: Activity },
  { href: '/playground', label: '聊天测试台', icon: Bot },
  { href: '/settings', label: '系统设置', icon: Settings },
];


export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { logout } = useAuth();

  return (
    <div className="flex h-dvh overflow-hidden">
      <aside className="flex w-56 shrink-0 flex-col border-r border-[hsl(var(--edge))] bg-[hsl(var(--rail))]">
        <div className="flex items-center gap-2 px-4 py-4">
          <span className="flex h-6 w-6 items-center justify-center rounded bg-[hsl(var(--signal)/0.14)]">
            <Server className="h-3.5 w-3.5 text-[hsl(var(--signal))]" />
          </span>
          <span className="text-[13px] font-semibold tracking-tight text-[hsl(var(--ink))]">
            KeyPort
          </span>
        </div>
        <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = href === '/' ? pathname === '/' : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                aria-current={active ? 'page' : undefined}
                className={cn(
                  'relative flex items-center gap-2 rounded px-3 py-1.5 text-[12.5px] transition-colors',
                  active
                    ? 'bg-[hsl(var(--well))] font-medium text-[hsl(var(--ink))]'
                    : 'text-[hsl(var(--ink-dim))] hover:bg-[hsl(var(--well)/0.6)] hover:text-[hsl(var(--ink))]',
                )}
              >
                {/* 激活指示条：导航的“当前档位”，比整块高亮更克制 */}
                <span
                  className={cn(
                    'absolute left-0 h-3.5 w-[2px] rounded-r',
                    active ? 'bg-[hsl(var(--signal))]' : 'bg-transparent',
                  )}
                  aria-hidden="true"
                />
                <Icon className="h-4 w-4 shrink-0" />
                {label}
              </Link>
            );
          })}
        </nav>
        <div className="flex items-center justify-between border-t border-[hsl(var(--edge))] px-3 py-2">
          <button
            onClick={() => void logout()}
            className="btn btn-ghost h-7 px-2 text-[11px]"
            aria-label="退出登录"
          >
            <LogOut className="h-3.5 w-3.5" />
            退出
          </button>
          <span className="mono text-[10px] text-[hsl(var(--ink-faint))]">v0.1.0</span>
        </div>
      </aside>
      <main className="scroll-thin flex-1 overflow-y-auto p-5">{children}</main>
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-[17px] font-semibold tracking-tight text-[hsl(var(--ink))] text-balance">
          {title}
        </h1>
        {description ? (
          <p className="mt-1 text-[11.5px] text-[hsl(var(--ink-faint))] text-pretty">
            {description}
          </p>
        ) : null}
      </div>
      {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
    </div>
  );
}
