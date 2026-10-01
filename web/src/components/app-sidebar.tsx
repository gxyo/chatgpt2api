"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  BarChart3,
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
import { getValidatedAuthSession } from "@/lib/auth-session";
import { cn } from "@/lib/utils";
import { clearStoredAuthSession, type StoredAuthSession } from "@/store/auth";

type NavItem = { href: string; label: string; icon: LucideIcon };

const adminNavItems: NavItem[] = [
  { href: "/image", label: "生图", icon: ImageIcon },
  { href: "/accounts", label: "号池管理", icon: Users },
  { href: "/register", label: "注册机", icon: UserPlus },
  { href: "/register-stats", label: "注册统计", icon: UserCheck },
  { href: "/image-manager", label: "图片管理", icon: Images },
  { href: "/stats", label: "统计", icon: BarChart3 },
  { href: "/logs", label: "日志管理", icon: ScrollText },
  { href: "/settings", label: "设置", icon: Settings },
];

const userNavItems: NavItem[] = [{ href: "/image", label: "画图", icon: ImageIcon }];

const brandLinkClass =
  "group inline-flex items-center gap-2.5 text-[15px] font-bold tracking-tight text-neutral-950 transition hover:opacity-80 dark:text-neutral-50";

/** 品牌标识：蓝→紫渐变方块，和强调色同源。 */
function BrandMark() {
  return (
    <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-gradient-to-br from-[#2a78d6] to-[#7c5cf0] text-white shadow-[0_4px_12px_-4px_rgba(42,120,214,0.6)]">
      <Sparkles className="size-4" />
    </span>
  );
}

/** 导航项是否处于选中态：子路由（如 /image/xxx）也算命中。 */
function isActiveRoute(pathname: string, href: string) {
  return pathname === href || pathname.startsWith(`${href}/`);
}

function useNavSession(pathname: string) {
  const router = useRouter();
  const [session, setSession] = useState<StoredAuthSession | null | undefined>(undefined);

  useEffect(() => {
    let active = true;

    const load = async () => {
      if (pathname === "/login") {
        if (active) {
          setSession(null);
        }
        return;
      }

      const storedSession = await getValidatedAuthSession();
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
  items,
  pathname,
  onNavigate,
}: {
  items: NavItem[];
  pathname: string;
  onNavigate?: () => void;
}) {
  return (
    <nav className="flex flex-col gap-0.5">
      {items.map((item) => {
        const active = isActiveRoute(pathname, item.href);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            aria-current={active ? "page" : undefined}
            className={cn(
              "relative flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
              active
                ? "bg-brand-soft font-semibold text-brand"
                : "font-medium text-neutral-500 hover:bg-neutral-100 hover:text-neutral-950 dark:text-neutral-400 dark:hover:bg-white/8 dark:hover:text-white",
            )}
          >
            {/* 选中态左侧的强调色竖条 */}
            {active ? (
              <span className="absolute top-1/2 -left-1.5 h-5 w-[3px] -translate-y-1/2 rounded-full bg-brand" />
            ) : null}
            <Icon
              className={cn(
                "size-4 shrink-0 transition-colors",
                active ? "text-brand" : "text-neutral-400 dark:text-neutral-500",
              )}
            />
            <span className="truncate">{item.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}

function UserBadge({ session }: { session: StoredAuthSession }) {
  const roleLabel = session.role === "admin" ? "管理员" : "普通用户";
  const displayName = session.name.trim() || roleLabel;
  const initial = displayName.slice(0, 1).toUpperCase();

  return (
    <div className="flex items-center gap-2.5 px-2 py-1.5">
      <span className="grid size-8 shrink-0 place-items-center rounded-full bg-neutral-100 text-xs font-semibold text-neutral-700 dark:bg-white/10 dark:text-neutral-200">
        {initial}
      </span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[13px] font-semibold text-neutral-950 dark:text-neutral-100">{displayName}</div>
        <div className="truncate text-[11px] text-neutral-500 dark:text-neutral-400">{roleLabel}</div>
      </div>
    </div>
  );
}

function LogoutButton({ onLogout }: { onLogout: () => void }) {
  return (
    <button
      type="button"
      onClick={onLogout}
      className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs font-medium text-neutral-500 transition hover:bg-neutral-100 hover:text-neutral-950 dark:text-neutral-400 dark:hover:bg-white/10 dark:hover:text-white"
    >
      <LogOut className="size-3.5" />
      退出
    </button>
  );
}

/** 桌面端左侧导航栏，固定在视口左侧，内容区靠左内边距让位。 */
export function AppSidebar() {
  const pathname = usePathname();
  const { session, handleLogout } = useNavSession(pathname);

  if (pathname === "/login" || session === undefined || !session) {
    return null;
  }

  const navItems = session.role === "admin" ? adminNavItems : userNavItems;

  return (
    <aside className="fixed inset-y-0 left-0 z-40 hidden w-60 flex-col border-r border-neutral-200 bg-white lg:flex dark:border-white/10 dark:bg-neutral-900">
      <div className="flex h-14 shrink-0 items-center px-5">
        <Link href={navItems[0].href} className={brandLinkClass}>
          <BrandMark />
          chatgpt2api
        </Link>
      </div>
      <div className="hide-scrollbar min-h-0 flex-1 overflow-y-auto px-3 py-2">
        <NavList items={navItems} pathname={pathname} />
      </div>
      <div className="shrink-0 border-t border-neutral-200 p-3 dark:border-white/10">
        <UserBadge session={session} />
        <div className="mt-1 flex items-center justify-between gap-1 px-1">
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

  const navItems = session.role === "admin" ? adminNavItems : userNavItems;

  return (
    <div className="sticky top-0 z-40 flex h-14 shrink-0 items-center gap-2 border-b border-neutral-200 bg-white/85 px-3 backdrop-blur lg:hidden dark:border-white/10">
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetTrigger className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-neutral-600 transition hover:bg-neutral-100 hover:text-neutral-950 dark:text-neutral-300 dark:hover:bg-white/10 dark:hover:text-white">
          <Menu className="size-4" />
          <span className="sr-only">打开导航</span>
        </SheetTrigger>
        <SheetContent side="left" className="w-72 gap-0 p-0">
          <div className="flex h-14 shrink-0 items-center px-5">
            <Link href={navItems[0].href} className={brandLinkClass} onClick={() => setOpen(false)}>
              <BrandMark />
              chatgpt2api
            </Link>
          </div>
          <div className="hide-scrollbar min-h-0 flex-1 overflow-y-auto px-3 py-2">
            <NavList items={navItems} pathname={pathname} onNavigate={() => setOpen(false)} />
          </div>
          <div className="shrink-0 border-t border-neutral-200 p-3 dark:border-white/10">
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
      <Link href={navItems[0].href} className={brandLinkClass}>
        <BrandMark />
        chatgpt2api
      </Link>
      <HeaderActions className="ml-auto" />
    </div>
  );
}
