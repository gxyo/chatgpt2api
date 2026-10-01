"use client";

import { Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type DomainListFieldProps = {
  label: string;
  /** 每一项对应一个独立输入框。空数组会渲染成一个空框，表示「未设置」。 */
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  hint?: string;
  disabled?: boolean;
  required?: boolean;
};

/**
 * 域名列表输入：一个域名一个输入框，可单独增删。
 * 原来是一个多行文本框靠换行分隔，域名一多就难定位、难改单个值、也看不出改的是哪个。
 */
export function DomainListField({
  label,
  value,
  onChange,
  placeholder,
  hint,
  disabled,
  required,
}: DomainListFieldProps) {
  // 空列表也要留一个空框，否则删到 0 行后就没地方输入了。
  const rows = value.length > 0 ? value : [""];

  const replace = (index: number, next: string) => {
    const updated = [...rows];
    updated[index] = next;
    onChange(updated);
  };

  const remove = (index: number) => {
    onChange(rows.filter((_, itemIndex) => itemIndex !== index));
  };

  const append = () => onChange([...rows, ""]);

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <label className="text-sm text-neutral-700">
          {label}
          {required ? <span className="text-red-400"> *</span> : null}
        </label>
        {rows.length > 1 ? (
          <span className="text-xs text-neutral-400 tabular-nums">共 {rows.length} 个</span>
        ) : null}
      </div>

      <div className="space-y-2">
        {rows.map((item, index) => (
          <div key={index} className="flex items-center gap-2">
            <Input
              value={item}
              onChange={(event) => replace(index, event.target.value)}
              onKeyDown={(event) => {
                // 在最后一个框里回车直接续一行，连续录入时不用去点按钮。
                if (event.key === "Enter") {
                  event.preventDefault();
                  if (index === rows.length - 1) {
                    append();
                  }
                }
              }}
              placeholder={placeholder}
              className="h-10 rounded-xl border-neutral-200 bg-white font-mono"
              disabled={disabled}
            />
            <button
              type="button"
              onClick={() => remove(index)}
              disabled={disabled || rows.length <= 1}
              title="删除该条目"
              className="shrink-0 rounded-lg p-2 text-neutral-400 transition hover:bg-rose-50 hover:text-rose-500 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-neutral-400"
            >
              <Trash2 className="size-4" />
            </button>
          </div>
        ))}
      </div>

      <Button
        type="button"
        variant="outline"
        className="h-9 rounded-xl border-neutral-200 bg-white px-3 text-neutral-700"
        onClick={append}
        disabled={disabled}
      >
        <Plus className="size-4" />
        添加
      </Button>

      {hint ? <p className="text-xs text-neutral-400">{hint}</p> : null}
    </div>
  );
}
