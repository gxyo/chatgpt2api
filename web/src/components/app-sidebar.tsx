"use client";

import Link from "next/link";
import { useEffect, useLayoutEffect, useState } from "react";
import {
  BarChart3,
  FileWarning,
  Image as ImageIcon,
  Images,
  LogOut,
  Menu,
  ScrollText,
  Settings,
  Sparkles,
  UserCheck,
  UserPlus,
  Users,
  type LucideIcon,
} from "lucide-react";
import { usePathname, useRouter } from "next/navigation";

import { HeaderActions } from "@/components/header-actions";
import { ThemeToggle } from "@/components/theme-toggle";
import { VersionReleaseDialog } from "@/components/version-release-dialog";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";
import { getValidatedAuthSession, peekAuthSession } from "@/lib/auth-session";
import { cn } from "@/lib/utils";
import { clearStoredAuthSession, type StoredAuthSession } from "@/store/auth";

type NavItem = { href: string; label: string; icon: LucideIcon };
type NavGroup = { label?: string; items: NavItem[] };

const adminNavGroups: NavGroup[] = [
  {
    label: "概览",
    items: [{ href: "/stats", label: "统计", icon: BarChart3 }],
  },
  {
    label: "业务",
    items: [
      { href: "/image", label: "生图", icon: ImageIcon },
      { href: "/accounts", label: "号池管理", icon: Users },
      { href: "/register", label: "注册机", icon: UserPlus },
      { href: "/register-stats", label: "注册统计", icon: UserCheck },
    ],
  },
  {
    label: "记录",
    items: [
      { href: "/register-logs", label: "注册日志", icon: FileWarning },
      { href: "/logs", label: "日志管理", icon: ScrollText },
      { href: "/image-manager", label: "图片管理", icon: Images },
    ],
  },
  {
    label: "系统",
    items: [{ href: "/settings", label: "设置", icon: Settings }],
  },
];

const userNavGroups: NavGroup[] = [
  { items: [{ href: "/image", label: "画图", icon: ImageIcon }] },
];

/** 品牌 logo 的落点：固定指向生图，不跟着导航项顺序跑。 */
const BRAND_HREF = "/image";

const brandLinkClass =
  "group inline-flex items-center gap-2.5 text-[15px] font-bold tracking-tight text-foreground transition hover:opacity-80";

/** 品牌标识：品牌蓝实心方块，和按钮、图表是同一套主色。 */
function BrandMark() {
  return (
    <span className="grid size-8 shrink-0 place-items-center rounded-[10px] bg-[#2563eb] text-white shadow-[0_6px_16px_-6px_rgba(37,99,235,0.55)]">
      <Sparkles className="size-4" />
    </span>
  );
}

/** 侧栏顶部品牌行：真实侧栏与首屏占位壳共用。 */
function SidebarBrandRow() {
  return (
    <div className="flex h-16 shrink-0 items-center px-5">
      <Link href={BRAND_HREF} className={brandLinkClass}>
        <BrandMark />
        chatgpt2api
      </Link>
    </div>
  );
}

/** 深色侧栏的外观，抽出来给占位壳复用。 */
const SIDEBAR_SURFACE_CLASS =
  "app-sidebar fixed inset-y-3 left-3 z-40 hidden w-[248px] flex-col overflow-hidden rounded-[26px] border border-white/[0.06] bg-[#0d1526] text-white shadow-[0_30px_80px_-30px_rgba(2,6,23,0.75)] lg:flex dark:bg-[#0a1120]";

/** 导航项是否处于选中态：子路由（如 /image/xxx）也算命中。 */
function isActiveRoute(pathname: string, href: string) {
  return pathname === href || pathname.startsWith(`${href}/`);
}

function useNavSession(pathname: string) {
  const router = useRouter();
  // 水合前后都先渲染空态；绘制前用同步缓存补上身份，整页刷新时侧栏不会迟到。
  const [session, setSession] = useState<StoredAuthSession | null | undefined>(undefined);

  useLayoutEffect(() => {
    const cached = peekAuthSession();
    if (cached) {
      setSession(cached.session);
    }
  }, []);

  useEffect(() => {
    let active = true;

    const load = async () => {
      if (pathname === "/login") {
        if (active) {
          setSession(null);
        }
        return;
      }

      // 页面守卫通常已经校验过并写进了内存缓存，跳转时直接复用，
      // 不再每点一次导航就多打一发 /auth/login。
      const cached = peekAuthSession();
      const storedSession = cached ? cached.session : await getValidatedAuthSession();
      if (active) {
        setSession(storedSession);
      }
    };

    void load();
    return () => {
      active = false;
    };
  }, [pathname]);

  const handleLogout = async () => {
    await clearStoredAuthSession();
    router.replace("/login");
  };

  return { session, handleLogout };
}

function NavList({
  groups,
  pathname,
  onNavigate,
}: {
  groups: NavGroup[];
  pathname: string;
  onNavigate?: () => void;
}) {
  // 点下去的瞬间就把高亮挪过去，不等路由落地 —— 否则这一下"点了没反应"就是卡顿感的来源。
  // 记下点击时的 pathname：它一变说明这一跳已经落地，高亮交还给真实路由状态。
  const [pending, setPending] = useState<{ href: string; from: string } | null>(null);
  if (pending && pending.from !== pathname) {
    setPending(null);
  }

  return (
    <nav className="flex flex-col gap-0.5">
      {groups.map((group, groupIndex) => (
        <div key={group.label ?? groupIndex} className="flex flex-col gap-0.5">
          {group.label ? (
            <div className="px-3 pt-5 pb-1.5 text-[10px] font-semibold tracking-[0.16em] text-muted-foreground/60 uppercase">
              {group.label}
            </div>
          ) : null}
          {group.items.map((item) => {
            const active = pending
              ? pending.href === item.href
              : isActiveRoute(pathname, item.href);
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                onClick={() => {
                  setPending({ href: item.href, from: pathname });
                  onNavigate?.();
                }}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "relative flex items-center gap-3 rounded-xl px-3 py-2.5 text-[13px] transition-colors",
                  active
                    ? "bg-[#2563eb] font-semibold text-white shadow-[0_10px_24px_-12px_rgba(37,99,235,0.6)]"
                    : "font-medium text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                <Icon
                  className={cn(
                    "size-[18px] shrink-0 transition-colors",
                    active ? "text-white" : "text-muted-foreground/80",
                  )}
                />
                <span className="truncate">{item.label}</span>
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

function UserBadge({ session }: { session: StoredAuthSession }) {
  const roleLabel = session.role === "admin" ? "管理员" : "普通用户";
  const displayName = session.name.trim() || roleLabel;
  const initial = displayName.slice(0, 1).toUpperCase();

  return (
    <div className="flex items-center gap-2.5 rounded-xl border border-border/70 bg-white/[0.04] px-2.5 py-2">
      <span className="grid size-8 shrink-0 place-items-center rounded-full bg-[#2563eb] text-[11px] font-semibold text-white">
        {initial}
      </span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[13px] font-semibold text-foreground">{displayName}</div>
        <div className="truncate text-[11px] text-muted-foreground">{roleLabel}</div>
      </div>
    </div>
  );
}

function LogoutButton({ onLogout }: { onLogout: () => void }) {
  return (
    <button
      type="button"
      onClick={onLogout}
      className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs font-medium text-muted-foreground transition hover:bg-muted hover:text-foreground"
    >
      <LogOut className="size-3.5" />
      退出
    </button>
  );
}

/** 桌面端左侧导航栏：深色悬浮面板，固定在一角，内容区靠左内边距让位。 */
export function AppSidebar() {
  const pathname = usePathname();
  const { session, handleLogout } = useNavSession(pathname);

  if (pathname === "/login") {
    return null;
  }

  // 身份还没就绪时先渲染深色空壳：静态导出的 HTML 里就有这块面板，
  // 硬刷新时首帧不会是「一片空白」，水合后再把导航项补进去。
  if (session === undefined) {
    return (
      <aside className={SIDEBAR_SURFACE_CLASS}>
        <SidebarBrandRow />
      </aside>
    );
  }

  if (!session) {
    return null;
  }

  const navGroups = session.role === "admin" ? adminNavGroups : userNavGroups;

  return (
    <aside className={SIDEBAR_SURFACE_CLASS}>
      <SidebarBrandRow />
      <div className="hide-scrollbar min-h-0 flex-1 overflow-y-auto px-3 py-1">
        <NavList groups={navGroups} pathname={pathname} />
      </div>
      <div className="shrink-0 border-t border-border p-3">
        <UserBadge session={session} />
        <div className="mt-2 flex items-center justify-between gap-1 px-1">
          <div className="flex items-center gap-1">
            <ThemeToggle />
            <VersionReleaseDialog />
          </div>
          <LogoutButton onLogout={() => void handleLogout()} />
        </div>
      </div>
    </aside>
  );
}

/** 移动端顶部栏：汉堡菜单打开抽屉式导航，右侧保留主题与版本入口。 */
export function MobileNavBar() {
  const pathname = usePathname();
  const { session, handleLogout } = useNavSession(pathname);
  const [open, setOpen] = useState(false);

  if (pathname === "/login" || session === undefined || !session) {
    return null;
  }

  const navGroups = session.role === "admin" ? adminNavGroups : userNavGroups;

  return (
    <div className="sticky top-0 z-40 flex h-14 shrink-0 items-center gap-2 border-b border-border bg-card/85 px-3 backdrop-blur lg:hidden">
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetTrigger className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition hover:bg-muted hover:text-foreground">
          <Menu className="size-4" />
          <span className="sr-only">打开导航</span>
        </SheetTrigger>
        <SheetContent side="left" className="app-sidebar w-72 gap-0 border-white/[0.06] bg-[#0d1526] p-0 text-white dark:bg-[#0a1120]">
          <div className="flex h-16 shrink-0 items-center px-5">
            <Link href={BRAND_HREF} className={brandLinkClass} onClick={() => setOpen(false)}>
              <BrandMark />
              chatgpt2api
            </Link>
          </div>
          <div className="hide-scrollbar min-h-0 flex-1 overflow-y-auto px-3 py-1">
            <NavList groups={navGroups} pathname={pathname} onNavigate={() => setOpen(false)} />
          </div>
          <div className="shrink-0 border-t border-border p-3">
            <UserBadge session={session} />
            <div className="mt-1 flex items-center justify-between gap-1 px-1">
              <div className="flex items-center gap-1">
                <ThemeToggle />
                <VersionReleaseDialog />
              </div>
              <LogoutButton onLogout={() => void handleLogout()} />
            </div>
          </div>
        </SheetContent>
      </Sheet>
      <Link href={BRAND_HREF} className={brandLinkClass}>
        <BrandMark />
        chatgpt2api
      </Link>
      <HeaderActions className="ml-auto" />
    </div>
  );
}
