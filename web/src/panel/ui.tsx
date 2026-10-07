import * as React from "react";

/** The few controls the panel needs, styled from the same tokens the Hermes components use. */

export function cn(...classes: unknown[]): string {
  return classes.filter((c) => typeof c === "string" && c).join(" ");
}

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  size?: "sm" | "xs" | "default"; ghost?: boolean; outlined?: boolean; destructive?: boolean;
};

export function Button({ size = "default", ghost, outlined, destructive, className, type, ...rest }: ButtonProps) {
  return (
    <button type={type ?? "button"} {...rest}
      className={cn("ha-btn", `ha-btn--${size}`, ghost && "ha-btn--ghost", outlined && "ha-btn--outlined",
        destructive && "ha-btn--destructive", className)} />
  );
}

export function Input({ className, ...rest }: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...rest} className={cn("ha-input", className)} />;
}

export function Badge({ tone = "default", className, ...rest }: React.HTMLAttributes<HTMLSpanElement> & {
  tone?: "default" | "outline" | "secondary" | "success" | "warning";
}) {
  return <span {...rest} className={cn("ha-badge", `ha-badge--${tone}`, className)} />;
}

export function Select({ value, onValueChange, children, ...rest }: Omit<React.SelectHTMLAttributes<HTMLSelectElement>, "onChange"> & {
  onValueChange: (value: string) => void;
}) {
  return (
    <select {...rest} value={value} className="ha-select" onChange={(e) => onValueChange(e.target.value)}>{children}</select>
  );
}

export type Toast = { message: string; type: "error" | "success" } | null;

export function useToast(duration: number): { toast: Toast; showToast: (message: string, type: "error" | "success") => void } {
  const [toast, setToast] = React.useState<Toast>(null);
  const timer = React.useRef<number | undefined>(undefined);
  const showToast = React.useCallback((message: string, type: "error" | "success") => {
    setToast({ message, type });
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setToast(null), duration);
  }, [duration]);
  React.useEffect(() => () => window.clearTimeout(timer.current), []);
  return { toast, showToast };
}

const UNITS: Array<[number, Intl.RelativeTimeFormatUnit]> = [
  [60, "second"], [60, "minute"], [24, "hour"], [7, "day"], [4.345, "week"], [12, "month"], [Infinity, "year"],
];

export function timeAgo(unixSeconds: number): string {
  let delta = unixSeconds - Date.now() / 1000;
  for (const [size, unit] of UNITS) {
    if (Math.abs(delta) < size) return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(Math.round(delta), unit);
    delta /= size;
  }
  return "";
}
