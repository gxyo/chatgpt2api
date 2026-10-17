import * as React from "react";

import { cn } from "@/lib/utils";

function Card({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card"
      className={cn(
        // 参考稿的卡片：纯白面 + 一像素冷灰边 + 几乎看不见的浅投影，圆角 16px。
        "bg-card text-card-foreground flex flex-col rounded-2xl border border-border shadow-[0_1px_2px_rgba(15,23,42,0.035)]",
        className,
      )}
      {...props}
    />
  );
}

function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-header"
      className={cn("flex flex-col gap-1.5 p-6", className)}
      {...props}
    />
  );
}

function CardTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-title"
      className={cn("leading-none font-semibold tracking-tight", className)}
      {...props}
    />
  );
}

function CardDescription({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-description"
      className={cn("text-muted-foreground text-sm", className)}
      {...props}
    />
  );
}

function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-content"
      className={cn("p-6 pt-0", className)}
      {...props}
    />
  );
}

/**
 * KPI 指标卡的样式（统计页、号池概览那些小卡片）。
 * 保持白面细边的静态观感，hover 只轻微提亮描边 + 投影，不位移，避免整页数字跳动。
 */
export const statCardClass = cn(
  "group relative rounded-2xl border-border bg-card shadow-[0_1px_2px_rgba(15,23,42,0.035)]",
  "transition-[box-shadow,border-color] duration-200 ease-out",
  "hover:border-brand/25 hover:shadow-[0_1px_2px_rgba(15,23,42,0.04),0_10px_30px_-18px_rgba(13,115,200,0.35)]",
);

export { Card, CardContent, CardDescription, CardHeader, CardTitle };
