import type { ReactNode } from 'react';

/* ------------------------------------------------- shared settings controls */

/** one label-left / control-right row inside a settings section grid.
 *  `center`: the LABEL STAYS PINNED at its column's left edge while the
 *  control floats in the middle of the leftover space — i.e. roughly the
 *  midpoint between this label and the next column's label. Default: the
 *  control hugs the cell's right edge.
 *  `labelClass`: fixed label-column width (e.g. "w-[7rem]") -- rows that
 *  share one width get IDENTICAL leftover space, so their slider tracks
 *  line up exactly instead of drifting with label text length. */
export function SettingRow({ icon, label, children, center = false, className = '', labelClass = '' }: {
  icon?: ReactNode; label: string; children: ReactNode;
  center?: boolean; className?: string; labelClass?: string;
}) {
  return (
    <div className={`flex items-center gap-3 ${className}`}>
      <span className={`flex shrink-0 items-center gap-2 text-[12.5px] text-ink-2 ${labelClass}`}>
        {icon}
        {label}
      </span>
      <span className={`flex min-w-0 items-center ${center ? 'flex-1 justify-center' : 'ml-auto'}`}>
        {children}
      </span>
    </div>
  );
}

/** slider + live value readout; default track stretches to fill its cell,
 *  `widthClass` overrides sizing (e.g. "w-4/5 flex-none" = 80% centered
 *  inside the leftover space of a `center` SettingRow) */
export function Slider({ value, min, max, step, onChange, format, widthClass }: {
  value: number; min: number; max: number; step: number;
  onChange: (v: number) => void; format: (v: number) => string;
  widthClass?: string;
}) {
  return (
    <span className={`flex min-w-0 items-center gap-3 ${widthClass ?? 'flex-1'}`}>
      <input
        type="range" min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="h-1.5 min-w-20 flex-1 cursor-pointer appearance-none rounded-full bg-line-2 accent-[var(--primary)]"
      />
      <span className="w-11 shrink-0 text-right font-mono text-[11px] text-primary">{format(value)}</span>
    </span>
  );
}
