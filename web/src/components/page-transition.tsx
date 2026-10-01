"use client";

import { usePathname } from "next/navigation";

/**
 * 路由内容的入场动画。key 跟着 pathname 走，切换路由时整块内容重新挂载、
 * 动画重放一次——跳转看起来是"淡入进来"，而不是先僵一下内容才出现。
 * 页面里真正的数据加载各自用 loading 态表达，不靠这里拦截。
 */
export function PageTransition({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <div
      key={pathname}
      className="animate-page-enter mx-auto box-border flex w-full max-w-[1440px] flex-1 flex-col gap-2 px-4 py-4 sm:gap-5 sm:px-6 sm:py-5 lg:px-8 lg:py-6"
    >
      {children}
    </div>
  );
}
