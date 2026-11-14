// IANA's Etc/GMT sign is reversed: Etc/GMT-8 is the fixed UTC+8 used across the project.
export const DEFAULT_TIME_ZONE = 'Etc/GMT-8';
// Test-only clock override: the production default stays Date.now(). Tests may pin the
// clock through the server's --now-file argument without changing production behavior.
let nowProvider = () => Date.now();
export function currentTimeMs() {
  return nowProvider();
}

export function setNowProvider(provider: () => number) {
  nowProvider = provider;
}
// Prompts resolve relative dates in DEFAULT_TIME_ZONE, so "now" must carry the same fixed +08:00 offset:
// a UTC timestamp would name the previous calendar date between 16:00 and 24:00 UTC.
export function nowInDefaultZone(ms = nowProvider()) {
  return new Date(ms + 8 * 3600 * 1000).toISOString().replace('Z', '+08:00');
}
// Require explicit offsets; reject invalid dates instead of silently normalizing them.
export function instant(value: unknown) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$/.test(value)) throw new Error('请提供含时区偏移的完整日期时间');
  const date = value.slice(0, 10);
  if (new Date(date).toISOString().slice(0, 10) !== date || !Number.isFinite(Date.parse(value)) ||
      Number(value.slice(11, 13)) > 23 || Number(value.slice(14, 16)) > 59 || Number(value.slice(17, 19)) > 59) throw new Error('日期时间无效');
  return Date.parse(value);
}
export function range(start: unknown, end: unknown): [number, number] {
  const a = instant(start), b = instant(end);
  if (b <= a) throw new Error('结束时间必须晚于开始时间');
  return [a, b];
}
export function zonedClock(timeZone: string) {
  const formatter = new Intl.DateTimeFormat('en-GB', {calendar: 'iso8601', timeZone,
    year: 'numeric', era: 'short', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', timeZoneName: 'longOffset'});
  const offset = (label?: string) => label?.slice(3).replace('−', '-') || '+00:00';
  const offsetOf = (ms: number) => offset(formatter.formatToParts(ms).find(part => part.type === 'timeZoneName')?.value);
  const format = (ms: number) => {
    const parts = Object.fromEntries(formatter.formatToParts(ms).map(({type, value}) => [type, value]));
    // ISO 8601 year 0000 is 1 BC, while 0001 is 1 AD.
    const year = parts.era === 'BC' ? 1 - Number(parts.year) : Number(parts.year);
    const isoYear = year < 0 || year > 9999
      ? (year < 0 ? '-' : '+') + String(Math.abs(year)).padStart(6, '0')
      : String(year).padStart(4, '0');
    return `${isoYear}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}${offset(parts.timeZoneName)}`;
  };
  const resolve = (wall: string) => {
    const utc = instant(wall + 'Z');
    // Check offsets on either side of a possible clock change.
    const day = 86400000;
    const offsets = new Set([offsetOf(utc - day), offsetOf(utc + day)]);
    const matches = [...offsets].map(offset => wall + offset)
      .filter(value => format(instant(value)) === value);
    if (matches.length !== 1) throw new Error('该本地时间不存在或有歧义，请提供明确的时区偏移');
    return matches[0];
  };
  return {format, resolve};
}
// Real Date arithmetic handles month/year boundaries; no hand-written carry logic.
export function addDays(date: string, days: number) {
  const [year, month, day] = date.split('-').map(Number);
  const target = new Date(0);
  target.setUTCFullYear(year, month - 1, day + days);
  return target.toISOString().split('T', 1)[0];
}
const MAX_MINUTES = 525600; // one year, keeps arithmetic inside the representable range
const DAY_OFFSETS: Record<string, number> = {today: 0, tomorrow: 1, dayAfterTomorrow: 2};
type WhenResult =
  | {ok: true; start: string; end?: string}
  | {ok: false; reason: string; missing?: ('day' | 'time')[]};

export function resolveWhen(input: unknown, nowMs = Date.now(), timeZone = DEFAULT_TIME_ZONE): WhenResult {
  if (!input || typeof input !== 'object' || Array.isArray(input)) return {ok: false, reason: '时间参数无效'};
  const spec = input as Record<string, unknown>;
  const wallFields = [spec.day, spec.date, spec.time].some(value => value !== undefined);
  const forms = [wallFields, spec.offsetMinutes !== undefined, spec.absolute !== undefined];
  if (forms.filter(Boolean).length > 1) return {ok: false, reason: '时间指定冲突'};
  if (spec.day !== undefined && spec.date !== undefined) return {ok: false, reason: '日期指定冲突'};
  let durationMs;
  if (spec.durationMinutes !== undefined) {
    if (typeof spec.durationMinutes !== 'number' || !Number.isInteger(spec.durationMinutes) || spec.durationMinutes <= 0 || spec.durationMinutes > MAX_MINUTES)
      return {ok: false, reason: 'durationMinutes 无效'};
    durationMs = spec.durationMinutes * 60000;
  }
  const clock = zonedClock(timeZone);
  let start;
  if (spec.offsetMinutes !== undefined) {
    if (typeof spec.offsetMinutes !== 'number' || !Number.isInteger(spec.offsetMinutes) || Math.abs(spec.offsetMinutes) > MAX_MINUTES)
      return {ok: false, reason: 'offsetMinutes 无效'};
    start = clock.format(nowMs + spec.offsetMinutes * 60000);
  } else if (spec.absolute !== undefined) {
    try {
      start = typeof spec.absolute === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(spec.absolute)
        ? clock.resolve(spec.absolute) : clock.format(instant(spec.absolute));
    } catch (error) {
      return {ok: false, reason: error instanceof Error ? error.message : String(error)};
    }
  } else {
    let date;
    if (spec.day !== undefined) {
      if (typeof spec.day !== 'string' || !Object.hasOwn(DAY_OFFSETS, spec.day)) return {ok: false, reason: '日期格式无效'};
      date = addDays(clock.format(nowMs).slice(0, 10), DAY_OFFSETS[spec.day]);
    } else if (spec.date !== undefined) {
      if (typeof spec.date !== 'string') return {ok: false, reason: '日期格式无效'};
      const parsed = Date.parse(spec.date);
      if (!Number.isFinite(parsed) || new Date(parsed).toISOString().slice(0, 10) !== spec.date)
        return {ok: false, reason: '日期格式无效'};
      date = spec.date;
    } else {
      return {ok: false, missing: ['day'], reason: '请向用户追问日期'};
    }
    if (spec.time === undefined) return {ok: false, missing: ['time'], reason: '请向用户追问具体时刻'};
    if (typeof spec.time !== 'string' || !/^([01]\d|2[0-3]):[0-5]\d$/.test(spec.time))
      return {ok: false, reason: '时间格式无效'};
    try {
      start = clock.resolve(`${date}T${spec.time}:00`);
    } catch (error) {
      return {ok: false, reason: error instanceof Error ? error.message : String(error)};
    }
  }
  return durationMs === undefined ? {ok: true, start} : {ok: true, start, end: clock.format(instant(start) + durationMs)};
}
