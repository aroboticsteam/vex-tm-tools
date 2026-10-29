/** Shape of the `site` system setting that drives the public website, plus
 * pure helpers the config editor uses. Unknown keys are preserved on save. */
export interface SiteSponsor {
  name: string;
  url?: string;
  logo_key?: string;
  logo_url?: string;
}

export interface SiteRoom {
  name: string;
  x: number;
  y: number;
  description?: string;
  teams?: string[];
}

export interface SiteBus {
  name: string;
  status?: string;
  eta?: string;
}

export interface SiteConfig {
  title?: string;
  presented_by?: string;
  next_competition?: {
    name?: string;
    date?: string;
    location?: string;
    url?: string;
    registration_url?: string;
    description?: string;
    teams?: string[];
  };
  sponsors?: SiteSponsor[];
  map?: { image_key?: string; image_url?: string; rooms?: SiteRoom[] };
  leaderboard?: { panel_seconds?: number; programme_seconds?: number };
  recording_url_template?: string;
  bus_tracker?: { title?: string; embed_url?: string; link_url?: string; note?: string; buses?: SiteBus[] };
  [key: string]: unknown;
}

export const DEFAULT_PANEL_SECONDS = 60;
export const DEFAULT_PROGRAMME_SECONDS = 240;

/** `{x, y}` in percent (0–100, one decimal) of a click inside a rectangle. */
export function percentPosition(
  click: { clientX: number; clientY: number },
  rect: { left: number; top: number; width: number; height: number },
): { x: number; y: number } {
  const pct = (value: number, size: number) =>
    size <= 0 ? 0 : Math.round(Math.max(0, Math.min(100, (value / size) * 100)) * 10) / 10;
  return { x: pct(click.clientX - rect.left, rect.width), y: pct(click.clientY - rect.top, rect.height) };
}

/** Splits "1234A, 5678B 9999Z" into trimmed, upper-cased, de-duplicated team numbers. */
export function parseTeamList(text: string): string[] {
  return [...new Set(text.split(/[\s,;]+/).map((t) => t.trim().toUpperCase()).filter(Boolean))];
}

export function isHttpUrl(value: string): boolean {
  return /^https?:\/\/\S+$/i.test(value.trim());
}

/** Problems that would make the public site drop or ignore a field. */
export function siteProblems(config: SiteConfig): string[] {
  const out: string[] = [];
  const check = (label: string, value: string | undefined) => {
    if (value && value.trim() && !isHttpUrl(value)) out.push(`${label} must start with http:// or https://`);
  };
  const nc = config.next_competition;
  check('Next competition link', nc?.url);
  check('Registration link', nc?.registration_url);
  config.sponsors?.forEach((s, i) => check(`Sponsor ${i + 1} link`, s.url));
  check('Bus tracker embed URL', config.bus_tracker?.embed_url);
  check('Bus tracker link', config.bus_tracker?.link_url);
  const template = config.recording_url_template;
  if (template && template.trim() && !isHttpUrl(template.replace(/\{[a-z]+\}/g, 'x'))) {
    out.push('Recording URL template must start with http:// or https://');
  }
  if (nc?.date && nc.date.trim() && !/^\d{4}-\d{2}-\d{2}$/.test(nc.date.trim())) out.push('Next competition date must be YYYY-MM-DD');
  const lb = config.leaderboard;
  for (const [label, value] of [['Panel seconds', lb?.panel_seconds], ['Programme seconds', lb?.programme_seconds]] as const) {
    if (value !== undefined && (!Number.isFinite(value) || value < 5 || value > 3600)) out.push(`${label} must be between 5 and 3600`);
  }
  return out;
}

/** Removes blank optional text so the stored JSON stays tidy. */
export function tidySite(config: SiteConfig): SiteConfig {
  const clean = <T extends object>(obj: T): T => {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(obj)) {
      if (v === '' || v === undefined) continue;
      out[k] = v;
    }
    return out as T;
  };
  const next: SiteConfig = clean({ ...config });
  if (next.next_competition) {
    next.next_competition = clean(next.next_competition);
    if (!next.next_competition.name) delete next.next_competition;
  }
  if (next.sponsors) next.sponsors = next.sponsors.filter((s) => s.name.trim()).map((s) => clean(s));
  if (next.map) {
    const rooms = (next.map.rooms ?? []).filter((r) => r.name.trim()).map((r) => clean(r));
    next.map = clean({ ...next.map, rooms });
    if (!next.map.image_key && !next.map.image_url && !rooms.length) delete next.map;
  }
  if (next.bus_tracker) {
    const buses = (next.bus_tracker.buses ?? []).filter((b) => b.name.trim()).map((b) => clean(b));
    const tracker = clean({ ...next.bus_tracker, buses: buses.length ? buses : undefined });
    if (Object.keys(tracker).length) next.bus_tracker = tracker;
    else delete next.bus_tracker;
  }
  return next;
}
