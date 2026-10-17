"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import { ArrowRight, ChevronLeft, ChevronRight, Download, HardDrive, ImageIcon, ListChecks, LoaderCircle, RefreshCw, RotateCcw, Route, Save, Search, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { ContentLoading } from "@/components/content-loading";
import { DateRangeFilter } from "@/components/date-range-filter";
import { ImageLightbox } from "@/components/image-lightbox";
import { PageSizeSelect } from "@/components/page-size-select";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cleanupSystemLogs, deleteSystemLogs, exportSystemLogs, fetchLogRetention, fetchSystemLogs, saveLogRetention, type LogRetentionInfo, type SystemLog } from "@/lib/api";
import { formatBeijingClock, getBeijingToday, shiftDate } from "@/lib/beijing-time";
import { useAuthGuard } from "@/lib/use-auth-guard";

const LogType = {
  Call: "call",
  Account: "account",
} as const;

const LogStatus = {
  All: "all",
  Success: "success",
  Failed: "failed",
  PolicyViolation: "policy_violation",
} as const;

const typeLabels: Record<string, string> = {
  [LogType.Call]: "调用日志",
  [LogType.Account]: "账号管理日志",
};

/** 详情弹窗里的字段名：翻译一下常用字段，其余保留原始键名。 */
const detailLabels: Record<string, string> = {
  request_text: "请求内容",
  error: "错误信息",
  upscale_target: "超分目标(px)",
  upscale_from_size: "超分前尺寸",
  upscale_to_size: "超分后尺寸",
  upscale_images: "超分张数",
  upscale_ms: "超分耗时(ms)",
  upscale_queue_wait_ms: "超分排队(ms)",
  upscale_error: "超分失败原因",
};

/** 这两个字段单独排版，不进右侧对齐的键值网格。 */
const trailDetailKeys = new Set(["account_trail", "attempts_summary"]);

type AttemptOutcomeStyle = {
  label: string;
  variant: "success" | "warning" | "danger" | "secondary";
};

const attemptOutcomes: Record<string, AttemptOutcomeStyle> = {
  success: { label: "成功", variant: "success" },
  message: { label: "返回文案", variant: "warning" },
  message_retry: { label: "文案重试", variant: "warning" },
  poll_timeout: { label: "轮询超时", variant: "warning" },
  transient_upstream: { label: "上游抖动", variant: "warning" },
  mainline_rejected: { label: "主链路拒绝", variant: "warning" },
  content_policy: { label: "内容拦截", variant: "danger" },
  generation_error: { label: "生成失败", variant: "danger" },
  no_image: { label: "未返回图片", variant: "danger" },
  empty_stream: { label: "空流", variant: "warning" },
  account_select_failed: { label: "选号失败", variant: "danger" },
  unknown: { label: "未知", variant: "secondary" },
};

function attemptOutcomeStyle(outcome: string): AttemptOutcomeStyle {
  return attemptOutcomes[outcome] || { label: outcome || "未知", variant: "secondary" };
}

/** 账号轨迹是后端拼好的一行 `a → b → c`。 */
function splitAccountTrail(value: unknown): string[] {
  if (typeof value !== "string") {
    return [];
  }
  return value.split("→").map((item) => item.trim()).filter(Boolean);
}

/**
 * 旧日志里同一段换号可能被每个流式 chunk 各拼了一遍（A→B→A→B…），
 * 显示前把周期性重复压成一轮，别让两个账号看起来像换了二十次。
 */
function collapseAccountTrail(accounts: string[]): string[] {
  const deduped = accounts.filter((email, index) => index === 0 || email !== accounts[index - 1]);
  const length = deduped.length;
  for (let period = 1; period <= Math.floor(length / 2); period += 1) {
    let periodic = true;
    for (let index = period; index < length; index += 1) {
      if (deduped[index] !== deduped[index % period]) {
        periodic = false;
        break;
      }
    }
    if (periodic) {
      const remainder = length % period;
      return remainder
        ? [...deduped.slice(0, period), ...deduped.slice(length - remainder)]
        : deduped.slice(0, period);
    }
  }
  return deduped;
}

type AttemptStep = {
  index: string;
  email: string;
  outcome: string;
  marks: string[];
  durationMs: number | null;
  error: string;
};

type AttemptGroup = {
  raw: string;
  steps: AttemptStep[];
  parsed: boolean;
};

/** 一条尝试：`#1 账号 结局 (标记) 123ms 错误`，结局之后几段都是可选的。 */
function parseAttemptStep(text: string): AttemptStep | null {
  const match = text.trim().match(/^#(\d+)\s+(\S*)\s+([A-Za-z_]+)(?:\s+\(([^)]*)\))?(?:\s+(\d+)ms)?(?:\s+(.*))?$/);
  if (!match) {
    return null;
  }
  return {
    index: `#${match[1]}`,
    email: match[2] === "?" ? "" : match[2],
    outcome: match[3],
    marks: (match[4] || "").split(",").map((mark) => mark.trim()).filter(Boolean),
    durationMs: match[5] === undefined ? null : Number(match[5]),
    error: (match[6] || "").trim(),
  };
}

/** 尝试轨迹：`#1 … → #2 …`；多张图并行时各组之间用 ` | ` 分隔。 */
function parseAttemptGroups(value: unknown): AttemptGroup[] {
  if (typeof value !== "string" || !value.trim()) {
    return [];
  }
  return value.split(" | ").map((raw) => {
    const parts = raw.split(" → ");
    const steps = parts.map(parseAttemptStep).filter((step): step is AttemptStep => step !== null);
    const parsed = steps.length === parts.length;
    return { raw, steps: parsed ? steps : [], parsed };
  });
}

function formatAttemptDuration(milliseconds: number | null) {
  return milliseconds === null ? "—" : `${(milliseconds / 1000).toFixed(2)} s`;
}

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 10 || index === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
}

/** 保留最近 days 天时，会被删掉的是这一天之前（不含）的日志。 */
function cleanupCutoffDay(days: number) {
  return shiftDate(getBeijingToday(), -(Math.max(1, days) - 1));
}

function formatDuration(item: SystemLog) {
  const value = item.detail?.duration_ms;
  return typeof value === "number" ? `${(value / 1000).toFixed(2)} s` : "-";
}

function getUrls(item: SystemLog | null) {
  const urls = item?.detail?.urls;
  return Array.isArray(urls) ? urls.filter((url): url is string => typeof url === "string") : [];
}

/**
 * 本次生图是否走了账号链路重试（换号重来）。
 * 后端只在真的换过号时才写 account_trail / attempts_summary，两个字段有任意一个即视为重试。
 */
function isAccountRetry(item: SystemLog) {
  const trail = item.detail?.account_trail;
  const attempts = item.detail?.attempts_summary;
  return (
    (typeof trail === "string" && trail.trim() !== "") ||
    (typeof attempts === "string" && attempts.trim() !== "")
  );
}

/** 重试列的小卡片：是=琥珀色，否=中性灰，避免纯文字在表格里不好扫。 */
function RetryCard({ retried }: { retried: boolean }) {
  return (
    <span
      className={`inline-flex w-14 items-center justify-center gap-1.5 rounded-lg border px-2 py-1 text-xs font-medium shadow-sm ${
        retried
          ? "border-amber-200 bg-amber-50 text-amber-700"
          : "border-neutral-200 bg-neutral-50 text-neutral-500"
      }`}
    >
      {retried ? <RotateCcw className="size-3.5" /> : <span className="size-1.5 rounded-full bg-neutral-400" />}
      {retried ? "是" : "否"}
    </span>
  );
}

/** 数量列的小卡片：本次生图实际返回的图片张数。 */
function ImageCountCard({ count }: { count: number }) {
  return (
    <span className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-neutral-200 bg-neutral-50 px-2 py-1 text-xs font-medium text-neutral-600 tabular-nums shadow-sm">
      <ImageIcon className="size-3.5 text-neutral-400" />
      {count} 张
    </span>
  );
}

type UpstreamErrorFrame = {
  type?: string;
  message?: string;
  context?: string;
  status_code?: number;
  code?: string;
  retry_after?: number;
  body?: string;
};

/** 上游报错链：第一条是抛给用户的异常，越往后越接近上游原始响应。 */
function getUpstreamError(item: SystemLog | null): UpstreamErrorFrame[] {
  const frames = item?.detail?.upstream_error;
  if (!Array.isArray(frames)) {
    return [];
  }
  return frames.filter((frame): frame is UpstreamErrorFrame => typeof frame === "object" && frame !== null);
}

function getStatus(item: SystemLog) {
  const status = item.detail?.status;
  if (status === "success") return "成功";
  if (status === "failed") return "失败";
  if (status === "policy_violation") return "违反安全策略";
  return "-";
}

function getStatusVariant(item: SystemLog) {
  const status = item.detail?.status;
  if (status === "failed") return "danger";
  if (status === "policy_violation") return "warning";
  return "success";
}

function LogsContent() {
  const [items, setItems] = useState<SystemLog[]>([]);
  const [type, setType] = useState<string>(LogType.Call);
  const [status, setStatus] = useState<string>(LogStatus.All);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [detailLog, setDetailLog] = useState<SystemLog | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [lightboxIndex, setLightboxIndex] = useState(0);
  const [lightboxOpen, setLightboxOpen] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [isLoading, setIsLoading] = useState(true);
  const [isDeleting, setIsDeleting] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [deletingItems, setDeletingItems] = useState<SystemLog[]>([]);
  const [retention, setRetention] = useState<LogRetentionInfo | null>(null);
  const [retentionDays, setRetentionDays] = useState("30");
  const [autoCleanup, setAutoCleanup] = useState(false);
  const [cleanupTime, setCleanupTime] = useState("03:00");
  const [isSavingRetention, setIsSavingRetention] = useState(false);
  const [isCleaning, setIsCleaning] = useState(false);
  const [cleanupOpen, setCleanupOpen] = useState(false);
  const [exportLimit, setExportLimit] = useState("10");
  const [isExporting, setIsExporting] = useState(false);
  const detailUrls = getUrls(detailLog);
  const detailUpstreamError = getUpstreamError(detailLog);
  const detail = detailLog?.detail || {};
  const detailFields = Object.entries(detail).filter(
    ([key, value]) => key !== "urls" && !trailDetailKeys.has(key) && typeof value !== "object",
  );
  const accountTrail = collapseAccountTrail(splitAccountTrail(detail.account_trail));
  const attemptGroups = parseAttemptGroups(detail.attempts_summary);
  const detailImages = detailUrls.map((url, index) => ({ id: `${index}`, src: url }));
  const isCallLog = type === LogType.Call;
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));
  const safePage = Math.min(page, pageCount);
  const currentRows = items.slice((safePage - 1) * pageSize, safePage * pageSize);
  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);
  const currentPageSelected = currentRows.length > 0 && currentRows.every((item) => selectedSet.has(item.id));
  const allSelected = items.length > 0 && items.every((item) => selectedSet.has(item.id));

  const loadLogs = async () => {
    setIsLoading(true);
    try {
      const data = await fetchSystemLogs({
        type,
        status: status === LogStatus.All ? "" : status,
        start_date: startDate,
        end_date: endDate,
      });
      setItems(data.items);
      setSelectedIds((current) => current.filter((id) => data.items.some((item) => item.id === id)));
      setPage(1);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载日志失败");
    } finally {
      setIsLoading(false);
    }
  };

  const loadRetention = async () => {
    try {
      const data = await fetchLogRetention();
      setRetention(data);
      setRetentionDays(String(data.days));
      setAutoCleanup(data.auto_cleanup);
      setCleanupTime(data.cleanup_time || "03:00");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "读取日志清理设置失败");
    }
  };

  const parsedRetentionDays = Number.parseInt(retentionDays, 10);
  const retentionValid = Number.isFinite(parsedRetentionDays) && parsedRetentionDays >= 1;
  const cleanupTimeValid = /^([01]\d|2[0-3]):[0-5]\d$/.test(cleanupTime);

  const saveRetention = async () => {
    if (!retentionValid) {
      toast.error("保留天数至少为 1");
      return;
    }
    if (!cleanupTimeValid) {
      toast.error("请选择每天自动清理的时间");
      return;
    }
    setIsSavingRetention(true);
    try {
      await saveLogRetention({
        log_retention_days: parsedRetentionDays,
        log_auto_cleanup: autoCleanup,
        log_cleanup_time: cleanupTime,
      });
      await loadRetention();
      toast.success("已保存日志清理设置");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存日志清理设置失败");
    } finally {
      setIsSavingRetention(false);
    }
  };

  const confirmCleanup = async () => {
    setIsCleaning(true);
    try {
      // 用输入框里的天数，而不是已保存的设置：弹窗承诺删到哪一天，就按哪一天执行。
      const data = await cleanupSystemLogs(parsedRetentionDays);
      toast.success(
        data.removed > 0
          ? `已清理 ${data.removed} 条日志，保留 ${data.kept} 条（统计不受影响）`
          : "没有需要清理的日志",
      );
      setCleanupOpen(false);
      setSelectedIds([]);
      await Promise.all([loadLogs(), loadRetention()]);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "清理日志失败");
    } finally {
      setIsCleaning(false);
    }
  };

  const parsedExportLimit = Number.parseInt(exportLimit, 10);
  const exportLimitValid = Number.isFinite(parsedExportLimit) && parsedExportLimit >= 1 && parsedExportLimit <= 500;

  const exportLogs = async () => {
    if (!exportLimitValid) {
      toast.error("导出条数需在 1 - 500 之间");
      return;
    }
    setIsExporting(true);
    try {
      // 跟着当前筛选走：只想导失败日志时，导出的就是失败的那几条。
      const count = await exportSystemLogs({
        type,
        status: status === LogStatus.All ? "" : status,
        start_date: startDate,
        end_date: endDate,
        limit: parsedExportLimit,
      });
      if (count === null) {
        toast.success("已导出日志文件");
      } else if (count > 0) {
        toast.success(`已导出 ${count} 条日志`);
      } else {
        toast.success("已导出日志文件（没有符合条件的记录）");
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "导出日志失败");
    } finally {
      setIsExporting(false);
    }
  };

  const clearFilters = () => {
    setStatus(LogStatus.All);
    setStartDate("");
    setEndDate("");
  };

  const changeType = (value: string) => {
    setType(value);
    // 成功/失败只对调用日志有意义，账号日志的 status 记的是账号状态（正常/限流/禁用）。
    if (value !== LogType.Call) {
      setStatus(LogStatus.All);
    }
  };

  const openDetail = (item: SystemLog) => {
    setDetailLog(item);
    setDetailOpen(true);
  };

  const toggleIds = (ids: string[], checked: boolean) => {
    setSelectedIds((current) => checked ? Array.from(new Set([...current, ...ids])) : current.filter((id) => !ids.includes(id)));
  };

  const confirmDelete = async () => {
    const ids = deletingItems.map((item) => item.id);
    if (ids.length === 0) return;
    setIsDeleting(true);
    try {
      const data = await deleteSystemLogs(ids);
      toast.success(`已删除 ${data.removed} 条日志`);
      setDeletingItems([]);
      setSelectedIds((current) => current.filter((id) => !ids.includes(id)));
      if (detailLog && ids.includes(detailLog.id)) {
        setDetailOpen(false);
        setDetailLog(null);
      }
      await loadLogs();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除日志失败");
    } finally {
      setIsDeleting(false);
    }
  };

  useEffect(() => {
    void loadLogs();
  }, [type, status, startDate, endDate]);

  useEffect(() => {
    void loadRetention();
  }, []);

  return (
    <section className="space-y-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-1">
          <div className="text-xs font-semibold tracking-[0.18em] text-neutral-500 uppercase">Logs</div>
          <h1 className="text-2xl font-semibold tracking-tight">日志管理</h1>
        </div>
        <div className="flex flex-wrap gap-2">
          <Select value={type} onValueChange={changeType}>
            <SelectTrigger className="h-10 w-[150px] rounded-xl border-neutral-200 bg-white"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value={LogType.Call}>调用日志</SelectItem>
              <SelectItem value={LogType.Account}>账号管理日志</SelectItem>
            </SelectContent>
          </Select>
          {isCallLog ? (
            <Select value={status} onValueChange={setStatus}>
              <SelectTrigger className="h-10 w-[150px] rounded-xl border-neutral-200 bg-white"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value={LogStatus.All}>全部状态</SelectItem>
                <SelectItem value={LogStatus.Success}>成功</SelectItem>
                <SelectItem value={LogStatus.Failed}>失败</SelectItem>
                <SelectItem value={LogStatus.PolicyViolation}>违反安全策略</SelectItem>
              </SelectContent>
            </Select>
          ) : null}
          <DateRangeFilter startDate={startDate} endDate={endDate} onChange={(start, end) => { setStartDate(start); setEndDate(end); }} />
          <Button variant="outline" onClick={clearFilters} className="h-10 rounded-xl border-neutral-200 bg-white px-4 text-neutral-700">
            清除筛选条件
          </Button>
          <Button onClick={() => void loadLogs()} disabled={isLoading} className="h-10 rounded-xl bg-neutral-950 px-4 text-white hover:bg-neutral-800">
            {isLoading ? <LoaderCircle className="size-4 animate-spin" /> : <Search className="size-4" />}
            查询
          </Button>
          {/* 一键导出：把最近 N 条日志的完整报文存成 txt，
              遇到问题时直接把这个文件发出来，不用一条条截图。 */}
          <label className="flex h-10 items-center gap-2 rounded-xl border border-neutral-200 bg-white px-3 text-sm text-neutral-600">
            导出最近
            <Input
              value={exportLimit}
              onChange={(event) => setExportLimit(event.target.value)}
              inputMode="numeric"
              aria-label="导出条数"
              className="h-8 w-14 rounded-lg border-neutral-200 bg-white px-1 text-center"
            />
            条
          </label>
          <Button
            variant="outline"
            onClick={() => void exportLogs()}
            disabled={isExporting || !exportLimitValid}
            title="导出最近 N 条日志的完整报文（txt），按时间由旧到新排列"
            className="h-10 rounded-xl border-neutral-200 bg-white px-4 text-neutral-700"
          >
            {isExporting ? <LoaderCircle className="size-4 animate-spin" /> : <Download className="size-4" />}
            导出日志
          </Button>
        </div>
      </div>

      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="flex flex-col gap-4 p-5 xl:flex-row xl:items-center xl:justify-between">
          <div className="space-y-1">
            <div className="flex items-center gap-2 text-sm font-medium text-neutral-700">
              <HardDrive className="size-4 text-neutral-400" />
              日志清理
            </div>
            <p className="text-sm text-neutral-500">
              当前占用 {formatBytes(retention?.size_bytes ?? 0)}
              {retention?.updated_at ? `，最后写入 ${formatBeijingClock(new Date(retention.updated_at * 1000).toISOString())}` : ""}
              。清理只删日志文件，统计页的数据会保留。
            </p>
          </div>
          {/* shrink-0：这一组控件不参与收缩，宽度不够时让左边的说明文字多换一行，
              而不是把控件折到第二行去。 */}
          <div className="flex shrink-0 flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-sm text-neutral-600">
              保留最近
              <Input
                value={retentionDays}
                onChange={(event) => setRetentionDays(event.target.value)}
                inputMode="numeric"
                className="h-10 w-20 rounded-xl border-neutral-200 bg-white text-center"
              />
              天
            </label>
            <label className="flex items-center gap-2 text-sm text-neutral-600">
              <Checkbox checked={autoCleanup} onCheckedChange={(checked) => setAutoCleanup(Boolean(checked))} />
              每天自动清理
            </label>
            <label
              className="flex items-center gap-2 text-sm text-neutral-600"
              title="按北京时间（UTC+8）执行"
            >
              <Input
                type="time"
                step={60}
                value={cleanupTime}
                onChange={(event) => setCleanupTime(event.target.value)}
                disabled={!autoCleanup}
                className="h-10 w-32 rounded-xl border-neutral-200 bg-white text-center disabled:cursor-not-allowed disabled:opacity-50"
              />
              <span className={autoCleanup ? "" : "text-neutral-400"}>北京时间</span>
            </label>
            {/* 两个按钮绑成一组：真到了换行的时候也一起走，
                「立即清理」永远紧跟在「保存设置」后面，不会单独占一行。 */}
            <div className="flex items-center gap-3">
              <Button variant="outline" className="h-10 rounded-xl border-neutral-200 bg-white px-4" onClick={() => void saveRetention()} disabled={isSavingRetention || !retentionValid || !cleanupTimeValid}>
                {/* 图标常驻再原地换成转圈：只在加载时插入图标会让按钮变宽（图标 + gap-2，
                    外加基础样式的 has-[>svg]:px-3 收窄内边距），把「立即清理」挤到下一行。
                    同页的「查询」按钮也是这么换的。 */}
                {isSavingRetention ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
                保存设置
              </Button>
              <Button
                variant="outline"
                className="h-10 rounded-xl border-rose-200 bg-white px-4 text-rose-600 hover:bg-rose-50"
                onClick={() => setCleanupOpen(true)}
                disabled={isCleaning || !retentionValid}
              >
                <Trash2 className="size-4" />
                立即清理
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card className="overflow-hidden rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="p-0">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-neutral-100 px-5 py-4">
            <div className="flex flex-wrap items-center gap-3 text-sm text-neutral-600">
              <span>共 {isLoading && items.length === 0 ? "—" : items.length} 条</span>
              <label className="flex items-center gap-2">
                <Checkbox checked={currentPageSelected} onCheckedChange={(checked) => toggleIds(currentRows.map((item) => item.id), Boolean(checked))} />
                本页全选
              </label>
              <label className="flex items-center gap-2">
                <Checkbox checked={allSelected} onCheckedChange={(checked) => toggleIds(items.map((item) => item.id), Boolean(checked))} />
                全选结果
              </label>
              {selectedIds.length > 0 ? <span>已选 {selectedIds.length} 条</span> : null}
            </div>
            <div className="flex items-center gap-2">
              <Button variant="ghost" className="h-8 rounded-lg px-3 text-neutral-500" onClick={() => void loadLogs()} disabled={isLoading}>
                <RefreshCw className={`size-4 ${isLoading ? "animate-spin" : ""}`} />
                刷新
              </Button>
              <button type="button" className="text-sm text-neutral-500 hover:text-neutral-900 disabled:text-neutral-300" onClick={() => setSelectedIds([])} disabled={selectedIds.length === 0 || isDeleting}>
                取消选择
              </button>
              <Button variant="outline" className="h-8 rounded-lg border-rose-200 bg-white px-3 text-rose-600 hover:bg-rose-50" onClick={() => setDeletingItems(items.filter((item) => selectedSet.has(item.id)))} disabled={selectedIds.length === 0 || isDeleting}>
                <Trash2 className="size-4" />
                删除所选
              </Button>
            </div>
          </div>
          <div className="overflow-x-auto">
            <Table className="min-w-[900px]">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-12"></TableHead>
                  <TableHead>时间</TableHead>
                  <TableHead>类型</TableHead>
                  {isCallLog ? <TableHead>调用耗时</TableHead> : null}
                  {isCallLog ? <TableHead>状态</TableHead> : null}
                  {isCallLog ? <TableHead>重试</TableHead> : null}
                  {isCallLog ? <TableHead>数量</TableHead> : null}
                  <TableHead>简述</TableHead>
                  <TableHead className="w-40">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {isLoading && items.length === 0 ? (
                  <TableRow className="hover:bg-transparent">
                    <TableCell colSpan={isCallLog ? 9 : 5} className="p-0">
                      <ContentLoading label="正在加载日志" />
                    </TableCell>
                  </TableRow>
                ) : null}
                {currentRows.map((item) => {
                  const urls = getUrls(item);
                  return (
                    <TableRow key={item.id} className="text-neutral-600">
                      <TableCell>
                        <Checkbox checked={selectedSet.has(item.id)} onCheckedChange={(checked) => toggleIds([item.id], Boolean(checked))} />
                      </TableCell>
                      <TableCell className="whitespace-nowrap">{item.time}</TableCell>
                      <TableCell><Badge variant="secondary" className="rounded-md">{typeLabels[item.type] || item.type}</Badge></TableCell>
                      {isCallLog ? <TableCell>{formatDuration(item)}</TableCell> : null}
                      {isCallLog ? (
                        <TableCell>
                          <Badge variant={getStatusVariant(item)} className="rounded-md">
                            {getStatus(item)}
                          </Badge>
                        </TableCell>
                      ) : null}
                      {isCallLog ? (
                        <TableCell>
                          <RetryCard retried={isAccountRetry(item)} />
                        </TableCell>
                      ) : null}
                      {isCallLog ? (
                        <TableCell>
                          <ImageCountCard count={urls.length} />
                        </TableCell>
                      ) : null}
                      <TableCell className="max-w-[420px] truncate text-neutral-500">{item.summary || "-"}</TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1">
                          <Button variant="ghost" className="h-8 rounded-lg px-3 text-neutral-600" onClick={() => openDetail(item)}>
                            查看详情
                          </Button>
                          <Button variant="ghost" className="h-8 rounded-lg px-3 text-rose-600 hover:bg-rose-50 hover:text-rose-700" onClick={() => setDeletingItems([item])}>
                            删除
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-x-3 gap-y-2 border-t border-neutral-100 px-4 py-3 text-sm text-neutral-500">
            <PageSizeSelect
              value={pageSize}
              options={[10, 20, 30, 40, 50, 100]}
              onChange={(size) => {
                setPageSize(size);
                setPage(1);
              }}
            />
            <span>第 {safePage} / {pageCount} 页，共 {items.length} 条</span>
            <Button variant="outline" size="icon" className="size-9 rounded-lg border-neutral-200 bg-white" disabled={safePage <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>
              <ChevronLeft className="size-4" />
            </Button>
            <Button variant="outline" size="icon" className="size-9 rounded-lg border-neutral-200 bg-white" disabled={safePage >= pageCount} onClick={() => setPage((value) => Math.min(pageCount, value + 1))}>
              <ChevronRight className="size-4" />
            </Button>
          </div>
          {!isLoading && items.length === 0 ? <div className="px-6 py-14 text-center text-sm text-neutral-500">没有找到日志</div> : null}
        </CardContent>
      </Card>
      <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
        <DialogContent className="flex h-[min(88vh,860px)] w-[min(92vw,920px)] flex-col overflow-hidden rounded-2xl p-0">
          <DialogHeader className="shrink-0 border-b border-neutral-100 px-6 py-5">
            <DialogTitle>日志详情</DialogTitle>
          </DialogHeader>
          <div className="flex-1 overflow-y-auto px-6 py-5">
            <div className="space-y-4">
              <div className="grid gap-x-6 gap-y-3 rounded-xl border border-neutral-200 bg-white p-4 text-sm text-neutral-600 md:grid-cols-2">
                {detailFields.map(([key, value]) => {
                  const label = detailLabels[key] || key;
                  // 提示词/错误信息是长文本，占满整行、左对齐，右对齐会挤成难读的一团。
                  if (key === "request_text" || key === "error") {
                    return (
                      <div key={key} className="space-y-1 md:col-span-2">
                        <div className="text-xs text-neutral-400">{label}</div>
                        <p className="leading-6 break-words whitespace-pre-wrap text-neutral-700">{String(value)}</p>
                      </div>
                    );
                  }
                  return (
                    <div key={key} className="flex min-w-0 items-start justify-between gap-4">
                      {/* shrink-0 + nowrap：格子变窄时标签不应该被压成竖排的单字。 */}
                      <span className="shrink-0 whitespace-nowrap text-neutral-400">{label}</span>
                      <span className="min-w-0 text-right font-medium break-words text-neutral-700">{String(value)}</span>
                    </div>
                  );
                })}
              </div>
              {accountTrail.length ? (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm font-medium text-neutral-700">
                    <Route className="size-4 text-neutral-400" />
                    账号轨迹
                    <span className="text-xs font-normal text-neutral-400">这次请求实际用过的账号，按尝试顺序</span>
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5 rounded-xl border border-neutral-200 bg-white p-3">
                    {accountTrail.map((email, index) => (
                      <Fragment key={`${email}-${index}`}>
                        {index > 0 ? <ArrowRight className="size-3.5 shrink-0 text-neutral-300" /> : null}
                        <span className="inline-flex max-w-full items-center gap-1.5 rounded-lg bg-neutral-100 py-1 pr-2 pl-1 text-xs text-neutral-700">
                          <span className="flex size-4 shrink-0 items-center justify-center rounded-full bg-white text-[10px] font-semibold text-neutral-500">
                            {index + 1}
                          </span>
                          <span className="break-all">{email}</span>
                        </span>
                      </Fragment>
                    ))}
                  </div>
                </div>
              ) : null}
              {attemptGroups.length ? (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm font-medium text-neutral-700">
                    <ListChecks className="size-4 text-neutral-400" />
                    尝试轨迹
                    <span className="text-xs font-normal text-neutral-400">每次换号的结局、耗时与现场</span>
                  </div>
                  <div className="space-y-2">
                    {attemptGroups.map((group, groupIndex) => (
                      <div key={groupIndex} className="overflow-hidden rounded-xl border border-neutral-200 bg-white">
                        {group.parsed ? (
                          group.steps.map((step, stepIndex) => {
                            const outcome = attemptOutcomeStyle(step.outcome);
                            return (
                              <div
                                key={stepIndex}
                                className={`flex flex-wrap items-start gap-x-3 gap-y-1.5 px-3 py-2.5 text-xs ${stepIndex > 0 ? "border-t border-neutral-100" : ""}`}
                              >
                                <span className="inline-flex h-5 min-w-8 shrink-0 items-center justify-center rounded-md bg-neutral-100 px-1 font-mono text-[11px] font-semibold text-neutral-600">
                                  {step.index}
                                </span>
                                <span className="break-all font-medium text-neutral-700">{step.email || "未选出账号"}</span>
                                <Badge variant={outcome.variant} className="rounded-md">{outcome.label}</Badge>
                                {step.marks.map((mark, markIndex) => (
                                  <span
                                    key={`${mark}-${markIndex}`}
                                    className="rounded-md bg-neutral-50 px-1.5 py-0.5 font-mono text-[11px] text-neutral-500 ring-1 ring-neutral-200 ring-inset"
                                  >
                                    {mark}
                                  </span>
                                ))}
                                <span className="ml-auto shrink-0 font-mono text-[11px] tabular-nums text-neutral-400">
                                  {formatAttemptDuration(step.durationMs)}
                                </span>
                                {step.error ? <p className="w-full leading-5 text-neutral-500">{step.error}</p> : null}
                              </div>
                            );
                          })
                        ) : (
                          <pre className="overflow-x-auto px-3 py-2.5 leading-6 whitespace-pre-wrap break-all text-neutral-600">
                            {group.raw}
                          </pre>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
              {detailUpstreamError.length ? (
                <div className="space-y-2">
                  <div className="text-sm font-medium text-neutral-700">上游报错</div>
                  {detailUpstreamError.map((frame, index) => (
                    <div key={index} className="space-y-2 rounded-xl border border-rose-100 bg-rose-50/60 p-3">
                      <div className="flex flex-wrap items-center gap-2 text-xs text-neutral-500">
                        <Badge variant="danger" className="rounded-md">{frame.type || "异常"}</Badge>
                        {typeof frame.status_code === "number" ? <span>status={frame.status_code}</span> : null}
                        {frame.code ? <span className="font-mono">{frame.code}</span> : null}
                        {frame.context ? <span>{frame.context}</span> : null}
                        {typeof frame.retry_after === "number" ? <span>retry_after={frame.retry_after}</span> : null}
                      </div>
                      <pre className="overflow-x-auto text-xs leading-6 whitespace-pre-wrap break-all text-neutral-700">
                        {frame.body || frame.message || "-"}
                      </pre>
                    </div>
                  ))}
                </div>
              ) : null}
              {detailUrls.length ? (
                <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-3">
                  {detailUrls.map((url, index) => (
                    <button
                      key={url}
                      type="button"
                      className="aspect-square overflow-hidden rounded-xl border border-neutral-200 bg-neutral-100"
                      onClick={() => {
                        setLightboxIndex(index);
                        setLightboxOpen(true);
                      }}
                    >
                      <img src={url} alt="" className="h-full w-full object-cover" />
                    </button>
                  ))}
                </div>
              ) : null}
              <pre className="max-h-[72vh] overflow-auto rounded-xl border border-neutral-200 bg-neutral-50 p-4 text-xs leading-6 text-neutral-700">
                {JSON.stringify(detailLog?.detail || {}, null, 2)}
              </pre>
            </div>
          </div>
        </DialogContent>
      </Dialog>
      <ImageLightbox
        images={detailImages}
        currentIndex={lightboxIndex}
        open={lightboxOpen}
        onOpenChange={setLightboxOpen}
        onIndexChange={setLightboxIndex}
      />
      <Dialog open={deletingItems.length > 0} onOpenChange={(open) => (!open ? setDeletingItems([]) : null)}>
        <DialogContent showCloseButton={false} className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>{deletingItems.length === 1 ? "删除日志" : "删除所选日志"}</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              确认删除 {deletingItems.length} 条日志吗？删除后无法恢复。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" className="rounded-xl" onClick={() => setDeletingItems([])} disabled={isDeleting}>
              取消
            </Button>
            <Button className="rounded-xl bg-rose-600 text-white hover:bg-rose-700" onClick={() => void confirmDelete()} disabled={isDeleting || deletingItems.length === 0}>
              {/* 同「保存设置」：图标常驻，点击时只在原地换成转圈，按钮不会因此变宽。 */}
              {isDeleting ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
              确认删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={cleanupOpen} onOpenChange={setCleanupOpen}>
        <DialogContent showCloseButton={false} className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>清理日志</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              将删除 {retentionValid ? cleanupCutoffDay(parsedRetentionDays) : "—"} 之前的所有日志，保留最近 {retentionValid ? parsedRetentionDays : "—"} 天。
              统计页的请求数与峰值不受影响，删除后无法恢复。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" className="rounded-xl" onClick={() => setCleanupOpen(false)} disabled={isCleaning}>
              取消
            </Button>
            <Button className="rounded-xl bg-rose-600 text-white hover:bg-rose-700" onClick={() => void confirmCleanup()} disabled={isCleaning || !retentionValid}>
              {isCleaning ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
              确认清理
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

export default function LogsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth || !session || session.role !== "admin") {
    return <div className="flex min-h-[40vh] items-center justify-center"><LoaderCircle className="size-5 animate-spin text-neutral-400" /></div>;
  }
  return <LogsContent />;
}
