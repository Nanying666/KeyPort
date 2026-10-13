'use client';

import { cn } from '@/lib/utils';

export function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return <div className={cn('card p-4', className)}>{children}</div>;
}

/** 面板标题栏：全大写小标签 + 可选右侧动作，统一各页区块头。 */
export function PanelHeader({
  title,
  hint,
  action,
  className,
}: {
  title: string;
  hint?: string;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('mb-3 flex items-center justify-between gap-3', className)}>
      <div className="flex items-baseline gap-2">
        <h2 className="eyebrow">{title}</h2>
        {hint ? <span className="text-[10px] text-[hsl(var(--ink-faint))]">{hint}</span> : null}
      </div>
      {action}
    </div>
  );
}

export function StatCard({
  label,
  value,
  hint,
  tone = 'default',
  className,
}: {
  label: string;
  value: string | number;
  hint?: string;
  tone?: 'default' | 'success' | 'warning' | 'danger' | 'live';
  className?: string;
}) {
  const toneCls = {
    default: 'text-[hsl(var(--ink))]',
    success: 'text-[hsl(var(--signal))]',
    warning: 'text-[hsl(var(--caution))]',
    danger: 'text-[hsl(var(--fault))]',
    live: 'text-[hsl(var(--live))]',
  }[tone];

  return (
    <div
      className={cn(
        'rounded-md border border-[hsl(var(--edge))] bg-[hsl(var(--panel))] px-3 py-2.5',
        className,
      )}
    >
      <div className="eyebrow mb-1.5 truncate">{label}</div>
      <div className={cn('mono text-[21px] font-semibold leading-none', toneCls)}>{value}</div>
      {hint ? (
        <div className="mt-1.5 text-[10px] text-[hsl(var(--ink-faint))]">{hint}</div>
      ) : null}
    </div>
  );
}

/** 指标卡片栅格：列宽自适应，避免 flex-1 在少量卡片时各撑满整行。 */
export function StatGrid({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        'mb-4 grid gap-2.5 [grid-template-columns:repeat(auto-fit,minmax(148px,1fr))]',
        className,
      )}
    >
      {children}
    </div>
  );
}

export function Badge({
  children,
  className,
  tone,
}: {
  children: React.ReactNode;
  className?: string;
  tone?: 'ok' | 'warn' | 'bad' | 'live' | 'neutral';
}) {
  const toneCls = tone
    ? {
        ok: 'bg-[hsl(var(--signal)/0.14)] text-[hsl(var(--signal))]',
        warn: 'bg-[hsl(var(--caution)/0.16)] text-[hsl(var(--caution))]',
        bad: 'bg-[hsl(var(--fault)/0.16)] text-[hsl(var(--fault))]',
        live: 'bg-[hsl(var(--live)/0.16)] text-[hsl(var(--live))]',
        neutral: 'bg-[hsl(var(--well))] text-[hsl(var(--ink-dim))]',
      }[tone]
    : undefined;
  return <span className={cn('badge', toneCls, className)}>{children}</span>;
}

/** 指示灯：池子里一个账号的健康状态。唯一允许的光效。 */
export function Lamp({
  tone,
  label,
  className,
}: {
  tone: 'ok' | 'warn' | 'bad' | 'idle';
  label?: string;
  className?: string;
}) {
  const cls = { ok: 'lamp-ok', warn: 'lamp-warn', bad: 'lamp-bad', idle: 'lamp-idle' }[tone];
  return <span className={cn('lamp', cls, className)} role="img" aria-label={label ?? tone} />;
}

/** 配额液位：把「已用 / 总额」画成细长条，像油量表而非纯文本。 */
export function Meter({
  value,
  max,
  className,
}: {
  value: number;
  max: number;
  className?: string;
}) {
  // max<=0 表示不限量：满格但用中性色，不误报成“告警”。
  const unlimited = !(max > 0);
  const ratio = unlimited ? 1 : Math.min(1, Math.max(0, value / max));
  const tone = unlimited ? 'idle' : ratio >= 0.9 ? 'bad' : ratio >= 0.7 ? 'warn' : 'ok';
  return (
    <span className={cn('meter', className)} role="presentation">
      <i
        className={tone === 'warn' ? 'warn' : tone === 'bad' ? 'bad' : undefined}
        style={{
          width: `${ratio * 100}%`,
          background: tone === 'idle' ? 'hsl(var(--ink-faint) / 0.5)' : undefined,
        }}
      />
    </span>
  );
}

export function Button({
  children,
  variant = 'ghost',
  size = 'md',
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'ghost' | 'danger' | 'outline';
  size?: 'sm' | 'md';
}) {
  return (
    <button
      className={cn('btn', `btn-${variant}`, size === 'sm' && 'h-7 px-2 text-[11px]', className)}
      {...props}
    >
      {children}
    </button>
  );
}

/** 图标按钮：强制 aria-label，避免出现只有图标的无名按钮。 */
export function IconButton({
  label,
  children,
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={cn(
        'inline-flex h-7 w-7 items-center justify-center rounded border border-transparent',
        'text-[hsl(var(--ink-dim))] transition-colors hover:border-[hsl(var(--edge))]',
        'hover:bg-[hsl(var(--well))] hover:text-[hsl(var(--ink))]',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--signal)/0.6)]',
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
}

/** 分段控件：用于时间范围等互斥切换。 */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  className,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  className?: string;
}) {
  return (
    <div className={cn('seg', className)} role="group">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          data-on={o.value === value}
          aria-pressed={o.value === value}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[11px] font-medium text-[hsl(var(--ink-dim))]">{label}</span>
      {children}
      {hint ? <span className="text-[10px] text-[hsl(var(--ink-faint))]">{hint}</span> : null}
    </label>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full',
        'transition-colors duration-200 ease-out',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--signal)/0.6)]',
        checked
          ? 'bg-[hsl(var(--signal))]'
          : 'border border-[hsl(var(--edge))] bg-[hsl(var(--well))]',
        disabled && 'cursor-not-allowed opacity-50',
      )}
    >
      {/* 必须显式 left-0.5 锚定：absolute 子元素默认取「静态位置」，
          而 <button> 的 text-align:center 会把它定在轨道中点，
          再叠加位移就会冲出轨道（曾表现为滑块跑到轨道右侧外面）。
          行程 = 轨道 36px − 滑块 16px − 左右内边距各 2px = 16px，即 translate-x-4。 */}
      <span
        className={cn(
          'pointer-events-none absolute left-0.5 top-0.5 h-4 w-4 rounded-full shadow-sm',
          'transition-transform duration-200 ease-out',
          checked
            ? 'translate-x-4 bg-[hsl(var(--surface))]'
            : 'translate-x-0 bg-[hsl(var(--ink-faint))]',
        )}
      />
    </button>
  );
}

export function EmptyState({
  message,
  action,
}: {
  message: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2.5 py-10 text-center">
      <p className="text-xs text-[hsl(var(--ink-faint))]">{message}</p>
      {action}
    </div>
  );
}

/** 结构性骨架屏：加载态用它，不用 spinner。 */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn('animate-pulse rounded bg-[hsl(var(--well))]', className)}
      aria-hidden="true"
    />
  );
}

export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 flex items-center justify-center bg-black/60 p-4"
      style={{ zIndex: 'var(--z-modal)' }}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="card w-full max-w-lg p-4" role="dialog" aria-modal="true" aria-label={title}>
        <div className="mb-3 flex items-center justify-between gap-3">
          <h3 className="text-[13px] font-semibold text-[hsl(var(--ink))]">{title}</h3>
          <IconButton label="关闭" onClick={onClose}>
            ✕
          </IconButton>
        </div>
        <div className="scroll-thin max-h-[60vh] overflow-y-auto">{children}</div>
        {footer ? <div className="mt-4 flex justify-end gap-2">{footer}</div> : null}
      </div>
    </div>
  );
}

/** 破坏性操作确认：替代 window.confirm，键盘可达且样式一致。 */
export function AlertDialog({
  open,
  title,
  message,
  confirmLabel = '确认',
  cancelLabel = '取消',
  danger = true,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 flex items-center justify-center bg-black/60 p-4"
      style={{ zIndex: 'var(--z-modal)' }}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div
        className="card w-full max-w-sm p-4"
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
      >
        <h3 className="text-[13px] font-semibold text-[hsl(var(--ink))]">{title}</h3>
        <p className="mt-2 text-xs leading-relaxed text-[hsl(var(--ink-dim))]">{message}</p>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="outline" onClick={onCancel}>
            {cancelLabel}
          </Button>
          <Button variant={danger ? 'danger' : 'primary'} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function Toast({ message, tone }: { message: string; tone: 'ok' | 'err' }) {
  if (!message) return null;
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'fixed right-5 rounded-md border bg-[hsl(var(--panel))] px-4 py-2.5 text-xs shadow-lg',
        tone === 'ok'
          ? 'border-[hsl(var(--signal)/0.5)] text-[hsl(var(--signal))]'
          : 'border-[hsl(var(--fault)/0.5)] text-[hsl(var(--fault))]',
      )}
      style={{
        zIndex: 'var(--z-toast)',
        bottom: 'calc(1.25rem + env(safe-area-inset-bottom))',
      }}
    >
      {message}
    </div>
  );
}
