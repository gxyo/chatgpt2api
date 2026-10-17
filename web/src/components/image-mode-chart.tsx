"use client";

import { useState } from "react";

import type { ImageStatsBreakdown } from "@/lib/api";
import { cn } from "@/lib/utils";

type ImageModeChartProps = {
  modes: ImageStatsBreakdown[];
};

type ModeTone = {
  /** 图例圆点 / 表格标签用的填充色。 */
  bar: string;
  /** 圆环用的描边色。 */
  stroke: string;
  opacity: number;
};

// 调用方式之间是「身份」而非「量级」，所以用同一主色的两档深浅区分：
// 深蓝是主方式，浅蓝是次方式，整页仍是一套蓝色语言，不再出现黄/品红这类跳色。
// 图例和表头都直接写出名称与百分比，颜色只做辅助，不承担唯一的信息通道。
const MODE_TONES: Record<string, ModeTone> = {
  generate: { bar: "bg-brand", stroke: "stroke-brand", opacity: 1 },
  edit: { bar: "bg-brand/40", stroke: "stroke-brand", opacity: 0.38 },
};
// 认不出的方式走中性灰：不占分类槽位（后端目前只会给出这两种），也不会伪装成一个序列。
const FALLBACK_TONE: ModeTone = {
  bar: "bg-muted-foreground/40",
  stroke: "stroke-muted-foreground",
  opacity: 0.45,
};

// 成功率是「一个比值对一个上限」，画成 meter：填充是蓝，轨道是同一色阶更浅的一档，
// 这样整条都在讲同一件事，不是两个独立序列。
const RATE_FILL = "bg-brand";
const RATE_TRACK = "bg-brand/15";

// 圆环尺寸：够大能看清两段，又不至于把整张卡撑高。
const DONUT_SIZE = 156;
const DONUT_STROKE = 22;
const DONUT_RADIUS = (DONUT_SIZE - DONUT_STROKE) / 2;
const DONUT_CIRCUMFERENCE = 2 * Math.PI * DONUT_RADIUS;

// 页面是静态导出，格式化必须与运行环境无关，否则水合时数字会对不上。
const NUMBER_FORMAT = new Intl.NumberFormat("en-US");

function formatCount(value: number) {
  return NUMBER_FORMAT.format(value);
}

function formatRate(value: number) {
  return `${(value * 100).toFixed(1)}%`;
}

function toneOf(mode: string) {
  return MODE_TONES[mode] ?? FALLBACK_TONE;
}

/**
 * 「按调用方式」的对比视图：左边环形图给整体占比，右边图例直标名称与比例，
 * 下面一张表给准确数字。表本身就是环形图的 table view，颜色不是唯一信息通道。
 */
export function ImageModeChart({ modes }: ImageModeChartProps) {
  // 鼠标悬停的扇区：圈出来的那一段往外凸一点，圆心文字换成它的名称、张数和占比。
  const [hovered, setHovered] = useState<string | null>(null);
  const total = modes.reduce((sum, item) => sum + item.images, 0);
  // 圆环只画有成功图片的方式：0 张的方式在图例和表里保留，但不占一个零宽扇区。
  const segments = modes.filter((item) => item.images > 0);
  const hoveredSegment = segments.find((item) => item.mode === hovered) ?? null;
  const activeMode = hoveredSegment?.mode ?? null;

  if (total === 0) {
    return <div className="py-10 text-center text-sm text-muted-foreground">该时间段没有成功生成的图片</div>;
  }

  // 扇区起点按累计占比往后排，-90° 让第一段从 12 点方向开始。
  let cursor = 0;
  const arcs = segments.map((item) => {
    const length = (item.images / total) * DONUT_CIRCUMFERENCE;
    const arc = { item, length, offset: cursor };
    cursor += length;
    return arc;
  });
  // 两段之间留 2px 缝隙；只有一段时画满整圈，不留缺口。
  const arcGap = segments.length > 1 ? 2 : 0;

  return (
    <div className="space-y-5">
      <div className="flex flex-col items-center gap-6 sm:flex-row sm:gap-7">
        <div className="relative size-[156px] shrink-0">
          {/* overflow-visible：悬停扇区加粗外凸会超出 viewBox，
              默认会被 svg 视口裁掉，圆环看起来就像被削平的方块。 */}
          <svg
            viewBox={`0 0 ${DONUT_SIZE} ${DONUT_SIZE}`}
            className="size-full -rotate-90 overflow-visible"
            role="img"
            aria-label={`成功图片按调用方式分布，共 ${formatCount(total)} 张`}
          >
            <circle
              cx={DONUT_SIZE / 2}
              cy={DONUT_SIZE / 2}
              r={DONUT_RADIUS}
              fill="none"
              strokeWidth={DONUT_STROKE}
              className="stroke-muted"
            />
            {arcs.map(({ item, length, offset }) => {
              const tone = toneOf(item.mode);
              const active = activeMode === item.mode;
              const muted = activeMode !== null && !active;
              // 扇区中点角度（用户坐标系，0 = 3 点钟方向）；悬停时沿这个方向往外推。
              const midAngle = ((offset + length / 2) / DONUT_CIRCUMFERENCE) * Math.PI * 2;
              const pop = active ? 5 : 0;
              return (
                <circle
                  key={item.mode}
                  cx={DONUT_SIZE / 2}
                  cy={DONUT_SIZE / 2}
                  r={DONUT_RADIUS}
                  fill="none"
                  strokeDasharray={`${Math.max(length - arcGap, 0)} ${DONUT_CIRCUMFERENCE}`}
                  strokeDashoffset={-offset}
                  strokeOpacity={muted ? tone.opacity * 0.45 : tone.opacity}
                  className={cn(tone.stroke, "cursor-pointer focus:outline-none")}
                  style={{
                    strokeWidth: active ? DONUT_STROKE + 5 : DONUT_STROKE,
                    transform: `translate(${Math.cos(midAngle) * pop}px, ${Math.sin(midAngle) * pop}px)`,
                    transition:
                      "stroke-width 180ms ease-out, stroke-opacity 180ms ease-out, transform 180ms ease-out",
                  }}
                  onMouseEnter={() => setHovered(item.mode)}
                  onMouseLeave={() => setHovered(null)}
                  onFocus={() => setHovered(item.mode)}
                  onBlur={() => setHovered(null)}
                  tabIndex={0}
                  role="img"
                  aria-label={`${item.label}：${formatCount(item.images)} 张，占比 ${formatRate(item.images / total)}`}
                >
                  <title>
                    {item.label}：{formatCount(item.images)} 张 · 占比 {formatRate(item.images / total)}
                  </title>
                </circle>
              );
            })}
          </svg>
          {/* 圆心：默认是总数；悬停某段时换成那一段的名称、张数和占比。 */}
          <div
            className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center px-6 text-center"
            aria-live="polite"
          >
            {hoveredSegment ? (
              <>
                <span className="text-[11px] text-muted-foreground">{hoveredSegment.label}</span>
                <span className="mt-0.5 text-2xl leading-none font-semibold tracking-tight tabular-nums text-foreground">
                  {formatCount(hoveredSegment.images)}
                </span>
                <span className="mt-1 text-[11px] text-muted-foreground">
                  张 · 占比 {formatRate(hoveredSegment.images / total)}
                </span>
              </>
            ) : (
              <>
                <span className="text-[11px] text-muted-foreground">成功图片</span>
                <span className="mt-0.5 text-2xl leading-none font-semibold tracking-tight tabular-nums text-foreground">
                  {formatCount(total)}
                </span>
                <span className="mt-1 text-[11px] text-muted-foreground">张</span>
              </>
            )}
          </div>
        </div>

        {/* 图例直接写出名称、张数与比例，颜色只是辅助。 */}
        <div className="grid w-full gap-2.5">
          {modes.map((item) => {
            const rate = total > 0 ? item.images / total : 0;
            return (
              <div
                key={item.mode}
                onMouseEnter={() => setHovered(item.mode)}
                onMouseLeave={() => setHovered(null)}
                className={cn(
                  "flex items-center justify-between gap-3 rounded-xl border px-4 py-2.5 transition-colors",
                  hovered === item.mode ? "border-brand/35 bg-brand-soft/70" : "border-border bg-muted/20",
                  item.images === 0 && "opacity-60",
                )}
              >
                <span className="flex min-w-0 items-center gap-2 text-[13px] font-medium text-foreground">
                  <span className={cn("size-2.5 shrink-0 rounded-full", toneOf(item.mode).bar)} />
                  <span className="truncate">{item.label}</span>
                </span>
                <span className="flex shrink-0 items-baseline gap-2 tabular-nums">
                  <span className="text-[15px] font-semibold text-foreground">{formatRate(rate)}</span>
                  <span className="text-[11px] text-muted-foreground">{formatCount(item.images)} 张</span>
                </span>
              </div>
            );
          })}
        </div>
      </div>

      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full min-w-[440px] caption-bottom text-sm">
          <thead className="border-b border-border bg-muted/40 text-xs font-medium text-muted-foreground">
            <tr>
              <th scope="col" className="h-10 px-4 text-left align-middle font-medium whitespace-nowrap">
                调用方式
              </th>
              <th scope="col" className="h-10 px-4 text-right align-middle font-medium whitespace-nowrap">
                成功图片
              </th>
              <th scope="col" className="h-10 px-4 text-right align-middle font-medium whitespace-nowrap">
                占比
              </th>
              <th scope="col" className="h-10 px-4 text-left align-middle font-medium whitespace-nowrap">
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
                  className="border-b border-border/70 transition-colors last:border-0 hover:bg-muted/40"
                >
                  <td className="px-4 py-3.5 align-middle">
                    <span className="inline-flex items-center gap-2 font-medium text-foreground">
                      <span className={cn("size-2.5 rounded-full", toneOf(item.mode).bar)} />
                      {item.label}
                    </span>
                  </td>
                  <td className="px-4 py-3.5 text-right align-middle font-semibold tabular-nums text-foreground">
                    {formatCount(item.images)}
                  </td>
                  <td className="px-4 py-3.5 text-right align-middle tabular-nums text-muted-foreground">
                    {formatRate(item.images / total)}
                  </td>
                  <td className="px-4 py-3.5 align-middle">
                    <div className="flex items-center gap-2.5">
                      <span className={cn("h-1.5 w-full max-w-[140px] overflow-hidden rounded-full", RATE_TRACK)}>
                        <span
                          className={cn("block h-full rounded-full", RATE_FILL)}
                          style={{ width: `${rate * 100}%` }}
                        />
                      </span>
                      <span className="w-12 shrink-0 tabular-nums text-foreground">{rateBase > 0 ? formatRate(rate) : "-"}</span>
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
