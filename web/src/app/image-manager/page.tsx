"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CalendarClock, CalendarDays, ChevronDown, ChevronLeft, ChevronRight, Copy, Download, ImageIcon, LoaderCircle, Maximize2, Plus, RefreshCw, Save, Search, Tag, Trash2, X } from "lucide-react";
import { toast } from "sonner";

import { ContentLoading } from "@/components/content-loading";
import { DateRangeFilter } from "@/components/date-range-filter";
import { FilterCard, FilterField } from "@/components/filter-card";
import { ImageLightbox } from "@/components/image-lightbox";
import { PageHeader } from "@/components/page-header";
import { PageSizeSelect } from "@/components/page-size-select";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { deleteImageTag, deleteManagedImages, downloadImages, downloadSingleImage, fetchImageStorage, fetchImageTags, fetchManagedImages, fetchSettingsConfig, setImageTags, updateSettingsConfig, type ImageStorageStats, type ManagedImage } from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";

const LONG_PRESS_MS = 800;
const IMAGE_MANAGER_CHECKBOX_CLASS = "border-neutral-300 bg-white/80 dark:border-white/35 dark:bg-white/5 data-[state=checked]:border-brand dark:data-[state=checked]:border-brand";
/** 定时清理的按天周期选项，与后端归一化白名单保持一致。 */
const CLEANUP_DAY_OPTIONS = [1, 3, 5, 7] as const;
type CleanupUnit = "days" | "minutes";

function formatSize(size: number) {
  return size > 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(2)} MB` : `${Math.ceil(size / 1024)} KB`;
}

function imageKey(item: ManagedImage) {
  return item.rel || item.url;
}

function useLongPress(onLongPress: () => void, ms = LONG_PRESS_MS) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activeRef = useRef(false);

  const start = useCallback((e: React.MouseEvent | React.TouchEvent) => {
    activeRef.current = true;
    timerRef.current = setTimeout(() => {
      if (activeRef.current) {
        onLongPress();
      }
    }, ms);
  }, [onLongPress, ms]);

  const stop = useCallback(() => {
    activeRef.current = false;
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  return {
    onMouseDown: start,
    onMouseUp: stop,
    onMouseLeave: stop,
    onTouchStart: start,
    onTouchEnd: stop,
  };
}

function ImageManagerContent() {
  const [items, setItems] = useState<ManagedImage[]>([]);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [lightboxIndex, setLightboxIndex] = useState(0);
  const [lightboxOpen, setLightboxOpen] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(8);
  // 图片库默认折叠：进页面不发列表/标签请求，点开才第一次加载。
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [deleteStartDate, setDeleteStartDate] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<ManagedImage | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [allTags, setAllTags] = useState<string[]>([]);
  const [storage, setStorage] = useState<ImageStorageStats | null>(null);
  const [storageLoading, setStorageLoading] = useState(false);

  const loadStorage = useCallback(async () => {
    try {
      setStorageLoading(true);
      const data = await fetchImageStorage();
      setStorage(data);
    } catch { /* ignore */ }
    finally { setStorageLoading(false); }
  }, []);

  useEffect(() => { void loadStorage(); }, [loadStorage]);

  // 定时清理配置：只加载/提交与图片清理相关的几个键，接口按合并处理，不影响其他设置。
  const [cleanupEnabled, setCleanupEnabled] = useState(true);
  const [cleanupUnit, setCleanupUnit] = useState<CleanupUnit>("days");
  const [cleanupDays, setCleanupDays] = useState(1);
  const [cleanupMinutes, setCleanupMinutes] = useState("60");
  const [cleanupTime, setCleanupTime] = useState("03:00");
  const [cleanupKeepToday, setCleanupKeepToday] = useState(false);
  const [cleanupLoading, setCleanupLoading] = useState(true);
  const [isSavingCleanup, setIsSavingCleanup] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchSettingsConfig()
      .then(({ config }) => {
        if (cancelled) return;
        const days = Number(config.image_cleanup_interval_days);
        setCleanupEnabled(config.image_cleanup_enabled !== false);
        setCleanupUnit(config.image_cleanup_interval_unit === "minutes" ? "minutes" : "days");
        setCleanupDays(CLEANUP_DAY_OPTIONS.includes(days as (typeof CLEANUP_DAY_OPTIONS)[number]) ? days : 1);
        setCleanupMinutes(String(config.image_cleanup_interval_minutes ?? 60));
        setCleanupTime(/^([01]\d|2[0-3]):[0-5]\d$/.test(String(config.image_cleanup_time || "")) ? String(config.image_cleanup_time) : "03:00");
        setCleanupKeepToday(Boolean(config.image_cleanup_keep_today));
      })
      .catch(() => {
        // 读取失败时保留默认值，不影响图片库本身。
      })
      .finally(() => {
        if (!cancelled) setCleanupLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const saveCleanupSettings = async () => {
    // 与设置页保存时的归一化保持一致，非法输入回落到安全默认值。
    const days = CLEANUP_DAY_OPTIONS.includes(cleanupDays as (typeof CLEANUP_DAY_OPTIONS)[number]) ? cleanupDays : 1;
    const minutes = Math.min(10080, Math.max(1, Math.floor(Number(cleanupMinutes) || 60)));
    const time = /^([01]\d|2[0-3]):[0-5]\d$/.test(cleanupTime) ? cleanupTime : "03:00";
    setIsSavingCleanup(true);
    try {
      await updateSettingsConfig({
        image_cleanup_enabled: cleanupEnabled,
        image_cleanup_interval_unit: cleanupUnit,
        image_cleanup_interval_days: days,
        image_cleanup_interval_minutes: minutes,
        image_cleanup_time: time,
        image_cleanup_keep_today: cleanupKeepToday,
      });
      setCleanupDays(days);
      setCleanupMinutes(String(minutes));
      setCleanupTime(time);
      toast.success("定时清理设置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存定时清理设置失败");
    } finally {
      setIsSavingCleanup(false);
    }
  };

  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [tagEditTarget, setTagEditTarget] = useState<ManagedImage | null>(null);
  const [tagInput, setTagInput] = useState("");
  const [dialogVisible, setDialogVisible] = useState(false);
  const deleteTargetRef = useRef<ManagedImage | null>(null);
  const [selectedPaths, setSelectedPaths] = useState<string[]>([]);
  const [deleteMode, setDeleteMode] = useState<"selected" | "filtered" | "byDate" | null>(null);
  const [isDownloading, setIsDownloading] = useState(false);

  const filteredItems = selectedTags.length > 0
    ? items.filter((item) => selectedTags.every((t) => (item.tags ?? []).includes(t)))
    : items;

  const lightboxImages = filteredItems.map((item) => ({
    id: item.name,
    src: item.url,
    sizeLabel: formatSize(item.size),
    dimensions: item.width && item.height ? `${item.width} x ${item.height}` : undefined,
  }));
  const pageCount = Math.max(1, Math.ceil(filteredItems.length / pageSize));
  const safePage = Math.min(page, pageCount);
  const currentRows = filteredItems.slice((safePage - 1) * pageSize, safePage * pageSize);
  const selectedSet = useMemo(() => new Set(selectedPaths), [selectedPaths]);
  const selectedCount = deleteMode === "filtered" ? items.length : deleteMode === "byDate" ? 0 : selectedPaths.length;
  const currentPageSelected = currentRows.length > 0 && currentRows.every((item) => selectedSet.has(imageKey(item)));
  const allSelected = filteredItems.length > 0 && filteredItems.every((item) => selectedSet.has(imageKey(item)));

  const loadImages = async () => {
    setIsLoading(true);
    try {
      const [data, tagsData] = await Promise.all([
        fetchManagedImages({ start_date: startDate, end_date: endDate }),
        fetchImageTags(),
      ]);
      setItems(data.items);
      setAllTags(tagsData.tags);
      setSelectedPaths((current) => current.filter((path) => data.items.some((item) => imageKey(item) === path)));
      setPage(1);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载图片失败");
    } finally {
      setIsLoading(false);
    }
  };

  const closeDialog = useCallback(() => {
    setDialogVisible(false);
    setTimeout(() => setDeleteTarget(null), 200);
  }, []);

  const openDeleteDialog = useCallback((item: ManagedImage) => {
    deleteTargetRef.current = item;
    setDeleteTarget(item);
    setDialogVisible(true);
  }, []);

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setIsDeleting(true);
    try {
      await deleteManagedImages({ paths: [deleteTarget.rel] });
      setItems((prev) => prev.filter((item) => item.rel !== deleteTarget.rel));
      setSelectedPaths((prev) => prev.filter((p) => p !== imageKey(deleteTarget)));
      toast.success("图片已删除");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除失败");
    } finally {
      setIsDeleting(false);
      closeDialog();
    }
  };

  const handleSetTags = async (item: ManagedImage, tags: string[]) => {
    try {
      const result = await setImageTags(item.rel, tags);
      setItems((prev) => prev.map((i) => i.rel === item.rel ? { ...i, tags: result.tags } : i));
      const tagsData = await fetchImageTags();
      setAllTags(tagsData.tags);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "设置标签失败");
    }
  };

  const handleAddTag = (item: ManagedImage) => {
    const tag = tagInput.trim();
    if (!tag) return;
    const current = item.tags ?? [];
    if (current.includes(tag)) {
      toast.error("标签已存在");
      return;
    }
    void handleSetTags(item, [...current, tag]);
    setTagInput("");
  };

  const handleRemoveTag = (item: ManagedImage, tag: string) => {
    void handleSetTags(item, (item.tags ?? []).filter((t) => t !== tag));
  };

  const toggleFilterTag = (tag: string) => {
    setSelectedTags((prev) => prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag]);
    setPage(1);
  };

  const [pressingTag, setPressingTag] = useState<string | null>(null);
  const pressTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [tagDeleteTarget, setTagDeleteTarget] = useState<string | null>(null);

  const handleDeleteTag = async (tag: string) => {
    try {
      const result = await deleteImageTag(tag);
      setAllTags((prev) => prev.filter((t) => t !== tag));
      setSelectedTags((prev) => prev.filter((t) => t !== tag));
      setItems((prev) => prev.map((item) => ({
        ...item,
        tags: (item.tags ?? []).filter((t) => t !== tag),
      })));
      toast.success(`标签"${tag}"已删除，影响 ${result.removed_from} 张图片`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除标签失败");
    }
  };

  const startTagPress = useCallback((tag: string) => {
    setPressingTag(tag);
    pressTimerRef.current = setTimeout(() => {
      setPressingTag(null);
      setTagDeleteTarget(tag);
    }, LONG_PRESS_MS);
  }, []);

  const stopTagPress = useCallback(() => {
    setPressingTag(null);
    if (pressTimerRef.current) {
      clearTimeout(pressTimerRef.current);
      pressTimerRef.current = null;
    }
  }, []);

  const clearFilters = () => {
    setStartDate("");
    setEndDate("");
    setSelectedTags([]);
  };

  const togglePaths = (paths: string[], checked: boolean) => {
    setSelectedPaths((current) => checked ? Array.from(new Set([...current, ...paths])) : current.filter((path) => !paths.includes(path)));
  };

  const confirmDelete = async () => {
    if (!deleteMode || selectedCount === 0) return;
    setIsDeleting(true);
    try {
      const data = await deleteManagedImages(deleteMode === "filtered" ? { start_date: startDate, end_date: endDate, all_matching: true } : { paths: selectedPaths });
      toast.success(`已删除 ${data.removed} 张图片`);
      setDeleteMode(null);
      setSelectedPaths([]);
      await loadImages();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除图片失败");
    } finally {
      setIsDeleting(false);
    }
  };

  const handleBatchDownload = async () => {
    const paths = deleteMode === "filtered" ? items.map((item) => item.rel) : selectedPaths;
    if (paths.length === 0) return;
    setIsDownloading(true);
    try {
      await downloadImages(paths);
      toast.success(`已下载 ${paths.length} 张图片`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "下载失败");
    } finally {
      setIsDownloading(false);
    }
  };

  const handleSingleDownload = async (item: ManagedImage) => {
    await downloadSingleImage(item.rel);
  };

  // 展开图片库或改动日期时才加载；折叠状态下一张图、一个请求都不发。
  useEffect(() => {
    if (!libraryOpen) {
      return;
    }
    void loadImages();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [libraryOpen, startDate, endDate]);

  return (
    <section className="space-y-5">
      <PageHeader
        title="图片管理"
        description="按日期与标签浏览、下载或清理已生成的图片。"
      />

      <FilterCard>
        <FilterField label="时间范围">
          <DateRangeFilter startDate={startDate} endDate={endDate} onChange={(start, end) => { setStartDate(start); setEndDate(end); }} />
        </FilterField>
        <div className="flex items-center gap-2 sm:ml-auto">
          <Button variant="ghost" onClick={clearFilters} className="h-10 px-3 text-muted-foreground">
            清除筛选
          </Button>
          <Button variant="outline" onClick={() => setDeleteMode("filtered")} disabled={isDeleting || items.length === 0 || (!startDate && !endDate)} className="h-10 border-rose-200 px-4 text-rose-600 hover:bg-rose-50">
            <Trash2 className="size-4" />
            删除匹配日期
          </Button>
          <Button
            onClick={() => {
              // 折叠状态下点查询 = 展开；加载由展开触发的 effect 负责，避免重复请求。
              if (libraryOpen) {
                void loadImages();
              } else {
                setLibraryOpen(true);
              }
            }}
            disabled={isLoading}
            className="h-10 px-5"
          >
            {isLoading ? <LoaderCircle className="size-4 animate-spin" /> : <Search className="size-4" />}
            查询
          </Button>
        </div>
      </FilterCard>

      {allTags.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium text-neutral-500">
            <Tag className="mr-1 inline size-3.5" />
            标签筛选：
          </span>
          {allTags.map((tag) => {
            const isPressing = pressingTag === tag;
            return (
              <span
                key={tag}
                className="relative inline-flex items-center"
                onMouseDown={() => startTagPress(tag)}
                onMouseUp={stopTagPress}
                onMouseLeave={stopTagPress}
                onTouchStart={() => startTagPress(tag)}
                onTouchEnd={stopTagPress}
              >
                <button
                  type="button"
                  onClick={() => toggleFilterTag(tag)}
                >
                  <Badge
                    variant={selectedTags.includes(tag) ? "default" : "outline"}
                    className={`cursor-pointer rounded-md transition-all hover:opacity-80 ${isPressing ? "ring-2 ring-red-400 ring-offset-1" : ""}`}
                  >
                    {tag}
                  </Badge>
                </button>
                {isPressing ? (
                  <span className="pointer-events-none absolute inset-0 overflow-hidden rounded-md">
                    <span className="absolute inset-0 animate-[grow_800ms_linear_forwards] rounded-md bg-red-400/20" />
                  </span>
                ) : null}
              </span>
            );
          })}
          {selectedTags.length > 0 ? (
            <button type="button" onClick={() => setSelectedTags([])}>
              <Badge variant="secondary" className="cursor-pointer rounded-md">
                <X className="mr-0.5 size-3" />
                清除
              </Badge>
            </button>
          ) : null}
        </div>
      ) : null}

      {/* 定时清理：总开关关闭后只是不再自动清理，手动按日期删除不受影响。 */}
      <Card>
        <CardContent className="space-y-4 p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="flex items-center gap-2 text-[15px] font-semibold tracking-tight">
                <CalendarClock className="size-4 text-neutral-500" />
                定时清理
              </div>
              <div className="mt-0.5 text-xs text-muted-foreground">
                按北京时间（UTC+8）执行；只删图片和缩略图，不影响统计。关闭总开关后不再自动清理。
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex h-10 items-center gap-2 rounded-xl border border-neutral-200 bg-white px-3 text-sm text-neutral-700">
                <Checkbox
                  checked={cleanupEnabled}
                  onCheckedChange={(checked) => setCleanupEnabled(Boolean(checked))}
                />
                启用定时清理
              </label>
              <Button
                className="h-10 px-4"
                onClick={() => void saveCleanupSettings()}
                disabled={isSavingCleanup || cleanupLoading}
              >
                {isSavingCleanup ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
                保存设置
              </Button>
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="space-y-2">
              <label className="text-sm text-neutral-700">清理周期</label>
              <Select
                value={cleanupUnit === "minutes" ? "minutes" : String(cleanupDays)}
                onValueChange={(value) => {
                  if (value === "minutes") {
                    setCleanupUnit("minutes");
                    return;
                  }
                  setCleanupUnit("days");
                  setCleanupDays(Number(value) || 1);
                }}
              >
                <SelectTrigger className="h-10 w-full rounded-xl border-neutral-200 bg-white">
                  <SelectValue placeholder="选择清理周期" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="1">每天</SelectItem>
                  <SelectItem value="3">每三天</SelectItem>
                  <SelectItem value="5">每五天</SelectItem>
                  <SelectItem value="7">每七天</SelectItem>
                  <SelectItem value="minutes">按分钟</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs leading-5 text-neutral-500">按「每 N 天」时在固定时刻清理。</p>
            </div>
            <div className="space-y-2">
              <label className="text-sm text-neutral-700">清理间隔（分钟）</label>
              <Input
                value={cleanupMinutes}
                onChange={(event) => setCleanupMinutes(event.target.value)}
                disabled={cleanupUnit !== "minutes"}
                inputMode="numeric"
                placeholder="60"
                className="h-10 rounded-xl border-neutral-200 bg-white disabled:cursor-not-allowed disabled:bg-neutral-50 disabled:text-neutral-400"
              />
              <p className="text-xs leading-5 text-neutral-500">仅在清理周期选「按分钟」时可填，范围 1-10080。</p>
            </div>
            <div className="space-y-2">
              <label className="text-sm text-neutral-700">清理时间</label>
              <Input
                type="time"
                step={60}
                value={cleanupTime}
                onChange={(event) => setCleanupTime(event.target.value)}
                disabled={cleanupUnit === "minutes"}
                className="h-10 rounded-xl border-neutral-200 bg-white disabled:cursor-not-allowed disabled:bg-neutral-50 disabled:text-neutral-400"
              />
              <p className="text-xs leading-5 text-neutral-500">
                {cleanupUnit === "minutes" ? "按分钟间隔清理时此项不生效。" : "默认删除执行日期及之前的全部图片。"}
              </p>
            </div>
            <div className="space-y-2">
              <label className="text-sm text-neutral-700">处理方式</label>
              <label className="flex h-10 items-center gap-2 rounded-xl border border-neutral-200 bg-white px-3 text-sm text-neutral-700">
                <Checkbox
                  checked={cleanupKeepToday}
                  onCheckedChange={(checked) => setCleanupKeepToday(Boolean(checked))}
                  disabled={cleanupUnit === "minutes"}
                />
                保留当天图片
              </label>
              <p className="text-xs leading-5 text-neutral-500">
                {cleanupUnit === "minutes"
                  ? "按分钟间隔清理时此项不生效。"
                  : "勾选后只删除昨天及更早的图片：例如 05:00 清理会保留今天 00:00-05:00 生成的图片。"}
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* 存储概览：四项指标共用一张卡，磁盘占用画成可用条。 */}
      <Card className="overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-4">
          <div>
            <div className="text-[15px] font-semibold tracking-tight">存储空间</div>
            <div className="mt-0.5 text-xs text-muted-foreground">
              图片与缩略图占用；剩余空间低于 500 MB 时建议清理
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button size="sm" variant="outline" className="h-9 px-3" disabled={storageLoading} onClick={() => { void loadStorage(); }}>
              <RefreshCw className={`size-3.5 ${storageLoading ? "animate-spin" : ""}`} />刷新
            </Button>
            <Button size="sm" variant="outline" className="h-9 border-rose-200 px-3 text-rose-600 hover:bg-rose-50 hover:text-rose-700"
              onClick={() => setDeleteMode("byDate")}>
              <Trash2 className="size-3.5" />
              按日期删除
            </Button>
          </div>
        </div>
        {storage ? (
          <div className="grid gap-px bg-border sm:grid-cols-2 lg:grid-cols-4">
            <div className="bg-card px-5 py-4">
              <div className="text-xs font-medium text-muted-foreground">磁盘总量</div>
              <div className="mt-2 text-2xl font-semibold tracking-tight tabular-nums">
                {storage.disk_total_mb >= 1024 ? `${(storage.disk_total_mb / 1024).toFixed(1)} GB` : `${storage.disk_total_mb} MB`}
              </div>
              {(() => {
                const usedPercent = storage.disk_total_mb > 0
                  ? Math.min(100, Math.max(0, Math.round(((storage.disk_total_mb - storage.disk_free_mb) / storage.disk_total_mb) * 100)))
                  : 0;
                return (
                  <div className="mt-3">
                    <div className="h-1.5 overflow-hidden rounded-full bg-brand/12">
                      <div className="h-full rounded-full bg-brand transition-[width] duration-500" style={{ width: `${usedPercent}%` }} />
                    </div>
                    <div className="mt-1.5 flex justify-between text-[11px] text-muted-foreground">
                      <span>已用 {usedPercent}%</span>
                      <span className="tabular-nums">{storage.disk_free_mb >= 1024 ? `${(storage.disk_free_mb / 1024).toFixed(1)} GB` : `${storage.disk_free_mb} MB`} 可用</span>
                    </div>
                  </div>
                );
              })()}
            </div>
            <div className="bg-card px-5 py-4">
              <div className="text-xs font-medium text-muted-foreground">剩余空间</div>
              <div className={`mt-2 text-2xl font-semibold tracking-tight tabular-nums ${storage.disk_free_mb < 200 ? "text-rose-500" : storage.disk_free_mb < 500 ? "text-amber-500" : "text-emerald-600"}`}>
                {storage.disk_free_mb >= 1024 ? `${(storage.disk_free_mb / 1024).toFixed(1)} GB` : `${storage.disk_free_mb} MB`}
              </div>
              <div className="mt-2 text-[11px] text-muted-foreground">{storage.disk_free_mb < 500 ? "空间偏紧，建议清理旧图片" : "空间充足"}</div>
            </div>
            <div className="bg-card px-5 py-4">
              <div className="text-xs font-medium text-muted-foreground">图片数量</div>
              <div className="mt-2 text-2xl font-semibold tracking-tight tabular-nums">{storage.image_count}</div>
              <div className="mt-2 text-[11px] text-muted-foreground">张原图（含缩略图）</div>
            </div>
            <div className="bg-card px-5 py-4">
              <div className="text-xs font-medium text-muted-foreground">图片占用</div>
              <div className="mt-2 text-2xl font-semibold tracking-tight tabular-nums">
                {storage.image_size_mb >= 1024 ? `${(storage.image_size_mb / 1024).toFixed(1)} GB` : `${storage.image_size_mb} MB`}
              </div>
              <div className="mt-2 text-[11px] text-muted-foreground">原图与缩略图合计</div>
            </div>
          </div>
        ) : (
          <div className="px-5 py-6 text-center text-sm text-muted-foreground">
            {storageLoading ? "加载存储信息..." : "存储信息加载失败"}
          </div>
        )}
      </Card>


      {/* Delete by date dialog */}
      <Dialog open={deleteMode === "byDate"} onOpenChange={() => setDeleteMode(null)}>
        <DialogContent className="sm:max-w-md rounded-2xl">
          <DialogHeader><DialogTitle>按日期删除图片</DialogTitle></DialogHeader>
          <div className="space-y-4">
            <div className="flex items-center gap-2">
              <label className="text-sm text-neutral-600 shrink-0">删除</label>
              <Input className="h-9 text-sm" type="date" value={deleteStartDate} onChange={(e) => setDeleteStartDate(e.target.value)} />
              <span className="text-sm text-neutral-400">之前的图片</span>
            </div>
            <p className="text-xs text-neutral-500">此操作不可撤销，将永久删除所有匹配日期的图片及其缩略图。</p>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDeleteMode(null)}>取消</Button>
            <Button variant="destructive" disabled={!deleteStartDate || isDeleting}
              onClick={async () => {
                if (!deleteStartDate) return;
                try {
                  setIsDeleting(true);
                  const r = await deleteManagedImages({ end_date: deleteStartDate, all_matching: true });
                  toast.success(`已删除 ${r.removed} 张图片`);
                  setDeleteMode(null);
                  void loadStorage();
                  void loadImages();
                } catch { toast.error("删除失败"); }
                finally { setIsDeleting(false); }
              }}>
              {isDeleting ? <LoaderCircle className="size-4 animate-spin" /> : null}
              确认删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Card>
        <CardContent className="p-0">
          {libraryOpen ? (
          <>
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-3.5">
            <div className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
              <button
                type="button"
                aria-expanded
                onClick={() => setLibraryOpen(false)}
                title="收起图片库"
                className="inline-flex items-center gap-2 rounded-lg pr-1 text-muted-foreground transition-colors hover:text-foreground"
              >
                <ImageIcon className="size-4" />
                <span className="text-[15px] font-semibold tracking-tight text-foreground">图片库</span>
                <ChevronDown className="size-4 rotate-180 transition-transform" />
              </button>
              <span className="rounded-full bg-brand-soft px-2 py-0.5 text-[11px] font-semibold text-brand tabular-nums">
                {filteredItems.length}
              </span>
              {selectedTags.length > 0 ? <span className="text-neutral-400">（筛选自 {items.length} 张）</span> : null}
              <label className="flex items-center gap-2">
                <Checkbox className={IMAGE_MANAGER_CHECKBOX_CLASS} checked={currentPageSelected} onCheckedChange={(checked) => togglePaths(currentRows.map(imageKey), Boolean(checked))} />
                本页全选
              </label>
              <label className="flex items-center gap-2">
                <Checkbox className={IMAGE_MANAGER_CHECKBOX_CLASS} checked={allSelected} onCheckedChange={(checked) => togglePaths(filteredItems.map(imageKey), Boolean(checked))} />
                全选结果
              </label>
              {selectedPaths.length > 0 ? <span>已选 {selectedPaths.length} 张</span> : null}
            </div>
            <div className="flex items-center gap-2">
              <Button variant="ghost" className="h-8 rounded-lg px-3 text-neutral-500" onClick={() => void loadImages()} disabled={isLoading}>
                <RefreshCw className={`size-4 ${isLoading ? "animate-spin" : ""}`} />
                刷新
              </Button>
              <button type="button" className="text-sm text-neutral-500 hover:text-neutral-900 disabled:text-neutral-300" onClick={() => setSelectedPaths([])} disabled={selectedPaths.length === 0 || isDeleting}>
                取消选择
              </button>
              <Button variant="outline" className="h-8 rounded-lg border-neutral-200 bg-white px-3 text-neutral-600 hover:bg-neutral-50" onClick={() => void handleBatchDownload()} disabled={selectedPaths.length === 0 || isDownloading || isDeleting}>
                {isDownloading ? <LoaderCircle className="size-4 animate-spin" /> : <Download className="size-4" />}
                下载所选
              </Button>
              <Button variant="outline" className="h-8 rounded-lg border-rose-200 bg-white px-3 text-rose-600 hover:bg-rose-50" onClick={() => setDeleteMode("selected")} disabled={selectedPaths.length === 0 || isDeleting}>
                <Trash2 className="size-4" />
                删除所选
              </Button>
            </div>
          </div>
          {isLoading && filteredItems.length === 0 ? <ContentLoading label="正在加载图片" /> : null}
          <div className="grid gap-0 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {currentRows.map((item) => {
              const imageIndex = filteredItems.findIndex((row) => row.url === item.url);
              return (
              <div key={item.rel} className="group border-r border-b border-neutral-100 p-4 transition hover:bg-neutral-50 dark:hover:bg-white/5">
                <div className="relative">
                  <button
                    type="button"
                    className="relative block aspect-square w-full cursor-zoom-in overflow-hidden rounded-lg bg-neutral-100 text-left"
                    onClick={() => {
                      setLightboxIndex(imageIndex);
                      setLightboxOpen(true);
                    }}
                  >
                    <img
                      src={item.thumbnail_url || item.url}
                      alt={item.name}
                      className="h-full w-full object-cover transition group-hover:scale-[1.02]"
                      onError={(event) => {
                        if (event.currentTarget.src !== item.url) {
                          event.currentTarget.src = item.url;
                        }
                      }}
                    />
                    <span className="absolute right-2 bottom-2 rounded-full bg-black/50 p-2 text-white opacity-100 transition sm:opacity-0 sm:group-hover:opacity-100">
                      <Maximize2 className="size-4" />
                    </span>
                  </button>
                  <button
                    type="button"
                    className="absolute top-2 right-2 z-10 inline-flex size-7 items-center justify-center rounded-full bg-black/50 text-white opacity-100 transition hover:bg-red-600 sm:opacity-0 sm:group-hover:opacity-100"
                    title="删除图片"
                    onClick={(e) => {
                      e.stopPropagation();
                      openDeleteDialog(item);
                    }}
                  >
                    <Trash2 className="size-3.5" />
                  </button>
                </div>
                <div className="mt-3 space-y-2 text-xs text-neutral-500">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-1 font-medium text-neutral-700">
                      <CalendarDays className="size-3.5" />
                      {item.created_at}
                    </div>
                    <div className="flex items-center gap-1">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-8 rounded-lg text-neutral-400 hover:bg-neutral-100 hover:text-neutral-700"
                        onClick={() => void handleSingleDownload(item)}
                        title="下载图片"
                      >
                        <Download className="size-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-8 rounded-lg text-neutral-400 hover:bg-neutral-100 hover:text-neutral-700"
                        onClick={() => {
                          void navigator.clipboard.writeText(item.url);
                          toast.success("图片地址已复制");
                        }}
                      >
                        <Copy className="size-4" />
                      </Button>
                      <Checkbox className={IMAGE_MANAGER_CHECKBOX_CLASS} checked={selectedSet.has(imageKey(item))} onCheckedChange={(checked) => togglePaths([imageKey(item)], Boolean(checked))} />
                    </div>
                  </div>
                  <div className="flex items-center justify-between gap-2">
                    <span>{formatSize(item.size)}</span>
                    <span>{item.width && item.height ? `${item.width} x ${item.height}` : "-"}</span>
                  </div>
                  <div className="flex flex-wrap items-center gap-1">
                    {(item.tags ?? []).map((tag) => (
                      <Badge key={tag} variant="secondary" className="gap-0.5 rounded-md py-0 pr-0.5 text-[10px]">
                        {tag}
                        <button
                          type="button"
                          className="inline-flex size-3.5 items-center justify-center rounded-full hover:bg-neutral-300"
                          onClick={() => handleRemoveTag(item, tag)}
                        >
                          <X className="size-2.5" />
                        </button>
                      </Badge>
                    ))}
                    <Popover open={tagEditTarget?.rel === item.rel} onOpenChange={(open) => { setTagEditTarget(open ? item : null); setTagInput(""); }}>
                      <PopoverTrigger asChild>
                        <button
                          type="button"
                          className="inline-flex size-5 items-center justify-center rounded-full border border-dashed border-neutral-300 text-neutral-400 hover:border-neutral-500 hover:text-neutral-600"
                          title="添加标签"
                        >
                          <Plus className="size-3" />
                        </button>
                      </PopoverTrigger>
                      <PopoverContent align="start" className="w-56 p-2">
                        <div className="space-y-2">
                          <div className="text-xs font-medium text-neutral-500">添加标签</div>
                          <div className="flex gap-1">
                            <Input
                              value={tagInput}
                              onChange={(e) => setTagInput(e.target.value)}
                              placeholder="输入标签名"
                              className="h-8 text-xs"
                              onKeyDown={(e) => {
                                if (e.key === "Enter") {
                                  e.preventDefault();
                                  handleAddTag(item);
                                }
                              }}
                            />
                            <Button
                              size="icon"
                              variant="outline"
                              className="size-8 shrink-0"
                              onClick={() => handleAddTag(item)}
                            >
                              <Plus className="size-3.5" />
                            </Button>
                          </div>
                          {allTags.filter((t) => !(item.tags ?? []).includes(t)).length > 0 ? (
                            <div className="flex flex-wrap gap-1 border-t border-neutral-100 pt-2">
                              {allTags.filter((t) => !(item.tags ?? []).includes(t)).map((tag) => (
                                <button
                                  key={tag}
                                  type="button"
                                  onClick={() => {
                                    void handleSetTags(item, [...(item.tags ?? []), tag]);
                                    setTagEditTarget(null);
                                  }}
                                >
                                  <Badge variant="outline" className="cursor-pointer rounded-md text-[10px] hover:bg-neutral-100">
                                    {tag}
                                  </Badge>
                                </button>
                              ))}
                            </div>
                          ) : null}
                        </div>
                      </PopoverContent>
                    </Popover>
                  </div>
                </div>
              </div>
            )})}
          </div>
          <div className="flex flex-wrap items-center justify-end gap-x-3 gap-y-2 border-t border-neutral-100 px-4 py-3 text-sm text-neutral-500">
            <PageSizeSelect
              value={pageSize}
              options={[8, 12, 16, 20]}
              unit="张"
              onChange={(size) => {
                setPageSize(size);
                setPage(1);
              }}
            />
            <span>第 {safePage} / {pageCount} 页，共 {filteredItems.length} 张</span>
            <Button variant="outline" size="icon" className="size-9 rounded-lg border-neutral-200 bg-white" disabled={safePage <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>
              <ChevronLeft className="size-4" />
            </Button>
            <Button variant="outline" size="icon" className="size-9 rounded-lg border-neutral-200 bg-white" disabled={safePage >= pageCount} onClick={() => setPage((value) => Math.min(pageCount, value + 1))}>
              <ChevronRight className="size-4" />
            </Button>
          </div>
          {!isLoading && filteredItems.length === 0 ? <div className="px-6 py-14 text-center text-sm text-neutral-500">没有找到图片</div> : null}
          </>
          ) : (
            <button
              type="button"
              aria-expanded={false}
              onClick={() => setLibraryOpen(true)}
              className="group flex w-full items-center gap-3.5 px-5 py-4 text-left transition-colors hover:bg-muted/40"
            >
              <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-brand-soft text-brand">
                <ImageIcon className="size-[18px]" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2">
                  <span className="text-[15px] font-semibold tracking-tight text-foreground">图片库</span>
                  <ChevronDown className="size-4 text-muted-foreground transition-transform group-hover:translate-y-0.5" />
                </span>
                <span className="mt-0.5 block text-xs text-muted-foreground">
                  默认不加载，展开后按上方时间范围加载图片
                </span>
              </span>
              <span className="hidden shrink-0 items-center rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors group-hover:border-brand/30 group-hover:text-foreground sm:inline-flex">
                展开加载
              </span>
            </button>
          )}
        </CardContent>
      </Card>

      <Dialog open={dialogVisible} onOpenChange={(open) => { if (!open) closeDialog(); }}>
        <DialogContent className="max-w-sm overflow-hidden rounded-2xl">
          <DialogHeader>
            <DialogTitle className="pr-8">确认删除</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-neutral-600">
            确定要删除这张图片吗？此操作不可恢复。
          </p>
          {deleteTarget ? (
            <div className="flex items-center gap-3 overflow-hidden rounded-xl border border-neutral-200 bg-neutral-50 p-3">
              <img
                src={deleteTarget.thumbnail_url || deleteTarget.url}
                alt=""
                className="size-16 shrink-0 rounded-lg object-cover"
                onError={(e) => { if (e.currentTarget.src !== deleteTarget.url) e.currentTarget.src = deleteTarget.url; }}
              />
              <div className="min-w-0 overflow-hidden text-xs text-neutral-500">
                <div className="truncate font-medium text-neutral-700">{deleteTarget.name}</div>
                <div className="truncate">{deleteTarget.created_at}</div>
                <div>{formatSize(deleteTarget.size)}</div>
              </div>
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={closeDialog} className="rounded-xl">
              取消
            </Button>
            <Button variant="destructive" onClick={() => void handleDelete()} disabled={isDeleting} className="rounded-xl">
              {isDeleting ? <LoaderCircle className="mr-1 size-4 animate-spin" /> : <Trash2 className="mr-1 size-4" />}
              删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ImageLightbox
        images={lightboxImages}
        currentIndex={lightboxIndex}
        open={lightboxOpen}
        onOpenChange={setLightboxOpen}
        onIndexChange={setLightboxIndex}
      />
      <Dialog open={deleteMode === "selected" || deleteMode === "filtered"} onOpenChange={(open) => (!open ? setDeleteMode(null) : null)}>
        <DialogContent showCloseButton={false} className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>{deleteMode === "filtered" ? "删除匹配日期的图片" : "删除所选图片"}</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-neutral-600">
            确认删除 {selectedCount} 张图片吗？删除后无法恢复。
          </p>
          <DialogFooter>
            <Button variant="outline" className="rounded-xl" onClick={() => setDeleteMode(null)} disabled={isDeleting}>
              取消
            </Button>
            <Button className="rounded-xl bg-rose-600 text-white hover:bg-rose-700" onClick={() => void confirmDelete()} disabled={isDeleting || selectedCount === 0}>
              {isDeleting ? <LoaderCircle className="size-4 animate-spin" /> : null}
              确认删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={Boolean(tagDeleteTarget)} onOpenChange={(open) => { if (!open) setTagDeleteTarget(null); }}>
        <DialogContent className="max-w-sm rounded-2xl">
          <DialogHeader>
            <DialogTitle>删除标签</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-neutral-600">
            确定要删除标签 <span className="font-semibold">"{tagDeleteTarget}"</span> 吗？将从所有图片中移除该标签。
          </p>
          <DialogFooter>
            <Button variant="outline" className="rounded-xl" onClick={() => setTagDeleteTarget(null)}>
              取消
            </Button>
            <Button
              variant="destructive"
              className="rounded-xl"
              onClick={() => {
                if (tagDeleteTarget) void handleDeleteTag(tagDeleteTarget);
                setTagDeleteTarget(null);
              }}
            >
              确认删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

export default function ImageManagerPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth || !session || session.role !== "admin") {
    return <div className="flex min-h-[40vh] items-center justify-center"><LoaderCircle className="size-5 animate-spin text-neutral-400" /></div>;
  }
  return <ImageManagerContent />;
}
