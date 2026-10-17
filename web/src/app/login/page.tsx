"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { BarChart3, LoaderCircle, ShieldCheck, Sparkles, Zap } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { HeaderActions } from "@/components/header-actions";
import { login } from "@/lib/api";
import { useRedirectIfAuthenticated } from "@/lib/use-auth-guard";
import { getDefaultRouteForRole, setStoredAuthSession } from "@/store/auth";

export default function LoginPage() {
  const router = useRouter();
  const [authKey, setAuthKey] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const { isCheckingAuth } = useRedirectIfAuthenticated();

  const handleLogin = async () => {
    const normalizedAuthKey = authKey.trim();
    if (!normalizedAuthKey) {
      toast.error("请输入 密钥");
      return;
    }

    setIsSubmitting(true);
    try {
      const data = await login(normalizedAuthKey);
      await setStoredAuthSession({
        key: normalizedAuthKey,
        role: data.role,
        subjectId: data.subject_id,
        name: data.name,
      });
      router.replace(getDefaultRouteForRole(data.role));
    } catch (error) {
      const message = error instanceof Error ? error.message : "登录失败";
      toast.error(message);
    } finally {
      setIsSubmitting(false);
    }
  };

  if (isCheckingAuth) {
    return (
      <div className="grid w-full flex-1 place-items-center px-4 py-6">
        <LoaderCircle className="size-5 animate-spin text-neutral-400" />
      </div>
    );
  }

  return (
    <div className="grid w-full flex-1 place-items-center px-4 py-6">
      <HeaderActions className="fixed top-4 right-4 z-10" />
      {/* 左右分栏的登录卡：左边是深色品牌面，右边只放表单，和侧栏是同一套深色语言。 */}
      <div className="grid w-full max-w-[940px] overflow-hidden rounded-[28px] border border-border bg-card shadow-[0_40px_100px_-48px_rgba(15,23,42,0.45)] md:grid-cols-[1.05fr_1fr]">
        <div className="relative hidden flex-col justify-between overflow-hidden bg-[#0d1526] p-9 text-white md:flex dark:bg-[#0a1120]">
          {/* 两团低饱和蓝光，只做背景氛围，不干扰文字。 */}
          <div className="pointer-events-none absolute -top-24 -right-20 size-64 rounded-full bg-brand/30 blur-[90px]" />
          <div className="pointer-events-none absolute -bottom-28 -left-16 size-64 rounded-full bg-[#2563eb]/20 blur-[100px]" />

          <div className="relative flex items-center gap-2.5 text-[15px] font-bold tracking-tight">
            <span className="grid size-8 place-items-center rounded-[10px] bg-[#2563eb] text-white shadow-[0_6px_16px_-6px_rgba(37,99,235,0.55)]">
              <Sparkles className="size-4" />
            </span>
            chatgpt2api
          </div>

          <div className="relative space-y-5">
            <div className="space-y-2.5">
              <h1 className="text-[28px] leading-[1.22] font-semibold tracking-tight">
                号池、注册与生图，
                <br />
                放在一个控制台里。
              </h1>
              <p className="max-w-xs text-[13px] leading-5 text-white/55">
                账号状态、额度消耗与调用明细实时汇总，异常账号一眼可见。
              </p>
            </div>

            <div className="space-y-3 text-sm">
              {[
                { icon: ShieldCheck, title: "账号池实时监控", desc: "状态、额度与恢复时间集中展示" },
                { icon: Zap, title: "批量注册与恢复", desc: "失败步骤与错误链完整留档" },
                { icon: BarChart3, title: "用量与收入统计", desc: "按小时、模型与调用方式拆分" },
              ].map((item) => {
                const Icon = item.icon;
                return (
                  <div key={item.title} className="flex items-start gap-3">
                    <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg bg-white/[0.07] text-white/80">
                      <Icon className="size-3.5" />
                    </span>
                    <div>
                      <div className="text-[13px] font-medium text-white/90">{item.title}</div>
                      <div className="text-[11px] leading-4 text-white/45">{item.desc}</div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="relative mt-5 text-[10px] text-white/30">内部工具 · 仅限授权人员使用</div>
        </div>

        <div className="flex flex-col justify-center gap-7 p-7 sm:p-10">
          <div className="space-y-2">
            <div className="inline-flex items-center gap-1.5 rounded-full bg-brand-soft px-2.5 py-1 text-[11px] font-semibold text-brand md:hidden">
              <Sparkles className="size-3" />
              chatgpt2api
            </div>
            <h2 className="text-[26px] leading-tight font-semibold tracking-tight text-foreground">欢迎回来</h2>
            <p className="text-[13px] leading-6 text-muted-foreground">
              输入密钥后继续使用账号管理和图片生成功能。
            </p>
          </div>

          <div className="space-y-5">
            <div className="space-y-2">
              <label htmlFor="auth-key" className="block text-xs font-medium text-muted-foreground">
                密钥
              </label>
              <Input
                id="auth-key"
                type="password"
                value={authKey}
                onChange={(event) => setAuthKey(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    void handleLogin();
                  }
                }}
                placeholder="请输入密钥"
                className="h-12"
              />
            </div>

            <Button
              className="h-12 w-full rounded-xl"
              onClick={() => void handleLogin()}
              disabled={isSubmitting}
            >
              {isSubmitting ? <LoaderCircle className="size-4 animate-spin" /> : null}
              登录
            </Button>
          </div>

          <p className="text-[11px] leading-5 text-muted-foreground/70">
            密钥由管理员在「设置 · 用户密钥」中维护。
          </p>
        </div>
      </div>
    </div>
  );
}
