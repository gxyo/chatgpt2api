import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export type MetricItem = {
  label: string;
  value: ReactNode;
  caption?: ReactNode;
  /** 图例色点，直接传 bg-* 工具类。 */
  tone?: string;
  action?: ReactNode;
};

/**
 * 大卡片右侧的两列指标格：内部用发丝线分隔，不拆成独立卡片。
 * 这是参考稿最有辨识度的一块版式——数字和图表共享同一张卡。
 */
export function MetricGrid({
  items,
  className,
}: {
  items: MetricItem[];
  className?: string;
}) {
  return (
    <div className={cn("grid grid-cols-2", className)}>
      {items.map((item, index) => (
        <div
          key={item.label}
          className={cn(
            "relative flex flex-col justify-center px-5 py-5",
            index % 2 === 1 && "border-l border-border",
            index >= 2 && "border-t border-border",
          )}
        >
          <div className="flex items-center justify-between gap-2">
            <span className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
              {item.tone ? <span className={cn("size-1.5 shrink-0 rounded-full", item.tone)} /> : null}
              {item.label}
            </span>
            {item.action}
          </div>
          <div className="mt-2.5 text-[30px] leading-none font-semibold tracking-tight tabular-nums text-foreground">
            {item.value}
          </div>
          {item.caption ? (
            <div className="mt-2 text-[11px] leading-4 text-muted-foreground/80">{item.caption}</div>
          ) : null}
        </div>
      ))}
    </div>
  );
}
