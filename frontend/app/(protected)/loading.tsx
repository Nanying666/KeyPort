/**
 * 路由级加载态（Next.js 栈指南：loading.tsx）。
 * 用结构性骨架屏而非 spinner——骨架的形状贴合真实内容布局，
 * 避免数据到达时布局跳变（CLS）。
 */
function Bar({ w }: { w: string }) {
  return <div className="animate-pulse rounded bg-[hsl(var(--well))]" style={{ width: w, height: 10 }} />;
}

export default function ProtectedLoading() {
  return (
    <div aria-busy="true" aria-label="加载中">
      <div className="mb-5">
        <div className="animate-pulse rounded bg-[hsl(var(--well))]" style={{ width: 120, height: 17 }} />
        <div className="mt-2 animate-pulse rounded bg-[hsl(var(--well))]" style={{ width: 260, height: 11 }} />
      </div>

      <div className="mb-4 grid gap-2.5 [grid-template-columns:repeat(auto-fit,minmax(148px,1fr))]">
        {Array.from({ length: 6 }).map((_, i) => (
          <div
            key={i}
            className="rounded-md border border-[hsl(var(--edge))] bg-[hsl(var(--panel))] px-3 py-2.5"
          >
            <div className="animate-pulse rounded bg-[hsl(var(--well))]" style={{ width: '45%', height: 9 }} />
            <div className="mt-2 animate-pulse rounded bg-[hsl(var(--well))]" style={{ width: '65%', height: 20 }} />
          </div>
        ))}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {Array.from({ length: 2 }).map((_, i) => (
          <div key={i} className="card p-4">
            <div className="animate-pulse rounded bg-[hsl(var(--well))]" style={{ width: '30%', height: 10 }} />
            <div className="mt-4 flex flex-col gap-3">
              {Array.from({ length: 5 }).map((__, j) => (
                <div key={j} className="flex items-center justify-between gap-4">
                  <Bar w={`${40 + ((j * 13) % 30)}%`} />
                  <Bar w={`${20 + ((j * 7) % 20)}%`} />
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}