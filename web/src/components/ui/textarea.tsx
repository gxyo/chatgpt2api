import * as React from "react";

import { cn } from "@/lib/utils";

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "border-input placeholder:text-muted-foreground focus-visible:border-brand/50 focus-visible:ring-brand/15 block min-h-32 w-full rounded-xl border bg-card px-3.5 py-3 text-sm shadow-[0_1px_2px_rgba(15,23,42,0.03)] outline-none focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}

export { Textarea };
