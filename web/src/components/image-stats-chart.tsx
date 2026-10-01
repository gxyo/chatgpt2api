"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import type { ImageStatsPoint } from "@/lib/api";
import { cn } from "@/lib/utils";

type ImageStatsChartProps = {
  series: ImageStatsPoint[];
  granularity: "hour" | "day";
};

const CHART_HEIGHT = 288;
const PADDING = { top: 24, right: 18, bottom: 30, left: 46 };
const MAX_X_LABELS = 8;

// 校验过的成对配色（蓝/红，protan/deutan ΔE ≈ 21.6 亮色、19.2 暗色），
// 红绿组合在色觉障碍下不可分辨，因此失败数不用绿色系。
const REQUESTS_STROKE = "stroke-[#2a78d6] dark:stroke-[#3987e5]";
const REQUESTS_FILL = "fill-[#2a78d6] dark:fill-[#3987e5]";
const FAILED_STROKE = "stroke-[#e34948] dark:stroke-[#e66767]";
const FAILED_FILL = "fill-[#e34948] dark:fill-[#e66767]";

// 图例/提示框里的色点是 HTML <span>，不是 SVG 节点 —— fill-* 是 SVG 专用属性，
// 套在 span 上不产生任何可见效果（色点会变成空白），必须用 bg-*。
const REQUESTS_DOT = "bg-[#2a78d6] dark:bg-[#3987e5]";
const FAILED_DOT = "bg-[#e34948] dark:bg-[#e66767]";

function niceScale(max: number) {
  const safeMax = Math.max(1, Math.ceil(max));
  const rough = safeMax / 4;
  const base = 10 ** Math.floor(Math.log10(rough));
  const step = Math.max(1, ([1, 2, 2.5, 5, 10].find((candidate) => candidate * base >= rough) ?? 10) * base);
  const top = Math.ceil(safeMax / step) * step;
  const ticks: number[] = [];
  for (let value = 0; value <= top + 1e-9; value += step) {
    ticks.push(Number(value.toFixed(6)));
  }
  return { max: top, ticks };
}

function formatRate(value: number) {
  return `${(value * 100).toFixed(1)}%`;
}

export function ImageStatsChart({ series, granularity }: ImageStatsChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(0);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);

  useEffect(() => {
    const node = containerRef.current;
    if (!node) {
      return;
    }
    setWidth(node.clientWidth);
    const observer = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect.width ?? 0;
      setWidth(Math.round(next));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const innerWidth = Math.max(0, width - PADDING.left - PADDING.right);
  const innerHeight = CHART_HEIGHT - PADDING.top - PADDING.bottom;

  const scale = useMemo(() => niceScale(Math.max(0, ...series.map((item) => item.requests))), [series]);

  const pointCount = series.length;
  const xAt = (index: number) => {
    if (pointCount <= 1) {
      return PADDING.left + innerWidth / 2;
    }
    return PADDING.left + (innerWidth * index) / (pointCount - 1);
  };
  const yAt = (value: number) => PADDING.top + innerHeight - (innerHeight * Math.min(value, scale.max)) / scale.max;

  const toLine = (pick: (point: ImageStatsPoint) => number) =>
    series
      .map((point, index) => `${index === 0 ? "M" : "L"} ${xAt(index).toFixed(2)} ${yAt(pick(point)).toFixed(2)}`)
      .join(" ");

  // 失败是稀疏事件：整段贴着 0 的红线会被误读成坐标轴，因此只画出现过失败数的区段，
  // 「0 还是缺数据」由 0 基线和面积填充区分。
  const toSparseLine = (pick: (point: ImageStatsPoint) => number) => {
    const subpaths: string[] = [];
    let current: string[] = [];
    series.forEach((point, index) => {
      const value = pick(point);
      const previous = index > 0 ? pick(series[index - 1]) : 0;
      if (value > 0 || previous > 0) {
        if (current.length === 0) {
          current.push(`M ${xAt(Math.max(0, index - 1)).toFixed(2)} ${yAt(previous).toFixed(2)}`);
        }
        current.push(`L ${xAt(index).toFixed(2)} ${yAt(value).toFixed(2)}`);
      } else if (current.length > 0) {
        subpaths.push(current.join(" "));
        current = [];
      }
    });
    if (current.length > 0) {
      subpaths.push(current.join(" "));
    }
    return subpaths.join(" ");
  };

  const requestsLine = toLine((point) => point.requests);
  const failedLine = toSparseLine((point) => point.failed);
  const baseline = yAt(0);
  const requestsArea = pointCount
    ? `${requestsLine} L ${xAt(pointCount - 1).toFixed(2)} ${baseline.toFixed(2)} L ${xAt(0).toFixed(2)} ${baseline.toFixed(2)} Z`
    : "";

  const peakIndex = useMemo(() => {
    let best = -1;
    series.forEach((point, index) => {
      if (point.requests > 0 && (best === -1 || point.requests > series[best].requests)) {
        best = index;
      }
    });
    return best;
  }, [series]);

  const labelStep = Math.max(1, Math.ceil(pointCount / MAX_X_LABELS));
  const active = activeIndex === null ? null : series[activeIndex];
  const activeX = activeIndex === null ? 0 : xAt(activeIndex);
  const tooltipLeft = Math.min(Math.max(activeX, 78), Math.max(78, width - 78));
  const axisLabel = granularity === "hour" ? "小时" : "日期";

  return (
    <div ref={containerRef} className="relative w-full">
      <div className="mb-2 flex flex-wrap items-center justify-end gap-4 text-xs text-neutral-500 dark:text-neutral-400">
        <span className="inline-flex items-center gap-1.5">
          <span className={cn("size-2.5 rounded-full", REQUESTS_DOT)} />
          请求数
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className={cn("size-2.5 rounded-full", FAILED_DOT)} />
          失败数
        </span>
      </div>

      <svg
        width="100%"
        height={CHART_HEIGHT}
        viewBox={`0 0 ${Math.max(width, 1)} ${CHART_HEIGHT}`}
        role="img"
        aria-label={`请求量波形图，按${axisLabel}展示请求数与失败数`}
        onMouseLeave={() => setActiveIndex(null)}
      >
        {scale.ticks.map((tick) => (
          <g key={tick}>
            <line
              x1={PADDING.left}
              x2={PADDING.left + innerWidth}
              y1={yAt(tick)}
              y2={yAt(tick)}
              className={cn("stroke-[1px]", tick === 0 ? "stroke-neutral-300 dark:stroke-white/25" : "stroke-neutral-200/80 dark:stroke-white/10")}
            />
            <text
              x={PADDING.left - 10}
              y={yAt(tick)}
              textAnchor="end"
              dominantBaseline="middle"
              className="fill-neutral-400 text-[11px] tabular-nums dark:fill-neutral-500"
            >
              {tick}
            </text>
          </g>
        ))}

        {series.map((point, index) =>
          index % labelStep === 0 ? (
            <text
              key={point.key}
              x={xAt(index)}
              y={CHART_HEIGHT - 10}
              textAnchor={index === 0 ? "start" : index === pointCount - 1 ? "end" : "middle"}
              className="fill-neutral-400 text-[11px] tabular-nums dark:fill-neutral-500"
            >
              {point.label}
            </text>
          ) : null,
        )}

        {requestsArea ? <path d={requestsArea} className={cn(REQUESTS_FILL, "opacity-[0.14] dark:opacity-[0.18]")} /> : null}
        {pointCount > 1 ? (
          <path
            d={requestsLine}
            fill="none"
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
            className={REQUESTS_STROKE}
          />
        ) : null}
        {failedLine ? (
          <path
            d={failedLine}
            fill="none"
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
            className={FAILED_STROKE}
          />
        ) : null}

        {peakIndex >= 0 && activeIndex === null ? (
          <g>
            <circle
              cx={xAt(peakIndex)}
              cy={yAt(series[peakIndex].requests)}
              r={4.5}
              strokeWidth={2}
              className={cn(REQUESTS_FILL, "stroke-white dark:stroke-neutral-900")}
            />
            <text
              x={Math.min(Math.max(xAt(peakIndex), 26), Math.max(26, width - 26))}
              y={Math.max(14, yAt(series[peakIndex].requests) - 14)}
              textAnchor="middle"
              className="fill-neutral-500 text-[11px] font-medium tabular-nums dark:fill-neutral-300"
            >
              峰值 {series[peakIndex].requests}
            </text>
          </g>
        ) : null}

        {active && activeIndex !== null ? (
          <g>
            <line
              x1={activeX}
              x2={activeX}
              y1={PADDING.top}
              y2={PADDING.top + innerHeight}
              className="stroke-neutral-300 dark:stroke-white/25"
              strokeWidth={1}
            />
            <circle
              cx={activeX}
              cy={yAt(active.requests)}
              r={4.5}
              strokeWidth={2}
              className={cn(REQUESTS_FILL, "stroke-white dark:stroke-neutral-900")}
            />
            {active.requests > 0 ? (
              <circle
                cx={activeX}
                cy={yAt(active.failed)}
                r={4.5}
                strokeWidth={2}
                className={cn(FAILED_FILL, "stroke-white dark:stroke-neutral-900")}
              />
            ) : null}
          </g>
        ) : null}

        <rect
          x={PADDING.left}
          y={PADDING.top}
          width={Math.max(innerWidth, 0)}
          height={innerHeight}
          fill="transparent"
          onMouseMove={(event) => {
            if (pointCount === 0 || innerWidth <= 0) {
              return;
            }
            const rect = event.currentTarget.getBoundingClientRect();
            const ratio = (event.clientX - rect.left) / innerWidth;
            const index = Math.round(Math.min(1, Math.max(0, ratio)) * (pointCount - 1));
            setActiveIndex(index);
          }}
        />
      </svg>

      {active && activeIndex !== null ? (
        <div
          className="pointer-events-none absolute top-6 z-10 w-max -translate-x-1/2 rounded-xl border border-neutral-200 bg-white/95 px-3 py-2 text-xs shadow-sm backdrop-blur dark:border-white/10 dark:bg-neutral-900/95"
          style={{ left: tooltipLeft }}
        >
          <div className="mb-1.5 font-medium text-neutral-500 dark:text-neutral-400">{active.full_label}</div>
          <div className="space-y-1 text-neutral-600 dark:text-neutral-300">
            <div className="flex items-center justify-between gap-4">
              <span className="inline-flex items-center gap-1.5">
                <span className={cn("size-2 rounded-full", REQUESTS_DOT)} />
                请求数
              </span>
              <span className="font-semibold tabular-nums text-neutral-900 dark:text-neutral-100">{active.requests}</span>
            </div>
            <div className="flex items-center justify-between gap-4">
              <span className="inline-flex items-center gap-1.5">
                <span className={cn("size-2 rounded-full", REQUESTS_DOT, "opacity-50")} />
                成功
              </span>
              <span className="tabular-nums">{active.success}</span>
            </div>
            <div className="flex items-center justify-between gap-4">
              <span className="inline-flex items-center gap-1.5">
                <span className={cn("size-2 rounded-full", FAILED_DOT)} />
                失败
              </span>
              <span className="tabular-nums">{active.failed}</span>
            </div>
            <div className="flex items-center justify-between gap-4 border-t border-neutral-100 pt-1 dark:border-white/10">
              <span>成功率</span>
              <span className="tabular-nums">{active.requests ? formatRate(active.success / active.requests) : "-"}</span>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
