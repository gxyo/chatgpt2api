const BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000;

/** 北京时间的今天（YYYY-MM-DD），与浏览器所在时区无关。 */
export function getBeijingToday(): string {
  return new Date(Date.now() + BEIJING_OFFSET_MS).toISOString().slice(0, 10);
}

/** 在 YYYY-MM-DD 上加减天数。 */
export function shiftDate(date: string, days: number): string {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
}
