"use client";

import { Card, CardContent, statCardClass } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/**
 * KPI 指标卡：一个圆点 + 标签 + 大数字 + 一行说明。
 * 原来私藏在统计页里，注册统计页也要用同一套观感，所以提出来共用。
 */
export function StatTile({
  label,
  value,
  caption,
  tone,
}: {
  label: string;
  value: string;
  caption?: string;
  tone: string;
}) {
  return (
    <Card className={statCardClass}>
      <CardContent className="p-5">
        <div className="flex items-center gap-2 text-xs font-medium text-neutral-500 dark:text-neutral-400">
          <span className={cn("size-2 rounded-full", tone)} />
          {label}
        </div>
        <div className="mt-3 text-3xl leading-none font-semibold tracking-tight text-neutral-900 dark:text-neutral-50">
          {value}
        </div>
        <div className="mt-2 min-h-4 text-xs text-neutral-400 dark:text-neutral-500">{caption}</div>
      </CardContent>
    </Card>
  );
}
