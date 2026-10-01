"use client";

import { Globe2 } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import type { CloudflareDomainStat } from "@/lib/api";

/**
 * 按域名的注册成功率（Cloudflare 临时邮箱）。
 * 这是历史累计数据，跟统计页上方的日期筛选不是一回事，所以单独成块并写明。
 */
export function DomainRegisterStats({ stats }: { stats: CloudflareDomainStat[] }) {
  const sorted = [...stats].sort((left, right) => {
    const rateDifference = right.success_rate - left.success_rate;
    if (rateDifference !== 0) return rateDifference;
    const totalDifference = right.total - left.total;
    if (totalDifference !== 0) return totalDifference;
    return left.domain.localeCompare(right.domain);
  });
  const totals = sorted.reduce(
    (acc, item) => ({ success: acc.success + item.success, fail: acc.fail + item.fail }),
    { success: 0, fail: 0 },
  );

  return (
    <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
      <CardContent className="space-y-4 p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex min-w-0 items-start gap-2.5">
            <div className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-lg border border-neutral-200 bg-white shadow-sm">
              <Globe2 className="size-4 text-neutral-600" />
            </div>
            <div className="min-w-0">
              <div className="font-semibold text-neutral-900 dark:text-neutral-50">域名注册表现</div>
              <div className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">
                Cloudflare 临时邮箱 · 历史累计，不受上方日期筛选影响
              </div>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1.5 font-mono text-[11px] tabular-nums">
            <span className="rounded-md bg-emerald-100 px-2 py-1 text-emerald-700">成功 {totals.success}</span>
            <span className="rounded-md bg-rose-100 px-2 py-1 text-rose-700">失败 {totals.fail}</span>
          </div>
        </div>

        <div className="max-h-72 overflow-y-auto rounded-xl border border-neutral-200">
          {sorted.length === 0 ? (
            <div className="px-3 py-6 text-center text-xs text-neutral-500">
              在注册机页配置 Cloudflare 临时邮箱域名后，注册结果会在这里按域名累计。
            </div>
          ) : (
            sorted.map((item) => (
              <div
                key={item.domain}
                className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 border-b border-neutral-200/80 px-3 py-2.5 last:border-b-0"
              >
                <div className="min-w-0">
                  <div className="flex items-center justify-between gap-3">
                    <span className="truncate font-mono text-xs font-medium text-neutral-700" title={item.domain}>
                      {item.domain}
                    </span>
                    <span className="shrink-0 font-mono text-[11px] text-neutral-500 tabular-nums">
                      {item.total ? `${item.success_rate}%` : "暂无结果"}
                    </span>
                  </div>
                  <div className={`mt-1.5 h-1 overflow-hidden rounded-full ${item.total ? "bg-rose-200/70" : "bg-neutral-200"}`}>
                    <div
                      className="h-full rounded-full bg-emerald-500 transition-[width] duration-500"
                      style={{ width: `${item.total ? item.success_rate : 0}%` }}
                    />
                  </div>
                </div>
                <div className="grid grid-cols-3 gap-1 font-mono text-[11px] tabular-nums">
                  <span className="rounded-md bg-white px-2 py-1 text-center text-neutral-500 shadow-sm">总 {item.total}</span>
                  <span className="rounded-md bg-emerald-50 px-2 py-1 text-center text-emerald-700">成 {item.success}</span>
                  <span className="rounded-md bg-rose-50 px-2 py-1 text-center text-rose-700">败 {item.fail}</span>
                </div>
              </div>
            ))
          )}
        </div>
      </CardContent>
    </Card>
  );
}
