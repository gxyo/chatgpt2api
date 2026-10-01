"use client";

import type { ImageStatsBreakdown } from "@/lib/api";
import { cn } from "@/lib/utils";

type ImageModeChartProps = {
  modes: ImageStatsBreakdown[];
};

// 与波形图共用同一对校验过的配色（蓝/红，protan/deutan ΔE ≈ 21.6 亮色、19.2 暗色）。
// 这里「成功 / 失败」是部分与整体，不是两个独立序列，所以画成堆叠条而不是并列条。
const SUCCESS_BAR = "bg-[#2a78d6] dark:bg-[#3987e5]";
const SUCCESS_TRACK = "bg-[#cde2fb] dark:bg-[#184f95]/40";
const FAILED_BAR = "bg-[#e34948] dark:bg-[#e66767]";

function formatRate(value: number) {
  return `${(value * 100).toFixed(1)}%`;
}

function ModePanel({ item, maxRequests }: { item: ImageStatsBreakdown; maxRequests: number }) {
  const requests = item.requests;
  const success = Math.max(0, item.success);
  const failed = Math.max(0, item.failed);
  // 条形长度按「最大的一条」归一，各面板等宽，因此两条之间可比；无数据时是空轨道。
  const barWidth = requests > 0 ? (requests / maxRequests) * 100 : 0;
  const successShare = requests > 0 ? (success / requests) * 100 : 0;

  return (
    <div
      className="rounded-xl border border-stone-100 p-4 transition-colors hover:bg-stone-50/70 dark:border-white/10 dark:hover:bg-white/5"
      title={`${item.label}：请求 ${requests} · 成功 ${success} · 失败 ${failed}`}
    >
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-sm font-medium text-stone-600 dark:text-stone-300">{item.label}</span>
        <span className="text-xl leading-none font-semibold text-stone-900 dark:text-stone-50">
          {requests}
          <span className="ml-1 text-xs font-normal text-stone-400">次</span>
        </span>
      </div>

      <div className={cn("mt-3 h-5 w-full overflow-hidden rounded-full", SUCCESS_TRACK)}>
        <div className="flex h-full" style={{ width: `${barWidth}%` }}>
          {success > 0 ? <div className={cn("h-full", SUCCESS_BAR)} style={{ width: `${successShare}%` }} /> : null}
          {/* 2px 的表面色缝隙把两段分开，不额外描边。 */}
          {success > 0 && failed > 0 ? <div className="h-full w-0.5 shrink-0 bg-white" /> : null}
          {failed > 0 ? <div className={cn("h-full flex-1", FAILED_BAR)} /> : null}
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-stone-500 dark:text-stone-400">
        <span className="inline-flex items-center gap-1.5">
          <span className={cn("size-2 rounded-full", SUCCESS_BAR)} />
          成功 <span className="tabular-nums text-stone-700 dark:text-stone-200">{success}</span>
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className={cn("size-2 rounded-full", failed > 0 ? FAILED_BAR : "bg-stone-300 dark:bg-stone-600")} />
          失败 <span className="tabular-nums text-stone-700 dark:text-stone-200">{failed}</span>
        </span>
        <span className="tabular-nums">成功率 {requests > 0 ? formatRate(success / requests) : "-"}</span>
      </div>
    </div>
  );
}

/** 堆叠条是两段颜色，图例固定挂在卡片标题行，避免只靠颜色区分成功与失败。 */
export function ImageModeLegend({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-4 text-xs text-stone-500 dark:text-stone-400",
        className,
      )}
    >
      <span className="inline-flex items-center gap-1.5">
        <span className={cn("size-2.5 rounded-full", SUCCESS_BAR)} />
        成功
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className={cn("size-2.5 rounded-full", FAILED_BAR)} />
        失败
      </span>
    </div>
  );
}

export function ImageModeChart({ modes }: ImageModeChartProps) {
  const maxRequests = Math.max(1, ...modes.map((item) => item.requests));

  if (modes.length === 0) {
    return <div className="py-10 text-center text-sm text-stone-400">暂无调用方式数据</div>;
  }

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {modes.map((item) => (
        <ModePanel key={item.mode} item={item} maxRequests={maxRequests} />
      ))}
    </div>
  );
}
