"use client";

import { useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight, Download, LoaderCircle, RefreshCw, Search, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { ContentLoading } from "@/components/content-loading";
import { DateRangeFilter } from "@/components/date-range-filter";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { deleteSystemLogs, exportSystemLogs, fetchSystemLogs, type SystemLog } from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";

/** 注册机失败日志的类型标识，与后端 services/log_service.py 的 LOG_TYPE_REGISTER 一致。 */
const REGISTER_LOG_TYPE = "register";

type UpstreamErrorFrame = {
  type?: string;
  message?: string;
  context?: string;
  status_code?: number;
  body?: string;
};

/** 非字符串标量直接展示，数组/对象交给下面的过程记录、堆栈和原始报文三块。 */
const STRUCTURED_KEYS = new Set(["steps", "traceback", "upstream_error", "urls", "error_chain"]);

function detailText(item: SystemLog | null | undefined, key: string) {
  const value = item?.detail?.[key];
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : [];
}

function formatDuration(item: SystemLog) {
  const value = item.detail?.duration_ms;
  return typeof value === "number" ? `${(value / 1000).toFixed(1)} s` : "-";
}

/** 上游报错链：第一条是抛给用户的异常，越往后越接近上游原始响应。 */
function upstreamFrames(item: SystemLog | null): UpstreamErrorFrame[] {
  const frames = item?.detail?.upstream_error;
  if (!Array.isArray(frames)) {
    return [];
  }
  return frames.filter((frame): frame is UpstreamErrorFrame => typeof frame === "object" && frame !== null);
}

function RegisterLogsContent() {
  const [items, setItems] = useState<SystemLog[]>([]);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [detailLog, setDetailLog] = useState<SystemLog | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [deletingItem, setDeletingItem] = useState<SystemLog | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [exportLimit, setExportLimit] = useState("10");
  const [isExporting, setIsExporting] = useState(false);
  const [page, setPage] = useState(1);
  const pageSize = 10;
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));
  const safePage = Math.min(page, pageCount);
  const currentRows = items.slice((safePage - 1) * pageSize, safePage * pageSize);

  const loadLogs = async () => {
    setIsLoading(true);
    try {
      const data = await fetchSystemLogs({ type: REGISTER_LOG_TYPE, start_date: startDate, end_date: endDate });
      setItems(data.items);
      setPage(1);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载注册日志失败");
    } finally {
      setIsLoading(false);
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
      // 跟着当前筛选走；注册日志本来就只记失败，不必再带状态筛选。
      const count = await exportSystemLogs({
        type: REGISTER_LOG_TYPE,
        start_date: startDate,
        end_date: endDate,
        limit: parsedExportLimit,
      });
      if (count === null) {
        toast.success("已导出注册日志文件");
      } else if (count > 0) {
        toast.success(`已导出 ${count} 条注册日志`);
      } else {
        toast.success("已导出注册日志文件（没有符合条件的记录）");
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "导出注册日志失败");
    } finally {
      setIsExporting(false);
    }
  };

  const confirmDelete = async () => {
    if (!deletingItem) return;
    setIsDeleting(true);
    try {
      const data = await deleteSystemLogs([deletingItem.id]);
      toast.success(`已删除 ${data.removed} 条注册日志`);
      if (detailLog?.id === deletingItem.id) {
        setDetailOpen(false);
        setDetailLog(null);
      }
      setDeletingItem(null);
      await loadLogs();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除注册日志失败");
    } finally {
      setIsDeleting(false);
    }
  };

  useEffect(() => {
    void loadLogs();
  }, [startDate, endDate]);

  const detailSteps = useMemo(() => stringList(detailLog?.detail?.steps), [detailLog]);
  const detailTraceback = useMemo(() => stringList(detailLog?.detail?.traceback), [detailLog]);
  const detailFrames = upstreamFrames(detailLog);
  const detailScalars = useMemo(
    () =>
      Object.entries(detailLog?.detail || {}).filter(
        ([key, value]) => !STRUCTURED_KEYS.has(key) && typeof value !== "object",
      ),
    [detailLog],
  );

  return (
    <section className="space-y-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-1">
          <div className="text-xs font-semibold tracking-[0.18em] text-neutral-500 uppercase">Register Logs</div>
          <h1 className="text-2xl font-semibold tracking-tight">注册日志</h1>
          <p className="text-sm text-neutral-500">
            只记录注册失败的记录（成功的注册不写日志），每条都带邮箱域名、停在的步骤和完整过程。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <DateRangeFilter
            startDate={startDate}
            endDate={endDate}
            onChange={(start, end) => {
              setStartDate(start);
              setEndDate(end);
            }}
          />
          <Button
            variant="outline"
            onClick={() => {
              setStartDate("");
              setEndDate("");
            }}
            className="h-10 rounded-xl border-neutral-200 bg-white px-4 text-neutral-700"
          >
            清除筛选条件
          </Button>
          <Button
            onClick={() => void loadLogs()}
            disabled={isLoading}
            className="h-10 rounded-xl bg-neutral-950 px-4 text-white hover:bg-neutral-800"
          >
            {isLoading ? <LoaderCircle className="size-4 animate-spin" /> : <Search className="size-4" />}
            查询
          </Button>
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
            title="导出最近 N 条注册失败日志的完整报文（txt），按时间由旧到新排列"
            className="h-10 rounded-xl border-neutral-200 bg-white px-4 text-neutral-700"
          >
            {isExporting ? <LoaderCircle className="size-4 animate-spin" /> : <Download className="size-4" />}
            导出日志
          </Button>
        </div>
      </div>

      <Card className="overflow-hidden rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="p-0">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-neutral-100 px-5 py-4 text-sm text-neutral-600">
            <span>共 {isLoading && items.length === 0 ? "—" : items.length} 条</span>
            <Button variant="ghost" className="h-8 rounded-lg px-3 text-neutral-500" onClick={() => void loadLogs()} disabled={isLoading}>
              <RefreshCw className={`size-4 ${isLoading ? "animate-spin" : ""}`} />
              刷新
            </Button>
          </div>
          <div className="overflow-x-auto">
            <Table className="min-w-[900px]">
              <TableHeader>
                <TableRow>
                  <TableHead>时间</TableHead>
                  <TableHead>任务</TableHead>
                  <TableHead>邮箱</TableHead>
                  <TableHead>停在的步骤</TableHead>
                  <TableHead>耗时</TableHead>
                  <TableHead className="w-40">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {isLoading && items.length === 0 ? (
                  <TableRow className="hover:bg-transparent">
                    <TableCell colSpan={6} className="p-0">
                      <ContentLoading label="正在加载注册日志" />
                    </TableCell>
                  </TableRow>
                ) : null}
                {currentRows.map((item) => (
                  <TableRow key={item.id} className="text-neutral-600">
                    <TableCell className="whitespace-nowrap">{item.time}</TableCell>
                    <TableCell>{detailText(item, "task_index") || "-"}</TableCell>
                    <TableCell className="max-w-[240px] truncate">
                      {detailText(item, "email") || "未知邮箱"}
                      {detailText(item, "mail_domain") ? (
                        <span className="ml-2 text-xs text-neutral-400">{detailText(item, "mail_domain")}</span>
                      ) : null}
                    </TableCell>
                    <TableCell className="max-w-[420px] truncate text-neutral-500" title={detailText(item, "stage")}>
                      {detailText(item, "stage") || item.summary || "-"}
                    </TableCell>
                    <TableCell className="whitespace-nowrap">{formatDuration(item)}</TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1">
                        <Button
                          variant="ghost"
                          className="h-8 rounded-lg px-3 text-neutral-600"
                          onClick={() => {
                            setDetailLog(item);
                            setDetailOpen(true);
                          }}
                        >
                          查看详情
                        </Button>
                        <Button
                          variant="ghost"
                          className="h-8 rounded-lg px-3 text-rose-600 hover:bg-rose-50 hover:text-rose-700"
                          onClick={() => setDeletingItem(item)}
                        >
                          删除
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          <div className="flex items-center justify-end gap-2 border-t border-neutral-100 px-4 py-3 text-sm text-neutral-500">
            <span>第 {safePage} / {pageCount} 页，共 {items.length} 条</span>
            <Button variant="outline" size="icon" className="size-9 rounded-lg border-neutral-200 bg-white" disabled={safePage <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>
              <ChevronLeft className="size-4" />
            </Button>
            <Button variant="outline" size="icon" className="size-9 rounded-lg border-neutral-200 bg-white" disabled={safePage >= pageCount} onClick={() => setPage((value) => Math.min(pageCount, value + 1))}>
              <ChevronRight className="size-4" />
            </Button>
          </div>
          {!isLoading && items.length === 0 ? (
            <div className="px-6 py-14 text-center text-sm text-neutral-500">
              没有失败的注册记录
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
        <DialogContent className="flex h-[min(88vh,880px)] w-[min(94vw,940px)] flex-col overflow-hidden rounded-2xl p-0">
          <DialogHeader className="shrink-0 border-b border-neutral-100 px-6 py-5">
            <DialogTitle>注册失败详情</DialogTitle>
            <DialogDescription className="text-sm text-neutral-500">
              {detailLog?.time || ""} {detailText(detailLog, "email") ? `· ${detailText(detailLog, "email")}` : ""}
            </DialogDescription>
          </DialogHeader>
          <div className="flex-1 overflow-y-auto px-6 py-5">
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="danger" className="rounded-md">失败</Badge>
                {detailText(detailLog, "stage") ? <span className="text-sm text-neutral-600">{detailText(detailLog, "stage")}</span> : null}
              </div>
              <div className="grid gap-3 rounded-xl border border-neutral-200 bg-white p-4 text-sm text-neutral-600 md:grid-cols-2">
                {detailScalars.map(([key, value]) => (
                  <div key={key} className="flex items-start justify-between gap-4">
                    <span className="text-neutral-400">{key}</span>
                    <span className="text-right font-medium break-all text-neutral-700">{String(value)}</span>
                  </div>
                ))}
              </div>
              {detailFrames.length ? (
                <div className="space-y-2">
                  <div className="text-sm font-medium text-neutral-700">错误链</div>
                  {detailFrames.map((frame, index) => (
                    <div key={index} className="space-y-2 rounded-xl border border-rose-100 bg-rose-50/60 p-3">
                      <div className="flex flex-wrap items-center gap-2 text-xs text-neutral-500">
                        <Badge variant="danger" className="rounded-md">{frame.type || "异常"}</Badge>
                        {typeof frame.status_code === "number" ? <span>status={frame.status_code}</span> : null}
                        {frame.context ? <span>{frame.context}</span> : null}
                      </div>
                      <pre className="overflow-x-auto text-xs leading-6 whitespace-pre-wrap break-all text-neutral-700">
                        {frame.body || frame.message || "-"}
                      </pre>
                    </div>
                  ))}
                </div>
              ) : null}
              {detailSteps.length ? (
                <div className="space-y-2">
                  <div className="text-sm font-medium text-neutral-700">过程记录（{detailSteps.length} 步）</div>
                  <pre className="max-h-[40vh] overflow-auto rounded-xl border border-neutral-200 bg-neutral-50 p-4 text-xs leading-6 text-neutral-700">
                    {detailSteps.join("\n")}
                  </pre>
                </div>
              ) : null}
              {detailTraceback.length ? (
                <div className="space-y-2">
                  <div className="text-sm font-medium text-neutral-700">堆栈</div>
                  <pre className="max-h-[32vh] overflow-auto rounded-xl border border-neutral-200 bg-neutral-50 p-4 text-xs leading-6 text-neutral-700">
                    {detailTraceback.join("\n")}
                  </pre>
                </div>
              ) : null}
              <pre className="max-h-[60vh] overflow-auto rounded-xl border border-neutral-200 bg-neutral-50 p-4 text-xs leading-6 text-neutral-700">
                {JSON.stringify(detailLog?.detail || {}, null, 2)}
              </pre>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={deletingItem !== null} onOpenChange={(open) => (!open ? setDeletingItem(null) : null)}>
        <DialogContent showCloseButton={false} className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>删除注册日志</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              确认删除这条注册失败日志吗？删除后无法恢复。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" className="rounded-xl" onClick={() => setDeletingItem(null)} disabled={isDeleting}>
              取消
            </Button>
            <Button className="rounded-xl bg-rose-600 text-white hover:bg-rose-700" onClick={() => void confirmDelete()} disabled={isDeleting}>
              {isDeleting ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
              确认删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

export default function RegisterLogsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-neutral-400" />
      </div>
    );
  }
  return <RegisterLogsContent />;
}
