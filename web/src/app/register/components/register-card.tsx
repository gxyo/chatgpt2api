"use client";

import { useState, type ReactNode } from "react";
import {
  AlertTriangle,
  LoaderCircle,
  Mail,
  Plus,
  Play,
  RotateCcw,
  Save,
  SlidersHorizontal,
  Square,
  Trash2,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { formatBeijingTimeOfDay } from "@/lib/beijing-time";

import { useSettingsStore } from "../../settings/store";
import { DomainListField } from "./domain-list-field";

/** 表单控件统一尺寸，省得每个字段各写一遍。 */
const FIELD_CLASS = "h-10 rounded-xl border-neutral-200 bg-white";

/** 两栏里的每一块都是独立卡片，靠留白分隔，不再共用一个大边框。 */
const PANEL_CARD_CLASS = "rounded-2xl border-white/80 bg-white/90 shadow-sm";

/** 单个邮箱服务：自带一圈浅底，和相邻服务区分开。
    底色固定写 /70 —— globals.css 的暗色覆盖只认这一档，换别的透明度在暗色下会漏成近白。 */
const PROVIDER_CARD_CLASS = "space-y-4 rounded-xl border border-neutral-200 bg-neutral-50/70 p-4";

/** 面板标题：图标 + 标题 + 说明 + 右侧操作。 */
function PanelHeading({
  icon,
  title,
  description,
  action,
}: {
  icon: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    // 窄屏时操作按钮换到标题下方，避免把说明文字挤成两三行。
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="flex min-w-0 items-start gap-2.5">
        <div className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-lg border border-neutral-200 bg-white shadow-sm">
          {icon}
        </div>
        <div className="min-w-0">
          <h2 className="text-base font-semibold tracking-tight">{title}</h2>
          {description ? (
            <p className="mt-0.5 text-xs text-neutral-500 dark:text-neutral-400">{description}</p>
          ) : null}
        </div>
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

/** 带标签的表单字段。 */
function Field({
  label,
  hint,
  required,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="space-y-2">
      <label className="text-sm text-neutral-700">
        {label}
        {required ? <span className="text-red-400"> *</span> : null}
      </label>
      {children}
      {hint ? <p className="text-xs text-neutral-400">{hint}</p> : null}
    </div>
  );
}

/** 勾选类选项，放在字段网格外侧，省得跟输入框做垂直对齐。 */
function ToggleOption({
  checked,
  onCheckedChange,
  disabled,
  children,
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <label className="flex items-center gap-2.5 text-sm text-neutral-700">
      <Checkbox checked={checked} onCheckedChange={(value) => onCheckedChange(Boolean(value))} disabled={disabled} />
      {children}
    </label>
  );
}

export function RegisterCard() {
  const config = useSettingsStore((state) => state.registerConfig);
  const isLoading = useSettingsStore((state) => state.isLoadingRegister);
  const isSaving = useSettingsStore((state) => state.isSavingRegister);
  const setProxy = useSettingsStore((state) => state.setRegisterProxy);
  const setEngine = useSettingsStore((state) => state.setRegisterEngine);
  const setTotal = useSettingsStore((state) => state.setRegisterTotal);
  const setThreads = useSettingsStore((state) => state.setRegisterThreads);
  const setMode = useSettingsStore((state) => state.setRegisterMode);
  const setTargetQuota = useSettingsStore((state) => state.setRegisterTargetQuota);
  const setTargetAvailable = useSettingsStore((state) => state.setRegisterTargetAvailable);
  const setRefreshBatchSize = useSettingsStore((state) => state.setRegisterRefreshBatchSize);
  const setCheckInterval = useSettingsStore((state) => state.setRegisterCheckInterval);
  const setMailField = useSettingsStore((state) => state.setRegisterMailField);
  const addProvider = useSettingsStore((state) => state.addRegisterProvider);
  const updateProvider = useSettingsStore((state) => state.updateRegisterProvider);
  const deleteProvider = useSettingsStore((state) => state.deleteRegisterProvider);
  const save = useSettingsStore((state) => state.saveRegister);
  const toggle = useSettingsStore((state) => state.toggleRegister);
  const reset = useSettingsStore((state) => state.resetRegister);
  const resetOutlookPool = useSettingsStore((state) => state.resetOutlookPool);

  // 必须声明在下面两个 early return 之前，否则违反 hooks 规则。
  const [activeTab, setActiveTab] = useState<"basic" | "mail">("basic");

  if (isLoading) {
    return (
      <div className="flex items-center justify-center rounded-xl border border-neutral-200 bg-white/80 p-10">
        <LoaderCircle className="size-5 animate-spin text-neutral-400" />
      </div>
    );
  }

  if (!config) return null;

  const stats = config.stats || { success: 0, fail: 0, done: 0, running: 0, threads: config.threads };
  const providers = config.mail.providers || [];
  const logs = config.logs || [];
  const locked = config.enabled;
  const updateProviderType = (index: number, type: string) => {
    updateProvider(index, {
      type,
      enable: true,
      ...(type === "cloudmail_gen" ? { api_base: "", admin_email: "", admin_password: "", domain: [], subdomain: [], email_prefix: "" } : {}),
      ...(type === "cloudflare_temp_email" ? { api_base: "", admin_password: "", domain: [] } : {}),
      ...(type === "tempmail_lol" ? { api_key: "", domain: [] } : {}),
      ...(type === "moemail" ? { api_base: "", api_key: "", domain: [] } : {}),
      ...(type === "inbucket" ? { api_base: "", domain: [], random_subdomain: true } : {}),
      ...(type === "duckmail" ? { api_key: "", default_domain: "duckmail.sbs" } : {}),
      ...(type === "gptmail" ? { api_key: "", default_domain: "" } : {}),
      ...(type === "yyds_mail" ? { api_base: "https://maliapi.215.im/v1", api_key: "", domain: [], subdomain: "", wildcard: false } : {}),
      ...(type === "ddg_mail" ? { ddg_token: "", cf_inbox_jwt: "", cf_domain: [], admin_password: "" } : {}),
      ...(type === "outlook_token" ? { mailboxes: "", mode: "graph", imap_host: "outlook.office365.com", message_limit: 10 } : {}),
    });
  };

  // 两栏各自独立成卡片并各自滚动：左边改配置、右边看运行，互不挤压。
  // 定高 cage 只在 xl 生效——并排才有「一屏内看全」的意义；窄屏让页面自然滚动，免得内容被裁。
  // grid-rows-[minmax(0,1fr)] 是关键：默认行高按内容算，会把 max-h 撑破导致两栏溢出而不是内部滚动。
  return (
    <div className="grid gap-4 xl:max-h-[calc(100dvh-9.5rem)] xl:min-h-[640px] xl:flex-1 xl:grid-cols-2 xl:grid-rows-[minmax(0,1fr)]">
      {/* ── 左栏：注册配置 ───────────────────────────────────────── */}
      <div className="flex flex-col gap-4 xl:min-h-0 xl:overflow-y-auto xl:pb-1">
        {/* 基础配置和邮箱服务本来是上下两张卡，provider 一多左栏必然超出一屏。
            合并成一张卡 + 页内 tab：切换看，两边都不用滚动，顺带省掉一个卡头和一处 gap。 */}
        <Card className={`${PANEL_CARD_CLASS} xl:min-h-0 xl:flex-1`}>
          <CardContent className="flex min-h-0 flex-1 flex-col gap-4 p-5">
            <PanelHeading
              icon={<SlidersHorizontal className="size-4 text-neutral-600" />}
              title="注册配置"
              description="运行中无法修改，需要先停止任务。"
              action={
                <Button
                  className="h-9 rounded-xl bg-neutral-950 px-4 text-white hover:bg-neutral-800"
                  onClick={() => void save()}
                  disabled={isSaving || locked}
                >
                  {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
                  保存配置
                </Button>
              }
            />

            <div className="flex items-start gap-2 rounded-xl border border-sky-200 bg-sky-50 px-3 py-2 text-xs leading-5 text-sky-800">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" />
              <span>如果注册日志出现 Cloudflare 拦截，请更换注册代理或出口 IP 后重试。</span>
            </div>

            <Tabs
              value={activeTab}
              onValueChange={(value) => setActiveTab(value as "basic" | "mail")}
              className="flex min-h-0 flex-1 flex-col gap-4"
            >
              {/* 用默认的分段药丸样式：自带底色和选中阴影，不像 line 变体那样靠
                  after:bottom-[-5px] 画下划线，不用跟边框较劲。 */}
              <TabsList className="w-full">
                <TabsTrigger value="basic">
                  <SlidersHorizontal className="size-3.5" />
                  基础配置
                </TabsTrigger>
                <TabsTrigger value="mail">
                  <Mail className="size-3.5" />
                  邮箱服务
                  <span className="ml-0.5 rounded-md bg-neutral-200/70 px-1.5 text-[11px] font-semibold tabular-nums text-neutral-600 dark:bg-white/10 dark:text-neutral-300">
                    {providers.length}
                  </span>
                </TabsTrigger>
              </TabsList>

              <TabsContent value="basic">
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="注册模式">
                    <Select
                      value={config.mode || "total"}
                      onValueChange={(value) => setMode(value as "total" | "quota" | "available")}
                      disabled={locked}
                    >
                      <SelectTrigger className={FIELD_CLASS}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="total">注册总数</SelectItem>
                        <SelectItem value="quota">号池剩余额度</SelectItem>
                        <SelectItem value="available">可用账号数量</SelectItem>
                      </SelectContent>
                    </Select>
                  </Field>
                  <Field label="注册引擎">
                    <Select
                      value={config.engine || "playwright"}
                      onValueChange={(value) => setEngine(value as "playwright" | "http")}
                      disabled={locked}
                    >
                      <SelectTrigger className={FIELD_CLASS}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="playwright">浏览器注册</SelectItem>
                        <SelectItem value="http">HTTP 注册（已失效）</SelectItem>
                      </SelectContent>
                    </Select>
                  </Field>
                  <Field label="注册总数" hint="仅「注册总数」模式下生效">
                    <Input
                      value={String(config.total)}
                      onChange={(event) => setTotal(event.target.value)}
                      className={FIELD_CLASS}
                      disabled={locked || config.mode !== "total"}
                    />
                  </Field>
                  <Field label="线程数">
                    <Input
                      value={String(config.threads)}
                      onChange={(event) => setThreads(event.target.value)}
                      className={FIELD_CLASS}
                      disabled={locked}
                    />
                  </Field>
                  <div className="sm:col-span-2">
                    <Field label="注册代理">
                      <Input
                        value={config.proxy}
                        onChange={(event) => setProxy(event.target.value)}
                        placeholder="http://127.0.0.1:7890"
                        className={FIELD_CLASS}
                        disabled={locked}
                      />
                    </Field>
                  </div>
                  <Field label="目标剩余额度" hint="仅「号池剩余额度」模式下生效">
                    <Input
                      value={String(config.target_quota || "")}
                      onChange={(event) => setTargetQuota(event.target.value)}
                      className={FIELD_CLASS}
                      disabled={locked || config.mode !== "quota"}
                    />
                  </Field>
                  <Field label="目标可用账号" hint="仅「可用账号数量」模式下生效">
                    <Input
                      value={String(config.target_available || "")}
                      onChange={(event) => setTargetAvailable(event.target.value)}
                      className={FIELD_CLASS}
                      disabled={locked || config.mode !== "available"}
                    />
                  </Field>
                  <Field label="每注册几个后刷新" hint="完成后刷新号池，并按最新缺口继续补号">
                    <Input
                      type="number"
                      min={1}
                      step={1}
                      value={String(config.refresh_batch_size || "")}
                      onChange={(event) => setRefreshBatchSize(event.target.value)}
                      className={`${FIELD_CLASS} tabular-nums`}
                      disabled={locked || config.mode === "total"}
                    />
                  </Field>
                  <Field label="检查间隔（秒）" hint="仅补号模式下生效">
                    <Input
                      value={String(config.check_interval || "")}
                      onChange={(event) => setCheckInterval(event.target.value)}
                      className={FIELD_CLASS}
                      disabled={locked || config.mode === "total"}
                    />
                  </Field>
                </div>
              </TabsContent>

              {/* provider 多到一屏放不下时，滚动发生在这个 tab 内部，不会带动整页。 */}
              <TabsContent value="mail" className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pr-1">
                <div className="grid gap-4 sm:grid-cols-3">
                  <Field label="请求超时">
                    <Input
                      value={String(config.mail.request_timeout || "")}
                      onChange={(event) => setMailField("request_timeout", event.target.value)}
                      className={FIELD_CLASS}
                      disabled={locked}
                    />
                  </Field>
                  <Field label="等待验证码超时">
                    <Input
                      value={String(config.mail.wait_timeout || "")}
                      onChange={(event) => setMailField("wait_timeout", event.target.value)}
                      className={FIELD_CLASS}
                      disabled={locked}
                    />
                  </Field>
                  <Field label="轮询间隔">
                    <Input
                      value={String(config.mail.wait_interval || "")}
                      onChange={(event) => setMailField("wait_interval", event.target.value)}
                      className={FIELD_CLASS}
                      disabled={locked}
                    />
                  </Field>
                </div>

                <div className="flex flex-wrap items-center justify-between gap-3">
                  <span className="text-xs text-neutral-500 dark:text-neutral-400">
                    共 {providers.length} 个邮箱服务，按启用顺序轮换。
                  </span>
                  <Button
                    type="button"
                    variant="outline"
                    className="h-9 rounded-xl border-neutral-200 bg-white px-3 text-neutral-700"
                    onClick={addProvider}
                    disabled={locked}
                  >
                    <Plus className="size-4" />
                    添加
                  </Button>
                </div>

                <div className="space-y-4">
                  {providers.map((provider, index) => {
                    const type = String(provider.type || "tempmail_lol");
                    const domains = Array.isArray(provider.domain) ? provider.domain.map(String) : [];
                    return (
                      <div key={index} className={PROVIDER_CARD_CLASS}>
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <h3 className="text-sm font-semibold text-neutral-800">邮箱服务 {index + 1}</h3>
                          <div className="flex items-center gap-2">
                            <ToggleOption
                              checked={Boolean(provider.enable)}
                              onCheckedChange={(checked) => updateProvider(index, { enable: checked })}
                              disabled={locked}
                            >
                              启用
                            </ToggleOption>
                            <button
                              type="button"
                              className="rounded-lg p-2 text-neutral-400 transition hover:bg-rose-50 hover:text-rose-500 disabled:opacity-50"
                              onClick={() => deleteProvider(index)}
                              disabled={locked || providers.length <= 1}
                              title="删除该邮箱服务"
                            >
                              <Trash2 className="size-4" />
                            </button>
                          </div>
                        </div>

                        <div className="grid gap-4 sm:grid-cols-2">
                          <Field label="类型">
                            <Select
                              value={type}
                              onValueChange={(value) => updateProviderType(index, value)}
                              disabled={locked}
                            >
                              <SelectTrigger className={FIELD_CLASS}>
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="cloudmail_gen">cloudmail_gen</SelectItem>
                                <SelectItem value="cloudflare_temp_email">cloudflare_temp_email</SelectItem>
                                <SelectItem value="tempmail_lol">tempmail_lol</SelectItem>
                                <SelectItem value="moemail">moemail</SelectItem>
                                <SelectItem value="inbucket">inbucket_mail</SelectItem>
                                <SelectItem value="duckmail">duckmail</SelectItem>
                                <SelectItem value="gptmail">gptmail(未测试)</SelectItem>
                                <SelectItem value="yyds_mail">yyds_mail</SelectItem>
                                <SelectItem value="ddg_mail">ddg_mail (DDG邮箱+CF中转)</SelectItem>
                                <SelectItem value="outlook_token">outlook_token (Outlook/Hotmail 邮箱池)</SelectItem>
                              </SelectContent>
                            </Select>
                          </Field>

                          {type === "cloudmail_gen" || type === "cloudflare_temp_email" || type === "moemail" || type === "inbucket" || type === "yyds_mail" || type === "ddg_mail" ? (
                            <>
                              <Field label={type === "cloudmail_gen" ? "CloudMail URL" : "API Base"}>
                                <Input
                                  value={String(provider.api_base || "")}
                                  onChange={(event) => updateProvider(index, { api_base: event.target.value })}
                                  className={FIELD_CLASS}
                                  disabled={locked}
                                />
                              </Field>
                              {type === "cloudmail_gen" ? (
                                <>
                                  <Field label="管理员邮箱">
                                    <Input
                                      value={String(provider.admin_email || "")}
                                      onChange={(event) => updateProvider(index, { admin_email: event.target.value })}
                                      className={FIELD_CLASS}
                                      disabled={locked}
                                    />
                                  </Field>
                                  <Field label="管理员密码">
                                    <Input
                                      value={String(provider.admin_password || "")}
                                      onChange={(event) => updateProvider(index, { admin_password: event.target.value })}
                                      className={FIELD_CLASS}
                                      disabled={locked}
                                    />
                                  </Field>
                                </>
                              ) : null}
                              {type === "cloudflare_temp_email" || type === "ddg_mail" ? (
                                <Field label="Admin Password">
                                  <Input
                                    value={String(provider.admin_password || "")}
                                    onChange={(event) => updateProvider(index, { admin_password: event.target.value })}
                                    className={FIELD_CLASS}
                                    disabled={locked}
                                  />
                                </Field>
                              ) : null}
                            </>
                          ) : null}

                          {type === "ddg_mail" ? (
                            <>
                              <Field label="DDG Token" required>
                                <Input
                                  value={String(provider.ddg_token || "")}
                                  onChange={(event) => updateProvider(index, { ddg_token: event.target.value })}
                                  className={FIELD_CLASS}
                                  disabled={locked}
                                  placeholder="DuckDuckGo Email Protection 的 Bearer Token"
                                />
                              </Field>
                              <Field label="CF Inbox JWT" required>
                                <Input
                                  value={String(provider.cf_inbox_jwt || "")}
                                  onChange={(event) => updateProvider(index, { cf_inbox_jwt: event.target.value })}
                                  className={FIELD_CLASS}
                                  disabled={locked}
                                  placeholder="CF 临时邮箱后端的固定收件箱 JWT（DDG 转发目标）"
                                />
                              </Field>
                              <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 sm:col-span-2">
                                <p className="mb-1 font-medium">使用说明</p>
                                <ol className="list-inside list-decimal space-y-0.5">
                                  <li>
                                    先在{" "}
                                    <a href="https://duckduckgo.com/email/" target="_blank" rel="noreferrer" className="underline">
                                      DuckDuckGo Email Protection
                                    </a>{" "}
                                    登录并设置转发目标为 CF 收件箱地址
                                  </li>
                                  <li>
                                    DDG Token 从浏览器 DevTools → Network → quack.duckduckgo.com 请求中获取{" "}
                                    <code className="rounded bg-amber-100 px-1">Authorization: Bearer</code>
                                  </li>
                                  <li>CF Inbox JWT 从 CF 临时邮箱后端创建固定收件箱后获取</li>
                                  <li>所有 @duck.com 别名收到的邮件会转发到同一个 CF 收件箱，系统按 To: 头自动匹配</li>
                                </ol>
                              </div>
                            </>
                          ) : null}

                          {type === "tempmail_lol" || type === "moemail" || type === "duckmail" || type === "gptmail" || type === "yyds_mail" ? (
                            <Field label="API Key">
                              <Input
                                value={String(provider.api_key || "")}
                                onChange={(event) => updateProvider(index, { api_key: event.target.value })}
                                className={FIELD_CLASS}
                                disabled={locked}
                              />
                            </Field>
                          ) : null}

                          {type === "duckmail" || type === "gptmail" ? (
                            <Field label="Default Domain">
                              <Input
                                value={String(provider.default_domain || "")}
                                onChange={(event) => updateProvider(index, { default_domain: event.target.value })}
                                placeholder={type === "duckmail" ? "duckmail.sbs" : ""}
                                className={FIELD_CLASS}
                                disabled={locked}
                              />
                            </Field>
                          ) : null}

                          {type === "yyds_mail" ? (
                            <Field label="Subdomain">
                              <Input
                                value={String(provider.subdomain || "")}
                                onChange={(event) => updateProvider(index, { subdomain: event.target.value })}
                                className={FIELD_CLASS}
                                disabled={locked}
                              />
                            </Field>
                          ) : null}

                          {type === "outlook_token" ? (
                            <>
                              <Field label="读取方式">
                                <Select
                                  value={String(provider.mode || "graph")}
                                  onValueChange={(value) => updateProvider(index, { mode: value })}
                                  disabled={locked}
                                >
                                  <SelectTrigger className={FIELD_CLASS}>
                                    <SelectValue />
                                  </SelectTrigger>
                                  <SelectContent>
                                    <SelectItem value="graph">Graph API</SelectItem>
                                    <SelectItem value="imap">IMAP (XOAUTH2)</SelectItem>
                                    <SelectItem value="auto">自动 (Graph→IMAP)</SelectItem>
                                  </SelectContent>
                                </Select>
                              </Field>
                              {String(provider.mode || "graph") !== "graph" ? (
                                <Field label="IMAP Host">
                                  <Input
                                    value={String(provider.imap_host || "outlook.office365.com")}
                                    onChange={(event) => updateProvider(index, { imap_host: event.target.value })}
                                    className={FIELD_CLASS}
                                    disabled={locked}
                                  />
                                </Field>
                              ) : null}
                            </>
                          ) : null}
                        </div>

                        {/* 勾选项单独一行，避开与输入框的垂直对齐 */}
                        {type === "inbucket" ? (
                          <ToggleOption
                            checked={Boolean(provider.random_subdomain ?? true)}
                            onCheckedChange={(checked) => updateProvider(index, { random_subdomain: checked })}
                            disabled={locked}
                          >
                            启用随机子域名
                          </ToggleOption>
                        ) : null}

                        {type === "yyds_mail" ? (
                          <ToggleOption
                            checked={Boolean(provider.wildcard)}
                            onCheckedChange={(checked) => updateProvider(index, { wildcard: checked })}
                            disabled={locked}
                          >
                            Wildcard
                          </ToggleOption>
                        ) : null}

                        {type === "cloudmail_gen" || type === "tempmail_lol" || type === "cloudflare_temp_email" || type === "moemail" || type === "inbucket" || type === "yyds_mail" || type === "ddg_mail" ? (
                          <DomainListField
                            label={type === "cloudmail_gen" ? "邮箱域名" : type === "inbucket" ? "基础域名列表" : "Domain"}
                            value={domains}
                            onChange={(next) => updateProvider(index, { domain: next })}
                            placeholder="example.com"
                            disabled={locked}
                            hint={
                              type === "cloudmail_gen"
                                ? "留空则使用服务默认域名"
                                : type === "inbucket"
                                  ? "系统会自动为每个基础域名生成随机子域名"
                                  : "留空则使用服务默认域名"
                            }
                          />
                        ) : null}

                        {type === "cloudmail_gen" ? (
                          <DomainListField
                            label="子域名（支持多个）"
                            value={Array.isArray(provider.subdomain) ? provider.subdomain.map(String) : []}
                            onChange={(next) => updateProvider(index, { subdomain: next })}
                            placeholder="mail"
                            disabled={locked}
                            hint="留空则直接使用主域名"
                          />
                        ) : null}

                        {type === "outlook_token"
                          ? (() => {
                              const mailboxStats = (provider.mailboxes_stats || {}) as Record<string, number>;
                              const savedCount = Number(provider.mailboxes_count || 0);
                              const preview = Array.isArray(provider.mailboxes_preview)
                                ? (provider.mailboxes_preview as string[])
                                : [];
                              const pendingCount = String(provider.mailboxes || "")
                                .split(/\r?\n/)
                                .filter((line) => line.includes("----") && line.split("----").length >= 4).length;
                              return (
                                <div className="space-y-3">
                                  <Field
                                    label="邮箱池导入"
                                    required
                                    hint="每个邮箱仅成功注册一次（状态记录在 data/outlook_token_used.json）。失败的邮箱会被标记原因，可用下方按钮释放后重试。"
                                  >
                                    <Textarea
                                      value={String(provider.mailboxes || "")}
                                      onChange={(event) => updateProvider(index, { mailboxes: event.target.value })}
                                      placeholder={"每行一个邮箱，格式：\n邮箱----密码----client_id----refresh_token\n（出于安全，已保存的密码/refresh_token 不会回显；此处仅用于新增或覆盖）"}
                                      className="min-h-32 rounded-xl border-neutral-200 bg-white font-mono text-xs"
                                      disabled={locked}
                                    />
                                  </Field>
                                  <div className="flex flex-wrap items-center justify-between gap-2">
                                    <div className="flex flex-wrap items-center gap-1.5 text-xs">
                                      <span className="rounded-md bg-neutral-100 px-2 py-1 text-neutral-600">未使用 {mailboxStats.unused ?? 0}</span>
                                      <span className="rounded-md bg-blue-50 px-2 py-1 text-blue-600">占用中 {mailboxStats.in_use ?? 0}</span>
                                      <span className="rounded-md bg-emerald-50 px-2 py-1 text-emerald-700">已用 {mailboxStats.used ?? 0}</span>
                                      <span className="rounded-md bg-amber-50 px-2 py-1 text-amber-700">token失效 {mailboxStats.token_invalid ?? 0}</span>
                                      <span className="rounded-md bg-rose-50 px-2 py-1 text-rose-600">失败 {mailboxStats.failed ?? 0}</span>
                                    </div>
                                    <span className="text-xs text-neutral-400 tabular-nums">
                                      已保存 {savedCount} 个{pendingCount ? ` · 待导入 ${pendingCount} 个` : ""}
                                    </span>
                                  </div>
                                  {preview.length ? (
                                    <p className="text-xs text-neutral-400">
                                      已保存邮箱（脱敏）：{preview.slice(0, 8).join("、")}
                                      {preview.length > 8 ? ` 等 ${preview.length} 个` : ""}
                                    </p>
                                  ) : null}
                                  <div className="flex flex-wrap items-center gap-2">
                                    <Button
                                      type="button"
                                      variant="outline"
                                      className="h-8 rounded-lg border-neutral-200 bg-white px-3 text-xs text-neutral-700"
                                      onClick={() => void resetOutlookPool("failed")}
                                      disabled={locked}
                                    >
                                      清除失败/占用状态
                                    </Button>
                                    <Button
                                      type="button"
                                      variant="outline"
                                      className="h-8 rounded-lg border-amber-200 bg-white px-3 text-xs text-amber-700 hover:bg-amber-50"
                                      onClick={() => {
                                        if (window.confirm("确定要从 Outlook 邮箱池中删除所有未使用邮箱吗？此操作会移除这些已保存凭据。")) {
                                          void resetOutlookPool("unused");
                                        }
                                      }}
                                      disabled={locked}
                                    >
                                      清空未使用
                                    </Button>
                                    <Button
                                      type="button"
                                      variant="outline"
                                      className="h-8 rounded-lg border-rose-200 bg-white px-3 text-xs text-rose-600 hover:bg-rose-50"
                                      onClick={() => {
                                        if (window.confirm("确定要重置整个 Outlook 邮箱池状态吗？所有邮箱会被标记为可重新使用。")) {
                                          void resetOutlookPool("all");
                                        }
                                      }}
                                      disabled={locked}
                                    >
                                      重置全部状态
                                    </Button>
                                  </div>
                                </div>
                              );
                            })()
                          : null}
                      </div>
                    );
                  })}
                </div>
              </TabsContent>
            </Tabs>
          </CardContent>
        </Card>
      </div>

      {/* ── 右栏：运行结果 ───────────────────────────────────────── */}
      <div className="flex flex-col gap-4 xl:min-h-0 xl:overflow-y-auto xl:pb-1">
        <Card className={PANEL_CARD_CLASS}>
          <CardContent className="space-y-4 p-5">
            <PanelHeading
              icon={<Play className="size-4 text-neutral-600" />}
              title="运行结果"
              description="SSE 实时推送当前状态。"
              action={
                <Badge variant={config.enabled ? "success" : "secondary"} className="rounded-md">
                  {config.enabled ? "运行中" : "已停止"}
                </Badge>
              }
            />

            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {[
                ["成功 / 成功率", `${stats.success} / ${stats.success_rate || 0}%`],
                ["失败", stats.fail],
                ["完成", stats.done],
                ["运行 / 线程", `${stats.running} / ${stats.threads}`],
                ["运行时间", `${stats.elapsed_seconds || 0}s`],
                ["平均注册单个", `${stats.avg_seconds || 0}s`],
                ["当前额度", stats.current_quota || 0],
                ["正常账号", stats.current_available || 0],
              ].map(([label, value]) => (
                <div key={label} className="rounded-xl border border-neutral-200 bg-white px-3 py-2">
                  <div className="text-xs text-neutral-400">{label}</div>
                  <div className="mt-1 text-sm font-semibold text-neutral-800 tabular-nums">{value}</div>
                </div>
              ))}
            </div>

            <div className="grid grid-cols-3 gap-2">
              <Button className="h-9 rounded-xl bg-neutral-950 px-3 text-white hover:bg-neutral-800" onClick={() => void toggle()} disabled={isSaving}>
                {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : config.enabled ? <Square className="size-4" /> : <Play className="size-4" />}
                {config.enabled ? "停止" : "启动"}
              </Button>
              <Button
                variant="outline"
                className="h-9 rounded-xl border-neutral-200 bg-white px-3 text-neutral-700"
                onClick={() => void reset()}
                disabled={isSaving || config.enabled}
              >
                <RotateCcw className="size-4" />
                重置
              </Button>
              <Button
                variant="outline"
                className="h-9 rounded-xl border-neutral-200 bg-white px-3 text-neutral-700"
                onClick={() => void save()}
                disabled={isSaving || config.enabled}
              >
                <Save className="size-4" />
                保存
              </Button>
            </div>

            <div className="flex items-center gap-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              <AlertTriangle className="size-4 shrink-0" />
              启动之前注意先保存配置。
            </div>
          </CardContent>
        </Card>


        <Card className={`${PANEL_CARD_CLASS} xl:min-h-[260px] xl:flex-1`}>
          <CardContent className="flex min-h-0 flex-1 flex-col space-y-3 p-5">
            <PanelHeading
              icon={<AlertTriangle className="size-4 text-amber-600" />}
              title="实时日志"
              description="遇到 HTTP 状态码 400 等错误，基本是邮箱滥用被封，需要更换新的域名邮箱。"
              action={
                <Badge variant="secondary" className="rounded-md tabular-nums">
                  {logs.length}
                </Badge>
              }
            />
            {/* overflow-y-auto 会把 overflow-x 的计算值也变成 auto，日志里的 URL/token 这类
                超长不可断词就会顶出一条横向滚动条；这里显式盖掉，并让长词换行而不是被裁掉。 */}
            <div className="h-[320px] overflow-x-hidden overflow-y-auto rounded-xl border border-neutral-200 bg-white/70 p-3 font-mono text-xs leading-6 xl:h-auto xl:min-h-[200px] xl:flex-1">
              {logs.length === 0 ? (
                <div className="text-neutral-500">暂无日志</div>
              ) : (
                logs.slice().reverse().map((item, index) => (
                  <div
                    key={`${item.time}-${index}`}
                    className={
                      item.level === "red"
                        ? "text-rose-600"
                        : item.level === "green"
                          ? "text-emerald-700"
                          : item.level === "yellow"
                            ? "text-amber-700"
                            : "text-neutral-700"
                    }
                  >
                    <span className="text-neutral-400">{formatBeijingTimeOfDay(item.time)}</span>
                    <span className="break-words pl-2">{item.text}</span>
                  </div>
                ))
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
