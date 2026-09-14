// Household-time-zone-aware formatting. All packet times are stored UTC;
// parents think in the school's zone, not the browser's.

const DATE_OPTS: Intl.DateTimeFormatOptions = {
  weekday: "short",
  day: "numeric",
  month: "short",
  year: "numeric",
};

const TIME_OPTS: Intl.DateTimeFormatOptions = {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
};

function parse(iso: string): Date | null {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

function fmt(
  d: Date,
  opts: Intl.DateTimeFormatOptions,
  timeZone?: string,
): string {
  try {
    return new Intl.DateTimeFormat("en-GB", { ...opts, timeZone }).format(d);
  } catch {
    return new Intl.DateTimeFormat("en-GB", opts).format(d);
  }
}

export function zoneShortName(iso: string, timeZone?: string): string {
  const d = parse(iso);
  if (d === null) return "";
  try {
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZoneName: "short",
      timeZone,
    }).formatToParts(d);
    return parts.find((p) => p.type === "timeZoneName")?.value ?? "";
  } catch {
    return "";
  }
}

export function formatInZone(iso: string, timeZone?: string): string | null {
  const d = parse(iso);
  if (d === null) return null;
  const tz = zoneShortName(iso, timeZone);
  const base = `${fmt(d, DATE_OPTS, timeZone)}, ${fmt(d, TIME_OPTS, timeZone)}`;
  return tz === "" ? base : `${base} (${tz})`;
}

export function formatRangeInZone(
  startIso: string,
  endIso: string,
  timeZone?: string,
): string | null {
  const start = parse(startIso);
  const end = parse(endIso);
  if (start === null || end === null) return null;
  const tz = zoneShortName(startIso, timeZone);
  const sameDay = fmt(start, DATE_OPTS, timeZone) === fmt(end, DATE_OPTS, timeZone);
  const tzSuffix = tz === "" ? "" : ` (${tz})`;
  if (sameDay) {
    return `${fmt(start, DATE_OPTS, timeZone)}, ${fmt(start, TIME_OPTS, timeZone)}–${fmt(end, TIME_OPTS, timeZone)}${tzSuffix}`;
  }
  return `${formatInZone(startIso, timeZone)} – ${formatInZone(endIso, timeZone)}`;
}
