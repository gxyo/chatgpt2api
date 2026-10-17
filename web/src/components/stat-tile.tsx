"use client";

import type { ReactNode } from "react";

import { Card, CardContent, statCardClass } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/**
 * KPI 指标卡：一个圆点 + 标签 + 大数字 + 一行说明。
 * 参考稿里的指标数字用等宽数字（tabular-nums），多张卡并排时小数点不会跳。
 */
export function StatTile({
  label,
  value,
  caption,
  tone,
  action,
}: {
  label: string;
  value: string;
  caption?: string;
  tone: string;
  /** 卡片右上角的操作区（比如只刷新这一张卡的按钮）。 */
  action?: ReactNode;
}) {
  return (
    <Card className={statCardClass}>
      <CardContent className="p-5">
        <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
          <span className={cn("size-2 shrink-0 rounded-full", tone)} />
          {label}
          {action ? <div className="ml-auto">{action}</div> : null}
        </div>
        <div className="mt-3 text-[30px] leading-none font-semibold tracking-tight tabular-nums text-foreground">
          {value}
        </div>
        <div className="mt-2 min-h-4 text-xs text-muted-foreground/80">{caption}</div>
      </CardContent>
    </Card>
  );
}
