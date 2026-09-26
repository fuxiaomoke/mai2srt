import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from 'react';
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, motion } from 'framer-motion';
import { AlertTriangle, Check, CheckCircle2, ChevronDown, Loader2, OctagonX, X } from 'lucide-react';

/* ------------------------------------------------------------------ Button */

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'soft' | 'ghost' | 'danger';
  busy?: boolean;
  /** hover dynamics shared across action buttons: lift + shadow + icon
   *  pop (colors untouched -- composited on top of any variant) */
  lift?: boolean;
};

export function Button({
  variant = 'primary',
  busy = false,
  lift = false,
  className = '',
  disabled,
  children,
  ...rest
}: ButtonProps) {
  const base =
    'inline-flex items-center justify-center gap-2 rounded-(--radius-s) px-4 h-9 text-[13px] font-semibold ' +
    'transition-all duration-(--dur-in) disabled:opacity-40 disabled:pointer-events-none ' +
    'focus-visible:outline-2 focus-visible:outline-primary';
  const liftCls = lift
    ? 'group hover:-translate-y-0.5 hover:shadow-[var(--shadow-1)] active:translate-y-0 ' +
      '[&>svg]:transition-transform [&>svg]:duration-(--dur-in) [&>svg]:group-hover:scale-110'
    : '';
  const styles = {
    primary:
      'bg-primary text-on-primary hover:bg-primary-hover active:brightness-95 shadow-[var(--shadow-1)]',
    // solid secondary-emphasis action: primary chip, light text; hover =
    // stronger border + lift + shadow (dynamics shared with the ghost
    // action buttons so the page feels like one system)
    soft:
      'bg-primary text-on-primary border border-primary/40 ' +
      'hover:border-primary hover:-translate-y-0.5 hover:shadow-[var(--shadow-1)] ' +
      'active:translate-y-0 active:brightness-95',
    ghost:
      'bg-transparent text-ink-1 border border-line-1 hover:border-line-2 hover:bg-hover',
    danger: 'bg-transparent text-err border border-err/40 hover:bg-err/10',
  }[variant];
  return (
    <button
      className={`${base} ${styles} ${liftCls} ${className}`}
      disabled={disabled || busy}
      {...rest}
    >
      {busy && <Loader2 size={14} strokeWidth={2} className="animate-spin" />}
      {children}
    </button>
  );
}

/* ------------------------------------------------------------------- Card */

export function Card({
  className = '',
  children,
  hover = false,
}: {
  className?: string;
  children: ReactNode;
  hover?: boolean;
}) {
  return (
    <div
      className={`glass rounded-(--radius-m) ${
        hover ? 'transition-transform duration-(--dur-in) hover:-translate-y-0.5' : ''
      } ${className}`}
    >
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------ Field */

type FieldProps = InputHTMLAttributes<HTMLInputElement> & {
  label?: string;
  hint?: string;
};

export function Field({ label, hint, className = '', ...rest }: FieldProps) {
  return (
    <label className="flex flex-col gap-1.5">
      {label && (
        <span className="text-[12px] font-medium text-ink-2">{label}</span>
      )}
      <input
        className={`glass-input h-9 px-3 text-[13px] text-ink-1 placeholder:text-ink-3 ${className}`}
        {...rest}
      />
      {hint && <span className="text-[11px] text-ink-3">{hint}</span>}
    </label>
  );
}

/* ------------------------------------------------------------------ Toggle */

export function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: string;
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className="inline-flex items-center gap-2.5"
    >
      <span
        className={`relative h-5 w-9 rounded-full border transition-colors duration-(--dur-in) ${
          checked ? 'border-primary bg-primary' : 'border-line-2 bg-transparent'
        }`}
      >
        <span
          className={`absolute top-1/2 h-3.5 w-3.5 -translate-y-1/2 rounded-full transition-[left] duration-(--dur-in) ${
            checked ? 'left-[18px] bg-on-primary' : 'left-[3px] bg-ink-3'
          }`}
        />
      </span>
      {label && <span className="text-[13px] text-ink-1">{label}</span>}
    </button>
  );
}

/* ------------------------------------------------------------------ Select */

/** Custom dropdown: the native <select> popup can't be themed (OS-drawn),
 *  so the panel is a glass-modal layer whose translucency and blur follow
 *  the user's glass sliders like every other surface.
 *
 *  The panel is PORTALED to <body> and fixed-positioned: .glass cards carry
 *  backdrop-filter, which creates a stacking context no in-card z-index can
 *  escape -- a dropdown opening near a card's bottom edge would be painted
 *  over by the NEXT card's glass background. The portal makes z compete
 *  globally instead. */
export function Select({
  value,
  onChange,
  options,
  className = '',
  placeholder = '',
  size = 'md',
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  className?: string;
  placeholder?: string;
  /** sm: compact trigger for dense tool rows (the panel stays standard) */
  size?: 'md' | 'sm';
}) {
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ left: 0, top: 0, width: 0, up: false });

  const updatePos = useCallback(() => {
    const r = btnRef.current?.getBoundingClientRect();
    if (!r) return;
    const estH = Math.min(options.length * 37 + 12, 264);
    const spaceBelow = window.innerHeight - r.bottom - 8;
    const up = spaceBelow < estH && r.top - 8 > spaceBelow;
    setPos({
      left: r.left,
      top: up ? Math.max(8, r.top - estH - 6) : r.bottom + 6,
      width: r.width,
      up,
    });
  }, [options.length]);

  useEffect(() => {
    if (!open) return;
    updatePos();
    const onPointer = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!btnRef.current?.contains(t) && !panelRef.current?.contains(t)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    const onMove = () => updatePos();
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('keydown', onKey);
    window.addEventListener('resize', onMove);
    // capture: the app scrolls an inner container, not the window
    window.addEventListener('scroll', onMove, true);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('resize', onMove);
      window.removeEventListener('scroll', onMove, true);
    };
  }, [open, updatePos]);

  const current = options.find((o) => o.value === value);

  return (
    <div className={`relative ${className}`}>
      <button
        ref={btnRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={`glass-input flex w-full items-center justify-between gap-2 text-ink-1 ${
          size === 'sm' ? 'h-7 px-2 text-[11.5px]' : 'h-9 px-2.5 text-[13px]'
        }`}
      >
        <span className={`truncate ${current ? '' : 'text-ink-3'}`}>
          {current?.label ?? (placeholder || value)}
        </span>
        <ChevronDown
          size={size === 'sm' ? 11 : 13}
          strokeWidth={2}
          className={`shrink-0 text-ink-3 transition-transform duration-(--dur-in) ${open ? 'rotate-180' : ''}`}
        />
      </button>
      {createPortal(
        <AnimatePresence>
          {open && (
            <motion.div
              ref={panelRef}
              initial={{ opacity: 0, y: pos.up ? 5 : -5 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: pos.up ? 5 : -5 }}
              transition={{ duration: 0.16, ease: [0.22, 1, 0.36, 1] }}
              style={{ position: 'fixed', left: pos.left, top: pos.top, width: pos.width, zIndex: 60 }}
              className="glass-modal max-h-64 overflow-y-auto rounded-(--radius-m) p-1"
            >
              {options.length === 0 && (
                <div className="px-2.5 py-2 text-[12px] text-ink-3">
                  {placeholder || '—'}
                </div>
              )}
              {options.map((o) => (
                <button
                  key={o.value}
                  type="button"
                  onClick={() => { onChange(o.value); setOpen(false); }}
                  className={`flex w-full items-center justify-between gap-2 rounded-(--radius-s) px-2.5 py-2 text-left text-[12.5px] transition-colors duration-(--dur-in) ${
                    o.value === value
                      ? 'bg-primary-dim text-primary'
                      : 'text-ink-1 hover:bg-hover'
                  }`}
                >
                  <span className="truncate">{o.label}</span>
                  {o.value === value && <Check size={13} strokeWidth={2.4} className="shrink-0" />}
                </button>
              ))}
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ Toast */

type ToastKind = 'ok' | 'warn' | 'error';
type Toast = { id: number; kind: ToastKind; text: string };

const ToastCtx = createContext<(kind: ToastKind, text: string) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

const TOAST_STYLE: Record<ToastKind, { icon: ReactNode; color: string }> = {
  ok: { icon: <CheckCircle2 size={16} strokeWidth={2} />, color: 'text-ok' },
  warn: { icon: <AlertTriangle size={16} strokeWidth={2} />, color: 'text-warn' },
  error: { icon: <OctagonX size={16} strokeWidth={2} />, color: 'text-err' },
};

export function ToastHost({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((kind: ToastKind, text: string) => {
    const id = Date.now() + Math.random();
    setToasts((ts) => [...ts, { id, kind, text }]);
    setTimeout(() => setToasts((ts) => ts.filter((t) => t.id !== id)), 4200);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed bottom-6 right-6 z-50 flex w-80 flex-col gap-2">
        <AnimatePresence>
          {toasts.map((t) => (
            <motion.div
              key={t.id}
              initial={{ opacity: 0, y: 16, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 8, scale: 0.97 }}
              transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
              className="glass-modal pointer-events-auto flex items-start gap-2.5 rounded-(--radius-m) p-3.5"
            >
              <span className={`mt-px shrink-0 ${TOAST_STYLE[t.kind].color}`}>
                {TOAST_STYLE[t.kind].icon}
              </span>
              <p className="flex-1 text-[12.5px] leading-relaxed text-ink-1">
                {t.text}
              </p>
              <button
                onClick={() => setToasts((ts) => ts.filter((x) => x.id !== t.id))}
                className="text-ink-3 transition-colors hover:text-ink-1"
              >
                <X size={13} strokeWidth={2} />
              </button>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </ToastCtx.Provider>
  );
}

/* ------------------------------------------------------------- StatusDot */

export function StatusDot({ ok, warn = false }: { ok: boolean; warn?: boolean }) {
  return (
    <span
      className={`inline-block h-1.5 w-1.5 rounded-full ${
        ok ? 'bg-ok' : warn ? 'bg-warn' : 'bg-err'
      }`}
    />
  );
}

/* ------------------------------------------------- two-step destructive */

/** How long an armed destructive action waits for its confirming click. Every
 *  site arms for this long AND draws its countdown from it, so the line can
 *  never drift from the window it describes. */
export const ARM_MS = 3000;

/** Whether the two-step model has been explained during this launch. One flag
 *  for the whole app: the first armed delete anywhere explains it, and the rest
 *  of the launch stays quiet. In memory on purpose -- dev runs are not a
 *  launch boundary, but a fresh window is when someone is most likely to have
 *  forgotten how deleting works. */
let twoStepHintShown = false;

export function useTwoStepHint() {
  const toast = useToast();
  return useCallback((text: string) => {
    if (twoStepHintShown) return;
    twoStepHintShown = true;
    toast('warn', text);
  }, [toast]);
}

/** The countdown: a 2px line that drains over exactly the arming window.
 *  Positioned by the caller -- a row's bottom edge, a button's bottom edge. */
export function ArmDrain({ className = '' }: { className?: string }) {
  return (
    <motion.span
      initial={{ scaleX: 1 }}
      animate={{ scaleX: 0 }}
      transition={{ duration: ARM_MS / 1000, ease: 'linear' }}
      className={`pointer-events-none absolute h-[2px] origin-left rounded-full bg-err/70 ${className}`}
    />
  );
}

/** A destructive icon button: the first click arms it, the second acts, and
 *  Escape or the timer disarms. The armed state is drawn three ways -- red
 *  fill, the icon morphing to a check with a single pop, and the draining line
 *  -- because a colour change alone is what made the first click look inert. */
export function ConfirmIconButton({
  icon, onConfirm, title, confirmTitle, hint,
  iconSize = 14, boxClass = 'h-8 w-8',
  idleClass = 'text-ink-3 hover:bg-err/10 hover:text-err',
  disabled = false,
}: {
  icon: ReactNode;
  onConfirm: () => void;
  title: string;
  confirmTitle: string;
  /** toasted on the first arm of the launch; omit to never explain */
  hint?: string;
  iconSize?: number;
  boxClass?: string;
  idleClass?: string;
  disabled?: boolean;
}) {
  const [armed, setArmed] = useState(false);
  const teach = useTwoStepHint();

  // an armed control must not stay armed: it would fire on some later,
  // unrelated click, which is the opposite of what a confirmation is for
  useEffect(() => {
    if (!armed) return;
    const id = window.setTimeout(() => setArmed(false), ARM_MS);
    return () => window.clearTimeout(id);
  }, [armed]);

  useEffect(() => {
    if (!armed) return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === 'Escape') setArmed(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [armed]);

  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => {
        if (armed) {
          setArmed(false);
          onConfirm();
        } else {
          setArmed(true);
          if (hint) teach(hint);
        }
      }}
      title={armed ? confirmTitle : title}
      className={`relative grid ${boxClass} shrink-0 place-items-center rounded-(--radius-s) transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
        armed ? 'bg-err/10 text-err hover:bg-err/20' : idleClass
      }`}
    >
      {/* keyed so arming replays the pop, and only arming */}
      <motion.span
        key={armed ? 'armed' : 'idle'}
        initial={{ scale: 0.7 }}
        animate={{ scale: [0.7, 1.3, 1] }}
        transition={{ duration: 0.26, ease: [0.22, 1, 0.36, 1] }}
        className="grid place-items-center"
      >
        {armed ? <Check size={iconSize} strokeWidth={2.5} /> : icon}
      </motion.span>
      {armed && <ArmDrain className="bottom-0 left-1 right-1" />}
    </button>
  );
}
