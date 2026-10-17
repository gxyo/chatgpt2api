/**
 * 路由内容的布局容器。这里刻意不做整块淡入：站点是客户端软导航，左侧导航保持不动，
 * 只有右侧内容按路由替换——再加一层 key 重挂 + 透明度动画，反而会让内容区先空一帧
 * 再亮起来，看起来像整页刷新。页面自己的加载态由各页面的 loading 分支表达。
 */
export function PageTransition({ children }: { children: React.ReactNode }) {
  return (
    <div className="mx-auto box-border flex w-full max-w-[1520px] flex-1 flex-col gap-2 px-4 py-4 sm:gap-5 sm:px-6 sm:py-5 lg:px-9 lg:py-8">
      {children}
    </div>
  );
}
