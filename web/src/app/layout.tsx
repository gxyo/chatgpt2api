import type { Metadata, Viewport } from "next";
import { Toaster } from "sonner";
import "./globals.css";
import { AppSidebar, MobileNavBar } from "@/components/app-sidebar";
import { ThemeScript } from "@/components/theme-script";

export const metadata: Metadata = {
  title: "ChatGPT 号池管理",
  description: "ChatGPT account pool management dashboard",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  userScalable: false,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#0a0a0a" },
  ],
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <ThemeScript />
      </head>
      <body
        className="antialiased"
        style={{
          fontFamily:
            '"SF Pro Display","SF Pro Text","PingFang SC","Microsoft YaHei","Helvetica Neue",sans-serif',
        }}
      >
        <Toaster position="top-center" richColors offset={48} />
        <div className="flex min-h-screen flex-col bg-background text-foreground transition-colors duration-300">
          <AppSidebar />
          <MobileNavBar />
          {/* 外层 min-h-screen + flex-1 传递高度：main 与内容列都铺满剩余视口，
              页面里需要整屏的区块（生图 / 注册机）直接 flex-1 即可，无需各自算 dvh。
              gap 保留页面内多段内容（标题区 + 卡片区）之间的原有间距。 */}
          <main className="flex min-w-0 flex-1 flex-col lg:pl-60">
            <div className="mx-auto box-border flex w-full max-w-[1440px] flex-1 flex-col gap-2 px-4 py-4 sm:gap-5 sm:px-6 sm:py-5 lg:px-8 lg:py-6">
              {children}
            </div>
          </main>
        </div>
      </body>
    </html>
  );
}
