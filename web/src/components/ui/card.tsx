import * as React from "react";

import { cn } from "@/lib/utils";

function Card({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card"
      className={cn(
        "bg-card text-card-foreground flex flex-col rounded-3xl border border-white/70 shadow-[0_1px_2px_rgba(16,24,40,0.04),0_12px_32px_-20px_rgba(16,24,40,0.22)]",
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
 * hover 时轻微上浮 + 加深投影，并让左上角掠过一层强调色柔光当作高光。
 * 暗色下投影被全局规则关掉了，改成提亮描边来体现反馈。
 */
export const statCardClass = cn(
  "group relative rounded-2xl border-white/80 bg-white/90 shadow-sm",
  "transition-[transform,box-shadow,border-color] duration-200 ease-out",
  "hover:-translate-y-0.5 hover:border-neutral-300/80 dark:hover:border-white/25",
  "hover:shadow-[0_2px_4px_rgba(16,24,40,0.05),0_18px_40px_-22px_rgba(16,24,40,0.30)]",
  "before:pointer-events-none before:absolute before:inset-0 before:rounded-[inherit] before:opacity-0 before:transition-opacity before:duration-300 before:content-['']",
  "before:bg-[radial-gradient(90%_70%_at_18%_0%,rgba(42,120,214,0.10),transparent_65%)]",
  // 暗色下 .dark .bg-white/* 带 !important，改不了底色，所以靠更亮的辉光 + 描边做反馈
  "dark:before:bg-[radial-gradient(90%_70%_at_18%_0%,rgba(88,160,255,0.22),transparent_65%)]",
  "hover:before:opacity-100",
);

export { Card, CardContent, CardDescription, CardHeader, CardTitle };
