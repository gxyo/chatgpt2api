import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * 所有页面的统一页头：左侧大标题 + 一行说明，右侧放筛选后的主操作。
 * 参考稿里的页面标题是整页最重的一层，不额外加图标或彩色装饰。
 */
export function PageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header
      className={cn(
        "flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between",
        className,
      )}
    >
      <div className="space-y-1.5">
        <h1 className="text-[28px] leading-[1.15] font-semibold tracking-tight text-foreground">
          {title}
        </h1>
        {description ? (
          <p className="max-w-3xl text-[13px] leading-6 text-muted-foreground">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}
