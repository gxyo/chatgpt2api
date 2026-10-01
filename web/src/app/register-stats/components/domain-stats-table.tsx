"use client";

import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { CloudflareDomainStat } from "@/lib/api";
import { cn } from "@/lib/utils";

/** 后端存的是 UTC ISO 串，按北京时间展示，和统计页的口径一致。 */
function formatUpdatedAt(value?: string) {
  if (!value) {
    return "-";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "-";
  }
  return parsed.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}

/**
 * 按域名的注册结果明细。
 * 数据是永久累计的：域名从注册机配置里删掉后仍然留在表里，只是不再带「配置中」标记。
 */
export function DomainStatsTable({ stats }: { stats: CloudflareDomainStat[] }) {
  // 按成功数排，用户问的是「每个域名总共成功了多少」，成功多的排前面最直观。
  const sorted = [...stats].sort((left, right) => {
    const successDifference = right.success - left.success;
    if (successDifference !== 0) return successDifference;
    const totalDifference = right.total - left.total;
    if (totalDifference !== 0) return totalDifference;
    return left.domain.localeCompare(right.domain);
  });

  return (
    <div className="space-y-3">
      <div className="overflow-x-auto rounded-xl border border-neutral-200 dark:border-white/10">
        <Table className="min-w-[780px]">
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>域名</TableHead>
              <TableHead className="w-[240px]">成功率</TableHead>
              <TableHead className="text-right">成功</TableHead>
              <TableHead className="text-right">失败</TableHead>
              <TableHead className="text-right">总数</TableHead>
              <TableHead className="text-right">最近更新</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {sorted.length === 0 ? (
              <TableRow className="hover:bg-transparent">
                <TableCell colSpan={6} className="py-10 text-center text-sm text-neutral-500">
                  还没有注册记录。在注册机页配置 Cloudflare 临时邮箱并跑一次任务后，这里会开始累计。
                </TableCell>
              </TableRow>
            ) : (
              sorted.map((item) => (
                <TableRow key={item.domain}>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <span
                        className="font-mono text-xs font-medium text-neutral-700 dark:text-neutral-200"
                        title={item.domain}
                      >
                        {item.domain}
                      </span>
                      {item.configured ? (
                        <Badge variant="success" className="rounded-md px-1.5 py-0 text-[10px]">
                          配置中
                        </Badge>
                      ) : null}
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <div
                        className={cn(
                          "h-1.5 w-full overflow-hidden rounded-full",
                          item.total ? "bg-rose-200/70" : "bg-neutral-200",
                        )}
                      >
                        <div
                          className="h-full rounded-full bg-emerald-500 transition-[width] duration-500"
                          style={{ width: `${item.total ? item.success_rate : 0}%` }}
                        />
                      </div>
                      <span className="w-16 shrink-0 text-right font-mono text-[11px] text-neutral-500 tabular-nums">
                        {item.total ? `${item.success_rate}%` : "暂无结果"}
                      </span>
                    </div>
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs text-emerald-700 tabular-nums dark:text-emerald-400">
                    {item.success}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs text-rose-700 tabular-nums dark:text-rose-400">
                    {item.fail}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs text-neutral-700 tabular-nums dark:text-neutral-200">
                    {item.total}
                  </TableCell>
                  <TableCell className="text-right font-mono text-[11px] whitespace-nowrap text-neutral-500">
                    {formatUpdatedAt(item.updated_at)}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      <p className="text-xs text-neutral-400 dark:text-neutral-500">
        没有「配置中」标记的域名已从注册机配置移除，历史数据仍保留并继续累计。
      </p>
    </div>
  );
}
