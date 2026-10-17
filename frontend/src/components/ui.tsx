import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from "lucide-react";
import { cx } from "../lib";

export function Badge({ tone = "neutral", children, title, className }: { tone?: "neutral" | "ok" | "warn" | "bad" | "info" | "accent"; children: ReactNode; title?: string; className?: string }) {
  const t = {
    neutral: "bg-s3 text-mute", ok: "bg-ok/15 text-ok", warn: "bg-warn/15 text-warn", bad: "bg-bad/15 text-bad",
    info: "bg-info/15 text-info", accent: "bg-accent/15 text-accent",
  }[tone];
  return <span title={title} className={cx("inline-flex items-center gap-1 h-5 px-1.5 rounded-md text-[11px] font-medium whitespace-nowrap", t, className)}>{children}</span>;
}

export function Skeleton({ className }: { className?: string }) { return <div className={cx("skeleton", className)} />; }

export function Empty({ icon, title, hint, action }: { icon?: ReactNode; title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-16 px-6 animate-fade">
      {icon && <div className="w-12 h-12 rounded-2xl bg-s2 border border-line grid place-items-center text-mute mb-4">{icon}</div>}
      <div className="font-medium">{title}</div>
      {hint && <div className="text-sm text-mute mt-1 max-w-md">{hint}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorBox({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <div className="card p-4 flex items-start gap-3 border-bad/30">
      <XCircle className="text-bad shrink-0 mt-0.5" size={18} />
      <div className="flex-1 text-sm"><div className="font-medium">Não foi possível carregar</div><div className="text-mute">{String((error as Error)?.message ?? error)}</div></div>
      {retry && <button className="btn-ghost" onClick={retry}>Tentar de novo</button>}
    </div>
  );
}

export function Progress({ value, total, tone = "accent" }: { value: number; total: number; tone?: string }) {
  const pct = total > 0 ? Math.min(100, (value / total) * 100) : 0;
  return <div className="h-1.5 rounded-full bg-s3 overflow-hidden"><div className={cx("h-full rounded-full transition-all duration-300", tone === "ok" ? "bg-ok" : "bg-accent")} style={{ width: `${pct}%` }} /></div>;
}

export function Toggle({ checked, onChange, label, hint, disabled }: { checked: boolean; onChange: (v: boolean) => void; label?: string; hint?: string; disabled?: boolean }) {
  return (
    <label className={cx("flex items-start gap-3 cursor-pointer", disabled && "opacity-50 pointer-events-none")}>
      <button type="button" role="switch" aria-checked={checked} onClick={() => onChange(!checked)}
        className={cx("relative shrink-0 w-9 h-5 rounded-full transition-colors mt-0.5", checked ? "bg-accent" : "bg-s3 border border-line")}>
        <span className={cx("absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all", checked ? "left-[18px]" : "left-0.5")} />
      </button>
      {(label || hint) && <span className="text-sm leading-5">{label}{hint && <span className="block text-xs text-mute">{hint}</span>}</span>}
    </label>
  );
}

export function Checkbox({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <button type="button" role="checkbox" aria-checked={checked} aria-label={label} onClick={(e) => { e.stopPropagation(); onChange(!checked); }}
      className={cx("w-4 h-4 rounded border grid place-items-center shrink-0 transition-colors", checked ? "bg-accent border-accent text-black" : "border-line bg-s2 hover:border-mute")}>
      {checked && <svg viewBox="0 0 12 12" className="w-3 h-3"><path d="M2.5 6.5l2.5 2.5 4.5-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>}
    </button>
  );
}

export function Modal({ open, onClose, title, children, footer, wide }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 grid place-items-center p-4 animate-fade" role="dialog" aria-modal="true">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-[2px]" onClick={onClose} />
      <div className={cx("relative card shadow-pop w-full max-h-[90vh] flex flex-col animate-rise", wide ? "max-w-3xl" : "max-w-lg")}>
        <div className="flex items-center justify-between px-5 py-4 border-b border-line">
          <h2 className="font-semibold text-[15px]">{title}</h2>
          <button className="btn-quiet !h-7 !w-7 !p-0" onClick={onClose} aria-label="Fechar"><X size={16} /></button>
        </div>
        <div className="p-5 overflow-y-auto">{children}</div>
        {footer && <div className="px-5 py-3 border-t border-line flex justify-end gap-2 flex-wrap">{footer}</div>}
      </div>
    </div>
  );
}

export function Drawer({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 animate-fade" role="dialog" aria-modal="true">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <aside className="absolute right-0 top-0 h-full w-full max-w-[640px] bg-bg border-l border-line shadow-pop flex flex-col animate-slide">
        <div className="flex items-center justify-between px-5 h-14 border-b border-line shrink-0">
          <div className="font-semibold truncate pr-3">{title}</div>
          <button className="btn-quiet !h-8 !w-8 !p-0" onClick={onClose} aria-label="Fechar"><X size={18} /></button>
        </div>
        <div className="flex-1 overflow-y-auto">{children}</div>
      </aside>
    </div>
  );
}

// ---- toasts -----------------------------------------------------------------
type Toast = { id: number; tone: "ok" | "bad" | "info" | "warn"; text: string };
const ToastCtx = createContext<(tone: Toast["tone"], text: string) => void>(() => {});
export const useToast = () => useContext(ToastCtx);
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((tone: Toast["tone"], text: string) => {
    const id = Date.now() + Math.random();
    setItems((x) => [...x, { id, tone, text }]);
    setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), tone === "bad" ? 7000 : 4000);
  }, []);
  const Icon = { ok: CheckCircle2, bad: XCircle, info: Info, warn: AlertTriangle };
  const col = { ok: "text-ok", bad: "text-bad", info: "text-info", warn: "text-warn" };
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="fixed bottom-4 right-4 z-[60] flex flex-col gap-2 w-[min(380px,calc(100vw-2rem))]" aria-live="polite">
        {items.map((t) => { const I = Icon[t.tone]; return (
          <div key={t.id} className="card shadow-pop px-4 py-3 flex gap-3 text-sm animate-rise"><I size={18} className={cx("shrink-0 mt-0.5", col[t.tone])} /><span>{t.text}</span></div>
        ); })}
      </div>
    </ToastCtx.Provider>
  );
}

export function Tabs<T extends string>({ value, onChange, items }: { value: T; onChange: (v: T) => void; items: { id: T; label: string; count?: number }[] }) {
  return (
    <div className="inline-flex p-0.5 rounded-lg bg-s2 border border-line overflow-x-auto max-w-full">
      {items.map((i) => (
        <button key={i.id} onClick={() => onChange(i.id)}
          className={cx("h-8 px-3 rounded-md text-[13px] font-medium whitespace-nowrap transition-colors", value === i.id ? "bg-s3 text-fg" : "text-mute hover:text-fg")}>
          {i.label}{i.count != null && <span className="ml-1.5 text-dim">{i.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3 mb-5">
      <div><h1 className="text-xl font-semibold tracking-tight">{title}</h1>{subtitle && <div className="text-sm text-mute mt-0.5">{subtitle}</div>}</div>
      {actions && <div className="flex items-center gap-2 flex-wrap">{actions}</div>}
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return <div className="space-y-1.5"><div className="label">{label}</div>{children}{hint && <div className="text-xs text-mute">{hint}</div>}</div>;
}
