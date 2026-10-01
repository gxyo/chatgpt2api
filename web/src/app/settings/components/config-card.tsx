"use client";

import { Ban, CalendarClock, LoaderCircle, PlugZap, Save } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { testProxy, type ProxyTestResult } from "@/lib/api";

import { useSettingsStore } from "../store";

export function ConfigCard() {
  const [isTestingProxy, setIsTestingProxy] = useState(false);
  const [proxyTestResult, setProxyTestResult] = useState<ProxyTestResult | null>(null);
  const logLevelOptions = ["debug", "info", "warning", "error"];
  const config = useSettingsStore((state) => state.config);
  const isLoadingConfig = useSettingsStore((state) => state.isLoadingConfig);
  const isSavingConfig = useSettingsStore((state) => state.isSavingConfig);
  const setRefreshAccountIntervalMinute = useSettingsStore((state) => state.setRefreshAccountIntervalMinute);
  const setRefreshAllAccountsIntervalMinute = useSettingsStore((state) => state.setRefreshAllAccountsIntervalMinute);
  const setImageCleanupIntervalDays = useSettingsStore((state) => state.setImageCleanupIntervalDays);
  const setImageCleanupTime = useSettingsStore((state) => state.setImageCleanupTime);
  const setImagePollTimeoutSecs = useSettingsStore((state) => state.setImagePollTimeoutSecs);
  const setImageAccountConcurrency = useSettingsStore((state) => state.setImageAccountConcurrency);
  const setImageQuotaErrorMessage = useSettingsStore((state) => state.setImageQuotaErrorMessage);
  const setAutoRemoveInvalidAccounts = useSettingsStore((state) => state.setAutoRemoveInvalidAccounts);
  const setAutoRemoveRateLimitedAccounts = useSettingsStore((state) => state.setAutoRemoveRateLimitedAccounts);
  const setLogLevel = useSettingsStore((state) => state.setLogLevel);
  const setProxy = useSettingsStore((state) => state.setProxy);
  const setBaseUrl = useSettingsStore((state) => state.setBaseUrl);
  const setGlobalSystemPrompt = useSettingsStore((state) => state.setGlobalSystemPrompt);
  const setSensitiveWordsText = useSettingsStore((state) => state.setSensitiveWordsText);
  const setAIReviewField = useSettingsStore((state) => state.setAIReviewField);
  const saveConfig = useSettingsStore((state) => state.saveConfig);

  const handleTestProxy = async () => {
    const candidate = String(config?.proxy || "").trim();
    if (!candidate) {
      toast.error("请先填写代理地址");
      return;
    }
    setIsTestingProxy(true);
    setProxyTestResult(null);
    try {
      const data = await testProxy(candidate);
      setProxyTestResult(data.result);
      if (data.result.ok) {
        toast.success(`代理可用（${data.result.latency_ms} ms，HTTP ${data.result.status}）`);
      } else {
        toast.error(`代理不可用：${data.result.error ?? "未知错误"}`);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "测试代理失败");
    } finally {
      setIsTestingProxy(false);
    }
  };

  if (isLoadingConfig) {
    return (
      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="flex items-center justify-center p-10">
          <LoaderCircle className="size-5 animate-spin text-neutral-400" />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
      <CardContent className="space-y-4 p-6 pb-24 md:pr-28 md:pb-6">
        <div className="rounded-xl border border-neutral-200 bg-neutral-50 px-4 py-3 text-sm leading-6 text-neutral-600">
          管理员登录密钥继续从部署配置读取，不再在此页面展示；如需分发给其他人，请在下方创建普通用户密钥。
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-2">
            <label className="text-sm text-neutral-700">限流账号检查间隔</label>
            <Input
              value={config?.refresh_account_interval_minute == null ? "" : String(config.refresh_account_interval_minute)}
              onChange={(event) => setRefreshAccountIntervalMinute(event.target.value)}
              placeholder="留空关闭"
              className="h-10 rounded-xl border-neutral-200 bg-white"
            />
            <p className="text-xs text-neutral-500">单位分钟，留空表示不启用。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-neutral-700">全部账号刷新间隔</label>
            <Input
              value={config?.refresh_all_accounts_interval_minute == null ? "" : String(config.refresh_all_accounts_interval_minute)}
              onChange={(event) => setRefreshAllAccountsIntervalMinute(event.target.value)}
              placeholder="留空关闭"
              className="h-10 rounded-xl border-neutral-200 bg-white"
            />
            <p className="text-xs text-neutral-500">单位分钟，自动刷新所有非禁用账号；留空表示不启用。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-neutral-700">全局代理</label>
            <Input
              value={String(config?.proxy || "")}
              onChange={(event) => {
                setProxy(event.target.value);
                setProxyTestResult(null);
              }}
              placeholder="http://127.0.0.1:7890"
              className="h-10 rounded-xl border-neutral-200 bg-white"
            />
            <p className="text-xs leading-5 text-neutral-500">
              留空表示不使用代理。支持协议://账号:密码@主机:端口，也可直接粘贴代理商的 主机:端口:账号:密码；示例 http://user:pass@127.0.0.1:7890、127.0.0.1:7890:user:pass。账号密码含 @/: 等特殊字符时需 URL 编码。
            </p>
            {proxyTestResult ? (
              <div
                className={`rounded-xl border px-3 py-2 text-xs leading-6 ${
                  proxyTestResult.ok
                    ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                    : "border-rose-200 bg-rose-50 text-rose-800"
                }`}
              >
                {proxyTestResult.ok
                  ? `代理可用：HTTP ${proxyTestResult.status}，用时 ${proxyTestResult.latency_ms} ms`
                  : `代理不可用：${proxyTestResult.error ?? "未知错误"}（用时 ${proxyTestResult.latency_ms} ms）`}
              </div>
            ) : null}
            <div className="flex justify-end">
              <Button
                type="button"
                variant="outline"
                className="h-9 rounded-xl border-neutral-200 bg-white px-4 text-neutral-700"
                onClick={() => void handleTestProxy()}
                disabled={isTestingProxy}
              >
                {isTestingProxy ? <LoaderCircle className="size-4 animate-spin" /> : <PlugZap className="size-4" />}
                测试代理
              </Button>
            </div>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-neutral-700">图片访问地址</label>
            <Input
              value={String(config?.base_url || "")}
              onChange={(event) => setBaseUrl(event.target.value)}
              placeholder="https://example.com"
              className="h-10 rounded-xl border-neutral-200 bg-white"
            />
            <p className="text-xs text-neutral-500">用于生成图片结果的访问前缀地址。</p>
          </div>
          <div className="space-y-2">
            <label className="flex items-center gap-2 text-sm text-neutral-700">
              <CalendarClock className="size-4 text-neutral-500" />
              定时清理周期
            </label>
            <Select
              value={String(config?.image_cleanup_interval_days || 1)}
              onValueChange={setImageCleanupIntervalDays}
            >
              <SelectTrigger className="h-10 w-full rounded-xl border-neutral-200 bg-white">
                <SelectValue placeholder="选择清理周期" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="1">每天</SelectItem>
                <SelectItem value="3">每三天</SelectItem>
                <SelectItem value="5">每五天</SelectItem>
                <SelectItem value="7">每七天</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs leading-5 text-neutral-500">按所选周期执行一次全部图片清理。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-neutral-700">定时清理时间</label>
            <Input
              type="time"
              step={60}
              value={String(config?.image_cleanup_time || "03:00")}
              onChange={(event) => setImageCleanupTime(event.target.value)}
              className="h-10 rounded-xl border-neutral-200 bg-white"
            />
            <p className="text-xs leading-5 text-neutral-500">按北京时间（UTC+8）执行，删除执行日期及之前的全部图片。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-neutral-700">图片轮询超时</label>
            <Input
              value={String(config?.image_poll_timeout_secs || "")}
              onChange={(event) => setImagePollTimeoutSecs(event.target.value)}
              placeholder="120"
              className="h-10 rounded-xl border-neutral-200 bg-white"
            />
            <p className="text-xs text-neutral-500">单位秒，等待上游图片结果的最长时间。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-neutral-700">单账号图片并发</label>
            <Input
              value={String(config?.image_account_concurrency || "")}
              onChange={(event) => setImageAccountConcurrency(event.target.value)}
              placeholder="1"
              className="h-10 rounded-xl border-neutral-200 bg-white"
            />
            <p className="text-xs text-neutral-500">限制每个账号同时处理的图片请求数量，默认 3。</p>
          </div>
          <div className="space-y-2 md:col-span-2">
            <label className="flex items-center gap-2 text-sm text-neutral-700">
              <Ban className="size-4 text-neutral-500" />
              无可用生图额度提示语
            </label>
            <Input
              value={String(config?.image_quota_error_message || "")}
              onChange={(event) => setImageQuotaErrorMessage(event.target.value)}
              placeholder="留空返回 no available image quota"
              className="h-10 rounded-xl border-neutral-200 bg-white"
            />
            <p className="text-xs leading-5 text-neutral-500">
              号池里没有可用的生图额度时，把 no available image quota 换成这里填写的文字，例如「请联系管理员补号」；留空则原样返回 no available image quota。
            </p>
          </div>
          <div className="space-y-2">
            <label className="flex items-center gap-3 rounded-xl border border-neutral-200 bg-white px-4 py-3 text-sm text-neutral-700">
              <Checkbox
                checked={Boolean(config?.auto_remove_invalid_accounts)}
                onCheckedChange={(checked) => setAutoRemoveInvalidAccounts(Boolean(checked))}
              />
              自动移除异常账号
            </label>
            <p className="text-xs text-neutral-500">刷新时检测并移除</p>
          </div>
          <label className="flex items-center gap-3 rounded-xl border border-neutral-200 bg-white px-4 py-3 text-sm text-neutral-700">
            <Checkbox
              checked={Boolean(config?.auto_remove_rate_limited_accounts)}
              onCheckedChange={(checked) => setAutoRemoveRateLimitedAccounts(Boolean(checked))}
            />
            自动移除限流账号
          </label>
          <div className="space-y-3 rounded-xl border border-neutral-200 bg-white px-4 py-3">
            <div>
              <label className="text-sm text-neutral-700">控制台日志级别</label>
              <p className="mt-1 text-xs text-neutral-500">不选择时使用默认 info / warning / error。</p>
            </div>
            <div className="grid grid-cols-2 gap-2">
              {logLevelOptions.map((level) => (
                <label key={level} className="flex items-center gap-2 text-sm capitalize text-neutral-700">
                  <Checkbox
                    checked={Boolean(config?.log_levels?.includes(level))}
                    onCheckedChange={(checked) => setLogLevel(level, Boolean(checked))}
                  />
                  {level}
                </label>
              ))}
            </div>
          </div>
          <div className="space-y-2 md:col-span-2">
            <label className="text-sm text-neutral-700">全局附加指令</label>
            <Textarea
              value={String(config?.global_system_prompt || "")}
              onChange={(event) => setGlobalSystemPrompt(event.target.value)}
              placeholder="例如：先判断用户提示词是否合规；遇到违法、色情、暴力、仇恨等请求时拒绝回答。"
              className="min-h-28 rounded-xl border-neutral-200 bg-white font-mono text-xs shadow-none"
            />
            <p className="text-xs text-neutral-500">每次请求都会作为 system 消息注入，可用于审核用户提示词、避免违规内容、统一约束模型行为或固定角色设定。</p>
          </div>
          <div className="space-y-2 md:col-span-2">
            <label className="text-sm text-neutral-700">敏感词</label>
            <Textarea
              value={(config?.sensitive_words || []).join("\n")}
              onChange={(event) => setSensitiveWordsText(event.target.value)}
              placeholder="一行一个，命中即拒绝"
              className="min-h-28 rounded-xl border-neutral-200 bg-white font-mono text-xs shadow-none"
            />
            <p className="text-xs text-neutral-500">只要用户请求包含任意敏感词，就直接返回拒绝。</p>
          </div>
          <div className="space-y-4 rounded-xl border border-neutral-200 bg-white px-4 py-3 md:col-span-2">
            <label className="flex items-center gap-3 text-sm text-neutral-700">
              <Checkbox
                checked={Boolean(config?.ai_review?.enabled)}
                onCheckedChange={(checked) => setAIReviewField("enabled", Boolean(checked))}
              />
              启用 AI 审核
            </label>
            <p className="text-xs leading-6 text-neutral-500">
              开启后会在请求进入生图账号前先调用审核模型，审核不通过会直接拒绝，减少违规提示词触达账号造成风控或封号的风险。
            </p>
            <div className="grid gap-4 md:grid-cols-3">
              <div className="space-y-2">
                <label className="text-sm text-neutral-700">Base URL</label>
                <Input value={String(config?.ai_review?.base_url || "")} onChange={(event) => setAIReviewField("base_url", event.target.value)} placeholder="https://api.openai.com" className="h-10 rounded-xl border-neutral-200 bg-white" />
              </div>
              <div className="space-y-2">
                <label className="text-sm text-neutral-700">API Key</label>
                <Input value={String(config?.ai_review?.api_key || "")} onChange={(event) => setAIReviewField("api_key", event.target.value)} placeholder="sk-..." className="h-10 rounded-xl border-neutral-200 bg-white" />
              </div>
              <div className="space-y-2">
                <label className="text-sm text-neutral-700">Model</label>
                <Input value={String(config?.ai_review?.model || "")} onChange={(event) => setAIReviewField("model", event.target.value)} placeholder="gpt-5.4-mini" className="h-10 rounded-xl border-neutral-200 bg-white" />
              </div>
            </div>
            <div className="space-y-2">
              <label className="text-sm text-neutral-700">审核提示词</label>
              <Textarea value={String(config?.ai_review?.prompt || "")} onChange={(event) => setAIReviewField("prompt", event.target.value)} placeholder="判断用户请求是否允许。只回答 ALLOW 或 REJECT。" className="min-h-24 rounded-xl border-neutral-200 bg-white text-xs shadow-none" />
            </div>
          </div>
        </div>

      </CardContent>
      <div className="fixed right-4 bottom-4 z-40 md:top-1/2 md:bottom-auto md:-translate-y-1/2">
        <Button
          className="h-12 rounded-2xl bg-neutral-950 px-4 text-white shadow-xl shadow-neutral-950/20 hover:bg-neutral-800 focus-visible:ring-2 focus-visible:ring-neutral-900/30 sm:px-5 dark:bg-neutral-100 dark:text-neutral-900 dark:shadow-black/30 dark:hover:bg-white"
          onClick={() => void saveConfig()}
          disabled={isSavingConfig}
          aria-label="保存基础配置"
          title="保存基础配置"
        >
          {isSavingConfig ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
          保存
        </Button>
      </div>
    </Card>
  );
}
