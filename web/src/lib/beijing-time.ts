/**
 * 北京时间（UTC+8）工具。
 *
 * 全站的时间展示都按北京时间，与浏览器所在时区无关。接口返回的时间有两类：
 *
 * - **带时区**（`...Z` / `+08:00`）：后端为机器比较保留的 UTC 时间戳，展示时换算；
 * - **不带时区**：后端写给人看的墙钟时间，本身就是北京时间，按原样理解。
 *
 * 需要机器比较、不用于展示的时间（token 退避、过期判断）不要经过这里。
 */

const BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000;
const BEIJING_TIME_ZONE = "Asia/Shanghai";

/** 解析时，不带时区的文本按哪个时区理解。 */
export type FallbackZone = "beijing" | "utc";

/** 北京时间的今天（YYYY-MM-DD），与浏览器所在时区无关。 */
export function getBeijingToday(): string {
  return new Date(Date.now() + BEIJING_OFFSET_MS).toISOString().slice(0, 10);
}

/** 在 YYYY-MM-DD 上加减天数。 */
export function shiftDate(date: string, days: number): string {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
}

const ZONE_SUFFIX = /(?:Z|[+-]\d{2}:?\d{2})$/i;
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/**
 * 把接口返回的时间文本解析成 Date。
 *
 * 带时区的按原意解析；不带时区的按 `fallbackZone` 补上时区——后端写给人看的
 * 墙钟时间都是北京时间，因此默认按北京时间理解。无法解析时返回 null。
 */
export function parseInstant(value?: string | null, fallbackZone: FallbackZone = "beijing"): Date | null {
  if (!value) {
    return null;
  }
  const text = String(value).trim();
  if (!text) {
    return null;
  }
  if (ZONE_SUFFIX.test(text)) {
    const zoned = new Date(text);
    return Number.isNaN(zoned.getTime()) ? null : zoned;
  }
  // "YYYY-MM-DD HH:MM:SS" 里的空格在部分浏览器里不是合法分隔符，统一换成 T，
  // 再显式补上时区，避免被当成浏览器本地时间。
  const normalized = text.replace(" ", "T");
  const withTime = DATE_ONLY.test(normalized) ? `${normalized}T00:00:00` : normalized;
  const date = new Date(`${withTime}${fallbackZone === "utc" ? "Z" : "+08:00"}`);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** 按北京时间格式化；解析失败时原样返回文本，便于排查。 */
export function formatBeijing(
  value: string | null | undefined,
  options: Intl.DateTimeFormatOptions,
  fallbackZone: FallbackZone = "beijing",
): string {
  const date = parseInstant(value, fallbackZone);
  if (!date) {
    return value ? String(value) : "—";
  }
  return new Intl.DateTimeFormat("zh-CN", { timeZone: BEIJING_TIME_ZONE, ...options }).format(date);
}

/** 北京时间 `YYYY-MM-DD HH:MM:SS`。 */
export function formatBeijingClock(value?: string | null, fallbackZone: FallbackZone = "beijing"): string {
  const date = parseInstant(value, fallbackZone);
  if (!date) {
    return value ? String(value) : "—";
  }
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: BEIJING_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  const pick = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? "";
  return `${pick("year")}-${pick("month")}-${pick("day")} ${pick("hour")}:${pick("minute")}:${pick("second")}`;
}

/** 北京时间 `HH:MM:SS`；解析失败返回空串。 */
export function formatBeijingTimeOfDay(value?: string | null, fallbackZone: FallbackZone = "beijing"): string {
  if (!parseInstant(value, fallbackZone)) {
    return "";
  }
  return formatBeijingClock(value, fallbackZone).slice(11);
}
