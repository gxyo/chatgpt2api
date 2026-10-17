"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
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
    <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-brand text-white shadow-sm shadow-brand/30">
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
  const [session, setSession] = useState<StoredAuthSession | null | undefined>(
    () => peekAuthSession()?.session,
  );

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
            <div className="px-3 pt-4 pb-1.5 text-[11px] font-medium tracking-wide text-muted-foreground/70">
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
                  "relative flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors",
                  active
                    ? "bg-brand-soft font-semibold text-brand"
                    : "font-medium text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                <Icon
                  className={cn(
                    "size-4 shrink-0 transition-colors",
                    active ? "text-brand" : "text-muted-foreground/70",
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
    <div className="flex items-center gap-2.5 px-2 py-1.5">
      <span className="grid size-8 shrink-0 place-items-center rounded-full bg-brand-soft text-xs font-semibold text-brand">
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

/** 桌面端左侧导航栏，固定在视口左侧，内容区靠左内边距让位。 */
export function AppSidebar() {
  const pathname = usePathname();
  const { session, handleLogout } = useNavSession(pathname);

  if (pathname === "/login" || session === undefined || !session) {
    return null;
  }

  const navGroups = session.role === "admin" ? adminNavGroups : userNavGroups;

  return (
    <aside className="fixed inset-y-0 left-0 z-40 hidden w-60 flex-col border-r border-border bg-sidebar lg:flex">
      <div className="flex h-14 shrink-0 items-center px-5">
        <Link href={BRAND_HREF} className={brandLinkClass}>
          <BrandMark />
          chatgpt2api
        </Link>
      </div>
      <div className="hide-scrollbar min-h-0 flex-1 overflow-y-auto px-3 py-1">
        <NavList groups={navGroups} pathname={pathname} />
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
        <SheetContent side="left" className="w-72 gap-0 p-0">
          <div className="flex h-14 shrink-0 items-center px-5">
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
