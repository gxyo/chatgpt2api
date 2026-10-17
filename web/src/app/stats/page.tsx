"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { LoaderCircle, RefreshCw, Search } from "lucide-react";
import { toast } from "sonner";

import { DateRangeFilter } from "@/components/date-range-filter";
import { ImageModeChart } from "@/components/image-mode-chart";
import { StatTile } from "@/components/stat-tile";
import { WaveChart } from "@/components/wave-chart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { SegmentedControl } from "@/components/ui/segmented";
import { fetchImageStats, fetchInflightStats, type ImageStatsResponse } from "@/lib/api";
import { getBeijingToday, shiftDate } from "@/lib/beijing-time";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { cn } from "@/lib/utils";

const SUCCESS_DOT = "bg-brand/50";
const FAILED_DOT = "bg-[#e34948] dark:bg-[#e66767]";
const POLICY_DOT = "bg-[#d97706] dark:bg-[#f0a13a]";
const NEUTRAL_DOT = "bg-neutral-300 dark:bg-neutral-600";
const LIVE_DOT = "bg-[#2fa96b] dark:bg-[#3fc07f]";
const REVENUE_DOT = "bg-[#c08a2e] dark:bg-[#e0ab4c]";
const LIFETIME_DOT = "bg-[#7c5c24] dark:bg-[#c9a45a]";

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

const currencyFormatter = new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY" });

function formatCurrency(value: number) {
  return currencyFormatter.format(Number.isFinite(value) ? value : 0);
}

function StatsContent() {
  const today = useMemo(() => getBeijingToday(), []);
  const [startDate, setStartDate] = useState(today);
  const [endDate, setEndDate] = useState(today);
  // 预设按钮的高亮状态；手动选过日期后置空，表示当前是自定义区间。
  const [preset, setPreset] = useState<PresetKey | null>("today");
  const [data, setData] = useState<ImageStatsResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  // 「当前请求数」是实时值，跟日期筛选无关，所以自己一份状态、自己一个刷新按钮。
  const [inflight, setInflight] = useState<number | null>(null);
  const [isInflightLoading, setIsInflightLoading] = useState(false);
  const [inflightUpdatedAt, setInflightUpdatedAt] = useState("");

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

  const loadInflight = async () => {
    setIsInflightLoading(true);
    try {
      const result = await fetchInflightStats();
      setInflight(result.count);
      setInflightUpdatedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "获取当前请求数失败");
    } finally {
      setIsInflightLoading(false);
    }
  };

  useEffect(() => {
    void loadStats(preset, startDate, endDate);
    // 与 logs / image-manager 页面一致：筛选条件变化即重新拉取。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preset, startDate, endDate]);

  useEffect(() => {
    void loadInflight();
    // 只在进入页面时拉一次，之后靠卡片右上角的按钮手动刷新。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
  const byModel = data?.by_model ?? [];
  // 更早的请求没有模型/图片记录，收入按成功次数估算，明细表里单列不出来。
  const hasLegacyRevenue = (totals?.legacy_success ?? 0) > 0 || (totals?.legacy_revenue ?? 0) > 0;
  return (
    <section className="space-y-5">
      <div className="flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
        <div className="space-y-1.5">
          <h1 className="text-[26px] leading-tight font-semibold tracking-tight">图片统计</h1>
          <p className="text-[13px] leading-5 text-muted-foreground">
            按北京时间统计成功生成与失败的图片数，默认展示今天（{today}）。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <SegmentedControl
            value={preset}
            ariaLabel="统计区间"
            options={PRESETS.map((item) => ({ value: item.key, label: item.label }))}
            onChange={(key) => {
              const item = PRESETS.find((preset) => preset.key === key);
              if (item) {
                applyPreset(item.key, item.days);
              }
            }}
          />
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
            className="h-10 px-4"
            onClick={() => {
              setStartDate(today);
              setEndDate(today);
              setPreset("today");
            }}
          >
            重置
          </Button>
          <Button
            className="h-10 px-4"
            onClick={() => {
              // 「查询」= 把这一页的数据都重拉一遍，当前请求数也顺带刷掉。
              void loadStats();
              void loadInflight();
            }}
            disabled={isLoading}
          >
            {isLoading ? <LoaderCircle className="size-4 animate-spin" /> : <Search className="size-4" />}
            查询
          </Button>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="成功图片"
          value={String(totals?.images ?? 0)}
          caption={rangeLabel || " "}
          tone={SUCCESS_DOT}
        />
        <StatTile
          label="生成失败"
          value={String(totals?.failed ?? 0)}
          caption="上游异常等，不含安全拦截"
          tone={FAILED_DOT}
        />
        <StatTile
          label="违反安全策略"
          value={String(totals?.policy_violation ?? 0)}
          caption="被安全策略拦截 · 不计费"
          tone={POLICY_DOT}
        />
        <StatTile
          label="成功率"
          value={totals ? formatRate(totals.success_rate) : "-"}
          caption={`平均耗时 ${formatDuration(totals?.avg_duration_ms ?? 0)}`}
          tone={NEUTRAL_DOT}
        />
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="收入"
          value={formatCurrency(totals?.revenue ?? 0)}
          caption={`成功图片 ${totals?.images ?? 0} 张 · 按计费时单价累计`}
          tone={REVENUE_DOT}
        />
        <StatTile
          label="总收入"
          value={formatCurrency(data?.lifetime.revenue ?? 0)}
          caption="全部时间累计，不随筛选变化"
          tone={LIFETIME_DOT}
        />
        <StatTile
          label="当前请求数"
          value={inflight === null ? "-" : String(inflight)}
          caption={inflightUpdatedAt ? `更新于 ${inflightUpdatedAt}` : "点右上角刷新"}
          tone={LIVE_DOT}
          action={
            <button
              type="button"
              title="刷新当前请求数"
              aria-label="刷新当前请求数"
              className="-my-1 rounded-md p-1 text-neutral-400 transition hover:bg-neutral-100 hover:text-neutral-700 disabled:opacity-50 dark:text-neutral-500 dark:hover:bg-white/10 dark:hover:text-neutral-100"
              disabled={isInflightLoading}
              onClick={() => void loadInflight()}
            >
              <RefreshCw className={cn("size-3.5", isInflightLoading && "animate-spin")} />
            </button>
          }
        />
      </div>

      <Card>
        <CardContent className="space-y-4 p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="font-semibold text-neutral-900 dark:text-neutral-50">成功图片波形</div>
              <div className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">{rangeLabel}</div>
            </div>
            {data?.peak ? (
              <Badge variant="info" className="rounded-md px-2.5 py-1 tabular-nums">
                峰值 {(data.peak.images ?? 0) > 0 ? data.peak.images : data.peak.requests} ·{" "}
                {data.peak.full_label}
              </Badge>
            ) : null}
          </div>

          {isLoading && !data ? (
            <div className="flex h-[288px] items-center justify-center text-sm text-neutral-400">
              <LoaderCircle className="mr-2 size-4 animate-spin" />
              加载中
            </div>
          ) : data && data.totals.requests > 0 ? (
            <WaveChart
              series={data.series}
              granularity={granularity}
              primary="images"
              labels={{
                primary: "成功图片",
                secondary: "失败数",
                ariaLabel: "成功图片与失败数波形图，按{axis}展示",
              }}
            />
          ) : (
            <div className="flex h-[288px] flex-col items-center justify-center gap-1 text-sm text-neutral-400">
              <span>该时间段没有生图记录</span>
              <span className="text-xs text-neutral-400/80">换一个日期范围试试</span>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="space-y-4 p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="font-semibold text-neutral-900 dark:text-neutral-50">模型收入明细</div>
              <div className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">
                收入按计费时的单价累计，调价只影响之后的请求 · {rangeLabel || "全部时间"}
              </div>
            </div>
            <Badge variant="info" className="rounded-md px-2.5 py-1 tabular-nums">
              合计 {formatCurrency(totals?.revenue ?? 0)}
            </Badge>
          </div>

          {byModel.length === 0 ? (
            <div className="flex h-32 flex-col items-center justify-center gap-1 text-sm text-neutral-400">
              <span>{hasLegacyRevenue ? "该时间段只有历史请求计费" : "该时间段没有成功生成的图片"}</span>
              <Link
                href="/settings"
                className="text-xs text-neutral-500 underline-offset-4 transition hover:text-neutral-800 hover:underline dark:text-neutral-400 dark:hover:text-neutral-100"
              >
                去设置模型定价
              </Link>
            </div>
          ) : (
            <div className="overflow-hidden rounded-xl border border-neutral-200 dark:border-white/10">
              <div className="grid grid-cols-[minmax(0,1fr)_4rem_5.5rem] items-center gap-3 border-b border-neutral-100 bg-neutral-50 px-4 py-2 text-xs font-medium text-neutral-500 sm:grid-cols-[minmax(0,1fr)_5rem_6rem_6rem] dark:border-white/10 dark:bg-white/5 dark:text-neutral-400">
                <span>模型</span>
                <span className="text-right">生成图片</span>
                <span className="hidden text-right sm:block">当前单价</span>
                <span className="text-right">收入</span>
              </div>
              <div className="divide-y divide-neutral-100 dark:divide-white/10">
                {byModel.map((item) => (
                  <div
                    key={item.model}
                    className="grid grid-cols-[minmax(0,1fr)_4rem_5.5rem] items-center gap-3 px-4 py-2.5 text-sm sm:grid-cols-[minmax(0,1fr)_5rem_6rem_6rem]"
                  >
                    <span className="truncate font-mono text-xs text-neutral-800 dark:text-neutral-200" title={item.model}>
                      {item.model}
                    </span>
                    <span className="text-right tabular-nums text-neutral-700 dark:text-neutral-300">{item.images}</span>
                    <span
                      className={cn(
                        "hidden text-right tabular-nums sm:block",
                        item.price === null
                          ? "text-xs text-amber-600 dark:text-amber-400"
                          : "text-neutral-600 dark:text-neutral-400",
                      )}
                    >
                      {item.price === null ? "未定价" : item.price === 0 ? "免费" : formatCurrency(item.price)}
                    </span>
                    <span className="text-right font-medium tabular-nums text-neutral-900 dark:text-neutral-50">
                      {formatCurrency(item.revenue)}
                    </span>
                  </div>
                ))}
              </div>
              <div className="grid grid-cols-[minmax(0,1fr)_4rem_5.5rem] items-center gap-3 border-t border-neutral-200 bg-neutral-50 px-4 py-2.5 text-sm font-semibold text-neutral-900 sm:grid-cols-[minmax(0,1fr)_5rem_6rem_6rem] dark:border-white/10 dark:bg-white/5 dark:text-neutral-50">
                <span>合计</span>
                <span className="text-right tabular-nums">{totals?.images ?? 0}</span>
                <span className="hidden sm:block" />
                <span className="text-right tabular-nums">{formatCurrency(totals?.revenue ?? 0)}</span>
              </div>
            </div>
          )}

          {hasLegacyRevenue ? (
            <div className="text-xs leading-relaxed text-neutral-500 dark:text-neutral-400">
              含历史请求 {totals?.legacy_success ?? 0} 次、{formatCurrency(totals?.legacy_revenue ?? 0)}
              （未记录模型，按主要模型单价估算，已计入合计）
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="space-y-4 p-5">
          <div>
            <div className="font-semibold text-neutral-900 dark:text-neutral-50">按调用方式</div>
            <div className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">
              条形长度 = 各方式占成功图片的比重；成功率条满格为 100%
            </div>
          </div>
          <ImageModeChart modes={data?.by_mode ?? []} />
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button
          variant="ghost"
          className="h-8 rounded-lg px-3 text-neutral-500"
          onClick={() => {
            void loadStats();
            void loadInflight();
          }}
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
        <LoaderCircle className="size-5 animate-spin text-neutral-400" />
      </div>
    );
  }
  return <StatsContent />;
}
