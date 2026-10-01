"use client";

import { LoaderCircle } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * 页面内的内容加载态。标题、筛选条、卡片外框这些骨架先渲染出来，
 * 这里只占住数据块的位置转圈 —— "已经进来了、数据在路上"，
 * 而不是让用户对着一块空白猜页面是不是坏了。
 */
export function ContentLoading({ label = "加载中", className }: { label?: string; className?: string }) {
  return (
    <div
      className={cn(
        "flex items-center justify-center gap-2 py-14 text-sm text-neutral-500 dark:text-neutral-400",
        className,
      )}
    >
      <LoaderCircle className="size-4 animate-spin text-neutral-400 dark:text-neutral-500" />
      {label}
    </div>
  );
}
