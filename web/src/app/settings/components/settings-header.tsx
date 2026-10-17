"use client";

export function SettingsHeader() {
  return (
    <section className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
      <div className="space-y-1.5">
        <h1 className="text-[26px] leading-tight font-semibold tracking-tight">设置</h1>
        <p className="text-[13px] leading-5 text-muted-foreground">
          维护服务地址、模型定价、用户密钥与接口接入方式。
        </p>
      </div>
    </section>
  );
}
