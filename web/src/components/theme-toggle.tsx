"use client";

import { AnimatedThemeToggler } from "@/components/ui/animated-theme-toggler";

export function ThemeToggle() {
  return (
    <AnimatedThemeToggler
      aria-label="切换主题"
      title="切换主题"
      variant="circle"
      className="inline-flex size-8 shrink-0 items-center justify-center text-neutral-500 transition hover:text-neutral-900 dark:text-neutral-300 dark:hover:text-white [&_svg]:size-4"
    />
  );
}
