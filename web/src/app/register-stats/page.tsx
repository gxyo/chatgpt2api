"use client";

import { useEffect, useState } from "react";
import { LoaderCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { StatTile } from "@/components/stat-tile";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { fetchRegisterConfig, type CloudflareDomainStat } from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { cn } from "@/lib/utils";

import { DomainStatsTable } from "./components/domain-stats-table";

const SUCCESS_DOT = "bg-emerald-500";
const FAILED_DOT = "bg-[#e34948] dark:bg-[#e66767]";
const TOTAL_DOT = "bg-neutral-300 dark:bg-neutral-600";
const RATE_DOT = "bg-[#2a78d6] dark:bg-[#3987e5]";

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
  const [stats, setStats] = useState<CloudflareDomainStat[]>([]);
  const [isLoading, setIsLoading] = useState(true);

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

  const refresh = async () => {
    setIsLoading(true);
    try {
      setStats(await loadDomainStats());
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载注册统计失败");
      setStats([]);
    } finally {
      setIsLoading(false);
    }
  };

  const totals = sumTotals(stats);

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
        <Button
          variant="outline"
          className="h-10 rounded-xl border-neutral-200 bg-white px-4 text-neutral-700"
          onClick={() => void refresh()}
          disabled={isLoading}
        >
          <RefreshCw className={cn("size-4", isLoading ? "animate-spin" : "")} />
          刷新
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="累计成功"
          value={String(totals.success)}
          caption="Cloudflare 临时邮箱"
          tone={SUCCESS_DOT}
        />
        <StatTile
          label="累计失败"
          value={String(totals.fail)}
          caption="含超时与上游异常"
          tone={FAILED_DOT}
        />
        <StatTile
          label="累计总数"
          value={String(totals.total)}
          caption="成功 + 失败"
          tone={TOTAL_DOT}
        />
        <StatTile
          label="整体成功率"
          value={totals.total ? `${totals.successRate.toFixed(1)}%` : "-"}
          caption="按总数加权，非各域名平均"
          tone={RATE_DOT}
        />
      </div>

      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="space-y-4 p-5">
          <div>
            <div className="font-semibold text-neutral-900 dark:text-neutral-50">按域名</div>
            <div className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">
              每个域名的累计注册结果，「重置」不会清空这里的数据。
            </div>
          </div>
          <DomainStatsTable stats={stats} />
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
