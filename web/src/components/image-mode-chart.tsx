"use client";

import { Fragment } from "react";

import type { ImageStatsBreakdown } from "@/lib/api";
import { cn } from "@/lib/utils";

type ImageModeChartProps = {
  modes: ImageStatsBreakdown[];
};

// 调用方式之间是「身份」而非「量级」，所以用分类色，按固定的 mode -> 槽位映射，不随排序变色。
// 这两个槽位（黄 / 品红）是在本页已经占用蓝（请求数、成功）的前提下，拿校验器对着卡片真实
// 表面（亮 #ffffff / 暗 #1c1917）跑出来的：两套主题都过 CVD 与常色觉分离度门槛。亮色下
// 两者对比度低于 3:1，按 relief 规则靠直标兜底 —— 每段旁边都写着名称与百分比，颜色只是辅助。
const MODE_BAR: Record<string, string> = {
  generate: "bg-[#eda100] dark:bg-[#c98500]",
  edit: "bg-[#e87ba4] dark:bg-[#d55181]",
};
// 认不出的方式走中性灰：不占分类槽位（后端目前只会给出这两种），也不会伪装成一个序列。
const MODE_FALLBACK_BAR = "bg-neutral-400 dark:bg-neutral-500";

// 成功率是「一个比值对一个上限」，画成 meter：填充是蓝，轨道是同一色阶更浅的一档，
// 这样整条都在讲同一件事，不是两个独立序列。
const RATE_FILL = "bg-[#2a78d6] dark:bg-[#3987e5]";
const RATE_TRACK = "bg-[#cde2fb] dark:bg-[#184f95]/40";

// 页面是静态导出，格式化必须与运行环境无关，否则水合时数字会对不上。
const NUMBER_FORMAT = new Intl.NumberFormat("en-US");

function formatCount(value: number) {
  return NUMBER_FORMAT.format(value);
}

function formatRate(value: number) {
  return `${(value * 100).toFixed(1)}%`;
}

function barClass(mode: string) {
  return MODE_BAR[mode] ?? MODE_FALLBACK_BAR;
}

/**
 * 「按调用方式」的对比视图：上面一条占比条给整体印象，下面一张表给准确数字。
 * 表本身就是占比条的 table view，所以颜色从来不是唯一的信息通道。
 */
export function ImageModeChart({ modes }: ImageModeChartProps) {
  const total = modes.reduce((sum, item) => sum + item.images, 0);
  // 占比条只画有成功图片的方式：0 张的方式在表里保留一行，但不该在条上占一个零宽段。
  const segments = modes.filter((item) => item.images > 0);

  if (total === 0) {
    return <div className="py-10 text-center text-sm text-neutral-400">该时间段没有成功生成的图片</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-neutral-100">
        {segments.map((item, index) => (
          <Fragment key={item.mode}>
            {/* 2px 的表面色缝隙分开两段，不额外描边。 */}
            {index > 0 ? <div className="h-full w-0.5 shrink-0 bg-white" /> : null}
            <div
              // flexGrow 按成功图片数分配，flexBasis 归零，缝隙的宽度就不会让总长溢出。
              style={{ flexGrow: item.images, flexBasis: 0 }}
              className={cn("h-full", barClass(item.mode))}
              title={`${item.label}：${formatCount(item.images)} 张 · 占比 ${formatRate(item.images / total)}`}
            />
          </Fragment>
        ))}
      </div>

      {/* 这条既是图例也是直标：身份不靠颜色单独承担。 */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 text-xs text-neutral-500 dark:text-neutral-400">
        {segments.map((item) => (
          <span key={item.mode} className="inline-flex items-center gap-1.5">
            <span className={cn("size-2.5 rounded-full", barClass(item.mode))} />
            <span className="text-neutral-600 dark:text-neutral-300">{item.label}</span>
            <span className="tabular-nums">{formatRate(item.images / total)}</span>
          </span>
        ))}
        <span className="ml-auto tabular-nums">共 {formatCount(total)} 张</span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[440px] text-sm">
          <thead>
            <tr className="border-b border-neutral-100 text-xs text-neutral-500 dark:border-white/10 dark:text-neutral-400">
              <th scope="col" className="py-2 pr-3 text-left font-medium">
                调用方式
              </th>
              <th scope="col" className="px-3 py-2 text-right font-medium">
                成功图片
              </th>
              <th scope="col" className="px-3 py-2 text-right font-medium">
                占比
              </th>
              <th scope="col" className="px-3 py-2 text-left font-medium">
                成功率
              </th>
            </tr>
          </thead>
          <tbody>
            {modes.map((item) => {
              // 成功率按成功图片 + 失败算，安全策略拦截不参与分母；
              // 没有图片记录的旧数据回落到成功请求数。
              const successImages = item.images > 0 ? item.images : item.success;
              const rateBase = successImages + item.failed;
              const rate = rateBase > 0 ? successImages / rateBase : 0;
              return (
                <tr
                  key={item.mode}
                  className="border-b border-neutral-100 transition-colors last:border-0 hover:bg-neutral-50/70 dark:border-white/10 dark:hover:bg-white/5"
                >
                  <td className="py-3 pr-3">
                    <span className="inline-flex items-center gap-2 font-medium text-neutral-700 dark:text-neutral-200">
                      <span className={cn("size-2.5 rounded-full", barClass(item.mode))} />
                      {item.label}
                    </span>
                  </td>
                  <td className="px-3 py-3 text-right font-semibold tabular-nums text-neutral-900 dark:text-neutral-50">
                    {formatCount(item.images)}
                  </td>
                  <td className="px-3 py-3 text-right tabular-nums text-neutral-500 dark:text-neutral-400">
                    {formatRate(item.images / total)}
                  </td>
                  <td className="px-3 py-3">
                    <div className="flex items-center gap-2.5">
                      <span className={cn("h-1.5 w-full max-w-[140px] overflow-hidden rounded-full", RATE_TRACK)}>
                        <span
                          className={cn("block h-full rounded-full", RATE_FILL)}
                          style={{ width: `${rate * 100}%` }}
                        />
                      </span>
                      <span className="w-12 shrink-0 tabular-nums text-neutral-700 dark:text-neutral-200">
                        {rateBase > 0 ? formatRate(rate) : "-"}
                      </span>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
