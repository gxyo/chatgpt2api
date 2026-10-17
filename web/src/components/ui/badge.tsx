import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center whitespace-nowrap rounded-full border border-transparent px-2.5 py-0.5 text-xs font-medium transition-colors",
  {
    variants: {
      variant: {
        // 参考稿的标签是一层浅色底、无边框；默认标签走品牌蓝的浅底
        default: "bg-brand-soft text-brand",
        secondary: "bg-secondary text-secondary-foreground",
        outline: "border-border bg-card text-muted-foreground",
        success:
          "bg-emerald-50 text-emerald-700 dark:bg-emerald-400/12 dark:text-emerald-300",
        warning:
          "bg-amber-50 text-amber-700 dark:bg-amber-400/12 dark:text-amber-300",
        danger:
          "bg-rose-50 text-rose-600 dark:bg-rose-400/12 dark:text-rose-300",
        info: "bg-sky-50 text-sky-700 dark:bg-sky-400/12 dark:text-sky-300",
        violet:
          "bg-violet-50 text-violet-700 dark:bg-violet-400/12 dark:text-violet-300",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

function Badge({
  className,
  variant,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return (
    <span className={cn(badgeVariants({ variant }), className)} {...props} />
  );
}

export { Badge, badgeVariants };
