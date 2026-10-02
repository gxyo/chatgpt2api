"use client";

import { create } from "zustand";
import { toast } from "sonner";

import {
  fetchRegisterConfig,
  resetRegister as resetRegisterApi,
  resetOutlookPool as resetOutlookPoolApi,
  fetchSettingsConfig,
  startRegister,
  stopRegister,
  updateRegisterConfig,
  updateSettingsConfig,
  type RegisterConfig,
  type SettingsConfig,
} from "@/lib/api";

function normalizeOptionalInterval(value: unknown, defaultValue: number | null = null): number | null {
  if (value === undefined) {
    return defaultValue;
  }
  if (value === null || String(value).trim() === "") {
    return null;
  }
  const interval = Number(value);
  if (!Number.isFinite(interval) || interval <= 0) {
    return null;
  }
  return Math.floor(interval);
}

function normalizeConfig(config: SettingsConfig): SettingsConfig {
  return {
    ...config,
    refresh_account_interval_minute: normalizeOptionalInterval(config.refresh_account_interval_minute, 5),
    refresh_all_accounts_interval_minute: normalizeOptionalInterval(config.refresh_all_accounts_interval_minute),
    image_retention_days: Number(config.image_retention_days || 30),
    image_cleanup_interval_days: [1, 3, 5, 7].includes(Number(config.image_cleanup_interval_days))
      ? Number(config.image_cleanup_interval_days)
      : 1,
    image_cleanup_time: /^([01]\d|2[0-3]):[0-5]\d$/.test(String(config.image_cleanup_time || ""))
      ? String(config.image_cleanup_time)
      : "03:00",
    image_poll_timeout_secs: Number(config.image_poll_timeout_secs || 75),
    image_account_concurrency: Number(config.image_account_concurrency || 3),
    image_upscale_enabled: Boolean(config.image_upscale_enabled),
    image_quota_error_message: String(config.image_quota_error_message || ""),
    auto_remove_invalid_accounts: Boolean(config.auto_remove_invalid_accounts),
    auto_remove_rate_limited_accounts: Boolean(config.auto_remove_rate_limited_accounts),
    log_levels: Array.isArray(config.log_levels) ? config.log_levels : [],
    proxy: typeof config.proxy === "string" ? config.proxy : "",
    base_url: typeof config.base_url === "string" ? config.base_url : "",
    global_system_prompt: String(config.global_system_prompt || ""),
    sensitive_words: Array.isArray(config.sensitive_words) ? config.sensitive_words : [],
    ai_review: {
      enabled: Boolean(config.ai_review?.enabled),
      base_url: String(config.ai_review?.base_url || ""),
      api_key: String(config.ai_review?.api_key || ""),
      model: String(config.ai_review?.model || ""),
      prompt: String(config.ai_review?.prompt || ""),
    },
  };
}

type SettingsStore = {
  config: SettingsConfig | null;
  isLoadingConfig: boolean;
  isSavingConfig: boolean;

  registerConfig: RegisterConfig | null;
  isLoadingRegister: boolean;
  isSavingRegister: boolean;

  initialize: () => Promise<void>;
  loadConfig: () => Promise<void>;
  saveConfig: () => Promise<boolean>;
  setRefreshAccountIntervalMinute: (value: string) => void;
  setRefreshAllAccountsIntervalMinute: (value: string) => void;
  setImageCleanupIntervalDays: (value: string) => void;
  setImageCleanupTime: (value: string) => void;
  setImagePollTimeoutSecs: (value: string) => void;
  setImageAccountConcurrency: (value: string) => void;
  setImageUpscaleEnabled: (value: boolean) => void;
  setImageQuotaErrorMessage: (value: string) => void;
  setAutoRemoveInvalidAccounts: (value: boolean) => void;
  setAutoRemoveRateLimitedAccounts: (value: boolean) => void;
  setLogLevel: (level: string, enabled: boolean) => void;
  setProxy: (value: string) => void;
  setBaseUrl: (value: string) => void;
  setGlobalSystemPrompt: (value: string) => void;
  setSensitiveWordsText: (value: string) => void;
  setAIReviewField: (key: "enabled" | "base_url" | "api_key" | "model" | "prompt", value: string | boolean) => void;

  loadRegister: (silent?: boolean) => Promise<void>;
  updateRegisterRuntime: (config: RegisterConfig) => void;
  setRegisterProxy: (value: string) => void;
  setRegisterEngine: (value: "playwright" | "http") => void;
  setRegisterTotal: (value: string) => void;
  setRegisterThreads: (value: string) => void;
  setRegisterMode: (value: "total" | "quota" | "available") => void;
  setRegisterTargetQuota: (value: string) => void;
  setRegisterTargetAvailable: (value: string) => void;
  setRegisterRefreshBatchSize: (value: string) => void;
  setRegisterCheckInterval: (value: string) => void;
  setRegisterMailField: (key: "request_timeout" | "wait_timeout" | "wait_interval", value: string) => void;
  addRegisterProvider: () => void;
  updateRegisterProvider: (index: number, updates: Record<string, unknown>) => void;
  deleteRegisterProvider: (index: number) => void;
  saveRegister: () => Promise<void>;
  toggleRegister: () => Promise<void>;
  resetRegister: () => Promise<void>;
  resetOutlookPool: (scope: "all" | "failed" | "unused") => Promise<void>;
};

export const useSettingsStore = create<SettingsStore>((set, get) => ({
  config: null,
  isLoadingConfig: true,
  isSavingConfig: false,

  registerConfig: null,
  isLoadingRegister: true,
  isSavingRegister: false,

  initialize: async () => {
    await get().loadConfig();
  },

  loadConfig: async () => {
    set({ isLoadingConfig: true });
    try {
      const data = await fetchSettingsConfig();
      const normalized = normalizeConfig(data.config);
      set({
        config: normalized,
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载系统配置失败");
    } finally {
      set({ isLoadingConfig: false });
    }
  },

  saveConfig: async () => {
    const { config } = get();
    if (!config) {
      return false;
    }

    set({ isSavingConfig: true });
    try {
      const data = await updateSettingsConfig({
        ...config,
        refresh_account_interval_minute: normalizeOptionalInterval(config.refresh_account_interval_minute, null),
        refresh_all_accounts_interval_minute: normalizeOptionalInterval(config.refresh_all_accounts_interval_minute, null),
        image_retention_days: Math.max(1, Number(config.image_retention_days) || 30),
        image_cleanup_interval_days: [1, 3, 5, 7].includes(Number(config.image_cleanup_interval_days))
          ? Number(config.image_cleanup_interval_days)
          : 1,
        image_cleanup_time: /^([01]\d|2[0-3]):[0-5]\d$/.test(String(config.image_cleanup_time || ""))
          ? String(config.image_cleanup_time)
          : "03:00",
        image_poll_timeout_secs: Math.max(1, Number(config.image_poll_timeout_secs) || 75),
        image_account_concurrency: Math.max(1, Number(config.image_account_concurrency) || 3),
        image_upscale_enabled: Boolean(config.image_upscale_enabled),
        image_quota_error_message: String(config.image_quota_error_message || "").trim(),
        auto_remove_invalid_accounts: Boolean(config.auto_remove_invalid_accounts),
        auto_remove_rate_limited_accounts: Boolean(config.auto_remove_rate_limited_accounts),
        proxy: config.proxy.trim(),
        base_url: String(config.base_url || "").trim(),
        global_system_prompt: String(config.global_system_prompt || "").trim(),
        sensitive_words: (config.sensitive_words || []).map((item) => String(item).trim()).filter(Boolean),
        ai_review: {
          enabled: Boolean(config.ai_review?.enabled),
          base_url: String(config.ai_review?.base_url || "").trim(),
          api_key: String(config.ai_review?.api_key || "").trim(),
          model: String(config.ai_review?.model || "").trim(),
          prompt: String(config.ai_review?.prompt || "").trim(),
        },
      });
      set({
        config: normalizeConfig(data.config),
      });
      toast.success("配置已保存");
      return true;
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存系统配置失败");
      return false;
    } finally {
      set({ isSavingConfig: false });
    }
  },

  setRefreshAccountIntervalMinute: (value) => {
    set((state) => {
      if (!state.config) {
        return {};
      }
      return {
        config: {
          ...state.config,
          refresh_account_interval_minute: value,
        },
      };
    });
  },

  setRefreshAllAccountsIntervalMinute: (value) => {
    set((state) => {
      if (!state.config) {
        return {};
      }
      return {
        config: {
          ...state.config,
          refresh_all_accounts_interval_minute: value,
        },
      };
    });
  },

  setImageCleanupIntervalDays: (value) => {
    set((state) => state.config ? { config: { ...state.config, image_cleanup_interval_days: Number(value) } } : {});
  },

  setImageCleanupTime: (value) => {
    set((state) => state.config ? { config: { ...state.config, image_cleanup_time: value } } : {});
  },

  setImagePollTimeoutSecs: (value) => {
    set((state) => state.config ? { config: { ...state.config, image_poll_timeout_secs: value } } : {});
  },

  setImageAccountConcurrency: (value) => {
    set((state) => state.config ? { config: { ...state.config, image_account_concurrency: value } } : {});
  },

  setImageUpscaleEnabled: (value) => {
    set((state) => state.config ? { config: { ...state.config, image_upscale_enabled: value } } : {});
  },

  setImageQuotaErrorMessage: (value) => {
    set((state) => state.config ? { config: { ...state.config, image_quota_error_message: value } } : {});
  },

  setAutoRemoveInvalidAccounts: (value) => {
    set((state) => state.config ? { config: { ...state.config, auto_remove_invalid_accounts: value } } : {});
  },

  setAutoRemoveRateLimitedAccounts: (value) => {
    set((state) => state.config ? { config: { ...state.config, auto_remove_rate_limited_accounts: value } } : {});
  },

  setLogLevel: (level, enabled) => {
    set((state) => {
      if (!state.config) return {};
      const levels = new Set(state.config.log_levels || []);
      if (enabled) levels.add(level);
      else levels.delete(level);
      return { config: { ...state.config, log_levels: Array.from(levels) } };
    });
  },

  setProxy: (value) => {
    set((state) => {
      if (!state.config) {
        return {};
      }
      return {
        config: {
          ...state.config,
          proxy: value,
        },
      };
    });
  },

  setBaseUrl: (value) => {
    set((state) => {
      if (!state.config) {
        return {};
      }
      return {
        config: {
          ...state.config,
          base_url: value,
        },
      };
    });
  },

  setGlobalSystemPrompt: (value) => {
    set((state) => state.config ? { config: { ...state.config, global_system_prompt: value } } : {});
  },

  setSensitiveWordsText: (value) => {
    set((state) => state.config ? { config: { ...state.config, sensitive_words: value.split("\n") } } : {});
  },

  setAIReviewField: (key, value) => {
    set((state) => state.config ? { config: { ...state.config, ai_review: { ...(state.config.ai_review || {}), [key]: value } } } : {});
  },

  loadRegister: async (silent = false) => {
    if (!silent) set({ isLoadingRegister: true });
    try {
      const data = await fetchRegisterConfig();
      set({ registerConfig: data.register });
    } catch (error) {
      if (!silent) toast.error(error instanceof Error ? error.message : "加载注册配置失败");
    } finally {
      if (!silent) set({ isLoadingRegister: false });
    }
  },

  updateRegisterRuntime: (config) => {
    set((state) => ({
      registerConfig: state.registerConfig
        ? {
            ...state.registerConfig,
            enabled: config.enabled,
            stats: config.stats,
            logs: config.logs,
            cloudflare_domain_stats: config.cloudflare_domain_stats,
          }
        : config,
      isLoadingRegister: false,
    }));
  },

  setRegisterProxy: (value) => {
    set((state) => state.registerConfig ? { registerConfig: { ...state.registerConfig, proxy: value } } : {});
  },

  setRegisterEngine: (value) => {
    set((state) => state.registerConfig ? { registerConfig: { ...state.registerConfig, engine: value } } : {});
  },

  setRegisterTotal: (value) => {
    set((state) => state.registerConfig ? { registerConfig: { ...state.registerConfig, total: Number(value) || 0 } } : {});
  },

  setRegisterThreads: (value) => {
    set((state) => state.registerConfig ? { registerConfig: { ...state.registerConfig, threads: Number(value) || 0 } } : {});
  },

  setRegisterMode: (value) => {
    set((state) => state.registerConfig ? { registerConfig: { ...state.registerConfig, mode: value } } : {});
  },

  setRegisterTargetQuota: (value) => {
    set((state) => state.registerConfig ? { registerConfig: { ...state.registerConfig, target_quota: Number(value) || 0 } } : {});
  },

  setRegisterTargetAvailable: (value) => {
    set((state) => state.registerConfig ? { registerConfig: { ...state.registerConfig, target_available: Number(value) || 0 } } : {});
  },

  setRegisterRefreshBatchSize: (value) => {
    set((state) => state.registerConfig ? { registerConfig: { ...state.registerConfig, refresh_batch_size: Number(value) || 0 } } : {});
  },

  setRegisterCheckInterval: (value) => {
    set((state) => state.registerConfig ? { registerConfig: { ...state.registerConfig, check_interval: Number(value) || 0 } } : {});
  },

  setRegisterMailField: (key, value) => {
    set((state) => state.registerConfig ? {
      registerConfig: {
        ...state.registerConfig,
        mail: { ...state.registerConfig.mail, [key]: Number(value) || 0 },
      },
    } : {});
  },

  addRegisterProvider: () => {
    set((state) => state.registerConfig ? {
      registerConfig: {
        ...state.registerConfig,
        mail: {
          ...state.registerConfig.mail,
          providers: [
            ...(state.registerConfig.mail.providers || []),
            { enable: true, type: "cloudmail_gen", api_base: "", admin_email: "", admin_password: "", domain: [], subdomain: [], email_prefix: "" },
          ],
        },
      },
    } : {});
  },

  updateRegisterProvider: (index, updates) => {
    set((state) => {
      if (!state.registerConfig) return {};
      const providers = [...(state.registerConfig.mail.providers || [])];
      providers[index] = { ...(providers[index] || {}), ...updates };
      return { registerConfig: { ...state.registerConfig, mail: { ...state.registerConfig.mail, providers } } };
    });
  },

  deleteRegisterProvider: (index) => {
    set((state) => state.registerConfig ? {
      registerConfig: {
        ...state.registerConfig,
        mail: {
          ...state.registerConfig.mail,
          providers: (state.registerConfig.mail.providers || []).filter((_, itemIndex) => itemIndex !== index),
        },
      },
    } : {});
  },

  saveRegister: async () => {
    const { registerConfig } = get();
    if (!registerConfig) return;
    try {
      set({ isSavingRegister: true });
      const data = await updateRegisterConfig({
        mail: registerConfig.mail,
        proxy: registerConfig.proxy.trim(),
        engine: registerConfig.engine || "playwright",
        total: Math.max(1, Number(registerConfig.total) || 1),
        threads: Math.max(1, Number(registerConfig.threads) || 1),
        mode: registerConfig.mode,
        target_quota: Math.max(1, Number(registerConfig.target_quota) || 1),
        target_available: Math.max(1, Number(registerConfig.target_available) || 1),
        refresh_batch_size: Math.max(1, Number(registerConfig.refresh_batch_size) || 5),
        check_interval: Math.max(1, Number(registerConfig.check_interval) || 5),
      });
      set({ registerConfig: data.register });
      toast.success("注册配置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存注册配置失败");
    } finally {
      set({ isSavingRegister: false });
    }
  },

  toggleRegister: async () => {
    const { registerConfig } = get();
    if (!registerConfig) return;
    set({ isSavingRegister: true });
    try {
      if (!registerConfig.enabled) {
        await updateRegisterConfig({
          mail: registerConfig.mail,
          proxy: registerConfig.proxy.trim(),
          engine: registerConfig.engine || "playwright",
          total: Math.max(1, Number(registerConfig.total) || 1),
          threads: Math.max(1, Number(registerConfig.threads) || 1),
          mode: registerConfig.mode,
          target_quota: Math.max(1, Number(registerConfig.target_quota) || 1),
          target_available: Math.max(1, Number(registerConfig.target_available) || 1),
          refresh_batch_size: Math.max(1, Number(registerConfig.refresh_batch_size) || 5),
          check_interval: Math.max(1, Number(registerConfig.check_interval) || 5),
        });
      }
      const data = registerConfig.enabled ? await stopRegister() : await startRegister();
      set({ registerConfig: data.register });
      toast.success(registerConfig.enabled ? "注册任务已停止" : "注册任务已启动");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "切换注册状态失败");
    } finally {
      set({ isSavingRegister: false });
    }
  },

  resetRegister: async () => {
    set({ isSavingRegister: true });
    try {
      const data = await resetRegisterApi();
      set({ registerConfig: data.register });
      toast.success("注册统计已重置");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重置注册统计失败");
    } finally {
      set({ isSavingRegister: false });
    }
  },

  resetOutlookPool: async (scope) => {
    set({ isSavingRegister: true });
    try {
      const data = await resetOutlookPoolApi(scope);
      set({ registerConfig: data.register });
      toast.success(scope === "unused" ? "已清空未使用邮箱" : scope === "failed" ? "已清除失败/占用的邮箱状态" : "Outlook 邮箱池状态已全部重置");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重置邮箱池状态失败");
    } finally {
      set({ isSavingRegister: false });
    }
  },

}));
