import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * 筛选条：一块独立的白色卡片，字段上方带小标签，主查询按钮放在最右。
 * 和参考稿一致——筛选条件不再堆在页面标题旁边，而是自成一层。
 */
export function FilterCard({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col gap-3 rounded-2xl border border-border bg-card p-3 shadow-[0_1px_2px_rgba(15,23,42,0.035)] sm:flex-row sm:flex-wrap sm:items-end",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function FilterField({
  label,
  children,
  className,
}: {
  label: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)}>
      <span className="pl-1 text-[11px] font-medium tracking-wide text-muted-foreground">
        {label}
      </span>
      {children}
    </div>
  );
}
