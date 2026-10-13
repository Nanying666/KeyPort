'use client';

import Link from 'next/link';

/** 404：给出回主路径，不留死路。 */
export default function NotFound() {
  return (
    <div className="flex h-dvh items-center justify-center p-6">
      <div className="card w-full max-w-md p-5">
        <p className="eyebrow">404</p>
        <h1 className="mt-2 text-[15px] font-semibold text-[hsl(var(--ink))]">页面不存在</h1>
        <p className="mt-2 text-xs leading-relaxed text-[hsl(var(--ink-dim))]">
          这个地址没有对应的页面，可能是链接已失效。
        </p>
        <Link href="/" className="btn btn-primary mt-4">
          回到概览
        </Link>
      </div>
    </div>
  );
}