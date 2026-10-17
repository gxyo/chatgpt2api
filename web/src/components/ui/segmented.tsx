"use client";

import { cn } from "@/lib/utils";

export type SegmentedOption<T extends string> = {
  value: T;
  label: string;
};

/**
 * 分段选择器：白底细边容器 + 蓝色实心选中药丸。
 * 统计页的日期预设、注册统计的区间切换共用这一套观感。
 */
export function SegmentedControl<T extends string>({
  value,
  options,
  onChange,
  className,
  ariaLabel,
}: {
  value: T | null;
  options: readonly SegmentedOption<T>[];
  onChange: (value: T) => void;
  className?: string;
  ariaLabel?: string;
}) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className={cn(
        "inline-flex items-center gap-1 rounded-xl border border-border bg-card p-1 shadow-[0_1px_2px_rgba(15,23,42,0.03)]",
        className,
      )}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(option.value)}
            className={cn(
              "rounded-lg px-3.5 py-1.5 text-[13px] font-medium whitespace-nowrap transition-colors",
              active
                ? "bg-brand text-brand-foreground shadow-[0_1px_2px_rgba(15,23,42,0.18),inset_0_1px_0_rgba(255,255,255,0.14)]"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
