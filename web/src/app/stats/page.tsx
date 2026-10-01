"use client";

import { useEffect, useMemo, useState } from "react";
import { LoaderCircle, RefreshCw, Search } from "lucide-react";
import { toast } from "sonner";

import { DateRangeFilter } from "@/components/date-range-filter";
import { ImageModeChart, ImageModeLegend } from "@/components/image-mode-chart";
import { ImageStatsChart } from "@/components/image-stats-chart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { fetchImageStats, type ImageStatsResponse } from "@/lib/api";
import { getBeijingToday, shiftDate } from "@/lib/beijing-time";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { cn } from "@/lib/utils";

const REQUESTS_DOT = "bg-[#2a78d6] dark:bg-[#3987e5]";
const SUCCESS_DOT = "bg-[#2a78d6]/50 dark:bg-[#3987e5]/50";
const FAILED_DOT = "bg-[#e34948] dark:bg-[#e66767]";
const NEUTRAL_DOT = "bg-stone-300 dark:bg-stone-600";

// days=0 表示不按天数取区间，交给后端按已有记录算「全部」。
const PRESETS = [
  { key: "today", label: "今天", days: 1 },
  { key: "week", label: "近 7 天", days: 7 },
  { key: "month", label: "近 30 天", days: 30 },
  { key: "all", label: "全部", days: 0 },
] as const;

type PresetKey = (typeof PRESETS)[number]["key"];

function formatRate(value: number) {
  return `${(value * 100).toFixed(1)}%`;
}

function formatDuration(value: number) {
  if (!value) {
    return "-";
  }
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value} ms`;
}

function StatTile({
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
    <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
      <CardContent className="p-5">
        <div className="flex items-center gap-2 text-xs font-medium text-stone-500 dark:text-stone-400">
          <span className={cn("size-2 rounded-full", tone)} />
          {label}
        </div>
        <div className="mt-3 text-3xl leading-none font-semibold tracking-tight text-stone-900 dark:text-stone-50">
          {value}
        </div>
        <div className="mt-2 min-h-4 text-xs text-stone-400 dark:text-stone-500">{caption}</div>
      </CardContent>
    </Card>
  );
}

function StatsContent() {
  const today = useMemo(() => getBeijingToday(), []);
  const [startDate, setStartDate] = useState(today);
  const [endDate, setEndDate] = useState(today);
  // 预设按钮的高亮状态；手动选过日期后置空，表示当前是自定义区间。
  const [preset, setPreset] = useState<PresetKey | null>("today");
  const [data, setData] = useState<ImageStatsResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const loadStats = async (nextPreset = preset, start = startDate, end = endDate) => {
    setIsLoading(true);
    try {
      const filters =
        nextPreset === "all" ? { scope: "all" as const } : { start_date: start, end_date: end };
      setData(await fetchImageStats(filters));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载统计数据失败");
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void loadStats(preset, startDate, endDate);
    // 与 logs / image-manager 页面一致：筛选条件变化即重新拉取。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preset, startDate, endDate]);

  const applyPreset = (key: PresetKey, days: number) => {
    if (key === "all") {
      setPreset("all");
      return;
    }
    const end = getBeijingToday();
    setStartDate(days <= 1 ? end : shiftDate(end, -(days - 1)));
    setEndDate(end);
    setPreset(key);
  };

  const totals = data?.totals;
  const range = data?.range;
  const granularity = range?.granularity ?? "hour";
  const rangeLabel = range
    ? range.granularity === "hour"
      ? `${range.start_date} 全天 · 按小时`
      : `${range.start_date} 至 ${range.end_date} · 按天`
    : "";
  return (
    <section className="space-y-5">
      <div className="flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
        <div className="space-y-1">
          <div className="text-xs font-semibold tracking-[0.18em] text-stone-500 uppercase">Statistics</div>
          <h1 className="text-2xl font-semibold tracking-tight">请求统计</h1>
          <p className="text-sm text-stone-500 dark:text-stone-400">
            按北京时间统计生图请求量，默认展示今天（{today}）。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <div className="flex items-center gap-1 rounded-xl border border-stone-200 bg-white p-1 dark:border-white/10 dark:bg-white/5">
            {PRESETS.map((item) => (
              <button
                key={item.key}
                type="button"
                aria-pressed={preset === item.key}
                className={cn(
                  "rounded-lg px-3 py-1.5 text-[13px] font-medium transition",
                  preset === item.key
                    ? "bg-stone-900 text-white shadow-sm dark:bg-white dark:text-stone-900"
                    : "text-stone-600 hover:bg-stone-100 hover:text-stone-900 dark:text-stone-300 dark:hover:bg-white/10 dark:hover:text-white",
                )}
                onClick={() => applyPreset(item.key, item.days)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <DateRangeFilter
            startDate={preset === "all" ? "" : startDate}
            endDate={preset === "all" ? "" : endDate}
            placeholder={preset === "all" ? "全部时间" : "选择日期范围"}
            onChange={(start, end) => {
              setStartDate(start);
              setEndDate(end);
              setPreset(null);
            }}
          />
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white px-4 text-stone-700"
            onClick={() => {
              setStartDate(today);
              setEndDate(today);
              setPreset("today");
            }}
          >
            重置
          </Button>
          <Button
            className="h-10 rounded-xl bg-stone-950 px-4 text-white hover:bg-stone-800"
            onClick={() => void loadStats()}
            disabled={isLoading}
          >
            {isLoading ? <LoaderCircle className="size-4 animate-spin" /> : <Search className="size-4" />}
            查询
          </Button>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="请求数"
          value={String(totals?.requests ?? 0)}
          caption={rangeLabel || " "}
          tone={REQUESTS_DOT}
        />
        <StatTile
          label="生成成功"
          value={String(totals?.success ?? 0)}
          caption="已成功返回图片"
          tone={SUCCESS_DOT}
        />
        <StatTile
          label="生成失败"
          value={String(totals?.failed ?? 0)}
          caption="含内容拦截与上游异常"
          tone={FAILED_DOT}
        />
        <StatTile
          label="成功率"
          value={totals ? formatRate(totals.success_rate) : "-"}
          caption={`平均耗时 ${formatDuration(totals?.avg_duration_ms ?? 0)}`}
          tone={NEUTRAL_DOT}
        />
      </div>

      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="space-y-4 p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="font-semibold text-stone-900 dark:text-stone-50">请求量波形</div>
              <div className="mt-0.5 text-xs text-stone-500 dark:text-stone-400">{rangeLabel}</div>
            </div>
            {data?.peak ? (
              <Badge variant="info" className="rounded-md px-2.5 py-1 tabular-nums">
                峰值 {data.peak.requests} · {data.peak.full_label}
              </Badge>
            ) : null}
          </div>

          {isLoading && !data ? (
            <div className="flex h-[288px] items-center justify-center text-sm text-stone-400">
              <LoaderCircle className="mr-2 size-4 animate-spin" />
              加载中
            </div>
          ) : data && data.totals.requests > 0 ? (
            <ImageStatsChart series={data.series} granularity={granularity} />
          ) : (
            <div className="flex h-[288px] flex-col items-center justify-center gap-1 text-sm text-stone-400">
              <span>该时间段没有生图请求</span>
              <span className="text-xs text-stone-400/80">换一个日期范围试试</span>
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="space-y-4 p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="font-semibold text-stone-900 dark:text-stone-50">按调用方式</div>
              <div className="mt-0.5 text-xs text-stone-500 dark:text-stone-400">
                条形总长 = 请求数，长度按最大的一条归一
              </div>
            </div>
            <ImageModeLegend className="pt-0.5" />
          </div>
          <ImageModeChart modes={data?.by_mode ?? []} />
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button
          variant="ghost"
          className="h-8 rounded-lg px-3 text-stone-500"
          onClick={() => void loadStats()}
          disabled={isLoading}
        >
          <RefreshCw className={cn("size-4", isLoading ? "animate-spin" : "")} />
          刷新
        </Button>
      </div>
    </section>
  );
}

export default function StatsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }
  return <StatsContent />;
}
