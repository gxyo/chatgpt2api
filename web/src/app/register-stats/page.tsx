"use client";

import { useEffect, useMemo, useState } from "react";
import { LoaderCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { ContentLoading } from "@/components/content-loading";
import { StatTile } from "@/components/stat-tile";
import { WaveChart } from "@/components/wave-chart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
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
const RATE_DOT = "bg-[#2a78d6] dark:bg-[#3987e5]";

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

function sumTotals(stats: CloudflareDomainStat[]) {
  // 对完整数组求和（不要先按 configured 过滤）：被移除的域名也是历史的一部分。
  const totals = stats.reduce(
    (acc, item) => ({ success: acc.success + item.success, fail: acc.fail + item.fail }),
    { success: 0, fail: 0 },
  );
  const total = totals.success + totals.fail;
  // 用总数加权重算，而不是对后端的 success_rate 求平均——后端是四舍五入到 1 位的小数，
  // 直接平均会让各域名权重失真。
  return { ...totals, total, successRate: total ? (totals.success * 100) / total : 0 };
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
    if (key === "all") {
      setPreset("all");
      return;
    }
    const end = getBeijingToday();
    setStartDate(days <= 1 ? end : shiftDate(end, -(days - 1)));
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

  const totals = sumTotals(stats);
  const range = history?.range;
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
          <div className="text-xs font-semibold tracking-[0.18em] text-neutral-500 uppercase">
            Registration
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">账号注册统计</h1>
          <p className="text-sm text-neutral-500 dark:text-neutral-400">
            Cloudflare 临时邮箱注册结果的长期累计，改动注册机页的域名不会清空历史。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <div className="flex items-center gap-1 rounded-xl border border-neutral-200 bg-white p-1 dark:border-white/10 dark:bg-white/5">
            {PRESETS.map((item) => (
              <button
                key={item.key}
                type="button"
                aria-pressed={preset === item.key}
                className={cn(
                  "rounded-lg px-3 py-1.5 text-[13px] font-medium transition",
                  preset === item.key
                    ? "bg-neutral-900 text-white shadow-sm dark:bg-white dark:text-neutral-900"
                    : "text-neutral-600 hover:bg-neutral-100 hover:text-neutral-900 dark:text-neutral-300 dark:hover:bg-white/10 dark:hover:text-white",
                )}
                onClick={() => applyPreset(item.key, item.days)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <Button
            className="h-10 rounded-xl bg-neutral-950 px-4 text-white hover:bg-neutral-800"
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
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="累计成功"
          value={isLoading ? "—" : String(totals.success)}
          caption="Cloudflare 临时邮箱"
          tone={SUCCESS_DOT}
        />
        <StatTile
          label="累计失败"
          value={isLoading ? "—" : String(totals.fail)}
          caption="含超时与上游异常"
          tone={FAILED_DOT}
        />
        <StatTile
          label="累计总数"
          value={isLoading ? "—" : String(totals.total)}
          caption="成功 + 失败"
          tone={TOTAL_DOT}
        />
        <StatTile
          label="整体成功率"
          value={isLoading ? "—" : totals.total ? `${totals.successRate.toFixed(1)}%` : "-"}
          caption="按总数加权，非各域名平均"
          tone={RATE_DOT}
        />
      </div>

      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="space-y-4 p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="font-semibold text-neutral-900 dark:text-neutral-50">注册结果波形</div>
              <div className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">
                {rangeLabel || " "}
              </div>
              {/* 上面的卡片是历史累计，波形只能从本功能上线后开始记——不写清楚会被当成 bug。 */}
              <div className="mt-0.5 text-xs text-neutral-400 dark:text-neutral-500">
                波形自本功能上线后开始记录，更早的历史不在图中
              </div>
            </div>
            {history?.peak ? (
              <Badge variant="info" className="rounded-md px-2.5 py-1 tabular-nums">
                峰值 {history.peak.success} · {history.peak.full_label}
              </Badge>
            ) : null}
          </div>

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
        </CardContent>
      </Card>

      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="space-y-4 p-5">
          <div>
            <div className="font-semibold text-neutral-900 dark:text-neutral-50">按域名</div>
            <div className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">
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
