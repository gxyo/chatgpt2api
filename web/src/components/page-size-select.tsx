"use client";

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

type PageSizeSelectProps = {
  value: number;
  options: number[];
  onChange: (value: number) => void;
  /** 单位文案：图片管理用「张」，日志类页面用「条」。 */
  unit?: string;
};

/** 分页器左侧的「每页 N 条/张」选择器，三个列表页共用同一套样式。 */
export function PageSizeSelect({ value, options, onChange, unit = "条" }: PageSizeSelectProps) {
  return (
    <label className="flex items-center gap-2 text-sm text-neutral-500">
      每页
      <Select value={String(value)} onValueChange={(next) => onChange(Number(next))}>
        <SelectTrigger className="h-8 w-[78px] rounded-lg border-neutral-200 bg-white px-2 text-neutral-700">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map((size) => (
            <SelectItem key={size} value={String(size)}>
              {size}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {unit}
    </label>
  );
}
