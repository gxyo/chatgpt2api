"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ComponentProps } from "react";
import {
  Ban,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleOff,
  Copy,
  Download,
  FlaskConical,
  Link2,
  List,
  LoaderCircle,
  LogIn,
  Pencil,
  RefreshCw,
  Search,
  Trash2,
  UserRound,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FilterCard, FilterField } from "@/components/filter-card";
import { PageHeader } from "@/components/page-header";
import { Card, CardContent } from "@/components/ui/card";
import { SegmentedControl } from "@/components/ui/segmented";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  deleteAccounts,
  fetchAccounts,
  fetchModels,
  fetchRefreshProgress,
  fetchReLoginProgress,
  fetchUpstreamModels,
  probeUpstreamModel,
  reLoginAccounts,
  refreshAccounts,
  testProxy,
  updateAccount,
  type Account,
  type AccountRefreshResponse,
  type AccountStatus,
  type Model,
  type RefreshProgressResponse,
  type UpstreamModelProbeResponse,
} from "@/lib/api";
import { formatBeijing, formatBeijingClock, parseInstant } from "@/lib/beijing-time";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { cn } from "@/lib/utils";

import { AccountImportDialog } from "./components/account-import-dialog";

const accountStatusOptions: { label: string; value: AccountStatus | "all" }[] = [
  { label: "全部状态", value: "all" },
  { label: "正常", value: "正常" },
  { label: "限流", value: "限流" },
  { label: "异常", value: "异常" },
  { label: "禁用", value: "禁用" },
];

const statusMeta: Record<
  AccountStatus,
  {
    icon: typeof CheckCircle2;
    badge: ComponentProps<typeof Badge>["variant"];
  }
> = {
  正常: { icon: CheckCircle2, badge: "success" },
  限流: { icon: CircleAlert, badge: "warning" },
  异常: { icon: CircleOff, badge: "danger" },
  禁用: { icon: Ban, badge: "secondary" },
};

const metricCards = [
  { key: "total", label: "账户总数", color: "text-neutral-900", icon: UserRound },
  { key: "active", label: "正常账户", color: "text-emerald-600", icon: CheckCircle2 },
  { key: "limited", label: "限流账户", color: "text-orange-500", icon: CircleAlert },
  { key: "abnormal", label: "异常账户", color: "text-rose-500", icon: CircleOff },
  { key: "disabled", label: "禁用账户", color: "text-neutral-500", icon: Ban },
  { key: "quota", label: "剩余额度", color: "text-blue-500", icon: RefreshCw },
] as const;

function isUnlimitedImageQuotaAccount(account: Account) {
  return account.type === "pro" || account.type === "prolite";
}

function imageQuotaUnknown(account: Account) {
  return Boolean(account.image_quota_unknown);
}

function formatCompact(value: number) {
  if (value >= 1000) {
    return `${(value / 1000).toFixed(1)}k`;
  }
  return String(value);
}

function formatQuota(account: Account) {
  if (isUnlimitedImageQuotaAccount(account)) {
    return "∞";
  }
  if (imageQuotaUnknown(account)) {
    return "未知";
  }
  return String(Math.max(0, account.quota));
}

function formatRestoreAt(value?: string | null) {
  if (!value) {
    return { absolute: "—", relative: "" };
  }

  // restore_at 来自上游，带时区；剩余时长只跟时刻有关，展示统一按北京时间。
  const date = parseInstant(value);
  if (!date) {
    return { absolute: value, relative: "" };
  }

  const diffMs = Math.max(0, date.getTime() - Date.now());
  const totalHours = Math.ceil(diffMs / (1000 * 60 * 60));
  const days = Math.floor(totalHours / 24);
  const hours = totalHours % 24;
  const relative = diffMs > 0 ? `剩余 ${days}d ${hours}h` : "已到恢复时间";

  return { absolute: formatBeijingClock(value), relative };
}

function formatQuotaSummary(accounts: Account[]) {
  const availableAccounts = accounts.filter((account) => account.status === "正常");
  if (availableAccounts.some(isUnlimitedImageQuotaAccount)) {
    return "∞";
  }
  if (availableAccounts.some(imageQuotaUnknown)) {
    return "未知";
  }
  return formatCompact(availableAccounts.reduce((sum, account) => sum + Math.max(0, account.quota), 0));
}

function maskToken(token?: string) {
  if (!token) return "—";
  if (token.length <= 18) return token;
  return `${token.slice(0, 16)}...${token.slice(-8)}`;
}

function downloadTokens(accounts: Account[]) {
  const content = `${accounts.map((account) => account.access_token).join("\n")}\n`;
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `accounts-${Date.now()}.txt`;
  link.click();
  URL.revokeObjectURL(url);
}

function displayAccountType(account: Account) {
  return account.type || "Free";
}

function displayAccountSource(account: Account) {
  const source = String(account.source_type || "").trim().toLowerCase();
  if (!source) {
    return "web";
  }
  if (source === "web") {
    return "web";
  }
  return source;
}

function AccountsPageContent() {
  const didLoadRef = useRef(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [availableModels, setAvailableModels] = useState<Model[]>([]);
  // 「获取模型列表」弹窗：直连 ChatGPT 上游取模型列表，看它到底返回了什么。
  const [isModelsDialogOpen, setIsModelsDialogOpen] = useState(false);
  const [isFetchingModelList, setIsFetchingModelList] = useState(false);
  const [modelsRaw, setModelsRaw] = useState<string | null>(null);
  const [modelsError, setModelsError] = useState<string | null>(null);
  // "anonymous" 或号池账号在 accounts 里的下标：决定探测走匿名还是该账号的登录通道。
  const [probeAccountKey, setProbeAccountKey] = useState("anonymous");
  const modelProbeSeqRef = useRef(0);
  // 模型可用性探测：真实发一条极短对话，看 slug 是否被接受、上游回报什么。
  const [probeModelInput, setProbeModelInput] = useState("");
  const [isProbingModel, setIsProbingModel] = useState(false);
  const [probeResult, setProbeResult] = useState<UpstreamModelProbeResponse | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState<AccountStatus | "all">("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState("10");
  const [editingAccount, setEditingAccount] = useState<Account | null>(null);
  const [editStatus, setEditStatus] = useState<AccountStatus>("正常");
  const [editProxy, setEditProxy] = useState("");
  const [isTestingProxy, setIsTestingProxy] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingModels, setIsLoadingModels] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshingTokens, setRefreshingTokens] = useState<Set<string>>(new Set());
  const [isDeleting, setIsDeleting] = useState(false);
  const [isUpdating, setIsUpdating] = useState(false);
  const [isRelogining, setIsRelogining] = useState(false);
  const [progress, setProgress] = useState<{
    visible: boolean;
    current: number;
    total: number;
    message: string;
    email: string;
  }>({
    visible: false,
    current: 0,
    total: 0,
    message: "",
    email: "",
  });
  const progressRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [refreshSummary, setRefreshSummary] = useState<Record<string, number | string> | null>(null);

  const loadAccounts = async (silent = false) => {
    if (!silent) {
      setIsLoading(true);
    }
    try {
      const data = await fetchAccounts();
      setAccounts(data.items);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载账户失败";
      toast.error(message);
    } finally {
      if (!silent) {
        setIsLoading(false);
      }
    }
  };

  const loadModels = async () => {
    setIsLoadingModels(true);
    try {
      const data = await fetchModels();
      setAvailableModels(Array.isArray(data.data) ? data.data : []);
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载模型列表失败";
      toast.error(message);
    } finally {
      setIsLoadingModels(false);
    }
  };

  const loadModelListSnapshot = async (accessToken = "") => {
    // 快速切换通道时只认最后一次请求，避免旧响应覆盖新选择。
    const seq = ++modelProbeSeqRef.current;
    setIsFetchingModelList(true);
    setModelsError(null);
    try {
      const data = await fetchUpstreamModels(accessToken);
      if (seq !== modelProbeSeqRef.current) {
        return;
      }
      // 不裁剪字段：弹窗要展示上游原样返回的内容。
      setModelsRaw(JSON.stringify(data, null, 2));
    } catch (error) {
      if (seq !== modelProbeSeqRef.current) {
        return;
      }
      setModelsRaw(null);
      setModelsError(error instanceof Error ? error.message : "获取模型列表失败");
    } finally {
      if (seq === modelProbeSeqRef.current) {
        setIsFetchingModelList(false);
      }
    }
  };

  const probeAccessToken = (key: string) =>
    key === "anonymous" ? "" : (accounts[Number(key)]?.access_token ?? "");

  const openModelListDialog = () => {
    // 号池在弹窗关闭期间可能变化：下标失效时回落到匿名通道。
    const key = probeAccountKey === "anonymous" || accounts[Number(probeAccountKey)]
      ? probeAccountKey
      : "anonymous";
    if (key !== probeAccountKey) {
      setProbeAccountKey(key);
    }
    setIsModelsDialogOpen(true);
    void loadModelListSnapshot(probeAccessToken(key));
  };

  const handleProbeModel = async () => {
    setIsProbingModel(true);
    setProbeResult(null);
    try {
      const data = await probeUpstreamModel(
        probeAccessToken(probeAccountKey),
        probeModelInput.trim(),
      );
      setProbeResult(data);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "探测模型失败");
    } finally {
      setIsProbingModel(false);
    }
  };

  const copyModelsRaw = async () => {
    if (!modelsRaw) {
      return;
    }
    try {
      await navigator.clipboard.writeText(modelsRaw);
      toast.success("模型列表 JSON 已复制");
    } catch {
      toast.error("复制失败");
    }
  };

  // 上游 payload.models 才是有效模型清单：slug 去空、去重，title 只作说明。
  const upstreamModelView = useMemo(() => {
    const empty = {
      channel: "",
      accountEmail: "",
      endpoint: "",
      models: [] as Array<{ slug: string; title: string }>,
    };
    if (!modelsRaw) {
      return empty;
    }
    try {
      const parsed = JSON.parse(modelsRaw) as {
        channel?: string;
        account_email?: string;
        endpoint?: string;
        payload?: { models?: unknown; data?: unknown };
      };
      const rawModels = Array.isArray(parsed.payload?.models)
        ? parsed.payload.models
        : Array.isArray(parsed.payload?.data)
          ? parsed.payload.data
          : [];
      const seen = new Set<string>();
      const models: Array<{ slug: string; title: string }> = [];
      for (const item of rawModels) {
        if (!item || typeof item !== "object") {
          continue;
        }
        const record = item as { slug?: unknown; id?: unknown; title?: unknown };
        const slug =
          typeof record.slug === "string"
            ? record.slug.trim()
            : typeof record.id === "string"
              ? record.id.trim()
              : "";
        if (!slug || seen.has(slug)) {
          continue;
        }
        seen.add(slug);
        models.push({ slug, title: typeof record.title === "string" ? record.title.trim() : "" });
      }
      return {
        channel: parsed.channel === "authenticated" ? "登录通道" : "匿名通道",
        accountEmail: typeof parsed.account_email === "string" ? parsed.account_email : "",
        endpoint: parsed.endpoint || "",
        models,
      };
    } catch {
      return empty;
    }
  }, [modelsRaw]);

  useEffect(() => {
    if (didLoadRef.current) {
      return;
    }
    didLoadRef.current = true;
    void loadAccounts();
    void loadModels();

    // 清理进度条定时器
    return () => {
      if (progressRef.current) clearInterval(progressRef.current);
    };
  }, []);

  const filteredAccounts = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return accounts.filter((account) => {
      const searchMatched =
        normalizedQuery.length === 0 || (account.email ?? "").toLowerCase().includes(normalizedQuery);
      const typeMatched = typeFilter === "all" || displayAccountType(account) === typeFilter;
      const statusMatched = statusFilter === "all" || account.status === statusFilter;
      return searchMatched && typeMatched && statusMatched;
    });
  }, [accounts, query, statusFilter, typeFilter]);

  const pageCount = Math.max(1, Math.ceil(filteredAccounts.length / Number(pageSize)));
  const safePage = Math.min(page, pageCount);
  const startIndex = (safePage - 1) * Number(pageSize);
  const currentRows = filteredAccounts.slice(startIndex, startIndex + Number(pageSize));
  const allCurrentSelected =
    currentRows.length > 0 && currentRows.every((row) => selectedIds.includes(row.access_token));

  const summary = useMemo(() => {
    const total = accounts.length;
    const active = accounts.filter((item) => item.status === "正常").length;
    const limited = accounts.filter((item) => item.status === "限流").length;
    const abnormal = accounts.filter((item) => item.status === "异常").length;
    const disabled = accounts.filter((item) => item.status === "禁用").length;
    const quota = formatQuotaSummary(accounts);

    return { total, active, limited, abnormal, disabled, quota };
  }, [accounts]);

  const accountTypeOptions = useMemo(
    () => [
      { label: "全部类型", value: "all" },
      ...Array.from(new Set(accounts.map(displayAccountType))).map((type) => ({ label: type, value: type })),
    ],
    [accounts],
  );

  const selectedTokens = useMemo(() => {
    const selectedSet = new Set(selectedIds);
    return accounts.filter((item) => selectedSet.has(item.access_token)).map((item) => item.access_token);
  }, [accounts, selectedIds]);

  const abnormalTokens = useMemo(() => {
    return accounts.filter((item) => item.status === "异常").map((item) => item.access_token);
  }, [accounts]);

  const paginationItems = useMemo(() => {
    const items: (number | "...")[] = [];
    const start = Math.max(1, safePage - 1);
    const end = Math.min(pageCount, safePage + 1);

    if (start > 1) items.push(1);
    if (start > 2) items.push("...");
    for (let current = start; current <= end; current += 1) items.push(current);
    if (end < pageCount - 1) items.push("...");
    if (end < pageCount) items.push(pageCount);

    return items;
  }, [pageCount, safePage]);

  const handleDeleteTokens = async (tokens: string[]) => {
    if (tokens.length === 0) {
      toast.error("请先选择要删除的账户");
      return;
    }

    setIsDeleting(true);
    try {
      const data = await deleteAccounts(tokens);
      setAccounts(data.items);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));
      toast.success(`删除 ${data.removed ?? 0} 个账户`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "删除账户失败";
      toast.error(message);
    } finally {
      setIsDeleting(false);
    }
  };

  const handleRefreshAccounts = async (accessTokens: string[]) => {
    if (accessTokens.length === 0) {
      toast.error("没有需要刷新的账户");
      return;
    }

    if (accessTokens.length === 1) {
      setRefreshingTokens((prev) => new Set([...prev, accessTokens[0]]));
      try {
        const { progress_id } = await refreshAccounts(accessTokens);
        // 单账号：轮询等待完成
        await pollRefreshProgress(progress_id, (progress) => {
          if (progress.done && progress.result) {
            setAccounts(progress.result.items);
            setSelectedIds((prev) => prev.filter((id) => progress.result!.items.some((item) => item.access_token === id)));
          }
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : "刷新账户失败";
        toast.error(message);
      } finally {
        setRefreshingTokens((prev) => {
          const next = new Set(prev);
          next.delete(accessTokens[0]);
          return next;
        });
      }
      return;
    }

    setIsRefreshing(true);

    // 计算非选中账号的基数（统计卡片联动用）
    const selectedTokenSet = new Set(accessTokens);
    const baseAccountsList = accounts.filter((a) => !selectedTokenSet.has(a.access_token));
    const baseActive = baseAccountsList.filter((a) => a.status === "正常").length;
    const baseLimited = baseAccountsList.filter((a) => a.status === "限流").length;
    const baseAbnormal = baseAccountsList.filter((a) => a.status === "异常").length;
    const baseDisabled = baseAccountsList.filter((a) => a.status === "禁用").length;
    const baseNormalAccounts = baseAccountsList.filter((a) => a.status === "正常");
    const baseHasUnlimited = baseNormalAccounts.some(isUnlimitedImageQuotaAccount);
    const baseHasUnknown = baseNormalAccounts.some(imageQuotaUnknown);
    const baseQuotaNum = baseNormalAccounts.reduce((s, a) => s + Math.max(0, a.quota), 0);

    // 显示进度条（只显示当前任务，不含分类统计）
    const total = accessTokens.length;
    setProgress({
      visible: true,
      current: 0,
      total,
      message: "正在刷新账号信息...",
      email: "",
    });

    try {
      const { progress_id } = await refreshAccounts(accessTokens);

      // 轮询进度到完成
      const data = await new Promise<AccountRefreshResponse>((resolve, reject) => {
        const pollTimer = setInterval(async () => {
          try {
            const p = await fetchRefreshProgress(progress_id);
            if (p.done) {
              clearInterval(pollTimer);
              if (p.error) {
                reject(new Error(p.error));
                return;
              }
              if (!p.result) {
                reject(new Error("刷新结果为空"));
                return;
              }
              // 更新最终进度显示
              setProgress((prev) => ({
                ...prev,
                current: prev.total,
                message: "刷新完成",
              }));
              // 清除联动统计
              setRefreshSummary(null);
              resolve(p.result);
            } else {
              // 实时更新进度
              setProgress((prev) => ({
                ...prev,
                current: p.processed,
              }));
              // 实时更新统计卡片：基数 + 已刷新的累加结果
              const runningActive = baseActive + ((p.status_counts?.["正常"]) ?? 0);
              const runningLimited = baseLimited + ((p.status_counts?.["限流"]) ?? 0);
              const runningAbnormal = baseAbnormal + ((p.status_counts?.["异常"]) ?? 0);
              const runningDisabled = baseDisabled + ((p.status_counts?.["禁用"]) ?? 0);
              let runningQuota: string | number;
              if (baseHasUnlimited) {
                runningQuota = "∞";
              } else if (baseHasUnknown) {
                runningQuota = "未知";
              } else {
                runningQuota = formatCompact(baseQuotaNum + (p.total_quota ?? 0));
              }
              setRefreshSummary({
                total: accounts.length,
                active: runningActive,
                limited: runningLimited,
                abnormal: runningAbnormal,
                disabled: runningDisabled,
                quota: runningQuota,
              });
            }
          } catch (err) {
            clearInterval(pollTimer);
            reject(err);
          }
        }, 300);
      });

      // 刷新完成，更新数据
      setAccounts(data.items);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));

      setProgress({
        visible: true,
        current: total,
        total,
        message: "刷新完成",
        email: "",
      });
      setTimeout(() => setProgress({ visible: false, current: 0, total: 0, message: "", email: "" }), 800);

      if ((data.errors ?? []).length > 0) {
        const firstError = data.errors?.[0]?.error;
        toast.error(
          `刷新成功 ${data.refreshed} 个，失败 ${(data.errors ?? []).length} 个${firstError ? `，首个错误：${firstError}` : ""}`,
        );
      } else {
        toast.success(`刷新成功 ${data.refreshed} 个账户`);
      }
    } catch (error) {
      setProgress({ visible: false, current: 0, total: 0, message: "", email: "" });
      setRefreshSummary(null);
      const message = error instanceof Error ? error.message : "刷新账户失败";
      toast.error(message);
    } finally {
      setIsRefreshing(false);
    }
  };

  const pollRefreshProgress = async (
    progressId: string,
    onUpdate: (p: RefreshProgressResponse) => void,
  ): Promise<void> => {
    return new Promise<void>((resolve, reject) => {
      const timer = setInterval(async () => {
        try {
          const p = await fetchRefreshProgress(progressId);
          if (p.done) {
            clearInterval(timer);
            if (p.error) {
              reject(new Error(p.error));
            } else {
              onUpdate(p);
              resolve();
            }
          }
        } catch (err) {
          clearInterval(timer);
          reject(err);
        }
      }, 500);
    });
  };

  const handleReLogin = async (accessTokens: string[]) => {
    if (accessTokens.length === 0) {
      toast.error("请先选择要恢复的账户");
      return;
    }

    // 只处理异常账号，过滤非异常账号
    const abnormalTokens = accessTokens.filter((token) => {
      const account = accounts.find((a) => a.access_token === token);
      return account?.status === "异常";
    });

    if (abnormalTokens.length === 0) {
      toast.error("选中账号中没有异常账号");
      return;
    }

    if (abnormalTokens.length < accessTokens.length) {
      toast.info(`已过滤 ${accessTokens.length - abnormalTokens.length} 个非异常账号`);
    }

    setIsRelogining(true);

    // 计算非选中账号的基数（统计卡片联动用）
    const selectedTokenSet = new Set(abnormalTokens);
    const baseAccountsList = accounts.filter((a) => !selectedTokenSet.has(a.access_token));
    const baseActive = baseAccountsList.filter((a) => a.status === "正常").length;
    const baseLimited = baseAccountsList.filter((a) => a.status === "限流").length;
    const baseAbnormal = baseAccountsList.filter((a) => a.status === "异常").length;
    const baseDisabled = baseAccountsList.filter((a) => a.status === "禁用").length;

    // 显示进度条（真实进度）
    const total = abnormalTokens.length;
    setProgress({ visible: true, current: 0, total, message: "正在尝试恢复异常账号...", email: "" });

    try {
      const { progress_id } = await reLoginAccounts(abnormalTokens);

      // 轮询进度到完成
      await new Promise<void>((resolve, reject) => {
        const pollTimer = setInterval(async () => {
          try {
            const p = await fetchReLoginProgress(progress_id);
            if (p.done) {
              clearInterval(pollTimer);
              if (p.error) {
                reject(new Error(p.error));
                return;
              }
              setProgress((prev) => ({ ...prev, current: prev.total, message: "恢复流程已完成" }));
              setRefreshSummary(null);
              resolve();
            } else {
              // 实时更新进度
              const results = p.results ?? [];
              // 找到最新一条有错误的结果
              const lastErrorResult = [...results].reverse().find((r) => r.error);
              const emailHint = lastErrorResult
                ? `失败: ${lastErrorResult.token} ${lastErrorResult.error ?? ""}`
                : `已处理 ${p.processed}/${p.total}`;
              setProgress((prev) => ({
                ...prev,
                current: p.processed,
                email: emailHint,
                message: "正在尝试恢复异常账号...",
              }));

              // 实时更新统计卡片：基数 + 已处理的恢复结果
              let runningActive = baseActive;
              let runningAbnormal = baseAbnormal;
              let runningDisabled = baseDisabled;
              for (const r of results) {
                if (r.status === "成功") {
                  runningActive += 1;
                  runningAbnormal -= 1;
                } else if (r.status === "禁用") {
                  runningDisabled += 1;
                  runningAbnormal -= 1;
                }
                // "异常"或"跳过"：保持异常状态不变
              }
              setRefreshSummary({
                total: accounts.length,
                active: runningActive,
                limited: baseLimited,
                abnormal: runningAbnormal,
                disabled: runningDisabled,
                quota: summary.quota,
              });
            }
          } catch (err) {
            clearInterval(pollTimer);
            reject(err);
          }
        }, 300);
      });

      // 等待后台线程完成，再拉取最新数据
      await new Promise<void>((resolve) => setTimeout(resolve, 500));
      try {
        const freshData = await fetchAccounts();
        setAccounts(freshData.items);
        setSelectedIds((prev) => prev.filter((id) => freshData.items.some((item) => item.access_token === id)));
      } catch { /* 静默失败 */ }

      setProgress({
        visible: true,
        current: total,
        total,
        message: "恢复完成",
        email: "",
      });
      setTimeout(() => setProgress({ visible: false, current: 0, total: 0, message: "", email: "" }), 800);

      toast.success(`恢复流程已全部完成`);
    } catch (error) {
      setProgress({ visible: false, current: 0, total: 0, message: "", email: "" });
      setRefreshSummary(null);
      const message = error instanceof Error ? error.message : "重新登录失败";
      toast.error(message);
    } finally {
      setIsRelogining(false);
    }
  };

  const openEditDialog = (account: Account) => {
    setEditingAccount(account);
    setEditStatus(account.status);
    setEditProxy(account.proxy ?? "");
  };

  const handleTestAccountProxy = async () => {
    const candidate = editProxy.trim();
    if (!candidate) {
      toast.error("请先填写代理地址");
      return;
    }
    setIsTestingProxy(true);
    try {
      const data = await testProxy(candidate);
      data.result.ok
        ? toast.success(`代理可用（${data.result.latency_ms} ms，HTTP ${data.result.status}）`)
        : toast.error(`代理不可用：${data.result.error ?? "未知错误"}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "测试代理失败");
    } finally {
      setIsTestingProxy(false);
    }
  };

  const handleUpdateAccount = async () => {
    if (!editingAccount) {
      return;
    }

    setIsUpdating(true);
    try {
      const data = await updateAccount(editingAccount.access_token, {
        status: editStatus,
        proxy: editProxy.trim(),
      });
      setAccounts(data.items);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));
      setEditingAccount(null);
      toast.success("账号信息已更新");
    } catch (error) {
      const message = error instanceof Error ? error.message : "更新账号失败";
      toast.error(message);
    } finally {
      setIsUpdating(false);
    }
  };

  const toggleSelectAll = (checked: boolean) => {
    if (checked) {
      setSelectedIds((prev) => Array.from(new Set([...prev, ...currentRows.map((item) => item.access_token)])));
      return;
    }
    setSelectedIds((prev) => prev.filter((id) => !currentRows.some((row) => row.access_token === id)));
  };

  return (
    <>
      <PageHeader
        title="号池管理"
        description="管理账号状态、额度与专属代理，异常账号可以批量刷新或移除。"
        actions={
          <>
            <Button
              variant="outline"
              className="h-10 px-4"
              onClick={() => void handleRefreshAccounts(accounts.map((item) => item.access_token))}
              disabled={isLoading || isRefreshing || isDeleting || accounts.length === 0}
            >
              <RefreshCw className={cn("size-4", isRefreshing ? "animate-spin" : "")} />
              一键刷新所有账号信息和额度
            </Button>
            <AccountImportDialog
              disabled={isLoading || isRefreshing || isDeleting}
              onImported={(items) => {
                setAccounts(items);
                setSelectedIds([]);
                setPage(1);
              }}
            />
            <Button
              variant="outline"
              className="h-10 px-4"
              onClick={() => downloadTokens(accounts)}
              disabled={accounts.length === 0}
            >
              <Download className="size-4" />
              导出全部 Token
            </Button>
          </>
        }
      />

      {/* 进度条 */}
      {progress.visible && (
        <div className="overflow-hidden rounded-2xl border border-neutral-200 bg-white/90 shadow-sm">
          <div className="px-4 py-3">
            <div className="flex items-center justify-between text-sm">
              <span className="text-neutral-600">
                {progress.message}
                {progress.email && <span className="ml-1 font-medium text-neutral-700">{progress.email}</span>}
              </span>
              <span className="font-medium text-neutral-700">
                {progress.current}/{progress.total}
              </span>
            </div>
            <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-neutral-100">
              <div
                className="h-full rounded-full bg-gradient-to-r from-amber-400 to-orange-500 transition-all duration-300 ease-out"
                style={{ width: `${progress.total > 0 ? (progress.current / progress.total) * 100 : 0}%` }}
              />
            </div>
          </div>
        </div>
      )}

      <Dialog open={Boolean(editingAccount)} onOpenChange={(open) => (!open ? setEditingAccount(null) : null)}>
        <DialogContent showCloseButton={false} className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>编辑账户</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              手动修改账号状态和专属代理。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm font-medium text-neutral-700">状态</label>
              <Select value={editStatus} onValueChange={(value) => setEditStatus(value as AccountStatus)}>
                <SelectTrigger className="h-11 rounded-xl border-neutral-200 bg-white">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {accountStatusOptions
                    .filter((option) => option.value !== "all")
                    .map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium text-neutral-700">账号代理</label>
              <div className="flex flex-col gap-2 sm:flex-row">
                <Input
                  value={editProxy}
                  onChange={(event) => setEditProxy(event.target.value)}
                  placeholder="留空走全局代理，例如 http://127.0.0.1:7890"
                  className="h-11 rounded-xl border-neutral-200 bg-white"
                />
                <Button
                  variant="outline"
                  className="h-11 rounded-xl border-neutral-200 bg-white px-4 text-neutral-700 sm:w-24"
                  onClick={() => void handleTestAccountProxy()}
                  disabled={isTestingProxy}
                >
                  {isTestingProxy ? <LoaderCircle className="size-4 animate-spin" /> : <Link2 className="size-4" />}
                  测试
                </Button>
              </div>
            </div>
          </div>
          <DialogFooter className="pt-2">
            <Button
              variant="secondary"
              className="h-10 rounded-xl bg-neutral-100 px-5 text-neutral-700 hover:bg-neutral-200"
              onClick={() => setEditingAccount(null)}
              disabled={isUpdating}
            >
              取消
            </Button>
            <Button
              className="h-10 rounded-xl bg-brand px-5 text-white hover:bg-brand/90"
              onClick={() => void handleUpdateAccount()}
              disabled={isUpdating}
            >
              {isUpdating ? <LoaderCircle className="size-4 animate-spin" /> : null}
              保存修改
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={isModelsDialogOpen} onOpenChange={setIsModelsDialogOpen}>
        <DialogContent className="max-h-[90dvh] w-[min(92vw,720px)] overflow-y-auto rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>获取模型列表</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              直连 ChatGPT 上游获取模型，返回内容未经过本项目{" "}
              <code className="rounded bg-muted px-1 py-0.5 font-mono text-[11px]">GET /v1/models</code>
              的本地合并。默认走匿名通道，选择号池账号可查看该账号登录后的模型列表。
            </DialogDescription>
          </DialogHeader>
          <div className="flex min-w-0 items-center gap-3">
            <span className="shrink-0 text-xs text-neutral-500">探测通道</span>
            <Select
              value={probeAccountKey}
              onValueChange={(value) => {
                setProbeAccountKey(value);
                setProbeResult(null);
                void loadModelListSnapshot(probeAccessToken(value));
              }}
              disabled={isFetchingModelList}
            >
              <SelectTrigger className="h-9 min-w-0 flex-1 rounded-xl">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="anonymous">匿名通道（/backend-anon/models）</SelectItem>
                {accounts.map((account, index) => (
                  <SelectItem key={account.access_token} value={String(index)}>
                    {account.email || maskToken(account.access_token)}（{displayAccountType(account)}）
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="min-w-0 space-y-2">
            <div className="flex min-w-0 items-center gap-2">
              <Input
                value={probeModelInput}
                onChange={(event) => setProbeModelInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    void handleProbeModel();
                  }
                }}
                placeholder="输入模型 slug 探测（留空探测默认上游模型）"
                className="h-9 min-w-0 flex-1 rounded-xl"
              />
              <Button
                variant="outline"
                className="h-9 shrink-0"
                onClick={() => void handleProbeModel()}
                disabled={isProbingModel}
              >
                {isProbingModel ? (
                  <LoaderCircle className="size-4 animate-spin" />
                ) : (
                  <FlaskConical className="size-4" />
                )}
                探测
              </Button>
            </div>
            <p className="text-xs text-neutral-400">
              会向当前通道真实发送一条极短对话，可能出现在该账号的对话历史里。
            </p>
            {probeResult ? (
              <div
                className={cn(
                  "rounded-xl border px-4 py-3 text-xs",
                  probeResult.ok
                    ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                    : "border-rose-200 bg-rose-50 text-rose-600",
                )}
              >
                <div className="flex items-center gap-1.5 font-medium">
                  {probeResult.ok ? (
                    <CheckCircle2 className="size-3.5" />
                  ) : (
                    <CircleOff className="size-3.5" />
                  )}
                  <span className="font-mono">{probeResult.model}</span>
                  {probeResult.ok ? "可请求" : "请求失败"}
                  <span className="font-normal opacity-70">
                    ·{" "}
                    {probeResult.channel === "authenticated"
                      ? `登录通道${probeResult.account_email ? `（${probeResult.account_email}）` : ""}`
                      : "匿名通道"}
                  </span>
                </div>
                {probeResult.ok ? (
                  <div className="mt-1 text-emerald-600">
                    上游回报 model_slug：{probeResult.model_slug || "（未返回）"}
                    {typeof probeResult.elapsed_ms === "number" ? ` · ${probeResult.elapsed_ms}ms` : ""}
                  </div>
                ) : (
                  <div className="mt-1 break-all">{probeResult.error || "未知错误"}</div>
                )}
              </div>
            ) : null}
          </div>
          {isFetchingModelList ? (
            <div className="flex items-center justify-center gap-2 py-12 text-sm text-neutral-500">
              <LoaderCircle className="size-4 animate-spin" />
              正在向接口请求模型列表…
            </div>
          ) : modelsError ? (
            <div className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-600">
              {modelsError}
            </div>
          ) : modelsRaw ? (
            <div className="min-w-0 space-y-3">
              <div className="flex min-w-0 items-center justify-between gap-2 text-xs text-neutral-500">
                <span className="min-w-0 truncate" title={upstreamModelView.endpoint}>
                  {upstreamModelView.channel
                    ? `${upstreamModelView.channel}${upstreamModelView.accountEmail ? `（${upstreamModelView.accountEmail}）` : ""} · `
                    : ""}
                  {upstreamModelView.endpoint || "上游"} · 共 {upstreamModelView.models.length} 个模型
                </span>
                <Button variant="outline" size="sm" onClick={() => void copyModelsRaw()}>
                  <Copy className="size-3.5" />
                  复制 JSON
                </Button>
              </div>
              <ul className="max-h-[40vh] divide-y divide-border overflow-auto rounded-xl border border-border">
                {upstreamModelView.models.length > 0 ? (
                  upstreamModelView.models.map((item) => (
                    <li key={item.slug} className="flex items-center justify-between gap-3 px-4 py-2.5">
                      <code className="font-mono text-xs font-medium text-foreground">{item.slug}</code>
                      {item.title ? (
                        <span className="truncate text-xs text-muted-foreground">{item.title}</span>
                      ) : null}
                    </li>
                  ))
                ) : (
                  <li className="px-4 py-3 text-xs text-neutral-500">上游没有返回有效的模型名</li>
                )}
              </ul>
              <details className="group rounded-xl border border-border">
                <summary className="flex cursor-pointer list-none items-center gap-2 rounded-xl px-4 py-2.5 text-xs font-medium text-neutral-600 transition-colors hover:bg-muted/40 [&::-webkit-details-marker]:hidden dark:text-neutral-400">
                  <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" />
                  原始 JSON
                </summary>
                <pre className="max-h-[45vh] overflow-auto border-t border-border bg-muted/40 p-4 font-mono text-[11px] leading-5 whitespace-pre-wrap break-all">
                  {modelsRaw}
                </pre>
              </details>
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => void loadModelListSnapshot()} disabled={isFetchingModelList}>
              <RefreshCw className={cn("size-4", isFetchingModelList ? "animate-spin" : "")} />
              重新获取
            </Button>
            <Button onClick={() => setIsModelsDialogOpen(false)}>关闭</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <section className="space-y-3">
        {/* 六项指标共享一张卡，用发丝线切成六格——参考稿的概览区就是这么组织的。 */}
        <Card className="overflow-hidden">
          <div className="flex items-center justify-between gap-2 border-b border-border px-5 py-4">
            <div className="text-[15px] font-semibold tracking-tight">号池概览</div>
            <div className="text-xs text-muted-foreground">实时状态</div>
          </div>
          <div className="grid grid-cols-2 gap-px bg-border sm:grid-cols-3 xl:grid-cols-6">
            {metricCards.map((item) => {
              const Icon = item.icon;
              const value = (refreshSummary ?? summary)[item.key];
              return (
                <div key={item.key} className="bg-card px-5 py-4">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-medium text-muted-foreground">{item.label}</span>
                    <Icon className="size-4 text-muted-foreground/70" />
                  </div>
                  <div className={cn("mt-2.5 text-[28px] leading-none font-semibold tracking-tight tabular-nums", item.color)}>
                    <span className={typeof value === "number" ? "" : "text-[1.1rem]"}>
                      {typeof value === "number" ? formatCompact(value) : value}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
        <Card>
          <CardContent className="p-5">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="text-[15px] font-semibold tracking-tight">系统可用模型</span>
                <span className="rounded-full bg-brand-soft px-2 py-0.5 text-[11px] font-semibold text-brand tabular-nums">
                  {availableModels.length}
                </span>
              </div>
              <Button variant="outline" size="sm" onClick={openModelListDialog}>
                <List className="size-3.5" />
                获取模型列表
              </Button>
            </div>
            <div className="flex flex-wrap gap-2">
              {availableModels.length > 0 ? (
                availableModels.map((model) => (
                  <button
                    key={model.id}
                    type="button"
                    className="inline-flex cursor-pointer items-center rounded-full border border-border bg-muted/40 px-3 py-1.5 text-xs font-medium text-foreground transition hover:border-brand/30 hover:bg-brand-soft hover:text-brand"
                    onClick={() => {
                      void navigator.clipboard.writeText(model.id);
                      toast.success("模型名已复制");
                    }}
                    title={`点击复制 ${model.id}`}
                  >
                    <img
                      src="/openai.svg"
                      alt=""
                      aria-hidden="true"
                      className="mr-1.5 size-3.5 shrink-0"
                    />
                    {model.id}
                  </button>
                ))
              ) : isLoadingModels ? (
                <span className="text-sm text-neutral-400">正在加载模型列表...</span>
              ) : (
                <span className="text-sm text-neutral-400">当前暂无可用模型</span>
              )}
            </div>
          </CardContent>
        </Card>
      </section>

      <section className="space-y-4">
        <FilterCard>
          <FilterField label="搜索邮箱">
            <div className="relative min-w-[220px]">
              <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground/70" />
              <Input
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setPage(1);
                }}
                placeholder="搜索邮箱"
                className="h-10 rounded-xl pl-10"
              />
            </div>
          </FilterField>
          <FilterField label="账号类型">
            <Select
              value={typeFilter}
              onValueChange={(value) => {
                setTypeFilter(value);
                setPage(1);
              }}
            >
              <SelectTrigger className="h-10 w-full rounded-xl sm:w-[150px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {accountTypeOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FilterField>
          <FilterField label="账号状态">
            <SegmentedControl
              value={statusFilter}
              ariaLabel="账号状态"
              options={accountStatusOptions.map((option) => ({
                value: option.value,
                label: option.value === "all" ? "全部" : option.label,
              }))}
              onChange={(value) => {
                setStatusFilter(value as AccountStatus | "all");
                setPage(1);
              }}
            />
          </FilterField>
          <div className="ml-auto pb-2 text-xs text-muted-foreground sm:pb-2.5">
            共 <span className="font-semibold tabular-nums text-foreground">{filteredAccounts.length}</span> 个账户
          </div>
        </FilterCard>

        {isLoading && accounts.length === 0 ? (
          <Card>
            <CardContent className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
              <div className="rounded-xl bg-neutral-100 p-3 text-neutral-500">
                <LoaderCircle className="size-5 animate-spin" />
              </div>
              <div className="space-y-1">
                <p className="text-sm font-medium text-neutral-700">正在加载账户</p>
                <p className="text-sm text-neutral-500">从后端同步账号列表和状态。</p>
              </div>
            </CardContent>
          </Card>
        ) : null}

        <Card
          className={cn(
            "overflow-hidden",
            isLoading && accounts.length === 0 ? "hidden" : "",
          )}
        >
          <CardContent className="space-y-0 p-0">
            <div className="flex flex-col gap-3 border-b border-border px-5 py-3.5 lg:flex-row lg:items-center lg:justify-between">
              <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                <span className="mr-1 text-[15px] font-semibold tracking-tight text-foreground">账户列表</span>
                <Badge variant="secondary" className="mr-1 rounded-full px-2 py-0.5 text-[11px] tabular-nums">
                  {filteredAccounts.length}
                </Badge>
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-neutral-500 hover:bg-neutral-100"
                  onClick={() => void handleRefreshAccounts(selectedTokens)}
                  disabled={selectedTokens.length === 0 || isRefreshing}
                >
                  {isRefreshing ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
                  刷新选中账号信息和额度
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-amber-600 hover:bg-amber-50 hover:text-amber-700"
                  onClick={() => void handleReLogin(selectedTokens)}
                  disabled={selectedTokens.length === 0 || isRelogining}
                  title="尝试密码登录恢复账号"
                >
                  {isRelogining ? <LoaderCircle className="size-4 animate-spin" /> : <LogIn className="size-4" />}
                  尝试恢复异常账号
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-rose-500 hover:bg-rose-50 hover:text-rose-600"
                  onClick={() => void handleDeleteTokens(abnormalTokens)}
                  disabled={abnormalTokens.length === 0 || isDeleting}
                >
                  {isDeleting ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
                  移除异常账号
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-rose-500 hover:bg-rose-50 hover:text-rose-600"
                  onClick={() => void handleDeleteTokens(selectedTokens)}
                  disabled={selectedTokens.length === 0 || isDeleting}
                >
                  {isDeleting ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
                  删除所选
                </Button>
                {selectedIds.length > 0 ? (
                  <span className="rounded-lg bg-neutral-100 px-2.5 py-1 text-xs font-medium text-neutral-600">
                    已选择 {selectedIds.length} 项
                  </span>
                ) : null}
              </div>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full min-w-[1000px] text-left">
                <thead className="border-b border-border bg-muted/40 text-xs font-medium whitespace-nowrap text-muted-foreground">
                  <tr>
                    <th className="w-12 px-4 py-3">
                      <Checkbox
                        checked={allCurrentSelected}
                        onCheckedChange={(checked) => toggleSelectAll(Boolean(checked))}
                      />
                    </th>
                    <th className="w-56 px-4 py-3">token</th>
                    <th className="w-28 px-4 py-3">类型</th>
                    <th className="w-24 px-4 py-3">来源</th>
                    <th className="w-24 px-4 py-3">状态</th>
                    <th className="w-56 px-4 py-3">账号信息</th>
                    <th className="w-32 px-4 py-3">创建时间</th>
                    <th className="w-24 px-4 py-3">额度</th>
                    <th className="w-40 px-4 py-3">恢复时间</th>
                    <th className="w-18 px-4 py-3">在途</th>
                    <th className="w-18 px-4 py-3">成功</th>
                    <th className="w-18 px-4 py-3">失败</th>
                    <th className="w-24 px-4 py-3">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {currentRows.map((account) => {
                    const status = statusMeta[account.status];
                    const StatusIcon = status.icon;

                    return (
                      <tr
                        key={account.access_token}
                        className="border-b border-neutral-100/80 text-sm text-neutral-600 transition-colors hover:bg-neutral-50/70"
                      >
                        <td className="px-4 py-3">
                          <Checkbox
                            checked={selectedIds.includes(account.access_token)}
                            onCheckedChange={(checked) => {
                              setSelectedIds((prev) =>
                                checked
                                  ? Array.from(new Set([...prev, account.access_token]))
                                  : prev.filter((item) => item !== account.access_token),
                              );
                            }}
                          />
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            <span className="font-medium tracking-tight text-neutral-700">
                              {maskToken(account.access_token)}
                            </span>
                            <button
                              type="button"
                              className="rounded-lg p-1 text-neutral-400 transition hover:bg-neutral-100 hover:text-neutral-700"
                              onClick={() => {
                                void navigator.clipboard.writeText(account.access_token);
                                toast.success("token 已复制");
                              }}
                            >
                              <Copy className="size-4" />
                            </button>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <Badge variant="secondary" className="rounded-md bg-neutral-100 text-neutral-700">
                            {displayAccountType(account)}
                          </Badge>
                        </td>
                        <td className="px-4 py-3">
                          <Badge variant="outline" className="rounded-md border-neutral-200 text-neutral-600">
                            {displayAccountSource(account)}
                          </Badge>
                        </td>
                        <td className="px-4 py-3">
                          <Badge
                            variant={status.badge}
                            className="inline-flex items-center gap-1 rounded-md px-2 py-1"
                          >
                            <StatusIcon className="size-3.5" />
                            {account.status}
                          </Badge>
                        </td>
                        <td className="px-4 py-3">
                          <div className="text-xs leading-5 text-neutral-500">{account.email ?? "—"}</div>
                        </td>
                        <td className="px-4 py-3 text-xs leading-5 whitespace-nowrap text-neutral-500">
                          {(() => {
                            const raw = (account as any).created_at;
                            if (!raw) return "—";
                            // 历史数据是不带时区的 UTC 文本，新数据带 +00:00，两种情况都按 UTC 解析。
                            return formatBeijing(
                              raw,
                              { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" },
                              "utc",
                            );
                          })()}
                        </td>
                        <td className="px-4 py-3">
                          <Badge variant="info" className="rounded-md">
                            {formatQuota(account)}
                          </Badge>
                        </td>
                        <td className="px-4 py-3 text-xs leading-5 text-neutral-500">
                          {(() => {
                            const restore = formatRestoreAt(account.restore_at);
                            return (
                              <div className="space-y-0.5 whitespace-nowrap">
                                {restore.relative ? <div className="font-medium text-neutral-700">{restore.relative}</div> : null}
                                <div>{restore.absolute}</div>
                              </div>
                            );
                          })()}
                        </td>
                        <td className="px-4 py-3">
                          {(() => {
                            const inflight = account.image_inflight ?? 0;
                            return (
                              <span
                                className={
                                  inflight > 0
                                    ? "font-semibold text-amber-600"
                                    : "text-neutral-400"
                                }
                                title={
                                  inflight > 0
                                    ? "当前正在生成的图片数。号池空闲时此值持续 > 0，说明并发槽位泄漏、该账号已被静默排除出调度"
                                    : "当前无在途生图任务"
                                }
                              >
                                {inflight}
                              </span>
                            );
                          })()}
                        </td>
                        <td className="px-4 py-3 text-neutral-500">{account.success}</td>
                        <td className="px-4 py-3 text-neutral-500">{account.fail}</td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1 text-neutral-400">
                            <button
                              type="button"
                              className="rounded-lg p-2 transition hover:bg-neutral-100 hover:text-neutral-700"
                              onClick={() => openEditDialog(account)}
                              disabled={isUpdating}
                            >
                              <Pencil className="size-4" />
                            </button>
                            <button
                              type="button"
                              className="rounded-lg p-2 transition hover:bg-neutral-100 hover:text-neutral-700"
                              onClick={() => void handleRefreshAccounts([account.access_token])}
                              disabled={isRefreshing || refreshingTokens.has(account.access_token)}
                            >
                              <RefreshCw className={cn("size-4", (isRefreshing || refreshingTokens.has(account.access_token)) ? "animate-spin" : "")} />
                            </button>
                            <button
                              type="button"
                              className="rounded-lg p-2 transition hover:bg-rose-50 hover:text-rose-500"
                              onClick={() => void handleDeleteTokens([account.access_token])}
                              disabled={isDeleting}
                            >
                              <Trash2 className="size-4" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>

              {!isLoading && currentRows.length === 0 ? (
                <div className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
                  <div className="rounded-xl bg-neutral-100 p-3 text-neutral-500">
                    <Search className="size-5" />
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm font-medium text-neutral-700">没有匹配的账户</p>
                    <p className="text-sm text-neutral-500">调整筛选条件或搜索关键字后重试。</p>
                  </div>
                </div>
              ) : null}
            </div>

            <div className="border-t border-neutral-100 px-4 py-4">
              <div className="flex items-center justify-center gap-3 overflow-x-auto whitespace-nowrap">
                <div className="shrink-0 text-sm text-neutral-500">
                显示第 {filteredAccounts.length === 0 ? 0 : startIndex + 1} -{" "}
                {Math.min(startIndex + Number(pageSize), filteredAccounts.length)} 条，共{" "}
                {filteredAccounts.length} 条
                </div>

                <span className="shrink-0 text-sm leading-none text-neutral-500">
                  {safePage} / {pageCount} 页
                </span>
                <Select
                  value={pageSize}
                  onValueChange={(value) => {
                    setPageSize(value);
                    setPage(1);
                  }}
                >
                  <SelectTrigger className="h-10 w-[108px] shrink-0 rounded-lg border-neutral-200 bg-white text-sm leading-none">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="10">10 / 页</SelectItem>
                    <SelectItem value="20">20 / 页</SelectItem>
                    <SelectItem value="50">50 / 页</SelectItem>
                    <SelectItem value="100">100 / 页</SelectItem>
                  </SelectContent>
                </Select>
                <Button
                  variant="outline"
                  size="icon"
                  className="size-10 shrink-0 rounded-lg border-neutral-200 bg-white"
                  disabled={safePage <= 1}
                  onClick={() => setPage((prev) => Math.max(1, prev - 1))}
                >
                  <ChevronLeft className="size-4" />
                </Button>
                {paginationItems.map((item, index) =>
                  item === "..." ? (
                    <span key={`ellipsis-${index}`} className="px-1 text-sm text-neutral-400">
                      ...
                    </span>
                  ) : (
                    <Button
                      key={item}
                      variant={item === safePage ? "default" : "outline"}
                      className={cn(
                        "h-10 min-w-10 shrink-0 rounded-lg px-3",
                        item === safePage
                          ? "bg-brand text-white hover:bg-brand/90"
                          : "border-neutral-200 bg-white text-neutral-700",
                      )}
                      onClick={() => setPage(item)}
                    >
                      {item}
                    </Button>
                  ),
                )}
                <Button
                  variant="outline"
                  size="icon"
                  className="size-10 shrink-0 rounded-lg border-neutral-200 bg-white"
                  disabled={safePage >= pageCount}
                  onClick={() => setPage((prev) => Math.min(pageCount, prev + 1))}
                >
                  <ChevronRight className="size-4" />
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      </section>
    </>
  );
}

export default function AccountsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);

  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-neutral-400" />
      </div>
    );
  }

  return <AccountsPageContent />;
}
