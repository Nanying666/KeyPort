'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/auth';
import { Shell } from '@/components/shell';

export default function ProtectedLayout({ children }: { children: React.ReactNode }) {
  const { authenticated, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !authenticated) router.replace('/login');
  }, [authenticated, loading, router]);

  if (loading) {
    return (
      <div className="flex min-h-dvh items-center justify-center text-xs text-[hsl(var(--ink-faint))]">
        加载中…
      </div>
    );
  }
  if (!authenticated) return null;
  return <Shell>{children}</Shell>;
}
