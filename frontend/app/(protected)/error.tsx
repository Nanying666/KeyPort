'use client';

/**
 * 路由级错误边界（Next.js 栈指南：error.tsx）。
 * 没有它时，一次渲染异常会让整个管控台白屏，用户只能刷新。
 * 这里给出「重试 / 回概览」两条出路，并暴露 requestId 便于查后端日志。
 */
export default function ProtectedError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className="flex h-dvh items-center justify-center p-6">
      <div className="card w-full max-w-md p-5">
        <h1 className="text-[15px] font-semibold text-[hsl(var(--ink))]">页面出错了</h1>
        <p className="mt-2 text-xs leading-relaxed text-[hsl(var(--ink-dim))]">
          这一页渲染失败，其余功能不受影响。可先重试；若持续失败，请联系管理员并附上错误标识。
        </p>
        {error.digest ? (
          <p className="mono mt-3 rounded border border-[hsl(var(--edge))] bg-[hsl(var(--well))] px-2 py-1.5 text-[11px] text-[hsl(var(--ink-faint))]">
            {error.digest}
          </p>
        ) : null}
        <div className="mt-4 flex gap-2">
          <button onClick={reset} className="btn btn-primary">
            重试
          </button>
          <a href="/" className="btn btn-outline">
            回到概览
          </a>
        </div>
      </div>
    </div>
  );
}