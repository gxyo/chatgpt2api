"use client";

import { useEffect, useMemo, useState } from "react";
import { LoaderCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { ContentLoading } from "@/components/content-loading";
import { FilterCard, FilterField } from "@/components/filter-card";
import { MetricGrid } from "@/components/metric-grid";
import { PageHeader } from "@/components/page-header";
import { WaveChart } from "@/components/wave-chart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { SegmentedControl } from "@/components/ui/segmented";
import {
  fetchRegisterConfig,
  fetchRegisterStats,
  type CloudflareDomainStat,
  type RegisterStatsResponse,
} from "@/lib/api";
import { getBeijingToday, shiftDate } from "@/lib/beijing-time";
import { cn } from "@/lib/utils";
import { useAuthGuard } from "@/lib/use-auth-guard";

import { DomainStatsTable } from "./components/domain-stats-table";

const SUCCESS_DOT = "bg-emerald-500";
const FAILED_DOT = "bg-[#e34948] dark:bg-[#e66767]";
const TOTAL_DOT = "bg-neutral-300 dark:bg-neutral-600";
const RATE_DOT = "bg-brand";

// 与请求统计页保持同一套区间口径，默认今天。
const PRESETS = [
  { key: "today", label: "今天", days: 1 },
  { key: "week", label: "近 7 天", days: 7 },
  { key: "month", label: "近 30 天", days: 30 },
  { key: "all", label: "全部", days: 0 },
] as const;

type PresetKey = (typeof PRESETS)[number]["key"];

/**
 * 注册结果画堆叠柱：成功 + 失败 = 尝试次数，一根柱子就是一个桶，
 * 柱高是总数、蓝段是成功、红段是失败，成功率直接读蓝段占比。
 *
 * 但柱子超过这个密度就退化——相邻柱要留 2px 表面间隙，而「全部」最多能到 400 个日点，
 * 那时柱宽只剩 1px。所以区间一长就退回折线，那是唯一只看长期走势、不需要逐桶细读的场景。
 */
const MAX_BAR_POINTS = 60;

/** 抽出来给 effect 和刷新按钮共用，本身不碰组件状态。 */
async function loadDomainStats(): Promise<CloudflareDomainStat[]> {
  const payload = await fetchRegisterConfig();
  return payload.register.cloudflare_domain_stats ?? [];
}

function RegisterStatsContent() {
  const today = useMemo(() => getBeijingToday(), []);
  const [stats, setStats] = useState<CloudflareDomainStat[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [startDate, setStartDate] = useState(today);
  const [endDate, setEndDate] = useState(today);
  // 预设按钮的高亮状态；手动选过日期后置空，表示当前是自定义区间。
  const [preset, setPreset] = useState<PresetKey | null>("today");
  const [history, setHistory] = useState<RegisterStatsResponse | null>(null);
  const [isHistoryLoading, setIsHistoryLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    // 刻意不用 async/await 的同步 setState 写法：effect 里同步 setState 会踩
    // react-hooks/set-state-in-effect，这里所有 setState 都在 promise 回调里。
    loadDomainStats()
      .then((next) => {
        if (!cancelled) setStats(next);
      })
      .catch(() => {
        if (!cancelled) setStats([]);
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const historyFilters = (nextPreset = preset, start = startDate, end = endDate) =>
    nextPreset === "all" ? { scope: "all" as const } : { start_date: start, end_date: end };

  useEffect(() => {
    let cancelled = false;
    // 与上面加载域名的 effect 同构：所有 setState 都在 promise 回调里，不写在 effect 体内，
    // 否则会踩 react-hooks/set-state-in-effect。
    fetchRegisterStats(historyFilters(preset, startDate, endDate))
      .then((next) => {
        if (!cancelled) setHistory(next);
      })
      .catch((error) => {
        if (!cancelled) {
          setHistory(null);
          toast.error(error instanceof Error ? error.message : "加载注册波形失败");
        }
      })
      .finally(() => {
        if (!cancelled) setIsHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // 与请求统计页一致：筛选条件变化即重新拉取。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preset, startDate, endDate]);

  const applyPreset = (key: PresetKey, days: number) => {
    const end = getBeijingToday();
    const start = key === "all" ? startDate : days <= 1 ? end : shiftDate(end, -(days - 1));
    // 区间没变就不进加载态（effect 不会重跑），变了才清空卡片数字，避免挂着上一个区间的数。
    if (key === preset && (key === "all" || (start === startDate && end === endDate))) {
      return;
    }
    setIsHistoryLoading(true);
    if (key === "all") {
      setPreset("all");
      return;
    }
    setStartDate(start);
    setEndDate(end);
    setPreset(key);
  };

  const refresh = async () => {
    setIsLoading(true);
    setIsHistoryLoading(true);
    try {
      setStats(await loadDomainStats());
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载注册统计失败");
      setStats([]);
    } finally {
      setIsLoading(false);
    }
    try {
      setHistory(await fetchRegisterStats(historyFilters()));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载注册波形失败");
      setHistory(null);
    } finally {
      setIsHistoryLoading(false);
    }
  };

  const range = history?.range;
  const granularity = range?.granularity ?? "hour";
  const rangeLabel = range
    ? range.granularity === "hour"
      ? `${range.start_date} 全天 · 按小时`
      : `${range.start_date} 至 ${range.end_date} · 按天`
    : "";
  // 卡片跟波形共用同一份区间数据：选「今天」就只算今天，不再展示历史累计。
  const totals = history?.totals;
  const cardsLoading = !totals;
  const presetLabel = preset
    ? PRESETS.find((item) => item.key === preset)?.label ?? ""
    : startDate === endDate
      ? startDate
      : `${startDate} 至 ${endDate}`;

  return (
    <section className="space-y-5">
      <PageHeader
        title="账号注册统计"
        description="Cloudflare 临时邮箱注册结果统计，卡片与波形跟随所选区间；改动注册机页的域名不会清空历史。"
      />

      <FilterCard>
        <FilterField label="统计区间">
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
        </FilterField>
        <div className="flex items-center gap-2 sm:ml-auto">
          <Button
            className="h-10 px-5"
            onClick={() => void refresh()}
            disabled={isLoading || isHistoryLoading}
          >
            {isLoading || isHistoryLoading ? (
              <LoaderCircle className="size-4 animate-spin" />
            ) : (
              <RefreshCw className="size-4" />
            )}
            刷新
          </Button>
        </div>
      </FilterCard>

      <Card className="overflow-hidden">
        <div className="grid xl:grid-cols-[minmax(0,1.65fr)_minmax(340px,1fr)]">
          <div className="flex min-w-0 flex-col p-5 lg:p-6">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-[15px] font-semibold tracking-tight">注册结果波形</div>
                <div className="mt-1 text-xs text-muted-foreground">{rangeLabel || " "}</div>
                {/* 卡片和波形都来自同一份区间账本，只能从本功能上线后开始记——不写清楚会被当成 bug。 */}
                <div className="mt-0.5 text-xs text-muted-foreground/70">
                  波形自本功能上线后开始记录，更早的历史不在图中
                </div>
              </div>
              {history?.peak ? (
                <Badge variant="info" className="rounded-full px-2.5 py-1 tabular-nums">
                  峰值 {history.peak.success} · {history.peak.full_label}
                </Badge>
              ) : null}
            </div>

            <div className="mt-4 flex-1">
              {isHistoryLoading && !history ? (
                <div className="flex h-[288px] items-center justify-center text-sm text-neutral-400">
                  <LoaderCircle className="mr-2 size-4 animate-spin" />
                  加载中
                </div>
              ) : history && history.totals.requests > 0 ? (
                <WaveChart
                  series={history.series}
                  granularity={granularity}
                  primary="success"
                  form={history.series.length > MAX_BAR_POINTS ? "line" : "bar"}
                  labels={{
                    primary: "成功数",
                    secondary: "失败数",
                    ariaLabel: "注册结果波形图，按{axis}展示成功数与失败数",
                  }}
                />
              ) : (
                <div className="flex h-[288px] flex-col items-center justify-center gap-1 text-sm text-neutral-400">
                  <span>该时间段没有注册记录</span>
                  <span className="text-xs text-neutral-400/80">换一个日期范围试试</span>
                </div>
              )}
            </div>
          </div>

          <div className="border-t border-border xl:border-t-0 xl:border-l">
            <MetricGrid
              className="h-full auto-rows-fr"
              items={[
                {
                  label: "成功",
                  value: cardsLoading ? "—" : String(totals.success),
                  caption: `${presetLabel} · Cloudflare 临时邮箱`,
                  tone: SUCCESS_DOT,
                },
                {
                  label: "失败",
                  value: cardsLoading ? "—" : String(totals.failed),
                  caption: `${presetLabel} · 含超时与上游异常`,
                  tone: FAILED_DOT,
                },
                {
                  label: "总数",
                  value: cardsLoading ? "—" : String(totals.requests),
                  caption: `${presetLabel} · 成功 + 失败`,
                  tone: TOTAL_DOT,
                },
                {
                  label: "成功率",
                  value: cardsLoading
                    ? "—"
                    : totals.requests
                      ? `${(totals.success_rate * 100).toFixed(1)}%`
                      : "-",
                  caption: `${presetLabel} · 按总数加权`,
                  tone: RATE_DOT,
                },
              ]}
            />
          </div>
        </div>
      </Card>

      <Card>
        <CardContent className="space-y-4 p-5 lg:p-6">
          <div>
            <div className="text-[15px] font-semibold tracking-tight">按域名</div>
            <div className="mt-1 text-xs text-muted-foreground">
              每个域名的累计注册结果，「重置」不会清空这里的数据。
            </div>
          </div>
          {isLoading ? <ContentLoading label="正在加载注册统计" /> : <DomainStatsTable stats={stats} />}
        </CardContent>
      </Card>
    </section>
  );
}

export default function RegisterStatsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-neutral-400" />
      </div>
    );
  }
  return <RegisterStatsContent />;
}
